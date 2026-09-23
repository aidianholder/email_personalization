"""Postgres persistence for story rows and their embeddings."""
from __future__ import annotations

import hashlib
import logging
import os
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Sequence

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import PROJECT_ROOT
from .ellington import Story

log = logging.getLogger(__name__)

MIGRATIONS_DIR = PROJECT_ROOT / "sql"
# Applied in filename order; every migration is written to be idempotent.
MIGRATIONS = (
    "001_extend_stories.sql",
    "002_runs_and_corpus_mean.sql",
    "003_user_profiles.sql",
    "004_editions.sql",
    "005_email_identity_and_popularity.sql",
    "006_payload_lookup_key.sql",
    "007_popularity_kind.sql",
    "008_reads.sql",
    "009_edition_settings_stamp.sql",
)

# Constant key for pg_advisory_lock. Any stable 64-bit int works; this is
# just a hash of the project name so it cannot collide with other apps.
LOCK_KEY = 0x4144475F56454331  # "ADG_VEC1"


def connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, row_factory=dict_row)
    try:
        register_vector(conn)
    except psycopg.ProgrammingError:
        # `vector` type not installed yet; init_db will create it and the
        # caller reconnects. Harmless on a fresh database.
        log.debug("pgvector type not registered yet")
    return conn


def init_db(database_url: str) -> None:
    """Apply the (idempotent) schema migrations, in order."""
    with psycopg.connect(database_url) as conn:
        for name in MIGRATIONS:
            with conn.cursor() as cur:
                cur.execute((MIGRATIONS_DIR / name).read_text())
            conn.commit()
            log.info("applied %s", name)


@contextmanager
def advisory_lock(conn: psycopg.Connection, key: int = LOCK_KEY):
    """Session-level lock so two runs cannot overlap.

    Held for the life of the connection, which is why the CLI opens one
    connection per invocation and passes it down: a `run` must hold the lock
    across both the fetch and the embed stage.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s) AS got", (key,))
        row = cur.fetchone()
    if not (row and row["got"]):
        raise RuntimeError(
            "another adgvec run holds the lock; refusing to start a second one"
        )
    try:
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            conn.commit()
        except Exception:  # noqa: BLE001 - never mask the original failure
            log.warning("could not release advisory lock", exc_info=True)


def start_run(
    conn: psycopg.Connection,
    command: str,
    window_start=None,
    window_end=None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_runs (command, window_start, window_end, host, pid)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (command, window_start, window_end, socket.gethostname(), os.getpid()),
        )
        run_id = cur.fetchone()["id"]
    conn.commit()
    return run_id


def update_run(conn: psycopg.Connection, run_id: int, counts: dict[str, int]) -> None:
    """Fold stage counts into an in-flight run row. Does not end the run."""
    if not counts:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pipeline_runs
               SET fetched  = %s, inserted = %s, updated = %s,
                   skipped  = %s, embedded = %s
             WHERE id = %s
            """,
            (
                counts.get("fetched", 0), counts.get("inserted", 0),
                counts.get("updated", 0), counts.get("skipped", 0),
                counts.get("embedded", 0), run_id,
            ),
        )
    conn.commit()


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    status: str,
    error: str | None = None,
) -> None:
    """Close out a run. Counts are written separately by update_run."""
    # The run may be failing *because* the transaction broke, so clear any
    # aborted state before trying to record the outcome.
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE pipeline_runs SET status = %s, finished_at = now(), "
            "error = %s WHERE id = %s",
            (status, error[:4000] if error else None, run_id),
        )
    conn.commit()


