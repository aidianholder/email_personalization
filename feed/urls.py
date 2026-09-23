from django.urls import path

from . import views

urlpatterns = [
    path("api/morning_email", views.morning_email, name="morning_email"),
    # Path form, for a CDN that strips or ignores query strings.
    path("api/morning_email/<path:uid>", views.morning_email, name="morning_email_path"),
    path("healthz", views.healthz, name="healthz"),
]
