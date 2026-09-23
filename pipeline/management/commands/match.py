"""Build the daily edition and payloads."""
from ._base import PipelineCommand


class Command(PipelineCommand):
    adgvec_command = "match"
