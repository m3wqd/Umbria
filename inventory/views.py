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


# =====================================================================
#  Утилиты поиска зонта по метке
# =====================================================================
def _get_umbrella_by_tag(tag: str) -> TrackedObject | None:
    tag = (tag or "").strip()
    if not tag:
        return None

    # 1) Точное совпадение
    obj = TrackedObject.objects.filter(irf_tag=tag).first()
    if obj:
        return obj

    # 2) Нормализуем (убираем пробелы, двоеточия, тире)
    def normalize(s: str) -> str:
        return re.sub(r"[\s:\-]+", "", s).upper()

    compact = normalize(tag)
    if not compact:
        return None
    for candidate in TrackedObject.objects.only("id", "irf_tag"):
        if normalize(candidate.irf_tag) == compact:
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


# =====================================================================
#  Ответы для Arduino: action + sound + open_door
# =====================================================================
_ARDUINO_ACTIONS = frozenset({
    "take", "return", "error", "wait_return", "empty", "ok",
})

# 🎯 Маппинг action → номер mp3 на SD (0001..0005.mp3)
_SOUND_BY_ACTION = {
    "take":        1,   # 0001.mp3  «Возьмите зонт»
    "wait_return": 2,   # 0002.mp3  «Положите зонт в бокс»
    "return":      3,   # 0003.mp3  «Зонт возвращён, спасибо»
    "ok":          2,   # ждём зонт — та же подсказка что и wait_return
    "empty":       5,   # 0005.mp3  «Нет свободных зонтов»
}

# Маппинг error_code → номер mp3 (для конкретных ошибок)
_SOUND_BY_ERROR = {
    "card_not_registered":  4,   # 0004.mp3 «Карта не зарегистрирована»
    "umbrella_not_found":   5,   # на всякий случай тоже звук «нет/ошибка»
    "no_active_handout":    5,
    "wrong_umbrella":       5,
    "already_out":          5,
    "is_drying":            5,
    "need_card_first":      4,
    "umbrella_required":    0,
    "card_required":        0,
    "invalid_json":         0,
    "unauthorized":         0,
    "unknown_mode":         0,
}


def _rent_response(payload: dict, *, status: int = 200) -> JsonResponse:
    """Универсальный ответ для прошивки стойки."""
    action = str(payload.get("action", ""))

    # Нормализуем action
    if action == "wait_umbrella":
        mode = payload.get("mode", "take")
        payload["action"] = "wait_return" if mode == "return" else "ok"
        action = payload["action"]
    elif action not in _ARDUINO_ACTIONS:
        payload["action"] = "ok"
        action = "ok"

    # Если sound уже задан явно — оставляем, иначе подставляем по action
    if "sound" not in payload:
        if action == "error":
            err = payload.get("error_code", "")
            payload["sound"] = _SOUND_BY_ERROR.get(err, 5)
        else:
            payload["sound"] = _SOUND_BY_ACTION.get(action, 0)

    payload.pop("available", None)
    payload.setdefault("open_door", False)
    return JsonResponse(payload, status=status)


def _api_error(
    message: str,
    *,
    error_code: str,
    status: int = 400,
    open_door: bool = False,
    sound: int | None = None,
    **extra,
) -> JsonResponse:
    payload = {
        "action": "error",
        "open_door": open_door,
        "message": message,
        "error_code": error_code,
    }
    if sound is not None:
        payload["sound"] = sound
    payload.update(extra)
    return _rent_response(payload, status=status)


def _api_json(payload: dict, *, status: int = 200) -> JsonResponse:
    return _rent_response(payload, status=status)


