"""Admin: tune the pipeline, inspect what it did.

Read-only for everything the pipeline owns. Only PipelineSettings is
editable, and it is a singleton -- there is nothing useful about a second row.
"""
from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from .models import EmailEdition, PipelineRun, PipelineSettings, Story


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PipelineSettings)
class PipelineSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Ranking", {
            "fields": ("hybrid_enabled", "hybrid_weight", "alpha", "max_pair_sim"),
            "description": (
                "<strong>hybrid_weight trades accuracy against how personal "
                "the email feels.</strong> Measured: 1.0 uses all 37 daily "
                "candidates and 14.7% of readers share the top personalized "
                "story; 0.6 uses 34 and 31.1% share it; 0.4 uses only 14 and "
                "42.3% share it. Recall@2 peaks at 0.6, but if readers say the "
                "email feels generic, move this <em>up</em>."
            ),
        }),
        ("Layout", {"fields": ("layout", "editor_section_front",
                               "require_thumbnail")}),
        ("Profiles", {
            "fields": ("profile_cluster", "profile_recency", "profile_rarity"),
            "description": (
                "All three measured worse than a plain unweighted mean on a "
                "temporally honest backtest. They are here to re-test, not "
                "because they are expected to help."
            ),
        }),
        ("Ingest", {"fields": ("corpus_mean_window_days", "popularity_hosts",
                               "popularity_window_hours")}),
        ("Provenance", {"fields": ("change_note", "updated_by", "updated_at")}),
    )
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        # Singleton: the row is created on first load.
        return not PipelineSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user.get_username()
        super().save_model(request, obj, form, change)

    def changelist_view(self, request, extra_context=None):
        PipelineSettings.load()          # ensure the row exists
        return super().changelist_view(request, extra_context)


@admin.register(EmailEdition)
class EmailEditionAdmin(ReadOnlyAdmin):
    list_display = ("id", "edition_date", "status", "layout", "hybrid_weight",
                    "editor_section_front", "published_at")
    list_filter = ("status", "edition_date")
    date_hierarchy = "edition_date"
    search_fields = ("notes",)


@admin.register(PipelineRun)
class PipelineRunAdmin(ReadOnlyAdmin):
    list_display = ("id", "command", "status_badge", "started_at", "duration",
                    "fetched", "inserted", "updated", "embedded", "short_error")
    list_filter = ("command", "status")
    date_hierarchy = "started_at"

    @admin.display(description="status", ordering="status")
    def status_badge(self, obj):
        colour = {"ok": "green", "error": "crimson",
                  "running": "darkorange"}.get(obj.status, "grey")
        return format_html('<b style="color:{}">{}</b>', colour, obj.status)

    @admin.display(description="took")
    def duration(self, obj):
        if not obj.finished_at:
            return "-"
        seconds = (obj.finished_at - obj.started_at).total_seconds()
        return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.1f}m"

    @admin.display(description="error")
    def short_error(self, obj):
        return (obj.error or "")[:80]


@admin.register(Story)
class StoryAdmin(ReadOnlyAdmin):
    list_display = ("story_id", "headline", "published", "has_image",
                    "embedded_at")
    list_filter = ("embed_model", "subscription_required")
    search_fields = ("headline", "tease")
    date_hierarchy = "published"

    @admin.display(boolean=True, description="image")
    def has_image(self, obj):
        return bool(obj.thumbnail_url)
