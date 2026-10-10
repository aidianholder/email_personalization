"""Django models for the pipeline.

Only ``PipelineSettings`` is managed by Django -- it is a new table whose
whole purpose is to be edited in the admin. Everything else is
``managed = False``: those tables are created and evolved by the hand-written
migrations in ``sql/``, and these models exist so the admin can *display*
them. Running two migration systems over one schema is a mess worth avoiding.

Vector columns are deliberately left off the models. The admin has no use for
a 1536-dimension array, and omitting them means this app needs no pgvector
integration at all.
"""
from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class PipelineSettings(models.Model):
    """Tunable knobs, editable in the admin. Secrets stay in the environment.

    Precedence is: explicit command-line flag > this table > .env default.
    """

    singleton = models.PositiveSmallIntegerField(primary_key=True, default=1)

    # --- ranking ---------------------------------------------------------- #
    hybrid_enabled = models.BooleanField(
        default=True,
        help_text="Blend the personal match score with pageview popularity. "
                  "Off means ranking on the personal score alone.")
    hybrid_weight = models.FloatField(
        default=0.6,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="Weight on the personal score; the rest goes to popularity. "
                  "1.0 = personal only, 0.0 = popularity only. Swept against "
                  "the temporal backtest: recall@2 peaks at 0.6 (34.2%) vs "
                  "26.6% popularity-only and 24.8% personal-only. LOWERING "
                  "this makes the email less distinctive: at 0.6 about 31% of "
                  "readers share the top personalized story and 34 of 37 "
                  "candidates get used; at 0.4 that is 42% and only 14 of 37. "
                  "If the email feels generic, RAISE this toward 1.0.")
    alpha = models.FloatField(
        default=0.4,
        help_text="Exponent on cluster strength in strength^alpha * cosine. "
                  "No effect while profiles are single-centroid.")
    max_pair_sim = models.FloatField(
        default=0.70,
        help_text="Similarity ceiling between the two personalized picks when "
                  "they come from the same cluster. Stops two versions of one "
                  "story without leaving the slot empty.")

    # --- layout ----------------------------------------------------------- #
    layout = models.CharField(
        max_length=16, default="EEEPP",
        help_text="Slot plan. E = editor pick, P = personalized. Length sets "
                  "the number of slots. Slots are filled by waterfall, so "
                  "every reader gets a full email regardless.")
    editor_section_front = models.IntegerField(
        default=186,
        help_text="Ellington section front supplying the editor picks: its "
                  "lead_story then additional_stories, in the desk's order. "
                  "0 disables editor picks entirely.")
    require_thumbnail = models.BooleanField(
        default=True,
        help_text="Exclude stories with no image from the candidate pool. "
                  "About 21% of stories have none.")

    # --- profiles --------------------------------------------------------- #
    profile_cluster = models.BooleanField(
        default=False,
        help_text="Build multi-centroid profiles. Measured WORSE than a plain "
                  "mean (20.4% vs 23.9% recall@2) and the gap widens with "
                  "more history. On only to re-test.")
    profile_recency = models.BooleanField(
        default=False,
        help_text="Apply recency decay to reads. Measured monotonically "
                  "harmful: every half-life from 0.25 to 30 days scored below "
                  "no decay at all.")
    profile_rarity = models.BooleanField(
        default=False,
        help_text="Down-weight widely-read stories when building a profile. "
                  "Costs about 1.4 points of recall@2 on its own.")

    # --- ingest ----------------------------------------------------------- #
    corpus_mean_window_days = models.IntegerField(
        default=30,
        help_text="Trailing window for the corpus mean vector, which is "
                  "subtracted from every embedding before matching.")
    popularity_hosts = models.CharField(
        max_length=500, default="www.arkansasonline.com",
        help_text="Comma-separated td_host values counted for popularity. "
                  "raw_web.pageviews covers every WEHCO title and nwaonline "
                  "usually outweighs arkansasonline, so leaving this open "
                  "makes ~70% of the signal other papers' readers.")
    popularity_window_hours = models.IntegerField(
        default=24, help_text="How far back to count pageviews.")

    # --- provenance ------------------------------------------------------- #
    change_note = models.TextField(
        blank=True,
        help_text="Why this change was made. Editions record the settings "
                  "they ran under, so a note here makes a later shift in "
                  "click-through traceable to a decision.")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=150, blank=True)

    class Meta:
        db_table = "pipeline_settings"
        verbose_name = "pipeline settings"
        verbose_name_plural = "pipeline settings"
        permissions = [
            # Separate from editing settings, so an editor can be allowed to
            # rebuild the email without also being able to retune the ranking.
            ("trigger_run", "Can trigger a pipeline run"),
        ]

    def __str__(self) -> str:
        return f"settings (hybrid weight {self.hybrid_weight}, layout {self.layout})"

    def save(self, *args, **kwargs):
        self.singleton = 1          # one row, always
        return super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "PipelineSettings":
        obj, _ = cls.objects.get_or_create(singleton=1)
        return obj


