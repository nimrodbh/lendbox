# ADR 0001: Category attributes, booking ranges and the area tree

- **Status:** Accepted (2026-10-24)
- **Contracts:** v0.2 · Schema: `lendbox/db/migrations/0001_init.sql`
- **Inputs:** two independent design reviews of schema v0.1; this ADR records the merged decision.

## Context
v1 lends belongings by the day. v2 adds apartment sublets by the night or month. Vehicles are excluded (peer-to-peer car lending needs platform insurance) but must not be designed out. Listings sit in an area tree (country > city > neighborhood | campus, hundreds of rows at most) and in flat interest communities. One `no_double_booking` exclusion constraint on `bookings` must keep working for every category.

## 1. Where category-specific attributes live

| Criterion | A. One `listings` table, `attributes jsonb` + JSON Schema | B. Class-table inheritance | C. EAV |
|---|---|---|---|
| Filtering | B-tree on real columns; GIN for `@>`; expression B-tree for numeric ranges | Native typed columns, joins per subtype | Self-joins per predicate, poor estimates |
| Validation | Full Draft 2020-12 in Python; DB checks object shape only | Strongest: SQL types and constraints | Weak |
| New category | Insert a row, zero DDL | New table, queries, deploy | Metadata rows |
| `no_double_booking` | Unchanged (one `listings.id`) | Unchanged | Unchanged |
| Interview story | Pragmatic Postgres, honest tradeoff | Textbook, heavy for dozens of niche types | Known anti-pattern |

**Decision: A.** Category schemas live in `categories.attribute_schema`; `jsonschema` (added to the stack) validates `listings.attributes` when a listing is published or a live listing is edited. Drafts may be incomplete, so new listings default to `status = 'draft'`. The tradeoff is explicit: a direct SQL writer can bypass schema validation. Only the application writes, so that is acceptable.

**Change our mind if:** one vertical (sublets, vehicles) grows many strictly typed, frequently filtered fields or its own lifecycle. Then that vertical gets a typed side table (`listing_sublets`), while belongings stay on `jsonb`.

## 2. Ranges per booking unit
Ranges are half-open `[start, end)` and finite (`isfinite` on both bounds, so `'infinity'` is rejected).

| Unit | UI input | Stored range | Units |
|---|---|---|---|
| day | first day `s`, last day `d` | `[s, d + 1)` | `end - start` days |
| night | check-in `s`, check-out `e` | `[s, e)`; the check-out day is free for the next guest | `end - start` nights |
| month | start `s`, `n` months | `[s, add_months(s, n))`, counted from `s` with end-of-month clamping | the `n` that matches, else `NotWholeMonths` |

**Rejected:** forcing months onto day-1 boundaries. Real sublets start mid-month (the founding pain was a sublet running "until mid-November"), so anniversary months it is. Partial-month pricing is out of scope.

Hourly booking is out of scope. It would need `tstzrange`, time-zone and DST rules, a new quote and a migrated exclusion constraint.

## 3. Vehicles (model only)
Vehicle categories stay `prohibited = true`. A later rollout needs relational, access-controlled data: insurance policies, verified driver licences per booking, and odometer readings at pickup and return. Descriptive specs (plate, transmission, included km) fit `attributes`; `listings` and `bookings` need no rewrite. Approval guards, pricing and legal checks would be new work.

## 4. Area tree
| Option | For | Against |
|---|---|---|
| Adjacency list + recursive CTE | No extension, trivial moves | O(depth), negligible at depth 3 |
| `ltree` | Fast `@>` | New extension, paths to maintain on moves |
| Closure table | Join-only reads | Trigger-maintained rows, overkill at this size |

**Decision: adjacency list.** Integrity is in DDL: `UNIQUE (id, kind)` plus a composite foreign key `(parent_id, kind)`, so an area's parent is always an area. `CHECK (id <> parent_id)` blocks self-loops, and a country is the only root. Level order and longer cycles are checked in `create_community` (admin-only in v1). `UNION` (not `UNION ALL`) makes the query terminate even on corrupt data.

```sql
WITH RECURSIVE subtree(id) AS (
    SELECT id FROM communities WHERE id = %(area_id)s AND kind = 'area'
    UNION
    SELECT child.id
    FROM communities AS child
    JOIN subtree AS parent ON child.parent_id = parent.id
    WHERE child.kind = 'area'
)
SELECT id FROM subtree;
-- index: communities_area_parent_idx ON communities (parent_id) WHERE kind = 'area'
```

The same composite-key trick, with constant generated columns (`area_kind`, `community_kind`, `interest_kind`), makes it impossible to use an interest as an area or the reverse, with no trigger.

## Consequences
- Zero DDL per new category; one booking constraint for everything.
- Verified on the official `postgres:16` image (16.15, real `btree_gist`) with 21 negative and positive probes (wrong-kind references, overlap `23P01`, touching ranges, blocks, infinite ranges, report targets).
- Deferred until a query needs them (v2 sublet search): the GIN index on `attributes` and the expression index on `rooms`. Also deferred: a trigger that freezes `attribute_schema` while live listings use it (admin-only edits in v1).
- Repository-level rules (not DDL): borrower is not the owner; message sender is a party to the booking; a review needs a returned booking and its two parties (`INSERT ... SELECT`); no listing in a prohibited category; `updated_at` set on every update; `max_price` filters only within one booking unit.

## Revisit when
A vertical needs many typed filters, mid-month partial pricing, one listing with both night and month pricing (today: separate categories), a non-Python writer, or hourly bookings.
