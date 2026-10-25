-- 0001_init.sql: Lendbox schema, contracts v0.2.
-- Integrity lives in the database wherever a constraint can say it:
--   * no_double_booking: an exclusion constraint over daterange, so two overlapping approvals cannot both commit.
--   * community kind: (id, kind) superkey plus constant generated columns, so an area can never be used
--     as an interest (or the reverse) and an area's parent is always an area. See docs/adr/0001.

CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE users (
    id                bigserial PRIMARY KEY,
    email             citext      NOT NULL UNIQUE,
    display_name      text        NOT NULL CHECK (length(display_name) BETWEEN 1 AND 60),
    password_hash     text        NOT NULL,
    email_verified_at timestamptz,
    is_admin          boolean     NOT NULL DEFAULT false,
    hidden_at         timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- Interest communities are flat; area communities form a tree: country > city > neighborhood | campus.
CREATE TABLE communities (
    id         bigserial PRIMARY KEY,
    slug       text   NOT NULL UNIQUE,
    name       text   NOT NULL,
    kind       text   NOT NULL CHECK (kind IN ('interest', 'area')),
    area_level text   CHECK (area_level IN ('country', 'city', 'neighborhood', 'campus')),
    parent_id  bigint,
    rules      text   NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT communities_id_kind_key UNIQUE (id, kind),
    CONSTRAINT communities_parent_same_kind_fkey FOREIGN KEY (parent_id, kind) REFERENCES communities (id, kind),
    CHECK (id <> parent_id),
    CHECK ((kind = 'interest' AND area_level IS NULL AND parent_id IS NULL)
        OR (kind = 'area' AND ((area_level = 'country' AND parent_id IS NULL)
                            OR (area_level IN ('city', 'neighborhood', 'campus') AND parent_id IS NOT NULL))))
);
CREATE INDEX communities_area_parent_idx ON communities (parent_id) WHERE kind = 'area';

CREATE TABLE community_members (
    user_id      bigint NOT NULL REFERENCES users (id),
    community_id bigint NOT NULL REFERENCES communities (id),
    joined_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, community_id)
);

-- booking_unit drives pricing and range rules: 'day' in v1; 'night' and 'month' reserved for sublets (v2).
CREATE TABLE categories (
    id               bigserial PRIMARY KEY,
    slug             text    NOT NULL UNIQUE,
    name             text    NOT NULL,
    booking_unit     text    NOT NULL CHECK (booking_unit IN ('day', 'night', 'month')),
    prohibited       boolean NOT NULL DEFAULT false,
    attribute_schema jsonb   NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attribute_schema) = 'object')
);

CREATE TABLE listings (
    id                    bigserial PRIMARY KEY,
    owner_id              bigint  NOT NULL REFERENCES users (id),
    category_id           bigint  NOT NULL REFERENCES categories (id),
    area_id               bigint  NOT NULL,
    area_kind             text    GENERATED ALWAYS AS ('area') STORED,
    title                 text    NOT NULL CHECK (length(title) BETWEEN 3 AND 80),
    description           text    NOT NULL DEFAULT '',
    condition             text    NOT NULL CHECK (condition IN ('new', 'like_new', 'good', 'fair')),
    price_per_unit_agorot integer NOT NULL CHECK (price_per_unit_agorot >= 0),
    deposit_agorot        integer NOT NULL CHECK (deposit_agorot > 0),
    min_units             integer NOT NULL DEFAULT 1 CHECK (min_units >= 1),
    late_fee_agorot       integer NOT NULL DEFAULT 0 CHECK (late_fee_agorot >= 0),
    attributes            jsonb   NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attributes) = 'object'),
    pickup_detail         text    NOT NULL DEFAULT '',
    status                text    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'live', 'hidden', 'archived')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT listings_area_kind_fkey FOREIGN KEY (area_id, area_kind) REFERENCES communities (id, kind)
);
CREATE INDEX listings_live_search_idx ON listings (area_id, category_id, price_per_unit_agorot) WHERE status = 'live';
CREATE INDEX listings_live_recent_idx ON listings (created_at DESC, id DESC) WHERE status = 'live';
-- Browse q: queries must use this exact expression with plainto_tsquery('simple', q).
CREATE INDEX listings_live_text_idx ON listings
    USING gin (to_tsvector('simple'::regconfig, title || ' ' || description)) WHERE status = 'live';

CREATE TABLE listing_interests (
    listing_id     bigint NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    community_id   bigint NOT NULL,
    community_kind text   GENERATED ALWAYS AS ('interest') STORED,
    PRIMARY KEY (listing_id, community_id),
    CONSTRAINT listing_interests_kind_fkey FOREIGN KEY (community_id, community_kind) REFERENCES communities (id, kind)
);
CREATE INDEX listing_interests_community_idx ON listing_interests (community_id, listing_id);