# =====================================================================
#  Логика выдачи / возврата
# =====================================================================
def _api_rent_start_session(card_uid: str) -> JsonResponse:
    """Шаг 1: приложили карту → создаём сессию, ждём зонт."""
    try:
        user = UserTag.objects.get(pass_tag=card_uid)
    except UserTag.DoesNotExist:
        print(f"  ❌ карта {card_uid!r} не зарегистрирована")
        return _api_error(
            "Карта не зарегистрирована",
            error_code="card_not_registered",
            status=404,
        )

    cutoff = timezone.now() - timezone.timedelta(seconds=120)
    RentSession.objects.filter(created_at__lt=cutoff).delete()

    has_umbrella = Handout.objects.filter(user=user, returned_at__isnull=True).exists()
    mode = "return" if has_umbrella else "take"

    RentSession.objects.filter(user=user).delete()
    RentSession.objects.create(user=user, mode=mode)

    if mode == "return":
        active = (
            Handout.objects.filter(user=user, returned_at__isnull=True)
            .select_related("object").first()
        )
        umbrella_tag = active.object.irf_tag if active else ""
        print(f"  → сессия RETURN, ждём зонт {umbrella_tag!r}")
        return _api_json({
            "action": "wait_return",
            "open_door": False,
            "umbrella": umbrella_tag,
            "message": "Приложите зонт для возврата",
        })

    free = _available_umbrellas_qs().count()
    print(f"  → сессия TAKE, ждём зонт (свободно в базе: {free})")
    return _api_json({
        "action": "ok",
        "open_door": False,
        "message": "Приложите зонт для выдачи",
    })


def _issue_umbrella(umbrella: TrackedObject, user: UserTag) -> None:
    if not umbrella.home_cell_id and umbrella.cell_id:
        umbrella.home_cell = umbrella.cell
    umbrella.cell = None
    umbrella.save(update_fields=["cell", "home_cell"])
    Handout.objects.create(object=umbrella, user=user, issued_at=timezone.now())


def _parse_rent_ids(data: dict) -> tuple[str, str]:
    """Разделить uid карты и метку зонта."""
    card_uid = (data.get("card") or "").strip()
    umbr_uid = (data.get("umbrella_uid") or data.get("umbrella") or "").strip()
    raw_uid = (data.get("uid") or "").strip()

    if not card_uid and raw_uid:
        if UserTag.objects.filter(pass_tag=raw_uid).exists():
            card_uid = raw_uid
        elif _get_umbrella_by_tag(raw_uid):
            umbr_uid = umbr_uid or raw_uid
    return card_uid, umbr_uid


def _parse_box_has(data: dict) -> bool | None:
    if "box_has_umbrella" not in data:
        return None
    return bool(data.get("box_has_umbrella"))


def _do_take(user: UserTag, umbrella: TrackedObject) -> JsonResponse | None:
    if Handout.objects.filter(object=umbrella, returned_at__isnull=True).exists():
        return _api_error("Этот зонт уже выдан", error_code="already_out", status=400)
    if umbrella.is_drying:
        return _api_error("Зонт в сушилке", error_code="is_drying", status=400)

    _issue_umbrella(umbrella, user)
    print(f"  ✅ ВЫДАН {umbrella.irf_tag} → {user.pass_tag}")
    return _api_json({
        "action": "take",
        "open_door": True,
        "umbrella": umbrella.irf_tag,
        "user": str(user),
        "message": "Зонт выдан",
    })


def _do_return(user: UserTag, umbrella: TrackedObject) -> JsonResponse | None:
    active = (
        Handout.objects.select_for_update()
        .filter(user=user, returned_at__isnull=True)
        .select_related("object")
        .first()
    )
    if not active:
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

    print(f"  ✅ ВОЗВРАТ {umbrella.irf_tag}")
    return _api_json({
        "action": "return",
        "open_door": True,
        "umbrella": umbrella.irf_tag,
        "user": str(user),
        "message": "Возврат принят, зонт на сушку",
    })


def _complete_rent_for_user(user: UserTag, umbrella: TrackedObject) -> JsonResponse:
    has_active = Handout.objects.filter(user=user, returned_at__isnull=True).exists()
    mode = "return" if has_active else "take"
    if mode == "take":
        resp = _do_take(user, umbrella)
    else:
        resp = _do_return(user, umbrella)
    assert resp is not None
    return resp


