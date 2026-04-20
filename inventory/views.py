from __future__ import annotations

import json

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from .models import Cell, Handout, TrackedObject, UserTag


# =====================================================================
#  API для Arduino выдачи (RFID карта клиента → выдать/вернуть зонт)
# =====================================================================

@csrf_exempt
@require_POST
def api_rent(request: HttpRequest) -> JsonResponse:
    expected_token = getattr(settings, "ARDUINO_TOKEN", None)
    if expected_token and request.headers.get("X-Device-Token") != expected_token:
        return JsonResponse({"action": "error", "message": "unauthorized"}, status=401)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"action": "error", "message": "invalid json"}, status=400)

    uid = (data.get("uid") or "").strip()
    if not uid:
        return JsonResponse({"action": "error", "message": "uid required"}, status=400)

    print(f"[api_rent] UID получен: {uid!r}")

    try:
        user = UserTag.objects.get(pass_tag=uid)
    except UserTag.DoesNotExist:
        print(f"[api_rent] Карта не зарегистрирована: {uid!r}")
        return JsonResponse({"action": "error", "message": "card not registered"}, status=404)

    print(f"[api_rent] Клиент: {user.full_name or '(без имени)'} [{user.pass_tag}]")

    with transaction.atomic():
        active = (
            Handout.objects.select_for_update()
            .filter(user=user, returned_at__isnull=True)
            .select_related("object", "object__home_cell")
            .first()
        )

        # ─────── ВОЗВРАТ ───────
        if active:
            obj = active.object
            print(f"[api_rent] ВОЗВРАТ зонта {obj.irf_tag}")

            active.returned_at = timezone.now()
            active.save(update_fields=["returned_at"])

            # ставим зонт в очередь на сушку
            obj.needs_drying = True
            if obj.home_cell_id:
                obj.cell = obj.home_cell
            obj.save(update_fields=["cell", "needs_drying"])

            return JsonResponse({
                "action": "return",
                "umbrella": obj.irf_tag,
                "message": "возврат принят, зонт отправлен на сушку",
                "needs_drying": True,
            })

        # ─────── ВЫДАЧА ───────
        open_handouts = Handout.objects.filter(
            object=OuterRef('pk'),
            returned_at__isnull=True,
        )

        umbrella = (
            TrackedObject.objects
            .filter(cell__isnull=False)
            .annotate(has_open_handout=Exists(open_handouts))
            .filter(has_open_handout=False)
            .order_by("irf_tag")
            .first()
        )

        if not umbrella:
            return JsonResponse(
                {"action": "error", "message": "нет свободных зонтов"},
                status=409,
            )

        if not umbrella.home_cell_id:
            umbrella.home_cell = umbrella.cell

        umbrella.cell = None
        umbrella.save(update_fields=["cell", "home_cell"])

        Handout.objects.create(
            object=umbrella,
            user=user,
            issued_at=timezone.now(),
        )

        print(f"[api_rent] ВЫДАН зонт {umbrella.irf_tag}")
        return JsonResponse({
            "action": "take",
            "umbrella": umbrella.irf_tag,
            "message": "зонт выдан",
        })


# =====================================================================
#  API для сушилки (ESP8266: DHT11 + RC522 + ТЭН + кулер)
# =====================================================================

