from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class UserTag(models.Model):
    full_name = models.CharField(max_length=200, blank=True, default="")
    pass_tag = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=32, blank=True, default="active")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Пользователь (метка/пропуск)"
        verbose_name_plural = "Пользователи (метки/пропуска)"

    def __str__(self) -> str:
        return f"{self.full_name or 'Без имени'} [{self.pass_tag}]"


class Cell(models.Model):
    cell_code = models.CharField(max_length=64, unique=True)
    zone = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=32, blank=True, default="active")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ячейка"
        verbose_name_plural = "Ячейки"

    def __str__(self) -> str:
        return self.cell_code


class TrackedObject(models.Model):
    irf_tag = models.CharField(max_length=128, unique=True)
    name = models.CharField(max_length=200, blank=True, default="")
    cell = models.ForeignKey(
        Cell,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tracked_objects",
    )
    state = models.CharField(max_length=32, blank=True, default="ok")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Объект"
        verbose_name_plural = "Объекты"

    def __str__(self) -> str:
        return f"{self.name or 'Объект'} [{self.irf_tag}]"

    @property
    def is_on_hands(self) -> bool:
        return self.cell_id is None


class Handout(models.Model):
    object = models.ForeignKey(
        TrackedObject, on_delete=models.PROTECT, related_name="handouts"
    )
    user = models.ForeignKey(UserTag, on_delete=models.PROTECT, related_name="handouts")
    issued_at = models.DateTimeField(default=timezone.now)
    returned_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "Выдача на руки"
        verbose_name_plural = "Выдачи на руки"
        constraints = [
            models.UniqueConstraint(
                fields=["object"],
                condition=Q(returned_at__isnull=True),
                name="uniq_active_handout_per_object",
            )
        ]

    def __str__(self) -> str:
        status = "активна" if self.returned_at is None else "закрыта"
        return f"{self.object.irf_tag} → {self.user.pass_tag} ({status})"

    def clean(self) -> None:
        super().clean()
        if self.returned_at is None and self.object.cell_id is not None:
            raise ValidationError(
                {"object": "Нельзя выдать объект на руки, пока он находится в ячейке."}
            )

