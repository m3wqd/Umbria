from __future__ import annotations
from django.contrib import messages
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from .models import Cell, Handout, TrackedObject, UserTag
import requests
import requests_mock



def test_signal(request):
    # Подключение к имитации ардуино
    url = "http://127.0.0" 
    try:
        response = requests.get(url, timeout=2)
        return HttpResponse(f"Ардуино ответила: {response.text}")
    except:
        return HttpResponse("Ошибка: Заглушка не запущена!", status=500)



# def send_command_to_arduino(request):
#     with requests_mock.Mocker() as m:
#         # Перехватываем запрос на этот адрес
#         m.get('http://fake-arduino.local', text='Success', status_code=200)
        
#         response = requests.get('http://fake-arduino.local')
#         return HttpResponse(f"Тестовый ответ: {response.text}")


# def trigger_arduino(request):
#     arduino_ip = "http://192.168.1" # IP  Arduino
#     try:
#         response = requests.get(arduino_ip, timeout=5)
#         return HttpResponse(f"Статус ответа: {response.status_code}")
#     except requests.exceptions.RequestException as e:
#         return HttpResponse(f"Ошибка связи: {e}")



def index(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        action = request.POST.get("action", "")
        irf_tag = (request.POST.get("irf_tag") or "").strip()
        pass_tag = (request.POST.get("pass_tag") or "").strip()

        if action == "take":
            if not irf_tag or not pass_tag:
                messages.error(request, "Нужно указать IRF-метку объекта и метку/пропуск пользователя.")
                return redirect("inventory:index")

            try:
                obj = TrackedObject.objects.get(irf_tag=irf_tag)
            except TrackedObject.DoesNotExist:
                messages.error(request, f"Объект с IRF-меткой '{irf_tag}' не найден.")
                return redirect("inventory:index")

            try:
                user = UserTag.objects.get(pass_tag=pass_tag)
            except UserTag.DoesNotExist:
                messages.error(request, f"Пользователь с меткой/пропуском '{pass_tag}' не найден.")
                return redirect("inventory:index")

            with transaction.atomic():
                active_exists = Handout.objects.filter(object=obj, returned_at__isnull=True).exists()
                if active_exists:
                    messages.error(request, "Этот объект уже находится на руках (есть активная выдача).")
                    return redirect("inventory:index")

                obj.cell = None
                obj.save(update_fields=["cell"])

                Handout.objects.create(object=obj, user=user, issued_at=timezone.now())

            messages.success(request, "Объект выдан на руки.")
            return redirect("inventory:index")

        if action == "return":
            if not irf_tag:
                messages.error(request, "Нужно указать IRF-метку объекта.")
                return redirect("inventory:index")

            cell_code = (request.POST.get("cell_code") or "").strip()
            try:
                obj = TrackedObject.objects.get(irf_tag=irf_tag)
            except TrackedObject.DoesNotExist:
                messages.error(request, f"Объект с IRF-меткой '{irf_tag}' не найден.")
                return redirect("inventory:index")

            with transaction.atomic():
                active = (
                    Handout.objects.select_for_update()
                    .filter(object=obj, returned_at__isnull=True)
                    .order_by("-issued_at")
                    .first()
                )
                if not active:
                    messages.error(request, "Для этого объекта нет активной выдачи.")
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
                    obj.save(update_fields=["cell"])

            messages.success(request, "Объект возвращён.")
            return redirect("inventory:index")

        messages.error(request, "Неизвестное действие.")
        return redirect("inventory:index")

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

