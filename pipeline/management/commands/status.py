"""Show what is in the database."""
from ._base import PipelineCommand


class Command(PipelineCommand):
    adgvec_command = "status"
