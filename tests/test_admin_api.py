import json
import pathlib
import tempfile
import unittest

import yaml
from fastapi.testclient import TestClient

from app.admin import AdminConfigValidationError, parse_admin_config_update, public_config
from app.audit import AuditLog
from app.config import DEFAULT_COMMANDS, load_config
from app.main import create_app
from app.manager import ControlManager
from app.setup import DEFAULT_TLS_CA_FILE, SetupStore


def full_status(**overrides):
    values = {
        "command": "push_status",
        "msg": 0,
        "gcode_state": "IDLE",
        "nozzle_temper": 25,
        "bed_temper": 25,
        "mc_percent": 0,
        "mc_remaining_time": 0,
        "layer_num": 0,
        "total_layer_num": 0,
        "spd_lvl": 2,
        "ams": {"ams": []},
        "lights_report": [{"node": "chamber_light", "mode": "off"}],
        "hms": [],
        "print_error": 0,
        "cooling_fan_speed": "0",
        "nozzle_target_temper": 0,
        "bed_target_temper": 0,
        "fun": 0,
    }
    values.update(overrides)
    return values


class FakeMqttClient:
    def __init__(self, config, store, on_state_change):
        self.config = config
        self.store = store
        self.on_state_change = on_state_change
        self.payloads = []

    def start(self):
        self.store.mark_connection(self.config.id, "online")

    def stop(self):
        self.store.mark_connection(self.config.id, "stopped")

    def publish(self, payload):
        self.payloads.append(payload)

    def publish_and_wait(self, payload, sequence_id, timeout=4.0):
        self.publish(payload)
        return {"sequence_id": sequence_id, "result": "success"}


