"""Configuration, loaded from the environment / a .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    database_url: str
    ellington_base_url: str
    ellington_token: str
    ellington_auth_scheme: str
    ellington_status: int
    editor_section_front: int
    ellington_site_id: str | None
    ellington_tz: ZoneInfo
    openai_token: str
    embed_model: str
    embed_dimensions: int
    corpus_mean_window_days: int
    td_api_key: str
    td_endpoint: str
    popularity_window_hours: int
    popularity_published_within_hours: int
    popularity_hosts: tuple[str, ...]
    feed_hash_salt: str
    # Ranking / layout tunables. Overridden by the pipeline_settings row when
    # the Django app is installed, and by explicit command-line flags above
    # both.
    hybrid_enabled: bool
    hybrid_weight: float
    alpha: float
    max_pair_sim: float
    layout: str
    require_thumbnail: bool
    profile_cluster: bool
    profile_recency: bool
    profile_rarity: bool

    def require_ellington(self) -> None:
        if not self.ellington_token:
            raise ConfigError(
                "Missing ELLINGTON_TOKEN. Copy .env.example to .env and fill it in."
            )

    def require_td(self) -> None:
        if not self.td_api_key:
            raise ConfigError("Missing TD_API_KEY. Set it in .env.")

    def require_openai(self) -> None:
        if not self.openai_token:
            raise ConfigError(
                "Missing OPEN_AI_TOKEN. Set it in .env (or export it) before embedding."
            )


def load_config(env_file: str | None = None) -> Config:
    load_dotenv(env_file or PROJECT_ROOT / ".env")

    tz_name = os.getenv("ELLINGTON_TZ", "America/Chicago")
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # pragma: no cover - bad tz is a config error
        raise ConfigError(f"Invalid ELLINGTON_TZ {tz_name!r}: {exc}") from exc

    site_id = (os.getenv("ELLINGTON_SITE_ID") or "").strip() or None

    return Config(
        database_url=os.getenv("DATABASE_URL", "postgresql:///personalization"),
        ellington_base_url=os.getenv(
            "ELLINGTON_BASE_URL", "https://www.arkansasonline.com/api"
        ).rstrip("/"),
        ellington_token=(os.getenv("ELLINGTON_TOKEN") or "").strip(),
        ellington_auth_scheme=os.getenv("ELLINGTON_AUTH_SCHEME", "Bearer").strip(),
        ellington_status=int(os.getenv("ELLINGTON_STATUS", "1")),
        editor_section_front=int(os.getenv("EDITOR_SECTION_FRONT", "186")),
        ellington_site_id=site_id,
        ellington_tz=tz,
        # OPEN_AI_TOKEN is the name in use here; OPENAI_API_KEY accepted as a fallback.
        openai_token=os.getenv("OPEN_AI_TOKEN") or os.getenv("OPENAI_API_KEY", ""),
        embed_model=os.getenv("EMBED_MODEL", "text-embedding-3-large"),
        embed_dimensions=int(os.getenv("EMBED_DIMENSIONS", "1536")),
        corpus_mean_window_days=int(os.getenv("CORPUS_MEAN_WINDOW_DAYS", "30")),
        td_api_key=(os.getenv("TD_API_KEY") or "").strip(),
        td_endpoint=os.getenv("TD_ENDPOINT", "https://api.treasuredata.com"),
        popularity_window_hours=int(os.getenv("POPULARITY_WINDOW_HOURS", "24")),
        popularity_published_within_hours=int(
            os.getenv("POPULARITY_PUBLISHED_WITHIN_HOURS", "48")),
        popularity_hosts=tuple(
            h.strip() for h in os.getenv(
                "POPULARITY_HOSTS", "www.arkansasonline.com").split(",") if h.strip()),
        feed_hash_salt=os.getenv("FEED_HASH_SALT", ""),
        hybrid_enabled=os.getenv("HYBRID_ENABLED", "1") == "1",
        hybrid_weight=float(os.getenv("HYBRID_WEIGHT", "0.6")),
        alpha=float(os.getenv("MATCH_ALPHA", "0.4")),
        max_pair_sim=float(os.getenv("MAX_PAIR_SIM", "0.70")),
        layout=os.getenv("MATCH_LAYOUT", "EEEPP"),
        require_thumbnail=os.getenv("REQUIRE_THUMBNAIL", "1") == "1",
        profile_cluster=os.getenv("PROFILE_CLUSTER", "0") == "1",
        profile_recency=os.getenv("PROFILE_RECENCY", "0") == "1",
        profile_rarity=os.getenv("PROFILE_RARITY", "0") == "1",
    )
