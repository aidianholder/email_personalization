"""Base class turning a registry entry into a Django management command.

Arguments come from ``adgvec.cli.COMMANDS`` so a flag is defined once and
appears in both ``manage.py <cmd>`` and ``python -m adgvec <cmd>``.
"""
from __future__ import annotations

import argparse

from django.core.management.base import BaseCommand

from adgvec.cli import COMMANDS, configure_logging, execute


class PipelineCommand(BaseCommand):
    #: key into adgvec.cli.COMMANDS
    adgvec_command: str = ""

    @property
    def spec(self):
        return COMMANDS[self.adgvec_command]

    @property
    def help(self) -> str:  # type: ignore[override]
        return self.spec.help

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--env-file", help="path to an alternate .env")
        self.spec.add_arguments(parser)
        if self.spec.defaults:
            parser.set_defaults(**self.spec.defaults)

    def handle(self, *args, **options) -> None:
        # Django owns -v/--verbosity; map it onto the pipeline's flag rather
        # than adding a conflicting one.
        options.setdefault("env_file", None)
        options["verbose"] = int(options.get("verbosity", 1)) > 1
        configure_logging(options["verbose"])
        code = execute(self.adgvec_command, argparse.Namespace(**options))
        if code:
            raise SystemExit(code)
