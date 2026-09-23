"""Leave-one-out evaluation of the ranker."""
from ._base import PipelineCommand


class Command(PipelineCommand):
    adgvec_command = "backtest"
