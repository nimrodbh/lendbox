"""FastAPI application: server-rendered pages, HTML forms, POST then redirect.

Security choices, in one place:
- Session cookie: a random token, stored only as its hash; HttpOnly, SameSite=Lax, Secure when configured.
- CSRF: double-submit. Every visitor gets a random `lb_csrf` cookie; every form posts the same value back, and
  `csrf_protect` rejects a POST whose field does not match. SameSite=Lax is the second layer.
- Messages after a redirect travel as `?msg=<key>` and only catalog keys under `flash.` are ever shown,
  so a crafted link cannot inject text.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lendbox import mail
from lendbox.db import accounts, demo, limits, listings, repo, social
from lendbox.db.connection import connect
from lendbox.domain.auth import InvalidSignup
from lendbox.domain.booking import LOCAL_TZ, BookingEvent, DateRange, format_range
from lendbox.domain.errors import (
    BelowMinimum,
    BookingConflict,
    BookingNotPending,
    EmailTaken,
    Forbidden,
    InvalidCredentials,
    InvalidRange,
    InvalidToken,
    NotFound,
    TooManyAttempts,
    TooManyOpenRequests,
)
from lendbox.domain.listing import (
    InvalidListing,
    attribute_fields,
    coerce_attributes,
    validate_attributes,
    validate_listing,
)
from lendbox.i18n import has, t

ENV = os.environ.get("LENDBOX_ENV", "development")
SESSION_COOKIE = "lb_session"
CSRF_COOKIE = "lb_csrf"
SECURE_COOKIES = os.environ.get("LENDBOX_SECURE_COOKIES") == "1"
DEV_MAIL = os.environ.get("LENDBOX_DEV_MAIL") == "1"
DEMO = os.environ.get("LENDBOX_DEMO") == "1"  # public demo: one-click demo accounts, no sign-up, daily reset  # show the verification link on screen instead of emailing it
BASE_URL = os.environ.get("LENDBOX_BASE_URL", "http://localhost:8000")
if ENV == "production" and (not SECURE_COOKIES or DEV_MAIL):
    # Threat model, threat 14: never serve production with cleartext cookies or on-screen verification links.
    raise RuntimeError("production needs LENDBOX_SECURE_COOKIES=1 and LENDBOX_DEV_MAIL unset")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
CONTACT_EMAIL = os.environ.get("LENDBOX_CONTACT_EMAIL", "contact@lendbox.example")
LEGAL_UPDATED = "October 25, 2026"

web_root = resources.files("lendbox.web")
EXPIRY_EVERY_SECONDS = 30


def _housekeeping() -> None:
    """Close lapsed requests and send queued mail (if a sender is configured)."""
    with connect() as conn:
        if DEMO:
            demo.ensure_fresh(conn, now=datetime.now(UTC))
        repo.expire_due_bookings(conn, now=datetime.now(UTC))
        send = mail.sender()
        if send:
            accounts.deliver_outbox(conn, send=send, now=datetime.now(UTC), base_url=BASE_URL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Every half minute: close lapsed requests (reads already treat them as closed) and drain the outbox."""

    async def loop() -> None:
        while True:
            try:
                await asyncio.to_thread(_housekeeping)
            except psycopg.Error:
                pass  # the database may be restarting; try again next round
            await asyncio.sleep(EXPIRY_EVERY_SECONDS)

    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(title="Lendbox", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(web_root / "static")), name="static")
templates = Jinja2Templates(directory=str(web_root / "templates"))


def ils(agorot: int) -> str:
    """Agorot as shekels for display: 1500 -> '₪15', 1250 -> '₪12.50'."""
    shekels, rest = divmod(agorot, 100)
    return f"₪{shekels:,}" if rest == 0 else f"₪{shekels:,}.{rest:02d}"


templates.env.globals["t"] = t
templates.env.filters["ils"] = ils
templates.env.filters["range"] = format_range


# ---- dependencies -------------------------------------------------------------------------------------------


def get_conn() -> Iterator[psycopg.Connection]:
    with connect() as conn:
        yield conn


def clock() -> datetime:
    return datetime.now(UTC)


Conn = Annotated[psycopg.Connection, Depends(get_conn)]
Now = Annotated[datetime, Depends(clock)]


