"""Recompute the corpus mean vector now."""
from ._base import PipelineCommand


class Command(PipelineCommand):
    adgvec_command = "corpus-mean"
