from django.urls import path

from . import views

app_name = "tenant_signup"

urlpatterns = [
    path("", views.start, name="start"),
    path("activate/<str:token>/", views.activation, name="activate"),
]
