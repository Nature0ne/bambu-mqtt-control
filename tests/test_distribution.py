import pathlib
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]


class StandaloneDistributionTests(unittest.TestCase):
    def test_distribution_contains_no_monorepo_or_private_infrastructure_references(self):
        forbidden = (
            "natureone.ddns.net",
            "/Users/natureone",
            "/etc/pi-ops",
            "TelegramBotPi",
            "telegrambotpi/",
            "nut-observability",
            "192.168.12.2",
            "ssh pihole",
        )
        text_suffixes = {
            ".css",
            ".html",
            ".js",
            ".md",
            ".py",
            ".sh",
            ".toml",
            ".txt",
            ".yaml",
            ".yml",
        }
        checked = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in text_suffixes:
                continue
            if path.resolve() == pathlib.Path(__file__).resolve():
                continue
            content = path.read_text(encoding="utf-8")
            checked.append(path)
            for value in forbidden:
                self.assertNotIn(value, content, f"{value!r} leaked into {path}")
            self.assertNotIn("BEGIN PRIVATE KEY", content, str(path))
        self.assertTrue(checked)

    def test_compose_is_bridge_based_hardened_and_keeps_metrics_private(self):
        compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        compose = yaml.safe_load(compose_text)
        app = compose["services"]["bambu-control"]
        proxy = compose["services"]["proxy"]

        self.assertNotIn("network_mode", app)
        self.assertNotIn("ports", app)
        self.assertEqual(app["expose"], ["9208"])
        self.assertTrue(app["read_only"])
        self.assertIn("ALL", app["cap_drop"])
        self.assertIn("no-new-privileges:true", app["security_opt"])
        self.assertTrue(proxy["read_only"])
        self.assertEqual(proxy["cap_add"], ["NET_BIND_SERVICE"])
        self.assertIn("127.0.0.1", proxy["ports"][0])

        caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("respond @metrics 404", caddy)
        self.assertNotIn("flush_interval -1", caddy)

    def test_container_is_non_root_and_bridge_reachable(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn('"--host", "0.0.0.0"', dockerfile)
        self.assertIn("/config", dockerfile)


if __name__ == "__main__":
    unittest.main()
