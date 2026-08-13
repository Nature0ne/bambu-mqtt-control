import pathlib
import re
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
PYTHON_DIGEST = (
    "sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
)
CADDY_DIGEST = (
    "sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
)
ACTION_PIN = re.compile(r"^[\w-]+/[\w-]+(?:/[\w-]+)*@[0-9a-f]{40}(?:\s+#.*)?$")


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

    def test_compose_defaults_to_public_image_and_is_hardened(self):
        compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        compose = yaml.safe_load(compose_text)
        app = compose["services"]["bambu-control"]
        proxy = compose["services"]["proxy"]

        self.assertEqual(
            app["image"],
            "${BAMBU_IMAGE:-ghcr.io/nature0ne/bambu-mqtt-control:latest}",
        )
        self.assertEqual(app["pull_policy"], "${BAMBU_PULL_POLICY:-missing}")
        self.assertNotIn("build", app)
        self.assertNotIn("network_mode", app)
        self.assertNotIn("ports", app)
        self.assertEqual(app["expose"], ["9208"])
        self.assertTrue(app["read_only"])
        self.assertIn("ALL", app["cap_drop"])
        self.assertIn("no-new-privileges:true", app["security_opt"])
        self.assertEqual(app["logging"]["driver"], "local")
        self.assertEqual(app["logging"]["options"]["max-size"], "10m")
        self.assertEqual(app["logging"]["options"]["max-file"], "3")

        self.assertTrue(proxy["read_only"])
        self.assertEqual(proxy["cap_add"], ["NET_BIND_SERVICE"])
        self.assertIn("127.0.0.1", proxy["ports"][0])
        self.assertIn(CADDY_DIGEST, proxy["image"])
        self.assertEqual(proxy["logging"]["driver"], "local")

    def test_external_proxy_stack_excludes_bundled_caddy(self):
        external = yaml.safe_load(
            (ROOT / "deploy" / "compose.external-proxy.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(external["services"]), {"bambu-control"})
        service = external["services"]["bambu-control"]
        self.assertEqual(service["extends"]["service"], "bambu-control")
        self.assertEqual(service["extends"]["file"], "../compose.yaml")
        self.assertIn("127.0.0.1", service["ports"][0])

    def test_health_checks_are_internal_and_match_the_expected_body(self):
        compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        compose = yaml.safe_load(compose_text)
        proxy = compose["services"]["proxy"]
        health_command = " ".join(proxy["healthcheck"]["test"])

        self.assertIn("http://127.0.0.1:2018/healthz", health_command)
        self.assertIn('"status":"ok"', health_command)
        self.assertNotIn("--no-check-certificate", health_command)
        self.assertNotIn("2018", " ".join(proxy.get("ports", [])))
        self.assertNotIn("2018", " ".join(proxy.get("expose", [])))

        caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:2018", caddy)
        self.assertIn("handle /healthz", caddy)
        self.assertIn("respond @metrics 404", caddy)
        self.assertNotIn("flush_interval -1", caddy)

    def test_local_build_override_and_container_metadata(self):
        override = yaml.safe_load(
            (ROOT / "deploy" / "compose.build.yaml").read_text(encoding="utf-8")
        )
        app = override["services"]["bambu-control"]
        self.assertIn("build", app)
        self.assertEqual(app["pull_policy"], "build")

        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertTrue(
            dockerfile.startswith(f"FROM python:3.12-alpine@{PYTHON_DIGEST}\n")
        )
        self.assertNotIn("ARG PYTHON_IMAGE", dockerfile)
        for argument in ("VERSION", "VCS_REF", "BUILD_DATE"):
            self.assertIn(f"ARG {argument}=", dockerfile)
        self.assertIn("org.opencontainers.image.source", dockerfile)
        self.assertIn("org.opencontainers.image.revision", dockerfile)
        self.assertIn("BAMBU_CONTROL_VERSION=${VERSION}", dockerfile)
        self.assertIn("LICENSE THIRD_PARTY_NOTICES.md /licenses/", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn('"--host", "0.0.0.0"', dockerfile)

    def test_release_workflows_are_sha_pinned_and_multi_platform(self):
        workflow_directory = ROOT / ".github" / "workflows"
        expected = {"ci.yml", "container.yml", "codeql.yml"}
        self.assertTrue(expected.issubset({path.name for path in workflow_directory.iterdir()}))
        for path in workflow_directory.glob("*.yml"):
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertIsInstance(workflow, dict, str(path))
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("uses:"):
                    use = stripped.removeprefix("uses:").strip()
                    self.assertRegex(use, ACTION_PIN, f"unpinned action in {path}: {use}")

        publish = (workflow_directory / "container.yml").read_text(encoding="utf-8")
        self.assertIn("linux/amd64,linux/arm64", publish)
        self.assertIn("ghcr.io/nature0ne/bambu-mqtt-control", publish)
        self.assertIn("sbom: true", publish)
        self.assertIn("provenance: mode=max", publish)
        self.assertIn("attest-build-provenance@", publish)

    def test_ci_checks_all_browser_scripts_and_both_compose_variants(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        for script in ("app.js", "login.js", "manage.js", "setup.js"):
            self.assertIn(f"node --check app/static/{script}", ci)
        self.assertIn("deploy/compose.build.yaml", ci)
        self.assertIn("deploy/compose.external-proxy.yaml", ci)
        self.assertIn("sh -n", ci)

    def test_backup_scripts_preserve_the_selected_compose_model(self):
        for name in ("backup", "restore"):
            script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("COMPOSE_FILE", script)
            self.assertIn("BAMBU_COMPOSE_FILES", script)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("BAMBU_COMPOSE_FILES=deploy/compose.external-proxy.yaml", readme)
        self.assertIn(
            "COMPOSE_FILE=deploy/compose.external-proxy.yaml docker compose up -d --remove-orphans",
            readme,
        )
        self.assertNotIn(
            "docker compose -f deploy/compose.external-proxy.yaml up",
            readme,
        )
        self.assertIn(
            "BAMBU_COMPOSE_FILES=compose.yaml:deploy/compose.build.yaml", readme
        )


if __name__ == "__main__":
    unittest.main()
