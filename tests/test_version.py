import os
import unittest
from unittest.mock import patch

from app.version import build_version


class BuildVersionTests(unittest.TestCase):
    def test_defaults_to_dev_when_environment_is_absent(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(build_version(), "dev")

    def test_accepts_short_display_safe_release_identifiers(self):
        for value in ("0.2.0", "v0.2.0-rc.1", "main+abc123", "edge_20260813"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"BAMBU_CONTROL_VERSION": value}, clear=True
            ):
                self.assertEqual(build_version(), value)

    def test_rejects_untrusted_or_oversized_build_identifiers(self):
        for value in ("", "v1<script>", "a" * 65, "line\nbreak"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"BAMBU_CONTROL_VERSION": value}, clear=True
            ):
                self.assertEqual(build_version(), "dev")

    def test_trims_environment_whitespace_before_validation(self):
        with patch.dict(
            os.environ, {"BAMBU_CONTROL_VERSION": " v0.2.0 "}, clear=True
        ):
            self.assertEqual(build_version(), "v0.2.0")


if __name__ == "__main__":
    unittest.main()