def _api_rent_umbrella_step(umbr_uid: str) -> JsonResponse:
    umbrella = _get_umbrella_by_tag(umbr_uid)
    if not umbrella:
        return _api_error(
            "Зонт не зарегистрирован",
            error_code="umbrella_not_found",
            status=404,
        )

    cutoff = timezone.now() - timezone.timedelta(seconds=120)
    RentSession.objects.filter(created_at__lt=cutoff).delete()

    with transaction.atomic():
        session = (
            RentSession.objects.select_for_update()
            .order_by("-created_at")
            .select_related("user")
            .first()
        )
        if not session:
            return _api_error(
                "Сначала приложите карту",
                error_code="need_card_first",
                status=400,
            )
        RentSession.objects.filter(pk=session.pk).delete()
        return _complete_rent_for_user(session.user, umbrella)


# =====================================================================
#  /api/rent/  —  карта (или карта+зонт за один раз)
# =====================================================================
@csrf_exempt
@require_POST
def api_rent(request: HttpRequest) -> JsonResponse:
    expected_token = getattr(settings, "ARDUINO_TOKEN", None)
    if expected_token and request.headers.get("X-Device-Token") != expected_token:
        return _api_error("unauthorized", error_code="unauthorized", status=401)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _api_error("invalid json", error_code="invalid_json", status=400)

    card_uid, umbr_uid = _parse_rent_ids(data)
    box_has = _parse_box_has(data)

    if not card_uid and umbr_uid:
        print(f"[api_rent] только зонт {umbr_uid!r}")
        return _api_rent_umbrella_step(umbr_uid)

    if not card_uid:
        return _api_error("card uid required", error_code="card_required", status=400)

    print(f"[api_rent] card={card_uid!r} box={box_has} umbrella={umbr_uid!r}")

    try:
        user = UserTag.objects.get(pass_tag=card_uid)
    except UserTag.DoesNotExist:
        return _api_error(
            "Карта не зарегистрирована",
            error_code="card_not_registered",
            status=404,
        )

    with transaction.atomic():
        active = (
            Handout.objects.select_for_update()
            .filter(user=user, returned_at__isnull=True)
            .select_related("object")
            .first()
        )

        if active and box_has is True:
            return _do_return(user, active.object)

        if active and box_has is not True:
            return _api_json({
                "action": "wait_return",
                "open_door": False,
                "umbrella": active.object.irf_tag,
                "user": str(user),
                "message": "Положите зонт в бокс",
            })

        if umbr_uid:
            umbrella = _get_umbrella_by_tag(umbr_uid)
            if not umbrella:
                return _api_error(
                    "Зонт не зарегистрирован",
                    error_code="umbrella_not_found",
                    status=404,
                )
            return _complete_rent_for_user(user, umbrella)

        if box_has is True:
            umbrella = _available_umbrellas_qs().first()
            if umbrella:
                return _do_take(user, umbrella)
            print(f"  ⚠ нет свободных — ждём метку зонта")
            return _api_json({
                "action": "ok",
                "open_door": False,
                "message": "Приложите зонт для выдачи",
            })

    return _api_rent_start_session(card_uid)


@csrf_exempt
@require_POST
def api_rent_card(request: HttpRequest) -> JsonResponse:
    """Алиас для /api/rent/."""
    return api_rent(request)


