"""Fetch then embed (the daily story job)."""
from ._base import PipelineCommand


class Command(PipelineCommand):
    adgvec_command = "run"
