-- Distinguishes the two popularity snapshot types sharing this table.
--
--   'daily'   -- the nightly sync: one shared as_of across all stories,
--                counting a trailing window. What production ranks on.
--   'at_send' -- backfilled per story: views accumulated between publication
--                and the first 5am after it, i.e. what would actually have
--                been known at send time. Each story carries its own as_of,
--                so this is the honest signal for backtesting.

ALTER TABLE story_popularity
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'daily';

ALTER TABLE story_popularity
    ADD COLUMN IF NOT EXISTS views_total integer;

CREATE INDEX IF NOT EXISTS story_popularity_kind_idx
    ON story_popularity (kind, story_id);
