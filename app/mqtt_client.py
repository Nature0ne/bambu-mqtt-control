from __future__ import annotations

import hashlib
import json
import logging
import secrets
import socket
import ssl
import threading
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from .commands import get_version_payload, push_all_payload
from .config import PrinterConfig
from .state import StateStore

LOGGER = logging.getLogger(__name__)
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


class MqttPublishError(RuntimeError):
    pass


def _pinned_tls_context(config: PrinterConfig) -> ssl.SSLContext:
    """Build trust for exactly the probed leaf before MQTT sends credentials."""
    expected = config.tls_fingerprint_sha256
    if not config.allow_self_signed_tls or config.tls_ca_file or not expected:
        raise ssl.SSLError("self-signed TLS requires a certificate fingerprint")

    probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE
    with socket.create_connection((config.host, config.port), timeout=5) as connection:
        with probe.wrap_socket(connection, server_hostname=config.host) as transport:
            certificate = transport.getpeercert(binary_form=True)
    if not certificate or not secrets.compare_digest(
        hashlib.sha256(certificate).hexdigest(), expected
    ):
        raise ssl.SSLError("printer certificate fingerprint mismatch")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(certificate))
    if not hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
        raise ssl.SSLError("leaf certificate pinning is unavailable")
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


class PrinterMqttClient:
    def __init__(
        self,
        config: PrinterConfig,
        store: StateStore,
        on_state_change: Callable[[], None],
    ):
        self.config = config
        self.store = store
        self.on_state_change = on_state_change
        self.report_topic = f"device/{config.serial}/report"
        self.request_topic = f"device/{config.serial}/request"
        client_id = f"bctl-{config.id[:16]}-{secrets.token_hex(3)}"
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        self.client.username_pw_set("bblp", config.access_code)
        if not config.allow_self_signed_tls and config.tls_ca_file:
            context = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH,
                cafile=config.tls_ca_file,
            )
            # The printer certificate is validated against the configured CA,
            # but its hostname commonly does not match the LAN IP address.
            context.check_hostname = False
            if hasattr(ssl, "VERIFY_X509_STRICT"):
                context.verify_flags &= ~ssl.VERIFY_X509_STRICT
            self.client.tls_set_context(context)
            self._tls_context_configured = True
        else:
            # The leaf pin is established synchronously in start(), before
            # Paho opens a connection that could carry the LAN access code.
            self._tls_context_configured = False
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_connect_fail = self._on_connect_fail
        self._pending_lock = threading.Lock()
        self._pending: dict[
            str,
            tuple[threading.Event, dict[str, Any], str, str],
        ] = {}
        self._tls_failed = False
        self._pin_retry_timer: threading.Timer | None = None
        self._refresh_timer: threading.Timer | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        LOGGER.info("Starting local MQTT connection for printer %s", self.config.id)
        self.store.mark_connection(self.config.id, "connecting")
        self._stopping.clear()
        if not self._tls_context_configured:
            self._schedule_pin_retry(delay=0)
            return
        self._start_mqtt_loop()

    def _start_mqtt_loop(self) -> None:
        self.client.connect_async(self.config.host, self.config.port, keepalive=60)
        self.client.loop_start()

    def _start_pinned_connection(self) -> None:
        if self._stopping.is_set():
            return
        try:
            context = _pinned_tls_context(self.config)
        except (OSError, ssl.SSLError):
            LOGGER.warning("TLS pin probe failed for printer %s", self.config.id)
            self.store.mark_connection(
                self.config.id,
                "offline",
                "TLS-Zertifikat konnte noch nicht sicher bestätigt werden",
            )
            self.on_state_change()
            self._schedule_pin_retry(delay=10)
            return
        if self._stopping.is_set():
            return
        self.client.tls_set_context(context)
        self._tls_context_configured = True
        self._pin_retry_timer = None
        self._start_mqtt_loop()

    def _schedule_pin_retry(self, *, delay: float) -> None:
        if self._stopping.is_set():
            return
        timer = threading.Timer(delay, self._start_pinned_connection)
        timer.daemon = True
        self._pin_retry_timer = timer
        timer.start()

    def stop(self) -> None:
        self._stopping.set()
        timer = self._pin_retry_timer
        self._pin_retry_timer = None
        if timer is not None:
            timer.cancel()
        self._cancel_full_refresh()
        self.client.disconnect()
        self.client.loop_stop()
        self.store.mark_connection(self.config.id, "stopped")
        self.on_state_change()

    def _certificate_matches(self, client: mqtt.Client) -> bool:
        expected = (
            self.config.tls_fingerprint_sha256
            if self.config.allow_self_signed_tls
            else None
        )
        if not expected:
            return True
        sock = client.socket()
        if sock is None or not hasattr(sock, "getpeercert"):
            return False
        certificate = sock.getpeercert(binary_form=True)
        return bool(certificate) and hashlib.sha256(certificate).hexdigest() == expected

    def _on_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any) -> None:
        code = int(getattr(reason_code, "value", reason_code))
        if code != 0:
            message = f"MQTT-Anmeldung abgelehnt (Code {reason_code})"
            self.store.mark_connection(self.config.id, "error", message)
            self.on_state_change()
            return
        if not self._certificate_matches(client):
            LOGGER.error("TLS fingerprint mismatch for printer %s", self.config.id)
            self.store.mark_connection(self.config.id, "error", "TLS-Zertifikat stimmt nicht überein")
            self._tls_failed = True
            client.disconnect()
            self.on_state_change()
            return
        self._tls_failed = False
        client.subscribe(self.report_topic, qos=0)
        self.store.mark_connection(self.config.id, "online")
        try:
            self._publish(push_all_payload())
        except MqttPublishError:
            LOGGER.warning("Initial full-state request failed for %s", self.config.id)
        try:
            self._publish(get_version_payload())
        except MqttPublishError:
            LOGGER.warning("Initial version request failed for %s", self.config.id)
        self._schedule_full_refresh()
        self.on_state_change()

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: Any,
        reason_code: Any,
        _properties: Any,
    ) -> None:
        if self._tls_failed:
            return
        self._cancel_full_refresh()
        code = int(getattr(reason_code, "value", reason_code))
        detail = None if code == 0 else f"MQTT-Verbindung getrennt (Code {reason_code})"
        self.store.mark_connection(self.config.id, "offline", detail)
        self._fail_pending("connection lost")
        self.on_state_change()

    def _on_connect_fail(self, _client: mqtt.Client, _userdata: Any) -> None:
        self.store.mark_connection(self.config.id, "offline", "Drucker nicht erreichbar")
        self.on_state_change()

    def _cancel_full_refresh(self) -> None:
        timer = self._refresh_timer
        self._refresh_timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_full_refresh(self) -> None:
        self._cancel_full_refresh()
        if self.config.full_refresh_seconds == 0 or self._stopping.is_set():
            return
        timer = threading.Timer(self.config.full_refresh_seconds, self._refresh_full_state)
        timer.daemon = True
        self._refresh_timer = timer
        timer.start()

    def _refresh_full_state(self) -> None:
        if self._stopping.is_set():
            return
        try:
            if self.client.is_connected():
                self._publish(push_all_payload())
        except MqttPublishError:
            LOGGER.warning("Periodic full-state request failed for %s", self.config.id)
        finally:
            self._schedule_full_refresh()

    def _on_message(self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
        if message.topic != self.report_topic:
            return
        if len(message.payload) > MAX_PAYLOAD_BYTES:
            LOGGER.warning("Ignoring oversized MQTT report from %s", self.config.id)
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            LOGGER.warning("Ignoring malformed MQTT report from %s", self.config.id)
            return
        if not isinstance(payload, dict):
            return
        self._resolve_pending(payload)
        if self.store.apply_report(self.config.id, payload):
            self.on_state_change()

    def _resolve_pending(self, payload: dict[str, Any]) -> None:
        for section_name, section in payload.items():
            if not isinstance(section, dict) or "result" not in section:
                continue
            sequence = section.get("sequence_id", section.get("sequenceId"))
            if sequence is None:
                continue
            with self._pending_lock:
                pending = self._pending.get(str(sequence))
                if pending:
                    event, holder, expected_section, expected_command = pending
                    if section_name != expected_section:
                        continue
                    if str(section.get("command") or "") != expected_command:
                        continue
                    holder.update(section)
                    event.set()

    def _fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            for event, holder, _section, _command in self._pending.values():
                holder.update({"result": "failed", "reason": reason})
                event.set()

    def _publish(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        section_name, section = next(iter(payload.items()))
        command = str(section.get("command") or "") if isinstance(section, dict) else ""
        # Current Bambu Studio publishes user-facing print controls at QoS 1.
        # Telemetry refreshes and all other commands keep their protocol default.
        qos = 1 if section_name == "print" and command in {"pause", "resume", "stop"} else 0
        result = self.client.publish(self.request_topic, encoded, qos=qos, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise MqttPublishError(f"MQTT publish failed with code {result.rc}")

    def publish(self, payload: dict[str, Any]) -> None:
        if not self.client.is_connected():
            raise MqttPublishError("printer is offline")
        self._publish(payload)

    def publish_and_wait(
        self,
        payload: dict[str, Any],
        sequence_id: str,
        timeout: float = 4.0,
    ) -> dict[str, Any] | None:
        event = threading.Event()
        holder: dict[str, Any] = {}
        expected_section, command_body = next(iter(payload.items()))
        expected_command = str(command_body.get("command") or "")
        with self._pending_lock:
            self._pending[sequence_id] = (
                event,
                holder,
                expected_section,
                expected_command,
            )
        try:
            self.publish(payload)
            if not event.wait(timeout):
                return None
            return dict(holder)
        finally:
            with self._pending_lock:
                self._pending.pop(sequence_id, None)
