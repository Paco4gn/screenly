import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import app as app_module


class AppValidationTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        app_module.LOGIN_ATTEMPTS.clear()
        with self.client.session_transaction() as login_session:
            login_session["authenticated_email"] = app_module.APP_EMAIL
            login_session["role"] = "admin"

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

    @patch("app.check_password_hash", return_value=True)
    def test_stored_user_login_sets_its_role(self, password_check):
        guest = app_module.app.test_client()
        with tempfile.TemporaryDirectory() as directory:
            users_path = Path(directory) / "users.json"
            users_path.write_text(
                json.dumps(
                    {
                        "users": [
                            {
                                "email": "operador@feval.com",
                                "passwordHash": "hash",
                                "role": "operator",
                                "active": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(app_module, "USERS_PATH", users_path):
                response = guest.post(
                    "/api/login",
                    json={"email": "operador@feval.com", "password": "provided-securely"},
                )
                session_response = guest.get("/api/session")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["role"], "operator")
        self.assertEqual(session_response.get_json()["role"], "operator")
        password_check.assert_called_once()

    def test_health_has_local_security_headers(self):
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_agent_installer_zip_contains_portable_files(self):
        response = self.client.get("/api/agent-installer.zip")

        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(BytesIO(response.data)) as archive:
            names = set(archive.namelist())

        self.assertIn("INSTALAR_AGENTE.bat", names)
        self.assertIn("instalar_agente.py", names)
        self.assertIn("fleet_monitor_agent.py", names)
        self.assertIn("requirements-agent.txt", names)
        self.assertIn("INSTALAR_AGENTE.md", names)

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

    def test_monitor_token_endpoint_returns_server_token_for_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "monitor.json"
            config_path.write_text(json.dumps({"token": "global-token", "port": 8765}), encoding="utf-8")
            with patch.object(app_module, "MONITOR_CONFIG_PATH", config_path):
                response = self.client.post(
                    "/api/monitor-token",
                    json={"host": "192.168.20.230"},
                )
                repeated = self.client.post(
                    "/api/monitor-token",
                    json={"host": "192.168.20.230"},
                )
                stored = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["host"], "192.168.20.230")
        self.assertEqual(response.get_json()["port"], 8765)
        self.assertEqual(response.get_json()["token"], "global-token")
        self.assertEqual(response.get_json()["token"], repeated.get_json()["token"])
        self.assertNotIn("hosts", stored)

    def test_monitor_token_endpoint_keeps_existing_host_token(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "monitor.json"
            config_path.write_text(
                json.dumps(
                    {
                        "token": "global-token",
                        "port": 8765,
                        "hosts": {"192.168.20.230": {"token": "screen-token"}},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(app_module, "MONITOR_CONFIG_PATH", config_path):
                response = self.client.post(
                    "/api/monitor-token",
                    json={"host": "192.168.20.230"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["token"], "screen-token")

    def test_screen_credentials_are_used_but_not_exposed(self):
        fleet = [
            {
                "host": "192.168.20.223",
                "name": "Pantalla",
                "auth": {
                    "enabled": True,
                    "username": "screenly",
                    "password": "secret",
                    "apiVersion": "v1.2",
                },
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "fleet.json"
            config_path.write_text(json.dumps(fleet), encoding="utf-8")
            with patch.object(app_module, "CONFIG_PATH", config_path):
                response = self.client.get("/api/hosts")
                auth = app_module.auth_for_host("192.168.20.223", {})

        payload = response.get_json()["hosts"][0]
        self.assertEqual(auth, ("screenly", "secret"))
        self.assertTrue(payload["auth"]["hasPassword"])
        self.assertNotIn("password", payload["auth"])

    def test_default_screen_credentials_are_saved_and_used_without_exposure(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            fleet_path = Path(directory) / "fleet.json"
            fleet_path.write_text(
                json.dumps([{"host": "192.168.20.223", "name": "Pantalla"}]),
                encoding="utf-8",
            )
            with patch.object(app_module, "SETTINGS_PATH", settings_path):
                with patch.object(app_module, "CONFIG_PATH", fleet_path):
                    response = self.client.patch(
                        "/api/settings",
                        json={
                            "defaultAuth": {
                                "enabled": True,
                                "username": "screenly",
                                "password": "global-secret",
                                "apiVersion": "v1.2",
                            }
                        },
                    )
                    auth = app_module.auth_for_host("192.168.20.223", {})
                    api = app_module.api_preference_for_host("192.168.20.223", "auto")

        payload = response.get_json()["settings"]["defaultAuth"]
        self.assertEqual(auth, ("screenly", "global-secret"))
        self.assertEqual(api, "v1.2")
        self.assertTrue(payload["hasPassword"])
        self.assertNotIn("password", payload)

    def test_blank_manual_password_keeps_using_saved_global_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            fleet_path = Path(directory) / "fleet.json"
            fleet_path.write_text(
                json.dumps([{"host": "192.168.20.223", "name": "Pantalla"}]),
                encoding="utf-8",
            )
            settings_path.write_text(
                json.dumps(
                    {
                        "defaultAuth": {
                            "enabled": True,
                            "username": "screenly",
                            "password": "global-secret",
                            "apiVersion": "v1.2",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(app_module, "SETTINGS_PATH", settings_path):
                with patch.object(app_module, "CONFIG_PATH", fleet_path):
                    auth = app_module.auth_for_host(
                        "192.168.20.223",
                        {"username": "screenly", "password": ""},
                    )

        self.assertEqual(auth, ("screenly", "global-secret"))

    @patch("app.screenly_request")
    def test_asset_media_streams_visual_content_inline(self, screenly_request):
        screenly_request.return_value = {
            "ok": True,
            "status": 200,
            "data": {
                "type": "file",
                "filename": "cartel.png",
                "mimetype": "image/png",
                "content": "aGVsbG8=",
            },
        }

        response = self.client.get("/api/asset-media/192.168.20.223/asset-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertEqual(response.data, b"hello")

    def test_admin_can_manage_users_without_exposing_password_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            users_path = Path(directory) / "users.json"
            with patch.object(app_module, "USERS_PATH", users_path):
                response = self.client.post(
                    "/api/users",
                    json={
                        "email": "visor@feval.com",
                        "password": "password-segura",
                        "role": "viewer",
                    },
                )
                list_response = self.client.get("/api/users")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["user"]["role"], "viewer")
        payload = list_response.get_json()["users"]
        self.assertTrue(any(user["email"] == "visor@feval.com" for user in payload))
        self.assertFalse(any("passwordHash" in user or "password" in user for user in payload))

    def test_operator_cannot_manage_users(self):
        with self.client.session_transaction() as login_session:
            login_session["authenticated_email"] = "operador@feval.com"
            login_session["role"] = "operator"
        with tempfile.TemporaryDirectory() as directory:
            users_path = Path(directory) / "users.json"
            users_path.write_text(
                json.dumps(
                    {
                        "users": [
                            {
                                "email": "operador@feval.com",
                                "passwordHash": "hash",
                                "role": "operator",
                                "active": True,
                            },
                            {
                                "email": app_module.APP_EMAIL,
                                "passwordHash": app_module.APP_PASSWORD_HASH,
                                "role": "admin",
                                "active": True,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(app_module, "USERS_PATH", users_path):
                response = self.client.get("/api/users")

        self.assertEqual(response.status_code, 403)

    def test_cannot_delete_the_current_admin_user(self):
        with tempfile.TemporaryDirectory() as directory:
            users_path = Path(directory) / "users.json"
            users_path.write_text(
                json.dumps(
                    {
                        "users": [
                            {
                                "email": app_module.APP_EMAIL,
                                "passwordHash": app_module.APP_PASSWORD_HASH,
                                "role": "admin",
                                "active": True,
                            },
                            {
                                "email": "otro@feval.com",
                                "passwordHash": "hash",
                                "role": "admin",
                                "active": True,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(app_module, "USERS_PATH", users_path):
                response = self.client.delete(f"/api/users/{app_module.APP_EMAIL}")

        self.assertEqual(response.status_code, 400)

    @patch("app.detect_api")
    @patch("app.monitor_status", return_value={"ok": False, "connected": False, "reason": "unconfigured"})
    def test_diagnostics_marks_maintenance_screens(self, monitor_status, detect_api):
        detect_api.return_value = {
            "ok": False,
            "version": None,
            "assets": [],
            "error": "No se puede conectar con la Raspberry",
            "status": 0,
        }
        fleet = [
            {
                "host": "192.168.20.223",
                "name": "Pantalla",
                "maintenance": True,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "fleet.json"
            config_path.write_text(json.dumps(fleet), encoding="utf-8")
            with patch.object(app_module, "CONFIG_PATH", config_path):
                response = self.client.post(
                    "/api/diagnostics", json={"hosts": ["192.168.20.223"]}
                )

        result = response.get_json()["results"][0]
        self.assertEqual(result["severity"], "maintenance")
        self.assertEqual(result["status"], "mantenimiento")


if __name__ == "__main__":
    unittest.main()