def current_user(request: Request, conn: Conn, now: Now) -> dict | None:
    raw = request.cookies.get(SESSION_COOKIE)
    return accounts.session_user(conn, raw_token=raw, now=now) if raw else None


User = Annotated[dict | None, Depends(current_user)]


async def csrf_protect(request: Request) -> None:
    form = await request.form()
    sent = str(form.get("csrf_token", ""))
    expected = request.cookies.get(CSRF_COOKIE, "")
    if not expected or not hmac.compare_digest(sent, expected):
        raise HTTPException(status_code=403, detail=t("error.csrf"))


Csrf = Depends(csrf_protect)


@app.middleware("http")
async def csrf_cookie(request: Request, call_next):
    token = request.cookies.get(CSRF_COOKIE)
    fresh = token is None
    request.state.csrf = token or secrets.token_urlsafe(32)
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if fresh:
        response.set_cookie(CSRF_COOKIE, request.state.csrf, httponly=True, samesite="lax", secure=SECURE_COOKIES)
    return response


def page(request: Request, name: str, user: dict | None, status_code: int = 200, **context) -> HTMLResponse:
    msg = request.query_params.get("msg", "")
    flash = t(f"flash.{msg}") if msg and has(f"flash.{msg}") else None
    return templates.TemplateResponse(
        request,
        name,
        {"user": user, "csrf": request.state.csrf, "flash": flash, "demo": DEMO, **context},
        status_code=status_code,
    )


def client_ip(request: Request) -> str:
    """The caller's address. Behind a proxy, uvicorn's --proxy-headers sets this from X-Forwarded-For, and only
    for proxies listed in --forwarded-allow-ips, so a client cannot spoof it."""
    return request.client.host if request.client else ""


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def require_user(user: dict | None, next_url: str) -> dict:
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": f"/login?msg=login_required&next={next_url}"})
    return user


# ---- pages --------------------------------------------------------------------------------------------------


@app.get("/healthz")
def healthz(conn: Conn) -> dict:
    rows = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
    return {"ok": True, "migrations": [r["name"] for r in rows]}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, conn: Conn, user: User):
    comms = repo.communities(conn)
    return page(
        request,
        "home.html",
        user,
        interests=[c for c in comms if c["kind"] == "interest"],
        areas=[c for c in comms if c["kind"] == "area" and c["area_level"] != "country"],
    )


@app.get("/terms", response_class=HTMLResponse)
@app.get("/privacy", response_class=HTMLResponse)
@app.get("/guidelines", response_class=HTMLResponse)
def legal(request: Request, user: User):
    name = request.url.path.strip("/")
    return page(request, f"legal/{name}.html", user, contact_email=CONTACT_EMAIL, updated=LEGAL_UPDATED)


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, user: User):
    if DEMO:
        return page(request, "message.html", user, text=t("demo.signup_off"))
    return page(request, "signup.html", user, values={}, error=None)


@app.post("/demo/{role}", dependencies=[Csrf])
def demo_login(request: Request, role: str, conn: Conn, now: Now):
    """Sign the visitor in as the demo lender or borrower. Only exists in demo mode."""
    if not DEMO or role not in ("lender", "borrower"):
        raise HTTPException(status_code=404)
    try:
        limits.hit(conn, limits.DEMO_LOGIN_PER_IP, client_ip(request), now=now)
        user_id = demo.demo_user(conn, role=role, now=now)
    except limits.RateLimited:
        return redirect("/?error=error.rate_limited")
    raw = accounts.create_session(conn, user_id=user_id, now=now)
    response = redirect("/lending" if role == "lender" else "/borrowing")
    response.set_cookie(
        SESSION_COOKIE, raw, max_age=int(accounts.SESSION_TTL.total_seconds()),
        httponly=True, samesite="lax", secure=SECURE_COOKIES,
    )
    return response


