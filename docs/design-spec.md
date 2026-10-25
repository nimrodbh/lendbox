## Design Spec: Lendbox

### Design direction
**A neighborhood noticeboard that feels calm and safe.** Warm paper background, one deep green for trust and action, amber only for prices. Big tap targets, plenty of air, no stock-photo gloss: it should feel like a well-run community library, not a store.

Pattern references (pattern, never skin):
- **Airbnb's booking box:** the calendar, the price math and the action live in one sticky panel next to the item. <- brief: requirement 4, Airbnb row
- **Nextdoor's resident-drawn neighborhoods and Buy Nothing's local rules:** areas nest (city > neighborhood or campus) and every community carries a short rules card. <- brief v2: community model
- **Facebook Marketplace's category chips and grid:** a horizontal chip row over a dense two-column grid on mobile. Sublets later become one more chip, already shown as "Sublets · soon". <- brief: requirement 3, product decision
- **Fat Llama's trust-forward item page:** the owner, their rating and the deposit appear before the description gets long. <- brief: trust killer, Fat Llama row

### Information architecture
```
Home (the need, how it works, communities by interest and by area, footer with contact email)
Community page (one interest or area: rules, Join, listings filtered by area)
Browse (search results: what, when, where)
├── Item page ── Request sent ── Booking thread (v1, not prototyped)
├── Looking for board (strip on Browse; full page later)
└── Empty search ── Post what you need
Your lending (lender inbox: Requests / Out now / Your items)
├── New listing (4 steps: Photos and basics, Details, Availability, Review)
└── Edit listing
Your borrowing (requests and active loans; mirrors Your lending)
Review (after return)
Account (verify email, community badge)
Admin (reports queue)
```

### Core flows

#### Borrower: find and request (target: under a minute)
1. Browse with "What" and "When" -> **Browse** (results already filtered to free dates)
2. Tap an item -> **Item page**: owner, rating, deposit, rules, calendar with booked days struck out
3. Tap a start day and an end day -> the summary updates: days, price, deposit, "paid in person"
4. Optional message -> **Request these dates** -> **Request sent** (states the 48-hour expiry)
5. Owner approves -> the exact pickup spot unlocks in the booking thread
6. After return -> **Review** (blind)

