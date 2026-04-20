from __future__ import annotations

import json
import re

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from .models import Cell, Handout, TrackedObject, UserTag, DryerStatus, RentSession


# =====================================================================
#  ОДНИМ ЗАПРОСОМ (совместимость со старой схемой)
# =====================================================================

@csrf_exempt
@require_POST
def api_rent(request: HttpRequest) -> JsonResponse:
    """Старая ручка: всё одним запросом. Оставлена для совместимости."""
    expected_token = getattr(settings, "ARDUINO_TOKEN", None)
    if expected_token and request.headers.get("X-Device-Token") != expected_token:
        return JsonResponse({"action": "error", "message": "unauthorized"}, status=401)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"action": "error", "message": "invalid json"}, status=400)

    card_uid = (data.get("card") or data.get("uid") or "").strip()
    umbr_uid = (data.get("umbrella_uid") or "").strip()
    if "box_has_umbrella" in data:
        box_has = bool(data.get("box_has_umbrella"))
    else:
        box_has = True  # по умолчанию — зонт есть

    if not card_uid:
        return JsonResponse({"action": "error", "message": "card uid required"}, status=400)

    print(f"[api_rent] card={card_uid!r} box={box_has} umbrella={umbr_uid!r}")

    try:
        user = UserTag.objects.get(pass_tag=card_uid)
    except UserTag.DoesNotExist:
        return JsonResponse({"action": "error", "message": "card not registered"}, status=404)

    with transaction.atomic():
        active = (
            Handout.objects.select_for_update()
            .filter(user=user, returned_at__isnull=True)
            .select_related("object").first()
        )

        if active and box_has:
            obj = active.object
            active.returned_at = timezone.now()
            active.save(update_fields=["returned_at"])
            obj.needs_drying = True
            if obj.home_cell_id:
                obj.cell = obj.home_cell
            obj.save(update_fields=["cell", "needs_drying"])
            print(f"  ✅ ВОЗВРАТ {obj.irf_tag}")
            return JsonResponse({
                "action": "return", "umbrella": obj.irf_tag,
                "message": "возврат принят, зонт отправлен на сушку",
            })

        if active and not box_has:
            return JsonResponse({
                "action": "wait_return", "umbrella": active.object.irf_tag,
                "message": "положите зонт в бокс",
            })

        if not active and box_has:
            umbrella = None
            if umbr_uid:
                try:
                    umbrella = TrackedObject.objects.get(irf_tag=umbr_uid)
                except TrackedObject.DoesNotExist:
                    pass
            if not umbrella:
                open_h = Handout.objects.filter(object=OuterRef('pk'), returned_at__isnull=True)
                umbrella = (
                    TrackedObject.objects.filter(cell__isnull=False)
                    .annotate(has_open=Exists(open_h)).filter(has_open=False)
                    .order_by("irf_tag").first()
                )
            if not umbrella:
                return JsonResponse({"action": "error", "message": "нет свободных зонтов"}, status=409)

            if not umbrella.home_cell_id and umbrella.cell_id:
                umbrella.home_cell = umbrella.cell
            umbrella.cell = None
            umbrella.save(update_fields=["cell", "home_cell"])

            Handout.objects.create(object=umbrella, user=user, issued_at=timezone.now())
            print(f"  ✅ ВЫДАН {umbrella.irf_tag}")
            return JsonResponse({
                "action": "take", "umbrella": umbrella.irf_tag,
                "message": "зонт выдан",
            })

        return JsonResponse({"action": "empty", "message": "в боксе нет зонта"})


# =====================================================================
#  ШАГ 1: приложили КАРТУ — создаём сессию, ждём зонт
# =====================================================================

