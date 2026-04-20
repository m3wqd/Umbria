from django.urls import path
from . import views

app_name = "inventory"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/rent/", views.api_rent, name="api_rent"),
    path("api/active/", views.api_active_handouts, name="api_active"),
    path("api/dryer/", views.api_dryer, name="api_dryer"),   # ✅ НОВЫЙ
]