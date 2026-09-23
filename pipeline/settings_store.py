"""Bridges the admin-editable settings row into the Django-free pipeline.

The scoring code takes a plain frozen ``Config``; this module reads the
database row and returns an updated copy. Precedence is:

    explicit command-line flag  >  pipeline_settings row  >  .env default

Secrets (API keys, the database URL) are never sourced from the database.
"""
from __future__ import annotations

import dataclasses
import logging

log = logging.getLogger(__name__)

# Config field <- model field. Only non-secret tunables appear here.
_FIELDS = {
    "hybrid_enabled": "hybrid_enabled",
    "hybrid_weight": "hybrid_weight",
    "alpha": "alpha",
    "max_pair_sim": "max_pair_sim",
    "layout": "layout",
    "editor_section_front": "editor_section_front",
    "require_thumbnail": "require_thumbnail",
    "profile_cluster": "profile_cluster",
    "profile_recency": "profile_recency",
    "profile_rarity": "profile_rarity",
    "corpus_mean_window_days": "corpus_mean_window_days",
    "popularity_window_hours": "popularity_window_hours",
}


def _ensure_django() -> None:
    """Boot Django if it is not already running.

    Both entry points must honour the admin-editable settings. Without this,
    `python -m adgvec match` would silently ignore a weight someone had just
    changed in the GUI while `manage.py match` respected it -- the kind of
    split that makes a tuning UI untrustworthy.
    """
    import os

    from django.apps import apps

    if apps.ready:
        return
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()


def apply_overrides(cfg):
    """Return cfg with database settings applied, or cfg unchanged on failure.

    Deliberately forgiving: the CLI must keep working before the table exists
    or without Django configured at all.
    """
    try:
        _ensure_django()
        from .models import PipelineSettings

        row = PipelineSettings.load()
    except Exception as exc:  # noqa: BLE001
        log.debug("no pipeline_settings row available (%s); using .env", exc)
        return cfg

    changes = {}
    for cfg_field, model_field in _FIELDS.items():
        if hasattr(cfg, cfg_field):
            changes[cfg_field] = getattr(row, model_field)
    if hasattr(cfg, "popularity_hosts"):
        hosts = tuple(h.strip() for h in (row.popularity_hosts or "").split(",")
                      if h.strip())
        if hosts:
            changes["popularity_hosts"] = hosts
    return dataclasses.replace(cfg, **changes) if changes else cfg