class FailingMqttClient(FakeMqttClient):
    def start(self):
        raise RuntimeError("simulated replacement connection failure")


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = pathlib.Path(self.temp_dir.name)
        self.password_path = self.directory / "bambu-web-password"
        self.access_path = self.directory / "bambu-werkstatt-access-code"
        self.config_path = self.directory / "printers.yml"
        self.audit_path = self.directory / "audit.sqlite3"
        self.password_path.write_text("web-secret-12345\n", encoding="utf-8")
        self.access_path.write_text("printer-secret\n", encoding="utf-8")
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    "audit_db": str(self.audit_path),
                    "web": {
                        "username": "admin",
                        "password_file": str(self.password_path),
                        "allowed_origins": ["https://testserver"],
                    },
                    "printers": [
                        {
                            "id": "werkstatt",
                            "name": "Werkstatt",
                            "model": "X1 Carbon",
                            "host": "192.0.2.20",
                            "port": 8883,
                            "serial": "01S00A000000000",
                            "access_code_file": str(self.access_path),
                            "writable": True,
                            "camera_enabled": True,
                            "allowed_commands": sorted(DEFAULT_COMMANDS),
                            "allow_self_signed_tls": True,
                            "tls_fingerprint_sha256": "a" * 64,
                            "stale_after_seconds": 90,
                            "full_refresh_seconds": 300,
                        }
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.config = load_config(self.config_path)
        self.audit = AuditLog(str(self.audit_path))
        self.manager = ControlManager(
            self.config,
            self.audit,
            client_factory=FakeMqttClient,
        )
        self.fail_replacement = False

        def manager_factory(config, audit):
            client_factory = FailingMqttClient if self.fail_replacement else FakeMqttClient
            return ControlManager(config, audit, client_factory=client_factory)

        setup_store = SetupStore(
            self.config_path,
            audit_db=str(self.audit_path),
        )
        self.client_context = TestClient(
            create_app(
                self.config,
                self.manager,
                supplied_setup_store=setup_store,
                supplied_manager_factory=manager_factory,
            ),
            base_url="https://testserver",
        )
        self.client = self.client_context.__enter__()
        self.auth = ("admin", "web-secret-12345")
        self.manager.store.apply_report(
            "werkstatt",
            {
                "info": {
                    "command": "get_version",
                    "module": [
                        {
                            "name": "ota",
                            "sw_ver": "01.08.02.00",
                            "hw_ver": "AP05",
                            "sn": "firmware-secret",
                        },
                        {"name": "n3f/0", "sw_ver": "00.00.06.40"},
                    ],
                }
            },
        )
        self.manager.store.apply_report(
            "werkstatt",
            {
                "print": full_status(
                    wifi_signal="-54dBm",
                    home_flag=0x00800100,
                    stg_cur=4,
                    mc_print_stage=7,
                    mc_print_sub_stage=2,
                    hms=[{"attr": 0x07000001, "code": 0x00030001}],
                    ams={
                        "ams": [
                            {"id": "0", "dry_time": 0, "tray": []},
                        ]
                    },
                )
            },
        )

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def update_payload(self):
        printer = self.config.printers[0]
        return {
            "current_password": "web-secret-12345",
            "web": {
                "username": "admin",
                "allowed_origins": ["https://testserver"],
                "password": None,
            },
            "printers": [
                {
                    "id": printer.id,
                    "name": printer.name,
                    "model": printer.model,
                    "host": printer.host,
                    "port": printer.port,
                    "serial": printer.serial,
                    "writable": printer.writable,
                    "camera_enabled": printer.camera_enabled,
                    "allowed_commands": sorted(printer.allowed_commands),
                    "allow_self_signed_tls": printer.allow_self_signed_tls,
                    "tls_ca_file": printer.tls_ca_file,
                    "tls_fingerprint_sha256": printer.tls_fingerprint_sha256,
                    "stale_after_seconds": printer.stale_after_seconds,
                    "full_refresh_seconds": printer.full_refresh_seconds,
                    "access_code": None,
                }
            ],
        }

    def csrf_headers(self):
        response = self.client.get("/manage", auth=self.auth)
        self.assertEqual(response.status_code, 200)
        token = self.client.cookies.get("bambu_csrf")
        self.assertTrue(token)
        return {
            "Origin": "https://testserver",
            "X-CSRF-Token": token,
        }

    def put_config(self, payload=None, *, headers=None):
        return self.client.put(
            "/api/admin/config",
            auth=self.auth,
            headers=headers or self.csrf_headers(),
            json=payload or self.update_payload(),
        )

    def test_manage_page_and_admin_endpoints_require_authentication(self):
        page = self.client.get("/manage", follow_redirects=False)
        config = self.client.get("/api/admin/config")
        audit = self.client.get("/api/admin/audit")
        diagnostics = self.client.get("/api/admin/diagnostics")

        self.assertEqual(page.status_code, 303)
        self.assertEqual(page.headers["location"], "/login")
        self.assertEqual(config.status_code, 401)
        self.assertEqual(audit.status_code, 401)
        self.assertEqual(diagnostics.status_code, 401)

    def test_public_config_is_secret_free_and_preserves_operational_settings(self):
        response = self.client.get("/api/admin/config", auth=self.auth)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["web"]["password_configured"])
        self.assertTrue(payload["printers"][0]["access_code_configured"])
        self.assertEqual(payload["printers"][0]["host"], "192.0.2.20")
        encoded = json.dumps(payload)
        self.assertNotIn("web-secret-12345", encoded)
        self.assertNotIn("printer-secret", encoded)
        self.assertNotIn(str(self.password_path), encoded)
        self.assertNotIn(str(self.access_path), encoded)
        self.assertNotIn("password_file", encoded)
        self.assertNotIn("access_code_file", encoded)

    def test_update_requires_csrf_and_an_exact_allowed_origin(self):
        payload = self.update_payload()
        missing = self.client.put(
            "/api/admin/config",
            auth=self.auth,
            json=payload,
        )
        headers = self.csrf_headers()
        wrong_origin = self.client.put(
            "/api/admin/config",
            auth=self.auth,
            headers={**headers, "Origin": "https://evil.example"},
            json=payload,
        )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(wrong_origin.status_code, 403)

    def test_wrong_confirmation_password_is_rejected_without_ending_session(self):
        payload = self.update_payload()
        payload["current_password"] = "wrong-password"
        response = self.put_config(payload)

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("wrong-password", response.text)
        self.assertEqual(
            self.client.get("/api/admin/config", auth=self.auth).status_code,
            200,
        )

    def test_current_origin_cannot_be_removed_by_an_update(self):
        payload = self.update_payload()
        payload["web"]["allowed_origins"] = ["https://other.example"]
        response = self.put_config(payload)

        self.assertEqual(response.status_code, 422)
        self.assertIn("Origin", response.text)

    def test_validation_is_strict_and_never_echoes_submitted_secrets(self):
        payload = self.update_payload()
        payload["printers"][0]["allowed_commands"] = ["start_drying"]
        payload["printers"][0]["access_code"] = "submitted-printer-secret"
        payload["web"]["password"] = "submitted-web-password"
        payload["unexpected"] = "also-secret"
        response = self.put_config(payload)

        self.assertEqual(response.status_code, 422)
        for secret in (
            "submitted-printer-secret",
            "submitted-web-password",
            "also-secret",
        ):
            self.assertNotIn(secret, response.text)

    def test_new_identity_requires_a_new_access_code(self):
        payload = self.update_payload()
        payload["printers"][0]["serial"] = "01S00A000000999"
        response = self.put_config(payload)

        self.assertEqual(response.status_code, 422)
        self.assertIn("Zugangscode", response.text)

    def test_update_preserves_omitted_secrets_and_hot_reloads_runtime(self):
        payload = self.update_payload()
        payload["printers"][0]["name"] = "Neue Werkstatt"
        response = self.put_config(payload)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["applied"])
        self.assertTrue(body["runtime"]["manager_reloaded"])
        reloaded = load_config(self.config_path)
        self.assertEqual(reloaded.printers[0].name, "Neue Werkstatt")
        self.assertEqual(reloaded.web.password, "web-secret-12345")
        self.assertEqual(reloaded.printers[0].access_code, "printer-secret")
        self.assertIsNot(self.client.app.state.manager, self.manager)

    def test_self_signed_tls_toggle_selects_and_restores_the_ca_mode(self):
        secure = self.update_payload()
        secure["printers"][0]["allow_self_signed_tls"] = False
        secure["printers"][0]["tls_ca_file"] = None
        secure["printers"][0]["tls_fingerprint_sha256"] = None
        response = self.put_config(secure)
        self.assertEqual(response.status_code, 200, response.text)
        configured = load_config(self.config_path).printers[0]
        self.assertFalse(configured.allow_self_signed_tls)
        self.assertEqual(configured.tls_ca_file, DEFAULT_TLS_CA_FILE)

        insecure = self.update_payload()
        insecure["printers"][0]["allow_self_signed_tls"] = True
        insecure["printers"][0]["tls_ca_file"] = None
        response = self.put_config(insecure)
        self.assertEqual(response.status_code, 200, response.text)
        configured = load_config(self.config_path).printers[0]
        self.assertTrue(configured.allow_self_signed_tls)
        self.assertIsNone(configured.tls_ca_file)

        secure_again = self.update_payload()
        secure_again["printers"][0]["allow_self_signed_tls"] = False
        secure_again["printers"][0]["tls_ca_file"] = None
        secure_again["printers"][0]["tls_fingerprint_sha256"] = None
        response = self.put_config(secure_again)
        self.assertEqual(response.status_code, 200, response.text)
        configured = load_config(self.config_path).printers[0]
        self.assertFalse(configured.allow_self_signed_tls)
        self.assertEqual(configured.tls_ca_file, DEFAULT_TLS_CA_FILE)

    def test_secret_rotation_removes_obsolete_managed_files_after_commit(self):
        payload = self.update_payload()
        payload["web"]["password"] = "replacement-web-secret"
        payload["printers"][0]["access_code"] = "replacement-printer-secret"
        response = self.put_config(payload)

        self.assertEqual(response.status_code, 200, response.text)
        reloaded = load_config(self.config_path)
        self.assertEqual(reloaded.web.password, "replacement-web-secret")
        self.assertEqual(reloaded.printers[0].access_code, "replacement-printer-secret")
        self.assertFalse(self.password_path.exists())
        self.assertFalse(self.access_path.exists())
        self.assertNotIn("replacement-web-secret", response.text)
        self.assertNotIn("replacement-printer-secret", response.text)

    def test_failed_replacement_runtime_leaves_original_config_and_manager_active(self):
        baseline = self.config_path.read_bytes()
        payload = self.update_payload()
        payload["printers"][0]["name"] = "Must Not Commit"
        self.fail_replacement = True
        response = self.put_config(payload)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.config_path.read_bytes(), baseline)
        self.assertIs(self.client.app.state.manager, self.manager)
        self.assertEqual(
            self.client.get("/api/admin/config", auth=self.auth).status_code,
            200,
        )

    def test_update_enforces_content_type_and_bounded_body(self):
        headers = self.csrf_headers()
        wrong_type = self.client.put(
            "/api/admin/config",
            auth=self.auth,
            headers={**headers, "Content-Type": "text/plain"},
            content="{}",
        )
        oversized = self.client.put(
            "/api/admin/config",
            auth=self.auth,
            headers={**headers, "Content-Type": "application/json"},
            content=b" " * (64 * 1024 + 1),
        )

        self.assertEqual(wrong_type.status_code, 415)
        self.assertEqual(oversized.status_code, 413)

    def test_audit_history_filters_pages_and_returns_only_safe_metadata(self):
        for index, result in enumerate(("accepted", "rejected", "accepted")):
            self.audit.record(
                actor="admin",
                printer_id="werkstatt",
                command="light",
                params={"access_code": f"secret-{index}"},
                sequence_id=str(index),
                result=result,
                detail=f"private-detail-{index}",
            )

        first = self.client.get(
            "/api/admin/audit",
            auth=self.auth,
            params={"limit": 1, "result": "accepted"},
        )
        self.assertEqual(first.status_code, 200)
        page = first.json()
        self.assertEqual(len(page["items"]), 1)
        self.assertIsNotNone(page["next_cursor"])
        self.assertEqual(
            set(page["items"][0]),
            {"id", "created_at", "actor", "printer_id", "command", "result"},
        )
        second = self.client.get(
            "/api/admin/audit",
            auth=self.auth,
            params={
                "limit": 1,
                "result": "accepted",
                "cursor": page["next_cursor"],
            },
        )
        self.assertEqual(second.status_code, 200)
        combined = first.text + second.text
        self.assertNotIn("secret-", combined)
        self.assertNotIn("private-detail", combined)
        self.assertNotIn("sequence_id", combined)

    def test_audit_time_filters_require_timezone_and_order(self):
        missing_zone = self.client.get(
            "/api/admin/audit",
            auth=self.auth,
            params={"from": "2026-08-13T10:00:00"},
        )
        backwards = self.client.get(
            "/api/admin/audit",
            auth=self.auth,
            params={
                "from": "2026-08-14T00:00:00Z",
                "to": "2026-08-13T00:00:00Z",
            },
        )
        self.assertEqual(missing_zone.status_code, 422)
        self.assertEqual(backwards.status_code, 422)

    def test_diagnostics_are_useful_but_strictly_whitelisted(self):
        response = self.client.get("/api/admin/diagnostics", auth=self.auth)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        printer = payload["printers"][0]
        self.assertEqual(printer["device"]["wifi_signal_dbm"], -54)
        self.assertTrue(printer["device"]["door_open"])
        self.assertEqual(
            printer["device"]["firmware"]["printer"]["software"],
            "01.08.02.00",
        )
        self.assertEqual(printer["device"]["hms"]["count"], 1)
        encoded = json.dumps(payload)
        for private in (
            "firmware-secret",
            "web-secret-12345",
            "printer-secret",
            self.config.printers[0].serial,
            self.config.printers[0].host,
            str(self.config_path),
        ):
            self.assertNotIn(private, encoded)


