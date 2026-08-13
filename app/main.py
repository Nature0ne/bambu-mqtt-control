from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from starlette.concurrency import run_in_threadpool

from .admin import (
    MAX_ADMIN_BODY_BYTES,
    AdminConfigConflict,
    AdminConfigPersistenceError,
    AdminConfigStore,
    AdminConfigValidationError,
    parse_admin_config_update,
    public_config,
)
from .audit import AuditLog
from .camera import (
    CAMERA_MEDIA_TYPE,
    CAMERA_TICKET_COOKIE,
    CameraBusy,
    CameraStreamManager,
    CameraTicketRejected,
    CameraUnavailable,
    camera_model_supported,
    camera_tls_supported,
)
from .commands import CommandError
from .config import AppConfig, ConfigError, load_config
from .manager import (
    CommandForbidden,
    CommandRateLimited,
    CommandUnavailable,
    ControlManager,
    PrinterNotFound,
)
from .metrics import render_metrics
from .setup import (
    MAX_SETUP_BODY_BYTES,
    SetupAlreadyConfigured,
    SetupPersistenceError,
    SetupRateLimited,
    SetupRateLimiter,
    SetupStore,
    SetupUnauthorized,
    SetupValidationError,
    parse_setup_request,
)
from .version import build_version
from .web import (
    SESSION_COOKIE_NAME,
    EventHub,
    LoginRateLimited,
    LoginRateLimiter,
    SessionStore,
    credentials_match,
    require_csrf,
    require_page_user,
    require_user,
    websocket_user,
)

LOGGER = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent
MAX_LOGIN_BODY_BYTES = 8 * 1024
MAX_COMMAND_BODY_BYTES = 16 * 1024
DEFAULT_WEBSOCKET_REVALIDATION_SECONDS = 5.0


def _audit_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Ungültiger Audit-Zeitfilter") from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail="Audit-Zeitfilter benötigt eine Zeitzone")
    return parsed.astimezone(timezone.utc).isoformat()


def _audit_filter(value: str | None, *, maximum: int) -> str | None:
    if value is None:
        return None
    if (
        not value
        or len(value) > maximum
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise HTTPException(status_code=422, detail="Ungültiger Audit-Filter")
    return value


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=64, pattern=r"^[a-z_]+$")
    params: dict[str, Any] = Field(default_factory=dict)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=1024)


def _health_payload(manager: ControlManager | None) -> dict[str, Any]:
    if manager is None:
        return {
            "status": "ok",
            "configured": False,
            "setup_required": True,
            "mode": "setup_required",
            "configured_printers": 0,
            "online_printers": 0,
            "stale_printers": 0,
        }
    snapshot = manager.snapshot()
    printers = snapshot["printers"]
    return {
        "status": "ok",
        "configured": True,
        "setup_required": False,
        "mode": "running",
        "configured_printers": len(printers),
        "online_printers": sum(1 for printer in printers if printer["online"]),
        "stale_printers": sum(1 for printer in printers if printer["stale"]),
    }


