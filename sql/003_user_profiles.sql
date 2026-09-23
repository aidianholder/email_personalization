-- Reader interest profiles: one row per (reader, interest cluster).
--
-- A reader with a single coherent interest gets one row; a reader who follows
-- Razorbacks football and the legislature gets two, with distinct centroids.
-- Matching scores a candidate story as the max similarity across a reader's
-- rows rather than against one blurred average.

-- Superseded by 005, which re-keys this table on email_address. Kept so a
-- fresh database replays the history in order; skipped once 005 has run.
DO $mig$
BEGIN
IF EXISTS (SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='users'
              AND column_name='braze_user_id') THEN

CREATE TABLE IF NOT EXISTS user_profiles (
    braze_user_id    varchar     NOT NULL
                     REFERENCES users (braze_user_id) ON DELETE CASCADE,
    cluster_index    smallint    NOT NULL,
    -- Centroid lives in CORPUS-CENTERED space: the corpus mean has been
    -- subtracted and the result renormalized. Candidate stories must be
    -- centered against the same corpus_mean_id before comparison.
    centroid         vector(1536) NOT NULL,
    -- Sum of member weights. Weights are normalized to mean 1 within a
    -- reader, so strength reads roughly as "effective stories in this
    -- interest" and is comparable across clusters of one reader.
    strength         real        NOT NULL,
    story_count      integer     NOT NULL,
    member_story_ids bigint[]    NOT NULL,
    -- Headline of the highest-weighted member: makes a profile legible at a
    -- glance when eyeballing cluster quality.
    label            text,
    built_at         timestamptz NOT NULL DEFAULT now(),
    embed_model      text        NOT NULL,
    corpus_mean_id   bigint      REFERENCES corpus_mean (id),
    run_id           bigint      REFERENCES pipeline_runs (id),
    PRIMARY KEY (braze_user_id, cluster_index)
);

CREATE INDEX IF NOT EXISTS user_profiles_built_idx
    ON user_profiles (built_at DESC);

CREATE INDEX IF NOT EXISTS user_profiles_strength_idx
    ON user_profiles (braze_user_id, strength DESC);

END IF;
END $mig$;
