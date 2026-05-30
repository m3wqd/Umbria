from __future__ import annotations

import json
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.db.models import Exists, OuterRef
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from .models import Cell, Handout, TrackedObject, UserTag, DryerStatus, RentSession


def _parse_box_has(data: dict) -> bool | None:
    """None — датчик не передал; True/False — явное значение."""
    if "box_has_umbrella" not in data:
        return None
    return bool(data.get("box_has_umbrella"))


def _get_umbrella_by_tag(tag: str) -> TrackedObject | None:
    tag = (tag or "").strip()
    if not tag:
        return None
    obj = TrackedObject.objects.filter(irf_tag=tag).first()
    if obj:
        return obj
    compact = re.sub(r"\s+", "", tag).upper()
    for candidate in TrackedObject.objects.only("id", "irf_tag"):
        if re.sub(r"\s+", "", candidate.irf_tag).upper() == compact:
            return TrackedObject.objects.filter(pk=candidate.pk).first()
    return None


def _available_umbrellas_qs():
    """Зонты в ячейке, готовые к выдаче."""
    open_h = Handout.objects.filter(object=OuterRef("pk"), returned_at__isnull=True)
    return (
        TrackedObject.objects.filter(
            cell__isnull=False,
            is_drying=False,
            needs_drying=False,
        )
        .annotate(has_open=Exists(open_h))
        .filter(has_open=False)
        .order_by("irf_tag")
    )


def _issue_block_reason(umbrella: TrackedObject) -> str | None:
    if Handout.objects.filter(object=umbrella, returned_at__isnull=True).exists():
        return "already_out"
    if umbrella.is_drying or umbrella.needs_drying:
        return "not_ready"
    if not umbrella.cell_id:
        return "not_in_station"
    return None


def _api_error(
    message: str,
    *,
    error_code: str,
    status: int = 400,
    open_door: bool = False,
    **extra,
) -> JsonResponse:
    payload = {
        "action": "error",
        "open_door": open_door,
        "message": message,
        "error_code": error_code,
    }
    payload.update(extra)
    return JsonResponse(payload, status=status)


