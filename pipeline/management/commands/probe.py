"""Print raw api fields for a few stories."""
from ._base import PipelineCommand


class Command(PipelineCommand):
    adgvec_command = "probe"