CREATE TABLE listing_photos (
    id         bigserial PRIMARY KEY,
    listing_id bigint  NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    path       text    NOT NULL,
    position   integer NOT NULL CHECK (position >= 0),
    UNIQUE (listing_id, position)
);

-- One table for loans AND owner blocks, so ONE exclusion constraint covers both.
-- An owner removes a block by cancelling it (history is kept, the dates free up).
CREATE TABLE bookings (
    id             bigserial PRIMARY KEY,
    listing_id     bigint    NOT NULL REFERENCES listings (id),
    kind           text      NOT NULL CHECK (kind IN ('loan', 'block')),
    borrower_id    bigint    REFERENCES users (id),
    during         daterange NOT NULL CHECK (NOT isempty(during) AND lower_inc(during) AND NOT upper_inc(during)
                                         AND isfinite(lower(during)) AND isfinite(upper(during))),
    status         text      NOT NULL CHECK (status IN
                     ('requested', 'approved', 'declined', 'expired', 'cancelled', 'active', 'returned')),
    rental_agorot  integer   NOT NULL DEFAULT 0 CHECK (rental_agorot >= 0),
    deposit_agorot integer   NOT NULL DEFAULT 0 CHECK (deposit_agorot >= 0),
    message        text      NOT NULL DEFAULT '',
    requested_at   timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz,
    decided_at     timestamptz,
    picked_up_at   timestamptz,
    returned_at    timestamptz,
    CHECK ((kind = 'block' AND borrower_id IS NULL AND status IN ('approved', 'cancelled') AND expires_at IS NULL)
        OR (kind = 'loan'  AND borrower_id IS NOT NULL AND expires_at IS NOT NULL)),
    CHECK (status <> 'returned' OR returned_at IS NOT NULL),
    CONSTRAINT no_double_booking EXCLUDE USING gist (listing_id WITH =, during WITH &&)
        WHERE (status IN ('approved', 'active'))
);
CREATE INDEX bookings_pending_expiry_idx ON bookings (expires_at) WHERE status = 'requested';
CREATE INDEX bookings_pending_overlap_idx ON bookings USING gist (listing_id, during) WHERE status = 'requested';

CREATE TABLE booking_messages (
    id         bigserial PRIMARY KEY,
    booking_id bigint NOT NULL REFERENCES bookings (id),
    sender_id  bigint NOT NULL REFERENCES users (id),
    body       text   NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Blind: visible once both sides reviewed, or 14 days after bookings.returned_at.
-- That the booking is returned and author/subject are its two parties is checked by submit_review's INSERT ... SELECT.
CREATE TABLE reviews (
    id         bigserial PRIMARY KEY,
    booking_id bigint   NOT NULL REFERENCES bookings (id),
    author_id  bigint   NOT NULL REFERENCES users (id),
    subject_id bigint   NOT NULL REFERENCES users (id),
    rating     smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
    body       text     NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (booking_id, author_id),
    CHECK (author_id <> subject_id)
);

CREATE TABLE looking_for_posts (
    id            bigserial PRIMARY KEY,
    author_id     bigint NOT NULL REFERENCES users (id),
    area_id       bigint NOT NULL,
    area_kind     text   GENERATED ALWAYS AS ('area') STORED,
    interest_id   bigint,
    interest_kind text   GENERATED ALWAYS AS ('interest') STORED,
    category_id   bigint REFERENCES categories (id),
    title         text   NOT NULL CHECK (length(title) BETWEEN 3 AND 80),
    body          text   NOT NULL DEFAULT '',
    wanted_during daterange CHECK (wanted_during IS NULL
                     OR (NOT isempty(wanted_during) AND lower_inc(wanted_during) AND NOT upper_inc(wanted_during)
                         AND isfinite(lower(wanted_during)) AND isfinite(upper(wanted_during)))),
    status        text   NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT looking_for_area_kind_fkey FOREIGN KEY (area_id, area_kind) REFERENCES communities (id, kind),
    CONSTRAINT looking_for_interest_kind_fkey FOREIGN KEY (interest_id, interest_kind) REFERENCES communities (id, kind)
);

-- Exactly one target, each a real foreign key (no polymorphic id).
CREATE TABLE reports (
    id                    bigserial PRIMARY KEY,
    reporter_id           bigint NOT NULL REFERENCES users (id),
    listing_id            bigint REFERENCES listings (id),
    target_user_id        bigint REFERENCES users (id),
    looking_for_id        bigint REFERENCES looking_for_posts (id),
    reason                text   NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 1000),
    created_at            timestamptz NOT NULL DEFAULT now(),
    resolved_at           timestamptz,
    resolved_by           bigint REFERENCES users (id),
    CHECK (num_nonnulls(listing_id, target_user_id, looking_for_id) = 1),
    CHECK ((resolved_at IS NULL) = (resolved_by IS NULL))
);
CREATE INDEX reports_unresolved_idx ON reports (created_at) WHERE resolved_at IS NULL;
