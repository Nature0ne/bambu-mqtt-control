from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from .audit import AuditLog
from .commands import BuiltCommand, CommandBuilder, CommandError
from .config import AppConfig, PrinterConfig
from .mqtt_client import MqttPublishError, PrinterMqttClient
from .state import StateStore, drying_printer_policy

LOGGER = logging.getLogger(__name__)
DRYING_COMMANDS = frozenset({"start_drying", "stop_drying"})
CAMERA_COMMANDS = frozenset(
    {"camera_recording", "camera_timelapse", "camera_resolution"}
)
SIGNED_PRINT_COMMANDS = frozenset(
    {
        "pause",
        "resume",
        "stop",
        "speed",
        "refresh_rfid",
        "start_drying",
        "stop_drying",
    }
)


class PrinterNotFound(KeyError):
    pass


class CommandForbidden(PermissionError):
    pass


class CommandUnavailable(RuntimeError):
    pass


class CommandRateLimited(CommandUnavailable):
    pass


class ControlManager:
    def __init__(
        self,
        config: AppConfig,
        audit: AuditLog,
        client_factory: Callable[[PrinterConfig, StateStore, Callable[[], None]], PrinterMqttClient] = PrinterMqttClient,
        drying_confirmation_timeout: float = 30.0,
    ):
        if drying_confirmation_timeout < 0:
            raise ValueError("drying confirmation timeout cannot be negative")
        self.config = config
        self.audit = audit
        self.store = StateStore(config.printers)
        self.command_builder = CommandBuilder()
        self._configs = {printer.id: printer for printer in config.printers}
        self._on_change: Callable[[], None] = lambda: None
        self._workers = {
            printer.id: client_factory(printer, self.store, self._notify)
            for printer in config.printers
        }
        self._command_locks = {printer.id: threading.Lock() for printer in config.printers}
        self._last_command_at = {printer.id: 0.0 for printer in config.printers}
        self._drying_confirmation_timeout = drying_confirmation_timeout

    def set_on_change(self, callback: Callable[[], None]) -> None:
        self._on_change = callback

    def _notify(self) -> None:
        self._on_change()

    def start(self, *, strict: bool = False) -> None:
        started: list[Any] = []
        failures: list[str] = []
        for printer_id, worker in self._workers.items():
            try:
                worker.start()
                started.append(worker)
            except Exception:
                LOGGER.exception("Failed to start MQTT worker for %s", printer_id)
                self.store.mark_connection(printer_id, "error", "MQTT-Client konnte nicht starten")
                self._notify()
                failures.append(printer_id)
        if strict and failures:
            for worker in started:
                try:
                    worker.stop()
                except Exception:
                    LOGGER.exception("Failed to roll back an MQTT worker start")
            raise RuntimeError("one or more MQTT workers could not be started")

    def stop(self) -> None:
        for printer_id, worker in self._workers.items():
            try:
                worker.stop()
            except Exception:
                LOGGER.exception("Failed to stop MQTT worker for %s", printer_id)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return self.store.snapshot()

    def _validate_state(self, printer_id: str, command: str) -> None:
        printer = next(
            item for item in self.snapshot()["printers"] if item["id"] == printer_id
        )
        if not printer["online"] or printer["stale"]:
            raise CommandUnavailable("Drucker ist offline oder die Daten sind veraltet")
        if printer["connection_state"] == "partial":
            raise CommandUnavailable("Vollständiger Druckerstatus steht noch aus")
        # Current printer firmware requires signed MQTT commands whenever the
        # Developer LAN mode flag is absent or disabled. Sending an unsigned
        # print command can otherwise look successful while doing nothing.
        # stop_drying is the sole fail-safe exception: it cannot initiate heat
        # or motion and may still stop an already active dryer on mixed/older
        # firmware. Its dedicated post-command state confirmation remains
        # mandatory below.
        if (
            command in SIGNED_PRINT_COMMANDS
            and command != "stop_drying"
            and printer["state"].get("developer_lan_mode") is not True
        ):
            raise CommandUnavailable(
                "Developer-LAN-Modus muss für diesen Druckerbefehl bestätigt sein"
            )
        status = str(printer["state"].get("status") or "unknown").lower()
        printing = {"running", "printing", "prepare", "slicing"}
        paused = {"pause", "paused"}
        if command == "pause" and status not in printing:
            raise CommandUnavailable("Pause ist nur während eines aktiven Drucks möglich")
        if command == "resume" and status not in paused:
            raise CommandUnavailable("Fortsetzen ist nur bei pausiertem Druck möglich")
        if command == "stop" and status not in printing | paused:
            raise CommandUnavailable("Es läuft kein abbrechbarer Druck")
        if command == "speed" and status not in printing | paused:
            raise CommandUnavailable("Druckgeschwindigkeit ist nur während eines Drucks änderbar")
        if command == "start_drying" and status != "idle":
            raise CommandUnavailable("AMS-Trocknung kann nur bei untätigem Drucker gestartet werden")
        if command in CAMERA_COMMANDS:
            camera = printer["state"].get("camera") or {}
            if camera.get("available") is not True:
                raise CommandUnavailable(
                    "Der Drucker hat keine steuerbare Kamera gemeldet"
                )

    def _printer_snapshot(self, printer_id: str) -> dict[str, Any]:
        return next(
            item for item in self.snapshot()["printers"] if item["id"] == printer_id
        )

    def _validate_drying_command(
        self,
        printer_id: str,
        command: str,
        params: Mapping[str, Any],
        built: BuiltCommand,
    ) -> None:
        printer = self._printer_snapshot(printer_id)
        policy = drying_printer_policy(str(printer.get("model") or ""))
        if policy == "blocked":
            raise CommandUnavailable(
                "Remote-Trocknung ist auf P1- und A1-Druckern nicht verfügbar"
            )
        if policy == "unsupported":
            raise CommandUnavailable(
                "Dieses Druckermodell ist für Remote-Trocknung nicht freigegeben"
            )
        experimental_ack = params.get("experimental_ack", False)
        if not isinstance(experimental_ack, bool):
            raise CommandError("experimental_ack must be true or false")
        if (
            command == "start_drying"
            and policy == "experimental"
            and experimental_ack is not True
        ):
            raise CommandUnavailable(
                "Für dieses Druckermodell muss die experimentelle AMS-Trocknung ausdrücklich bestätigt werden"
            )

        print_payload = built.payload["print"]
        ams_id = int(print_payload["ams_id"])
        units = (printer["state"].get("ams") or {}).get("units") or []
        unit = next((item for item in units if item.get("id") == ams_id), None)
        if unit is None:
            raise CommandError("Die angeforderte AMS-ID wurde nicht gemeldet")
        if not unit.get("dry_capable"):
            model = str(unit.get("model") or "unknown")
            if model == "unknown":
                raise CommandUnavailable(
                    "Der AMS-Typ wurde noch nicht sicher erkannt"
                )
            raise CommandUnavailable(
                "Nur AMS 2 Pro und AMS HT unterstützen Remote-Trocknung"
            )
        drying = unit.get("drying") or {}
        remaining = drying.get("remaining_minutes")
        active = bool(drying.get("active"))
        if command == "start_drying":
            if printer["state"].get("developer_lan_mode") is not True:
                raise CommandUnavailable(
                    "Developer-LAN-Modus muss vom Drucker bestätigt sein"
                )
            if active or (isinstance(remaining, (int, float)) and remaining > 0):
                raise CommandUnavailable("Dieses AMS trocknet bereits")
            if self.store.ams_operation_risk(printer_id):
                raise CommandUnavailable(
                    "AMS-Trocknung ist gesperrt, solange Filament geladen oder das AMS in Bewegung ist"
                )
            if unit.get("model") == "AMS 2 Pro" and int(print_payload["temp"]) > 65:
                raise CommandError("temp must be between 45 and 65 for AMS 2 Pro")

    def _build_commands(
        self, printer_id: str, command: str, params: Mapping[str, Any]
    ) -> list[BuiltCommand]:
        if command in DRYING_COMMANDS:
            allowed_params = (
                {"ams_id", "temp", "duration", "rotate_tray", "filament", "experimental_ack"}
                if command == "start_drying"
                else {"ams_id", "experimental_ack"}
            )
            unexpected = set(params) - allowed_params
            if unexpected:
                raise CommandError(
                    f"unexpected parameters for {command}: {sorted(map(str, unexpected))}"
                )
            protocol_params = {
                key: value for key, value in params.items() if key != "experimental_ack"
            }
            built = self.command_builder.build(command, protocol_params)
            self._validate_drying_command(printer_id, command, params, built)
            return [built]
        if command == "refresh_rfid":
            try:
                raw_ams_id = params.get("ams_id")
                if isinstance(raw_ams_id, bool):
                    raise TypeError
                numeric_ams_id = float(raw_ams_id)
                if not math.isfinite(numeric_ams_id) or not numeric_ams_id.is_integer():
                    raise ValueError
                ams_id = int(numeric_ams_id)
            except (TypeError, ValueError) as exc:
                raise CommandError("ams_id must be an integer") from exc
            printer = next(
                item for item in self.snapshot()["printers"] if item["id"] == printer_id
            )
            units = (printer["state"].get("ams") or {}).get("units") or []
            unit = next((item for item in units if item.get("id") == ams_id), None)
            reported_slot_ids = [
                slot.get("id") for slot in (unit or {}).get("slots", [])
            ]
            if not reported_slot_ids:
                raise CommandError("Das AMS meldet keine aktualisierbaren Slots")
            if "slot_id" in params:
                built = self.command_builder.build(command, params)
                requested_slot = built.payload["print"]["slot_id"]
                if requested_slot not in reported_slot_ids:
                    raise CommandError("Der angeforderte AMS-Slot wurde nicht gemeldet")
                return [built]
            return [
                self.command_builder.build(command, {"ams_id": ams_id, "slot_id": slot_id})
                for slot_id in reported_slot_ids
            ]
        if command == "camera_resolution":
            built = self.command_builder.build(command, params)
            resolution = built.payload["camera"]["resolution"]
            printer = self._printer_snapshot(printer_id)
            camera = printer["state"].get("camera") or {}
            supported = camera.get("resolution_supported") or []
            if resolution not in supported:
                raise CommandUnavailable(
                    "Diese Kameraauflösung wurde vom Drucker nicht gemeldet"
                )
            return [built]
        if command == "light":
            built = self.command_builder.build(command, params)
            node = built.payload["system"]["led_node"]
            printer = self._printer_snapshot(printer_id)
            lights = printer["state"].get("lights") or {}
            reported_nodes = {
                item.get("node")
                for item in (lights.get("nodes") or [])
                if isinstance(item, Mapping)
            }
            if node not in reported_nodes:
                raise CommandUnavailable(
                    "Dieser Lichtknoten wurde vom Drucker nicht gemeldet"
                )
            return [built]
        return [self.command_builder.build(command, params)]

    def send_command(
        self,
        printer_id: str,
        command: str,
        params: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        config = self._configs.get(printer_id)
        if config is None:
            raise PrinterNotFound(printer_id)
        canonical = command
        if not config.writable:
            self.audit.record(
                actor=actor,
                printer_id=printer_id,
                command=canonical,
                params=params,
                result="forbidden",
                detail="printer is read-only",
            )
            raise CommandForbidden("Schreibzugriff ist für diesen Drucker deaktiviert")
        if canonical == "start_drying" and "stop_drying" not in config.allowed_commands:
            self.audit.record(
                actor=actor,
                printer_id=printer_id,
                command=canonical,
                params=params,
                result="forbidden",
                detail="start_drying requires the stop_drying safety permission",
            )
            raise CommandForbidden(
                "AMS-Trocknung darf nur zusammen mit der Stop-Freigabe gestartet werden"
            )
        if canonical not in config.allowed_commands:
            self.audit.record(
                actor=actor,
                printer_id=printer_id,
                command=canonical,
                params=params,
                result="forbidden",
                detail="command is not allowlisted",
            )
            raise CommandForbidden("Dieser Befehl ist für den Drucker nicht freigegeben")
        try:
            self._validate_state(printer_id, canonical)
        except CommandUnavailable as exc:
            self.audit.record(
                actor=actor,
                printer_id=printer_id,
                command=canonical,
                params=params,
                result="rejected",
                detail=str(exc),
            )
            raise

        try:
            built_commands = self._build_commands(printer_id, canonical, params)
        except (CommandError, CommandUnavailable) as exc:
            self.audit.record(
                actor=actor,
                printer_id=printer_id,
                command=canonical,
                params=params,
                result="rejected",
                detail=str(exc),
            )
            raise

        with self._command_locks[printer_id]:
            now = time.monotonic()
            if now - self._last_command_at[printer_id] < 0.75:
                self.audit.record(
                    actor=actor,
                    printer_id=printer_id,
                    command=canonical,
                    params=params,
                    result="rate_limited",
                    detail="commands must be at least 750 ms apart",
                )
                raise CommandRateLimited("Bitte kurz warten, bevor ein weiterer Befehl gesendet wird")
            self._last_command_at[printer_id] = now
            try:
                acknowledgements: list[dict[str, Any] | None] = []
                dry_generation: int | None = None
                for built in built_commands:
                    worker = self._workers[printer_id]
                    if canonical in DRYING_COMMANDS:
                        dry_generation = self.store.ams_dry_generation(
                            printer_id,
                            int(built.payload["print"]["ams_id"]),
                        )
                    if hasattr(worker, "publish_and_wait"):
                        acknowledgement = worker.publish_and_wait(
                            built.payload, built.sequence_id
                        )
                    else:
                        worker.publish(built.payload)
                        acknowledgement = None
                    acknowledgements.append(acknowledgement)
            except MqttPublishError as exc:
                self.audit.record(
                    actor=actor,
                    printer_id=printer_id,
                    command=canonical,
                    params=params,
                    result="failed",
                    sequence_id=built_commands[0].sequence_id,
                    detail=str(exc),
                )
                raise CommandUnavailable("MQTT-Befehl konnte nicht gesendet werden") from exc

        failed_ack = next(
            (
                ack
                for ack in acknowledgements
                if ack is not None and str(ack.get("result", "")).lower() != "success"
            ),
            None,
        )
        if failed_ack is not None:
            reason = str(failed_ack.get("reason") or "Drucker hat den Befehl abgelehnt")[:300]
            self.audit.record(
                actor=actor,
                printer_id=printer_id,
                command=canonical,
                params=params,
                result="rejected",
                sequence_id=built_commands[0].sequence_id,
                detail=reason,
            )
            raise CommandUnavailable(reason)

        sequence_ids = [built.sequence_id for built in built_commands]
        acknowledged = all(ack is not None for ack in acknowledgements)
        if canonical in DRYING_COMMANDS:
            body = built_commands[0].payload["print"]
            if dry_generation is None:
                raise RuntimeError("missing AMS drying status generation")
            confirmed = self.store.wait_for_ams_drying_state(
                printer_id,
                int(body["ams_id"]),
                active=canonical == "start_drying",
                after_generation=dry_generation,
                timeout=self._drying_confirmation_timeout,
            )
            if not confirmed:
                self.audit.record(
                    actor=actor,
                    printer_id=printer_id,
                    command=canonical,
                    params=params,
                    result="unconfirmed",
                    sequence_id=sequence_ids[0],
                    detail="AMS dry_time did not reach the requested state",
                )
                return {
                    "ok": False,
                    "status": "unconfirmed",
                    "command": canonical,
                    "sequence_id": sequence_ids[0],
                    "sequence_ids": sequence_ids,
                    "acknowledged": acknowledged,
                    "detail": "Der Drucker hat geantwortet, aber den Trocknungszustand nicht bestätigt",
                }
            self.audit.record(
                actor=actor,
                printer_id=printer_id,
                command=canonical,
                params=params,
                result="confirmed",
                sequence_id=sequence_ids[0],
            )
            return {
                "ok": True,
                "status": "confirmed",
                "command": canonical,
                "sequence_id": sequence_ids[0],
                "sequence_ids": sequence_ids,
                "acknowledged": acknowledged,
                "detail": (
                    None
                    if acknowledged
                    else "Trocknungszustand bestätigt; separate MQTT-Antwort blieb aus"
                ),
            }
        self.audit.record(
            actor=actor,
            printer_id=printer_id,
            command=canonical,
            params=params,
            result="acknowledged" if acknowledged else "timeout",
            sequence_id=",".join(sequence_ids),
        )
        status = "acknowledged" if acknowledged else "timeout"
        return {
            "ok": acknowledged,
            "status": status,
            "command": canonical,
            "sequence_id": sequence_ids[0],
            "sequence_ids": sequence_ids,
            "acknowledged": acknowledged,
            "detail": None
            if acknowledged
            else "Befehl gesendet, aber vom Drucker nicht bestätigt",
        }
