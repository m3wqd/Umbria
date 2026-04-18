from django.contrib import admin
from django.utils import timezone

from .models import Cell, Handout, TrackedObject, UserTag


@admin.register(UserTag)
class UserTagAdmin(admin.ModelAdmin):
    list_display = ("pass_tag", "full_name", "status", "created_at")
    search_fields = ("pass_tag", "full_name")
    list_filter = ("status",)


@admin.register(Cell)
class CellAdmin(admin.ModelAdmin):
    list_display = ("cell_code", "zone", "status", "created_at")
    search_fields = ("cell_code", "zone")
    list_filter = ("status", "zone")


@admin.register(TrackedObject)
class TrackedObjectAdmin(admin.ModelAdmin):
    list_display = ("irf_tag", "name", "cell", "state", "created_at")
    search_fields = ("irf_tag", "name")
    list_filter = ("state", "cell")
    autocomplete_fields = ("cell",)


@admin.register(Handout)
class HandoutAdmin(admin.ModelAdmin):
    list_display = ("object", "user", "issued_at", "returned_at", "is_active")
    search_fields = ("object__irf_tag", "object__name", "user__pass_tag", "user__full_name")
    list_filter = ("returned_at",)
    autocomplete_fields = ("object", "user")
    actions = ("mark_returned_now",)

    @admin.display(boolean=True, description="Активна")
    def is_active(self, obj: Handout) -> bool:
        return obj.returned_at is None

    @admin.action(description="Отметить как возвращённые (сейчас)")
    def mark_returned_now(self, request, queryset):
        queryset.filter(returned_at__isnull=True).update(returned_at=timezone.now())
    
    @admin.register(TrackedObject)
    class TrackedObjectAdmin(admin.ModelAdmin):
        list_display = ("irf_tag", "name", "cell", "home_cell", "state", "created_at")
        search_fields = ("irf_tag", "name")
        list_filter = ("state", "cell")
        autocomplete_fields = ("cell", "home_cell")

