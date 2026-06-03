from __future__ import annotations

import json
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Exists, OuterRef
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from .models import Cell, Handout, TrackedObject, UserTag, DryerStatus, RentSession


# ==========================================================
# HELPERS
# ==========================================================

def _get_umbrella_by_tag(tag: str) -> TrackedObject | None:
    tag = (tag or "").strip()
    if not tag:
        return None

    obj = TrackedObject.objects.filter(irf_tag=tag).first()
    if obj:
        return obj

    compact = re.sub(r"\s+", "", tag).upper()
    for c in TrackedObject.objects.only("id", "irf_tag"):
        if re.sub(r"\s+", "", c.irf_tag).upper() == compact:
            return TrackedObject.objects.filter(pk=c.pk).first()

    return None


def _available_umbrellas_qs():
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


ARDUINO_ACTIONS = {"take", "return", "error", "wait_return", "ok"}


# ==========================================================
# ESP SAFE RESPONSE (ВАЖНО!)
# ==========================================================

def _esp_response(payload: dict, status=200):
    """
    ESP32 НЕ должен получать лишние поля.
    Иначе: unknown error + DFPlayer 5.
    """
    clean = {
        "action": payload.get("action", "ok"),
        "open_door": bool(payload.get("open_door", False)),
    }

    if "umbrella" in payload:
        clean["umbrella"] = payload["umbrella"]

    return JsonResponse(clean, status=status)


def _error(msg: str, code: str = "error", status=400):
    return _esp_response({
        "action": "error",
        "open_door": False,
        "message": msg,
        "code": code,
    }, status=status)


# ==========================================================
# SESSION SAFE GET
# ==========================================================

def _get_session():
    cutoff = timezone.now() - timezone.timedelta(seconds=120)

    return (
        RentSession.objects
        .select_related("user")
        .filter(created_at__gte=cutoff)
        .order_by("-created_at")
        .first()
    )


# ==========================================================
# STEP 1 - CARD
# ==========================================================

def _start_session(user: UserTag):
    has_umbrella = Handout.objects.filter(user=user, returned_at__isnull=True).exists()
    mode = "return" if has_umbrella else "take"

    RentSession.objects.filter(user=user).delete()
    RentSession.objects.create(user=user, mode=mode)

    if mode == "return":
        active = Handout.objects.filter(user=user, returned_at__isnull=True).first()
        return _esp_response({
            "action": "wait_return",
            "umbrella": active.object.irf_tag if active else "",
            "open_door": False,
        })

    return _esp_response({
        "action": "ok",
        "open_door": False,
    })


# ==========================================================
# TAKE / RETURN
# ==========================================================

def _do_take(user: UserTag, umbrella: TrackedObject):
    if Handout.objects.filter(user=user, returned_at__isnull=True).exists():
        return _error("user already has umbrella", "user_busy")

    if Handout.objects.filter(object=umbrella, returned_at__isnull=True).exists():
        return _error("already issued", "already_out")

    if umbrella.is_drying:
        return _error("drying", "is_drying")

    if not umbrella.cell:
        return _error("no cell", "no_cell")

    umbrella.cell = None
    umbrella.save(update_fields=["cell"])

    Handout.objects.create(object=umbrella, user=user)

    return _esp_response({
        "action": "take",
        "open_door": True,
        "umbrella": umbrella.irf_tag,
    })


def _do_return(user: UserTag, umbrella: TrackedObject):
    active = (
        Handout.objects
        .select_for_update()
        .filter(user=user, returned_at__isnull=True)
        .first()
    )

    if not active:
        return _error("no active handout", "no_handout")

    if active.object_id != umbrella.id:
        return _error("wrong umbrella", "wrong_umbrella")

    active.returned_at = timezone.now()
    active.save()

    umbrella.needs_drying = True
    umbrella.save(update_fields=["needs_drying"])

    return _esp_response({
        "action": "return",
        "open_door": True,
        "umbrella": umbrella.irf_tag,
    })


# ==========================================================
# MAIN API
# ==========================================================

@csrf_exempt
@require_POST
def api_rent(request: HttpRequest):
    try:
        data = json.loads(request.body.decode())
    except Exception:
        return _error("invalid json", "json")

    card = (data.get("card") or "").strip()
    umbrella_uid = (data.get("umbrella") or data.get("uid") or "").strip()

    # STEP 2 - umbrella
    if not card and umbrella_uid:
        umbrella = _get_umbrella_by_tag(umbrella_uid)
        if not umbrella:
            return _error("umbrella not found", "umbrella_not_found")

        session = _get_session()
        if not session:
            return _error("need card first", "no_session")

        user = session.user
        session.delete()

        if session.mode == "take":
            return _do_take(user, umbrella)
        else:
            return _do_return(user, umbrella)

    # STEP 1 - card
    if card:
        try:
            user = UserTag.objects.get(pass_tag=card)
        except UserTag.DoesNotExist:
            return _error("card not registered", "card_not_found")

        return _start_session(user)

    return _error("empty request", "empty")


@csrf_exempt
@require_POST
def api_rent_card(request):
    return api_rent(request)


@csrf_exempt
@require_POST
def api_rent_umbrella(request):
    return api_rent(request)


# ==========================================================
# FIXED OBJECT LIST
# ==========================================================

@require_GET
def api_objects(request):
    objects = TrackedObject.objects.select_related("cell", "home_cell")

    active_ids = set(
        Handout.objects.filter(returned_at__isnull=True)
        .values_list("object_id", flat=True)
    )

    data = []

    for o in objects:
        if o.id in active_ids:
            status = "out"
        elif o.is_drying:
            status = "drying"
        elif o.needs_drying:
            status = "queue"
        elif o.cell:
            status = "ok"
        else:
            status = "out"

        data.append({
            "irf_tag": o.irf_tag,
            "name": o.name or "",
            "status": status,
        })

    return JsonResponse({"objects": data})


# ==========================================================
# PANEL + HOME (НЕ ТРОГАЛ)
# ==========================================================

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
        messages.error(request, "Доступ только staff.")
        return redirect("inventory:home")

    objects = TrackedObject.objects.select_related("cell", "home_cell").order_by("irf_tag")

    active_handouts = Handout.objects.select_related("object", "user").filter(
        returned_at__isnull=True
    )

    return render(request, "inventory/panel.html", {
        "objects": objects,
        "active_handouts": active_handouts,
    })