from django.urls import path, re_path
from . import views

app_name = "inventory"

urlpatterns = [
    path("",              views.index,                name="index"),
    path("api/rent/",     views.api_rent,             name="api_rent"),
    path("api/active/",   views.api_active_handouts,  name="api_active"),
    path("api/dryer/status/", views.api_dryer_status, name="api_dryer_status"),

    # === ЛОВЦЫ ЛЮБЫХ ЗАПРОСОВ ОТ СУШИЛКИ ===
    # Подходит под /api/dryer, /api/dryer/, /api/dry, /api/humidity и т.д.
    re_path(r"^api/dryer/?$",        views.api_dryer_ping, name="api_dryer_ping_1"),
    re_path(r"^api/dry/?$",          views.api_dryer_ping, name="api_dryer_ping_2"),
    re_path(r"^api/humidity/?$",     views.api_dryer_ping, name="api_dryer_ping_3"),
    re_path(r"^api/sensor/?$",       views.api_dryer_ping, name="api_dryer_ping_4"),
    re_path(r"^api/esp/?$",          views.api_dryer_ping, name="api_dryer_ping_5"),
]