def _diagnostics_payload(request: Request) -> dict[str, Any]:
    config: AppConfig = request.app.state.config
    snapshot = request.app.state.manager.snapshot()
    configs = {printer.id: printer for printer in config.printers}
    printers: list[dict[str, Any]] = []
    for printer in snapshot["printers"]:
        printer_config = configs[printer["id"]]
        state = printer.get("state") or {}
        camera = state.get("camera") or {}
        ams = state.get("ams") or {}
        reported = state.get("diagnostics") or {}
        sd_card = reported.get("sd_card") or {}
        print_stage = reported.get("print_stage") or {}
        firmware = reported.get("firmware") or {}
        printer_firmware = firmware.get("printer") or {}
        hms = reported.get("hms") or {}
        printers.append(
            {
                "id": printer["id"],
                "name": printer["name"],
                "model": printer["model"],
                "connection_state": printer["connection_state"],
                "online": bool(printer["online"]),
                "stale": bool(printer["stale"]),
                "last_seen": printer.get("last_seen"),
                "writable": printer_config.writable,
                "allowed_commands": (
                    sorted(printer_config.allowed_commands)
                    if printer_config.writable
                    else []
                ),
                "developer_lan_mode": state.get("developer_lan_mode"),
                "camera": {
                    "configured": printer_config.camera_enabled,
                    "reported_available": camera.get("available"),
                    "local_protocol": camera.get("local_protocol"),
                },
                "device": {
                    "wifi_signal_dbm": reported.get("wifi_signal_dbm"),
                    "door_open": reported.get("door_open"),
                    "sd_card": {
                        "present": sd_card.get("present"),
                        "status": sd_card.get("status"),
                    },
                    "print_stage": {
                        "phase_id": print_stage.get("phase_id"),
                        "stage_id": print_stage.get("stage_id"),
                        "substage_id": print_stage.get("substage_id"),
                    },
                    "firmware": {
                        "printer": {
                            "software": printer_firmware.get("software"),
                            "hardware": printer_firmware.get("hardware"),
                        },
                        "modules": [
                            {
                                "name": module.get("name"),
                                "software": module.get("software"),
                                "hardware": module.get("hardware"),
                            }
                            for module in firmware.get("modules") or []
                            if isinstance(module, dict)
                        ],
                    },
                    "hms": {
                        "count": hms.get("count", 0),
                        "items": [
                            {
                                "code": item.get("code"),
                                "module": item.get("module"),
                                "severity": item.get("severity"),
                            }
                            for item in hms.get("items") or []
                            if isinstance(item, dict)
                        ],
                    },
                },
                "ams": [
                    {
                        "id": unit.get("id"),
                        "model": unit.get("model"),
                        "dry_capable": bool(unit.get("dry_capable")),
                        "experimental": bool(unit.get("experimental")),
                        "drying_active": bool((unit.get("drying") or {}).get("active")),
                    }
                    for unit in ams.get("units") or []
                ],
            }
        )
    return {
        "version": 1,
        "build_version": build_version(),
        "configured": True,
        "restart_required": False,
        "uptime_seconds": max(
            0,
            int(time.monotonic() - request.app.state.started_at_monotonic),
        ),
        "audit_retention": request.app.state.audit.retention(),
        "printers": printers,
    }


