-- Optional privacy-preserving lookup key for the Connected Content endpoint.
--
-- Keying the feed URL on a raw email address puts it in web server logs,
-- referrer headers and any analytics on the path. When FEED_HASH_SALT is set
-- the pipeline also stores sha256(salt || lower(email)) and the endpoint will
-- accept that instead, so the address never travels in a query string.

ALTER TABLE edition_payload ADD COLUMN IF NOT EXISTS lookup_key text;

CREATE INDEX IF NOT EXISTS edition_payload_lookup_idx
    ON edition_payload (edition_id, lookup_key) WHERE lookup_key IS NOT NULL;
