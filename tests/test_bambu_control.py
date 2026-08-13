import asyncio
import base64
import json
import os
import pathlib
import sqlite3
import ssl
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock

import yaml

SERVICE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

from app.audit import AuditLog
from app.camera import (
    CAMERA_BOUNDARY,
    CameraBusy,
    CameraLimits,
    CameraStreamManager,
    CameraTicketRejected,
    CameraUnavailable,
    camera_input_url,
    camera_tls_supported,
)
from app.commands import (
    CommandBuilder,
    CommandError,
    get_version_payload,
    push_all_payload,
)
from app.config import (
    DEFAULT_COMMANDS,
    KNOWN_COMMANDS,
    AppConfig,
    ConfigError,
    PrinterConfig,
    WebConfig,
    load_config,
)
from app.main import create_app
from app.manager import CommandForbidden, CommandUnavailable, ControlManager
from app.metrics import render_metrics
from app.mqtt_client import PrinterMqttClient, _pinned_tls_context
from app.setup import (
    SetupRateLimiter,
    SetupStore,
    SetupValidationError,
    parse_setup_request,
)
from app.state import StateStore, deep_merge, normalise_report
from app.web import LoginRateLimiter, SessionStore
from fastapi.testclient import TestClient


def printer_config(**overrides):
    values = {
        "id": "werkstatt",
        "name": "Werkstatt",
        "host": "192.168.1.50",
        "serial": "01P00A000000000",
        "access_code": "secret-code",
        "model": "P1S",
        "writable": True,
        "allowed_commands": frozenset(
            {
                "pause",
                "resume",
                "stop",
                "speed",
                "light",
                "refresh_rfid",
            }
        ),
        "allow_self_signed_tls": True,
    }
    values.update(overrides)
    return PrinterConfig(**values)


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
    instances: ClassVar[dict[str, "FakeMqttClient"]] = {}

    def __init__(self, config, store, on_state_change):
        self.config = config
        self.store = store
        self.on_state_change = on_state_change
        self.payloads = []
        self.instances[config.id] = self

    def start(self):
        self.store.mark_connection(self.config.id, "online")

    def stop(self):
        self.store.mark_connection(self.config.id, "stopped")

    def publish(self, payload):
        self.payloads.append(payload)

    def publish_and_wait(self, payload, sequence_id, timeout=4.0):
        self.publish(payload)
        return {"sequence_id": sequence_id, "result": "success"}


class FakeCameraReader:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, _size):
        await asyncio.sleep(0)
        return self.chunks.pop(0) if self.chunks else b""


class FakeCameraProcess:
    def __init__(self, chunks):
        self.stdout = FakeCameraReader(chunks)
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        await asyncio.sleep(0)
        return self.returncode


class BlockingCameraProcess(FakeCameraProcess):
    def __init__(self, chunks):
        super().__init__(chunks)
        self.wait_started = asyncio.Event()
        self.wait_allowed = asyncio.Event()
        self.terminate_calls = 0

    def terminate(self):
        self.terminate_calls += 1
        super().terminate()

    async def wait(self):
        self.wait_started.set()
        await self.wait_allowed.wait()
        return self.returncode


class FakeCameraProcessFactory:
    def __init__(self, launches=None):
        self.chunk_sets = list(launches or [[b"--bambu_frame\r\n", b""]])
        self.commands = []
        self.launch_payloads = []
        self.processes = []

    async def __call__(self, *command, **options):
        launch_fd = options["pass_fds"][0]
        encoded = os.read(launch_fd, 4096)
        self.commands.append((command, options))
        self.launch_payloads.append(json.loads(encoded))
        chunks = self.chunk_sets.pop(0) if self.chunk_sets else [b""]
        process = FakeCameraProcess(chunks)
        self.processes.append(process)
        return process


class SessionStoreTests(unittest.TestCase):
    def test_session_expires_can_be_revoked_and_does_not_survive_a_new_store(self):
        now = [100.0]
        store = SessionStore(
            ttl_seconds=30,
            clock=lambda: now[0],
            token_factory=lambda: "a" * 32,
        )

        token = store.issue("admin")
        self.assertEqual(store.authenticate(token), "admin")
        self.assertIsNone(SessionStore().authenticate(token))

        store.revoke(token)
        self.assertIsNone(store.authenticate(token))

        token = store.issue("admin")
        now[0] += 31
        self.assertIsNone(store.authenticate(token))

    def test_session_store_is_safe_for_concurrent_issue_and_authentication(self):
        store = SessionStore()
        tokens = []
        output_lock = threading.Lock()

        def issue_and_check(index):
            token = store.issue(f"actor-{index}")
            self.assertEqual(store.authenticate(token), f"actor-{index}")
            with output_lock:
                tokens.append(token)

        threads = [threading.Thread(target=issue_and_check, args=(index,)) for index in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(tokens), len(set(tokens)))
        self.assertEqual(len(tokens), len(threads))


class ConfigTests(unittest.TestCase):
    def test_loads_secrets_from_files_without_exposing_them_in_repr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            access = tmp_path / "access"
            password = tmp_path / "password"
            access.write_text("access-123\n")
            password.write_text("web-password-123\n")
            config_path = tmp_path / "printers.yml"
            config_path.write_text(
                f"""
web:
  username: admin
  password_file: {password}
  allowed_origins:
    - https://printer.test
printers:
  - id: p1s
    host: 192.168.1.51
    serial: 01P00A000000001
    access_code_file: {access}
    allow_self_signed_tls: true
    tls_fingerprint_sha256: {"a" * 64}
"""
            )

            config = load_config(config_path)

            self.assertEqual(config.printers[0].access_code, "access-123")
            self.assertFalse(config.printers[0].camera_enabled)
            self.assertNotIn("access-123", repr(config.printers[0]))
            self.assertNotIn("web-password-123", repr(config.web))

    def test_rejects_quoted_booleans_that_could_enable_writes_or_insecure_tls(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            access = tmp_path / "access"
            password = tmp_path / "password"
            access.write_text("access-123\n")
            password.write_text("web-password-123\n")
            config_path = tmp_path / "printers.yml"
            config_path.write_text(
                f"""
web:
  username: admin
  password_file: {password}
  allowed_origins: [https://printer.test]
printers:
  - id: p1s
    host: 192.168.1.51
    serial: 01P00A000000001
    access_code_file: {access}
    writable: "false"
    allow_self_signed_tls: "true"
"""
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_requires_an_explicit_tls_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            access = tmp_path / "access"
            password = tmp_path / "password"
            access.write_text("access")
            password.write_text("password")
            config_path = tmp_path / "printers.yml"
            config_path.write_text(
                f"""
web:
  username: admin
  password_file: {password}
  allowed_origins:
    - https://printer.test
printers:
  - id: p1s
    host: 192.168.1.51
    serial: 01P00A000000001
    access_code_file: {access}
"""
            )

            with self.assertRaisesRegex(ConfigError, "tls_ca_file"):
                load_config(config_path)

    def test_onboarding_accepts_drying_only_as_an_explicit_permission(self):
        request = {
            "bootstrap_token": "bootstrap-token-for-tests-123456",
            "web": {
                "username": "admin",
                "password": "web-password-123",
            },
            "printers": [
                {
                    "id": "x1c",
                    "name": "X1C",
                    "model": "X1 Carbon",
                    "host": "192.168.1.50",
                    "serial": "01P00A000000009",
                    "access_code": "access-123",
                    "writable": True,
                    "allowed_commands": ["start_drying", "stop_drying"],
                }
            ],
        }
        setup = parse_setup_request(request)

        self.assertEqual(
            setup.printers[0].allowed_commands,
            ["start_drying", "stop_drying"],
        )

        request["printers"][0]["allowed_commands"] = ["stop_drying"]
        stop_only = parse_setup_request(request)
        self.assertEqual(stop_only.printers[0].allowed_commands, ["stop_drying"])

        request["printers"][0]["allowed_commands"] = ["start_drying"]
        with self.assertRaises(SetupValidationError):
            parse_setup_request(request)

    def test_config_rejects_start_drying_without_stop_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            access = tmp_path / "access"
            password = tmp_path / "password"
            access.write_text("access-123\n")
            password.write_text("web-password-123\n")
            config_path = tmp_path / "printers.yml"
            config_path.write_text(
                f"""
web:
  username: admin
  password_file: {password}
  allowed_origins: [https://printer.test]
printers:
  - id: x1c
    host: 192.168.1.51
    serial: 01P00A000000001
    access_code_file: {access}
    writable: true
    allowed_commands: [start_drying]
    allow_self_signed_tls: true
"""
            )

            with self.assertRaisesRegex(ConfigError, "requires stop_drying"):
                load_config(config_path)


class SetupApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = pathlib.Path(self.temp_dir.name)
        self.config_path = self.directory / "printers.yml"
        self.token_path = self.directory / "bootstrap-token"
        self.bootstrap_token = "bootstrap-token-for-tests-123456"
        self.store = SetupStore(
            self.config_path,
            token_path=self.token_path,
            audit_db=str(self.directory / "audit.sqlite3"),
            token_factory=lambda: self.bootstrap_token,
        )
        self.manager_instances = []

        def manager_factory(config, audit):
            manager = ControlManager(config, audit, client_factory=FakeMqttClient)
            self.manager_instances.append(manager)
            return manager

        self.client_context = TestClient(
            create_app(
                supplied_setup_store=self.store,
                supplied_manager_factory=manager_factory,
            ),
            base_url="https://testserver",
        )
        self.client = self.client_context.__enter__()

    def tearDown(self):
        if self.client_context is not None:
            self.client_context.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def payload(self, **overrides):
        payload = {
            "bootstrap_token": self.bootstrap_token,
            "web": {
                "username": "admin",
                "password": "new-web-password-123",
            },
            "printers": [
                {
                    "id": "werkstatt",
                    "name": "Werkstatt",
                    "model": "P1S",
                    "host": "192.168.1.50",
                    "port": 8883,
                    "serial": "01P00A000000000",
                    "access_code": "lan-access-123",
                    "writable": False,
                }
            ],
        }
        payload.update(overrides)
        return payload

    def test_setup_mode_health_metrics_and_status_do_not_need_a_manager(self):
        health = self.client.get("/healthz")
        metrics = self.client.get("/metrics")
        status_response = self.client.get("/api/setup/status")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assertFalse(health.json()["configured"])
        self.assertEqual(health.json()["mode"], "setup_required")
        self.assertIn("bambu_setup_required 1", metrics.text)
        self.assertEqual(
            status_response.json(),
            {"configured": False, "setup_required": True},
        )
        self.assertEqual(self.client.get("/").status_code, 200)
        root_redirect = self.client.get("/", follow_redirects=False)
        setup_page = self.client.get("/setup")
        self.assertEqual(root_redirect.status_code, 307)
        self.assertEqual(root_redirect.headers["location"], "/setup")
        login_redirect = self.client.get("/login", follow_redirects=False)
        self.assertEqual(login_redirect.status_code, 307)
        self.assertEqual(login_redirect.headers["location"], "/setup")
        self.assertEqual(setup_page.headers["cache-control"], "no-store")
        self.assertIn("default-src 'self'", setup_page.headers["content-security-policy"])
        self.assertEqual(self.client.get("/api/printers").status_code, 503)
        self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)

    def test_successful_setup_commits_private_files_last_and_starts_manager(self):
        response = self.client.post(
            "/api/setup",
            headers={"Origin": "https://testserver"},
            json=self.payload(),
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "configured": True,
                "setup_required": False,
                "printer_count": 1,
            },
        )
        self.assertEqual(len(self.manager_instances), 1)
        self.assertFalse(self.token_path.exists())
        config_text = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn("new-web-password-123", config_text)
        self.assertNotIn("lan-access-123", config_text)
        self.assertIn("https://testserver", config_text)
        self.assertIn("/app/certs/bambu-lab-ca.pem", config_text)
        for name in ("printers.yml", "bambu-web-password", "bambu-werkstatt-access-code"):
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600)

        loaded = load_config(self.config_path)
        self.assertEqual(loaded.web.password, "new-web-password-123")
        self.assertEqual(loaded.printers[0].access_code, "lan-access-123")
        self.assertEqual(
            self.client.get("/api/setup/status").json(),
            {"configured": True, "setup_required": False},
        )
        self.assertEqual(
            self.client.get(
                "/api/printers", auth=("admin", "new-web-password-123")
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/api/setup",
                headers={"Origin": "https://testserver"},
                json=self.payload(),
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.get("/setup", follow_redirects=False).headers["location"],
            "/",
        )

    def test_setup_rejects_non_https_origin_without_committing(self):
        response = self.client.post(
            "/api/setup",
            headers={"Origin": "http://testserver"},
            json=self.payload(),
        )

        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.config_path.exists())
        self.assertTrue(self.token_path.exists())

    def test_setup_rejects_non_json_requests_without_committing(self):
        response = self.client.post(
            "/api/setup",
            headers={"Origin": "https://testserver"},
            content="bootstrap_token=not-json",
        )

        self.assertEqual(response.status_code, 415)
        self.assertFalse(self.config_path.exists())
        self.assertTrue(self.token_path.exists())

    def test_setup_rejects_secrets_that_would_be_silently_trimmed(self):
        payload = self.payload()
        payload["web"]["password"] = " new-web-password-123"
        payload["printers"][0]["access_code"] = "lan-access-123 "

        response = self.client.post(
            "/api/setup",
            headers={"Origin": "https://testserver"},
            json=payload,
        )

        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.config_path.exists())
        self.assertNotIn("new-web-password-123", response.text)
        self.assertNotIn("lan-access-123", response.text)

    def test_validation_response_never_echoes_submitted_secrets(self):
        payload = self.payload(unexpected="lan-access-123")
        response = self.client.post(
            "/api/setup",
            headers={"Origin": "https://testserver"},
            json=payload,
        )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(self.bootstrap_token, response.text)
        self.assertNotIn("new-web-password-123", response.text)
        self.assertNotIn("lan-access-123", response.text)

    def test_setup_rejects_start_only_drying_without_echoing_secrets(self):
        payload = self.payload()
        payload["printers"][0]["writable"] = True
        payload["printers"][0]["allowed_commands"] = ["start_drying"]

        response = self.client.post(
            "/api/setup",
            headers={"Origin": "https://testserver"},
            json=payload,
        )

        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.config_path.exists())
        self.assertNotIn(self.bootstrap_token, response.text)
        self.assertNotIn("new-web-password-123", response.text)
        self.assertNotIn("lan-access-123", response.text)

    def test_setup_accepts_stop_only_as_a_safety_permission(self):
        payload = self.payload()
        payload["printers"][0]["writable"] = True
        payload["printers"][0]["allowed_commands"] = ["stop_drying"]

        response = self.client.post(
            "/api/setup",
            headers={"Origin": "https://testserver"},
            json=payload,
        )

        self.assertEqual(response.status_code, 201, response.text)
        loaded = load_config(self.config_path)
        self.assertEqual(loaded.printers[0].allowed_commands, {"stop_drying"})

    def test_setup_persists_explicit_camera_opt_in(self):
        payload = self.payload()
        payload["printers"][0]["camera_enabled"] = True

        response = self.client.post(
            "/api/setup",
            headers={"Origin": "https://testserver"},
            json=payload,
        )

        self.assertEqual(response.status_code, 201, response.text)
        loaded = load_config(self.config_path)
        self.assertTrue(loaded.printers[0].camera_enabled)

    def test_setup_attempts_are_rate_limited(self):
        self.client_context.__exit__(None, None, None)
        self.client_context = None
        limiter = SetupRateLimiter(max_attempts=2, window_seconds=60)
        self.client_context = TestClient(
            create_app(
                supplied_setup_store=self.store,
                supplied_setup_rate_limiter=limiter,
            ),
            base_url="https://testserver",
        )
        self.client = self.client_context.__enter__()
        payload = self.payload(bootstrap_token="wrong-bootstrap-token-value")
        first = self.client.post(
            "/api/setup", headers={"Origin": "https://testserver"}, json=payload
        )
        second = self.client.post(
            "/api/setup", headers={"Origin": "https://testserver"}, json=payload
        )
        third = self.client.post(
            "/api/setup",
            headers={"Origin": "https://testserver"},
            json=self.payload(),
        )

        self.assertEqual(first.status_code, 401)
        self.assertEqual(second.status_code, 401)
        self.assertEqual(third.status_code, 429)
        self.assertEqual(third.headers["retry-after"], "60")
        self.assertFalse(self.config_path.exists())

    def test_existing_invalid_configuration_is_not_treated_as_first_run(self):
        self.client_context.__exit__(None, None, None)
        self.client_context = None
        self.config_path.write_text("not: a usable config\n", encoding="utf-8")
        with self.assertRaises(ConfigError), TestClient(
            create_app(supplied_setup_store=self.store),
            base_url="https://testserver",
        ):
            pass


