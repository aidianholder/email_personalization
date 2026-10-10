"""Launching pipeline jobs from the admin.

A run takes minutes -- `match` alone scores sixteen thousand readers -- so it
cannot happen inside the web request: the browser would time out and a worker
would be tied up for the duration. Instead the job is started as a detached
subprocess and the admin redirects immediately. Progress shows up in the
Pipeline runs screen, which the job writes to as it goes.

Detached (`start_new_session`) rather than a thread, so restarting or
redeploying the web server does not kill a run in flight.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

LOG_DIR = settings.BASE_DIR / "logs"

#: slug -> (management command, extra args, label shown in the admin)
JOBS = {
    "match": ("match", [], "Rebuild today's email"),
    "stories": ("run", ["--hours", "36"], "Refresh stories from the CMS"),
    "popularity": ("sync_popularity", [], "Refresh traffic numbers"),
    "nightly": ("nightly", [], "Full nightly run"),
}

#: A run already going for this long is treated as stuck, not active.
STALE_AFTER = timedelta(hours=2)
#: How long after a launch to assume the job is still booting up. A
#: subprocess needs a moment to start Django and write its own run row.
BOOTING_FOR = timedelta(seconds=90)


def running_job():
    """A description of the job in flight, or None.

    Guards the button against double-clicks. The advisory lock already makes
    a second run harmless, but it fails with a stack trace rather than
    something an editor can read.

    Checks two things, because a run is invisible for its first few seconds:
    an active row in the run log, and failing that a launch too recent for
    its subprocess to have registered yet.
    """
    from .models import PipelineLaunch, PipelineRun

    now = timezone.now()
    active = (PipelineRun.objects
              .filter(status="running", started_at__gte=now - STALE_AFTER)
              .order_by("-started_at").first())
    if active:
        return {"command": active.command, "since": active.started_at}

    booting = (PipelineLaunch.objects
               .filter(launched_at__gte=now - BOOTING_FOR)
               .order_by("-launched_at").first())
    if booting:
        command = JOBS.get(booting.job, (booting.job,))[0]
        return {"command": command, "since": booting.launched_at}
    return None


def start(slug: str, username: str = "") -> tuple[bool, str]:
    """Launch a job. Returns (started, message for the user)."""
    if slug not in JOBS:
        return False, "Unknown job."

    command, extra, label = JOBS[slug]
    busy = running_job()
    if busy:
        started = timezone.localtime(busy["since"]).strftime("%H:%M:%S")
        return False, (
            f"“{busy['command']}” has been running since {started} and is not "
            f"finished yet. Wait for it to end, then try again."
        )

    from .models import PipelineLaunch

    # Recorded BEFORE spawning, so a second press sees it immediately.
    PipelineLaunch.objects.create(job=slug, launched_by=username or "")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"admin-{slug}.log"
    argv = [sys.executable, str(settings.BASE_DIR / "manage.py"), command, *extra]
    try:
        with open(logfile, "ab") as handle:
            handle.write(
                f"\n=== {timezone.now().isoformat()} admin-triggered {slug} ===\n".encode())
            subprocess.Popen(
                argv, cwd=str(settings.BASE_DIR),
                stdout=handle, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("could not start %s", slug)
        return False, f"Could not start the job: {exc}"

    log.info("admin started %s (%s)", slug, " ".join(argv))
    return True, (
        f"“{label}” has started. It usually takes a few minutes. "
        f"Refresh this page to follow it — the top row turns green when it finishes."
    )