def recompute_corpus_mean(
    conn: psycopg.Connection,
    model: str,
    dimensions: int,
    window_days: int,
    run_id: int | None = None,
) -> dict[str, Any] | None:
    """Recompute the trailing-window mean vector and mark it current.

    This is the vector every embedding gets subtracted from before matching.
    Without it, the shared "local Arkansas news" direction dominates every
    comparison and every reader's top matches converge on the same stories.

    Only vectors from `model` are averaged, so a model change cannot silently
    mix incompatible geometries into one mean.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT avg(embedding)::vector AS mean_vector,
                   count(*)               AS story_count,
                   min(published)         AS window_start,
                   max(published)         AS window_end
              FROM stories
             WHERE embedding IS NOT NULL
               AND embed_model = %(model)s
               AND published >= now() - make_interval(days => %(days)s)
            """,
            {"model": model, "days": window_days},
        )
        row = cur.fetchone()

    if not row or not row["story_count"]:
        log.warning(
            "no embedded stories in the last %s days for %s; corpus mean unchanged",
            window_days, model,
        )
        return None

    with conn.cursor() as cur:
        # Demote the previous current row first: the partial unique index
        # allows only one current row per model.
        cur.execute(
            "UPDATE corpus_mean SET is_current = false "
            "WHERE embed_model = %s AND is_current",
            (model,),
        )
        cur.execute(
            """
            INSERT INTO corpus_mean (
                embed_model, dimensions, window_days, window_start, window_end,
                story_count, mean_vector, is_current, run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, true, %s)
            RETURNING id, computed_at, story_count
            """,
            (
                model, dimensions, window_days, row["window_start"],
                row["window_end"], row["story_count"], row["mean_vector"], run_id,
            ),
        )
        created = cur.fetchone()
    conn.commit()
    log.info(
        "corpus mean recomputed over %s stories (%s-day window)",
        created["story_count"], window_days,
    )
    return created


def current_corpus_mean(conn: psycopg.Connection, model: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, computed_at, story_count, window_days, dimensions "
            "FROM corpus_mean WHERE embed_model = %s AND is_current",
            (model,),
        )
        return cur.fetchone()


def recent_runs(conn: psycopg.Connection, limit: int = 10) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, command, status, started_at, finished_at,
                   fetched, inserted, updated, skipped, embedded, error
              FROM pipeline_runs ORDER BY started_at DESC LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


UPSERT = """
INSERT INTO stories (
    "storyId", headline, tease, kicker, share_url, thumbnail_url, thumbnail_raw,
    published, updated_at, subscription_required, embed_input, embed_input_hash,
    fetched_at
) VALUES (
    %(story_id)s, %(headline)s, %(tease)s, %(kicker)s, %(share_url)s,
    %(thumbnail_url)s, %(thumbnail_raw)s, %(published)s, %(updated_at)s,
    %(subscription_required)s, %(embed_input)s, %(embed_input_hash)s, now()
)
ON CONFLICT ("storyId") DO UPDATE SET
    headline              = EXCLUDED.headline,
    tease                 = EXCLUDED.tease,
    kicker                = EXCLUDED.kicker,
    share_url             = EXCLUDED.share_url,
    thumbnail_url         = EXCLUDED.thumbnail_url,
    thumbnail_raw         = EXCLUDED.thumbnail_raw,
    published             = EXCLUDED.published,
    updated_at            = EXCLUDED.updated_at,
    subscription_required = EXCLUDED.subscription_required,
    embed_input           = EXCLUDED.embed_input,
    embed_input_hash      = EXCLUDED.embed_input_hash,
    fetched_at            = now()
-- NOTE: `embedding` is deliberately not touched here. A story whose text
-- changed keeps its old vector (so the endpoint never serves a NULL) and is
-- picked up by the embed step via the now-mismatched hash.
RETURNING (xmax = 0) AS inserted;
"""