class PipelineLaunch(models.Model):
    """Who pressed which button, and when.

    Exists for two reasons. It is an audit trail -- the run log records that
    a job ran, not that a person asked for it. And it closes a race: a
    subprocess takes a couple of seconds to boot Django and write its own
    run row, so checking only PipelineRun let a second button press through
    and show a misleading "started" message. The advisory lock stopped the
    duplicate run either way; this stops the wrong message.
    """

    job = models.CharField(max_length=40)
    launched_at = models.DateTimeField(auto_now_add=True)
    launched_by = models.CharField(max_length=150, blank=True)

    class Meta:
        db_table = "pipeline_launch"
        ordering = ["-launched_at"]
        verbose_name = "run request"
        verbose_name_plural = "run requests"

    def __str__(self) -> str:
        return f"{self.job} by {self.launched_by or 'system'}"


class PipelineRun(models.Model):
    """Read-only view of sql/002's run log."""

    command = models.TextField()
    status = models.TextField()
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    window_start = models.DateTimeField(null=True)
    window_end = models.DateTimeField(null=True)
    fetched = models.IntegerField()
    inserted = models.IntegerField()
    updated = models.IntegerField()
    skipped = models.IntegerField()
    embedded = models.IntegerField()
    error = models.TextField(null=True)
    host = models.TextField(null=True)
    pid = models.IntegerField(null=True)

    class Meta:
        managed = False
        db_table = "pipeline_runs"
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"#{self.pk} {self.command} ({self.status})"


class EmailEdition(models.Model):
    """Read-only view of sql/004's editions, with the settings each ran under."""

    edition_date = models.DateField()
    status = models.TextField()
    layout = models.TextField()
    slots = models.SmallIntegerField()
    corpus_mean_id = models.BigIntegerField(null=True)
    embed_model = models.TextField(null=True)
    hybrid_weight = models.FloatField(null=True)
    editor_section_front = models.IntegerField(null=True)
    profile_cluster = models.BooleanField(null=True)
    profile_recency = models.BooleanField(null=True)
    profile_rarity = models.BooleanField(null=True)
    created_at = models.DateTimeField()
    published_at = models.DateTimeField(null=True)
    notes = models.TextField(null=True)

    class Meta:
        managed = False
        db_table = "email_edition"
        ordering = ["-edition_date", "-id"]

    def __str__(self) -> str:
        return f"{self.edition_date} #{self.pk} ({self.status})"


class Story(models.Model):
    """Read-only view of the story corpus. The embedding column is omitted."""

    story_id = models.BigIntegerField(db_column="storyId", primary_key=True)
    headline = models.TextField(null=True)
    published = models.DateTimeField(null=True)
    tease = models.TextField(null=True)
    kicker = models.TextField(null=True)
    share_url = models.TextField(null=True)
    thumbnail_url = models.TextField(null=True)
    subscription_required = models.BooleanField(null=True)
    embed_model = models.TextField(null=True)
    embedded_at = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = "stories"
        ordering = ["-published"]
        verbose_name_plural = "stories"

    def __str__(self) -> str:
        return f"{self.story_id} {self.headline or ''}"[:80]
