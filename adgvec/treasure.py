"""Read-only Treasure Data client.

Two imports run nightly:

* **Reader history** from ``reporting_environment.adg_articles_past_thirty``
  -- one row per (account, email) with a 30-day array of Ellington story ids.
* **Story popularity** from ``raw_web.pageviews`` -- view counts for recently
  published stories, normalised by how long each has been live.

This module never writes to Treasure Data. Every statement it issues is
checked against a deny-list before it is sent, so a mistake in query
construction cannot mutate the warehouse.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, Sequence

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://api.treasuredata.com"
HISTORY_DB = "reporting_environment"
HISTORY_TABLE = "adg_articles_past_thirty"
PAGEVIEW_DB = "raw_web"
PAGEVIEW_TABLE = "pageviews"

# Anything that could alter Treasure Data. Checked on every statement.
FORBIDDEN = (
    "insert", "create", "drop", "delete", "update", "alter", "truncate",
    "rename", "grant", "revoke", "merge", "replace", "set ", "call ",
)


class ReadOnlyViolation(RuntimeError):
    pass


def assert_read_only(sql: str) -> None:
    text = " ".join(sql.lower().split())
    if not text.lstrip("( ").startswith(("select", "with")):
        raise ReadOnlyViolation("only SELECT/WITH statements are permitted")
    for word in FORBIDDEN:
        token = word if word.endswith(" ") else f"{word} "
        if f" {token}" in f" {text} " or text.startswith(token):
            raise ReadOnlyViolation(f"statement contains {word.strip()!r}")


class TreasureDataClient:
    def __init__(self, api_key: str, endpoint: str = DEFAULT_ENDPOINT,
                 timeout: int = 900) -> None:
        if not api_key:
            raise RuntimeError("Missing TD_API_KEY. Set it in .env.")
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def _call(self, path: str, data: dict | None = None, timeout: int = 120) -> Any:
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(
            f"{self.endpoint}{path}", data=body,
            headers={"Authorization": f"TD1 {self.api_key}",
                     "Accept": "application/json",
                     "User-Agent": "adg-personalization/0.1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise RuntimeError(f"Treasure Data auth failed ({exc.code}). "
                                   f"Check TD_API_KEY. {detail}") from exc
            raise RuntimeError(f"Treasure Data {exc.code}: {detail}") from exc

    def query(self, database: str, sql: str, poll: int = 5) -> Iterator[dict]:
        assert_read_only(sql)
        job = self._call(f"/v3/job/issue/presto/{database}", {"query": sql})
        job_id = job["job_id"]
        log.info("TD job %s issued against %s", job_id, database)

        waited = 0
        while waited < self.timeout:
            status = self._call(f"/v3/job/status/{job_id}")
            if status["status"] in ("success", "error", "killed"):
                break
            time.sleep(poll)
            waited += poll
        else:
            raise TimeoutError(f"TD job {job_id} still running after {self.timeout}s")

        detail = self._call(f"/v3/job/show/{job_id}")
        if status["status"] != "success":
            raise RuntimeError(
                f"TD job {job_id} {status['status']}: {str(detail.get('debug'))[:600]}")

        schema = detail["hive_result_schema"]
        if isinstance(schema, str):
            schema = json.loads(schema)
        columns = [c[0] for c in schema]

        req = urllib.request.Request(
            f"{self.endpoint}/v3/job/result/{job_id}?format=json",
            headers={"Authorization": f"TD1 {self.api_key}"})
        with urllib.request.urlopen(req, timeout=600) as r:
            for line in r:
                line = line.strip()
                if line:
                    yield dict(zip(columns, json.loads(line)))

    # ----------------------------------------------------------------- #

    def reader_history(self, limit: int | None = None) -> Iterator[dict]:
        """30-day reading history, one row per (auth0 id, email).

        Emails are lower-cased and trimmed here so the merge downstream is
        exact; TD holds duplicates per address.

        The source dropped its `account_key` column when it was rebuilt on
        2026-09-23. `users.account_key` is left in place, unpopulated, so the
        column is ready if it ever comes back.
        """
        sql = f"""
            SELECT lower(trim(email_address)) AS email_address,
                   arbitrary(auth0_id)        AS auth0_id,
                   array_distinct(flatten(array_agg(articles_read))) AS articles_read,
                   count(*)                   AS source_rows
              FROM {HISTORY_TABLE}
             WHERE email_address IS NOT NULL AND trim(email_address) <> ''
             GROUP BY lower(trim(email_address))
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query(HISTORY_DB, sql)

    def story_popularity(
        self,
        window_hours: int = 24,
        published_within_hours: int = 48,
        human_only: bool = True,
        hosts: Sequence[str] = ("www.arkansasonline.com",),
    ) -> Iterator[dict]:
        """View counts for recently published stories.

        `views` alone favours whatever was published earliest in the window,
        so `hours_live` is returned alongside and ranking uses views per hour.

        Bot traffic is excluded by default. Rows with a null traffic_type are
        treated as non-human: they carry almost no article_id values, so
        including them adds noise rather than signal.

        `hosts` matters more than it looks: raw_web.pageviews covers every
        WEHCO title, and on a normal day nwaonline.com outweighs
        arkansasonline.com. Unfiltered, roughly 70% of the counted volume
        would be other papers' readers, and a story syndicated across titles
        would outrank a genuine ADG lead.
        """
        traffic = "AND traffic_type = 'human'" if human_only else ""
        host_list = ", ".join(
            "'" + h.replace("'", "''") + "'" for h in hosts if h.strip())
        host_filter = f"AND td_host IN ({host_list})" if host_list else ""
        return self.query(PAGEVIEW_DB, f"""
            WITH v AS (
              SELECT CAST(article_id AS bigint) AS story_id,
                     from_iso8601_timestamp(article_publish_date) AS published,
                     td_client_id, email_address
                FROM {PAGEVIEW_TABLE}
               WHERE time >= to_unixtime(now()) - {int(window_hours) * 3600}
                 {traffic}
                 {host_filter}
                 AND article_id IS NOT NULL AND article_id <> ''
                 AND regexp_like(article_id, '^[0-9]+$')
                 AND article_publish_date IS NOT NULL
                 AND article_publish_date <> ''
                 AND from_iso8601_timestamp(article_publish_date)
                       >= now() - interval '{int(published_within_hours)}' hour
                 AND from_iso8601_timestamp(article_publish_date) <= now()
            )
            SELECT story_id,
                   count(*)                       AS views,
                   count(DISTINCT td_client_id)   AS uniques,
                   count(DISTINCT email_address) FILTER (
                       WHERE email_address IS NOT NULL AND email_address <> ''
                   )                              AS identified,
                   date_diff('minute', max(published), now()) / 60.0 AS hours_live
              FROM v
             GROUP BY story_id
        """)

    def historical_popularity(
        self,
        pub_since: str,
        pub_until: str,
        hosts: Sequence[str] = ("www.arkansasonline.com",),
        send_hour_local: int = 5,
        timezone: str = "America/Chicago",
        human_only: bool = True,
    ) -> Iterator[dict]:
        """Views each story had accumulated by its own send time.

        Backtesting against an all-time view count gives popularity credit for
        readership it could not have known about -- measured on a sample day,
        only ~58% of a story's eventual views had landed by the next 5am. This
        counts views up to the first `send_hour_local` after publication, which
        is what the morning job would actually have seen, and also returns the
        all-time total so the two can be compared.

        Dates are naive local-day boundaries, e.g. "2026-08-20".
        """
        traffic = "AND traffic_type = 'human'" if human_only else ""
        host_list = ", ".join(
            "'" + h.replace("'", "''") + "'" for h in hosts if h.strip())
        host_filter = f"AND td_host IN ({host_list})" if host_list else ""
        # Views land after publication, so the scan window must run past the
        # last publication day far enough to include its send cutoff.
        return self.query(PAGEVIEW_DB, f"""
            WITH v AS (
              SELECT CAST(article_id AS bigint) AS story_id,
                     time AS view_ts,
                     to_unixtime(from_iso8601_timestamp(article_publish_date)) AS pub_ts,
                     to_unixtime(date_trunc('day',
                         from_iso8601_timestamp(article_publish_date)
                             AT TIME ZONE '{timezone}')
                         + interval '{24 + int(send_hour_local)}' hour) AS cutoff_ts,
                     td_client_id, email_address
                FROM {PAGEVIEW_TABLE}
               WHERE time >= to_unixtime(timestamp '{pub_since} 00:00:00')
                 AND time <  to_unixtime(timestamp '{pub_until} 00:00:00')
                            + {(24 + int(send_hour_local)) * 3600}
                 {traffic}
                 {host_filter}
                 AND article_id IS NOT NULL AND article_id <> ''
                 AND regexp_like(article_id, '^[0-9]+$')
                 AND article_publish_date IS NOT NULL
                 AND article_publish_date <> ''
            )
            SELECT story_id,
                   max(cutoff_ts)                                  AS cutoff_ts,
                   min(pub_ts)                                     AS pub_ts,
                   count(*) FILTER (WHERE view_ts <= cutoff_ts)    AS views,
                   count(DISTINCT td_client_id)
                       FILTER (WHERE view_ts <= cutoff_ts)         AS uniques,
                   count(DISTINCT email_address) FILTER (
                       WHERE view_ts <= cutoff_ts
                         AND email_address IS NOT NULL
                         AND email_address <> '')                  AS identified,
                   count(*)                                        AS views_total
              FROM v
             WHERE pub_ts >= to_unixtime(timestamp '{pub_since} 00:00:00')
               AND pub_ts <  to_unixtime(timestamp '{pub_until} 00:00:00')
             GROUP BY story_id
        """)

    def reader_history_timestamped(
        self,
        days: int = 30,
        hosts: Sequence[str] = ("www.arkansasonline.com",),
        human_only: bool = False,
    ) -> Iterator[dict]:
        """Per-read detail with timestamps, for the subscriber panel.

        Joins pageviews on auth0_id -- which appears on far more article views
        than email_address and recovers 100% of the reads the aggregate table
        knows about, against 63% by email -- then translates to email here, so
        nothing downstream ever sees an auth0 id as an identifier.

        `human_only` defaults off: an auth0 session is a signed-in reader, and
        filtering on traffic_type loses rows the aggregate table includes.
        """
        traffic = "AND traffic_type = 'human'" if human_only else ""
        host_list = ", ".join(
            "'" + h.replace("'", "''") + "'" for h in hosts if h.strip())
        host_filter = f"AND td_host IN ({host_list})" if host_list else ""
        return self.query(HISTORY_DB, f"""
            WITH panel AS (
              SELECT lower(trim(auth0_id)) AS auth0,
                     arbitrary(lower(trim(email_address))) AS email
                FROM {HISTORY_TABLE}
               WHERE auth0_id IS NOT NULL AND trim(auth0_id) <> ''
                 AND email_address IS NOT NULL AND trim(email_address) <> ''
               GROUP BY lower(trim(auth0_id))
            ),
            pv AS (
              SELECT lower(trim(auth0_id)) AS auth0,
                     CAST(article_id AS bigint) AS story_id,
                     min(time) AS first_ts,
                     max(time) AS last_ts,
                     count(*)  AS view_count
                FROM {PAGEVIEW_DB}.{PAGEVIEW_TABLE}
               WHERE time >= to_unixtime(now()) - {int(days)} * 86400
                 {traffic}
                 {host_filter}
                 AND article_id IS NOT NULL AND article_id <> ''
                 AND regexp_like(article_id, '^[0-9]+$')
                 AND auth0_id IS NOT NULL AND trim(auth0_id) <> ''
               GROUP BY 1, 2
            )
            SELECT p.email AS email_address, pv.story_id,
                   pv.first_ts, pv.last_ts, pv.view_count
              FROM pv JOIN panel p ON p.auth0 = pv.auth0
        """)
