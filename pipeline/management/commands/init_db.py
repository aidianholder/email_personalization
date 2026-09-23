"""Apply the hand-written SQL migrations in sql/ (idempotent).

Separate from Django's own migrations, which manage only the
pipeline_settings table. Everything else is owned by sql/.
"""
from ._base import PipelineCommand


class Command(PipelineCommand):
    adgvec_command = "init-db"
