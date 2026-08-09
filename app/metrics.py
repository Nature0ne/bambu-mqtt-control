from __future__ import annotations

from typing import Any


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**values: Any) -> str:
    return "{" + ",".join(f'{key}="{_escape(value)}"' for key, value in sorted(values.items())) + "}"


def _sample(lines: list[str], name: str, value: Any, **labels: Any) -> None:
    if value is None:
        return
    lines.append(f"{name}{_labels(**labels) if labels else ''} {value}")


def render_metrics(snapshot: dict[str, Any], audit_counts: dict[str, int]) -> str:
    lines = [
        "# HELP bambu_printer_info Configured local Bambu printer information",
        "# TYPE bambu_printer_info gauge",
        "# HELP bambu_printer_online Whether fresh MQTT data is available",
        "# TYPE bambu_printer_online gauge",
        "# HELP bambu_printer_stale Whether the latest MQTT report is stale",
        "# TYPE bambu_printer_stale gauge",
        "# HELP bambu_print_progress_percent Current print progress",
        "# TYPE bambu_print_progress_percent gauge",
        "# HELP bambu_print_remaining_minutes Estimated remaining print time",
        "# TYPE bambu_print_remaining_minutes gauge",
        "# HELP bambu_temperature_celsius Current printer temperature",
        "# TYPE bambu_temperature_celsius gauge",
        "# HELP bambu_temperature_target_celsius Target printer temperature",
        "# TYPE bambu_temperature_target_celsius gauge",
        "# HELP bambu_ams_environment AMS temperature or humidity",
        "# TYPE bambu_ams_environment gauge",
        "# HELP bambu_ams_slot_remaining_percent Reported filament remaining",
        "# TYPE bambu_ams_slot_remaining_percent gauge",
        "# HELP bambu_command_audit_total Audited command outcomes",
        "# TYPE bambu_command_audit_total counter",
    ]
    for printer in snapshot.get("printers", []):
        printer_id = printer.get("id", "unknown")
        model = printer.get("model", "unknown")
        _sample(lines, "bambu_printer_info", 1, printer=printer_id, model=model)
        _sample(lines, "bambu_printer_online", int(bool(printer.get("online"))), printer=printer_id)
        _sample(lines, "bambu_printer_stale", int(bool(printer.get("stale"))), printer=printer_id)
        state = printer.get("state") or {}
        _sample(lines, "bambu_print_progress_percent", state.get("progress"), printer=printer_id)
        _sample(lines, "bambu_print_remaining_minutes", state.get("remaining_minutes"), printer=printer_id)
        for component, values in (state.get("temperatures") or {}).items():
            values = values or {}
            _sample(lines, "bambu_temperature_celsius", values.get("current"), printer=printer_id, component=component)
            _sample(lines, "bambu_temperature_target_celsius", values.get("target"), printer=printer_id, component=component)
        for unit in ((state.get("ams") or {}).get("units") or []):
            ams_id = unit.get("id", "unknown")
            _sample(lines, "bambu_ams_environment", unit.get("temperature"), printer=printer_id, ams=ams_id, measurement="temperature_celsius")
            _sample(lines, "bambu_ams_environment", unit.get("humidity_percent"), printer=printer_id, ams=ams_id, measurement="humidity_percent")
            _sample(lines, "bambu_ams_environment", unit.get("humidity_index"), printer=printer_id, ams=ams_id, measurement="humidity_index")
            for slot in unit.get("slots") or []:
                _sample(
                    lines,
                    "bambu_ams_slot_remaining_percent",
                    slot.get("remaining_percent"),
                    printer=printer_id,
                    ams=ams_id,
                    slot=slot.get("id", "unknown"),
                )
    for result, count in sorted(audit_counts.items()):
        _sample(lines, "bambu_command_audit_total", count, result=result)
    return "\n".join(lines) + "\n"
