from __future__ import annotations
from django.contrib import messages
from django.db import transaction
from django.db.models import OuterRef, Exists
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
import json

from .models import Cell, Handout, TrackedObject, UserTag


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def _get_free_object() -> TrackedObject | None:
    """Любой объект, который лежит в ячейке и не на руках."""
    active_handouts = Handout.objects.filter(
        object=OuterRef("pk"), returned_at__isnull=True
    )
    return (
        TrackedObject.objects
        .filter(cell__isnull=False)
        .annotate(has_active=Exists(active_handouts))
        .filter(has_active=False)
        .order_by("irf_tag")
        .first()
    )


def _get_free_cell() -> Cell | None:
    """Любая свободная ячейка (без объектов)."""
    return (
        Cell.objects
        .filter(tracked_objects__isnull=True, status="active")
        .order_by("cell_code")
        .first()
    )


# ---------- API ДЛЯ ARDUINO ----------

@csrf_exempt
@require_POST
def api_rent(request: HttpRequest) -> JsonResponse:
    """
    Упрощённый API: достаточно пропуска пользователя.
    Ожидает JSON: {"user_uid": "..."}
    - Если у пользователя есть активная выдача → возвращаем объект в свободную ячейку
    - Иначе → выдаём любой свободный объект
    """
    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse({"action": "error", "message": "invalid json"}, status=400)

    user_uid = (data.get("user_uid") or "").strip()
    if not user_uid:
        return JsonResponse(
            {"action": "error", "message": "user_uid required"}, status=400
        )

    try:
        user = UserTag.objects.get(pass_tag=user_uid)
    except UserTag.DoesNotExist:
        return JsonResponse(
            {"action": "error", "message": f"user {user_uid} not found"}, status=404
        )

    with transaction.atomic():
        # Есть ли у пользователя активная выдача?
        active = (
            Handout.objects.select_for_update()
            .filter(user=user, returned_at__isnull=True)
            .select_related("object")
            .first()
        )

        if active:
            # ВОЗВРАТ
            free_cell = _get_free_cell()
            if not free_cell:
                return JsonResponse(
                    {"action": "error", "message": "no free cells"}, status=409
                )

            active.returned_at = timezone.now()
            active.save(update_fields=["returned_at"])

            obj = active.object
            obj.cell = free_cell
            obj.save(update_fields=["cell"])

            return JsonResponse({
                "action": "return",
                "message": "returned",
                "object_uid": obj.irf_tag,
                "cell_code": free_cell.cell_code,
            })
        else:
            # ВЫДАЧА
            obj = _get_free_object()
            if not obj:
                return JsonResponse(
                    {"action": "error", "message": "no free objects"}, status=409
                )

            obj.cell = None
            obj.save(update_fields=["cell"])
            Handout.objects.create(object=obj, user=user, issued_at=timezone.now())

            return JsonResponse({
                "action": "take",
                "message": "issued",
                "object_uid": obj.irf_tag,
            })


# ---------- ГЛАВНАЯ СТРАНИЦА ----------

def index(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        action = request.POST.get("action", "")
        pass_tag = (request.POST.get("pass_tag") or "").strip()

        if not pass_tag:
            messages.error(request, "Нужно указать метку/пропуск пользователя.")
            return redirect("inventory:index")

        try:
            user = UserTag.objects.get(pass_tag=pass_tag)
        except UserTag.DoesNotExist:
            messages.error(request, f"Пользователь с меткой/пропуском '{pass_tag}' не найден.")
            return redirect("inventory:index")

        # ---------- ВЗЯТИЕ ----------
        if action == "take":
            with transaction.atomic():
                # Не даём взять второй зонт, если уже есть активная выдача
                already = Handout.objects.filter(user=user, returned_at__isnull=True).exists()
                if already:
                    messages.error(request, "У вас уже есть зонт на руках. Сначала верните его.")
                    return redirect("inventory:index")

                obj = _get_free_object()
                if not obj:
                    messages.error(request, "Нет свободных зонтов.")
                    return redirect("inventory:index")

                obj.cell = None
                obj.save(update_fields=["cell"])
                Handout.objects.create(object=obj, user=user, issued_at=timezone.now())

            messages.success(request, f"Вам выдан зонт: {obj.name or obj.irf_tag}.")
            return redirect("inventory:index")

        # ---------- ВОЗВРАТ ----------
        if action == "return":
            with transaction.atomic():
                active = (
                    Handout.objects.select_for_update()
                    .filter(user=user, returned_at__isnull=True)
                    .select_related("object")
                    .order_by("-issued_at")
                    .first()
                )
                if not active:
                    messages.error(request, "У вас нет зонтов на руках.")
                    return redirect("inventory:index")

                free_cell = _get_free_cell()
                if not free_cell:
                    messages.error(request, "Нет свободных ячеек для возврата.")
                    return redirect("inventory:index")

                active.returned_at = timezone.now()
                active.save(update_fields=["returned_at"])

                obj = active.object
                obj.cell = free_cell
                obj.save(update_fields=["cell"])

            messages.success(
                request,
                f"Зонт {obj.name or obj.irf_tag} возвращён в ячейку {free_cell.cell_code}."
            )
            return redirect("inventory:index")

        messages.error(request, "Неизвестное действие.")
        return redirect("inventory:index")

    # GET
    objects = TrackedObject.objects.select_related("cell").order_by("irf_tag")
    active_handouts = (
        Handout.objects.select_related("object", "user")
        .filter(returned_at__isnull=True)
        .order_by("-issued_at")
    )
    cells = Cell.objects.order_by("cell_code")
    users = UserTag.objects.order_by("pass_tag")

    return render(
        request,
        "inventory/index.html",
        {
            "objects": objects,
            "active_handouts": active_handouts,
            "cells": cells,
            "users": users,
        },
    )