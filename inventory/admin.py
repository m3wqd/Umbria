from django.contrib import admin

from .models import Cell, Handout, TrackedObject, UserTag


@admin.register(UserTag)
class UserTagAdmin(admin.ModelAdmin):
    list_display = ("pass_tag", "full_name", "status", "created_at")
    search_fields = ("pass_tag", "full_name")
    list_filter = ("status",)
    ordering = ("pass_tag",)


@admin.register(Cell)
class CellAdmin(admin.ModelAdmin):
    list_display = ("cell_code", "zone", "status", "created_at")
    search_fields = ("cell_code", "zone")
    list_filter = ("status",)
    ordering = ("cell_code",)


@admin.register(TrackedObject)
class TrackedObjectAdmin(admin.ModelAdmin):
    list_display = ("irf_tag", "name", "cell", "home_cell", "state", "created_at")
    search_fields = ("irf_tag", "name")
    list_filter = ("state", "cell")
    autocomplete_fields = ("cell", "home_cell")
    ordering = ("irf_tag",)


@admin.register(Handout)
class HandoutAdmin(admin.ModelAdmin):
    list_display = ("object", "user", "issued_at", "returned_at")
    list_filter = ("returned_at",)
    search_fields = (
        "object__irf_tag",
        "object__name",
        "user__pass_tag",
        "user__full_name",
    )
    autocomplete_fields = ("object", "user")
    date_hierarchy = "issued_at"
    ordering = ("-issued_at",)