@app.post("/signup", response_class=HTMLResponse, dependencies=[Csrf])
def signup(
    request: Request,
    conn: Conn,
    now: Now,
    user: User,
    email: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    if DEMO:
        return page(request, "message.html", user, 403, text=t("demo.signup_off"))
    values = {"email": email, "display_name": display_name}
    try:
        limits.hit(conn, limits.SIGNUP_PER_IP, client_ip(request), now=now)
        user_id = accounts.sign_up(conn, email=email, display_name=display_name, password=password, now=now)
    except limits.RateLimited:
        return page(request, "message.html", user, 429, text=t("error.rate_limited"))
    except InvalidSignup as exc:
        return page(request, "signup.html", user, 422, values=values, error={"field": exc.field, "key": exc.key})
    except EmailTaken:
        error = {"field": "email", "key": "signup.error.taken"}
        return page(request, "signup.html", user, 409, values=values, error=error)
    dev_link = None
    if DEV_MAIL:  # local only: mint a link to show on screen; production sends it by mail and never shows it
        dev_link = f"/verify?token={accounts.issue_verification(conn, user_id=user_id, now=now)}"
    return page(request, "check_email.html", user, email=email.strip().lower(), dev_link=dev_link)


@app.get("/verify")
def verify(conn: Conn, now: Now, token: str = ""):
    try:
        accounts.verify_email(conn, raw_token=token, now=now)
    except InvalidToken:
        return redirect("/login?msg=verify_failed")
    return redirect("/login?msg=verified")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, user: User, next: str = "/browse"):
    return page(request, "login.html", user, values={}, error=None, next=_safe_next(next))


@app.post("/login", dependencies=[Csrf])
def login(
    request: Request,
    conn: Conn,
    now: Now,
    user: User,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/browse",
):
    try:
        user_id = accounts.authenticate(conn, email=email, password=password, now=now, ip=client_ip(request))
    except (InvalidCredentials, TooManyAttempts) as exc:
        key = "login.error.throttled" if isinstance(exc, TooManyAttempts) else "login.error.invalid"
        status = 429 if isinstance(exc, TooManyAttempts) else 401
        return page(request, "login.html", user, status, values={"email": email}, error=key, next=_safe_next(next))
    raw = accounts.create_session(conn, user_id=user_id, now=now)
    response = redirect(_safe_next(next))
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=int(accounts.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=SECURE_COOKIES,
    )
    return response


@app.post("/logout", dependencies=[Csrf])
def logout(request: Request, conn: Conn):
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        accounts.end_session(conn, raw_token=raw)
    response = redirect("/?msg=logged_out")
    response.delete_cookie(SESSION_COOKIE)
    return response


def _safe_next(url: str) -> str:
    """Only same-site paths: '/items/3' yes, '//evil.example' or 'https://...' no (open-redirect guard)."""
    return url if url.startswith("/") and not url.startswith("//") else "/browse"


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value and value.strip().isdigit() else None


