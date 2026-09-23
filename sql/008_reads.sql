-- Per-read detail with timestamps, reconstructed from raw_web.pageviews.
--
-- users.articles_read is a bare 30-day array with no times, which blocks
-- three things: fitting the recency half-life, exposure-normalising panel
-- popularity, and running a temporally honest backtest (leave-one-out on an
-- untimed array builds each profile partly from reads that happened *after*
-- the held-out story).
--
-- pageviews carries auth0_id on more article views than email_address
-- (22,550 vs 14,351 over a sample week), and matching known reads by
-- auth0_id recovers 100% of them against 63% by email. So the import joins
-- on auth0_id inside Treasure Data and translates to email before anything
-- lands here -- auth0 is a source-side join key, not a pipeline identifier.

CREATE TABLE IF NOT EXISTS reads (
    email_address text        NOT NULL
                  REFERENCES users (email_address) ON DELETE CASCADE,
    story_id      bigint      NOT NULL,
    first_read_at timestamptz NOT NULL,
    last_read_at  timestamptz,
    view_count    integer     NOT NULL DEFAULT 1,
    synced_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (email_address, story_id)
);

CREATE INDEX IF NOT EXISTS reads_email_time_idx
    ON reads (email_address, first_read_at DESC);
CREATE INDEX IF NOT EXISTS reads_story_idx ON reads (story_id);
CREATE INDEX IF NOT EXISTS reads_time_idx ON reads (first_read_at DESC);
