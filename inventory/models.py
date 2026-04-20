from django.db import models


# ═════════════════════════════════════════════════════════════
#  Ячейка (слот для зонта в стойке)
# ═════════════════════════════════════════════════════════════
class Cell(models.Model):
    cell_code = models.CharField(
        max_length=32,
        unique=True,
        verbose_name="Код ячейки",
        help_text="Например: A1, A2, B1...",
    )
    zone = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="Зона / локация",
        help_text="Например: Главный вход, Кафетерий",
    )

    class Meta:
        verbose_name = "Ячейка"
        verbose_name_plural = "Ячейки"
        ordering = ["cell_code"]

    def __str__(self):
        return self.cell_code


# ═════════════════════════════════════════════════════════════
#  Объект учёта (зонт)
# ═════════════════════════════════════════════════════════════
class TrackedObject(models.Model):
    irf_tag = models.CharField(
        max_length=128,
        unique=True,
        verbose_name="IRF-метка (UID зонта)",
    )
    name = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="Название / номер зонта",
    )
    cell = models.ForeignKey(
        Cell,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="current_objects",   # ← ИСПРАВЛЕНО (было "objects")
        verbose_name="Текущая ячейка",
        help_text="Где зонт сейчас. NULL — на руках у клиента.",
    )
    home_cell = models.ForeignKey(
        Cell,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="home_objects",
        verbose_name="Домашняя ячейка",
        help_text="Куда возвращать зонт по умолчанию.",
    )
    state = models.CharField(
        max_length=32,
        default="ok",
        verbose_name="Состояние",
        help_text="ok / broken / lost ...",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        null=True,
        blank=True,
        verbose_name="Дата добавления",
    )

    # ───── Поля для системы сушки ─────
    needs_drying = models.BooleanField(
        default=False,
        verbose_name="Требует сушки",
        help_text="Устанавливается при возврате зонта. Сушилка опрашивает это поле.",
    )
    is_drying = models.BooleanField(
        default=False,
        verbose_name="Сушится сейчас",
        help_text="True — зонт в процессе сушки.",
    )
    last_dried_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Последняя сушка",
    )
    last_humidity = models.FloatField(
        null=True,
        blank=True,
        verbose_name="Последняя влажность, %",
    )
    last_temp = models.FloatField(
        null=True,
        blank=True,
        verbose_name="Последняя температура, °C",
    )

    class Meta:
        verbose_name = "Зонт"
        verbose_name_plural = "Зонты"
        ordering = ["irf_tag"]

    def __str__(self):
        return f"{self.irf_tag} ({self.name or 'зонт'})"


# ═════════════════════════════════════════════════════════════
#  Пользовательская метка (RFID-карта клиента)
# ═════════════════════════════════════════════════════════════
class UserTag(models.Model):
    pass_tag = models.CharField(
        max_length=128,
        unique=True,
        verbose_name="UID карты",
        help_text="Например: '93 94 31 30'",
    )
    full_name = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="ФИО клиента",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        null=True,
        blank=True,
        verbose_name="Дата регистрации",
    )

    class Meta:
        verbose_name = "Клиент / карта"
        verbose_name_plural = "Клиенты / карты"
        ordering = ["pass_tag"]

    def __str__(self):
        return f"{self.full_name or '(без имени)'} [{self.pass_tag}]"


# ═════════════════════════════════════════════════════════════
#  Выдача (факт того, что зонт на руках у клиента)
# ═════════════════════════════════════════════════════════════
class Handout(models.Model):
    object = models.ForeignKey(
        TrackedObject,
        on_delete=models.CASCADE,
        related_name="handouts",
        verbose_name="Зонт",
    )
    user = models.ForeignKey(
        UserTag,
        on_delete=models.CASCADE,
        related_name="handouts",
        verbose_name="Клиент",
    )
    issued_at = models.DateTimeField(
        verbose_name="Выдан",
    )
    returned_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Возвращён",
    )

    class Meta:
        verbose_name = "Выдача"
        verbose_name_plural = "Выдачи"
        ordering = ["-issued_at"]

    def __str__(self):
        status = "активна" if self.returned_at is None else "закрыта"
        return f"{self.object.irf_tag} → {self.user.pass_tag} ({status})"