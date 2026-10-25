# Lendbox v1 Threat Model and Abuse Review

- **Contracts:** 0.5 · **Date:** 2026-10-25 · **Status:** all ten items in section 5 built (A-4); photo upload rules apply when uploads are built
- **Authors:** drafted from the schema and contracts; every control living in Python was then checked against the code (see "Verification" at the end). Labels below: **Verified (SQL)** = seen in the migrations; **Verified (code)** = confirmed in the Python source.

## 1. Scope and assets

Lendbox v1 is a server-rendered FastAPI and PostgreSQL neighborhood lending site where strangers arrange in-person loans of physical items. Prices and deposits are settled in person (cash, Bit or PayBox); there is no payment processor.

We protect six assets:
1. **Accounts and credentials:** `users.password_hash`, sessions (`sessions.token_hash`), verification tokens (`email_tokens.token_hash`). Compromise lets an attacker impersonate a trusted neighbor.
2. **Calendar integrity:** `bookings.during` and the `no_double_booking` exclusion constraint. A double-booked tent means a confrontation at pickup.
3. **The private pickup spot (`listings.pickup_detail`):** hidden until the owner approves. Price, deposit and area already advertise what valuable gear lives in which neighborhood, so an early leak is a burglary map.
4. **Physical safety at handover:** robbery, extortion or stalking when strangers meet.
5. **Community trust:** blind reviews, Looking for posts, categories and reports. If scammers, prohibited items or fake ratings dominate browse and community pages, honest neighbors leave.
6. **Availability:** a cheap script must not exhaust connections, fill the disk, or lock members out during a 48-hour request window.

## 2. Actors

- **Honest member:** sometimes slow to answer or late to return; moves to WhatsApp as soon as a booking is approved.
- **Scammer or thief:** disposable email, requests high-value gear with a low deposit or pays with a reversible transfer; or posts bait listings to collect deposits in advance.
- **Harasser or stalker:** abuses request messages, threads or Looking for; or requests an item only to learn the owner's pickup spot.
- **Scraper:** crawls browse, community and item pages for names, inventories and prices.
- **Griefer or competitor:** floods owners with requests (date hoarding) or abuses the login throttle to lock owners out.
- **Attacker with a stolen session:** shared computer, XSS, or cleartext cookie if `LENDBOX_SECURE_COOKIES` is not set.
- **Insider or leaked backup:** read access to a database dump.

## 3. Threats

