"""Pull stories into postgres (no embedding)."""
from ._base import PipelineCommand


class Command(PipelineCommand):
    adgvec_command = "fetch"
