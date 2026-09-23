-- Operational tables: a run log so a failing daily job is visible, and the
-- corpus mean vector used to mean-center embeddings before matching.

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id           bigserial PRIMARY KEY,
    command      text        NOT NULL,
    status       text        NOT NULL DEFAULT 'running',  -- running | ok | error
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    window_start timestamptz,
    window_end   timestamptz,
    fetched      integer     NOT NULL DEFAULT 0,
    inserted     integer     NOT NULL DEFAULT 0,
    updated      integer     NOT NULL DEFAULT 0,
    skipped      integer     NOT NULL DEFAULT 0,
    embedded     integer     NOT NULL DEFAULT 0,
    error        text,
    host         text,
    pid          integer
);

CREATE INDEX IF NOT EXISTS pipeline_runs_started_idx
    ON pipeline_runs (started_at DESC);

-- Rows are kept rather than overwritten: the vector used on a given morning
-- is what makes that morning's recommendations reproducible after the fact.
-- `is_current` marks the one the matching step should read.
CREATE TABLE IF NOT EXISTS corpus_mean (
    id           bigserial   PRIMARY KEY,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    embed_model  text        NOT NULL,
    dimensions   integer     NOT NULL,
    window_days  integer     NOT NULL,
    window_start timestamptz,
    window_end   timestamptz,
    story_count  integer     NOT NULL,
    mean_vector  vector(1536) NOT NULL,
    is_current   boolean     NOT NULL DEFAULT true,
    run_id       bigint      REFERENCES pipeline_runs (id) ON DELETE SET NULL
);

-- At most one current row per model.
CREATE UNIQUE INDEX IF NOT EXISTS corpus_mean_current_idx
    ON corpus_mean (embed_model) WHERE is_current;

CREATE INDEX IF NOT EXISTS corpus_mean_computed_idx
    ON corpus_mean (computed_at DESC);
