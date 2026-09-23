"""Embed stories that need it."""
from ._base import PipelineCommand


class Command(PipelineCommand):
    adgvec_command = "embed"
