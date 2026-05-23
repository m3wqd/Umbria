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
@csrf_exempt
def api_rent(request: HttpRequest) -> JsonResponse:
    """Аренда/возврат + команда открыть дверь."""
    expected_token = getattr(settings, "ARDUINO_TOKEN", None)
    if expected_token and request.headers.get("X-Device-Token") != expected_token:
        return JsonResponse({
            "action": "error", "open_door": False,
            "message": "unauthorized"
        }, status=401)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({
            "action": "error", "open_door": False,
            "message": "invalid json"
        }, status=400)

    card_uid = (data.get("card") or data.get("uid") or "").strip()
    umbr_uid = (data.get("umbrella_uid") or "").strip()
    box_has  = bool(data.get("box_has_umbrella", True))

    if not card_uid:
        return JsonResponse({
            "action": "error", "open_door": False,
            "message": "card uid required"
        }, status=400)

    print(f"[api_rent] card={card_uid!r} box={box_has} umbrella={umbr_uid!r}")

    # ─── Карта зарегистрирована? ───
    try:
        user = UserTag.objects.get(pass_tag=card_uid)
    except UserTag.DoesNotExist:
        print(f"  ❌ карта {card_uid} не зарегистрирована — дверь НЕ открыть")
        return JsonResponse({
            "action": "error",
            "open_door": False,                  # 🔒 чужак — не пускаем
            "message": "card not registered",
        }, status=404)

    with transaction.atomic():
        active = (
            Handout.objects.select_for_update()
            .filter(user=user, returned_at__isnull=True)
            .select_related("object").first()
        )

        # ─── 1. ВОЗВРАТ ───
        if active and box_has:
            obj = active.object
            active.returned_at = timezone.now()
            active.save(update_fields=["returned_at"])
            obj.needs_drying = True
            if obj.home_cell_id:
                obj.cell = obj.home_cell
            obj.save(update_fields=["cell", "needs_drying"])
            print(f"  ✅ ВОЗВРАТ {obj.irf_tag} — 🔓 дверь открыта")
            return JsonResponse({
                "action":    "return",
                "open_door": True,               # 🔓 открыть
                "umbrella":  obj.irf_tag,
                "user":      str(user),
                "message":   "Возврат принят, зонт на сушку",
            })

        # ─── 2. Ждём пока положат зонт в бокс ───
        if active and not box_has:
            print(f"  ⏳ ждём возврата {active.object.irf_tag} — 🔒 дверь НЕ открыта")
            return JsonResponse({
                "action":    "wait_return",
                "open_door": False,              # 🔒 не открываем — пусть кладёт
                "umbrella":  active.object.irf_tag,
                "user":      str(user),
                "message":   "Положите зонт в бокс",
            })

        # ─── 3. ВЫДАЧА ───
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
                print(f"  ⚠ нет свободных зонтов — 🔒 дверь НЕ открыта")
                return JsonResponse({
                    "action":    "error",
                    "open_door": False,          # 🔒 нечего выдавать — не открываем
                    "user":      str(user),
                    "message":   "Нет свободных зонтов",
                }, status=409)

            if not umbrella.home_cell_id and umbrella.cell_id:
                umbrella.home_cell = umbrella.cell
            umbrella.cell = None
            umbrella.save(update_fields=["cell", "home_cell"])

            Handout.objects.create(object=umbrella, user=user, issued_at=timezone.now())
            print(f"  ✅ ВЫДАН {umbrella.irf_tag} — 🔓 дверь открыта")
            return JsonResponse({
                "action":    "take",
                "open_door": True,               # 🔓 открыть
                "umbrella":  umbrella.irf_tag,
                "user":      str(user),
                "message":   "Зонт выдан",
            })

        # ─── 4. Бокс пустой ───
        print(f"  ℹ бокс пуст — 🔒 дверь НЕ открыта")
        return JsonResponse({
            "action":    "empty",
            "open_door": False,                  # 🔒 нечего брать
            "user":      str(user),
            "message":   "В боксе нет зонта",
        })

#  ШАГ 1: приложили КАРТУ - создаём сессию, ждём зонт

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


#  ШАГ 2: приложили ЗОНТ - ищем сессию, завершаем операцию

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


 # 3. Открытие двери
 


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


#  API: активные выдачи

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
            status_code, status_label = "drying", "сушится"
        elif o.needs_drying:
            status_code, status_label = "queue", "в очереди"
        elif o.cell_id:
            status_code, status_label = "ok", "на месте"
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


#  Сушилка - ловит запросов ESP

