# Lendbox

[![ci](https://github.com/nimrodbh/lendbox/actions/workflows/ci.yml/badge.svg)](https://github.com/nimrodbh/lendbox/actions/workflows/ci.yml)

**A neighborhood library of things.** Neighbors lend each other tools, camping gear, instruments and electronics for a date range, with a deposit, and review each other after the return. Listings live in communities by interest (Musicians, Students, Builders) and by area (a city, with its neighborhoods and campuses inside it).

**Live demo: [lendbox.onrender.com](https://lendbox.onrender.com)** · one click signs you in as a demo lender or borrower; no sign-up, and the data resets daily. (Free hosting: the first visit after a quiet spell can take up to a minute to wake the server.)

![Home page with the one-click demo](docs/screenshots/home.png)

| Item page: booked dates, deposit, private pickup spot | Browsing an area (child neighborhoods included) |
|---|---|
| ![Item page](docs/screenshots/item.png) | ![Browse](docs/screenshots/browse.png) |

## What makes it interesting

**1. A listing can never be double booked, and the database is what guarantees it.**
Approvals are serialized by a row lock in the application, but correctness does not depend on that code: a PostgreSQL exclusion constraint refuses any two overlapping approved bookings for the same item, whoever writes them.

```sql
CONSTRAINT no_double_booking EXCLUDE USING gist (listing_id WITH =, during WITH &&)
    WHERE (status IN ('approved', 'active'))
```
`tests/db/test_booking_race.py` proves it: eight threads approve overlapping requests at the same instant and exactly one wins; a second test bypasses the application entirely with two raw transactions and shows the database itself rejecting the second one (SQLSTATE `23P01`). Owner-blocked dates live in the same table, so one constraint covers both.

**2. Integrity in the schema, not only in the code.** An area can never be tagged as an interest (composite foreign keys over `(id, kind)` with constant generated columns, no triggers); date ranges are half-open and finite; a report has exactly one typed target. Each rule is a negative test against real PostgreSQL.

**3. A community tree.** Areas nest (Tel Aviv-Yafo contains Florentin and Tel Aviv University). A recursive CTE returns "this area and everything inside it", so a Florentin listing shows when you browse Tel Aviv.

**4. One listing model for very different things.** Each category carries a JSON Schema; the listing form is generated from it and the attributes are validated against it before publishing. A new category is a row, not a migration. The trade-offs are written down in [ADR 0001](docs/adr/0001-listing-polymorphism.md).

**5. Security that was threat-modeled, then built.** [docs/threat-model.md](docs/threat-model.md) lists 20 threats for a site where strangers meet to hand over valuable things; the top ten are implemented and tested, for example:
- tokens and sessions stored only as hashes, and no verification link ever written to the database (mail is built at send time);
- login throttling per IP (checked before any password hashing), per email and IP, and per email from anywhere, so one machine can neither stuff credentials nor lock a victim out;
- CSRF on every form (a test enumerates every POST route), a strict Content-Security-Policy, blind two-sided reviews whose averages count each author once;
- the exact pickup spot stays hidden until the owner approves, and phone numbers are kept out of public listing text.

## How it is built

```mermaid
flowchart LR
  B[Browser] -->|HTML forms| W[FastAPI + Jinja<br/>lendbox/web]
  W --> D[Domain rules, pure Python<br/>lendbox/domain]
  W --> R[Repositories, all SQL<br/>lendbox/db]
  R --> P[(PostgreSQL 16<br/>exclusion constraint,<br/>recursive CTE)]
  D -. state machine, pricing,<br/>validation .-> R
```

- **Python 3.12, FastAPI, server-rendered Jinja** (no JavaScript framework), **PostgreSQL 16** through psycopg 3 with plain SQL and numbered migrations (no ORM, so the constraints stay visible).
- **Domain layer is pure** (no I/O): the booking state machine, prices in integer agorot, half-open date ranges. 220 table-driven tests cover every state, event and actor.
- **434 tests**, all against a real PostgreSQL (never SQLite), run in GitHub Actions with ruff.
- **Accessible and RTL-ready**: logical CSS properties, labelled fields, errors announced to screen readers; every user-facing string sits in one catalog so Hebrew is a translation, not a rewrite.

## Run it locally

```bash
docker compose up -d --build                                  # app on http://localhost:8000
docker compose exec app python -m lendbox.db.seed --reset      # 40 made-up members, 120 items
```
Demo accounts `u01@example.test` to `u40@example.test` share the local password `lendbox-demo-2026`.

Tests:
```bash
docker compose exec db psql -U lendbox -c "CREATE DATABASE lendbox_test"   # once
pip install -e ".[dev]"
LENDBOX_TEST_DATABASE_URL=postgresql://lendbox:lendbox-dev@localhost:5433/lendbox_test pytest -q
```

Deployment: `render.yaml` describes the service (Docker, free plan) and the settings it needs; the database is any PostgreSQL 16 with `btree_gist` and `citext`. In production the app refuses to start without secure cookies.

## Documents

- [Product brief](docs/market-brief.md): the problem, comparable services, requirements
- [Design spec](docs/design-spec.md) and the [clickable prototype](design/prototype.html)
- [ADR 0001](docs/adr/0001-listing-polymorphism.md): listing model, date ranges, the area tree
- [Threat model](docs/threat-model.md)