def create_app(
    supplied_config: AppConfig | None = None,
    supplied_manager: ControlManager | None = None,
    supplied_setup_store: SetupStore | None = None,
    supplied_manager_factory: Callable[[AppConfig, AuditLog], ControlManager] | None = None,
    supplied_setup_rate_limiter: SetupRateLimiter | None = None,
    supplied_session_store: SessionStore | None = None,
    supplied_login_rate_limiter: LoginRateLimiter | None = None,
    supplied_websocket_revalidation_seconds: float | None = None,
    supplied_camera_manager: CameraStreamManager | None = None,
) -> FastAPI:
    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
    hub = EventHub()
    manager_factory = supplied_manager_factory or ControlManager
    camera_manager = supplied_camera_manager or CameraStreamManager()
    websocket_revalidation_seconds = (
        DEFAULT_WEBSOCKET_REVALIDATION_SECONDS
        if supplied_websocket_revalidation_seconds is None
        else supplied_websocket_revalidation_seconds
    )
    if websocket_revalidation_seconds <= 0:
        raise ValueError("websocket revalidation interval must be positive")

    def activate_runtime(
        application: FastAPI,
        config: AppConfig,
        manager: ControlManager | None = None,
    ) -> ControlManager:
        audit = manager.audit if manager else AuditLog(config.audit_db)
        runtime_manager = manager or manager_factory(config, audit)
        runtime_manager.set_on_change(
            lambda: hub.publish_threadsafe(runtime_manager.snapshot())
        )
        application.state.config = config
        application.state.audit = audit
        application.state.manager = runtime_manager
        runtime_manager.start()
        return runtime_manager

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        config_path = os.environ.get("BAMBU_CONFIG_FILE", "/config/printers.yml")
        token_path = os.environ.get("BAMBU_BOOTSTRAP_TOKEN_FILE")
        setup_store = supplied_setup_store or SetupStore(
            config_path,
            token_path=token_path,
        )
        application.state.config = None
        application.state.audit = None
        application.state.manager = None
        application.state.setup_store = setup_store
        application.state.admin_config_store = AdminConfigStore(setup_store.config_path)
        application.state.setup_rate_limiter = supplied_setup_rate_limiter or SetupRateLimiter()
        application.state.session_store = supplied_session_store or SessionStore()
        application.state.login_rate_limiter = supplied_login_rate_limiter or LoginRateLimiter()
        application.state.camera_manager = camera_manager
        application.state.websocket_revalidation_seconds = websocket_revalidation_seconds
        application.state.started_at_monotonic = time.monotonic()
        application.state.runtime_lock = asyncio.Lock()
        application.state.csrf_token = secrets.token_urlsafe(32)
        application.state.hub = hub
        hub.start()

        config = supplied_config
        manager = supplied_manager
        if config is None and manager is not None:
            config = manager.config
        if config is not None:
            activate_runtime(application, config, manager)
        elif setup_store.configuration_present():
            try:
                activate_runtime(application, load_config(setup_store.config_path))
            except ConfigError:
                LOGGER.exception("Unsafe or incomplete Bambu Control configuration")
                raise
        else:
            setup_store.prepare()
        try:
            yield
        finally:
            try:
                await application.state.camera_manager.close()
            finally:
                runtime_manager = application.state.manager
                if runtime_manager is not None:
                    runtime_manager.stop()

    application = FastAPI(
        title="Bambu Kontrollzentrum",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @application.get("/", response_class=HTMLResponse)
    async def index(request: Request, actor: str | None = Depends(require_page_user)):
        if request.app.state.config is None:
            return RedirectResponse(url="/setup", status_code=307)
        if actor is None:
            return RedirectResponse(url="/login", status_code=303)
        response = templates.TemplateResponse(request=request, name="index.html", context={})
        response.set_cookie(
            "bambu_csrf",
            request.app.state.csrf_token,
            secure=True,
            httponly=False,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @application.get("/login", response_class=HTMLResponse)
    async def login_page(
        request: Request,
        actor: str | None = Depends(require_page_user),
    ):
        if request.app.state.config is None:
            return RedirectResponse(url="/setup", status_code=307)
        if actor is not None:
            return RedirectResponse(url="/", status_code=303)
        response = templates.TemplateResponse(request=request, name="login.html", context={})
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @application.get("/manage", response_class=HTMLResponse)
    async def manage_page(
        request: Request,
        actor: str | None = Depends(require_page_user),
    ):
        if request.app.state.config is None:
            return RedirectResponse(url="/setup", status_code=307)
        if actor is None:
            return RedirectResponse(url="/login", status_code=303)
        response = templates.TemplateResponse(request=request, name="manage.html", context={})
        response.set_cookie(
            "bambu_csrf",
            request.app.state.csrf_token,
            secure=True,
            httponly=False,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @application.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        if request.app.state.config is not None:
            return RedirectResponse(url="/", status_code=307)
        response = templates.TemplateResponse(request=request, name="setup.html", context={})
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @application.get("/api/printers")
    async def printers(request: Request, _actor: str = Depends(require_user)):
        return request.app.state.manager.snapshot()

    def camera_context(request: Request, printer_id: str) -> tuple[Any, dict[str, Any]]:
        config = next(
            (
                printer
                for printer in request.app.state.config.printers
                if printer.id == printer_id
            ),
            None,
        )
        if config is None:
            raise HTTPException(status_code=404, detail="Drucker nicht gefunden")
        printer = next(
            (
                item
                for item in request.app.state.manager.snapshot()["printers"]
                if item["id"] == printer_id
            ),
            None,
        )
        if printer is None:
            raise HTTPException(status_code=404, detail="Drucker nicht gefunden")
        return config, printer

    def camera_session(request: Request, actor: str) -> str:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        session_store: SessionStore = request.app.state.session_store
        if token is None or session_store.authenticate(token) != actor:
            raise HTTPException(
                status_code=401,
                detail="Für die Kamera ist eine gültige Web-Sitzung erforderlich",
            )
        origin = request.headers.get("origin")
        if origin is not None and origin not in request.app.state.config.web.allowed_origins:
            raise HTTPException(status_code=403, detail="Origin ist nicht freigegeben")
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            raise HTTPException(status_code=403, detail="Kameraaufruf ist nicht gleich-origin")
        return token

    def camera_reports_rtsps(printer: dict[str, Any]) -> bool:
        camera = printer.get("state", {}).get("camera") or {}
        return bool(
            camera.get("available") is True
            and camera.get("local_protocol") == "rtsps"
        )

    def require_camera_ready(config: Any, printer: dict[str, Any]) -> None:
        if config.camera_enabled is not True:
            raise HTTPException(status_code=403, detail="Kamera ist nicht freigegeben")
        if not camera_model_supported(config.model):
            raise HTTPException(
                status_code=409,
                detail="Dieses Druckermodell unterstützt den Kamerastream nicht",
            )
        if not camera_tls_supported(config):
            raise HTTPException(
                status_code=409,
                detail="Kamerastream erfordert eine überprüfte TLS-Verbindung",
            )
        if not printer["online"] or printer["stale"]:
            raise HTTPException(
                status_code=409,
                detail="Drucker ist offline oder die Daten sind veraltet",
            )
        if not camera_reports_rtsps(printer):
            raise HTTPException(
                status_code=409,
                detail="Der Drucker meldet keinen verfügbaren lokalen RTSPS-Stream",
            )

    @application.get("/api/printers/{printer_id}/camera/status")
    async def camera_status(
        printer_id: str,
        request: Request,
        actor: str = Depends(require_user),
    ):
        camera_session(request, actor)
        config, printer = camera_context(request, printer_id)
        active = await request.app.state.camera_manager.is_active(printer_id)
        supported = camera_model_supported(config.model)
        enabled = config.camera_enabled is True
        secure_tls = camera_tls_supported(config)
        rtsps_available = camera_reports_rtsps(printer)
        if not enabled:
            reason = "disabled"
        elif not supported:
            reason = "unsupported_model"
        elif not secure_tls:
            reason = "unsafe_tls"
        elif printer["stale"]:
            reason = "stale"
        elif not printer["online"]:
            reason = "offline"
        elif not rtsps_available:
            reason = "rtsps_unavailable"
        elif active:
            reason = "streaming"
        else:
            reason = "ready"
        ready = (
            enabled
            and supported
            and secure_tls
            and printer["online"]
            and not printer["stale"]
            and rtsps_available
        )
        return {
            "enabled": enabled,
            "supported": supported,
            "online": printer["online"],
            "stale": printer["stale"],
            "available": ready and not active,
            "active": active,
            "reason": reason,
        }

    @application.post("/api/printers/{printer_id}/camera/ticket")
    async def camera_ticket(
        printer_id: str,
        request: Request,
        actor: str = Depends(require_csrf),
    ):
        token = camera_session(request, actor)
        config, printer = camera_context(request, printer_id)
        require_camera_ready(config, printer)
        session_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        ticket, expires_in = await request.app.state.camera_manager.issue_ticket(
            printer_id,
            session_key,
        )
        response = JSONResponse(
            {"expires_in": expires_in},
            headers={"Cache-Control": "no-store, private"},
        )
        response.set_cookie(
            CAMERA_TICKET_COOKIE,
            ticket,
            max_age=expires_in,
            secure=True,
            httponly=True,
            samesite="strict",
            path=f"/api/printers/{printer_id}/camera/stream",
        )
        return response

    @application.get("/api/printers/{printer_id}/camera/stream")
    async def camera_stream(
        printer_id: str,
        request: Request,
        actor: str = Depends(require_user),
    ):
        token = camera_session(request, actor)
        config, printer = camera_context(request, printer_id)
        require_camera_ready(config, printer)

        session_store: SessionStore = request.app.state.session_store
        session_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        ticket = request.cookies.get(CAMERA_TICKET_COOKIE)

        def stream_is_authorized() -> bool:
            if session_store.authenticate(token) != actor:
                return False
            try:
                current_config, current_printer = camera_context(request, printer_id)
            except HTTPException:
                return False
            return bool(
                current_config.camera_enabled is True
                and camera_model_supported(current_config.model)
                and camera_tls_supported(current_config)
                and current_printer["online"]
                and not current_printer["stale"]
                and camera_reports_rtsps(current_printer)
            )

        try:
            stream = await request.app.state.camera_manager.open_ticketed_stream(
                config,
                ticket=ticket,
                session_key=session_key,
                is_authorized=stream_is_authorized,
            )
        except CameraTicketRejected as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except CameraBusy as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except CameraUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        response = StreamingResponse(
            stream.iter_bytes(),
            media_type=CAMERA_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-store, private",
                "Pragma": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        response.delete_cookie(
            CAMERA_TICKET_COOKIE,
            secure=True,
            httponly=True,
            samesite="strict",
            path=f"/api/printers/{printer_id}/camera/stream",
        )
        return response

    @application.post(
        "/api/printers/{printer_id}/camera/stop",
        status_code=204,
    )
    async def camera_stop(
        printer_id: str,
        request: Request,
        actor: str = Depends(require_csrf),
    ):
        camera_session(request, actor)
        camera_context(request, printer_id)
        # There is exactly one configured web account. A CSRF-authenticated
        # session for that account may therefore recover a stream left behind
        # by an older browser session, while the per-printer scope remains
        # explicit.
        await request.app.state.camera_manager.revoke_printer_tickets(printer_id)
        await request.app.state.camera_manager.close_stream(printer_id)
        response = Response(status_code=204)
        response.delete_cookie(
            CAMERA_TICKET_COOKIE,
            secure=True,
            httponly=True,
            samesite="strict",
            path=f"/api/printers/{printer_id}/camera/stream",
        )
        return response

    @application.get("/api/setup/status")
    async def setup_status(request: Request):
        configured = request.app.state.config is not None
        return {
            "configured": configured,
            "setup_required": not configured,
        }

    @application.post("/api/login", status_code=204)
    async def login(request: Request):
        config = request.app.state.config
        if config is None:
            raise HTTPException(status_code=503, detail="Einrichtung erforderlich")

        origin = request.headers.get("origin")
        if not origin or origin not in config.web.allowed_origins:
            raise HTTPException(status_code=403, detail="Origin ist nicht freigegeben")

        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != "application/json":
            raise HTTPException(status_code=415, detail="Anmeldung muss JSON enthalten")

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_LOGIN_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="Anmeldung ist zu groß")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Ungültige Anmeldung") from exc
        body_buffer = bytearray()
        async for chunk in request.stream():
            if len(body_buffer) + len(chunk) > MAX_LOGIN_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Anmeldung ist zu groß")
            body_buffer.extend(chunk)

        try:
            raw_login = json.loads(bytes(body_buffer))
            login_request = LoginRequest.model_validate(raw_login)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            RecursionError,
            ValidationError,
        ) as exc:
            raise HTTPException(status_code=422, detail="Ungültige Anmeldedaten") from exc

        username = login_request.username
        password = login_request.password.get_secret_value()
        client_key = request.client.host if request.client else "unknown"
        limiter: LoginRateLimiter = request.app.state.login_rate_limiter
        try:
            limiter.check(client_key)
        except LoginRateLimited as exc:
            retry_after = max(1, int(limiter.window_seconds))
            raise HTTPException(
                status_code=429,
                detail="Zu viele Anmeldeversuche",
                headers={"Retry-After": str(retry_after)},
            ) from exc
        if not credentials_match(username, password, config.web):
            limiter.record_failure(client_key)
            raise HTTPException(status_code=401, detail="Anmeldung fehlgeschlagen")
        limiter.reset_on_success(client_key)

        session_store: SessionStore = request.app.state.session_store
        session_store.revoke(request.cookies.get(SESSION_COOKIE_NAME))
        session_token = session_store.issue(username)
        response = Response(status_code=204)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_token,
            max_age=session_store.ttl_seconds,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @application.post("/api/logout", status_code=204)
    async def logout(
        request: Request,
        _actor: str = Depends(require_csrf),
    ):
        session_store: SessionStore = request.app.state.session_store
        session_token = request.cookies.get(SESSION_COOKIE_NAME)
        # Revocation is synchronous and happens before any await so a camera
        # request that has not atomically reserved its slot can no longer do so.
        session_store.revoke(session_token)
        if session_token is not None:
            session_key = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
            await request.app.state.camera_manager.revoke_tickets(session_key)
            await request.app.state.camera_manager.close_session(session_key)
        response = Response(status_code=204)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        response.delete_cookie(
            "bambu_csrf",
            secure=True,
            httponly=False,
            samesite="strict",
            path="/",
        )
        for printer in request.app.state.config.printers:
            response.delete_cookie(
                CAMERA_TICKET_COOKIE,
                secure=True,
                httponly=True,
                samesite="strict",
                path=f"/api/printers/{printer.id}/camera/stream",
            )
        return response

    @application.post("/api/setup", status_code=201)
    async def complete_setup(request: Request):
        if request.app.state.config is not None:
            raise HTTPException(status_code=409, detail="Setup ist bereits abgeschlossen")

        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != "application/json":
            raise HTTPException(
                status_code=415,
                detail="Setup-Anfrage muss JSON enthalten",
            )

        client_key = request.client.host if request.client else "unknown"
        try:
            request.app.state.setup_rate_limiter.consume(client_key)
        except SetupRateLimited as exc:
            raise HTTPException(
                status_code=429,
                detail="Zu viele Setup-Versuche",
                headers={"Retry-After": "60"},
            ) from exc

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_SETUP_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="Setup-Anfrage ist zu groß")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Ungültige Setup-Anfrage") from exc
        body_buffer = bytearray()
        async for chunk in request.stream():
            if len(body_buffer) + len(chunk) > MAX_SETUP_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Setup-Anfrage ist zu groß")
            body_buffer.extend(chunk)
        body = bytes(body_buffer)
        try:
            raw_setup = json.loads(body)
            setup_request = parse_setup_request(raw_setup)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            RecursionError,
            SetupValidationError,
        ) as exc:
            raise HTTPException(status_code=422, detail="Ungültige Setup-Daten") from exc

        async with request.app.state.runtime_lock:
            if request.app.state.config is not None:
                raise HTTPException(status_code=409, detail="Setup ist bereits abgeschlossen")
            try:
                config = await run_in_threadpool(
                    request.app.state.setup_store.complete,
                    setup_request,
                    origin_header=request.headers.get("origin"),
                    header_token=request.headers.get("x-setup-token"),
                )
            except SetupAlreadyConfigured as exc:
                raise HTTPException(status_code=409, detail="Setup ist bereits abgeschlossen") from exc
            except SetupUnauthorized as exc:
                raise HTTPException(status_code=401, detail="Ungültiger Setup-Token") from exc
            except SetupValidationError as exc:
                raise HTTPException(status_code=422, detail="Ungültige Setup-Daten") from exc
            except SetupPersistenceError as exc:
                LOGGER.error("Bambu setup configuration could not be persisted")
                raise HTTPException(status_code=500, detail="Setup konnte nicht gespeichert werden") from exc

            activate_runtime(request.app, config)
            return {
                "ok": True,
                "configured": True,
                "setup_required": False,
                "printer_count": len(config.printers),
            }

    @application.get("/api/admin/config")
    async def admin_config(request: Request, _actor: str = Depends(require_user)):
        return public_config(request.app.state.config)

    @application.put("/api/admin/config")
    async def update_admin_config(
        request: Request,
        actor: str = Depends(require_csrf),
    ):
        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != "application/json":
            raise HTTPException(
                status_code=415,
                detail="Konfigurationsänderung muss JSON enthalten",
            )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_ADMIN_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="Konfigurationsanfrage ist zu groß")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Ungültige Konfigurationsanfrage") from exc
        body_buffer = bytearray()
        async for chunk in request.stream():
            if len(body_buffer) + len(chunk) > MAX_ADMIN_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Konfigurationsanfrage ist zu groß")
            body_buffer.extend(chunk)
        try:
            raw_update = json.loads(bytes(body_buffer))
            config_update = parse_admin_config_update(raw_update)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            RecursionError,
            AdminConfigValidationError,
        ) as exc:
            raise HTTPException(status_code=422, detail="Ungültige Konfigurationsdaten") from exc

        async with request.app.state.runtime_lock:
            current: AppConfig = request.app.state.config
            current_password = config_update.current_password.get_secret_value()
            client_key = (
                f"{request.client.host}:admin-config"
                if request.client
                else "unknown:admin-config"
            )
            limiter: LoginRateLimiter = request.app.state.login_rate_limiter
            try:
                limiter.check(client_key)
            except LoginRateLimited as exc:
                raise HTTPException(
                    status_code=429,
                    detail="Zu viele Bestätigungsversuche",
                    headers={"Retry-After": str(max(1, int(limiter.window_seconds)))},
                ) from exc
            if not credentials_match(
                current.web.username,
                current_password,
                current.web,
            ):
                limiter.record_failure(client_key)
                # The web session is still valid.  Keep 401 reserved for an
                # expired/invalid session so browser clients do not redirect
                # away from the correction flow.
                raise HTTPException(status_code=403, detail="Passwortbestätigung fehlgeschlagen")
            limiter.reset_on_success(client_key)

            origin = request.headers.get("origin")
            if origin not in config_update.web.allowed_origins:
                raise HTTPException(
                    status_code=422,
                    detail="Der aktuelle Origin muss freigegeben bleiben",
                )

            prepared = None
            candidate_manager = None
            old_manager: ControlManager = request.app.state.manager
            try:
                prepared = await run_in_threadpool(
                    request.app.state.admin_config_store.prepare,
                    config_update,
                    current,
                )
                candidate_manager = manager_factory(prepared.config, request.app.state.audit)
            except AdminConfigValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except AdminConfigConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except AdminConfigPersistenceError as exc:
                LOGGER.error("Administrative configuration could not be prepared")
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            except Exception as exc:
                if prepared is not None:
                    await run_in_threadpool(prepared.abort)
                LOGGER.exception("Replacement runtime could not be constructed")
                raise HTTPException(
                    status_code=500,
                    detail="Neue Laufzeit konnte nicht vorbereitet werden",
                ) from exc

            old_stopped = False
            try:
                await request.app.state.camera_manager.close()
                await run_in_threadpool(old_manager.stop)
                old_stopped = True
                await run_in_threadpool(candidate_manager.start, strict=True)
                await run_in_threadpool(prepared.commit)
            except AdminConfigConflict as exc:
                await run_in_threadpool(candidate_manager.stop)
                if old_stopped:
                    await run_in_threadpool(old_manager.start)
                await run_in_threadpool(prepared.abort)
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except AdminConfigPersistenceError as exc:
                await run_in_threadpool(candidate_manager.stop)
                if old_stopped:
                    await run_in_threadpool(old_manager.start)
                await run_in_threadpool(prepared.abort)
                LOGGER.error("Administrative configuration could not be committed")
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            except Exception as exc:
                await run_in_threadpool(candidate_manager.stop)
                if old_stopped:
                    await run_in_threadpool(old_manager.start)
                await run_in_threadpool(prepared.abort)
                LOGGER.exception("Replacement runtime failed to start")
                raise HTTPException(
                    status_code=500,
                    detail="Neue Laufzeit konnte nicht gestartet werden",
                ) from exc

            request.app.state.config = prepared.config
            request.app.state.manager = candidate_manager
            candidate_manager.set_on_change(
                lambda: hub.publish_threadsafe(candidate_manager.snapshot())
            )
            await run_in_threadpool(prepared.finalize)
            hub.publish_threadsafe(candidate_manager.snapshot())

            try:
                request.app.state.audit.record(
                    actor=actor,
                    printer_id="system",
                    command="update_config",
                    params={},
                    result="applied",
                )
            except Exception:
                LOGGER.exception("Administrative configuration audit failed")

            credentials_changed = (
                current.web.username != prepared.config.web.username
                or current.web.password != prepared.config.web.password
            )
            response_payload = public_config(prepared.config)
            response_payload.update(
                {
                    "applied": True,
                    "runtime": {
                        "manager_reloaded": True,
                        "reconnecting_printer_ids": [
                            printer.id for printer in prepared.config.printers
                        ],
                    },
                }
            )
            response = JSONResponse(response_payload)
            if credentials_changed:
                session_store: SessionStore = request.app.state.session_store
                session_token = session_store.replace_all(prepared.config.web.username)
                response.set_cookie(
                    SESSION_COOKIE_NAME,
                    session_token,
                    max_age=session_store.ttl_seconds,
                    secure=True,
                    httponly=True,
                    samesite="strict",
                    path="/",
                )
            return response

    @application.get("/api/admin/audit")
    async def admin_audit_history(
        request: Request,
        _actor: str = Depends(require_user),
        limit: int = Query(default=50, ge=1, le=100),
        cursor: int | None = Query(default=None, ge=1),
        printer_id: str | None = Query(default=None, max_length=64),
        command: str | None = Query(default=None, max_length=64),
        result: str | None = Query(default=None, max_length=32),
        actor: str | None = Query(default=None, max_length=128),
        from_time: str | None = Query(default=None, alias="from", max_length=64),
        to_time: str | None = Query(default=None, alias="to", max_length=64),
    ):
        canonical_from = _audit_timestamp(from_time)
        canonical_to = _audit_timestamp(to_time)
        if canonical_from is not None and canonical_to is not None and canonical_from > canonical_to:
            raise HTTPException(status_code=422, detail="Ungültiger Audit-Zeitraum")
        return await run_in_threadpool(
            request.app.state.audit.history,
            limit=limit,
            cursor=cursor,
            printer_id=_audit_filter(printer_id, maximum=64),
            command=_audit_filter(command, maximum=64),
            result=_audit_filter(result, maximum=32),
            actor=_audit_filter(actor, maximum=128),
            from_time=canonical_from,
            to_time=canonical_to,
        )

    @application.get("/api/admin/diagnostics")
    async def admin_diagnostics(
        request: Request,
        _actor: str = Depends(require_user),
    ):
        return _diagnostics_payload(request)

    @application.post("/api/printers/{printer_id}/commands")
    async def printer_command(
        printer_id: str,
        request: Request,
        actor: str = Depends(require_csrf),
    ):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="JSON erwartet")
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_COMMAND_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="Befehl ist zu groß")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Ungültige Anfrage") from exc
        body_buffer = bytearray()
        async for chunk in request.stream():
            if len(body_buffer) + len(chunk) > MAX_COMMAND_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Befehl ist zu groß")
            body_buffer.extend(chunk)
        try:
            raw_command = json.loads(body_buffer)
            command_request = CommandRequest.model_validate(raw_command)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="Ungültiger Befehl") from exc
        try:
            return await run_in_threadpool(
                request.app.state.manager.send_command,
                printer_id,
                command_request.command,
                command_request.params,
                actor,
            )
        except PrinterNotFound as exc:
            raise HTTPException(status_code=404, detail="Drucker nicht gefunden") from exc
        except CommandForbidden as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except CommandRateLimited as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except (CommandError, CommandUnavailable) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/health")
    async def health(request: Request, _actor: str = Depends(require_user)):
        return _health_payload(request.app.state.manager)

    @application.get("/healthz")
    async def healthz(request: Request):
        return _health_payload(request.app.state.manager)

    @application.get("/readyz")
    async def readyz(request: Request):
        payload = _health_payload(request.app.state.manager)
        if payload["configured"]:
            payload["status"] = "ready" if payload["online_printers"] else "degraded"
        return payload

    @application.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request):
        if request.app.state.manager is None:
            return PlainTextResponse(
                "# HELP bambu_setup_required Whether first-run setup is required.\n"
                "# TYPE bambu_setup_required gauge\n"
                "bambu_setup_required 1\n",
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )
        return PlainTextResponse(
            render_metrics(
                request.app.state.manager.snapshot(),
                request.app.state.audit.counts(),
            ),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @application.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        if websocket.app.state.manager is None:
            await websocket.close(code=4403)
            return
        try:
            identity = websocket_user(websocket)
        except LoginRateLimited:
            await websocket.accept()
            await websocket.close(code=4401)
            return
        if identity is None:
            await websocket.accept()
            await websocket.close(code=4401)
            return
        await websocket.accept()
        queue = websocket.app.state.hub.subscribe()
        session_store: SessionStore = websocket.app.state.session_store
        revalidation_seconds = websocket.app.state.websocket_revalidation_seconds
        loop = asyncio.get_running_loop()
        next_revalidation = (
            loop.time() + revalidation_seconds
            if identity.session_token is not None
            else None
        )
        try:
            if not identity.is_current(session_store):
                await websocket.close(code=4401)
                return
            await websocket.send_json(
                {"type": "snapshot", "data": websocket.app.state.manager.snapshot()}
            )
            while True:
                state_task = asyncio.create_task(queue.get())
                receive_task = asyncio.create_task(websocket.receive())
                timeout = (
                    max(0.0, next_revalidation - loop.time())
                    if next_revalidation is not None
                    else None
                )
                done, pending = await asyncio.wait(
                    {state_task, receive_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

                if receive_task in done:
                    event = receive_task.result()
                    if event["type"] == "websocket.disconnect":
                        break

                if next_revalidation is not None and loop.time() >= next_revalidation:
                    if not identity.is_current(session_store):
                        await websocket.close(code=4401)
                        break
                    next_revalidation = loop.time() + revalidation_seconds

                if state_task in done:
                    if not identity.is_current(session_store):
                        await websocket.close(code=4401)
                        break
                    await websocket.send_json(state_task.result())
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            websocket.app.state.hub.unsubscribe(queue)

    return application


logging.basicConfig(
    level=os.environ.get("BAMBU_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
app = create_app()