@csrf_exempt
@require_POST
def api_rent_card(request: HttpRequest) -> JsonResponse:
    expected_token = getattr(settings, "ARDUINO_TOKEN", None)
    if expected_token and request.headers.get("X-Device-Token") != expected_token:
        return JsonResponse({"action": "error", "message": "unauthorized"}, status=401)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"action": "error", "message": "invalid json"}, status=400)

    card_uid = (data.get("card") or data.get("uid") or "").strip()
    if not card_uid:
        return JsonResponse({"action": "error", "message": "card uid required"}, status=400)

    print(f"[api_rent_card] card={card_uid!r}")

    try:
        user = UserTag.objects.get(pass_tag=card_uid)
    except UserTag.DoesNotExist:
        return JsonResponse({"action": "error", "message": "card not registered"}, status=404)

    # чистим просроченные сессии (старше 30 сек)
    cutoff = timezone.now() - timezone.timedelta(seconds=30)
    RentSession.objects.filter(created_at__lt=cutoff).delete()

    # есть ли у клиента зонт на руках?
    has_umbrella = Handout.objects.filter(user=user, returned_at__isnull=True).exists()
    mode = "return" if has_umbrella else "take"

    # создаём свежую сессию (удаляем старые этого клиента)
    RentSession.objects.filter(user=user).delete()
    RentSession.objects.create(user=user, mode=mode)

    if mode == "return":
        active = (
            Handout.objects.filter(user=user, returned_at__isnull=True)
            .select_related("object").first()
        )
        umbrella_tag = active.object.irf_tag if active else ""
        print(f"  → ждём ВОЗВРАТ зонта {umbrella_tag}")
        return JsonResponse({
            "action":   "wait_umbrella",
            "mode":     "return",
            "message":  "приложите зонт для возврата",
            "umbrella": umbrella_tag,
        })
    else:
        print(f"  → ждём зонт для ВЫДАЧИ")
        return JsonResponse({
            "action":  "wait_umbrella",
            "mode":    "take",
            "message": "приложите зонт для выдачи",
        })


# =====================================================================
#  ШАГ 2: приложили ЗОНТ — ищем сессию, завершаем операцию
# =====================================================================

@csrf_exempt
@require_POST
def api_rent_umbrella(request: HttpRequest) -> JsonResponse:
    expected_token = getattr(settings, "ARDUINO_TOKEN", None)
    if expected_token and request.headers.get("X-Device-Token") != expected_token:
        return JsonResponse({"action": "error", "message": "unauthorized"}, status=401)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"action": "error", "message": "invalid json"}, status=400)

    umbrella_uid = (data.get("umbrella") or data.get("uid") or "").strip()
    if not umbrella_uid:
        return JsonResponse({"action": "error", "message": "umbrella uid required"}, status=400)

    print(f"[api_rent_umbrella] umbrella={umbrella_uid!r}")

    try:
        umbrella = TrackedObject.objects.get(irf_tag=umbrella_uid)
    except TrackedObject.DoesNotExist:
        return JsonResponse({"action": "error", "message": "umbrella not registered"}, status=404)

    cutoff = timezone.now() - timezone.timedelta(seconds=30)
    RentSession.objects.filter(created_at__lt=cutoff).delete()

    with transaction.atomic():
        session = RentSession.objects.select_for_update().order_by("-created_at").first()
        if not session:
            return JsonResponse({
                "action":  "error",
                "message": "сначала приложите карту",
            }, status=409)

        user = session.user
        mode = session.mode

        # ─── ВЫДАЧА ───
        if mode == "take":
            if Handout.objects.filter(object=umbrella, returned_at__isnull=True).exists():
                session.delete()
                return JsonResponse({
                    "action": "error", "message": "этот зонт уже на руках",
                }, status=409)

            if not umbrella.home_cell_id and umbrella.cell_id:
                umbrella.home_cell = umbrella.cell
            umbrella.cell = None
            umbrella.save(update_fields=["cell", "home_cell"])

            Handout.objects.create(object=umbrella, user=user, issued_at=timezone.now())
            session.delete()

            print(f"  ✅ ВЫДАН {umbrella.irf_tag} → {user.pass_tag}")
            return JsonResponse({
                "action":   "take",
                "umbrella": umbrella.irf_tag,
                "message":  "зонт выдан",
            })

        # ─── ВОЗВРАТ ───
        if mode == "return":
            active = (
                Handout.objects.select_for_update()
                .filter(user=user, returned_at__isnull=True).first()
            )
            if not active:
                session.delete()
                return JsonResponse({
                    "action": "error", "message": "у клиента нет активной выдачи",
                }, status=409)

            if active.object_id != umbrella.id:
                return JsonResponse({
                    "action": "error",
                    "message": f"ожидали зонт {active.object.irf_tag}, получен {umbrella.irf_tag}",
                }, status=409)

            active.returned_at = timezone.now()
            active.save(update_fields=["returned_at"])

            umbrella.needs_drying = True
            if umbrella.home_cell_id:
                umbrella.cell = umbrella.home_cell
            umbrella.save(update_fields=["cell", "needs_drying"])

            session.delete()

            print(f"  ✅ ВОЗВРАТ {umbrella.irf_tag}")
            return JsonResponse({
                "action":   "return",
                "umbrella": umbrella.irf_tag,
                "message":  "возврат принят, зонт отправлен на сушку",
            })

        session.delete()
        return JsonResponse({"action": "error", "message": "unknown mode"}, status=400)