def upsert_stories(conn: psycopg.Connection, rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
    inserted = updated = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(UPSERT, row)
            result = cur.fetchone()
            if result and result["inserted"]:
                inserted += 1
            else:
                updated += 1
    conn.commit()
    return inserted, updated


def stories_needing_embedding(
    conn: psycopg.Connection,
    model: str,
    force: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Rows whose stored vector is missing, stale, or from a different model.

    Stale means embedded_hash (what the vector was built from) has drifted
    from embed_input_hash (what the text says now) -- i.e. an editor rewrote
    the headline or tease after we embedded it.
    """
    conditions = ["embed_input IS NOT NULL", "embed_input <> ''"]
    if not force:
        conditions.append(
            "(embedding IS NULL"
            " OR embedded_hash IS DISTINCT FROM embed_input_hash"
            " OR embed_model IS DISTINCT FROM %(model)s)"
        )
    sql = f"""
        SELECT "storyId" AS story_id, headline, embed_input, embed_input_hash
          FROM stories
         WHERE {" AND ".join(conditions)}
         ORDER BY published DESC NULLS LAST
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(sql, {"model": model})
        return cur.fetchall()


def save_embeddings(
    conn: psycopg.Connection,
    model: str,
    items: Sequence[tuple[int, str, list[float]]],
) -> int:
    with conn.cursor() as cur:
        for story_id, input_hash, vector in items:
            cur.execute(
                """
                UPDATE stories
                   SET embedding     = %s,
                       embed_model   = %s,
                       embedded_hash = %s,
                       embedded_at   = now()
                 WHERE "storyId" = %s
                """,
                (vector, model, input_hash, story_id),
            )
    conn.commit()
    return len(items)


def status(conn: psycopg.Connection) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)                                              AS total,
                   count(*) FILTER (WHERE embedding IS NOT NULL)         AS embedded,
                   count(*) FILTER (WHERE share_url IS NOT NULL)         AS with_share_url,
                   count(*) FILTER (WHERE thumbnail_url IS NOT NULL)     AS with_thumbnail,
                   count(*) FILTER (WHERE tease IS NOT NULL AND tease <> '') AS with_tease,
                   count(*) FILTER (WHERE published > now() - interval '24 hours')
                                                                         AS last_24h,
                   min(published) AS oldest, max(published) AS newest
              FROM stories
            """
        )
        return cur.fetchone() or {}


# --------------------------------------------------------------------------- #
# readers (Treasure Data import)
# --------------------------------------------------------------------------- #

def upsert_readers(conn: psycopg.Connection, rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Replace each reader's 30-day history with the latest TD snapshot.

    The source is a rolling 30-day window, so the array is overwritten rather
    than merged -- a story that aged out of TD should age out here too.
    """
    inserted = updated = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO users (email_address, account_key, auth0_id,
                                   articles_read, source_rows, synced_at)
                VALUES (%(email_address)s, %(account_key)s, %(auth0_id)s,
                        %(articles_read)s, %(source_rows)s, now())
                ON CONFLICT (email_address) DO UPDATE SET
                    account_key   = EXCLUDED.account_key,
                    auth0_id      = EXCLUDED.auth0_id,
                    articles_read = EXCLUDED.articles_read,
                    source_rows   = EXCLUDED.source_rows,
                    synced_at     = now()
                RETURNING (xmax = 0) AS inserted
                """,
                row,
            )
            result = cur.fetchone()
            if result and result["inserted"]:
                inserted += 1
            else:
                updated += 1
    conn.commit()
    return inserted, updated


def replace_reads(conn: psycopg.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """Full refresh of the reads table, in one transaction.

    The source is a rolling 30-day window, so replace rather than merge: a
    story that aged out of TD should age out here. Several auth0 ids can map
    to one email, so the upsert folds duplicates by taking the earliest first
    read, the latest last read, and the summed view count.
    """
    n = 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reads")
        for row in rows:
            cur.execute(
                """
                INSERT INTO reads (email_address, story_id, first_read_at,
                                   last_read_at, view_count, synced_at)
                VALUES (%(email_address)s, %(story_id)s, %(first_read_at)s,
                        %(last_read_at)s, %(view_count)s, now())
                ON CONFLICT (email_address, story_id) DO UPDATE SET
                    first_read_at = LEAST(reads.first_read_at, EXCLUDED.first_read_at),
                    last_read_at  = GREATEST(reads.last_read_at, EXCLUDED.last_read_at),
                    view_count    = reads.view_count + EXCLUDED.view_count
                """,
                row,
            )
            n += 1
    conn.commit()
    return n


def reads_summary(conn: psycopg.Connection) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS reads, count(DISTINCT email_address) AS readers,
                   count(DISTINCT story_id) AS stories,
                   min(first_read_at) AS oldest, max(first_read_at) AS newest,
                   round(avg(view_count)::numeric, 2) AS avg_views
              FROM reads
            """)
        return cur.fetchone() or {}


