"""Build reader interest profiles."""
from ._base import PipelineCommand


class Command(PipelineCommand):
    adgvec_command = "profiles"