def _issue_umbrella(umbrella: TrackedObject, user: UserTag) -> None:
    if not umbrella.home_cell_id and umbrella.cell_id:
        umbrella.home_cell = umbrella.cell
    umbrella.cell = None
    umbrella.save(update_fields=["cell", "home_cell"])
    Handout.objects.create(object=umbrella, user=user, issued_at=timezone.now())


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
    umbr_uid = (
        data.get("umbrella_uid") or data.get("umbrella") or data.get("uid_umbrella") or ""
    ).strip()
    box_has = _parse_box_has(data)

    if not card_uid:
        return JsonResponse({
            "action": "error", "open_door": False,
            "message": "card uid required"
        }, status=400)

    print(f"[api_rent] card={card_uid!r} box={box_has} umbrella={umbr_uid!r}")

    #   Карта зарегистрирована?  
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

        #   1. ВОЗВРАТ
        if active and box_has is True:
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

        #   2. Ждём пока положат зонт в бокс
        if active and box_has is not True:
            print(f"  ⏳ ждём возврата {active.object.irf_tag} — 🔒 дверь НЕ открыта")
            return JsonResponse({
                "action":    "wait_return",
                "open_door": False,              # 🔒 не открываем — пусть кладёт
                "umbrella":  active.object.irf_tag,
                "user":      str(user),
                "message":   "Положите зонт в бокс",
            })

        # 3. ВЫДАЧА
        if not active:
            if umbr_uid:
                umbrella = _get_umbrella_by_tag(umbr_uid)
                if not umbrella:
                    print(f"  ⚠ зонт {umbr_uid!r} не в базе")
                    return _api_error(
                        "Зонт не зарегистрирован",
                        error_code="umbrella_not_found",
                        status=404,
                        user=str(user),
                    )
                block = _issue_block_reason(umbrella)
                if block == "already_out":
                    return _api_error(
                        "Этот зонт уже выдан",
                        error_code="already_out",
                        status=400,
                        user=str(user),
                    )
                if block == "not_ready":
                    return _api_error(
                        "Зонт ещё сохнет, подождите",
                        error_code="not_ready",
                        status=400,
                        user=str(user),
                    )
                if block == "not_in_station":
                    return _api_error(
                        "Зонт не в стойке",
                        error_code="not_in_station",
                        status=400,
                        user=str(user),
                    )
                _issue_umbrella(umbrella, user)
                print(f"  ✅ ВЫДАН {umbrella.irf_tag} — 🔓 дверь открыта")
                return JsonResponse({
                    "action": "take",
                    "open_door": True,
                    "umbrella": umbrella.irf_tag,
                    "user": str(user),
                    "message": "Зонт выдан",
                })

            if box_has is True:
                umbrella = _available_umbrellas_qs().first()
                if not umbrella:
                    print(f"  ⚠ нет свободных зонтов — 🔒 дверь НЕ открыта")
                    return _api_error(
                        "Нет свободных зонтов",
                        error_code="no_umbrellas",
                        status=409,
                        user=str(user),
                    )
                _issue_umbrella(umbrella, user)
                print(f"  ✅ ВЫДАН {umbrella.irf_tag} — 🔓 дверь открыта")
                return JsonResponse({
                    "action": "take",
                    "open_door": True,
                    "umbrella": umbrella.irf_tag,
                    "user": str(user),
                    "message": "Зонт выдан",
                })

            if box_has is False:
                print(f"  ℹ бокс пуст — 🔒 дверь НЕ открыта")
                return JsonResponse({
                    "action": "empty",
                    "open_door": False,
                    "user": str(user),
                    "message": "В боксе нет зонта",
                    "error_code": "box_empty",
                })

            print(f"  → ждём метку зонта (карта уже есть)")
            return JsonResponse({
                "action": "wait_umbrella",
                "open_door": False,
                "mode": "take",
                "user": str(user),
                "message": "Приложите зонт для выдачи",
                "available": _available_umbrellas_qs().count(),
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
        available = _available_umbrellas_qs().count()
        if available == 0:
            print(f"  ⚠ нет свободных зонтов (card)")
            return _api_error(
                "Нет свободных зонтов",
                error_code="no_umbrellas",
                status=409,
            )
        print(f"  → ждём зонт для ВЫДАЧИ ({available} доступно)")
        return JsonResponse({
            "action": "wait_umbrella",
            "mode": "take",
            "message": "приложите зонт для выдачи",
            "available": available,
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

    umbrella = _get_umbrella_by_tag(umbrella_uid)
    if not umbrella:
        return _api_error(
            "Зонт не зарегистрирован",
            error_code="umbrella_not_found",
            status=404,
        )

    cutoff = timezone.now() - timezone.timedelta(seconds=30)
    RentSession.objects.filter(created_at__lt=cutoff).delete()

    with transaction.atomic():
        session = RentSession.objects.select_for_update().order_by("-created_at").first()
        if not session:
            return _api_error(
                "Сначала приложите карту",
                error_code="need_card_first",
                status=400,
            )

        user = session.user
        mode = session.mode

        #   ВЫДАЧА  
        if mode == "take":
            block = _issue_block_reason(umbrella)
            if block == "already_out":
                session.delete()
                return _api_error(
                    "Этот зонт уже выдан",
                    error_code="already_out",
                    status=400,
                )
            if block == "not_ready":
                session.delete()
                return _api_error(
                    "Зонт ещё сохнет, подождите",
                    error_code="not_ready",
                    status=400,
                )
            if block == "not_in_station":
                session.delete()
                return _api_error(
                    "Зонт не в стойке",
                    error_code="not_in_station",
                    status=400,
                )

            _issue_umbrella(umbrella, user)
            session.delete()

            print(f"  ✅ ВЫДАН {umbrella.irf_tag} → {user.pass_tag}")
            return JsonResponse({
                "action": "take",
                "open_door": True,
                "umbrella": umbrella.irf_tag,
                "message": "зонт выдан",
            })

        #   ВОЗВРАТ  
        if mode == "return":
            active = (
                Handout.objects.select_for_update()
                .filter(user=user, returned_at__isnull=True).first()
            )
            if not active:
                session.delete()
                return _api_error(
                    "У клиента нет активной выдачи",
                    error_code="no_active_handout",
                    status=400,
                )

            if active.object_id != umbrella.id:
                return _api_error(
                    f"Ожидали зонт {active.object.irf_tag}, получен {umbrella.irf_tag}",
                    error_code="wrong_umbrella",
                    status=400,
                )

            active.returned_at = timezone.now()
            active.save(update_fields=["returned_at"])

            umbrella.needs_drying = True
            if umbrella.home_cell_id:
                umbrella.cell = umbrella.home_cell
            umbrella.save(update_fields=["cell", "needs_drying"])

            session.delete()

            print(f"  ✅ ВОЗВРАТ {umbrella.irf_tag}")
            return JsonResponse({
                "action": "return",
                "open_door": True,
                "umbrella": umbrella.irf_tag,
                "message": "возврат принят, зонт отправлен на сушку",
            })

        session.delete()
        return JsonResponse({"action": "error", "message": "unknown mode"}, status=400)


 # 3. Открытие двери
 


# =====================================================================
#  Веб-интерфейс
# =====================================================================

def home(request: HttpRequest) -> HttpResponse:
    """Публичная страница для посетителей."""
    total = TrackedObject.objects.count()
    on_hand = Handout.objects.filter(returned_at__isnull=True).count()
    available = TrackedObject.objects.filter(
        cell__isnull=False,
        is_drying=False,
        needs_drying=False,
    ).count()
    drying = TrackedObject.objects.filter(
        Q(is_drying=True) | Q(needs_drying=True)
    ).count()
    stations = Cell.objects.count()
    return render(request, "inventory/home.html", {
        "total": total,
        "on_hand": on_hand,
        "available": available,
        "drying": drying,
        "stations": stations,
    })


@login_required
def panel(request: HttpRequest) -> HttpResponse:
    if not request.user.is_staff:
        messages.error(request, "Доступ к панели только у сотрудников.")
        return redirect("inventory:home")
    if request.method == "POST":
        action   = request.POST.get("action", "")
        irf_tag  = (request.POST.get("irf_tag") or "").strip()
        pass_tag = (request.POST.get("pass_tag") or "").strip()

        if action == "take":
            if not irf_tag or not pass_tag:
                messages.error(request, "Укажите IRF-метку зонта и карту клиента.")
                return redirect("inventory:panel")
            try:
                obj = TrackedObject.objects.get(irf_tag=irf_tag)
            except TrackedObject.DoesNotExist:
                messages.error(request, f"Зонт '{irf_tag}' не найден.")
                return redirect("inventory:panel")
            try:
                user = UserTag.objects.get(pass_tag=pass_tag)
            except UserTag.DoesNotExist:
                messages.error(request, f"Карта '{pass_tag}' не найдена.")
                return redirect("inventory:panel")

            with transaction.atomic():
                if Handout.objects.filter(object=obj, returned_at__isnull=True).exists():
                    messages.error(request, "Этот зонт уже на руках.")
                    return redirect("inventory:panel")
                if not obj.home_cell_id and obj.cell_id:
                    obj.home_cell = obj.cell
                obj.cell = None
                obj.save(update_fields=["cell", "home_cell"])
                Handout.objects.create(object=obj, user=user, issued_at=timezone.now())

            messages.success(request, "Зонт выдан.")
            return redirect("inventory:panel")

        if action == "return":
            if not irf_tag:
                messages.error(request, "Укажите IRF-метку зонта.")
                return redirect("inventory:panel")
            cell_code = (request.POST.get("cell_code") or "").strip()
            try:
                obj = TrackedObject.objects.get(irf_tag=irf_tag)
            except TrackedObject.DoesNotExist:
                messages.error(request, f"Зонт '{irf_tag}' не найден.")
                return redirect("inventory:panel")

            with transaction.atomic():
                active = (
                    Handout.objects.select_for_update()
                    .filter(object=obj, returned_at__isnull=True)
                    .order_by("-issued_at").first()
                )
                if not active:
                    messages.error(request, "Активной выдачи для этого зонта нет.")
                    return redirect("inventory:panel")

                active.returned_at = timezone.now()
                active.save(update_fields=["returned_at"])

                if cell_code:
                    try:
                        cell = Cell.objects.get(cell_code=cell_code)
                    except Cell.DoesNotExist:
                        messages.error(request, f"Ячейка '{cell_code}' не найдена.")
                        return redirect("inventory:panel")
                    obj.cell = cell
                elif obj.home_cell_id:
                    obj.cell = obj.home_cell

                obj.needs_drying = True
                obj.save(update_fields=["cell", "needs_drying"])

            messages.success(request, "Зонт возвращён и отправлен на сушку.")
            return redirect("inventory:panel")

        messages.error(request, "Неизвестное действие.")
        return redirect("inventory:panel")

    # GET
    objects = TrackedObject.objects.select_related("cell", "home_cell").order_by("irf_tag")
    active_handouts = (
        Handout.objects.select_related("object", "user")
        .filter(returned_at__isnull=True).order_by("-issued_at")
    )
    cells = Cell.objects.order_by("cell_code")
    users = UserTag.objects.order_by("pass_tag")

    return render(request, "inventory/panel.html", {
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

    #   ЯВНЫЕ EVENT'ы от Arduino  
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