def prune_readers(conn: psycopg.Connection, before) -> int:
    """Drop readers absent from the latest sync (unsubscribed or aged out)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE synced_at < %s", (before,))
        removed = cur.rowcount
    conn.commit()
    return removed


def save_popularity(
    conn: psycopg.Connection, as_of, window_hours: int,
    rows: Iterable[dict[str, Any]], kind: str = "daily",
) -> int:
    """Store a popularity snapshot.

    For kind='at_send' each row carries its own as_of (that story's send-time
    cutoff), so a row per story rather than a shared snapshot timestamp.
    """
    n = 0
    with conn.cursor() as cur:
        for row in rows:
            hours = float(row.get("hours_live") or 0.0)
            views = int(row.get("views") or 0)
            row_as_of = row.get("as_of") or as_of
            cur.execute(
                """
                INSERT INTO story_popularity (story_id, as_of, window_hours,
                    views, uniques, identified, hours_live, views_per_hour,
                    kind, views_total)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (story_id, as_of) DO UPDATE SET
                    views = EXCLUDED.views, uniques = EXCLUDED.uniques,
                    identified = EXCLUDED.identified,
                    hours_live = EXCLUDED.hours_live,
                    views_per_hour = EXCLUDED.views_per_hour,
                    kind = EXCLUDED.kind,
                    views_total = EXCLUDED.views_total
                """,
                (int(row["story_id"]), row_as_of, window_hours, views,
                 int(row.get("uniques") or 0), int(row.get("identified") or 0),
                 hours, views / max(hours, 1.0), kind,
                 int(row["views_total"]) if row.get("views_total") is not None else None),
            )
            n += 1
    conn.commit()
    return n


def load_at_send_popularity(conn: psycopg.Connection) -> dict[int, dict[str, Any]]:
    """Per-story popularity as it stood at that story's send time.

    This is what a ranker could actually have used, unlike an all-time count.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT story_id, views, uniques, identified, hours_live, "
            "views_per_hour, views_total FROM story_popularity "
            "WHERE kind = 'at_send'")
        return {r["story_id"]: r for r in cur.fetchall()}


def load_popularity(
    conn: psycopg.Connection, as_of=None, max_age_hours: int = 36
) -> dict[int, dict[str, Any]]:
    """Most recent popularity snapshot, keyed by story id."""
    with conn.cursor() as cur:
        if as_of is None:
            cur.execute(
                "SELECT max(as_of) AS as_of FROM story_popularity "
                "WHERE as_of >= now() - make_interval(hours => %s)",
                (max_age_hours,),
            )
            row = cur.fetchone()
            as_of = row["as_of"] if row else None
        if as_of is None:
            return {}
        cur.execute(
            "SELECT story_id, views, uniques, identified, hours_live, "
            "views_per_hour FROM story_popularity WHERE as_of = %s", (as_of,))
        return {r["story_id"]: r for r in cur.fetchall()}


# --------------------------------------------------------------------------- #
# reader profiles
# --------------------------------------------------------------------------- #

