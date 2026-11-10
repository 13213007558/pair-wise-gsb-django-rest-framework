from django.urls import path

from . import views

urlpatterns = [
    path("csrf/", views.csrf_view),
    path("session-first/", views.SessionFirstView.as_view()),
    path("token-first/", views.TokenFirstView.as_view()),
]