@csrf_exempt
@require_POST
def api_dryer(request: HttpRequest) -> JsonResponse:
    expected_token = getattr(settings, "ARDUINO_TOKEN", None)
    if expected_token and request.headers.get("X-Device-Token") != expected_token:
        return JsonResponse({"ok": False, "message": "unauthorized"}, status=401)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "message": "invalid json"}, status=400)

    event    = (data.get("event") or "").strip()
    uid      = (data.get("uid") or "").strip()
    temp     = data.get("temp")
    humidity = data.get("humidity")

    # ─────── CHECK — есть ли зонт на сушку? ───────
    if event == "check":
        obj = (
            TrackedObject.objects
            .filter(needs_drying=True, is_drying=False)
            .order_by("irf_tag")
            .first()
        )
        if obj:
            print(f"[api_dryer] → задание на сушку: {obj.irf_tag}")
            return JsonResponse({
                "ok": True,
                "action": "dry",
                "umbrella": obj.irf_tag,
                "name": obj.name or "",
            })
        return JsonResponse({"ok": True, "action": "idle"})

    # ─────── START ───────
    if event == "start":
        if not uid:
            return JsonResponse({"ok": False, "message": "uid required"}, status=400)
        try:
            obj = TrackedObject.objects.get(irf_tag=uid)
        except TrackedObject.DoesNotExist:
            return JsonResponse({"ok": False, "message": "umbrella not found"}, status=404)

        obj.is_drying = True
        if humidity is not None: obj.last_humidity = float(humidity)
        if temp is not None:     obj.last_temp = float(temp)
        obj.save(update_fields=["is_drying", "last_humidity", "last_temp"])
        print(f"[api_dryer] СТАРТ сушки: {uid} (H={humidity}% T={temp}°C)")
        return JsonResponse({"ok": True, "message": "drying started"})

    # ─────── FINISHED ───────
    if event == "finished":
        if not uid:
            return JsonResponse({"ok": False, "message": "uid required"}, status=400)
        try:
            obj = TrackedObject.objects.get(irf_tag=uid)
        except TrackedObject.DoesNotExist:
            return JsonResponse({"ok": False, "message": "umbrella not found"}, status=404)

        obj.needs_drying  = False
        obj.is_drying     = False
        obj.last_dried_at = timezone.now()
        if humidity is not None: obj.last_humidity = float(humidity)
        if temp is not None:     obj.last_temp = float(temp)
        obj.save(update_fields=["needs_drying", "is_drying", "last_dried_at", "last_humidity", "last_temp"])
        print(f"[api_dryer] ВЫСОХ: {uid}")
        return JsonResponse({"ok": True, "message": "ok"})

    # ─────── FAILED ───────
    if event == "failed":
        if uid:
            try:
                obj = TrackedObject.objects.get(irf_tag=uid)
                obj.is_drying = False
                obj.save(update_fields=["is_drying"])
            except TrackedObject.DoesNotExist:
                pass
        print(f"[api_dryer] ❌ СБОЙ: {uid}")
        return JsonResponse({"ok": True})

    # ─────── STATUS ───────
    if event == "status":
        state  = data.get("state", "?")
        heater = data.get("heater", "?")
        fan    = data.get("fan", "?")
        print(f"[api_dryer] status: state={state} T={temp}°C H={humidity}% heater={heater} fan={fan}")
        return JsonResponse({"ok": True})

    return JsonResponse({"ok": False, "message": f"unknown event: {event}"}, status=400)


# =====================================================================
#  Веб-интерфейс (главная страница)
# =====================================================================

