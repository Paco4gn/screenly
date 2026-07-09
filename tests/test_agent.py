import tempfile
import unittest
from pathlib import Path

from fleet_monitor_agent import detect_media_type


class AgentTests(unittest.TestCase):
    def test_detects_mp4_without_a_filename_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "asset-id"
            media.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00")

            self.assertEqual(detect_media_type(str(media)), "video/mp4")

    def test_detects_png_without_a_filename_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "asset-id"
            media.write_bytes(b"\x89PNG\r\n\x1a\ncontent")

            self.assertEqual(detect_media_type(str(media)), "image/png")


if __name__ == "__main__":
    unittest.main()