def _date_or_none(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@app.get("/browse", response_class=HTMLResponse)
def browse(
    request: Request,
    conn: Conn,
    user: User,
    q: str = "",
    start: str = "",
    last_day: str = "",
    area: str = "",
    interest: str = "",
    category: str = "",
    max_price: str = "",
):
    first, last = _date_or_none(start), _date_or_none(last_day)
    rng = DateRange.from_inclusive(first, last) if first and last and first <= last else None
    max_shekels = _int_or_none(max_price)
    results = repo.search_listings(
        conn,
        area_id=_int_or_none(area),
        interest_id=_int_or_none(interest),
        category_id=_int_or_none(category),
        start=rng.start if rng else None,
        end=rng.end if rng else None,
        max_price_agorot=max_shekels * 100 if max_shekels is not None else None,
        q=q or None,
    )
    comms = repo.communities(conn)
    return page(
        request,
        "browse.html",
        user,
        results=results,
        rng=rng,
        areas=[c for c in comms if c["kind"] == "area"],
        interests=[c for c in comms if c["kind"] == "interest"],
        categories=repo.categories(conn),
        form={
            "q": q, "start": start, "last_day": last_day, "area": area,
            "interest": interest, "category": category, "max_price": max_price,
        },
    )


@app.get("/items/{listing_id}", response_class=HTMLResponse)
def item(request: Request, listing_id: int, conn: Conn, now: Now, user: User, error: str = "", min: str = ""):
    try:
        listing = repo.listing_detail(conn, listing_id=listing_id, today=now.astimezone(LOCAL_TZ).date())
    except NotFound:
        return page(request, "not_found.html", user, 404)
    show_pickup = user is not None and (
        user["id"] == listing["owner_id"]
        or repo.borrower_has_approved_loan(conn, listing_id=listing_id, user_id=user["id"])
    )
    error_text = t(error, min=min or "1") if error.startswith("error.") and has(error) else None
    owner_rating = social.rating(conn, user_id=listing["owner_id"], now=now)
    return page(
        request, "item.html", user, listing=listing, show_pickup=show_pickup, error=error_text, rating=owner_rating
    )


@app.post("/items/{listing_id}/requests", dependencies=[Csrf])
def request_dates(
    listing_id: int,
    conn: Conn,
    now: Now,
    user: User,
    start: Annotated[str, Form()],
    last_day: Annotated[str, Form()],
    message: Annotated[str, Form()] = "",
):
    user = require_user(user, f"/items/{listing_id}")
    back = f"/items/{listing_id}"
    if not user["verified"]:
        return redirect(f"{back}?error=error.not_verified")
    first, last = _date_or_none(start), _date_or_none(last_day)
    if not first or not last or last < first:
        return redirect(f"{back}?error=error.range")
    try:
        limits.hit(conn, limits.REQUESTS_PER_USER, user["id"], now=now)
        repo.request_booking(
            conn,
            listing_id=listing_id,
            borrower_id=user["id"],
            start=first,
            end=last + timedelta(days=1),
            message=message.strip()[:2000],
            now=now,
        )
    except Forbidden:
        return redirect(f"{back}?error=error.own_item")
    except InvalidRange:
        return redirect(f"{back}?error=error.range")
    except BelowMinimum as exc:
        return redirect(f"{back}?error=error.minimum&min={exc.min_units}")
    except NotFound:
        return redirect(f"{back}?error=error.not_found")
    except TooManyOpenRequests:
        return redirect(f"{back}?error=error.too_many_requests")
    except limits.RateLimited:
        return redirect(f"{back}?error=error.rate_limited")
    return redirect(f"{back}?msg=request_sent")


@app.get("/lending", response_class=HTMLResponse)
def lending(request: Request, conn: Conn, now: Now, user: User, tab: str = "requests", error: str = ""):
    user = require_user(user, "/lending")
    inbox = repo.lender_inbox(conn, owner_id=user["id"], now=now)
    for r in inbox["requests"]:
        r["hours_left"] = max(0, int((r["expires_at"] - now).total_seconds() // 3600))
    tab = tab if tab in {"requests", "out", "items"} else "requests"
    error_text = t(error) if error.startswith("error.") and has(error) else None
    today = now.astimezone(LOCAL_TZ).date()
    return page(request, "lending.html", user, inbox=inbox, tab=tab, error=error_text, today=today)


_LIFECYCLE = {
    "cancel": (BookingEvent.CANCEL, "cancelled"),
    "pickup": (BookingEvent.PICKUP, "picked_up"),
    "return": (BookingEvent.RETURN, "returned"),
}


def booking_action(
    request: Request, booking_id: int, conn: Conn, now: Now, user: User, back: Annotated[str, Form()] = "/lending"
):
    """One handler for the five state changes; registered on explicit paths below so it never shadows
    /bookings/{id}/messages or /bookings/{id}/review."""
    action = request.url.path.rsplit("/", 1)[1]
    user = require_user(user, "/lending")
    back = _safe_next(back)
    sep = "&" if "?" in back else "?"
    try:
        if action == "approve":
            repo.approve_booking(conn, booking_id=booking_id, owner_id=user["id"], now=now)
            return redirect(f"{back}{sep}msg=approved")
        if action == "decline":
            repo.decline_booking(conn, booking_id=booking_id, owner_id=user["id"], now=now)
            return redirect(f"{back}{sep}msg=declined")
        if action in _LIFECYCLE:
            event, flash = _LIFECYCLE[action]
            repo.act_on_booking(conn, booking_id=booking_id, user_id=user["id"], event=event, now=now)
            return redirect(f"{back}{sep}msg={flash}")
    except BookingConflict:
        return redirect(f"{back}{sep}error=error.conflict")
    except BookingNotPending:
        return redirect(f"{back}{sep}error=error.not_pending")
    except (Forbidden, NotFound):
        return redirect(f"{back}{sep}error=error.forbidden")
    return Response(status_code=404)


for _action in ("approve", "decline", *_LIFECYCLE):
    app.add_api_route(
        f"/bookings/{{booking_id}}/{_action}", booking_action, methods=["POST"], dependencies=[Csrf]
    )


# ---- borrowing, threads, reviews ----------------------------------------------------------------------------


@app.get("/borrowing", response_class=HTMLResponse)
def borrowing(request: Request, conn: Conn, now: Now, user: User, error: str = ""):
    user = require_user(user, "/borrowing")
    rows = repo.borrower_bookings(conn, user_id=user["id"], now=now)
    return page(request, "borrowing.html", user, bookings=rows, error=_error_text(error))


@app.get("/bookings/{booking_id}", response_class=HTMLResponse)
def thread(request: Request, booking_id: int, conn: Conn, now: Now, user: User, error: str = ""):
    user = require_user(user, f"/bookings/{booking_id}")
    try:
        b = social.booking_thread(conn, booking_id=booking_id, user_id=user["id"])
    except NotFound:
        return page(request, "not_found.html", user, 404)
    today = now.astimezone(LOCAL_TZ).date()
    return page(request, "thread.html", user, b=b, today=today, error=_error_text(error))


@app.post("/bookings/{booking_id}/messages", dependencies=[Csrf])
def message(booking_id: int, conn: Conn, now: Now, user: User, body: Annotated[str, Form()] = ""):
    user = require_user(user, f"/bookings/{booking_id}")
    try:
        limits.hit(conn, limits.MESSAGES_PER_USER, user["id"], now=now)
        social.post_message(conn, booking_id=booking_id, sender_id=user["id"], body=body, now=now)
    except NotFound:
        return redirect(f"/bookings/{booking_id}?error=error.thread_closed")
    except limits.RateLimited:
        return redirect(f"/bookings/{booking_id}?error=error.rate_limited")
    return redirect(f"/bookings/{booking_id}#messages")


@app.get("/bookings/{booking_id}/review", response_class=HTMLResponse)
def review_form(request: Request, booking_id: int, conn: Conn, user: User):
    user = require_user(user, f"/bookings/{booking_id}/review")
    try:
        b = social.booking_thread(conn, booking_id=booking_id, user_id=user["id"])
    except NotFound:
        return page(request, "not_found.html", user, 404)
    return page(request, "review.html", user, b=b)


@app.post("/bookings/{booking_id}/review", dependencies=[Csrf])
def review(
    booking_id: int,
    conn: Conn,
    now: Now,
    user: User,
    rating: Annotated[int, Form()],
    body: Annotated[str, Form()] = "",
):
    user = require_user(user, f"/bookings/{booking_id}/review")
    try:
        social.submit_review(conn, booking_id=booking_id, author_id=user["id"], rating=rating, body=body, now=now)
    except social.ReviewNotAllowed:
        return redirect(f"/bookings/{booking_id}?error=error.review")
    return redirect(f"/bookings/{booking_id}?msg=reviewed")


# ---- listing a new item -------------------------------------------------------------------------------------


@app.get("/listings/new", response_class=HTMLResponse)
def new_listing(request: Request, conn: Conn, user: User, category: str = ""):
    user = require_user(user, "/listings/new")
    if not user["verified"]:
        return page(request, "message.html", user, 403, text=t("error.not_verified"))
    category_id = _int_or_none(category)
    if category_id is None:
        return page(request, "listing_category.html", user, categories=repo.categories(conn))
    try:
        cat = listings.category_for_form(conn, category_id)
    except InvalidListing:
        return redirect("/listings/new")
    return _listing_form(request, conn, user, cat, values={}, error=None)


def _listing_form(request, conn, user, cat, values, error, status_code=200):
    comms = repo.communities(conn)
    return page(
        request,
        "listing_form.html",
        user,
        status_code,
        cat=cat,
        fields=attribute_fields(cat["attribute_schema"]),
        areas=[c for c in comms if c["kind"] == "area" and c["area_level"] != "country"],
        interests=[c for c in comms if c["kind"] == "interest"],
        values=values,
        error=error,
    )


@app.post("/listings/new", dependencies=[Csrf])
async def create_listing(request: Request, conn: Conn, now: Now, user: User):
    user = require_user(user, "/listings/new")
    if not user["verified"]:
        return page(request, "message.html", user, 403, text=t("error.not_verified"))
    form = await request.form()
    values = {k: str(v) for k, v in form.items() if k != "interests"}
    values["interests"] = ",".join(str(v) for v in form.getlist("interests"))
    try:
        cat = listings.category_for_form(conn, int(values.get("category", "0")))
    except (InvalidListing, ValueError):
        return redirect("/listings/new")
    comms = repo.communities(conn)
    try:
        fields = validate_listing(
            values,
            area_ids={c["id"] for c in comms if c["kind"] == "area" and c["area_level"] != "country"},
            interest_ids={c["id"] for c in comms if c["kind"] == "interest"},
        )
        attributes = coerce_attributes(cat["attribute_schema"], values)
        validate_attributes(cat["attribute_schema"], attributes)
    except InvalidListing as exc:
        return _listing_form(request, conn, user, cat, values, {"field": exc.field, "key": exc.key}, 422)
    listing_id = listings.create_listing(
        conn, owner_id=user["id"], category_id=cat["id"], fields=fields, attributes=attributes, now=now
    )
    return redirect(f"/listings/{listing_id}/manage?msg=listing_saved")


@app.get("/listings/{listing_id}/manage", response_class=HTMLResponse)
def manage_listing(request: Request, listing_id: int, conn: Conn, now: Now, user: User, error: str = ""):
    user = require_user(user, f"/listings/{listing_id}/manage")
    try:
        listing = listings.owner_listing(conn, listing_id=listing_id, owner_id=user["id"])
    except NotFound:
        return page(request, "not_found.html", user, 404)
    blocks = listings.owner_blocks(conn, listing_id=listing_id, today=now.astimezone(LOCAL_TZ).date())
    return page(request, "manage.html", user, listing=listing, blocks=blocks, error=_error_text(error))


@app.post("/listings/{listing_id}/publish", dependencies=[Csrf])
def publish(listing_id: int, conn: Conn, now: Now, user: User):
    user = require_user(user, f"/listings/{listing_id}/manage")
    back = f"/listings/{listing_id}/manage"
    try:
        listings.publish_listing(conn, listing_id=listing_id, owner_id=user["id"], now=now)
    except InvalidListing as exc:
        return redirect(f"{back}?error={exc.key}")
    except NotFound:
        return Response(status_code=404)
    return redirect(f"{back}?msg=published")


@app.post("/listings/{listing_id}/blocks", dependencies=[Csrf])
def block_dates(
    listing_id: int, conn: Conn, user: User, start: Annotated[str, Form()], last_day: Annotated[str, Form()]
):
    user = require_user(user, f"/listings/{listing_id}/manage")
    back = f"/listings/{listing_id}/manage"
    first, last = _date_or_none(start), _date_or_none(last_day)
    if not first or not last or last < first:
        return redirect(f"{back}?error=error.range")
    try:
        listings.add_block(conn, listing_id=listing_id, owner_id=user["id"], start=first, end=last + timedelta(days=1))
    except BookingConflict:
        return redirect(f"{back}?error=error.conflict")
    except (Forbidden, NotFound):
        return Response(status_code=404)
    return redirect(f"{back}?msg=blocked")


@app.post("/blocks/{block_id}/remove", dependencies=[Csrf])
def unblock(block_id: int, conn: Conn, user: User, back: Annotated[str, Form()] = "/lending?tab=items"):
    user = require_user(user, "/lending")
    back = _safe_next(back)
    try:
        listings.remove_block(conn, booking_id=block_id, owner_id=user["id"])
    except NotFound:
        return Response(status_code=404)
    return redirect(f"{back}{'&' if '?' in back else '?'}msg=unblocked")


# ---- communities and Looking for ----------------------------------------------------------------------------


@app.get("/c/{slug}", response_class=HTMLResponse)
def community_page(request: Request, slug: str, conn: Conn, user: User, area: str = ""):
    try:
        c = social.community(conn, slug=slug, user_id=user["id"] if user else None)
    except NotFound:
        return page(request, "not_found.html", user, 404)
    comms = repo.communities(conn)
    if c["kind"] == "area":
        area_id, interest_id = c["id"], None
    else:
        area_id, interest_id = _int_or_none(area), c["id"]
    results = repo.search_listings(conn, area_id=area_id, interest_id=interest_id)
    area_ids = repo.area_subtree_ids(conn, area_id) if area_id else None
    posts = social.looking_for(conn, area_ids=area_ids, interest_id=interest_id, limit=6)
    return page(
        request,
        "community.html",
        user,
        c=c,
        results=results,
        posts=posts,
        areas=[x for x in comms if x["kind"] == "area"],
        selected_area=area,
    )


@app.post("/c/{slug}/join", dependencies=[Csrf])
def join(slug: str, conn: Conn, user: User):
    user = require_user(user, f"/c/{slug}")
    try:
        c = social.community(conn, slug=slug, user_id=user["id"])
    except NotFound:
        return Response(status_code=404)
    joined = social.toggle_membership(conn, community_id=c["id"], user_id=user["id"])
    return redirect(f"/c/{slug}?msg={'joined' if joined else 'left'}")


@app.get("/looking-for", response_class=HTMLResponse)
def looking_for(request: Request, conn: Conn, user: User, error: str = ""):
    comms = repo.communities(conn)
    return page(
        request,
        "looking_for.html",
        user,
        posts=social.looking_for(conn, limit=60),
        areas=[c for c in comms if c["kind"] == "area" and c["area_level"] != "country"],
        interests=[c for c in comms if c["kind"] == "interest"],
        error=_error_text(error),
    )


@app.post("/looking-for", dependencies=[Csrf])
def post_looking_for(
    conn: Conn,
    now: Now,
    user: User,
    title: Annotated[str, Form()],
    area: Annotated[str, Form()],
    body: Annotated[str, Form()] = "",
    interest: Annotated[str, Form()] = "",
    start: Annotated[str, Form()] = "",
    last_day: Annotated[str, Form()] = "",
):
    user = require_user(user, "/looking-for")
    if not user["verified"]:
        return redirect("/looking-for?error=error.not_verified")
    first, last = _date_or_none(start), _date_or_none(last_day)
    wanted = (first, last + timedelta(days=1)) if first and last and first <= last else None
    area_id = _int_or_none(area)
    if area_id is None:
        return redirect("/looking-for?error=error.looking_title")
    try:
        limits.hit(conn, limits.POSTS_PER_USER, user["id"], now=now)
        social.post_looking_for(
            conn, author_id=user["id"], area_id=area_id, interest_id=_int_or_none(interest),
            title=title, body=body, wanted=wanted, now=now,
        )
    except (ValueError, psycopg.errors.ForeignKeyViolation):
        return redirect("/looking-for?error=error.looking_title")
    except limits.RateLimited:
        return redirect("/looking-for?error=error.rate_limited")
    return redirect("/looking-for?msg=posted")


# ---- reports and the admin queue ----------------------------------------------------------------------------


@app.post("/reports", dependencies=[Csrf])
def report(
    conn: Conn,
    now: Now,
    user: User,
    target_type: Annotated[str, Form()],
    target_id: Annotated[int, Form()],
    reason: Annotated[str, Form()],
    back: Annotated[str, Form()] = "/",
):
    back = _safe_next(back)
    user = require_user(user, back)
    sep = "&" if "?" in back else "?"
    try:
        limits.hit(conn, limits.REPORTS_PER_USER, user["id"], now=now)
        social.file_report(
            conn, reporter_id=user["id"], target_type=target_type, target_id=target_id, reason=reason, now=now
        )
    except limits.RateLimited:
        return redirect(f"{back}{sep}error=error.rate_limited")
    except (ValueError, psycopg.errors.ForeignKeyViolation):
        return redirect(f"{back}{sep}error=error.forbidden")
    return redirect(f"{back}{sep}msg=reported")


def require_admin(user: dict | None) -> dict:
    """Non-admins get 404, so the admin area does not announce itself."""
    if user is None or not user["is_admin"]:
        raise HTTPException(status_code=404)
    return user


@app.get("/admin/reports", response_class=HTMLResponse)
def admin_reports(request: Request, conn: Conn, user: User):
    user = require_admin(user)
    return page(request, "admin_reports.html", user, reports=social.open_reports(conn))


@app.post("/admin/reports/{report_id}/{action}", dependencies=[Csrf])
def admin_resolve(report_id: int, action: str, conn: Conn, now: Now, user: User):
    user = require_admin(user)
    if action not in ("hidden", "dismissed"):
        raise HTTPException(status_code=404)
    try:
        social.resolve_report(conn, report_id=report_id, admin_id=user["id"], action=action, now=now)
    except NotFound:
        raise HTTPException(status_code=404) from None
    return redirect("/admin/reports?msg=resolved")


def _error_text(key: str) -> str | None:
    return t(key) if key.startswith(("error.", "listing.error.")) and has(key) else None