# Two sources, merged per (reader, story).
#
# Web readers appear in `reads` with a real timestamp, reconstructed from
# pageviews. App readers never touch the web pageview log, so their history
# arrives only as the untimed `users.articles_read` array -- roughly two
# thirds of the panel. Preferring one table wholesale would drop those
# readers entirely, so this takes every read from both and keeps the
# timestamp wherever one exists.
#
# The anti-join is per (reader, story), not per reader, so someone who reads
# in both the app and the browser keeps both halves of their history.
READS_SQL = """
SELECT r.email_address,
       s."storyId"     AS story_id,
       s.embedding,
       s.published,
       s.headline,
       r.first_read_at AS read_at,
       r.view_count
  FROM reads r
  JOIN stories s ON s."storyId" = r.story_id
 WHERE s.embedding IS NOT NULL
   AND s.embed_model = %(model)s
   AND (%(email)s::text IS NULL OR r.email_address = %(email)s)

UNION ALL

SELECT u.email_address,
       s."storyId" AS story_id,
       s.embedding,
       s.published,
       s.headline,
       NULL::timestamptz AS read_at,
       1 AS view_count
  FROM users u
  CROSS JOIN LATERAL unnest(u.articles_read) AS sid
  JOIN stories s ON s."storyId" = sid
  LEFT JOIN reads r ON r.email_address = u.email_address AND r.story_id = sid
 WHERE s.embedding IS NOT NULL
   AND s.embed_model = %(model)s
   AND (%(email)s::text IS NULL OR u.email_address = %(email)s)
   AND r.story_id IS NULL

ORDER BY 1
"""


def reads_coverage(conn: psycopg.Connection) -> dict[str, Any]:
    """How much of the panel has timestamps, for reporting."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS readers,
                   count(*) FILTER (WHERE r.n > 0)  AS with_timestamps,
                   count(*) FILTER (WHERE r.n IS NULL OR r.n = 0) AS array_only
              FROM users u
              LEFT JOIN (SELECT email_address, count(*) AS n FROM reads
                          GROUP BY 1) r ON r.email_address = u.email_address
            """)
        return cur.fetchone() or {}


def load_reads(
    conn: psycopg.Connection,
    model: str,
    user_id: str | None = None,
    limit: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Reader email -> their matched, embedded reads.

    Merges the timestamped `reads` table with the untimed `articles_read`
    array so app-only readers are not dropped. See READS_SQL.
    """
    with conn.cursor() as cur:
        cur.execute(READS_SQL, {"model": model, "email": user_id})
        rows = cur.fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row["email_address"], []).append(row)
    if limit:
        out = dict(list(out.items())[:limit])
    return out


def reader_story_counts(conn: psycopg.Connection, model: str) -> dict[int, int]:
    """How many readers in the panel opened each story.

    Used for rarity weighting inside profile building. Distinct from the
    story_popularity table, which is pageview-derived and used for ranking.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s."storyId" AS story_id, count(DISTINCT u.email_address) AS readers
              FROM users u
              CROSS JOIN LATERAL unnest(u.articles_read) AS sid
              JOIN stories s ON s."storyId" = sid
             WHERE s.embedding IS NOT NULL AND s.embed_model = %s
             GROUP BY 1
            """,
            (model,),
        )
        return {r["story_id"]: r["readers"] for r in cur.fetchall()}


def reader_story_counts_excluding(
    conn: psycopg.Connection, model: str, exclude: set[str]
) -> dict[int, int]:
    if not exclude:
        return reader_story_counts(conn, model)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s."storyId" AS story_id, count(DISTINCT u.email_address) AS readers
              FROM users u
              CROSS JOIN LATERAL unnest(u.articles_read) AS sid
              JOIN stories s ON s."storyId" = sid
             WHERE s.embedding IS NOT NULL AND s.embed_model = %s
               AND NOT (u.email_address = ANY(%s))
             GROUP BY 1
            """,
            (model, list(exclude)),
        )
        return {r["story_id"]: r["readers"] for r in cur.fetchall()}


def users_in_domains(conn: psycopg.Connection, domains: Sequence[str]) -> set[str]:
    """Reader emails in any of these domains (e.g. internal staff)."""
    if not domains:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT email_address FROM users "
            "WHERE lower(split_part(email_address, '@', 2)) = ANY(%s)",
            ([d.strip().lower().lstrip("@") for d in domains],),
        )
        return {r["email_address"] for r in cur.fetchall()}


