-- 0004_demo.sql: when the public demo world was last loaded, so it can be reset once a day.
CREATE TABLE demo_state (
    id        smallint    PRIMARY KEY CHECK (id = 1),
    loaded_at timestamptz NOT NULL
);
