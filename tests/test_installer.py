import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import instalar_agente as installer


class InstallerTests(unittest.TestCase):
    def test_each_new_screen_receives_a_different_token(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "monitor.json"
            with patch.object(installer, "MONITOR_CONFIG", config):
                first = installer.load_or_create_token("192.168.20.223")
                second = installer.load_or_create_token("192.168.20.224")
                repeated = installer.load_or_create_token("192.168.20.223")
                stored = json.loads(config.read_text(encoding="utf-8"))

        self.assertNotEqual(first, second)
        self.assertEqual(first, repeated)
        self.assertEqual(stored["hosts"]["192.168.20.223"]["token"], first)


if __name__ == "__main__":
    unittest.main()