def load_corpus_mean_vector(
    conn: psycopg.Connection, model: str, mean_id: int | None = None
) -> tuple[int, Any] | None:
    with conn.cursor() as cur:
        if mean_id is not None:
            cur.execute("SELECT id, mean_vector FROM corpus_mean WHERE id = %s",
                        (mean_id,))
        else:
            cur.execute(
                "SELECT id, mean_vector FROM corpus_mean "
                "WHERE embed_model = %s AND is_current", (model,))
        row = cur.fetchone()
    return (row["id"], row["mean_vector"]) if row else None


def save_profiles(
    conn: psycopg.Connection,
    user_id: str,
    clusters: Sequence[Any],
    model: str,
    corpus_mean_id: int | None,
    run_id: int | None = None,
) -> int:
    """Replace a reader's profile wholesale. Rebuilds are idempotent."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_profiles WHERE email_address = %s", (user_id,))
        for index, cluster in enumerate(clusters):
            cur.execute(
                """
                INSERT INTO user_profiles (
                    email_address, cluster_index, centroid, strength,
                    story_count, member_story_ids, label, embed_model,
                    corpus_mean_id, run_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, index, cluster.centroid, cluster.strength,
                 cluster.story_count, cluster.story_ids, cluster.label,
                 model, corpus_mean_id, run_id),
            )
    return len(clusters)


def prune_profiles(conn: psycopg.Connection, before) -> int:
    """Drop profiles not touched by the current full rebuild.

    save_profiles only replaces the readers it is given, so a reader who
    drops out of the source keeps a stale profile -- potentially built under
    a different shape, weighting or corpus mean than everyone else's.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_profiles WHERE built_at < %s", (before,))
        removed = cur.rowcount
    conn.commit()
    return removed


def profile_summary(conn: psycopg.Connection) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(DISTINCT email_address)       AS readers,
                   count(*)                            AS clusters,
                   round(avg(story_count)::numeric, 2) AS avg_stories_per_cluster,
                   max(story_count)                    AS max_stories,
                   max(built_at)                       AS built_at
              FROM user_profiles
            """
        )
        return cur.fetchone() or {}


def load_all_profiles(conn: psycopg.Connection) -> dict[str, list[dict[str, Any]]]:
    """email -> interest clusters, strongest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT email_address, cluster_index, centroid, strength "
            "FROM user_profiles ORDER BY email_address, cluster_index")
        out: dict[str, list[dict[str, Any]]] = {}
        for row in cur.fetchall():
            out.setdefault(row["email_address"], []).append(row)
        return out


def load_read_story_ids(conn: psycopg.Connection) -> dict[str, set[int]]:
    """Stories each reader has already opened -- never recommend these back."""
    with conn.cursor() as cur:
        cur.execute("SELECT email_address, articles_read FROM users")
        return {r["email_address"]: set(r["articles_read"] or ())
                for r in cur.fetchall()}


def all_external_ids(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT email_address FROM users ORDER BY email_address")
        return [r["email_address"] for r in cur.fetchall()]


def load_candidates(
    conn: psycopg.Connection, model: str, since, until
) -> list[dict[str, Any]]:
    """Stories published in the window that are embedded and renderable."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT "storyId" AS story_id, headline, tease, share_url,
                   thumbnail_url, thumbnail_raw, published, embedding
              FROM stories
             WHERE embedding IS NOT NULL
               AND embed_model = %(model)s
               AND published >= %(since)s AND published < %(until)s
               AND share_url IS NOT NULL AND headline <> ''
             ORDER BY published DESC
            """,
            {"model": model, "since": since, "until": until},
        )
        return cur.fetchall()


def create_edition(
    conn: psycopg.Connection, edition_date, layout: str, slots: int,
    corpus_mean_id: int | None, model: str,
    settings: dict[str, Any] | None = None,
) -> int:
    """Create an edition, stamped with the settings it is running under."""
    settings = settings or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO email_edition (
                edition_date, layout, slots, corpus_mean_id, embed_model,
                hybrid_weight, editor_section_front,
                profile_cluster, profile_recency, profile_rarity)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (edition_date, layout, slots, corpus_mean_id, model,
             settings.get("hybrid_weight"), settings.get("editor_section_front"),
             settings.get("profile_cluster"), settings.get("profile_recency"),
             settings.get("profile_rarity")),
        )
        edition_id = cur.fetchone()["id"]
    conn.commit()
    return edition_id


def save_edition_stories(
    conn: psycopg.Connection, edition_id: int, rows: Sequence[dict[str, Any]]
) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO edition_story (edition_id, story_id, headline, tease,
                                       share_url, thumbnail_url, thumbnails, pub_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (edition_id, story_id) DO NOTHING
            """,
            [(edition_id, r["story_id"], r["headline"], r["tease"], r["share_url"],
              r["thumbnail_url"], Jsonb(r.get("thumbnails") or {}), r["published"])
             for r in rows],
        )
    conn.commit()
    return len(rows)


