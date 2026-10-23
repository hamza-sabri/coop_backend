"""/public/app-config/: the phone app's remote switches."""
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

URL = "/api/v1/public/app-config/"


class AppConfig(TestCase):
    def test_defaults_are_harmless(self):
        with override_settings(APP_MIN_BUILD=0, APP_LATEST_BUILD=0, APP_API_BASE=""):
            body = APIClient().get(URL).json()
        self.assertEqual(body["min_build"], 0)
        self.assertEqual(body["api_base"], "")
        self.assertIn("play.google.com", body["android_url"])

    @override_settings(APP_MIN_BUILD=3, APP_LATEST_BUILD=5, APP_IOS_URL="https://apps.apple.com/app/id1",
                       APP_API_BASE="https://coop-api.sijiliati.com/api/v1/", APP_API_HOSTS=["sijiliati.com"])
    def test_values_and_a_new_api_address(self):
        body = APIClient().get(URL).json()
        self.assertEqual((body["min_build"], body["latest_build"]), (3, 5))
        self.assertEqual(body["ios_url"], "https://apps.apple.com/app/id1")
        self.assertEqual(body["api_base"], "https://coop-api.sijiliati.com/api/v1")

    def test_a_bad_api_address_is_never_sent(self):
        for bad in ("http://coop-api.sijiliati.com/api/v1", "https://evil.example.com/api/v1",
                    "https://sijiliati.com.evil.io/api/v1"):
            with override_settings(APP_API_BASE=bad, APP_API_HOSTS=["sijiliati.com"]):
                self.assertEqual(APIClient().get(URL).json()["api_base"], "", bad)