class StateTests(unittest.TestCase):
    def test_deep_merge_preserves_unchanged_delta_fields(self):
        target = {"print": {"mc_percent": 10, "temps": {"bed": 50, "nozzle": 210}}}
        deep_merge(target, {"print": {"mc_percent": 11, "temps": {"bed": 51}}})
        self.assertEqual(
            target,
            {"print": {"mc_percent": 11, "temps": {"bed": 51, "nozzle": 210}}},
        )

    def test_normalises_printer_and_multiple_ams_units(self):
        report = {
            "print": {
                "gcode_state": "RUNNING",
                "mc_percent": 42,
                "mc_remaining_time": 18,
                "nozzle_temper": 219.5,
                "nozzle_target_temper": 220,
                "cooling_fan_speed": "15",
                "ams": {
                    "tray_now": "5",
                    "ams": [
                        {"id": "0", "humidity": "3", "tray": [{"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 80}]},
                        {"id": "1", "temp": "24.5", "tray": [{"id": "1", "tray_type": "PETG", "tray_color": "00FF00FF", "remain": 33}]},
                    ],
                },
            }
        }

        state = normalise_report(report)

        self.assertEqual(state["status"], "running")
        self.assertEqual(state["fans"]["part"], 100)
        self.assertEqual(len(state["ams"]["units"]), 2)
        self.assertEqual(state["active_tray"], 5)
        self.assertTrue(state["ams"]["units"][1]["slots"][0]["active"])
        self.assertEqual(state["ams"]["units"][0]["slots"][0]["color"], "#FF0000")
        self.assertIsNone(
            state["ams"]["units"][0]["slots"][0]["remaining_percent"]
        )

    def test_normalises_camera_capabilities_without_exposing_a_stream_url(self):
        state = normalise_report(
            {
                "print": {
                    "ipcam": {
                        "ipcam_dev": "1",
                        "ipcam_record": "enable",
                        "timelapse": "disable",
                        "resolution": "1080p",
                        "resolution_supported": ["720p", "1080p", "1080p"],
                        "liveview": {"local": "rtsps", "remote": "none"},
                        "rtsp_url": "rtsps://secret:must-not-leak@printer/live",
                    }
                }
            }
        )

        self.assertEqual(
            state["camera"],
            {
                "available": True,
                "recording": True,
                "timelapse": False,
                "resolution": "1080p",
                "resolution_supported": ["720p", "1080p"],
                "local_protocol": "rtsps",
            },
        )
        self.assertNotIn("rtsp_url", state["camera"])
        self.assertNotIn("must-not-leak", repr(state))

    def test_normalises_an_active_external_spool(self):
        state = normalise_report(
            {
                "print": {
                    "ams": {"tray_now": "254", "ams": []},
                    "vt_tray": {
                        "id": "254",
                        "tray_type": "TPU",
                        "tray_color": "112233FF",
                        "tag_uid": "0000000000000000",
                        "remain": 100,
                    },
                }
            }
        )

        spool = state["ams"]["external_spool"]
        self.assertEqual(state["active_tray"], 254)
        self.assertTrue(spool["active"])
        self.assertEqual(spool["material"], "TPU")
        self.assertEqual(spool["rfid_state"], "unknown")
        self.assertIsNone(spool["remaining_percent"])

    def test_marks_old_reports_stale_and_offline(self):
        now = [1000.0]
        config = printer_config(stale_after_seconds=30)
        store = StateStore((config,), clock=lambda: now[0])
        store.mark_connection(config.id, "online")
        store.apply_report(
            config.id,
            {"print": full_status()},
        )
        self.assertTrue(store.snapshot()["printers"][0]["online"])

        now[0] += 31
        snapshot = store.snapshot()["printers"][0]
        self.assertTrue(snapshot["stale"])
        self.assertFalse(snapshot["online"])

    def test_parses_raw_ams_humidity_log_without_losing_pushall_data(self):
        config = printer_config()
        store = StateStore((config,))
        store.apply_report(
            config.id,
            {
                "print": full_status(
                    ams={"ams": [{"id": "0", "humidity": "4", "temp": "20"}]},
                ),
                "mc_print": {
                    "command": "push_info",
                    "param": "[AMS][TASK]ams0 temp:18.4;humidity:30%;humidity_idx:3",
                },
            },
        )
        unit = store.snapshot()["printers"][0]["state"]["ams"]["units"][0]
        self.assertEqual(unit["humidity_percent"], 30)
        self.assertEqual(unit["humidity_index"], 3)
        self.assertEqual(unit["temperature"], 18.4)

    def test_maps_version_modules_and_normalises_drying_state(self):
        config = printer_config(model="X1 Carbon")
        store = StateStore((config,))
        store.apply_report(
            config.id,
            {
                "info": {
                    "command": "get_version",
                    "module": [
                        {"name": "n3f/0", "sn": "must-not-be-exposed"},
                        {"name": "n3s/0", "sn": "must-not-be-exposed"},
                        {"name": "ams/1", "sn": "must-not-be-exposed"},
                    ],
                }
            },
        )
        store.apply_report(
            config.id,
            {
                "print": full_status(
                    fun=0,
                    ams={
                        "tray_now": "255",
                        "ams": [
                            {
                                "id": "0",
                                "dry_time": "0",
                                "dry_setting": {
                                    "dry_temperature": "60",
                                    "dry_duration": "8",
                                    "dry_filament": "PETG",
                                },
                                "tray": [{"id": "0"}] * 4,
                            },
                            {
                                "id": "128",
                                "dry_time": "119",
                                "dry_setting": {
                                    "dry_temperature": "70",
                                    "dry_duration": "2",
                                    "dry_filament": "PA-CF",
                                },
                                "tray": [{"id": "0"}],
                            },
                            {"id": "1", "dry_time": "0", "tray": []},
                        ],
                    },
                )
            },
        )

        snapshot = store.snapshot()["printers"][0]
        units = {unit["id"]: unit for unit in snapshot["state"]["ams"]["units"]}
        self.assertTrue(snapshot["state"]["developer_lan_mode"])
        self.assertEqual(units[0]["model"], "AMS 2 Pro")
        self.assertEqual(units[0]["model_source"], "get_version")
        self.assertTrue(units[0]["dry_capable"])
        self.assertTrue(units[0]["experimental"])
        self.assertEqual(units[128]["model"], "AMS HT")
        self.assertEqual(
            units[128]["drying"],
            {
                "active": True,
                "remaining_minutes": 119,
                "temperature": 70,
                "duration_hours": 2,
                "filament": "PA-CF",
            },
        )
        self.assertEqual(units[1]["model"], "AMS")
        self.assertFalse(units[1]["dry_capable"])
        self.assertNotIn("must-not-be-exposed", repr(snapshot))

    def test_signature_required_bit_and_top_level_ams_status_are_safety_gates(self):
        config = printer_config(model="P2S")
        store = StateStore((config,))
        store.apply_report(
            config.id,
            {
                "print": full_status(
                    fun="20000000",
                    ams_status=1,
                    ams={
                        "tray_now": "255",
                        "ams": [{"id": "0", "tray": []}],
                    },
                )
            },
        )

        self.assertFalse(
            store.snapshot()["printers"][0]["state"]["developer_lan_mode"]
        )
        self.assertTrue(store.ams_operation_risk(config.id))

    def test_get_version_does_not_make_stale_print_status_fresh(self):
        now = [1000.0]
        config = printer_config(model="X1 Carbon", stale_after_seconds=30)
        store = StateStore((config,), clock=lambda: now[0])
        store.apply_report(config.id, {"print": full_status()})
        now[0] += 31

        self.assertTrue(
            store.apply_report(
                config.id,
                {
                    "info": {
                        "command": "get_version",
                        "module": [{"name": "n3s/0"}],
                    }
                },
            )
        )
        self.assertTrue(store.snapshot()["printers"][0]["stale"])

    def test_keeps_delta_state_partial_until_full_report_and_ignores_command_ack(self):
        config = printer_config()
        store = StateStore((config,))
        store.mark_connection(config.id, "online")
        store.apply_report(
            config.id,
            {
                "print": {
                    "command": "push_status",
                    "msg": 1,
                    "gcode_state": "RUNNING",
                    "percent": 17,
                    "remain_time": 9,
                }
            },
        )
        partial = store.snapshot()["printers"][0]
        self.assertEqual(partial["connection_state"], "partial")
        self.assertEqual(partial["state"]["progress"], 17)
        self.assertFalse(
            store.apply_report(
                config.id,
                {
                    "print": {
                        "command": "pause",
                        "sequence_id": "7",
                        "result": "success",
                    }
                },
            )
        )
        self.assertNotEqual(store.raw_report(config.id)["print"]["command"], "pause")

        store.apply_report(
            config.id,
            {"print": full_status(gcode_state="RUNNING")},
        )
        self.assertEqual(store.snapshot()["printers"][0]["connection_state"], "online")

    def test_msg_zero_delta_does_not_unlock_controls_after_reconnect(self):
        config = printer_config()
        store = StateStore((config,))
        store.mark_connection(config.id, "online")

        store.apply_report(
            config.id,
            {
                "print": {
                    "command": "push_status",
                    "msg": 0,
                    "gcode_state": "RUNNING",
                }
            },
        )

        self.assertEqual(
            store.snapshot()["printers"][0]["connection_state"], "partial"
        )

    def test_old_cache_plus_delta_stays_partial_after_reconnect(self):
        config = printer_config()
        store = StateStore((config,))
        store.apply_report(config.id, {"print": full_status(mc_percent=73)})
        self.assertEqual(
            store.snapshot()["printers"][0]["connection_state"], "online"
        )
        store.mark_connection(config.id, "offline")
        store.mark_connection(config.id, "online")

        store.apply_report(
            config.id,
            {
                "print": {
                    "command": "push_status",
                    "msg": 0,
                    "gcode_state": "RUNNING",
                }
            },
        )

        snapshot = store.snapshot()["printers"][0]
        self.assertEqual(snapshot["connection_state"], "partial")
        self.assertEqual(snapshot["state"]["progress"], 73)

    def test_merges_push_status_deltas_even_when_only_a_secondary_field_changed(self):
        config = printer_config()
        store = StateStore((config,))
        store.apply_report(
            config.id,
            {"print": full_status(gcode_state="RUNNING", mc_remaining_time=22)},
        )

        changed = store.apply_report(
            config.id,
            {
                "print": {
                    "command": "push_status",
                    "mc_remaining_time": 11,
                }
            },
        )

        self.assertTrue(changed)
        self.assertEqual(
            store.snapshot()["printers"][0]["state"]["remaining_minutes"], 11
        )

    def test_does_not_merge_commandless_print_objects_into_durable_state(self):
        config = printer_config()
        store = StateStore((config,))

        changed = store.apply_report(config.id, {"print": {"gcode_state": "RUNNING"}})

        self.assertFalse(changed)
        self.assertEqual(store.raw_report(config.id), {})

    def test_auxiliary_ams_log_does_not_make_stale_status_look_fresh(self):
        now = [1000.0]
        config = printer_config(stale_after_seconds=30)
        store = StateStore((config,), clock=lambda: now[0])
        store.apply_report(
            config.id,
            {"print": full_status()},
        )
        now[0] += 31

        changed = store.apply_report(
            config.id,
            {
                "mc_print": {
                    "command": "push_info",
                    "param": "[AMS][TASK]ams0 temp:18.4;humidity:30%;humidity_idx:3",
                }
            },
        )

        self.assertTrue(changed)
        self.assertTrue(store.snapshot()["printers"][0]["stale"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.builder = CommandBuilder()

    def test_builds_safe_print_and_light_commands(self):
        pause = self.builder.build("pause")
        light = self.builder.build("light", {"on": True})
        speed = self.builder.build("speed", {"level": "sport"})
        self.assertEqual(pause.payload["print"]["command"], "pause")
        self.assertEqual(pause.payload["print"]["param"], "")
        self.assertEqual(light.payload["system"]["led_node"], "chamber_light")
        self.assertEqual(light.payload["system"]["led_mode"], "on")
        self.assertEqual(speed.payload["print"]["param"], "3")

    def test_rejects_invalid_speed_and_unknown_commands(self):
        with self.assertRaises(CommandError):
            self.builder.build("speed", {"level": 5})
        with self.assertRaises(CommandError):
            self.builder.build("set_temperature", {"component": "nozzle", "value": 400})
        with self.assertRaises(CommandError):
            self.builder.build("pause", {"param": "surprise"})

    def test_rfid_supports_new_ams_identifiers(self):
        refresh = self.builder.build("refresh_rfid", {"ams_id": 128, "slot_id": 0})
        self.assertEqual(refresh.payload["print"]["ams_id"], 128)

    def test_rfid_does_not_hardcode_four_slots(self):
        refresh = self.builder.build("refresh_rfid", {"ams_id": 128, "slot_id": 7})
        self.assertEqual(refresh.payload["print"]["slot_id"], 7)

    def test_pushall_contains_required_protocol_fields(self):
        self.assertEqual(
            push_all_payload(),
            {
                "pushing": {
                    "sequence_id": "0",
                    "command": "pushall",
                    "version": 1,
                    "push_target": 1,
                }
            },
        )

    def test_builds_exact_fresh_start_and_stop_drying_payloads(self):
        start = self.builder.build(
            "start_drying",
            {
                "ams_id": 128,
                "temp": 75,
                "duration": 6,
                "rotate_tray": False,
                "filament": "PA-CF",
            },
        )
        self.assertEqual(
            start.payload,
            {
                "print": {
                    "sequence_id": start.sequence_id,
                    "command": "ams_filament_drying",
                    "ams_id": 128,
                    "mode": 1,
                    "filament": "PA-CF",
                    "temp": 75,
                    "duration": 6,
                    "humidity": 0,
                    "rotate_tray": False,
                    "cooling_temp": 45,
                    "close_power_conflict": False,
                }
            },
        )
        stop = self.builder.build("stop_drying", {"ams_id": 128})
        self.assertEqual(
            stop.payload,
            {
                "print": {
                    "sequence_id": stop.sequence_id,
                    "command": "ams_filament_drying",
                    "ams_id": 128,
                    "mode": 0,
                    "filament": "",
                    "temp": 0,
                    "duration": 0,
                    "humidity": 0,
                    "rotate_tray": False,
                    "cooling_temp": 0,
                    "close_power_conflict": False,
                }
            },
        )

        with self.assertRaisesRegex(TypeError, "immutable"):
            start.payload["print"]["temp"] = 1
        second = self.builder.build(
            "start_drying", {"ams_id": 128, "temp": 75, "duration": 6}
        )
        self.assertIsNot(start.payload, second.payload)
        self.assertEqual(second.payload["print"]["temp"], 75)
        self.assertFalse(second.payload["print"]["rotate_tray"])
        self.assertEqual(second.payload["print"]["filament"], "")

    def test_rejects_unsafe_drying_protocol_parameters(self):
        for params in (
            {"ams_id": 128, "temp": 75, "duration": 0},
            {"ams_id": 128, "temp": 44, "duration": 1},
            {"ams_id": True, "temp": 75, "duration": 1},
            {"ams_id": 128, "temp": 75, "duration": 1, "rotate_tray": "false"},
            {"ams_id": 128, "temp": 75, "duration": 1, "filament": "PA\nCF"},
        ):
            with self.subTest(params=params), self.assertRaises(CommandError):
                self.builder.build("start_drying", params)

    def test_builds_exact_camera_setting_payloads(self):
        recording = self.builder.build("camera_recording", {"on": True})
        timelapse = self.builder.build("camera_timelapse", {"on": False})
        resolution = self.builder.build(
            "camera_resolution", {"resolution": "1080p"}
        )

        self.assertEqual(
            recording.payload,
            {
                "camera": {
                    "sequence_id": recording.sequence_id,
                    "command": "ipcam_record_set",
                    "control": "enable",
                }
            },
        )
        self.assertEqual(timelapse.payload["camera"]["control"], "disable")
        self.assertEqual(
            resolution.payload["camera"]["command"], "ipcam_resolution_set"
        )
        self.assertEqual(resolution.payload["camera"]["resolution"], "1080p")

        for command, params in (
            ("camera_recording", {"on": "true"}),
            ("camera_timelapse", {"on": 1}),
            ("camera_resolution", {"resolution": "1080p\nunsafe"}),
        ):
            with self.subTest(command=command), self.assertRaises(CommandError):
                self.builder.build(command, params)

    def test_get_version_payload_is_minimal(self):
        self.assertEqual(
            get_version_payload("17"),
            {"info": {"sequence_id": "17", "command": "get_version"}},
        )

    def test_drying_permissions_are_known_but_not_enabled_by_default(self):
        self.assertIn("start_drying", KNOWN_COMMANDS)
        self.assertIn("stop_drying", KNOWN_COMMANDS)
        self.assertNotIn("start_drying", DEFAULT_COMMANDS)
        self.assertNotIn("stop_drying", DEFAULT_COMMANDS)
        self.assertTrue(
            {
                "camera_recording",
                "camera_timelapse",
                "camera_resolution",
            }.issubset(KNOWN_COMMANDS)
        )
        self.assertFalse(
            {
                "camera_recording",
                "camera_timelapse",
                "camera_resolution",
            }.intersection(DEFAULT_COMMANDS)
        )


class SignatureCommandGateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager_index = 0

    def tearDown(self):
        self.temp_dir.cleanup()

    def manager_with_feature_flag(self, feature_flag):
        self.manager_index += 1
        printer = printer_config(
            model="P2S",
            allowed_commands=frozenset(
                {
                    "pause",
                    "resume",
                    "stop",
                    "speed",
                    "light",
                    "refresh_rfid",
                    "start_drying",
                    "stop_drying",
                }
            ),
        )
        config = AppConfig(
            printers=(printer,),
            web=WebConfig(
                username="admin",
                password="web-secret",
                allowed_origins=("https://testserver",),
            ),
            audit_db=str(
                pathlib.Path(self.temp_dir.name)
                / f"audit-{self.manager_index}.sqlite3"
            ),
        )
        manager = ControlManager(
            config,
            AuditLog(config.audit_db),
            client_factory=FakeMqttClient,
        )
        manager.start()
        report = full_status(
            gcode_state="RUNNING",
            lights_report=[{"node": "chamber_light", "mode": "off"}],
            ams={"tray_now": "255", "ams": [{"id": "0", "tray": []}]},
        )
        if feature_flag is None:
            report.pop("fun")
        else:
            report["fun"] = feature_flag
        manager.store.apply_report(printer.id, {"print": report})
        return manager, printer

    def test_normal_print_commands_fail_closed_without_confirmed_developer_lan_mode(self):
        commands = (
            ("pause", {}),
            ("resume", {}),
            ("stop", {}),
            ("speed", {"level": 2}),
            ("refresh_rfid", {"ams_id": 0}),
            (
                "start_drying",
                {"ams_id": 0, "temp": 55, "duration": 2},
            ),
        )
        for feature_flag in (None, "20000000"):
            for command, params in commands:
                with self.subTest(feature_flag=feature_flag, command=command):
                    manager, printer = self.manager_with_feature_flag(feature_flag)
                    with self.assertRaisesRegex(
                        CommandUnavailable,
                        "Developer-LAN-Modus",
                    ):
                        manager.send_command(
                            printer.id,
                            command,
                            params,
                            "admin",
                        )
                    manager.stop()

    def test_explicit_developer_lan_mode_allows_a_valid_print_command(self):
        manager, printer = self.manager_with_feature_flag(0)

        result = manager.send_command(printer.id, "pause", {}, "admin")

        self.assertEqual(result["status"], "acknowledged")
        self.assertEqual(
            manager._workers[printer.id].payloads[-1]["print"]["command"],
            "pause",
        )
        manager.stop()

    def test_reported_system_light_remains_separately_gated(self):
        manager, printer = self.manager_with_feature_flag("20000000")

        result = manager.send_command(
            printer.id,
            "light",
            {"node": "chamber_light", "on": True},
            "admin",
        )

        self.assertEqual(result["status"], "acknowledged")
        self.assertEqual(
            manager._workers[printer.id].payloads[-1]["system"]["led_node"],
            "chamber_light",
        )
        manager.stop()


class DryingManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.audit_path = str(pathlib.Path(self.temp_dir.name) / "audit.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def manager(
        self,
        *,
        printer_model="P2S",
        module_name="n3f/0",
        ams_id=0,
        dry_time=0,
        status="IDLE",
        fun=0,
        tray_now=255,
        tray_tar=255,
        ams_status=0,
        allowed_commands=frozenset({"start_drying", "stop_drying"}),
    ):
        config = printer_config(
            model=printer_model,
            allowed_commands=allowed_commands,
        )
        app_config = AppConfig(
            printers=(config,),
            web=WebConfig(
                username="admin",
                password="web-secret",
                allowed_origins=("https://testserver",),
            ),
            audit_db=self.audit_path,
        )
        manager = ControlManager(
            app_config,
            AuditLog(self.audit_path),
            client_factory=FakeMqttClient,
            drying_confirmation_timeout=0,
        )
        if module_name is not None:
            manager.store.apply_report(
                config.id,
                {
                    "info": {
                        "command": "get_version",
                        "module": [{"name": module_name}],
                    }
                },
            )
        manager.store.apply_report(
            config.id,
            {
                "print": full_status(
                    gcode_state=status,
                    fun=fun,
                    ams_status=ams_status,
                    ams={
                        "tray_now": str(tray_now),
                        "tray_tar": str(tray_tar),
                        "ams": [
                            {
                                "id": str(ams_id),
                                "dry_time": str(dry_time),
                                "dry_setting": {
                                    "dry_temperature": "60",
                                    "dry_duration": "2",
                                    "dry_filament": "PETG",
                                },
                                "tray": [{"id": "0"}],
                            }
                        ],
                    },
                )
            },
        )
        return manager, config

    @staticmethod
    def start_params(ams_id=0, **overrides):
        params = {
            "ams_id": ams_id,
            "temp": 60,
            "duration": 2,
            "rotate_tray": False,
        }
        params.update(overrides)
        return params

    def set_dry_time_on_publish(self, manager, config, ams_id, dry_time, acknowledgement):
        worker = FakeMqttClient.instances[config.id]

        def publish_and_wait(payload, sequence_id, timeout=4.0):
            worker.publish(payload)
            manager.store.apply_report(
                config.id,
                {
                    "print": {
                        "command": "push_status",
                        "ams": {
                            "ams": [
                                {
                                    "id": str(ams_id),
                                    "dry_time": str(dry_time),
                                    "tray": [{"id": "0"}],
                                }
                            ]
                        },
                    }
                },
            )
            return acknowledgement

        worker.publish_and_wait = publish_and_wait

    def test_start_is_successful_only_after_dry_time_confirms_it(self):
        manager, config = self.manager()
        self.set_dry_time_on_publish(
            manager,
            config,
            0,
            120,
            {"result": "success"},
        )

        result = manager.send_command(
            config.id,
            "start_drying",
            self.start_params(),
            "admin",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["acknowledged"])
        payload = FakeMqttClient.instances[config.id].payloads[-1]["print"]
        self.assertEqual(payload["mode"], 1)
        self.assertEqual(payload["cooling_temp"], 45)
        self.assertFalse(payload["close_power_conflict"])

    def test_state_can_confirm_start_even_when_separate_ack_times_out(self):
        manager, config = self.manager()
        self.set_dry_time_on_publish(manager, config, 0, 120, None)

        result = manager.send_command(
            config.id,
            "start_drying",
            self.start_params(),
            "admin",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "confirmed")
        self.assertFalse(result["acknowledged"])

    def test_ack_without_state_transition_is_unconfirmed_not_success(self):
        manager, config = self.manager()

        result = manager.send_command(
            config.id,
            "start_drying",
            self.start_params(),
            "admin",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unconfirmed")
        self.assertTrue(result["acknowledged"])

    def test_ack_timeout_without_state_transition_is_unconfirmed(self):
        manager, config = self.manager()
        worker = FakeMqttClient.instances[config.id]
        worker.publish_and_wait = lambda payload, sequence_id, timeout=4.0: None

        result = manager.send_command(
            config.id,
            "start_drying",
            self.start_params(),
            "admin",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unconfirmed")
        self.assertFalse(result["acknowledged"])

    def test_explicit_failed_ack_is_rejected_even_if_state_changes(self):
        manager, config = self.manager()
        self.set_dry_time_on_publish(
            manager,
            config,
            0,
            120,
            {"result": "failed", "reason": "not allowed"},
        )

        with self.assertRaisesRegex(CommandUnavailable, "not allowed"):
            manager.send_command(
                config.id,
                "start_drying",
                self.start_params(),
                "admin",
            )

    def test_stop_uses_official_zero_payload_and_requires_state_confirmation(self):
        manager, config = self.manager(dry_time=120)
        self.set_dry_time_on_publish(
            manager,
            config,
            0,
            0,
            {"result": "success"},
        )

        result = manager.send_command(
            config.id,
            "stop_drying",
            {"ams_id": 0},
            "admin",
        )

        self.assertEqual(result["status"], "confirmed")
        payload = FakeMqttClient.instances[config.id].payloads[-1]["print"]
        self.assertEqual(payload["mode"], 0)
        self.assertEqual(payload["cooling_temp"], 0)
        self.assertEqual(payload["duration"], 0)
        self.assertFalse(payload["rotate_tray"])

    def test_stop_is_idempotent_when_dry_time_is_already_zero(self):
        manager, config = self.manager(dry_time=0)

        result = manager.send_command(
            config.id,
            "stop_drying",
            {"ams_id": 0},
            "admin",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unconfirmed")
        payload = FakeMqttClient.instances[config.id].payloads[-1]["print"]
        self.assertEqual(payload["command"], "ams_filament_drying")
        self.assertEqual(payload["mode"], 0)

    def test_stop_at_zero_is_confirmed_by_a_fresh_zero_report(self):
        manager, config = self.manager(dry_time=0)
        self.set_dry_time_on_publish(
            manager,
            config,
            0,
            0,
            {"result": "success"},
        )

        result = manager.send_command(
            config.id,
            "stop_drying",
            {"ams_id": 0},
            "admin",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "confirmed")

    def test_stop_is_published_when_dry_time_is_missing(self):
        manager, config = self.manager(dry_time=0)
        manager.store.apply_report(
            config.id,
            {
                "print": {
                    "command": "push_status",
                    "ams": {
                        "ams": [
                            {
                                "id": "0",
                                "tray": [{"id": "0"}],
                            }
                        ]
                    },
                }
            },
        )

        result = manager.send_command(
            config.id,
            "stop_drying",
            {"ams_id": 0},
            "admin",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unconfirmed")
        payload = FakeMqttClient.instances[config.id].payloads[-1]["print"]
        self.assertEqual(payload["mode"], 0)

    def test_stop_recovers_after_an_unconfirmed_start(self):
        manager, config = self.manager(dry_time=0)
        start = manager.send_command(
            config.id,
            "start_drying",
            self.start_params(),
            "admin",
        )
        self.assertEqual(start["status"], "unconfirmed")

        # Production waits up to 30 seconds for start confirmation, comfortably
        # beyond the generic command rate interval. Tests use a zero timeout.
        manager._last_command_at[config.id] = 0
        self.set_dry_time_on_publish(
            manager,
            config,
            0,
            0,
            {"result": "success"},
        )
        stop = manager.send_command(
            config.id,
            "stop_drying",
            {"ams_id": 0},
            "admin",
        )

        self.assertEqual(stop["status"], "confirmed")
        sent_modes = [
            payload["print"]["mode"]
            for payload in FakeMqttClient.instances[config.id].payloads[-2:]
        ]
        self.assertEqual(sent_modes, [1, 0])

    def test_stop_ignores_start_only_error_busy_and_signature_gates(self):
        for printer_status in ("CHECKING", "ERROR"):
            with self.subTest(printer_status=printer_status):
                manager, config = self.manager(
                    printer_model="X1 Carbon",
                    module_name="n3s/0",
                    ams_id=128,
                    dry_time=0,
                    status=printer_status,
                    fun="20000000",
                    tray_now=0,
                    ams_status=1,
                )
                self.set_dry_time_on_publish(
                    manager,
                    config,
                    128,
                    0,
                    {"result": "success"},
                )

                result = manager.send_command(
                    config.id,
                    "stop_drying",
                    {"ams_id": 128},
                    "admin",
                )

                self.assertEqual(result["status"], "confirmed")

    def test_x1_start_needs_explicit_ack_but_stop_never_does(self):
        manager, config = self.manager(
            printer_model="X1 Carbon",
            module_name="n3s/0",
            ams_id=128,
        )
        with self.assertRaisesRegex(CommandUnavailable, "ausdrücklich bestätigt"):
            manager.send_command(
                config.id,
                "start_drying",
                self.start_params(128),
                "admin",
            )

        manager, config = self.manager(
            printer_model="X1 Carbon",
            module_name="n3s/0",
            ams_id=128,
            dry_time=60,
            fun="20000000",
            ams_status=1,
        )
        self.set_dry_time_on_publish(
            manager,
            config,
            128,
            0,
            {"result": "success"},
        )
        result = manager.send_command(
            config.id,
            "stop_drying",
            {"ams_id": 128},
            "admin",
        )
        self.assertEqual(result["status"], "confirmed")

    def test_ams_2_pro_and_ht_have_different_temperature_limits(self):
        manager, config = self.manager()
        with self.assertRaisesRegex(CommandError, "45 and 65"):
            manager.send_command(
                config.id,
                "start_drying",
                self.start_params(temp=66),
                "admin",
            )

        manager, config = self.manager(
            printer_model="X1 Carbon",
            module_name="n3s/0",
            ams_id=128,
        )
        result = manager.send_command(
            config.id,
            "start_drying",
            self.start_params(128, temp=85, experimental_ack=True),
            "admin",
        )
        self.assertEqual(result["status"], "unconfirmed")

    def test_blocks_incompatible_ams_printers_and_unknown_ids(self):
        cases = (
            {"printer_model": "P1S", "module_name": "n3f/0"},
            {"printer_model": "A1 Mini", "module_name": "n3f/0"},
            {"printer_model": "P2S", "module_name": "ams/0"},
            {"printer_model": "P2S", "module_name": None},
        )
        for case in cases:
            with self.subTest(case=case):
                manager, config = self.manager(**case)
                with self.assertRaises(CommandUnavailable):
                    manager.send_command(
                        config.id,
                        "start_drying",
                        self.start_params(),
                        "admin",
                    )

        manager, config = self.manager()
        with self.assertRaisesRegex(CommandError, "AMS-ID"):
            manager.send_command(
                config.id,
                "start_drying",
                self.start_params(7),
                "admin",
            )

    def test_blocks_start_during_printing_loading_or_signature_required_mode(self):
        cases = (
            ({"status": "RUNNING"}, "untätigem Drucker"),
            ({"tray_now": 0}, "Filament geladen"),
            ({"ams_status": 1}, "Filament geladen"),
            ({"ams_status": None}, "Filament geladen"),
            ({"fun": 0x20000000}, "Developer-LAN-Modus"),
            ({"fun": None}, "Developer-LAN-Modus"),
        )
        for options, message in cases:
            with self.subTest(options=options):
                manager, config = self.manager(**options)
                with self.assertRaisesRegex(CommandUnavailable, message):
                    manager.send_command(
                        config.id,
                        "start_drying",
                        self.start_params(),
                        "admin",
                    )

    def test_existing_allowlist_is_not_silently_expanded(self):
        manager, config = self.manager(allowed_commands=DEFAULT_COMMANDS)
        with self.assertRaises(CommandForbidden):
            manager.send_command(
                config.id,
                "start_drying",
                self.start_params(),
                "admin",
            )

    def test_runtime_rejects_programmatic_start_only_permission(self):
        manager, config = self.manager(
            allowed_commands=frozenset({"start_drying"})
        )

        with self.assertRaisesRegex(CommandForbidden, "Stop-Freigabe"):
            manager.send_command(
                config.id,
                "start_drying",
                self.start_params(),
                "admin",
            )


class CameraCommandManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.audit_path = str(pathlib.Path(self.temp_dir.name) / "audit.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def manager(self, *, ipcam=None):
        commands = frozenset(
            {"camera_recording", "camera_timelapse", "camera_resolution"}
        )
        config = printer_config(
            model="X1 Carbon",
            allowed_commands=commands,
        )
        app_config = AppConfig(
            printers=(config,),
            web=WebConfig(
                username="admin",
                password="web-secret",
                allowed_origins=("https://testserver",),
            ),
            audit_db=self.audit_path,
        )
        manager = ControlManager(
            app_config,
            AuditLog(self.audit_path),
            client_factory=FakeMqttClient,
        )
        manager.store.apply_report(
            config.id,
            {
                "print": full_status(
                    ipcam=(
                        {
                            "ipcam_dev": "1",
                            "ipcam_record": "disable",
                            "timelapse": "disable",
                            "resolution": "720p",
                            "resolution_supported": ["720p", "1080p"],
                            "liveview": {"local": "rtsps"},
                        }
                        if ipcam is None
                        else ipcam
                    )
                )
            },
        )
        return manager, config

    def test_sends_allowlisted_camera_commands(self):
        manager, config = self.manager()

        result = manager.send_command(
            config.id, "camera_recording", {"on": True}, "admin"
        )

        self.assertTrue(result["ok"])
        payload = FakeMqttClient.instances[config.id].payloads[-1]["camera"]
        self.assertEqual(payload["command"], "ipcam_record_set")
        self.assertEqual(payload["control"], "enable")

    def test_resolution_must_be_reported_by_the_printer(self):
        manager, config = self.manager()

        with self.assertRaisesRegex(CommandUnavailable, "nicht gemeldet"):
            manager.send_command(
                config.id,
                "camera_resolution",
                {"resolution": "4k"},
                "admin",
            )

    def test_camera_commands_fail_closed_when_camera_is_not_reported(self):
        manager, config = self.manager(ipcam={"ipcam_dev": "0"})

        with self.assertRaisesRegex(CommandUnavailable, "keine steuerbare Kamera"):
            manager.send_command(
                config.id,
                "camera_timelapse",
                {"on": True},
                "admin",
            )


class CameraStreamManagerTests(unittest.IsolatedAsyncioTestCase):
    def limits(self, **overrides):
        values = {
            "max_total_sessions": 2,
            "startup_timeout_seconds": 0.2,
            "inactivity_timeout_seconds": 0.2,
            "max_session_seconds": 10,
            "max_restarts": 0,
            "terminate_timeout_seconds": 0.2,
            "frames_per_second": 3,
            "max_width": 960,
        }
        values.update(overrides)
        return CameraLimits(**values)

    def config(self, **overrides):
        values = {
            "model": "X1 Carbon",
            "camera_enabled": True,
            "allow_self_signed_tls": False,
            "tls_ca_file": "/app/certs/bambu-lab-ca.pem",
        }
        values.update(overrides)
        return printer_config(**values)

    async def test_stream_uses_secretless_launcher_argv_and_cleans_up(self):
        factory = FakeCameraProcessFactory(
            [[b"--bambu_frame\r\nContent-Type: image/jpeg\r\n\r\nJPEG", b""]]
        )
        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        config = self.config(access_code="secret:camera@code")

        stream = await manager.open_stream(
            config,
            session_key="session-digest",
            is_authorized=lambda: True,
        )
        iterator = stream.iter_bytes()
        first = await anext(iterator)
        with self.assertRaises(StopAsyncIteration):
            await anext(iterator)

        self.assertIn(CAMERA_BOUNDARY.encode(), first)
        visible_process_data = repr(factory.commands)
        self.assertNotIn("secret:camera@code", visible_process_data)
        self.assertNotIn("secret%3Acamera%40code", visible_process_data)
        launch_arguments = factory.launch_payloads[0]["arguments"]
        input_url = launch_arguments[launch_arguments.index("-i") + 1]
        self.assertEqual(input_url, camera_input_url(config))
        self.assertIn("secret%3Acamera%40code", input_url)
        self.assertTrue(factory.processes[0].terminated)
        self.assertFalse(await manager.is_active(config.id))

    async def test_only_one_ffmpeg_process_is_allowed_per_printer(self):
        factory = FakeCameraProcessFactory([[b"frame"], [b"other"]])
        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        config = self.config()
        first = await manager.open_stream(
            config,
            session_key="one",
            is_authorized=lambda: True,
        )

        with self.assertRaises(CameraBusy):
            await manager.open_stream(
                config,
                session_key="two",
                is_authorized=lambda: True,
            )

        self.assertEqual(len(factory.processes), 1)
        await first.close()
        self.assertFalse(await manager.is_active(config.id))

    async def test_only_owning_session_can_explicitly_stop_a_stream(self):
        factory = FakeCameraProcessFactory([[b"frame"]])
        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        config = self.config()
        await manager.open_stream(
            config,
            session_key="session-owner",
            is_authorized=lambda: True,
        )

        self.assertFalse(await manager.close_stream(config.id, "other-session"))
        self.assertTrue(await manager.is_active(config.id))
        self.assertTrue(await manager.close_stream(config.id, "session-owner"))
        self.assertFalse(await manager.is_active(config.id))
        self.assertTrue(factory.processes[0].terminated)

    async def test_failed_start_releases_slot_and_terminates_process(self):
        factory = FakeCameraProcessFactory([[b""]])
        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        config = self.config()

        with self.assertRaises(CameraUnavailable):
            await manager.open_stream(
                config,
                session_key="one",
                is_authorized=lambda: True,
            )

        self.assertTrue(factory.processes[0].terminated)
        self.assertFalse(await manager.is_active(config.id))

    async def test_revoked_session_stops_before_forwarding_buffered_image(self):
        authorized = [True]
        factory = FakeCameraProcessFactory([[b"frame-one", b"frame-two"]])
        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        config = self.config()
        stream = await manager.open_stream(
            config,
            session_key="one",
            is_authorized=lambda: authorized[0],
        )
        authorized[0] = False

        iterator = stream.iter_bytes()
        with self.assertRaises(StopAsyncIteration):
            await anext(iterator)

        self.assertTrue(factory.processes[0].terminated)

    async def test_stream_restart_is_bounded(self):
        factory = FakeCameraProcessFactory(
            [[b"first", b""], [b"second", b""]]
        )
        manager = CameraStreamManager(
            limits=self.limits(max_restarts=1),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        config = self.config()
        stream = await manager.open_stream(
            config,
            session_key="one",
            is_authorized=lambda: True,
        )

        chunks = [chunk async for chunk in stream.iter_bytes()]

        self.assertEqual(chunks, [b"first", b"second"])
        self.assertEqual(len(factory.processes), 2)
        self.assertTrue(all(process.terminated for process in factory.processes))

    async def test_cancelled_close_is_shared_and_still_releases_the_slot(self):
        blocking = BlockingCameraProcess([b"frame"])
        replacement = FakeCameraProcess([b"replacement"])
        processes = [blocking, replacement]

        async def factory(*_command, **_options):
            return processes.pop(0)

        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        config = self.config()
        stream = await manager.open_stream(
            config,
            session_key="one",
            is_authorized=lambda: True,
        )

        cancelled_caller = asyncio.create_task(stream.close())
        await asyncio.wait_for(blocking.wait_started.wait(), timeout=0.2)
        cancelled_caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled_caller

        second_caller = asyncio.create_task(stream.close())
        await asyncio.sleep(0)
        self.assertFalse(second_caller.done())
        blocking.wait_allowed.set()
        await second_caller

        self.assertEqual(blocking.terminate_calls, 1)
        self.assertFalse(await manager.is_active(config.id))
        restarted = await manager.open_stream(
            config,
            session_key="two",
            is_authorized=lambda: True,
        )
        await restarted.close()
        self.assertFalse(await manager.is_active(config.id))

    async def test_process_wait_failure_cannot_leave_a_busy_slot(self):
        class FailingWaitProcess(FakeCameraProcess):
            async def wait(self):
                raise RuntimeError("simulated wait failure")

        process = FailingWaitProcess([b"frame"])

        async def factory(*_command, **_options):
            return process

        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        config = self.config()
        stream = await manager.open_stream(
            config,
            session_key="one",
            is_authorized=lambda: True,
        )

        with self.assertRaisesRegex(RuntimeError, "simulated wait failure"):
            await stream.close()

        self.assertFalse(await manager.is_active(config.id))

    async def test_close_during_restart_spawn_cannot_orphan_a_process(self):
        spawn_started = asyncio.Event()
        spawn_allowed = asyncio.Event()
        first = FakeCameraProcess([b"first", b""])
        second = FakeCameraProcess([b"second"])
        launches = 0

        async def factory(*_command, **_options):
            nonlocal launches
            launches += 1
            if launches == 1:
                return first
            spawn_started.set()
            await spawn_allowed.wait()
            return second

        manager = CameraStreamManager(
            limits=self.limits(max_restarts=1),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        config = self.config()
        stream = await manager.open_stream(
            config,
            session_key="one",
            is_authorized=lambda: True,
        )
        iterator = stream.iter_bytes()
        self.assertEqual(await anext(iterator), b"first")
        next_chunk = asyncio.create_task(anext(iterator))
        await asyncio.wait_for(spawn_started.wait(), timeout=1)

        close_task = asyncio.create_task(stream.close())
        await asyncio.sleep(0)
        spawn_allowed.set()
        await close_task
        with self.assertRaises(StopAsyncIteration):
            await next_chunk

        self.assertTrue(first.terminated)
        self.assertTrue(second.terminated)
        self.assertFalse(await manager.is_active(config.id))

    async def test_ticket_reservation_is_atomic_with_stop(self):
        spawn_started = asyncio.Event()
        spawn_allowed = asyncio.Event()
        first = FakeCameraProcess([b"first"])
        replacement = FakeCameraProcess([b"replacement"])
        launches = 0

        async def factory(*_command, **_options):
            nonlocal launches
            launches += 1
            if launches == 1:
                spawn_started.set()
                await spawn_allowed.wait()
                return first
            return replacement

        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        config = self.config()
        ticket, _expires_in = await manager.issue_ticket(config.id, "session")
        start_task = asyncio.create_task(
            manager.open_ticketed_stream(
                config,
                ticket=ticket,
                session_key="session",
                is_authorized=lambda: True,
            )
        )
        await asyncio.wait_for(spawn_started.wait(), timeout=0.2)

        await manager.revoke_printer_tickets(config.id)
        stop_task = asyncio.create_task(manager.close_stream(config.id))
        await asyncio.sleep(0)
        spawn_allowed.set()
        with self.assertRaises(CameraUnavailable):
            await start_task
        self.assertTrue(await stop_task)
        self.assertTrue(first.terminated)
        self.assertFalse(await manager.is_active(config.id))

        next_ticket, _expires_in = await manager.issue_ticket(config.id, "session")
        restarted = await manager.open_ticketed_stream(
            config,
            ticket=next_ticket,
            session_key="session",
            is_authorized=lambda: True,
        )
        await restarted.close()
        self.assertFalse(await manager.is_active(config.id))

    async def test_revoked_ticket_cannot_reserve_a_stream_after_stop(self):
        factory = FakeCameraProcessFactory([[b"frame"]])
        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        config = self.config()
        ticket, _expires_in = await manager.issue_ticket(config.id, "session")

        await manager.revoke_printer_tickets(config.id)
        self.assertFalse(await manager.close_stream(config.id))
        with self.assertRaises(CameraTicketRejected):
            await manager.open_ticketed_stream(
                config,
                ticket=ticket,
                session_key="session",
                is_authorized=lambda: True,
            )

        self.assertEqual(factory.processes, [])
        self.assertFalse(await manager.is_active(config.id))

    async def test_disabled_and_non_x1_configs_fail_before_process_start(self):
        factory = FakeCameraProcessFactory()
        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )
        for config in (
            self.config(camera_enabled=False),
            self.config(model="P1S"),
        ):
            with self.subTest(config=config.model), self.assertRaises(CameraUnavailable):
                await manager.open_stream(
                    config,
                    session_key="one",
                    is_authorized=lambda: True,
                )
        self.assertEqual(factory.processes, [])

    async def test_camera_stream_rejects_unverified_tls_before_process_start(self):
        factory = FakeCameraProcessFactory()
        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=factory,
        )

        with self.assertRaises(CameraUnavailable):
            await manager.open_stream(
                self.config(allow_self_signed_tls=True, tls_ca_file=None),
                session_key="one",
                is_authorized=lambda: True,
            )

        self.assertFalse(
            camera_tls_supported(
                self.config(allow_self_signed_tls=True, tls_ca_file=None)
            )
        )
        self.assertEqual(factory.processes, [])

    async def test_camera_tickets_are_short_lived_bound_and_single_use(self):
        now = [100.0]
        manager = CameraStreamManager(
            limits=self.limits(),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=FakeCameraProcessFactory(),
            clock=lambda: now[0],
        )
        ticket, expires_in = await manager.issue_ticket("werkstatt", "session-a")
        self.assertEqual(expires_in, 30)
        self.assertFalse(
            await manager.consume_ticket(
                "wrong-ticket-value-that-is-long-enough",
                printer_id="werkstatt",
                session_key="session-a",
            )
        )
        self.assertTrue(
            await manager.consume_ticket(
                ticket,
                printer_id="werkstatt",
                session_key="session-a",
            )
        )
        self.assertFalse(
            await manager.consume_ticket(
                ticket,
                printer_id="werkstatt",
                session_key="session-a",
            )
        )

        other_ticket, _expires_in = await manager.issue_ticket(
            "werkstatt", "session-a"
        )
        self.assertFalse(
            await manager.consume_ticket(
                other_ticket,
                printer_id="anderer-drucker",
                session_key="session-a",
            )
        )
        expired_ticket, _expires_in = await manager.issue_ticket(
            "werkstatt", "session-a"
        )
        now[0] += 31
        self.assertFalse(
            await manager.consume_ticket(
                expired_ticket,
                printer_id="werkstatt",
                session_key="session-a",
            )
        )


class MqttClientTests(unittest.TestCase):
    def test_builds_a_clean_mqtt_v311_client_with_the_vendored_ca(self):
        ca_file = SERVICE_DIR / "certs" / "bambu-lab-ca.pem"
        config = printer_config(
            tls_ca_file=str(ca_file),
            allow_self_signed_tls=False,
        )
        store = StateStore((config,))

        worker = PrinterMqttClient(config, store, lambda: None)

        self.assertEqual(worker.report_topic, f"device/{config.serial}/report")
        self.assertEqual(worker.request_topic, f"device/{config.serial}/request")
        self.assertTrue(worker.client._clean_session)

    def test_print_lifecycle_controls_use_qos_one(self):
        config = printer_config()
        worker = PrinterMqttClient(config, StateStore((config,)), lambda: None)
        worker.client.publish = Mock(return_value=SimpleNamespace(rc=0))
        builder = CommandBuilder(start=10)

        for command in ("pause", "resume", "stop"):
            worker._publish(builder.build(command).payload)
            self.assertEqual(worker.client.publish.call_args.kwargs["qos"], 1)

        worker._publish(builder.build("light", {"on": True}).payload)
        self.assertEqual(worker.client.publish.call_args.kwargs["qos"], 0)

    def test_pinned_tls_retries_without_connecting_before_validation(self):
        class FakeTimer:
            def __init__(self, delay, callback):
                self.delay = delay
                self.callback = callback
                self.daemon = False
                self.started = False

            def start(self):
                self.started = True

            def cancel(self):
                pass

        timers = []

        def timer_factory(delay, callback):
            timer = FakeTimer(delay, callback)
            timers.append(timer)
            return timer

        config = printer_config(
            tls_ca_file=None,
            allow_self_signed_tls=True,
            tls_fingerprint_sha256="a" * 64,
        )
        worker = PrinterMqttClient(config, StateStore((config,)), lambda: None)
        worker.client.tls_set_context = Mock()
        worker.client.connect_async = Mock()
        worker.client.loop_start = Mock()
        pinned_context = Mock()

        with (
            unittest.mock.patch.object(
                threading, "Timer", side_effect=timer_factory
            ),
            unittest.mock.patch(
                "app.mqtt_client._pinned_tls_context",
                side_effect=[OSError("offline"), pinned_context],
            ),
        ):
            worker.start()
            self.assertEqual(timers[0].delay, 0)
            self.assertTrue(timers[0].started)
            worker.client.connect_async.assert_not_called()
            timers[0].callback()
            self.assertEqual(timers[1].delay, 10)
            self.assertTrue(timers[1].started)
            worker.client.connect_async.assert_not_called()
            timers[1].callback()

        worker.client.tls_set_context.assert_called_once_with(pinned_context)
        worker.client.connect_async.assert_called_once_with(
            config.host, config.port, keepalive=60
        )
        worker.client.loop_start.assert_called_once_with()

    def test_pinned_tls_probe_mismatch_never_builds_a_credentials_connection(self):
        config = printer_config(
            tls_ca_file=None,
            allow_self_signed_tls=True,
            tls_fingerprint_sha256="a" * 64,
        )
        connection = Mock()
        transport = Mock()
        transport.getpeercert.return_value = b"different-certificate"
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        transport.__enter__ = Mock(return_value=transport)
        transport.__exit__ = Mock(return_value=False)
        probe_context = Mock()
        probe_context.wrap_socket.return_value = transport

        with (
            unittest.mock.patch(
                "app.mqtt_client.ssl.SSLContext", return_value=probe_context
            ),
            unittest.mock.patch(
                "app.mqtt_client.socket.create_connection",
                return_value=connection,
            ),
        ):
            with self.assertRaises(ssl.SSLError):
                _pinned_tls_context(config)

        self.assertEqual(probe_context.minimum_version, ssl.TLSVersion.TLSv1_2)
        probe_context.wrap_socket.assert_called_once()

    def test_config_rejects_mixed_or_incomplete_tls_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            access = root / "access"
            password = root / "password"
            access.write_text("access-code\n", encoding="utf-8")
            password.write_text("web-password-long\n", encoding="utf-8")

            base = {
                "web": {
                    "username": "admin",
                    "password_file": str(password),
                    "allowed_origins": ["https://printer.test"],
                },
                "printers": [
                    {
                        "id": "x1c",
                        "host": "192.0.2.1",
                        "serial": "01S00A000000001",
                        "access_code_file": str(access),
                    }
                ],
            }
            invalid_modes = (
                {
                    "allow_self_signed_tls": True,
                    "tls_ca_file": "/app/certs/bambu-lab-ca.pem",
                    "tls_fingerprint_sha256": "a" * 64,
                },
                {
                    "allow_self_signed_tls": False,
                    "tls_ca_file": "/app/certs/bambu-lab-ca.pem",
                    "tls_fingerprint_sha256": "a" * 64,
                },
            )
            for index, mode in enumerate(invalid_modes):
                with self.subTest(mode=mode):
                    document = json.loads(json.dumps(base))
                    document["printers"][0].update(mode)
                    path = root / f"invalid-{index}.yml"
                    path.write_text(yaml.safe_dump(document), encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_config(path)

    def test_legacy_self_signed_config_loads_but_cannot_send_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            access = root / "access"
            password = root / "password"
            access.write_text("access-code\n", encoding="utf-8")
            password.write_text("web-password-long\n", encoding="utf-8")
            config_path = root / "printers.yml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "web": {
                            "username": "admin",
                            "password_file": str(password),
                            "allowed_origins": ["https://printer.test"],
                        },
                        "printers": [
                            {
                                "id": "legacy",
                                "host": "192.0.2.1",
                                "serial": "01S00A000000001",
                                "access_code_file": str(access),
                                "allow_self_signed_tls": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path).printers[0]

        self.assertIsNone(config.tls_fingerprint_sha256)
        worker = PrinterMqttClient(config, StateStore((config,)), lambda: None)
        worker.client.connect_async = Mock()
        worker.client.loop_start = Mock()
        timer = Mock()
        with unittest.mock.patch.object(threading, "Timer", return_value=timer):
            worker.start()
        worker.client.connect_async.assert_not_called()
        worker.client.loop_start.assert_not_called()
        timer.start.assert_called_once_with()


class AuditAndMetricsTests(unittest.TestCase):
    def test_audit_redacts_nested_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "audit.sqlite3")
            audit = AuditLog(path)
            audit.record(
                actor="admin",
                printer_id="p1s",
                command="pause",
                params={"nested": {"access_code": "never-store-this"}},
                result="sent",
            )
            with closing(sqlite3.connect(path)) as connection:
                value = connection.execute("SELECT params_json FROM command_audit").fetchone()[0]
            self.assertNotIn("never-store-this", value)
            self.assertIn("[redacted]", value)

    def test_metrics_do_not_expose_serial_or_access_code(self):
        config = printer_config()
        store = StateStore((config,))
        store.mark_connection(config.id, "online")
        store.apply_report(
            config.id,
            {"print": full_status(mc_percent=7)},
        )
        text = render_metrics(store.snapshot(), {"sent": 2})
        self.assertIn('bambu_printer_online{printer="werkstatt"} 1', text)
        self.assertNotIn(config.serial, text)
        self.assertNotIn(config.access_code, text)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            printers=(printer_config(),),
            web=WebConfig(
                username="admin",
                password="web-secret",
                allowed_origins=("https://testserver",),
            ),
            audit_db=str(pathlib.Path(self.temp_dir.name) / "audit.sqlite3"),
        )
        self.audit = AuditLog(self.config.audit_db)
        self.manager = ControlManager(self.config, self.audit, client_factory=FakeMqttClient)
        self.session_clock = [100.0]
        self.session_store = SessionStore(
            ttl_seconds=60,
            clock=lambda: self.session_clock[0],
        )
        self.login_rate_limiter = LoginRateLimiter()
        self.manager.store.apply_report(
            "werkstatt",
            {
                "print": full_status(
                    gcode_state="RUNNING",
                    mc_percent=12,
                    ams={
                        "ams": [
                            {
                                "id": "1",
                                "tray": [
                                    {"id": "0", "tray_type": "PLA"},
                                    {"id": "1", "tray_type": "PETG"},
                                ],
                            }
                        ]
                    },
                )
            },
        )
        self.client_context = TestClient(
            create_app(
                self.config,
                self.manager,
                supplied_session_store=self.session_store,
                supplied_login_rate_limiter=self.login_rate_limiter,
                supplied_websocket_revalidation_seconds=0.02,
            ),
            base_url="https://testserver",
        )
        self.client = self.client_context.__enter__()
        self.auth = ("admin", "web-secret")

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def login(self, username="admin", password="web-secret", origin="https://testserver"):
        return self.client.post(
            "/api/login",
            headers={"Origin": origin},
            json={"username": username, "password": password},
        )

    def test_api_requires_authentication(self):
        self.assertEqual(self.client.get("/api/printers").status_code, 401)
        response = self.client.get("/api/printers", auth=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["printers"][0]["id"], "werkstatt")

    def test_unauthenticated_page_redirects_to_login_but_api_remains_basic_compatible(self):
        root = self.client.get("/", follow_redirects=False)
        login_page = self.client.get("/login")
        basic_login = self.client.get("/login", auth=self.auth, follow_redirects=False)
        api = self.client.get("/api/printers")

        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn("default-src 'self'", login_page.headers["content-security-policy"])
        self.assertEqual(basic_login.status_code, 303)
        self.assertEqual(basic_login.headers["location"], "/")
        self.assertEqual(api.status_code, 401)
        self.assertIn("Basic", api.headers["www-authenticate"])

    def test_json_login_sets_a_bounded_secure_server_side_session(self):
        response = self.login()

        self.assertEqual(response.status_code, 204, response.text)
        cookie = response.headers["set-cookie"]
        self.assertIn("bambu_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Max-Age=60", cookie)
        self.assertEqual(self.client.get("/api/printers").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)

        self.session_clock[0] += 61
        self.assertEqual(self.client.get("/api/printers").status_code, 401)
        expired_root = self.client.get("/", follow_redirects=False)
        self.assertEqual(expired_root.status_code, 303)
        self.assertEqual(expired_root.headers["location"], "/login")

    def test_login_rejects_wrong_origin_non_json_and_invalid_credentials_without_echoing_them(self):
        wrong_origin = self.login(origin="https://testserver/")
        non_json = self.client.post(
            "/api/login",
            headers={"Origin": "https://testserver"},
            content="username=admin&password=do-not-echo-this",
        )
        invalid = self.login(password="do-not-echo-this")

        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(non_json.status_code, 415)
        self.assertEqual(invalid.status_code, 401)
        self.assertNotIn("do-not-echo-this", non_json.text)
        self.assertNotIn("do-not-echo-this", invalid.text)

    def test_login_schema_is_strict_and_rate_limited_per_client(self):
        extra_field = self.client.post(
            "/api/login",
            headers={"Origin": "https://testserver"},
            json={
                "username": "admin",
                "password": "web-secret",
                "remember_me": True,
            },
        )
        self.assertEqual(extra_field.status_code, 422)
        for _attempt in range(5):
            self.assertEqual(self.login(password="wrong-password").status_code, 401)
        limited = self.login(password="web-secret")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "60")

    def test_successful_password_authentication_resets_prior_failures(self):
        for _attempt in range(4):
            self.assertEqual(self.login(password="wrong-password").status_code, 401)
        self.assertEqual(self.client.get("/api/printers", auth=self.auth).status_code, 200)

        for _attempt in range(4):
            self.assertEqual(
                self.client.get(
                    "/api/printers",
                    auth=("admin", "wrong-password"),
                ).status_code,
                401,
            )
        self.assertEqual(self.login().status_code, 204)

    def test_missing_credentials_do_not_consume_the_password_rate_limit(self):
        for _attempt in range(8):
            root = self.client.get("/", follow_redirects=False)
            api = self.client.get("/api/printers")
            self.assertEqual(root.status_code, 303)
            self.assertEqual(api.status_code, 401)
        for _attempt in range(8):
            with self.client.websocket_connect(
                "/ws",
                headers={"Origin": "https://testserver"},
            ) as websocket:
                self.assertEqual(websocket.receive()["code"], 4401)
        self.assertEqual(self.login().status_code, 204)

    def test_invalid_basic_api_passwords_lock_even_correct_credentials(self):
        for _attempt in range(5):
            response = self.client.get(
                "/api/printers",
                auth=("admin", "wrong-password"),
            )
            self.assertEqual(response.status_code, 401)
        limited = self.client.get("/api/printers", auth=self.auth)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "60")

    def test_invalid_basic_page_passwords_share_the_password_lock(self):
        for _attempt in range(5):
            response = self.client.get(
                "/",
                auth=("admin", "wrong-password"),
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/login")
        limited = self.client.get("/", auth=self.auth, follow_redirects=False)
        self.assertEqual(limited.status_code, 429)

    def test_password_failures_are_shared_across_every_password_entry_point(self):
        self.assertEqual(self.login(password="wrong-password").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/printers",
                auth=("admin", "wrong-password"),
            ).status_code,
            401,
        )
        page = self.client.get(
            "/",
            auth=("admin", "wrong-password"),
            follow_redirects=False,
        )
        self.assertEqual(page.status_code, 303)

        wrong_token = base64.b64encode(b"admin:wrong-password").decode("ascii")
        with self.client.websocket_connect(
            "/ws",
            headers={
                "Authorization": f"Basic {wrong_token}",
                "Origin": "https://testserver",
            },
        ) as websocket:
            self.assertEqual(websocket.receive()["code"], 4401)

        self.assertEqual(self.login(password="wrong-password").status_code, 401)
        locked = self.client.get("/api/printers", auth=self.auth)
        self.assertEqual(locked.status_code, 429)

    def test_logout_requires_origin_and_csrf_then_revokes_the_session(self):
        self.assertEqual(self.login().status_code, 204)
        self.client.get("/")
        csrf = self.client.cookies["bambu_csrf"]

        rejected = self.client.post(
            "/api/logout",
            headers={
                "Origin": "https://testserver/",
                "X-CSRF-Token": csrf,
            },
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.client.get("/api/printers").status_code, 200)

        response = self.client.post(
            "/api/logout",
            headers={
                "Origin": "https://testserver",
                "X-CSRF-Token": csrf,
            },
        )
        self.assertEqual(response.status_code, 204)
        self.assertIn("bambu_session=", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/printers").status_code, 401)

    def test_websocket_accepts_the_secure_session_cookie(self):
        self.assertEqual(self.login().status_code, 204)
        session_cookie = self.client.cookies["bambu_session"]
        with self.client.websocket_connect(
            "/ws",
            headers={
                "Origin": "https://testserver",
                "Cookie": f"bambu_session={session_cookie}",
            },
        ) as websocket:
            message = websocket.receive_json()
        self.assertEqual(message["type"], "snapshot")
        self.assertEqual(message["data"]["printers"][0]["id"], "werkstatt")

    def test_session_websocket_closes_after_logout(self):
        self.assertEqual(self.login().status_code, 204)
        self.client.get("/")
        session_cookie = self.client.cookies["bambu_session"]
        csrf = self.client.cookies["bambu_csrf"]

        with self.client.websocket_connect(
            "/ws",
            headers={
                "Origin": "https://testserver",
                "Cookie": f"bambu_session={session_cookie}",
            },
        ) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "snapshot")
            logout = self.client.post(
                "/api/logout",
                headers={
                    "Origin": "https://testserver",
                    "X-CSRF-Token": csrf,
                },
            )
            self.assertEqual(logout.status_code, 204)
            closed = websocket.receive()

        self.assertEqual(closed["type"], "websocket.close")
        self.assertEqual(closed["code"], 4401)

    def test_session_websocket_closes_after_server_side_expiry(self):
        self.assertEqual(self.login().status_code, 204)
        session_cookie = self.client.cookies["bambu_session"]

        with self.client.websocket_connect(
            "/ws",
            headers={
                "Origin": "https://testserver",
                "Cookie": f"bambu_session={session_cookie}",
            },
        ) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "snapshot")
            self.session_clock[0] += 61
            closed = websocket.receive()

        self.assertEqual(closed["type"], "websocket.close")
        self.assertEqual(closed["code"], 4401)

    def test_session_is_revalidated_before_each_websocket_snapshot(self):
        self.client.app.state.websocket_revalidation_seconds = 60
        self.assertEqual(self.login().status_code, 204)
        session_cookie = self.client.cookies["bambu_session"]

        with self.client.websocket_connect(
            "/ws",
            headers={
                "Origin": "https://testserver",
                "Cookie": f"bambu_session={session_cookie}",
            },
        ) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "snapshot")
            self.session_store.revoke(session_cookie)
            self.client.app.state.hub.publish_threadsafe(self.manager.snapshot())
            closed = websocket.receive()

        self.assertEqual(closed["type"], "websocket.close")
        self.assertEqual(closed["code"], 4401)

    def test_command_requires_csrf_then_publishes(self):
        self.client.get("/", auth=self.auth)
        csrf = self.client.cookies["bambu_csrf"]
        response = self.client.post(
            "/api/printers/werkstatt/commands",
            auth=self.auth,
            headers={"Origin": "https://testserver", "X-CSRF-Token": csrf},
            json={"command": "pause", "params": {}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["acknowledged"])
        payloads = FakeMqttClient.instances["werkstatt"].payloads
        self.assertEqual(payloads[-1]["print"]["command"], "pause")

        without_csrf = self.client.post(
            "/api/printers/werkstatt/commands",
            auth=self.auth,
            json={"command": "pause", "params": {}},
        )
        self.assertEqual(without_csrf.status_code, 403)

    def test_command_body_is_bounded_after_auth_and_never_echoed(self):
        self.client.get("/", auth=self.auth)
        csrf = self.client.cookies["bambu_csrf"]
        headers = {
            "Origin": "https://testserver",
            "X-CSRF-Token": csrf,
            "Content-Type": "application/json",
        }
        secret = "do-not-echo-command-body"
        oversized = self.client.post(
            "/api/printers/werkstatt/commands",
            auth=self.auth,
            headers={**headers, "Content-Length": str(16 * 1024 + 1)},
            content=b"{}",
        )
        streamed = self.client.post(
            "/api/printers/werkstatt/commands",
            auth=self.auth,
            headers=headers,
            content=json.dumps(
                {"command": "pause", "params": {"note": secret * 1000}}
            ),
        )
        malformed = self.client.post(
            "/api/printers/werkstatt/commands",
            auth=self.auth,
            headers=headers,
            content=f'{{"command":"pause","{secret}":',
        )

        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(streamed.status_code, 413)
        self.assertEqual(malformed.status_code, 422)
        self.assertNotIn(secret, streamed.text + malformed.text)

    def test_unit_rfid_refresh_publishes_all_reported_slots(self):
        self.client.get("/", auth=self.auth)
        csrf = self.client.cookies["bambu_csrf"]
        response = self.client.post(
            "/api/printers/werkstatt/commands",
            auth=self.auth,
            headers={"Origin": "https://testserver", "X-CSRF-Token": csrf},
            json={"command": "refresh_rfid", "params": {"ams_id": 1}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        sent = FakeMqttClient.instances["werkstatt"].payloads[-2:]
        self.assertEqual([item["print"]["slot_id"] for item in sent], [0, 1])

    def test_rfid_refresh_rejects_a_slot_not_reported_by_the_ams(self):
        self.client.get("/", auth=self.auth)
        csrf = self.client.cookies["bambu_csrf"]
        response = self.client.post(
            "/api/printers/werkstatt/commands",
            auth=self.auth,
            headers={"Origin": "https://testserver", "X-CSRF-Token": csrf},
            json={
                "command": "refresh_rfid",
                "params": {"ams_id": 1, "slot_id": 7},
            },
        )

        self.assertEqual(response.status_code, 409)

    def test_commands_are_rate_limited_per_printer(self):
        self.client.get("/", auth=self.auth)
        csrf = self.client.cookies["bambu_csrf"]
        headers = {"Origin": "https://testserver", "X-CSRF-Token": csrf}
        first = self.client.post(
            "/api/printers/werkstatt/commands",
            auth=self.auth,
            headers=headers,
            json={"command": "pause", "params": {}},
        )
        second = self.client.post(
            "/api/printers/werkstatt/commands",
            auth=self.auth,
            headers=headers,
            json={"command": "light", "params": {"on": True}},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_missing_printer_ack_is_reported_as_a_timeout_not_success(self):
        worker = FakeMqttClient.instances["werkstatt"]
        worker.publish_and_wait = lambda payload, sequence_id, timeout=4.0: None
        self.client.get("/", auth=self.auth)
        csrf = self.client.cookies["bambu_csrf"]

        response = self.client.post(
            "/api/printers/werkstatt/commands",
            auth=self.auth,
            headers={"Origin": "https://testserver", "X-CSRF-Token": csrf},
            json={"command": "light", "params": {"on": True}},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["status"], "timeout")

    def test_health_and_metrics_are_loopback_probe_friendly(self):
        health = self.client.get("/healthz")
        metrics = self.client.get("/metrics")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assertIn("bambu_printer_online", metrics.text)
        self.assertNotIn("secret-code", metrics.text)

    def test_websocket_requires_same_origin_auth_and_sends_snapshot(self):
        token = base64.b64encode(b"admin:web-secret").decode("ascii")
        with self.client.websocket_connect(
            "/ws",
            headers={
                "Authorization": f"Basic {token}",
                "Origin": "https://testserver",
            },
        ) as websocket:
            message = websocket.receive_json()
        self.assertEqual(message["type"], "snapshot")
        self.assertEqual(message["data"]["printers"][0]["id"], "werkstatt")

    def test_invalid_basic_websocket_passwords_are_rate_limited_before_compare(self):
        wrong_token = base64.b64encode(b"admin:wrong-password").decode("ascii")
        for _attempt in range(5):
            with self.client.websocket_connect(
                "/ws",
                headers={
                    "Authorization": f"Basic {wrong_token}",
                    "Origin": "https://testserver",
                },
            ) as websocket:
                closed = websocket.receive()
            self.assertEqual(closed["type"], "websocket.close")
            self.assertEqual(closed["code"], 4401)

        correct_token = base64.b64encode(b"admin:web-secret").decode("ascii")
        with self.client.websocket_connect(
            "/ws",
            headers={
                "Authorization": f"Basic {correct_token}",
                "Origin": "https://testserver",
            },
        ) as websocket:
            limited = websocket.receive()
        self.assertEqual(limited["type"], "websocket.close")
        self.assertEqual(limited["code"], 4401)

    def test_root_sets_security_headers_and_secure_csrf_cookie(self):
        response = self.client.get("/", auth=self.auth)
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("Secure", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])


class CameraApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            printers=(
                printer_config(
                    model="X1 Carbon",
                    camera_enabled=True,
                    access_code="camera-access-secret",
                    allow_self_signed_tls=False,
                    tls_ca_file="/app/certs/bambu-lab-ca.pem",
                ),
            ),
            web=WebConfig(
                username="admin",
                password="web-secret",
                allowed_origins=("https://testserver",),
            ),
            audit_db=str(pathlib.Path(self.temp_dir.name) / "audit.sqlite3"),
        )
        self.audit = AuditLog(self.config.audit_db)
        self.manager = ControlManager(
            self.config,
            self.audit,
            client_factory=FakeMqttClient,
        )
        self.manager.store.apply_report(
            "werkstatt",
            {
                "print": full_status(
                    gcode_state="IDLE",
                    ipcam={
                        "ipcam_dev": "enable",
                        "liveview": {"local": "rtsps"},
                    },
                )
            },
        )
        self.camera_factory = FakeCameraProcessFactory(
            [
                [
                    b"--bambu_frame\r\nContent-Type: image/jpeg\r\n\r\nJPEG",
                    b"",
                ]
                for _index in range(8)
            ]
        )
        self.camera_manager = CameraStreamManager(
            limits=CameraLimits(
                max_total_sessions=2,
                startup_timeout_seconds=0.2,
                inactivity_timeout_seconds=0.2,
                max_session_seconds=10,
                max_restarts=0,
                terminate_timeout_seconds=0.2,
                frames_per_second=3,
                max_width=960,
            ),
            ffmpeg_path="/usr/bin/ffmpeg",
            process_factory=self.camera_factory,
        )
        self.client_context = TestClient(
            create_app(
                self.config,
                self.manager,
                supplied_camera_manager=self.camera_manager,
            ),
            base_url="https://testserver",
        )
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def login(self):
        response = self.client.post(
            "/api/login",
            headers={"Origin": "https://testserver"},
            json={"username": "admin", "password": "web-secret"},
        )
        self.assertEqual(response.status_code, 204)

    def issue_camera_ticket(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        csrf = self.client.cookies["bambu_csrf"]
        response = self.client.post(
            "/api/printers/werkstatt/camera/ticket",
            headers={
                "Origin": "https://testserver",
                "X-CSRF-Token": csrf,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["expires_in"], 30)
        self.assertIn("bambu_camera_ticket=", response.headers["set-cookie"])
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertNotIn("ticket", response.json())
        return self.client.cookies["bambu_camera_ticket"]

    def replace_printer_config(self, **changes):
        current = self.client.app.state.config
        self.client.app.state.config = replace(
            current,
            printers=(replace(current.printers[0], **changes),),
        )

    def test_camera_endpoints_require_web_session_and_same_origin(self):
        status_url = "/api/printers/werkstatt/camera/status"
        self.assertEqual(self.client.get(status_url).status_code, 401)
        self.assertEqual(
            self.client.get(status_url, auth=("admin", "web-secret")).status_code,
            401,
        )
        self.login()
        cross_site = self.client.get(
            status_url,
            headers={
                "Origin": "https://attacker.invalid",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        self.assertEqual(cross_site.status_code, 403)

    def test_camera_status_is_secret_free_and_reports_availability(self):
        self.login()

        response = self.client.get("/api/printers/werkstatt/camera/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "enabled": True,
                "supported": True,
                "online": True,
                "stale": False,
                "available": True,
                "active": False,
                "reason": "ready",
            },
        )
        self.assertNotIn("camera-access-secret", response.text)
        self.assertNotIn("rtsps://", response.text)

    def test_camera_stream_is_mjpeg_no_store_and_server_side_only(self):
        self.login()
        self.issue_camera_ticket()

        response = self.client.get("/api/printers/werkstatt/camera/stream")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("multipart/x-mixed-replace", response.headers["content-type"])
        self.assertIn("boundary=bambu_frame", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-store, private")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertIn(b"--bambu_frame", response.content)
        self.assertNotIn(b"camera-access-secret", response.content)
        self.assertTrue(self.camera_factory.processes[0].terminated)

    def test_stream_rejects_missing_wrong_and_reused_ticket(self):
        self.login()
        stream_url = "/api/printers/werkstatt/camera/stream"
        self.assertEqual(self.client.get(stream_url).status_code, 403)

        session = self.client.cookies["bambu_session"]
        wrong = self.client.get(
            stream_url,
            headers={
                "Cookie": (
                    f"bambu_session={session}; "
                    "bambu_camera_ticket=wrong-ticket-value-that-is-long-enough"
                )
            },
        )
        self.assertEqual(wrong.status_code, 403)

        ticket = self.issue_camera_ticket()
        first = self.client.get(stream_url)
        self.assertEqual(first.status_code, 200)
        reused = self.client.get(
            stream_url,
            headers={
                "Cookie": (
                    f"bambu_session={session}; bambu_camera_ticket={ticket}"
                )
            },
        )
        self.assertEqual(reused.status_code, 403)
        self.assertEqual(len(self.camera_factory.processes), 1)

    def test_ticket_requires_csrf_and_is_revoked_on_logout(self):
        self.login()
        ticket_url = "/api/printers/werkstatt/camera/ticket"
        self.assertEqual(self.client.post(ticket_url).status_code, 403)
        ticket = self.issue_camera_ticket()
        csrf = self.client.cookies["bambu_csrf"]
        logout = self.client.post(
            "/api/logout",
            headers={
                "Origin": "https://testserver",
                "X-CSRF-Token": csrf,
            },
        )
        self.assertEqual(logout.status_code, 204)

        self.login()
        new_session = self.client.cookies["bambu_session"]
        rejected = self.client.get(
            "/api/printers/werkstatt/camera/stream",
            headers={
                "Cookie": (
                    f"bambu_session={new_session}; bambu_camera_ticket={ticket}"
                )
            },
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.camera_factory.processes, [])

    def test_camera_stop_requires_csrf_and_is_idempotent(self):
        self.login()
        stop_url = "/api/printers/werkstatt/camera/stop"
        self.assertEqual(self.client.post(stop_url).status_code, 403)
        self.assertEqual(self.client.get("/").status_code, 200)
        csrf = self.client.cookies["bambu_csrf"]

        stopped = self.client.post(
            stop_url,
            headers={
                "Origin": "https://testserver",
                "X-CSRF-Token": csrf,
            },
        )

        self.assertEqual(stopped.status_code, 204)

    def test_unknown_disabled_unsupported_and_offline_streams_fail_closed(self):
        self.login()
        self.assertEqual(
            self.client.get("/api/printers/unknown/camera/status").status_code,
            404,
        )

        self.replace_printer_config(camera_enabled=False)
        disabled_status = self.client.get("/api/printers/werkstatt/camera/status")
        disabled_stream = self.client.get("/api/printers/werkstatt/camera/stream")
        self.assertEqual(disabled_status.json()["reason"], "disabled")
        self.assertEqual(disabled_stream.status_code, 403)

        self.replace_printer_config(camera_enabled=True, model="P1S")
        unsupported = self.client.get("/api/printers/werkstatt/camera/stream")
        self.assertEqual(unsupported.status_code, 409)

        self.replace_printer_config(camera_enabled=True, model="X1 Carbon")
        self.manager.store.mark_connection("werkstatt", "offline")
        offline_status = self.client.get("/api/printers/werkstatt/camera/status")
        offline_stream = self.client.get("/api/printers/werkstatt/camera/stream")
        self.assertEqual(offline_status.json()["reason"], "offline")
        self.assertEqual(offline_stream.status_code, 409)
        self.assertEqual(self.camera_factory.processes, [])

    def test_unverified_tls_and_missing_rtsps_fail_closed(self):
        self.login()
        status_url = "/api/printers/werkstatt/camera/status"
        ticket_url = "/api/printers/werkstatt/camera/ticket"
        self.assertEqual(self.client.get("/").status_code, 200)
        csrf = self.client.cookies["bambu_csrf"]
        headers = {"Origin": "https://testserver", "X-CSRF-Token": csrf}

        self.replace_printer_config(
            allow_self_signed_tls=True,
            tls_ca_file=None,
        )
        self.assertEqual(self.client.get(status_url).json()["reason"], "unsafe_tls")
        self.assertEqual(self.client.post(ticket_url, headers=headers).status_code, 409)

        self.replace_printer_config(
            allow_self_signed_tls=False,
            tls_ca_file="/app/certs/bambu-lab-ca.pem",
        )
        self.manager.store.apply_report(
            "werkstatt",
            {"print": {"command": "push_status", "ipcam": {"ipcam_dev": "enable", "liveview": {"local": "disabled"}}}},
        )
        missing = self.client.get(status_url)
        self.assertEqual(missing.json()["reason"], "rtsps_unavailable")
        self.assertFalse(missing.json()["available"])
        self.assertEqual(self.client.post(ticket_url, headers=headers).status_code, 409)


if __name__ == "__main__":
    unittest.main()