#### Lender: list and approve
1. **List an item** (header, or "I have one" on a Looking for post) -> **New listing** step 1, photos and basics
2. Step 2, details: condition, price per day or free, deposit (required), pickup neighborhood
3. Step 3, availability: block dates on the same calendar component
4. Step 4, review and publish -> live at once, no admin queue <- pain: 8 of 13 posts waiting on admins (the author's sublet search)
5. A request arrives -> **Your lending** card with dates, borrower rating, "closes in 46 h"
6. **Approve** -> overlapping requests decline automatically with a polite note

### Key screens

#### 0. Home
- Purpose: explain in ten seconds what Lendbox solves, then route people into a community or a search. Primary action: Find something to borrow.
- Layout: eyebrow line; headline "Borrow what you need. Lend what you don't use."; one paragraph on the need; two buttons (Find something to borrow, List an item); three cards for the need (Things sit idle, Borrowing is messy, Lendbox fixes the how); How it works in 3 numbered steps with the "no payments through the site" line; Communities by interest (6 cards plus a dashed "Start a community, coming soon"); Communities by area (cities with their neighborhood and campus nested beneath them, a type badge on each); footer with the founder's contact email only.
- States: the community grid has no empty state (seeded); loading -> card skeletons; error on community counts -> hide counts, keep cards.
- Traces to: brief v2 MUST 4, 5 and 13; the pain paragraph (Facebook groups, WhatsApp, the HaMahsan Google Sheet).

#### 0b. Community page (example: Musicians)
- Purpose: browse one community and join it. Primary action: Join.
- Layout: breadcrumb (Communities / By interest); icon, name, one-line purpose; Join toggle (aria-pressed); community rules card; Area filter (an interest community can be narrowed to an area); listing grid.
- States: **empty** -> "No musicians lending in Haifa yet. Be the first: list one item, or post what you're looking for so lenders nearby can see it." with List an item and Post what you need; loading -> skeleton grid; error -> retry line; success -> grid.
- Traces to: brief v2 MUST 4 and 11; Buy Nothing (local rules text), Leesa Rentals and Peerby (cold start: never a dead end, request-first).

#### 1. Browse
- Purpose: find something free on my dates. Primary action: open an item.
- Layout: headline and one line of promise; What + When + Where (area select with nested neighborhoods and campuses) + Search; a line under the result heading says which child areas are included; category chips; result count; item grid (tile, condition badge, title, price per day or "Free", deposit, area, rating); "People are looking for" strip.
- States: empty -> screen 6; loading -> skeleton tiles in the grid shape; error -> "We couldn't load items. Try again" with a retry button; success -> grid; conflict -> n/a.
- Traces to: requirements 7 and 10; Spinlister row (many categories), Peerby row (looking for).

#### 2. Item page with dates
- Purpose: decide and request. Primary action: Request these dates.
- Layout: photo; category badge (v2 adds a "Checked at last return" badge with the handover checklist); title; description; owner card (name, Verified email, rating, response time); facts grid (pickup area with "exact spot shared after approval", deposit, minimum, late fee); rules. Booking panel: price, calendar, legend, summary, message, sticky CTA.
- States: empty (no dates) -> button reads "Pick your dates" and is disabled; loading -> calendar skeleton; error -> booked day inside the range: "Nov 18 is already booked. Pick dates that skip it."; success -> Request sent; **conflict** -> "Someone booked Nov 12 to 14 a moment ago. Pick new dates; your message is kept." The dates clear, the message stays.
- Traces to: requirements 2, 4; Airbnb and Fat Llama rows; trust and money killers.

#### 3. Request sent
- Purpose: confidence about what happens next. Primary action: keep browsing.
- Layout: success mark, "Request sent to Dana", expected answer time, the 48-hour rule, a request card with a "Waiting for Dana" badge.
- States: error (send failed) -> stay on screen 2 with "Your request didn't go through. Nothing was booked. Try again."; conflict -> screen 2 conflict state.
- Traces to: requirement 5; pain: Messenger leads with no status.

#### 4. Your lending (lender inbox)
- Purpose: answer requests fast. Primary action: Approve.
- Layout: tabs Requests (count badge) / Out now / Your items; request cards with borrower identity, rating, item, dates, total, message, "Closes in N h", Decline and Approve side by side.
- States: empty -> "No requests yet. Clear photos and a fair deposit help people say yes."; loading -> card skeletons; error -> inline retry; success -> card turns into an "Approved" notice explaining the pickup spot is now shared; **conflict** -> overlapping requests carry "These dates overlap with Noam's request. Approving one declines the other automatically."
- Traces to: requirements 4, 5; engineering showcase (state machine, exclusion constraint).

#### 5. New listing (step 2 of 4)
- Purpose: a complete, honest listing on the first try. Primary action: Next.
- Layout: step label and progress bar; photos (at least 1, hint to show wear); title; category (with the prohibited-items hint); condition; price per day (0 means free) and deposit side by side; pickup area with the privacy hint.
- States: empty -> placeholders; error -> field-level message under the field, focus moves there ("Add a deposit, even a small one. It protects you if the item comes back damaged."); success -> next step; conflict -> n/a.
- Traces to: requirement 2 (pain: a post declined for a missing field nobody told him about), requirement 3 and the law killer.

#### 6. Empty search
- Purpose: never a dead end. Primary action: Post to the neighborhood.
- Layout: icon, "No drills free on Nov 12 to 14", why (booked, not missing), a prefilled Looking for form, a link to the booked items' next free dates.
- Traces to: cold-start killer; Peerby row.

#### 7. Review
- Purpose: honest ratings. Primary action: Submit review.
- Layout: item and return summary; question; 5 star buttons (44px); text; the blind-review rule in plain words.
- States: error -> "Pick a rating to submit"; success -> toast "It will show once Dana reviews too."
- Traces to: requirement 8; Airbnb row.

### Design system
- **Color tokens** in `lendbox/web/static/tokens.css`, light and dark, AA checked: every text pairing used is at least 4.5:1 (lowest: success on success-soft at 4.72 in light). The light amber was darkened from #b86e12 (3.98:1, fail) to #945708 (5.79:1).
- **Type:** system font stack with Inter first and Noto Sans Hebrew as the Hebrew fallback. Scale 12 / 14 / 16 / 20 / 26 / 34; body line-height 1.55.
- **Spacing:** 4px steps (4, 8, 12, 16, 24, 32, 48).
- **Shape:** radius 8 / 12 / 18 / pill; tap target 44px.
- **Components (named for the templates):** `btn` (primary, ghost, danger, block), `input` and `field` (label, hint, error), `card`, `badge` (neutral, green, amber, red), `chip`, `tile`, `cal` and `day` (booked, past, in, edge), `notice` (warn, ok, info), `toast`, `tabs`, `steps`.

### Microcopy
- Home: "Borrow what you need. Lend what you don't use." / "Most of what we own sits in a closet, and most of what we need, we need once."
- Areas: "A listing in Florentin also shows in Tel Aviv-Yafo." / "Your neighborhood or campus isn't here yet? Soon you can open it yourself."
- Footer: "Questions or ideas? Write to {contact email}", the address set in `LENDBOX_CONTACT_EMAIL` (email only: no phone, no address)
- Actions: "Request these dates", "Approve", "Decline", "List an item", "Post to the neighborhood", "Submit review", "Next: availability".
- Disabled CTA: "Pick your dates", then "Pick a return date".
- Money: "You pay Dana in person at pickup. Lendbox never charges you." / "Deposit, returned when you bring it back".
- Privacy: "Only the neighborhood is public. You share the exact spot after you approve."
- Law: "Cars, scooters, weapons and medication can't be listed. Lending them needs insurance or a license."
- Conflict: "Someone booked Nov 12 to 14 a moment ago. Pick new dates; your message is kept."
- Expiry: "If there is no answer in 48 hours, the request closes on its own and the dates open up again."
- Ranges are written "Nov 12 to 14", never with a dash.

### Accessibility and i18n
- Every input has a visible label; errors are tied to fields and announced (`role="alert"`, `aria-invalid`); toasts use `aria-live="polite"`.
- Tap targets 44px; focus ring 3px; booked days are disabled buttons with "booked" in their accessible name, not color alone (also struck through).
- `prefers-reduced-motion` respected; dark mode by system setting or toggle.
- RTL: the whole layout uses logical properties (`margin-inline`, `inset-inline-start`, `border-inline-start`), so `dir="rtl"` mirrors it with no second stylesheet. Verified in the prototype: the calendar, cards and notices mirror correctly. Hebrew strings will live in a translation catalog from day one, so v2 is a content task, not a layout task.

### Prototype
`P-projects/lendbox/design/prototype.html`, self-contained (tokens inlined from `tokens.css`, which stays the source the templates import). Screens: Home, Community page (Join, area filter, empty state), Browse with area filter, Item and dates (live calendar with booked days, range validation, conflict simulation), Request sent, Lender inbox (approve with auto-decline of the overlap), New listing step 2 (deposit validation), Empty search, Review. A prototype bar switches screens and toggles RTL and dark mode. Verified in the browser at 375px (no horizontal scroll, header on one line) and at desktop width (two-column item page).
