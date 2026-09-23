"""Import pageview counts from treasure data."""
from ._base import PipelineCommand


class Command(PipelineCommand):
    adgvec_command = "sync-popularity"