class AdminInputTests(unittest.TestCase):
    def test_pydantic_validation_does_not_put_secret_values_in_public_exception(self):
        with self.assertRaises(AdminConfigValidationError) as caught:
            parse_admin_config_update(
                {
                    "current_password": "current-secret",
                    "web": {
                        "username": "admin",
                        "allowed_origins": ["http://unsafe.example"],
                        "password": "replacement-secret",
                    },
                    "printers": [],
                }
            )
        rendered = str(caught.exception)
        self.assertNotIn("current-secret", rendered)
        self.assertNotIn("replacement-secret", rendered)

    def test_public_config_helper_never_serialises_runtime_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = pathlib.Path(temp_dir)
            password = directory / "password"
            access = directory / "access"
            config_path = directory / "printers.yml"
            password.write_text("web-private-123\n", encoding="utf-8")
            access.write_text("printer-private\n", encoding="utf-8")
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "web": {
                            "username": "admin",
                            "password_file": str(password),
                            "allowed_origins": ["https://printer.example"],
                        },
                        "printers": [
                            {
                                "id": "x1c",
                                "host": "192.0.2.1",
                                "serial": "01S00A000000001",
                                "access_code_file": str(access),
                                "allow_self_signed_tls": True,
                                "tls_fingerprint_sha256": "a" * 64,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            encoded = json.dumps(public_config(load_config(config_path)))
            self.assertNotIn("web-private-123", encoded)
            self.assertNotIn("printer-private", encoded)
            self.assertNotIn(str(password), encoded)
            self.assertNotIn(str(access), encoded)


if __name__ == "__main__":
    unittest.main()
