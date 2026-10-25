-- 0002_auth.sql: sessions, email verification, an email outbox and login throttling.
-- Tokens are stored only as SHA-256 hashes: a leaked database cannot be replayed as live sessions or links.

CREATE TABLE sessions (
    token_hash bytea       PRIMARY KEY,
    user_id    bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > created_at)
);
CREATE INDEX sessions_user_idx ON sessions (user_id);

CREATE TABLE email_tokens (
    token_hash bytea       PRIMARY KEY,
    user_id    bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    purpose    text        NOT NULL CHECK (purpose IN ('verify_email')),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);

-- Mail is written here inside the same transaction as the change that caused it; a sender drains it.
-- Nothing is lost if the mail provider is down, and nothing is sent for a rolled-back signup.
CREATE TABLE email_outbox (
    id         bigserial   PRIMARY KEY,
    to_email   citext      NOT NULL,
    subject    text        NOT NULL,
    body       text        NOT NULL,
    created_at timestamptz NOT NULL,
    sent_at    timestamptz
);
CREATE INDEX email_outbox_unsent_idx ON email_outbox (created_at) WHERE sent_at IS NULL;

-- Failed logins per email, for throttling (5 per 15 minutes). Old rows are pruned on each attempt.
CREATE TABLE login_failures (
    email     citext      NOT NULL,
    failed_at timestamptz NOT NULL
);
CREATE INDEX login_failures_email_idx ON login_failures (email, failed_at);
