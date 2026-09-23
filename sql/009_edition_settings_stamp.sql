-- Records the settings each edition actually ran under.
--
-- Once the weights are editable in a GUI, "click-through moved last week"
-- is unanswerable unless every edition carries the configuration that
-- produced it. Without this a tuning UI quietly makes its own metrics
-- uninterpretable.

ALTER TABLE email_edition
    ADD COLUMN IF NOT EXISTS hybrid_weight        real,
    ADD COLUMN IF NOT EXISTS editor_section_front integer,
    ADD COLUMN IF NOT EXISTS profile_cluster      boolean,
    ADD COLUMN IF NOT EXISTS profile_recency      boolean,
    ADD COLUMN IF NOT EXISTS profile_rarity       boolean;
