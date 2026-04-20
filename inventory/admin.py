from django.contrib import admin
from .models import Cell, TrackedObject, UserTag, Handout


# ═════════════════════════════════════════════════════════════
#  Ячейки
# ═════════════════════════════════════════════════════════════
@admin.register(Cell)
class CellAdmin(admin.ModelAdmin):
    list_display  = ("cell_code", "zone")
    search_fields = ("cell_code", "zone")
    ordering      = ("cell_code",)


# ═════════════════════════════════════════════════════════════
#  Зонты (TrackedObject)
# ═════════════════════════════════════════════════════════════
@admin.register(TrackedObject)
class TrackedObjectAdmin(admin.ModelAdmin):
    list_display = (
        "irf_tag",
        "name",
        "cell",
        "home_cell",
        "needs_drying",
        "is_drying",
        "last_humidity",
        "last_temp",
        "last_dried_at",
    )
    list_filter = (
        "needs_drying",
        "is_drying",
        "cell",
    )
    search_fields       = ("irf_tag", "name")
    autocomplete_fields = ("cell", "home_cell")
    readonly_fields     = ("last_dried_at", "last_humidity", "last_temp")
    ordering            = ("irf_tag",)

    fieldsets = (
        ("Основное", {
            "fields": ("irf_tag", "name", "cell", "home_cell"),
        }),
        ("Сушка", {
            "fields": (
                "needs_drying",
                "is_drying",
                "last_dried_at",
                "last_humidity",
                "last_temp",
            ),
        }),
    )


# ═════════════════════════════════════════════════════════════
#  Клиенты (RFID-карты)
# ═════════════════════════════════════════════════════════════
@admin.register(UserTag)
class UserTagAdmin(admin.ModelAdmin):
    list_display  = ("pass_tag", "full_name")
    search_fields = ("pass_tag", "full_name")
    ordering      = ("pass_tag",)


# ═════════════════════════════════════════════════════════════
#  Выдачи (Handout)
# ═════════════════════════════════════════════════════════════
@admin.register(Handout)
class HandoutAdmin(admin.ModelAdmin):
    list_display = (
        "object",
        "user",
        "issued_at",
        "returned_at",
        "is_active",
    )
    list_filter         = ("returned_at", "issued_at")
    autocomplete_fields = ("object", "user")
    search_fields       = ("object__irf_tag", "user__pass_tag", "user__full_name")
    date_hierarchy      = "issued_at"
    ordering            = ("-issued_at",)
    readonly_fields     = ("issued_at",)

    @admin.display(boolean=True, description="Активна")
    def is_active(self, obj):
        return obj.returned_at is None