def index(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        action   = request.POST.get("action", "")
        irf_tag  = (request.POST.get("irf_tag") or "").strip()
        pass_tag = (request.POST.get("pass_tag") or "").strip()

        if action == "take":
            if not irf_tag or not pass_tag:
                messages.error(request, "Укажите IRF-метку объекта и метку пользователя.")
                return redirect("inventory:index")

            try:
                obj = TrackedObject.objects.get(irf_tag=irf_tag)
            except TrackedObject.DoesNotExist:
                messages.error(request, f"Объект '{irf_tag}' не найден.")
                return redirect("inventory:index")

            try:
                user = UserTag.objects.get(pass_tag=pass_tag)
            except UserTag.DoesNotExist:
                messages.error(request, f"Пользователь '{pass_tag}' не найден.")
                return redirect("inventory:index")

            with transaction.atomic():
                if Handout.objects.filter(object=obj, returned_at__isnull=True).exists():
                    messages.error(request, "Этот объект уже на руках.")
                    return redirect("inventory:index")

                if not obj.home_cell_id and obj.cell_id:
                    obj.home_cell = obj.cell
                obj.cell = None
                obj.save(update_fields=["cell", "home_cell"])

                Handout.objects.create(object=obj, user=user, issued_at=timezone.now())

            messages.success(request, "Объект выдан.")
            return redirect("inventory:index")

        if action == "return":
            if not irf_tag:
                messages.error(request, "Укажите IRF-метку объекта.")
                return redirect("inventory:index")

            cell_code = (request.POST.get("cell_code") or "").strip()
            try:
                obj = TrackedObject.objects.get(irf_tag=irf_tag)
            except TrackedObject.DoesNotExist:
                messages.error(request, f"Объект '{irf_tag}' не найден.")
                return redirect("inventory:index")

            with transaction.atomic():
                active = (
                    Handout.objects.select_for_update()
                    .filter(object=obj, returned_at__isnull=True)
                    .order_by("-issued_at")
                    .first()
                )
                if not active:
                    messages.error(request, "Активной выдачи для этого объекта нет.")
                    return redirect("inventory:index")

                active.returned_at = timezone.now()
                active.save(update_fields=["returned_at"])

                if cell_code:
                    try:
                        cell = Cell.objects.get(cell_code=cell_code)
                    except Cell.DoesNotExist:
                        messages.error(request, f"Ячейка '{cell_code}' не найдена.")
                        return redirect("inventory:index")
                    obj.cell = cell
                elif obj.home_cell_id:
                    obj.cell = obj.home_cell

                obj.needs_drying = True
                obj.save(update_fields=["cell", "needs_drying"])

            messages.success(request, "Объект возвращён и отправлен на сушку.")
            return redirect("inventory:index")

        messages.error(request, "Неизвестное действие.")
        return redirect("inventory:index")

    # GET-запрос — показываем страницу
    objects = TrackedObject.objects.select_related("cell", "home_cell").order_by("irf_tag")
    active_handouts = (
        Handout.objects
        .select_related("object", "user")
        .filter(returned_at__isnull=True)
        .order_by("-issued_at")
    )
    cells = Cell.objects.order_by("cell_code")
    users = UserTag.objects.order_by("pass_tag")

    return render(request, "inventory/index.html", {
        "objects": objects,
        "active_handouts": active_handouts,
        "cells": cells,
        "users": users,
    })


# =====================================================================
#  API: активные выдачи (для автообновления на сайте)
# =====================================================================

@require_GET
def api_active_handouts(request: HttpRequest) -> JsonResponse:
    handouts = (
        Handout.objects
        .select_related("object", "user")
        .filter(returned_at__isnull=True)
        .order_by("-issued_at")
    )

    data = [
        {
            "object_name": h.object.name or "Объект",
            "object_tag":  h.object.irf_tag,
            "user_name":   h.user.full_name or "Без имени",
            "user_tag":    h.user.pass_tag,
            "issued_at":   timezone.localtime(h.issued_at).strftime("%d.%m.%Y %H:%M:%S"),
        }
        for h in handouts
    ]

    return JsonResponse({"handouts": data})

@csrf_exempt
@require_POST
def api_humidity(request):
    """Просто принимает данные от датчика влажности."""
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "message": "bad json"}, status=400)

    humidity = data.get("humidity")
    temp     = data.get("temp")
    uid      = data.get("uid", "")

    print(f"📊 Датчик: H={humidity}% T={temp}°C uid={uid}")

    # Если нужно — сохраняем в БД:
    if uid:
        try:
            obj = TrackedObject.objects.get(irf_tag=uid)
            if humidity is not None:
                obj.last_humidity = float(humidity)
            if temp is not None:
                obj.last_temp = float(temp)
            obj.save(update_fields=["last_humidity", "last_temp"])
        except TrackedObject.DoesNotExist:
            pass

    return JsonResponse({"ok": True})