def save_selections(
    conn: psycopg.Connection, edition_id: int, batch: Sequence[tuple]
) -> int:
    """batch: (external_user_id, slot, story_id, source, score, cluster_index)"""
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO edition_selection (edition_id, external_user_id, slot,
                                           story_id, source, score, cluster_index)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (edition_id, external_user_id, slot) DO NOTHING
            """,
            [(edition_id, *row) for row in batch],
        )
    return len(batch)


def lookup_key(email: str, salt: str) -> str | None:
    """sha256(salt || lower(email)) -- lets the feed URL omit the address."""
    if not salt:
        return None
    return hashlib.sha256(f"{salt}{email.strip().lower()}".encode()).hexdigest()


def save_payloads(
    conn: psycopg.Connection, edition_id: int, batch: Sequence[tuple],
    salt: str = "",
) -> int:
    """batch: (external_user_id, payload_dict)"""
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO edition_payload (edition_id, external_user_id,
                                         payload, lookup_key)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (edition_id, external_user_id)
            DO UPDATE SET payload = EXCLUDED.payload,
                          lookup_key = EXCLUDED.lookup_key
            """,
            [(edition_id, uid, Jsonb(payload), lookup_key(uid, salt))
             for uid, payload in batch],
        )
    return len(batch)


def publish_edition(conn: psycopg.Connection, edition_id: int) -> None:
    """Flip to 'ready' in one statement, superseding the day's previous one.

    Until this runs the endpoint cannot see the edition, so a job that dies
    midway leaves yesterday's edition serving rather than a partial one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE email_edition SET status = 'superseded'
             WHERE status = 'ready'
               AND edition_date = (SELECT edition_date FROM email_edition WHERE id = %s)
            """,
            (edition_id,),
        )
        cur.execute(
            "UPDATE email_edition SET status = 'ready', published_at = now() "
            "WHERE id = %s",
            (edition_id,),
        )
    conn.commit()


def edition_summary(conn: psycopg.Connection, limit: int = 5) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.edition_date, e.status, e.layout, e.published_at,
                   (SELECT count(*) FROM edition_payload p WHERE p.edition_id = e.id)
                       AS payloads,
                   (SELECT count(*) FROM edition_story s WHERE s.edition_id = e.id)
                       AS stories
              FROM email_edition e
             ORDER BY e.edition_date DESC, e.id DESC LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def popularity_match_rate(conn: psycopg.Connection, as_of) -> int:
    with conn.cursor() as cur:
        cur.execute(
            'SELECT count(*) AS n FROM story_popularity p '
            'JOIN stories s ON s."storyId" = p.story_id '
            'WHERE p.as_of = %s AND s.embedding IS NOT NULL', (as_of,))
        return cur.fetchone()["n"]


def popularity_summary(conn: psycopg.Connection) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT max(as_of) AS as_of FROM story_popularity")
        row = cur.fetchone()
        if not row or not row["as_of"]:
            return {}
        as_of = row["as_of"]
        cur.execute(
            """
            SELECT %s::timestamptz AS as_of, count(*) AS stories,
                   sum(views) AS views,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY views) AS median_views
              FROM story_popularity WHERE as_of = %s
            """, (as_of, as_of))
        return cur.fetchone() or {}
