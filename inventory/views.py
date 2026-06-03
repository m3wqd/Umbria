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
#  UTILS
# ==========================================================

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
            return candidate
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


# ==========================================================
#  ESP32 PROTOCOL HELPERS
# ==========================================================

_ARDUINO_ACTIONS = frozenset({"take", "return", "error", "wait_return", "ok"})


def _rent_response(payload: dict, *, status: int = 200) -> JsonResponse:
    action = str(payload.get("action", ""))

    if action not in _ARDUINO_ACTIONS:
        payload["action"] = "ok"

    payload.setdefault("open_door", False)
    payload["sound"] = payload.get("sound", 0)

    return JsonResponse(payload, status=status)


def _api_error(message: str, *, error_code: str, status: int = 400, **extra):
    return _rent_response({
        "action": "error",
        "message": message,
        "error_code": error_code,
        "open_door": False,
        **extra,
    }, status=status)


def _api_json(payload: dict, *, status: int = 200):
    return _rent_response(payload, status=status)


# ==========================================================
#  SESSION START (CARD)
# ==========================================================

def _api_rent_start_session(card_uid: str) -> JsonResponse:
    try:
        user = UserTag.objects.get(pass_tag=card_uid)
    except UserTag.DoesNotExist:
        return _api_error("card_not_registered", error_code="card_not_registered", status=404)

    has_umbrella = Handout.objects.filter(user=user, returned_at__isnull=True).exists()
    mode = "return" if has_umbrella else "take"

    RentSession.objects.filter(user=user).delete()
    RentSession.objects.create(user=user, mode=mode)

    if mode == "return":
        active = Handout.objects.filter(user=user, returned_at__isnull=True).first()
        return _api_json({
            "action": "wait_return",
            "umbrella": active.object.irf_tag if active else "",
            "open_door": False,
        })

    return _api_json({
        "action": "ok",
        "open_door": False,
    })


# ==========================================================
#  CORE TAKE / RETURN
# ==========================================================

def _issue_umbrella(umbrella: TrackedObject, user: UserTag):
    if not umbrella.home_cell_id and umbrella.cell_id:
        umbrella.home_cell = umbrella.cell

    umbrella.cell = None
    umbrella.save(update_fields=["cell", "home_cell"])

    Handout.objects.create(object=umbrella, user=user, issued_at=timezone.now())


def _do_take(user: UserTag, umbrella: TrackedObject):
    if Handout.objects.filter(object=umbrella, returned_at__isnull=True).exists():
        return _api_error("already_taken", error_code="already_taken", status=400)

    _issue_umbrella(umbrella, user)

    return _api_json({
        "action": "take",
        "open_door": True,
        "umbrella": umbrella.irf_tag,
    })


def _do_return(user: UserTag, umbrella: TrackedObject):
    active = Handout.objects.filter(user=user, returned_at__isnull=True).first()

    if not active:
        return _api_error("no_active_rent", error_code="no_active_rent", status=400)

    if active.object_id != umbrella.id:
        return _api_error("wrong_umbrella", error_code="wrong_umbrella", status=400)

    active.returned_at = timezone.now()
    active.save()

    umbrella.needs_drying = True
    if umbrella.home_cell_id:
        umbrella.cell = umbrella.home_cell

    umbrella.save(update_fields=["cell", "needs_drying"])

    return _api_json({
        "action": "return",
        "open_door": True,
        "umbrella": umbrella.irf_tag,
    })


def _complete(user: UserTag, umbrella: TrackedObject):
    has_active = Handout.objects.filter(user=user, returned_at__isnull=True).exists()
    return _do_return(user, umbrella) if has_active else _do_take(user, umbrella)


# ==========================================================
#  API: ACTIVE HANDOUTS (FIXED — YOUR ERROR)
# ==========================================================

@require_GET
def api_active_handouts(request: HttpRequest) -> JsonResponse:
    handouts = Handout.objects.select_related("object", "user").filter(
        returned_at__isnull=True
    ).order_by("-issued_at")

    return JsonResponse({
        "handouts": [
            {
                "object_name": h.object.name or "object",
                "object_tag": h.object.irf_tag,
                "user_name": h.user.full_name or "user",
                "user_tag": h.user.pass_tag,
                "issued_at": timezone.localtime(h.issued_at).strftime("%d.%m.%Y %H:%M:%S"),
            }
            for h in handouts
        ]
    })


# ==========================================================
#  API: MAIN RENT ENDPOINT
# ==========================================================

@csrf_exempt
@require_POST
def api_rent(request: HttpRequest) -> JsonResponse:
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return _api_error("invalid_json", error_code="invalid_json", status=400)

    card = (data.get("card") or "").strip()
    umbrella = (data.get("umbrella") or "").strip()

    if not card and umbrella:
        obj = _get_umbrella_by_tag(umbrella)
        if not obj:
            return _api_error("umbrella_not_found", error_code="umbrella_not_found", status=404)

        user = UserTag.objects.filter(handout__returned_at__isnull=True).first()
        if not user:
            return _api_error("need_card_first", error_code="need_card_first", status=400)

        return _complete(user, obj)

    if not card:
        return _api_error("card_required", error_code="card_required", status=400)

    user = UserTag.objects.get(pass_tag=card)

    if umbrella:
        obj = _get_umbrella_by_tag(umbrella)
        if not obj:
            return _api_error("umbrella_not_found", error_code="umbrella_not_found", status=404)
        return _complete(user, obj)

    return _api_rent_start_session(card)


# ==========================================================
#  PANEL (simplified, unchanged logic preserved)
# ==========================================================

@login_required
def panel(request):
    if not request.user.is_staff:
        return redirect("inventory:home")

    objects = TrackedObject.objects.select_related("cell", "home_cell")
    handouts = Handout.objects.filter(returned_at__isnull=True)

    return render(request, "inventory/panel.html", {
        "objects": objects,
        "active_handouts": handouts,
    })


# ==========================================================
#  HOME
# ==========================================================

def home(request):
    return render(request, "inventory/home.html", {})