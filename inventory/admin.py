from django.contrib import admin
from .models import Cell, TrackedObject, UserTag, Handout, DryerStatus


@admin.register(Cell)
class CellAdmin(admin.ModelAdmin):
    list_display  = ("cell_code", "zone")
    search_fields = ("cell_code", "zone")
    ordering      = ("cell_code",)


@admin.register(TrackedObject)
class TrackedObjectAdmin(admin.ModelAdmin):
    list_display = (
        "irf_tag", "name",
        "cell", "home_cell",
        "needs_drying", "is_drying",
        "last_humidity", "last_temp", "last_dried_at",
    )
    list_filter   = ("needs_drying", "is_drying", "cell")
    search_fields = ("irf_tag", "name")
    autocomplete_fields = ("cell", "home_cell")
    readonly_fields = ("last_dried_at", "created_at")
    ordering = ("irf_tag",)


@admin.register(UserTag)
class UserTagAdmin(admin.ModelAdmin):
    list_display  = ("pass_tag", "full_name", "created_at")
    search_fields = ("pass_tag", "full_name")
    ordering      = ("pass_tag",)


@admin.register(Handout)
class HandoutAdmin(admin.ModelAdmin):
    list_display  = ("object", "user", "issued_at", "returned_at", "is_active")
    list_filter   = ("returned_at", "issued_at")
    search_fields = ("object__irf_tag", "user__pass_tag", "user__full_name")
    autocomplete_fields = ("object", "user")
    date_hierarchy = "issued_at"

    @admin.display(boolean=True, description="Активна")
    def is_active(self, obj):
        return obj.returned_at is None


@admin.register(DryerStatus)
class DryerStatusAdmin(admin.ModelAdmin):
    list_display = ("is_active", "last_humidity", "last_temp", "last_update")
    readonly_fields = ("last_update", "last_raw")