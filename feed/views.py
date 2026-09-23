"""Connected Content endpoint.

Braze fires one request per recipient at send time, so a send is a burst of
concurrent requests under a short timeout. The contract here is therefore
narrow and strict:

* one primary-key lookup of a pre-rendered blob -- no joins, no serialization
* **always HTTP 200**, on every error path. A personalization outage should
  cost personalization, not the newsletter.
"""
from __future__ import annotations

import json
import logging
import time

from django.conf import settings
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)

DEFAULT_USER_KEY = "__default__"
# Absolute last resort if even the default payload cannot be read.
EMPTY_PAYLOAD = {"id": None, "name": "Morning News Update",
                 "date_effective": None, "pieces": {"stories": []}}

_edition_cache: dict[str, float | int | None] = {"id": None, "at": 0.0}


def _set_timeout(cur) -> None:
    cur.execute("SET LOCAL statement_timeout = %s", (settings.DB_STATEMENT_TIMEOUT_MS,))


def _current_edition_id() -> int | None:
    """Newest ready edition, cached briefly.

    Resolving this once turns the per-request query into a true primary-key
    lookup on (edition_id, external_user_id).
    """
    now = time.monotonic()
    if (_edition_cache["id"] is not None
            and now - float(_edition_cache["at"]) < settings.EDITION_CACHE_SECONDS):
        return int(_edition_cache["id"])
    with connection.cursor() as cur:
        _set_timeout(cur)
        cur.execute(
            "SELECT id FROM email_edition WHERE status = 'ready' "
            "ORDER BY edition_date DESC, id DESC LIMIT 1"
        )
        row = cur.fetchone()
    _edition_cache["id"] = row[0] if row else None
    _edition_cache["at"] = now
    return _edition_cache["id"]


def _payload(edition_id: int, uid: str, by_hash: bool = False):
    column = "lookup_key" if by_hash else "external_user_id"
    with connection.cursor() as cur:
        _set_timeout(cur)
        cur.execute(
            f"SELECT payload FROM edition_payload "
            f"WHERE edition_id = %s AND {column} = %s",
            (edition_id, uid),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _authorized(request) -> bool:
    if not settings.FEED_TOKEN:
        return True  # unset in dev; set it in production
    return request.META.get(settings.FEED_TOKEN_HEADER, "") == settings.FEED_TOKEN


@require_GET
def morning_email(request, uid: str | None = None):
    if not _authorized(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    # Readers are keyed by email address. Braze URL-encodes the "@"; Django
    # has already decoded it. Normalised to lower case to match the import.
    key = (request.GET.get("k") or "").strip().lower()
    uid = (uid or request.GET.get("uid") or "").strip().lower()

    served, payload = "default", None
    try:
        edition_id = _current_edition_id()
        if edition_id is None:
            log.error("no ready edition; serving empty payload")
        else:
            # `k` is sha256(salt || email): preferred, because it keeps the
            # address out of logs and referrers. `uid` accepts a raw email.
            if key:
                payload = _payload(edition_id, key, by_hash=True)
                if payload is not None:
                    served = "personalized"
            if payload is None and uid:
                payload = _payload(edition_id, uid)
                if payload is not None:
                    served = "personalized"
            if payload is None:
                payload = _payload(edition_id, DEFAULT_USER_KEY)
    except Exception:  # noqa: BLE001 - never fail a send over this
        log.exception("morning_email lookup failed; serving empty payload")
        payload = None
        # A failed query can leave the connection unusable for the next one.
        _edition_cache["at"] = 0.0

    if payload is None:
        body = json.dumps(EMPTY_PAYLOAD)
    elif isinstance(payload, str):
        # Django's psycopg3 backend returns jsonb as text. Pass it straight
        # through -- it is already valid JSON, and re-parsing then
        # re-serializing it on every request would be pure waste.
        body = payload
    else:
        body = json.dumps(payload)

    response = HttpResponse(body, content_type="application/json")
    # Per-user URLs make shared caching useless, and a stale email is worse
    # than a slow one.
    response["Cache-Control"] = "no-store"
    response["X-Feed-Served"] = served
    return response


@require_GET
def healthz(request):
    """Pre-send check: is there a ready edition, and how big is it?"""
    try:
        with connection.cursor() as cur:
            _set_timeout(cur)
            cur.execute(
                """
                SELECT e.id, e.edition_date, e.published_at,
                       (SELECT count(*) FROM edition_payload p
                         WHERE p.edition_id = e.id) AS payloads
                  FROM email_edition e
                 WHERE e.status = 'ready'
                 ORDER BY e.edition_date DESC, e.id DESC LIMIT 1
                """
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({"ok": False, "error": str(exc)}, status=503)
    if not row:
        return JsonResponse({"ok": False, "error": "no ready edition"}, status=503)
    return JsonResponse({
        "ok": True, "edition_id": row[0], "edition_date": str(row[1]),
        "published_at": row[2].isoformat() if row[2] else None, "payloads": row[3],
    })
