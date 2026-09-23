"""Minimal client for the Ellington REST API (/news/story/, HTTP Basic auth)."""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Sequence

log = logging.getLogger(__name__)

# NOTE: the MCP server's docs claim 1=Draft/2=Scheduled/3=Published, but on
# this install status=1 is the live state (1.04M rows, current to today)
# while status=3 has been stale since April 2026. Verified against live
# share_urls on arkansasonline.com. Override with ELLINGTON_STATUS if needed.
STATUS_PUBLISHED = 1
PAGE_SIZE = 100

# `thumbnails` is a dict of named crops: "original", "medium" (640x480
# landscape) and "small" (729x923 portrait). Landscape reads better in an
# email row, so medium leads.
THUMBNAIL_PREFERENCE = ("medium", "small", "original")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Story:
    story_id: int
    headline: str
    tease: str
    kicker: str
    share_url: str | None
    thumbnail_url: str | None
    thumbnail_raw: str | None
    published: datetime | None
    updated_at: datetime | None
    subscription_required: bool | None
    raw: dict[str, Any]


def strip_html(value: str | None) -> str:
    """Teases routinely contain markup; embed the prose, not the tags."""
    if not value:
        return ""
    import html as _html

    text = _TAG_RE.sub(" ", value)
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def extract_thumbnail_url(
    value: Any,
    preferred: Sequence[str] = THUMBNAIL_PREFERENCE,
    _depth: int = 0,
) -> str | None:
    """Pull a URL out of Ellington's `thumbnails` field.

    In practice this is a dict keyed by crop name, but the API schema types it
    only as "string", so a bare URL, a JSON-encoded object and nested shapes
    are all handled. The raw value is stored in thumbnail_raw alongside, so a
    different crop can be chosen later without refetching.
    """
    if value is None or _depth > 6:
        return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith(("http://", "https://")):
            return text
        if text.startswith(("{", "[")):
            try:
                return extract_thumbnail_url(json.loads(text), preferred, _depth + 1)
            except json.JSONDecodeError:
                pass
        match = _URL_RE.search(text)
        return match.group(0) if match else None

    if isinstance(value, dict):
        # Named crops first, in preference order, then generic keys, then any
        # URL-shaped value at all.
        for key in (*preferred, "url", "thumbnail", "thumb", "large", "src"):
            if key in value:
                found = extract_thumbnail_url(value[key], preferred, _depth + 1)
                if found:
                    return found
        for nested in value.values():
            found = extract_thumbnail_url(nested, preferred, _depth + 1)
            if found:
                return found
        return None

    if isinstance(value, (list, tuple)):
        for item in value:
            found = extract_thumbnail_url(item, preferred, _depth + 1)
            if found:
                return found
    return None


def _naive_local(value: datetime) -> str:
    """Format a datetime the way this Ellington install expects: no offset."""
    return value.replace(tzinfo=None).isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value[: len(fmt) + 2], fmt)
            except ValueError:
                continue
    log.warning("could not parse datetime %r", value)
    return None


def to_story(
    record: dict[str, Any],
    thumbnail_preference: Sequence[str] = THUMBNAIL_PREFERENCE,
) -> Story:
    raw_thumb = record.get("thumbnails")
    raw_thumb_text = (
        raw_thumb if isinstance(raw_thumb, str) else json.dumps(raw_thumb)
    ) if raw_thumb not in (None, "") else None

    return Story(
        story_id=int(record["id"]),
        headline=strip_html(record.get("headline")),
        # `tease` is the intended field; `description`/`subhead` are decent
        # stand-ins when an editor left it blank, which is common.
        tease=strip_html(
            record.get("tease") or record.get("description") or record.get("subhead")
        ),
        kicker=strip_html(record.get("kicker")),
        share_url=(record.get("share_url") or None),
        thumbnail_url=extract_thumbnail_url(raw_thumb, thumbnail_preference)
        or extract_thumbnail_url(record.get("tease_photo"), thumbnail_preference),
        thumbnail_raw=raw_thumb_text,
        published=_parse_dt(record.get("pub_date")),
        updated_at=_parse_dt(record.get("update_date") or record.get("updated")),
        subscription_required=record.get("subscription_required"),
        raw=record,
    )


class EllingtonClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        auth_scheme: str = "Bearer",
        site_id: str | None = None,
        timeout: int = 60,
        thumbnail_preference: Sequence[str] = THUMBNAIL_PREFERENCE,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.site_id = site_id
        self.thumbnail_preference = tuple(thumbnail_preference)
        self.timeout = timeout
        self._token = token
        self._scheme = auth_scheme or "Bearer"
        # DRF's built-in TokenAuthentication expects "Token", not "Bearer".
        # Rather than make the operator guess, try the configured scheme and
        # fall back to the other one once on a 401.
        self._fallback = "Token" if self._scheme.lower() == "bearer" else "Bearer"
        self._fallback_used = False

    def _request(self, url: str, scheme: str) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            headers={
                "Authorization": f"{scheme} {self._token}",
                "Accept": "application/json",
                "User-Agent": "adg-personalization/0.1",
            },
        )

    def _get(self, url: str, retries: int = 3) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                request = self._request(url, self._scheme)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read()[:400].decode("utf-8", "replace")
                if exc.code in (401, 403):
                    if not self._fallback_used:
                        self._fallback_used = True
                        log.warning(
                            "%s auth rejected (%s); retrying with %s",
                            self._scheme, exc.code, self._fallback,
                        )
                        self._scheme, self._fallback = self._fallback, self._scheme
                        continue
                    raise RuntimeError(
                        f"Ellington auth failed ({exc.code}) with both "
                        f"'{self._scheme}' and '{self._fallback}' schemes. "
                        f"Check ELLINGTON_TOKEN. {body}"
                    ) from exc
                if exc.code < 500 and exc.code != 429:
                    raise RuntimeError(f"Ellington {exc.code} for {url}: {body}") from exc
                last = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
            sleep_for = 2**attempt
            log.warning("request failed (%s), retrying in %ss", last, sleep_for)
            time.sleep(sleep_for)
        raise RuntimeError(f"Ellington request failed after {retries} attempts: {last}")

    def section_front_story_ids(self, section_id: int) -> list[int]:
        """Ordered story ids from a section front's editor-curated slots.

        Section front 186 ("Morning News Update - A") carries `lead_story`
        (one story) and `additional_stories` (a list), which is exactly the
        five editor picks in priority order. Other `pieces` keys on that front
        -- email_title, other_newsletters, bottom_navigation -- are template
        furniture and are ignored.
        """
        payload = self._get(f"{self.base_url}/sectionfronts/{int(section_id)}/")
        pieces = payload.get("pieces") or {}
        ids: list[int] = []

        def add(value: Any) -> None:
            if isinstance(value, dict):
                story_id = value.get("id")
                # Only real stories; furniture blocks carry other object types.
                kind = value.get("ellington_object_type") or ""
                if story_id and ("story" in str(kind).lower() or "share_url" in value):
                    if int(story_id) not in ids:
                        ids.append(int(story_id))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    add(item)

        # Lead first, then the additional slots, in the order the desk set.
        add(pieces.get("lead_story"))
        add(pieces.get("additional_stories"))
        log.info("section front %s: %d editor picks (%s)",
                 section_id, len(ids), payload.get("name"))
        return ids

    def iter_stories(
        self,
        since: datetime,
        until: datetime | None = None,
        limit: int | None = None,
        status: int | None = STATUS_PUBLISHED,
    ) -> Iterator[Story]:
        # pub_date is stored naive here -- sending an offset-aware value
        # matches zero rows, so boundaries are converted to the site's local
        # wall clock and sent without an offset.
        params: dict[str, Any] = {
            "pub_date__gte": _naive_local(since),
            "ordering": "-pub_date",
            "limit": min(PAGE_SIZE, limit) if limit else PAGE_SIZE,
            "offset": 0,
        }
        if until is not None:
            params["pub_date__lte"] = _naive_local(until)
        if status is not None:
            params["status"] = status
        if self.site_id:
            params["sites"] = self.site_id

        url = f"{self.base_url}/news/story/?{urllib.parse.urlencode(params)}"
        yielded = 0
        while url:
            payload = self._get(url)
            results = payload.get("results", [])
            if not results:
                break
            for record in results:
                yield to_story(record, self.thumbnail_preference)
                yielded += 1
                if limit and yielded >= limit:
                    return
            url = payload.get("next") or ""
