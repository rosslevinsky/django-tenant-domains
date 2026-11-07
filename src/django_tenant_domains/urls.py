from django.urls import path, re_path

from . import views

app_name = "tenant_domains"

urlpatterns = [
    path("tenants/", views.switcher, name="switcher"),
    path("invitations/<str:token>/", views.invitation, name="invitation"),
    path("t/<str:tenant_slug>/join/", views.join, name="join"),
    path("t/<str:tenant_slug>/members/invite/", views.invite_member, name="invite"),
    path("t/<str:tenant_slug>/members/requests/", views.join_requests, name="join_requests"),
    re_path(
        r"^t/(?P<tenant_slug>[^/]+)/members/requests/(?P<pk>\d+)/(?P<decision>approve|decline)/$",
        views.decide_join_request,
        name="decide_join_request",
    ),
]
