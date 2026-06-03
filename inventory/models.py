from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET
from django.db import transaction
import json

from .models import TrackedObject, UserTag, Handout, RentSession, DryerStatus, Cell


# =========================================================
#  ЕДИНЫЙ ОТВЕТ ДЛЯ ESP
# =========================================================
def esp_response(action="ok", open_door=False, message="", sound=0, status=200):
    return JsonResponse({
        "action": action,
        "open_door": open_door,
        "message": message,
        "sound": sound
    }, status=status)


# =========================================================
#  ACTIVE HANDOUTS (FIX URL ERROR)
# =========================================================
@require_GET
def api_active_handouts(request):
    handouts = Handout.objects.filter(returned_at__isnull=True)

    return JsonResponse({
        "handouts": [
            {
                "object": h.object.irf_tag,
                "user": h.user.pass_tag,
            }
            for h in handouts
        ]
    })


# =========================================================
#  RENT SESSION START (CARD)
# =========================================================
@csrf_exempt
@require_POST
def api_rent_card(request):
    try:
        data = json.loads(request.body)
    except:
        return esp_response("error", message="bad json", sound=5, status=400)

    card = (data.get("card") or "").strip()
    if not card:
        return esp_response("error", message="no card", sound=5, status=400)

    try:
        user = UserTag.objects.get(pass_tag=card)
    except UserTag.DoesNotExist:
        return esp_response("error", message="card not registered", sound=4, status=404)

    has_umbrella = Handout.objects.filter(user=user, returned_at__isnull=True).exists()
    mode = "return" if has_umbrella else "take"

    RentSession.objects.filter(user=user).delete()
    RentSession.objects.create(user=user, mode=mode)

    if mode == "return":
        return esp_response(
            "wait_return",
            message="Put umbrella back",
            sound=2
        )

    return esp_response(
        "ok",
        message="Take umbrella",
        sound=1
    )


# =========================================================
#  UMBRELLA STEP
# =========================================================
@csrf_exempt
@require_POST
def api_rent_umbrella(request):
    try:
        data = json.loads(request.body)
    except:
        return esp_response("error", message="bad json", sound=5, status=400)

    uid = (data.get("umbrella") or data.get("uid") or "").strip()

    if not uid:
        return esp_response("error", message="no umbrella", sound=5, status=400)

    try:
        umbrella = TrackedObject.objects.get(irf_tag=uid)
    except TrackedObject.DoesNotExist:
        return esp_response("error", message="umbrella not found", sound=5, status=404)

    session = RentSession.objects.order_by("-created_at").first()
    if not session:
        return esp_response("error", message="no session", sound=4, status=400)

    user = session.user

    # TAKE
    if session.mode == "take":
        if Handout.objects.filter(object=umbrella, returned_at__isnull=True).exists():
            return esp_response("error", message="already taken", sound=5, status=400)

        umbrella.cell = None
        umbrella.save()

        Handout.objects.create(object=umbrella, user=user, issued_at=timezone.now())

        session.delete()

        return esp_response(
            "take",
            open_door=True,
            message="umbrella issued",
            sound=1
        )

    # RETURN
    active = Handout.objects.filter(user=user, returned_at__isnull=True).first()
    if not active:
        return esp_response("error", message="no active rent", sound=4, status=400)

    if active.object.irf_tag != umbrella.irf_tag:
        return esp_response("error", message="wrong umbrella", sound=5, status=400)

    active.returned_at = timezone.now()
    active.save()

    umbrella.needs_drying = True
    umbrella.save()

    session.delete()

    return esp_response(
        "return",
        open_door=True,
        message="returned",
        sound=3
    )


# =========================================================
#  SIMPLE RENT (OLD COMPAT)
# =========================================================
@csrf_exempt
@require_POST
def api_rent(request):
    return api_rent_card(request)


# =========================================================
#  HOME
# =========================================================
def home(request):
    return HttpResponse("OK")


# =========================================================
#  PANEL (MINIMAL SAFE)
# =========================================================
def panel(request):
    return HttpResponse("panel ok")


# =========================================================
#  ACTIVE alias fix (IMPORTANT)
# =========================================================
api_rent_card = api_rent_card
api_rent_umbrella = api_rent_umbrella
api_active_handouts = api_active_handouts