| # | Threat | STRIDE | L | I | Existing control | Gap | Mitigation | Effort |
|---|---|---|---|---|---|---|---|---|
| 1 | Fake or bait listings asking for an advance Bit or PayBox deposit | S, F | M | H | Verified (code): unverified users cannot list; `reports.listing_id`; `listings.status = 'hidden'` | Verification is free; nothing warns against paying before pickup | Banner on item and booking pages: never transfer money before holding the item; flag messages that mention Bit, PayBox or a phone number; admin reports page | S |
| 2 | Inbox flooding and date hoarding with many open requests | D | H | M | Verified (SQL): `requested` rows do not hold dates; loans expire after 48 h; approval auto-declines overlaps | Verified (code): no cap on open requests per borrower | `request_booking`: at most 1 open request per (borrower, listing) and 5 overall | S |
| 3 | Approve then no-show or last-minute cancel | D | M | M | Verified (code): borrower may cancel only before the start day | Verified (code): the owner cannot cancel on or after the start day either, so a no-show holds the dates | Let the owner cancel an approved, not-picked-up loan on or after the start day (domain change plus tests) | S |
| 4 | Damage and deposit disputes | R, T | H | M | Verified (SQL): condition, deposit, late fee, `picked_up_at`, `returned_at` | No condition evidence; an owner in a dispute can refuse to mark returned and so block the borrower's review | Handover checklist at pickup and return; let the borrower report if the owner does not mark returned within 48 h after the end | M |
| 5 | Theft at handover (deposit below replacement value) | T, E | M | H | Verified (SQL): deposit must be positive; every loan is approved by hand | A small deposit on an expensive item invites theft | Warn on low deposits; show borrower account age and completed loans on request cards; guidelines on meeting safely | S |
| 6 | Harassment in messages | T | M | M | Verified (code): only the two parties can post; messages capped at 2,000 characters | Verified (code): threads stay writable after decline, cancel or expiry; no block feature | Read-only threads once declined, expired or cancelled, or 14 days after return; per-user message rate limit | S |
| 7 | Spam and phishing in Looking for | T, D | M | M | Verified (code): verified users only; title 3 to 80; body truncated to 1,000 | No rate limit | At most 3 open posts per user; DB length bound | S |
| 8 | Scraping listings and names | I | H | M | Verified (code): emails never shown; pickup spot hidden; names are first name and initial by convention | Sequential ids and no rate limit on public pages | Per-IP rate limit on browse, community and item pages | S |
| 9 | Id enumeration on bookings, drafts, threads | I | H | M | Verified (code): non-parties get 404 on threads and lifecycle actions; drafts visible only to their owner | Not every endpoint has a test proving the identical 404 | One parametrized test over every booking and block endpoint as a stranger | S |
| 10 | Credential stuffing across many emails | S | H | H | Verified (code): scrypt, dummy hash for unknown emails, 5 failures per email per 15 min | Per-email throttling does not stop one IP trying thousands of emails, and each attempt costs a scrypt computation (CPU) plus a `login_failures` row | Per-IP failure budget checked before scrypt; prune `login_failures` | M |
| 11 | Lockout as denial of service against a known email | D | M | H | Verified (code): `login_failures` keyed on email only | Anyone who knows an owner's email can keep them locked out past their 48-hour request windows | Strict limit per (email, IP); a looser global limit per email; or a one-time sign-in link when locked | M |
| 12 | CSRF | S, E | L | H | Verified (code): double-submit token compared with `hmac.compare_digest`; every POST route carries the check (checked by enumerating routes); SameSite=Lax | Cookie tossing from a sibling subdomain could plant `lb_csrf` | Keep the route check as a test; bind the token to the session if subdomains are ever shared | S |
| 13 | Stored XSS through user text | T, E | L | H | Verified (code): Jinja autoescape; no user value in `href` or `src`; flash only from catalog keys | No Content-Security-Policy | CSP and security headers; when photos arrive, validate paths and never render user URLs | S |
| 14 | Session theft | S, I | M | H | Verified (SQL and code): only token hashes stored; HttpOnly, SameSite=Lax; Secure when configured | Deploy could forget `LENDBOX_SECURE_COOKIES=1`; no "sign out everywhere" | Refuse to start in production without secure cookies; sign out everywhere action | S |
| 15 | Review farming with sock-puppet pairs | T, S | M | M | Verified (SQL and code): one review per party per returned loan; subject derived server-side; blind visibility | Two colluding accounts can run many instant loans and swap 5-star reviews | Count one rating per (author, subject) pair in the average; require a minimum time between pickup and return | S |
| 16 | Prohibited item in an allowed category | T | M | H | Verified (code): prohibited categories cannot be listed or published | Free text is not checked ("electric scooter" under Sports) | Keyword check on publish (English and Hebrew) that blocks or files a report; admin reports page | S |
| 17 | Location leak (pickup spot, phone or address in public text) | I | M | H | Verified (code): pickup spot hidden until approval; areas stop at neighborhood or campus | Owners may paste a phone or street address into the public description | Warn or block phone and street patterns in description on publish; helper text suggests a landmark | S |
| 18 | Oversized text and outbox flooding | D | M | L | Verified (code): every user write path truncates (descriptions 2,000, pickup 200, messages 2,000, reviews and posts 1,000) | DB has no upper bound on those columns (defense in depth only); signup can queue unlimited emails | Migration adding length bounds; rate-limit signup and verification resends | S |
| 19 | Malicious photo upload (when built) | T, D, E | M | H | Upload not built | SVG or HTML payloads, huge files, EXIF GPS of the owner's home | Re-encode with Pillow to JPEG or WebP, strip EXIF, 5 MB cap, random filenames, `nosniff`, at most 8 photos | M |
| 20 | Leaked database backup | I | L | H | Verified (SQL): passwords scrypt-hashed; session and verification tokens stored as hashes | **Verified (code): `email_outbox.body` stores the verification link with the raw token**, so a dump can verify any pending account | Redact the body once sent, or store template parameters and build the link only at send time | S |

