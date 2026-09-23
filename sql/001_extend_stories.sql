-- Extends the existing public.stories table for the article-vectorization
-- pipeline. Idempotent: safe to run repeatedly. Nothing is dropped, and
-- existing rows (headline / story / summary / embedding) are preserved.

CREATE EXTENSION IF NOT EXISTS vector;

-- "storyId" arrived as numeric(7,0), which cannot hold an 8-digit id.
-- Current ADG ids are already ~3.7M, so this would start rejecting inserts
-- at 10,000,000. Widen to bigint. No-op once applied.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'stories'
      AND column_name = 'storyId' AND data_type = 'numeric'
  ) THEN
    ALTER TABLE public.stories ALTER COLUMN "storyId" TYPE bigint;
  END IF;
END $$;

ALTER TABLE public.stories
  ADD COLUMN IF NOT EXISTS tease                 text,
  ADD COLUMN IF NOT EXISTS kicker                text,
  ADD COLUMN IF NOT EXISTS share_url             text,
  ADD COLUMN IF NOT EXISTS thumbnail_url         text,
  -- Raw `thumbnails` payload kept verbatim: the API types it only as
  -- "string", so this lets us re-derive thumbnail_url without a refetch.
  ADD COLUMN IF NOT EXISTS thumbnail_raw         text,
  ADD COLUMN IF NOT EXISTS subscription_required boolean,
  ADD COLUMN IF NOT EXISTS updated_at            timestamptz,
  ADD COLUMN IF NOT EXISTS fetched_at            timestamptz,
  -- Exactly the text that was sent to the embedding model, plus its hash.
  -- The hash is what makes re-embedding incremental: if an editor rewrites a
  -- headline, the hash changes and that story alone is re-embedded.
  ADD COLUMN IF NOT EXISTS embed_input           text,
  ADD COLUMN IF NOT EXISTS embed_input_hash      text,
  -- Hash of the text the *stored* vector was actually built from. When it
  -- drifts from embed_input_hash the row is stale and gets re-embedded.
  ADD COLUMN IF NOT EXISTS embedded_hash         text,
  ADD COLUMN IF NOT EXISTS embed_model           text,
  ADD COLUMN IF NOT EXISTS embedded_at           timestamptz;

CREATE INDEX IF NOT EXISTS stories_published_idx
  ON public.stories (published DESC);

-- Partial index driving the "what still needs embedding?" query.
CREATE INDEX IF NOT EXISTS stories_needs_embedding_idx
  ON public.stories (published DESC)
  WHERE embedding IS NULL;
