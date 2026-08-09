from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import PrinterConfig

CAMERA_BOUNDARY = "bambu_frame"
CAMERA_MEDIA_TYPE = f"multipart/x-mixed-replace; boundary={CAMERA_BOUNDARY}"
CAMERA_TICKET_COOKIE = "bambu_camera_ticket"
CAMERA_TICKET_TTL_SECONDS = 30
MAX_CAMERA_TICKETS = 256
MAX_LAUNCH_PAYLOAD_BYTES = 4096
READ_CHUNK_BYTES = 64 * 1024
SAFE_CAMERA_HOST = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


class CameraError(RuntimeError):
    """Base class for camera failures safe to expose without credentials."""


class CameraBusy(CameraError):
    pass


class CameraUnavailable(CameraError):
    pass


class CameraTicketRejected(CameraError):
    pass


@dataclass(frozen=True)
class CameraLimits:
    max_total_sessions: int = 2
    startup_timeout_seconds: float = 12.0
    inactivity_timeout_seconds: float = 20.0
    max_session_seconds: float = 10 * 60.0
    max_restarts: int = 1
    terminate_timeout_seconds: float = 2.0
    frames_per_second: int = 3
    max_width: int = 960

    def __post_init__(self) -> None:
        if (
            self.max_total_sessions < 1
            or self.startup_timeout_seconds <= 0
            or self.inactivity_timeout_seconds <= 0
            or self.max_session_seconds <= 0
            or self.max_restarts < 0
            or self.terminate_timeout_seconds <= 0
            or not 1 <= self.frames_per_second <= 10
            or not 320 <= self.max_width <= 1920
        ):
            raise ValueError("invalid camera resource limits")


def camera_model_supported(model: str) -> bool:
    canonical = re.sub(r"[^a-z0-9]", "", str(model or "").lower())
    return canonical.startswith("x1")


def camera_tls_supported(config: PrinterConfig) -> bool:
    return bool(config.tls_ca_file and not config.allow_self_signed_tls)


def _url_host(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not SAFE_CAMERA_HOST.fullmatch(host):
            raise CameraUnavailable("Ungültige Kameraadresse")
        return host
    return f"[{address}]" if address.version == 6 else str(address)


def camera_input_url(config: PrinterConfig) -> str:
    password = quote(config.access_code, safe="")
    return (
        f"rtsps://bblp:{password}@{_url_host(config.host)}:322"
        "/streaming/live/1"
    )


def ffmpeg_arguments(
    config: PrinterConfig,
    input_url: str,
    limits: CameraLimits,
    ffmpeg_path: str,
) -> list[str]:
    if not camera_tls_supported(config):
        raise CameraUnavailable(
            "Kamerastream erfordert eine überprüfte TLS-Verbindung"
        )
    arguments = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "quiet",
        "-nostdin",
        "-rtsp_transport",
        "tcp",
        "-timeout",
        "10000000",
    ]
    arguments.extend(
        [
            "-tls_verify",
            "1",
            "-ca_file",
            config.tls_ca_file,
            "-verifyhost",
            config.serial,
        ]
    )
    arguments.extend(
        [
            "-i",
            input_url,
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-filter_threads",
            "1",
            "-threads",
            "1",
            "-vf",
            (
                f"fps={limits.frames_per_second},"
                f"scale=w=min({limits.max_width}\\,iw):h=-2:flags=fast_bilinear"
            ),
            "-c:v",
            "mjpeg",
            "-q:v",
            "6",
            "-f",
            "mpjpeg",
            "-boundary_tag",
            CAMERA_BOUNDARY,
            "pipe:1",
        ]
    )
    return arguments


ProcessFactory = Callable[..., Awaitable[Any]]
AuthorizationCheck = Callable[[], bool]


