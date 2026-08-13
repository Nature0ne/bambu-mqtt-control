import unittest

from app.config import PrinterConfig
from app.state import StateStore, normalise_report


def printer_config(**overrides):
    values = {
        "id": "x1c",
        "name": "X1C",
        "host": "192.0.2.10",
        "serial": "01S00A000000000",
        "access_code": "not-public",
        "model": "X1 Carbon",
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
        "lights_report": [],
        "hms": [],
        "print_error": 0,
        "cooling_fan_speed": "0",
        "nozzle_target_temper": 0,
        "bed_target_temper": 0,
    }
    values.update(overrides)
    return values


class StateDiagnosticsTests(unittest.TestCase):
    def test_normalises_useful_device_diagnostics(self):
        state = normalise_report(
            {
                "print": {
                    "wifi_signal": " -57 dBm ",
                    "home_flag": 0x00800100,
                    "stg_cur": 7,
                    "mc_print_stage": "12",
                    "mc_print_sub_stage": 3,
                    "hms": [
                        {"attr": 0x07000001, "code": 0x00020002},
                        {"attr": 0x07000001, "code": 0x00020002},
                    ],
                },
                "_bambu_control_printer_model": "X1 Carbon",
                "_bambu_control_version_modules": [
                    {"name": "ota", "software": "01.08.00.00", "hardware": "AP05"},
                    {"name": "n3f/0", "software": "00.00.06.40", "hardware": None},
                ],
            }
        )

        diagnostics = state["diagnostics"]
        self.assertEqual(diagnostics["wifi_signal_dbm"], -57)
        self.assertTrue(diagnostics["door_open"])
        self.assertEqual(diagnostics["sd_card"], {"present": True, "status": "normal"})
        self.assertEqual(
            diagnostics["print_stage"],
            {"phase_id": 7, "stage_id": 12, "substage_id": 3},
        )
        self.assertEqual(
            diagnostics["firmware"]["printer"],
            {"software": "01.08.00.00", "hardware": "AP05"},
        )
        self.assertEqual(diagnostics["hms"]["count"], 1)
        self.assertEqual(diagnostics["hms"]["items"][0]["module"], "ams")
        self.assertEqual(diagnostics["hms"]["items"][0]["severity"], "serious")

    def test_diagnostic_strings_are_strictly_bounded_and_not_reflected(self):
        secret = "secret value with spaces and <script>"
        state = normalise_report(
            {
                "print": {"wifi_signal": secret},
                "_bambu_control_version_modules": [
                    {"name": "ota", "software": secret, "hardware": "safe-1"},
                    {"name": secret, "software": "1.2.3"},
                    {"name": "toolhead", "software": "x" * 65},
                ],
            }
        )

        diagnostics = state["diagnostics"]
        self.assertIsNone(diagnostics["wifi_signal_dbm"])
        self.assertEqual(diagnostics["firmware"]["printer"]["software"], None)
        self.assertEqual(diagnostics["firmware"]["printer"]["hardware"], "safe-1")
        self.assertNotIn(secret, repr(diagnostics))

    def test_sd_card_precedence_and_unknown_values_fail_closed(self):
        abnormal = normalise_report(
            {"print": {"home_flag": 0x200, "sdcard": False}}
        )["diagnostics"]["sd_card"]
        direct = normalise_report({"print": {"sdcard": "on"}})["diagnostics"]["sd_card"]
        missing = normalise_report({"print": {"home_flag": 0}})["diagnostics"]["sd_card"]
        unknown = normalise_report({"print": {}})["diagnostics"]["sd_card"]

        self.assertEqual(abnormal, {"present": True, "status": "abnormal"})
        self.assertEqual(direct, {"present": True, "status": "normal"})
        self.assertEqual(missing, {"present": False, "status": "missing"})
        self.assertEqual(unknown, {"present": None, "status": "unknown"})

    def test_door_sensor_uses_model_specific_bits_and_explicit_value_wins(self):
        h2 = normalise_report(
            {
                "print": {"stat": "00800000"},
                "_bambu_control_printer_model": "H2D",
            }
        )["diagnostics"]["door_open"]
        x1 = normalise_report(
            {
                "print": {"home_flag": 0x00800000},
                "_bambu_control_printer_model": "X1C",
            }
        )["diagnostics"]["door_open"]
        explicit = normalise_report(
            {
                "print": {"door_open": "off", "home_flag": 0x00800000},
                "_bambu_control_printer_model": "X1C",
            }
        )["diagnostics"]["door_open"]
        unsupported = normalise_report(
            {
                "print": {"home_flag": 0x00800000},
                "_bambu_control_printer_model": "P1S",
            }
        )["diagnostics"]["door_open"]

        self.assertTrue(h2)
        self.assertTrue(x1)
        self.assertFalse(explicit)
        self.assertIsNone(unsupported)

    def test_invalid_numeric_diagnostics_are_discarded(self):
        diagnostics = normalise_report(
            {
                "print": {
                    "wifi_signal": 1,
                    "stg_cur": -1,
                    "mc_print_stage": 255,
                    "mc_print_sub_stage": 1.5,
                    "hms": [
                        {"attr": -1, "code": 1},
                        {"attr": 1, "code": 0x1_0000_0000},
                        {"attr": True, "code": 2},
                    ],
                }
            }
        )["diagnostics"]

        self.assertIsNone(diagnostics["wifi_signal_dbm"])
        self.assertEqual(
            diagnostics["print_stage"],
            {"phase_id": None, "stage_id": None, "substage_id": None},
        )
        self.assertEqual(diagnostics["hms"], {"count": 0, "items": []})

    def test_get_version_metadata_survives_separate_status_reports(self):
        config = printer_config()
        store = StateStore((config,))
        store.apply_report(
            config.id,
            {
                "info": {
                    "command": "get_version",
                    "module": [
                        {
                            "name": "ota",
                            "sw_ver": "01.08.02.00",
                            "hw_ver": "AP05",
                            "sn": "must-not-leak",
                        }
                    ],
                }
            },
        )
        store.apply_report(
            config.id,
            {"print": full_status(wifi_signal="-61dBm", home_flag=0x100)},
        )

        diagnostics = store.snapshot()["printers"][0]["state"]["diagnostics"]
        self.assertEqual(diagnostics["wifi_signal_dbm"], -61)
        self.assertEqual(
            diagnostics["firmware"]["printer"],
            {"software": "01.08.02.00", "hardware": "AP05"},
        )
        self.assertNotIn("must-not-leak", repr(diagnostics))


if __name__ == "__main__":
    unittest.main()
