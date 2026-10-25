-- 0003_hardening.sql: fixes from docs/threat-model.md before going public.

-- Threat 20: no raw token at rest. The outbox now stores what to send, not the link itself; the sender mints
-- the verification token at send time and never writes it down. Existing bodies may hold live links: drop them.
ALTER TABLE email_outbox
    ADD COLUMN kind    text   NOT NULL DEFAULT 'verify_email' CHECK (kind IN ('verify_email')),
    ADD COLUMN user_id bigint REFERENCES users (id) ON DELETE CASCADE,
    ALTER COLUMN body DROP NOT NULL,
    ALTER COLUMN subject DROP NOT NULL;
UPDATE email_outbox SET body = NULL, subject = NULL;
ALTER TABLE email_outbox ADD CONSTRAINT email_outbox_no_body CHECK (body IS NULL);

-- Threats 10 and 11: throttle by client too, not only by email. IPs are stored as salted hashes, never raw.
ALTER TABLE login_failures ADD COLUMN ip_hash bytea;
CREATE INDEX login_failures_ip_idx ON login_failures (ip_hash, failed_at);

-- Generic rate limiting for signup, requests, messages, reports and posts (fixed window, counted in SQL).
CREATE TABLE rate_events (
    bucket text        NOT NULL,
    key    bytea       NOT NULL,
    at     timestamptz NOT NULL
);
CREATE INDEX rate_events_lookup_idx ON rate_events (bucket, key, at);

-- Threat 18: length bounds in the database, behind the truncation the application already does.
ALTER TABLE listings
    ADD CONSTRAINT listings_description_len CHECK (length(description) <= 5000),
    ADD CONSTRAINT listings_pickup_detail_len CHECK (length(pickup_detail) <= 500),
    ADD CONSTRAINT listings_attributes_size CHECK (octet_length(attributes::text) <= 8192);
ALTER TABLE bookings ADD CONSTRAINT bookings_message_len CHECK (length(message) <= 2000);
ALTER TABLE reviews ADD CONSTRAINT reviews_body_len CHECK (length(body) <= 2000);
ALTER TABLE looking_for_posts ADD CONSTRAINT looking_for_body_len CHECK (length(body) <= 3000);
ALTER TABLE communities ADD CONSTRAINT communities_rules_len CHECK (length(rules) <= 2000);
ALTER TABLE listing_photos ADD CONSTRAINT listing_photos_max CHECK (position < 8);

-- Threat 1: the admin queue lists open reports newest first (index exists from 0001); resolution notes.
ALTER TABLE reports
    ADD COLUMN resolution text CHECK (resolution IN ('hidden', 'dismissed')),
    ADD CONSTRAINT reports_resolution_pair CHECK ((resolution IS NULL) = (resolved_at IS NULL));
