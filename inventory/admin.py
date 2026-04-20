from django.contrib import admin
from .models import Cell, TrackedObject, UserTag, Handout


@admin.register(Cell)
class CellAdmin(admin.ModelAdmin):
    list_display = ("cell_code", "zone")
    search_fields = ("cell_code", "zone")


@admin.register(TrackedObject)
class TrackedObjectAdmin(admin.ModelAdmin):
    list_display = ("irf_tag", "name", "cell", "home_cell", "needs_drying", "is_drying")
    list_filter = ("needs_drying", "is_drying")
    search_fields = ("irf_tag", "name")        # ← ОБЯЗАТЕЛЬНО для autocomplete
    autocomplete_fields = ("cell", "home_cell")


@admin.register(UserTag)
class UserTagAdmin(admin.ModelAdmin):
    list_display = ("pass_tag", "full_name")
    search_fields = ("pass_tag", "full_name")  # ← ОБЯЗАТЕЛЬНО для autocomplete


@admin.register(Handout)
class HandoutAdmin(admin.ModelAdmin):
    list_display = ("object", "user", "issued_at", "returned_at")
    list_filter = ("returned_at",)
    autocomplete_fields = ("object", "user")   # ← для этого нужны search_fields у TrackedObject и UserTag
    search_fields = ("object__irf_tag", "user__pass_tag")