@csrf_exempt
def api_dryer_ping(request: HttpRequest, path: str = "") -> JsonResponse:
    """
    Ловятся запросы от ESP сушилки.

    Стадии:
      • UID считан + влажность > 80   ->  СТАРТ сушки (is_drying=True)
      • UID считан + 40 < H ≤ 80      ->  В ПРОЦЕССЕ (обновляем H, T)
      • event="finished" ИЛИ H < 40   ->  ЗАВЕРШЕНО (is_drying=False)
      • event="failed"                -> СБОЙ
    """
    HUMIDITY_WET     = 80.0   # > этого — точно мокрый, начинаем сушку
    HUMIDITY_DRY     = 40.0   # < этого — сухой, завершаем

    status = DryerStatus.get()

    # Парсим тело запроса 
    raw = ""
    try:
        raw = request.body.decode("utf-8", errors="replace")[:500]
    except Exception:
        pass

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
            event = (data.get("event") or "").strip().lower()
    except Exception:
        parsed = False

    if not parsed:
        m_h = re.search(r"humidity[=:]\s*([\d.]+)", raw)
        m_t = re.search(r"temp[=:]\s*([\d.]+)", raw)
        m_u = re.search(r"uid[=:]\s*([A-Fa-f0-9 :]+)", raw)
        m_e = re.search(r"event[=:]\s*(\w+)", raw)
        if m_h:
            try: humidity = float(m_h.group(1))
            except ValueError: pass
        if m_t:
            try: temp = float(m_t.group(1))
            except ValueError: pass
        if m_u: uid   = m_u.group(1).strip()
        if m_e: event = m_e.group(1).strip().lower()

    # Лог входящего запроса 
    print("\n" + "═" * 60)
    print(f"DRYER PING  /{path}")
    print(f"raw     = {raw[:200]}")
    print(f"uid     = {uid!r}")
    print(f"H       = {humidity}")
    print(f"T       = {temp}")
    print(f"event   = {event!r}")
    print("─" * 60)

    # Общий статус сушилки 
    if humidity is not None: status.last_humidity = humidity
    if temp     is not None: status.last_temp     = temp

    # ОБРАБОТКА БЕЗ UID 
    if not uid:
        if event == "finished":
            cnt = TrackedObject.objects.filter(is_drying=True).update(
                is_drying=False, last_dried_at=timezone.now()
            )
            status.is_active = False
            print(f"СТАДИЯ: ЗАВЕРШЕНО (без UID) — снято {cnt} зонтов")
        elif event in ("failed", "idle", "stop"):
            status.is_active = False
            print(f"СТАДИЯ: СТОП ({event})")
        else:
            # пустой пинг - сушилка просто жива
            status.is_active = True
            print(f"СТАДИЯ: ПИНГ (без UID, ничего не меняем)")
        status.last_raw = f"uid= H={humidity} T={temp} ev={event}"
        status.save()
        print("═" * 60)
        return JsonResponse({"ok": True, "message": "ping", "event": event})

    # ОБРАБОТКА С UID 
    try:
        obj = TrackedObject.objects.get(irf_tag=uid)
    except TrackedObject.DoesNotExist:
        print(f"зонт {uid!r} НЕ найден в БД")
        status.is_active = True
        status.last_raw  = f"uid={uid}(unknown) H={humidity} T={temp} ev={event}"
        status.save()
        print("═" * 60)
        return JsonResponse({"ok": False, "message": "umbrella not found"}, status=404)

    # обновляем замеры зонта
    if humidity is not None: obj.last_humidity = humidity
    if temp     is not None: obj.last_temp     = temp

    # ─── ЯВНЫЕ EVENT'ы от Arduino ───
    if event == "finished":
        obj.is_drying     = False
        obj.needs_drying  = False
        obj.last_dried_at = timezone.now()
        status.is_active  = False
        print(f"   📍 СТАДИЯ: ✅ ЗАВЕРШЕНО (event=finished)")
        print(f"            зонт {uid} высох (H={humidity})")

    elif event == "failed":
        obj.is_drying    = False
        status.is_active = False
        print(f"   📍 СТАДИЯ: ❌ СБОЙ (event=failed)")

    elif event == "start":
        obj.is_drying    = True
        obj.needs_drying = True
        status.is_active = True
        print(f"   📍 СТАДИЯ: ▶ СТАРТ (event=start, H={humidity})")

    else:
        # event пустой -> определяем стадию по влажности
        if humidity is None:
            # UID есть, но без замера — просто отмечаем что в сушилке
            obj.is_drying    = True
            obj.needs_drying = True
            status.is_active = True
            print(f"   СТАДИЯ: вставлен зонт (H неизвестна) — сушим")

        elif humidity > HUMIDITY_WET:
            # МОКРЫЙ → начинаем сушить
            obj.is_drying    = True
            obj.needs_drying = True
            status.is_active = True
            print(f"   СТАДИЯ: СУШИТСЯ  (H={humidity} > {HUMIDITY_WET})")

        elif humidity < HUMIDITY_DRY:
            # СУХОЙ → завершаем
            obj.is_drying     = False
            obj.needs_drying  = False
            obj.last_dried_at = timezone.now()
            status.is_active  = False
            print(f"    СТАДИЯ:  ВЫСОХ автоматически (H={humidity} < {HUMIDITY_DRY})")

        else:
            # промежуточная зона — продолжаем сушить
            obj.is_drying    = True
            obj.needs_drying = True
            status.is_active = True
            print(f"   📍 СТАДИЯ: 🔄 в процессе (H={humidity}, T={temp})")

    obj.save(update_fields=[
        "is_drying", "needs_drying",
        "last_humidity", "last_temp", "last_dried_at",
    ])

    status.last_raw = f"uid={uid} H={humidity} T={temp} ev={event}"
    status.save()

    print(f"   💾 saved: is_drying={obj.is_drying} needs_drying={obj.needs_drying}")
    print("═" * 60)

    return JsonResponse({
        "ok":       True,
        "umbrella": uid,
        "event":    event,
        "humidity": humidity,
        "temp":     temp,
    })


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