# =====================================================================
#  /api/rent/umbrella/  —  второй шаг: метка зонта
# =====================================================================
@csrf_exempt
@require_POST
def api_rent_umbrella(request: HttpRequest) -> JsonResponse:
    expected_token = getattr(settings, "ARDUINO_TOKEN", None)
    if expected_token and request.headers.get("X-Device-Token") != expected_token:
        return _api_error("unauthorized", error_code="unauthorized", status=401)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _api_error("invalid json", error_code="invalid_json", status=400)

    _, umbrella_uid = _parse_rent_ids(data)
    if not umbrella_uid:
        umbrella_uid = (data.get("umbrella") or data.get("uid") or "").strip()
    if not umbrella_uid:
        return _api_error("umbrella uid required", error_code="umbrella_required", status=400)

    print(f"[api_rent_umbrella] umbrella={umbrella_uid!r}")

    umbrella = _get_umbrella_by_tag(umbrella_uid)
    if not umbrella:
        return _api_error(
            "Зонт не зарегистрирован",
            error_code="umbrella_not_found",
            status=404,
        )

    cutoff = timezone.now() - timezone.timedelta(seconds=120)
    RentSession.objects.filter(created_at__lt=cutoff).delete()

    with transaction.atomic():
        session = (
            RentSession.objects.select_for_update()
            .order_by("-created_at")
            .select_related("user")
            .first()
        )
        if not session:
            return _api_error(
                "Сначала приложите карту",
                error_code="need_card_first",
                status=400,
            )

        user = session.user
        RentSession.objects.filter(pk=session.pk).delete()

        if session.mode == "take":
            resp = _do_take(user, umbrella)
        elif session.mode == "return":
            resp = _do_return(user, umbrella)
        else:
            return _api_error("unknown mode", error_code="unknown_mode", status=400)

        return resp


# =====================================================================
#  Веб-интерфейс
# =====================================================================
def home(request: HttpRequest) -> HttpResponse:
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
#  API: список зонтов
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


# =====================================================================
#  Сушилка
# =====================================================================
@csrf_exempt
def api_dryer_ping(request: HttpRequest, path: str = "") -> JsonResponse:
    HUMIDITY_WET = 80.0
    HUMIDITY_DRY = 40.0

    status = DryerStatus.get()

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

    print("\n" + "═" * 60)
    print(f"DRYER PING  /{path}")
    print(f"raw     = {raw[:200]}")
    print(f"uid     = {uid!r}")
    print(f"H       = {humidity}")
    print(f"T       = {temp}")
    print(f"event   = {event!r}")
    print("─" * 60)

    if humidity is not None: status.last_humidity = humidity
    if temp     is not None: status.last_temp     = temp

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
            status.is_active = True
            print(f"СТАДИЯ: ПИНГ (без UID, ничего не меняем)")
        status.last_raw = f"uid= H={humidity} T={temp} ev={event}"
        status.save()
        print("═" * 60)
        return JsonResponse({"ok": True, "message": "ping", "event": event})

    try:
        obj = TrackedObject.objects.get(irf_tag=uid)
    except TrackedObject.DoesNotExist:
        print(f"зонт {uid!r} НЕ найден в БД")
        status.is_active = True
        status.last_raw  = f"uid={uid}(unknown) H={humidity} T={temp} ev={event}"
        status.save()
        print("═" * 60)
        return JsonResponse({"ok": False, "message": "umbrella not found"}, status=404)

    if humidity is not None: obj.last_humidity = humidity
    if temp     is not None: obj.last_temp     = temp

    if event == "finished":
        obj.is_drying     = False
        obj.needs_drying  = False
        obj.last_dried_at = timezone.now()
        status.is_active  = False
        print(f"   📍 СТАДИЯ: ✅ ЗАВЕРШЕНО (event=finished)")

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
        if humidity is None:
            obj.is_drying    = True
            obj.needs_drying = True
            status.is_active = True
            print(f"   СТАДИЯ: вставлен зонт (H неизвестна) — сушим")

        elif humidity > HUMIDITY_WET:
            obj.is_drying    = True
            obj.needs_drying = True
            status.is_active = True
            print(f"   СТАДИЯ: СУШИТСЯ  (H={humidity} > {HUMIDITY_WET})")

        elif humidity < HUMIDITY_DRY:
            obj.is_drying     = False
            obj.needs_drying  = False
            obj.last_dried_at = timezone.now()
            status.is_active  = False
            print(f"    СТАДИЯ:  ВЫСОХ автоматически (H={humidity} < {HUMIDITY_DRY})")

        else:
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