## 4. Product problems, not code problems

v1 takes no payments, holds no escrow, verifies no IDs and insures nothing. Once a booking is approved, most pairs will swap numbers and move to WhatsApp, and settle money in cash, Bit or PayBox. Code cannot stop a dropped projector, so the product sets rules, keeps a record, and says so plainly.

1. **WhatsApp is where people will talk.** The booking page says: coordinate wherever is convenient, but record date changes, condition notes and the deposit amount in the Lendbox thread, because reports are judged on the booking record.
2. **No-shows.** No money moves before pickup. On or after the start day, if the owner has not marked pickup, the owner can cancel (threat 3) and report. Repeated no-show reports lead to hiding the account.
3. **Damage and deposit disputes.** Lendbox is a noticeboard, not a party to the loan. Before pickup both sides test the item together and take two photos; the deposit is returned on the spot if the item comes back in the same state. Admins do not arbitrate money; they hide members who act in bad faith, and both sides can leave an honest review.
4. **Not returned.** Guidelines advise owners not to hand over an item worth more than the deposit to someone they have not checked (account age, reviews, a name that matches). If an item is stolen, the owner has the booking record for a police complaint, and Lendbox answers lawful requests.

## 5. Top 10 before going public

1. **Admin reports page and one-click hide** (`/admin/reports`): hide listing, post or user, resolve the report. Test: non-admins get 404; a hidden listing leaves search.
2. **Per-IP limits and the lockout fix** (`accounts.authenticate`, signup, requests, messages, reports): failure budget per IP before scrypt; strict limit per (email, IP). Test: IP A locked, IP B with the right password still signs in.
3. **Raw token out of the outbox** (`accounts.sign_up`, the future sender): no plaintext token at rest. Test: no row's body contains the token after sending.
4. **Open-request caps** (`repo.request_booking`): 1 per (borrower, listing), 5 overall. Test: the sixth raises.
5. **Owner can cancel a no-show** (`domain.booking.transition`): approved, not picked up, on or after the start day. Test: owner succeeds, borrower still refused.
6. **Security headers and a production cookie guard** (`web/app.py`): CSP, `nosniff`, `Referrer-Policy`, `frame-ancestors`; refuse to start in production without secure cookies.
7. **Closed threads** (`social.post_message`): read-only after decline, expiry or cancel, and 14 days after return.
8. **Review farming** (`social.rating`): one counted rating per (author, subject) pair.
9. **Contact details in public text** (`listings.publish_listing`): block phone patterns in the description.
10. **Length bounds migration** (`0003_bounds.sql`): defense in depth behind the existing truncation.

Photo upload (threat 19) follows the same rules whenever it is built.

## 6. Accepted risks for v1

1. **Money settles off-platform.** Holding deposits or processing payments brings payment-service regulation that v1 avoids; in-person deposits and owner approval are the v1 control.
2. **Conversations move to WhatsApp.** Fighting the habit adds friction without safety; Lendbox keeps the authoritative booking state and the reviews.
3. **No ID or SMS verification.** It costs money per user and creates sensitive data to protect; email verification plus owner approval is the v1 bar.
4. **Sequential ids in URLs.** They reveal rough volume; authorization (404 for non-parties) and rate limits protect the data itself.
5. **No volumetric DDoS protection.** Application limits stop scripts; large attacks are handled at the edge (a CDN or proxy) if ever needed.

## Verification

The first draft was written without the Python files, so every application control was marked as claimed. Claude then checked each against the code on 2026-10-25:

- Confirmed present: `hmac.compare_digest` in `csrf_protect`; every POST route carries the CSRF dependency (enumerated from `app.routes`); truncation on all user text paths; 404 for non-parties in `social.booking_thread`, `social.post_message`, `repo.act_on_booking`; drafts private (`listings.owner_listing`); pickup hidden until approval.
- Confirmed gaps: raw token in `email_outbox.body` (threat 20); no status check in `social.post_message` (6); no open-request cap in `repo.request_booking` (2); owner cannot cancel on the start day (3); per-email-only throttling (10, 11).
- Downgraded: threat 18 from M to L impact, because the application already truncates every user write.
