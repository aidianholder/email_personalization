-- Re-keys the pipeline on email_address and the Treasure Data source tables.
--
--   1. `users` is rebuilt around reporting_environment.adg_articles_past_thirty:
--      keyed on email_address, and articles_read holds Ellington story IDs
--      rather than URLs, so reads join straight to stories."storyId" with no
--      URL matching.
--   2. auth0_id is retained as a reference column only. It is no longer an
--      identifier anywhere in the pipeline.
--   3. story_popularity holds view counts derived from raw_web.pageviews.
--
-- Idempotent. The destructive branch runs only while the pre-migration
-- schema is still present (users keyed on braze_user_id), so re-running
-- init-db can never wipe imported readers or built profiles.

DO $$
DECLARE
    legacy boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'users'
           AND column_name = 'braze_user_id'
    ) INTO legacy;

    IF legacy THEN
        RAISE NOTICE 'legacy users table found; rebuilding on email_address';
        DROP TABLE IF EXISTS user_profiles CASCADE;
        DROP TABLE IF EXISTS users CASCADE;
        -- Editions from the old scheme are keyed by auth0 id and cannot be
        -- served against email-keyed profiles.
        TRUNCATE edition_payload, edition_selection, edition_story, email_edition
            RESTART IDENTITY CASCADE;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS users (
    email_address text PRIMARY KEY,
    account_key   text,
    -- Kept for cross-referencing other systems. NOT used as an identifier.
    auth0_id      text,
    -- Ellington story ids, as TD supplies them.
    articles_read bigint[] NOT NULL DEFAULT '{}',
    -- TD delivers one row per (account, email); duplicates are merged on
    -- import and this records how many source rows contributed.
    source_rows   integer NOT NULL DEFAULT 1,
    synced_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS users_synced_idx ON users (synced_at DESC);

CREATE TABLE IF NOT EXISTS user_profiles (
    email_address    text     NOT NULL
                     REFERENCES users (email_address) ON DELETE CASCADE,
    cluster_index    smallint NOT NULL,
    centroid         vector(1536) NOT NULL,
    strength         real     NOT NULL,
    story_count      integer  NOT NULL,
    member_story_ids bigint[] NOT NULL,
    label            text,
    built_at         timestamptz NOT NULL DEFAULT now(),
    embed_model      text     NOT NULL,
    corpus_mean_id   bigint   REFERENCES corpus_mean (id),
    run_id           bigint   REFERENCES pipeline_runs (id),
    PRIMARY KEY (email_address, cluster_index)
);

CREATE INDEX IF NOT EXISTS user_profiles_strength_idx
    ON user_profiles (email_address, strength DESC);

-- Popularity from raw_web.pageviews. One row per (story, snapshot), so the
-- signal used on a given morning stays reproducible after the fact.
CREATE TABLE IF NOT EXISTS story_popularity (
    story_id       bigint      NOT NULL,
    as_of          timestamptz NOT NULL,
    window_hours   integer     NOT NULL,
    views          integer     NOT NULL DEFAULT 0,
    uniques        integer     NOT NULL DEFAULT 0,
    identified     integer     NOT NULL DEFAULT 0,
    -- Hours the story had been live at as_of. Raw counts favour whatever was
    -- published earliest in the window, so ranking uses views_per_hour.
    hours_live     real,
    views_per_hour real,
    PRIMARY KEY (story_id, as_of)
);

CREATE INDEX IF NOT EXISTS story_popularity_asof_idx
    ON story_popularity (as_of DESC, views_per_hour DESC);
