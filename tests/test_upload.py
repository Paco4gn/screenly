import io
import unittest
from unittest.mock import patch

import app as app_module

from app import normalize_uploaded_file


class NormalizeUploadedFileTests(unittest.TestCase):
    def test_old_screenly_returns_the_uri_as_a_json_string(self):
        result = normalize_uploaded_file(
            "/home/pi/screenly_assets/video.mp4",
            "video.mp4",
            "video/mp4",
        )

        self.assertEqual(result["uri"], "/home/pi/screenly_assets/video.mp4")
        self.assertEqual(result["mimetype"], "video")
        self.assertEqual(result["ext"], ".mp4")

    def test_anthias_returns_upload_metadata(self):
        result = normalize_uploaded_file(
            {
                "uri": "/data/screenly_assets/cartel.png",
                "mimetype": "image",
                "ext": ".png",
            },
            "cartel.png",
            "image/png",
        )

        self.assertEqual(result["uri"], "/data/screenly_assets/cartel.png")
        self.assertEqual(result["mimetype"], "image")
        self.assertEqual(result["ext"], ".png")

    @patch("app.detect_api")
    @patch("app.screenly_request")
    def test_upload_creates_an_asset_from_an_old_screenly_response(
        self, screenly_request, detect_api
    ):
        detect_api.return_value = {"ok": True, "version": "v1.2"}
        created_payload = {}

        def request_result(method, host, path, auth, **kwargs):
            if path == "/api/v1/file_asset":
                return {
                    "ok": True,
                    "status": 200,
                    "data": "/home/pi/screenly_assets/prueba.mp4",
                }
            created_payload.update(kwargs["json"])
            return {"ok": True, "status": 201, "data": {"asset_id": "asset-1"}}

        screenly_request.side_effect = request_result
        client = app_module.app.test_client()
        with client.session_transaction() as login_session:
            login_session["authenticated_email"] = app_module.APP_EMAIL
        response = client.post(
            "/api/upload",
            data={
                "hosts": "192.168.20.223",
                "video": (io.BytesIO(b"video-data"), "prueba.mp4", "video/mp4"),
                "enabled": "1",
                "skipAssetCheck": "1",
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["results"][0]["ok"])
        self.assertEqual(
            created_payload["uri"], "/home/pi/screenly_assets/prueba.mp4"
        )
        self.assertEqual(created_payload["mimetype"], "video")


if __name__ == "__main__":
    unittest.main()