# =====================================================================
#  Веб-интерфейс
# =====================================================================

def index(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        action   = request.POST.get("action", "")
        irf_tag  = (request.POST.get("irf_tag") or "").strip()
        pass_tag = (request.POST.get("pass_tag") or "").strip()

        if action == "take":
            if not irf_tag or not pass_tag:
                messages.error(request, "Укажите IRF-метку зонта и карту клиента.")
                return redirect("inventory:index")
            try:
                obj = TrackedObject.objects.get(irf_tag=irf_tag)
            except TrackedObject.DoesNotExist:
                messages.error(request, f"Зонт '{irf_tag}' не найден.")
                return redirect("inventory:index")
            try:
                user = UserTag.objects.get(pass_tag=pass_tag)
            except UserTag.DoesNotExist:
                messages.error(request, f"Карта '{pass_tag}' не найдена.")
                return redirect("inventory:index")

            with transaction.atomic():
                if Handout.objects.filter(object=obj, returned_at__isnull=True).exists():
                    messages.error(request, "Этот зонт уже на руках.")
                    return redirect("inventory:index")
                if not obj.home_cell_id and obj.cell_id:
                    obj.home_cell = obj.cell
                obj.cell = None
                obj.save(update_fields=["cell", "home_cell"])
                Handout.objects.create(object=obj, user=user, issued_at=timezone.now())

            messages.success(request, "Зонт выдан.")
            return redirect("inventory:index")

        if action == "return":
            if not irf_tag:
                messages.error(request, "Укажите IRF-метку зонта.")
                return redirect("inventory:index")
            cell_code = (request.POST.get("cell_code") or "").strip()
            try:
                obj = TrackedObject.objects.get(irf_tag=irf_tag)
            except TrackedObject.DoesNotExist:
                messages.error(request, f"Зонт '{irf_tag}' не найден.")
                return redirect("inventory:index")

            with transaction.atomic():
                active = (
                    Handout.objects.select_for_update()
                    .filter(object=obj, returned_at__isnull=True)
                    .order_by("-issued_at").first()
                )
                if not active:
                    messages.error(request, "Активной выдачи для этого зонта нет.")
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

            messages.success(request, "Зонт возвращён и отправлен на сушку.")
            return redirect("inventory:index")

        messages.error(request, "Неизвестное действие.")
        return redirect("inventory:index")

    # GET
    objects = TrackedObject.objects.select_related("cell", "home_cell").order_by("irf_tag")
    active_handouts = (
        Handout.objects.select_related("object", "user")
        .filter(returned_at__isnull=True).order_by("-issued_at")
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
#  API: активные выдачи
# =====================================================================

@require_GET
def api_active_handouts(request: HttpRequest) -> JsonResponse:
    handouts = (
        Handout.objects.select_related("object", "user")
        .filter(returned_at__isnull=True).order_by("-issued_at")
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


# =====================================================================
#  API: список всех зонтов
# =====================================================================

@require_GET
def api_objects(request: HttpRequest) -> JsonResponse:
    objects = TrackedObject.objects.select_related("cell", "home_cell").order_by("irf_tag")
    data = []
    for o in objects:
        if o.is_drying:
            status_code, status_label = "drying", "🌧 сушится"
        elif o.needs_drying:
            status_code, status_label = "queue", "⏳ в очереди"
        elif o.cell_id:
            status_code, status_label = "ok", "✓ на месте"
        else:
            status_code, status_label = "out", "на руках"

        data.append({
            "irf_tag":      o.irf_tag,
            "name":         o.name or "",
            "cell":         o.cell.cell_code if o.cell else "",
            "home_cell":    o.home_cell.cell_code if o.home_cell else "",
            "status_code":  status_code,
            "status_label": status_label,
            "humidity":     o.last_humidity,
            "temp":         o.last_temp,
        })
    return JsonResponse({"objects": data})


# =====================================================================
#  Сушилка — ловец запросов ESP
# =====================================================================

@csrf_exempt
def api_dryer_ping(request: HttpRequest, path: str = "") -> JsonResponse:
    status = DryerStatus.get()
    status.is_active = True

    raw = ""
    try:
        raw = request.body.decode("utf-8", errors="replace")[:500]
    except Exception:
        raw = ""

    humidity, temp, uid, event = None, None, "", ""

    parsed = False
    try:
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            parsed = True
            if "humidity" in data:
                try: humidity = float(data["humidity"])
                except (ValueError, TypeError): pass
            if "temp" in data:
                try: temp = float(data["temp"])
                except (ValueError, TypeError): pass
            uid   = (data.get("uid") or "").strip()
            event = (data.get("event") or "").strip()
    except Exception:
        parsed = False

    if not parsed:
        m_h = re.search(r"humidity[=:]\s*([\d.]+)", raw)
        m_t = re.search(r"temp[=:]\s*([\d.]+)", raw)
        m_u = re.search(r"uid[=:]\s*([A-Fa-f0-9 :]+)", raw)
        if m_h:
            try: humidity = float(m_h.group(1))
            except ValueError: pass
        if m_t:
            try: temp = float(m_t.group(1))
            except ValueError: pass
        if m_u: uid = m_u.group(1).strip()

    if humidity is not None: status.last_humidity = humidity
    if temp     is not None: status.last_temp     = temp

    if uid:
        try:
            obj = TrackedObject.objects.get(irf_tag=uid)
            obj.is_drying = True
            if humidity is not None: obj.last_humidity = humidity
            if temp     is not None: obj.last_temp     = temp
            if event == "finished":
                obj.is_drying     = False
                obj.needs_drying  = False
                obj.last_dried_at = timezone.now()
            obj.save(update_fields=[
                "is_drying", "needs_drying",
                "last_humidity", "last_temp", "last_dried_at",
            ])
        except TrackedObject.DoesNotExist:
            pass

    status.last_raw = f"uid={uid} H={humidity} T={temp} ev={event}"
    status.save()

    print(f"🌧 DRYER: {request.method} /{path} uid={uid!r} H={humidity} T={temp} ev={event!r}")
    return JsonResponse({"ok": True, "message": "caught", "umbrella": uid or None})


# =====================================================================
#  API: статус сушилки
# =====================================================================

@require_GET
def api_dryer_status(request: HttpRequest) -> JsonResponse:
    s = DryerStatus.get()
    idle_after_sec = 30
    is_active = s.is_active
    if s.last_update:
        delta = (timezone.now() - s.last_update).total_seconds()
        if delta > idle_after_sec:
            is_active = False
            if s.is_active:
                s.is_active = False
                s.save(update_fields=["is_active"])

    current = TrackedObject.objects.filter(is_drying=True).first()

    return JsonResponse({
        "active":   is_active,
        "humidity": s.last_humidity,
        "temp":     s.last_temp,
        "updated":  timezone.localtime(s.last_update).strftime("%H:%M:%S") if s.last_update else None,
        "umbrella": {
            "uid":  current.irf_tag if current else None,
            "name": (current.name if current else "") or "",
        } if current else None,
    })


# =====================================================================
#  API: ручное завершение сушки
# =====================================================================

@csrf_exempt
@require_POST
def api_dryer_done(request: HttpRequest) -> JsonResponse:
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        data = {}
    uid = (data.get("uid") or "").strip()
    if not uid:
        return JsonResponse({"ok": False, "message": "uid required"}, status=400)
    try:
        obj = TrackedObject.objects.get(irf_tag=uid)
    except TrackedObject.DoesNotExist:
        return JsonResponse({"ok": False, "message": "not found"}, status=404)

    obj.is_drying     = False
    obj.needs_drying  = False
    obj.last_dried_at = timezone.now()
    obj.save(update_fields=["is_drying", "needs_drying", "last_dried_at"])
    return JsonResponse({"ok": True})