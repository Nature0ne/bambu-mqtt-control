import pathlib
import tempfile
import unittest

from app.audit import AuditLog
from app.commands import LIGHT_NODES, CommandBuilder, CommandError
from app.config import AppConfig, PrinterConfig, WebConfig
from app.manager import CommandUnavailable, ControlManager
from app.state import normalise_report


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
        "lights_report": [],
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
        self.payloads = []

    def start(self):
        self.store.mark_connection(self.config.id, "online")

    def stop(self):
        self.store.mark_connection(self.config.id, "stopped")

    def publish_and_wait(self, payload, sequence_id, timeout=4.0):
        self.payloads.append(payload)
        return {"sequence_id": sequence_id, "result": "success"}


class LightNodeTests(unittest.TestCase):
    def test_builder_supports_each_known_node_with_an_exact_system_payload(self):
        builder = CommandBuilder(start=42)
        for node in sorted(LIGHT_NODES):
            with self.subTest(node=node):
                command = builder.build("light", {"node": node, "on": True})
                self.assertEqual(command.payload["system"]["command"], "ledctrl")
                self.assertEqual(command.payload["system"]["led_node"], node)
                self.assertEqual(command.payload["system"]["led_mode"], "on")

    def test_builder_defaults_to_chamber_and_rejects_unknown_or_non_boolean_input(self):
        builder = CommandBuilder()
        self.assertEqual(
            builder.build("light", {"on": False}).payload["system"]["led_node"],
            "chamber_light",
        )
        for params in (
            {"on": True, "node": "evil_light"},
            {"on": True, "node": 1},
            {"on": "true", "node": "chamber_light"},
        ):
            with self.subTest(params=params), self.assertRaises(CommandError):
                builder.build("light", params)

    def test_state_exposes_only_allowlisted_reported_nodes_and_keeps_legacy_fields(self):
        lights = normalise_report(
            {
                "print": {
                    "lights_report": [
                        {"node": "chamber_light", "mode": "on"},
                        {"led_node": "chamber_light2", "led_mode": "flashing"},
                        {"node": "work_light", "mode": "off"},
                        {"node": "heatbed_light", "mode": "broken"},
                        {"node": "network_password", "mode": "secret"},
                    ]
                }
            }
        )["lights"]

        self.assertEqual(lights["chamber"], "on")
        self.assertEqual(lights["work"], "off")
        self.assertEqual(
            [item["node"] for item in lights["nodes"]],
            [
                "chamber_light",
                "chamber_light2",
                "work_light",
                "heatbed_light",
            ],
        )
        self.assertEqual(lights["nodes"][-1]["mode"], "unknown")
        self.assertNotIn("network_password", repr(lights))

    def test_state_uses_latest_duplicate_report_and_never_invents_nodes(self):
        lights = normalise_report(
            {
                "print": {
                    "lights_report": [
                        {"node": "chamber_light", "mode": "off"},
                        {"node": "chamber_light", "mode": "on"},
                    ]
                }
            }
        )["lights"]
        self.assertEqual(lights["chamber"], "on")
        self.assertEqual(len(lights["nodes"]), 1)
        self.assertEqual(normalise_report({"print": {}})["lights"]["nodes"], [])

    def test_manager_only_controls_nodes_confirmed_by_current_printer_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            printer = PrinterConfig(
                id="x1c",
                name="X1C",
                host="192.0.2.10",
                serial="01S00A000000000",
                access_code="secret",
                model="X1 Carbon",
                writable=True,
                allowed_commands=frozenset({"light"}),
                allow_self_signed_tls=True,
            )
            config = AppConfig(
                printers=(printer,),
                web=WebConfig("admin", "web-secret", ("https://testserver",)),
                audit_db=str(pathlib.Path(temp_dir) / "audit.sqlite3"),
            )
            manager = ControlManager(
                config,
                AuditLog(config.audit_db),
                client_factory=FakeMqttClient,
            )
            manager.start()
            manager.store.apply_report(
                printer.id,
                {
                    "print": full_status(
                        lights_report=[{"node": "work_light", "mode": "off"}]
                    )
                },
            )

            result = manager.send_command(
                printer.id,
                "light",
                {"node": "work_light", "on": True},
                "admin",
            )
            self.assertEqual(result["status"], "acknowledged")
            payload = manager._workers[printer.id].payloads[-1]  # noqa: SLF001
            self.assertEqual(payload["system"]["led_node"], "work_light")

            with self.assertRaises(CommandUnavailable):
                manager.send_command(
                    printer.id,
                    "light",
                    {"node": "chamber_light", "on": True},
                    "admin",
                )


if __name__ == "__main__":
    unittest.main()
