import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module


class AppValidationTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        app_module.LOGIN_ATTEMPTS.clear()
        with self.client.session_transaction() as login_session:
            login_session["authenticated_email"] = app_module.APP_EMAIL

    def test_api_requires_application_login(self):
        guest = app_module.app.test_client()

        response = guest.get("/api/health")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Inicia sesion para continuar")

    @patch("app.check_password_hash", return_value=True)
    def test_valid_login_opens_the_application(self, password_check):
        guest = app_module.app.test_client()

        response = guest.post(
            "/api/login",
            json={"email": app_module.APP_EMAIL, "password": "provided-securely"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(guest.get("/api/health").status_code, 200)
        password_check.assert_called_once()

    def test_health_has_local_security_headers(self):
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_favicon_is_served(self):
        response = self.client.get("/favicon.ico")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn(
                response.mimetype, {"image/x-icon", "image/vnd.microsoft.icon"}
            )
        finally:
            response.close()

        png_response = self.client.get("/favicon-32.png")
        try:
            self.assertEqual(png_response.status_code, 200)
            self.assertEqual(png_response.mimetype, "image/png")
        finally:
            png_response.close()

    def test_schedule_rejects_an_end_before_the_start(self):
        with self.assertRaisesRegex(ValueError, "fecha de fin"):
            app_module.schedule_dates("2026-06-19T10:00", "2026-06-18T10:00")

    def test_clean_hosts_rejects_private_ips_outside_the_fleet(self):
        with patch("app.fleet_hosts", return_value=["192.168.20.223"]):
            with self.assertRaisesRegex(ValueError, "IP no permitida"):
                app_module.clean_hosts(["192.168.20.224"])

    def test_response_errors_are_short_and_readable(self):
        message = app_module.response_error_message(
            {"raw": "<html><h1>Not found</h1></html>"}, 404
        )

        self.assertEqual(
            message, "La funcion solicitada no existe en esta version de Screenly"
        )

    @patch("app.detect_api")
    @patch("app.screenly_request")
    def test_v2_playlist_uses_the_supported_v2_endpoint(
        self, screenly_request, detect_api
    ):
        detect_api.return_value = {
            "ok": True,
            "version": "v2",
            "assets": [{"asset_id": "asset-1"}, {"asset_id": "asset-2"}],
        }
        screenly_request.return_value = {"ok": True, "status": 204, "data": {}}

        with patch("app.fleet_hosts", return_value=["192.168.20.223"]):
            response = self.client.post(
                "/api/order",
                json={
                    "hosts": ["192.168.20.223"],
                    "orderedIds": ["asset-2", "asset-1"],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(screenly_request.call_args.args[2], "/api/v2/assets/order")

    def test_monitor_config_supports_a_token_per_screen(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "monitor.json"
            config_path.write_text(
                json.dumps(
                    {
                        "token": "legacy",
                        "port": 9000,
                        "hosts": {"192.168.20.223": {"token": "specific"}},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(app_module, "MONITOR_CONFIG_PATH", config_path):
                specific = app_module.monitor_credentials("192.168.20.223")
                fallback = app_module.monitor_credentials("192.168.20.224")

        self.assertEqual(specific, {"token": "specific", "port": 9000})
        self.assertEqual(fallback, {"token": "legacy", "port": 9000})


if __name__ == "__main__":
    unittest.main()
