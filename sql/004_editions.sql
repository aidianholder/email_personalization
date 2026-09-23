-- Daily email editions: what each reader is sent, and the payload served to
-- Braze Connected Content.
--
-- Keyed on external_user_id (the auth0_user_id column on users), because that
-- is the identifier Braze sends back in the Connected Content URL.

CREATE TABLE IF NOT EXISTS email_edition (
    id             bigserial   PRIMARY KEY,
    edition_date   date        NOT NULL,
    -- Readers must never see a half-written edition. Rows are built under
    -- 'building' and the flag flips to 'ready' in one statement at the end;
    -- the serving endpoint only ever reads 'ready'.
    status         text        NOT NULL DEFAULT 'building',
    layout         text        NOT NULL,     -- e.g. "EEEPP": E=editor, P=personalized
    slots          smallint    NOT NULL,
    corpus_mean_id bigint      REFERENCES corpus_mean (id),
    embed_model    text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    published_at   timestamptz,
    notes          text
);

-- At most one ready edition per day; rebuilding supersedes the previous one.
CREATE UNIQUE INDEX IF NOT EXISTS email_edition_ready_idx
    ON email_edition (edition_date) WHERE status = 'ready';

-- Story metadata frozen at selection time. ~100 rows per edition, NOT per
-- reader. A send takes minutes; without this snapshot an editor's mid-send
-- headline change would split the audience across two versions of the email.
CREATE TABLE IF NOT EXISTS edition_story (
    edition_id    bigint  NOT NULL REFERENCES email_edition (id) ON DELETE CASCADE,
    story_id      bigint  NOT NULL,
    headline      text,
    tease         text,
    share_url     text,
    thumbnail_url text,
    thumbnails    jsonb,
    pub_date      timestamptz,
    PRIMARY KEY (edition_id, story_id)
);

-- One row per reader per slot: the record of what was sent and why.
CREATE TABLE IF NOT EXISTS edition_selection (
    edition_id       bigint   NOT NULL REFERENCES email_edition (id) ON DELETE CASCADE,
    external_user_id varchar  NOT NULL,
    slot             smallint NOT NULL,
    story_id         bigint   NOT NULL,
    source           text     NOT NULL,   -- editor | personalized | tail
    score            real,                -- null for non-personalized slots
    cluster_index    smallint,
    PRIMARY KEY (edition_id, external_user_id, slot)
);

CREATE INDEX IF NOT EXISTS edition_selection_story_idx
    ON edition_selection (edition_id, story_id);

-- Serving projection: the exact response body, pre-rendered. The endpoint is
-- a single primary-key lookup returning these bytes, with no joins or
-- serialization on the request path.
-- The row with external_user_id = '__default__' is the editor-only fallback
-- served to unknown readers and to anyone whose personalization failed.
CREATE TABLE IF NOT EXISTS edition_payload (
    edition_id       bigint  NOT NULL REFERENCES email_edition (id) ON DELETE CASCADE,
    external_user_id varchar NOT NULL,
    payload          jsonb   NOT NULL,
    PRIMARY KEY (edition_id, external_user_id)
);
