-- 0005_demo_version.sql: which demo files the loaded world came from, so a deploy with new demo data
-- (new listings, new photos) reloads it right away instead of waiting for the daily reset.
ALTER TABLE demo_state ADD COLUMN seed_version text NOT NULL DEFAULT '';
