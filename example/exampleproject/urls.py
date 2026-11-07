from address_book import views
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("signup/", include("django_tenant_domains.contrib.signup.urls")),
    path("", include("django_tenant_domains.urls")),
    path("t/<str:tenant_slug>/", views.contact_list, name="contacts"),
    path("t/<str:tenant_slug>/contacts/add/", views.contact_add, name="contact_add"),
    path("t/<str:tenant_slug>/contacts/<int:pk>/edit/", views.contact_edit, name="contact_edit"),
    path(
        "t/<str:tenant_slug>/contacts/<int:pk>/delete/",
        views.contact_delete,
        name="contact_delete",
    ),
]
