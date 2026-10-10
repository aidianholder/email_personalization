"""Run the whole nightly chain in order.

This is what cron runs, and what the admin's "Full nightly run" button
launches. The stages must happen in this order: stories have to exist and be
processed before profiles can reference them, and profiles have to exist
before the lineups can be built.

Each stage takes the advisory lock in turn, so a second nightly run started
while this one is going will stop at its first stage rather than interleave.
"""
from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand

# (command, kwargs, human label)
STAGES = (
    ("run", {"hours": 36}, "collect and process the day's stories"),
    ("sync_readers", {"timestamped": True}, "pull reading history"),
    ("sync_popularity", {}, "pull today's traffic numbers"),
    ("profiles", {}, "rebuild reader interests"),
    ("match", {}, "build the email lineups"),
)


class Command(BaseCommand):
    help = "Run every nightly stage in order (stories, readers, popularity, profiles, match)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip", action="append", default=[],
            help="stage to skip; repeatable (e.g. --skip run)")
        parser.add_argument(
            "--stop-on-error", action="store_true",
            help="halt at the first failing stage instead of continuing")

    def handle(self, *args, **options):
        failures = []
        for name, kwargs, label in STAGES:
            if name in options["skip"]:
                self.stdout.write(f"--- skipping {name} ({label})")
                continue
            self.stdout.write(self.style.MIGRATE_HEADING(f"--- {name}: {label}"))
            try:
                call_command(name, **kwargs)
            except SystemExit as exc:          # a stage returned non-zero
                if exc.code:
                    failures.append(name)
                    self.stderr.write(self.style.ERROR(f"--- {name} failed"))
                    if options["stop_on_error"]:
                        break
            except Exception as exc:           # noqa: BLE001
                failures.append(name)
                self.stderr.write(self.style.ERROR(f"--- {name} failed: {exc}"))
                if options["stop_on_error"]:
                    break

        if failures:
            self.stderr.write(self.style.ERROR(
                f"finished with {len(failures)} failed stage(s): {', '.join(failures)}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("nightly run complete"))
