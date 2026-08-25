from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import LoginView, LogoutView, MeView

app_name = "accounts"

# No public registration: accounts are created by staff via the Django admin or
# `python manage.py createsuperuser`. There is intentionally no register route.
urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
]