class CameraStreamManager:
    def __init__(
        self,
        *,
        limits: CameraLimits | None = None,
        ffmpeg_path: str | None = None,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits or CameraLimits()
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        if not Path(self.ffmpeg_path).is_absolute():
            raise ValueError("ffmpeg path must be absolute")
        self._process_factory = process_factory
        self._clock = clock
        self._lock = asyncio.Lock()
        self._sessions: dict[str, CameraStream] = {}
        self._sessions_by_printer: dict[str, str] = {}
        self._tickets: dict[bytes, tuple[str, str, float]] = {}

    def _prune_tickets_locked(self, now: float) -> None:
        for digest, (_printer_id, _session_key, expires_at) in tuple(
            self._tickets.items()
        ):
            if expires_at <= now:
                self._tickets.pop(digest, None)

    async def issue_ticket(self, printer_id: str, session_key: str) -> tuple[str, int]:
        now = self._clock()
        async with self._lock:
            self._prune_tickets_locked(now)
            for digest, (bound_printer, bound_session, _expiry) in tuple(
                self._tickets.items()
            ):
                if bound_printer == printer_id and bound_session == session_key:
                    self._tickets.pop(digest, None)
            if len(self._tickets) >= MAX_CAMERA_TICKETS:
                oldest = min(self._tickets, key=lambda item: self._tickets[item][2])
                self._tickets.pop(oldest, None)
            ticket = secrets.token_urlsafe(32)
            digest = hashlib.sha256(ticket.encode("utf-8")).digest()
            self._tickets[digest] = (
                printer_id,
                session_key,
                now + CAMERA_TICKET_TTL_SECONDS,
            )
        return ticket, CAMERA_TICKET_TTL_SECONDS

    async def consume_ticket(
        self,
        ticket: str | None,
        *,
        printer_id: str,
        session_key: str,
    ) -> bool:
        if ticket is None or not 24 <= len(ticket) <= 256:
            return False
        digest = hashlib.sha256(ticket.encode("utf-8")).digest()
        now = self._clock()
        async with self._lock:
            self._prune_tickets_locked(now)
            binding = self._tickets.pop(digest, None)
        return bool(
            binding is not None
            and binding[0] == printer_id
            and secrets.compare_digest(binding[1], session_key)
            and binding[2] > now
        )

    async def revoke_tickets(self, session_key: str) -> None:
        async with self._lock:
            for digest, (_printer_id, bound_session, _expiry) in tuple(
                self._tickets.items()
            ):
                if secrets.compare_digest(bound_session, session_key):
                    self._tickets.pop(digest, None)

    async def revoke_printer_tickets(self, printer_id: str) -> None:
        async with self._lock:
            for digest, (bound_printer, _bound_session, _expiry) in tuple(
                self._tickets.items()
            ):
                if bound_printer == printer_id:
                    self._tickets.pop(digest, None)

    async def open_stream(
        self,
        config: PrinterConfig,
        *,
        session_key: str,
        is_authorized: AuthorizationCheck,
    ) -> CameraStream:
        self._validate_stream_request(config)
        if not is_authorized():
            raise CameraUnavailable("Web-Sitzung ist nicht mehr gültig")

        async with self._lock:
            stream = self._reserve_stream_locked(
                config,
                session_key=session_key,
                is_authorized=is_authorized,
            )
        return await self._start_reserved_stream(stream)

    async def open_ticketed_stream(
        self,
        config: PrinterConfig,
        *,
        ticket: str | None,
        session_key: str,
        is_authorized: AuthorizationCheck,
    ) -> CameraStream:
        self._validate_stream_request(config)
        if ticket is None or not 24 <= len(ticket) <= 256:
            raise CameraTicketRejected("Kameraticket ist ungültig oder abgelaufen")
        digest = hashlib.sha256(ticket.encode("utf-8")).digest()
        now = self._clock()

        # Consuming the one-time ticket, revalidating the session and
        # reserving the per-printer slot are one transaction. Stop/logout can
        # therefore never finish between authorization and registration.
        async with self._lock:
            self._prune_tickets_locked(now)
            binding = self._tickets.pop(digest, None)
            if not (
                binding is not None
                and binding[0] == config.id
                and secrets.compare_digest(binding[1], session_key)
                and binding[2] > now
                and is_authorized()
            ):
                raise CameraTicketRejected(
                    "Kameraticket ist ungültig oder abgelaufen"
                )
            stream = self._reserve_stream_locked(
                config,
                session_key=session_key,
                is_authorized=is_authorized,
            )
        return await self._start_reserved_stream(stream)

    @staticmethod
    def _validate_stream_request(config: PrinterConfig) -> None:
        if config.camera_enabled is not True:
            raise CameraUnavailable("Kamera ist nicht freigegeben")
        if not camera_model_supported(config.model):
            raise CameraUnavailable("Dieses Druckermodell unterstützt den Kamerastream nicht")
        if not camera_tls_supported(config):
            raise CameraUnavailable(
                "Kamerastream erfordert eine überprüfte TLS-Verbindung"
            )

    def _reserve_stream_locked(
        self,
        config: PrinterConfig,
        *,
        session_key: str,
        is_authorized: AuthorizationCheck,
    ) -> CameraStream:
        stream_id = secrets.token_urlsafe(18)
        if config.id in self._sessions_by_printer:
            raise CameraBusy("Für diesen Drucker läuft bereits ein Kamerastream")
        if len(self._sessions) >= self.limits.max_total_sessions:
            raise CameraBusy("Maximale Anzahl paralleler Kamerastreams erreicht")
        stream = CameraStream(
            manager=self,
            stream_id=stream_id,
            config=config,
            session_key=session_key,
            is_authorized=is_authorized,
        )
        self._sessions[stream_id] = stream
        self._sessions_by_printer[config.id] = stream_id
        return stream

    async def _start_reserved_stream(self, stream: CameraStream) -> CameraStream:
        try:
            await stream.start()
        except BaseException:
            await stream.close()
            raise
        return stream

    async def is_active(self, printer_id: str) -> bool:
        async with self._lock:
            return printer_id in self._sessions_by_printer

    async def close_stream(
        self,
        printer_id: str,
        session_key: str | None = None,
    ) -> bool:
        async with self._lock:
            stream_id = self._sessions_by_printer.get(printer_id)
            stream = self._sessions.get(stream_id) if stream_id is not None else None
            if stream is None:
                return False
            if session_key is not None and not secrets.compare_digest(
                stream.session_key, session_key
            ):
                return False
        await stream.close()
        return True

    async def close_session(self, session_key: str) -> None:
        async with self._lock:
            streams = tuple(
                stream
                for stream in self._sessions.values()
                if secrets.compare_digest(stream.session_key, session_key)
            )
        if streams:
            await asyncio.gather(
                *(stream.close() for stream in streams),
                return_exceptions=True,
            )

    async def close(self) -> None:
        async with self._lock:
            sessions = tuple(self._sessions.values())
        if sessions:
            await asyncio.gather(
                *(session.close() for session in sessions),
                return_exceptions=True,
            )

    async def _release(self, stream: CameraStream) -> None:
        async with self._lock:
            self._sessions.pop(stream.stream_id, None)
            if self._sessions_by_printer.get(stream.config.id) == stream.stream_id:
                self._sessions_by_printer.pop(stream.config.id, None)

    async def _spawn(self, config: PrinterConfig) -> Any:
        arguments = ffmpeg_arguments(
            config,
            camera_input_url(config),
            self.limits,
            self.ffmpeg_path,
        )
        encoded = json.dumps(
            {"executable": self.ffmpeg_path, "arguments": arguments[1:]},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_LAUNCH_PAYLOAD_BYTES:
            raise CameraUnavailable("Kamerakonfiguration ist zu groß")

        read_fd, write_fd = os.pipe()
        try:
            written = os.write(write_fd, encoded)
            if written != len(encoded):
                raise CameraUnavailable("Kameraprozess konnte nicht vorbereitet werden")
        finally:
            os.close(write_fd)
        runner = str(Path(__file__).with_name("camera_runner.py"))
        try:
            process = await self._process_factory(
                sys.executable,
                runner,
                str(read_fd),
                pass_fds=(read_fd,),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env={
                    "LANG": "C",
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "PYTHONUNBUFFERED": "1",
                },
            )
        except (OSError, ValueError) as exc:
            raise CameraUnavailable("Kameraprozess konnte nicht gestartet werden") from exc
        finally:
            os.close(read_fd)
        if process.stdout is None:
            await _stop_process(process, self.limits.terminate_timeout_seconds)
            raise CameraUnavailable("Kameraprozess liefert keinen Videostream")
        return process


class CameraStream:
    def __init__(
        self,
        *,
        manager: CameraStreamManager,
        stream_id: str,
        config: PrinterConfig,
        session_key: str,
        is_authorized: AuthorizationCheck,
    ) -> None:
        self.manager = manager
        self.stream_id = stream_id
        self.config = config
        self.session_key = session_key
        self._is_authorized = is_authorized
        self._process: Any | None = None
        self._first_chunk: bytes | None = None
        self._started_at = self.manager._clock()
        self._restarts = 0
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._process_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        while True:
            if self._closed:
                raise CameraUnavailable("Kamerastream wurde bereits beendet")
            try:
                await self._start_process()
                return
            except CameraUnavailable:
                if self._closed or self._restarts >= self.manager.limits.max_restarts:
                    raise
                self._restarts += 1
                await asyncio.sleep(0.2)

    async def _start_process(self) -> None:
        async with self._process_lock:
            if self._closed:
                raise CameraUnavailable("Kamerastream wurde bereits beendet")
            process = await self.manager._spawn(self.config)
            if self._closed:
                await _stop_process(
                    process,
                    self.manager.limits.terminate_timeout_seconds,
                )
                raise CameraUnavailable("Kamerastream wurde bereits beendet")
            self._process = process
        try:
            first_chunk = await asyncio.wait_for(
                process.stdout.read(READ_CHUNK_BYTES),
                timeout=self.manager.limits.startup_timeout_seconds,
            )
        except TimeoutError as exc:
            await self._stop_current_process()
            raise CameraUnavailable("Kamera antwortet nicht rechtzeitig") from exc
        if not first_chunk:
            await self._stop_current_process()
            raise CameraUnavailable("Kamera liefert kein Bild")
        if self._closed:
            await self._stop_current_process()
            raise CameraUnavailable("Kamerastream wurde bereits beendet")
        if not self._is_authorized():
            await self._stop_current_process()
            raise CameraUnavailable("Web-Sitzung ist nicht mehr gültig")
        self._first_chunk = first_chunk

    async def _restart(self) -> bool:
        if self._closed or self._restarts >= self.manager.limits.max_restarts:
            return False
        self._restarts += 1
        await self._stop_current_process()
        await asyncio.sleep(0.2)
        if self._closed:
            return False
        try:
            await self._start_process()
        except CameraUnavailable:
            return False
        return True

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        try:
            while not self._closed:
                if not self._is_authorized():
                    break
                if self.manager._clock() - self._started_at >= self.manager.limits.max_session_seconds:
                    break
                if self._first_chunk is not None:
                    chunk = self._first_chunk
                    self._first_chunk = None
                else:
                    remaining_session = max(
                        0.0,
                        self.manager.limits.max_session_seconds
                        - (self.manager._clock() - self._started_at),
                    )
                    try:
                        chunk = await asyncio.wait_for(
                            self._process.stdout.read(READ_CHUNK_BYTES),
                            timeout=min(
                                self.manager.limits.inactivity_timeout_seconds,
                                remaining_session,
                            ),
                        )
                    except TimeoutError:
                        chunk = b""
                if not chunk:
                    if (
                        self._closed
                        or not self._is_authorized()
                        or self.manager._clock() - self._started_at
                        >= self.manager.limits.max_session_seconds
                    ):
                        break
                    if await self._restart():
                        continue
                    break
                if self._closed or not self._is_authorized():
                    break
                yield chunk
        finally:
            close_task = asyncio.create_task(self.close())
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                await close_task
                raise

    async def _stop_current_process(self) -> None:
        async with self._process_lock:
            process = self._process
            self._process = None
            if process is not None:
                await _stop_process(
                    process,
                    self.manager.limits.terminate_timeout_seconds,
                )

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_task is None:
                self._closed = True
                self._close_task = asyncio.create_task(
                    self._close_impl(),
                    name=f"camera-cleanup-{self.config.id}",
                )
            close_task = self._close_task
        # Request or StreamingResponse cancellation must not cancel the one
        # cleanup operation shared by all concurrent close callers.
        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        try:
            await self._stop_current_process()
        finally:
            await self.manager._release(self)


async def _stop_process(process: Any, timeout: float) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        return
    except TimeoutError:
        pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except (ProcessLookupError, TimeoutError):
        pass
