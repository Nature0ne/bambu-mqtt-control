(() => {
  "use strict";

  const dom = {
    activeCount: document.querySelector("#active-count"),
    amsCount: document.querySelector("#ams-count"),
    amsTemplate: document.querySelector("#ams-template"),
    confirmStopDryingButton: document.querySelector("#confirm-stop-drying-button"),
    confirmStopButton: document.querySelector("#confirm-stop-button"),
    connectionLabel: document.querySelector("#connection-label"),
    connectionStatus: document.querySelector("#connection-status"),
    dryingDialog: document.querySelector("#drying-dialog"),
    dryingDialogClose: document.querySelector("#drying-dialog-close"),
    dryingDialogSubtitle: document.querySelector("#drying-dialog-subtitle"),
    dryingDuration: document.querySelector("#drying-duration"),
    dryingExperimentalAck: document.querySelector("#drying-experimental-ack"),
    dryingExperimentalField: document.querySelector("#drying-experimental-field"),
    dryingFilament: document.querySelector("#drying-filament"),
    dryingForm: document.querySelector("#drying-form"),
    dryingFormError: document.querySelector("#drying-form-error"),
    dryingRotate: document.querySelector("#drying-rotate"),
    dryingTemperature: document.querySelector("#drying-temperature"),
    dryingTemperatureMax: document.querySelector("#drying-temperature-max"),
    dryingTemperatureMin: document.querySelector("#drying-temperature-min"),
    dryingTemperatureOutput: document.querySelector("#drying-temperature-output"),
    emptyView: document.querySelector("#empty-view"),
    globalAlert: document.querySelector("#global-alert"),
    globalAlertMessage: document.querySelector("#global-alert-message"),
    globalAlertTitle: document.querySelector("#global-alert-title"),
    lastUpdate: document.querySelector("#last-update"),
    loadingView: document.querySelector("#loading-view"),
    onlineCount: document.querySelector("#online-count"),
    printerGrid: document.querySelector("#printer-grid"),
    printerTemplate: document.querySelector("#printer-template"),
    retryButton: document.querySelector("#retry-button"),
    slotTemplate: document.querySelector("#slot-template"),
    stopDialog: document.querySelector("#stop-dialog"),
    stopDryingDialog: document.querySelector("#stop-drying-dialog"),
    stopDryingDialogMessage: document.querySelector("#stop-drying-dialog-message"),
    stopDryingDialogTitle: document.querySelector("#stop-drying-dialog-title"),
    stopPrinterName: document.querySelector("#stop-printer-name"),
    toastRegion: document.querySelector("#toast-region"),
  };

  const app = {
    printers: [],
    cameraSessions: new Map(),
    cameraStatusOverrides: new Map(),
    cameraStatusPending: new Set(),
    loaded: false,
    apiHealthy: null,
    websocket: null,
    websocketOpen: false,
    reconnectAttempts: 0,
    reconnectTimer: null,
    authRedirecting: false,
    stopped: false,
    lastMessageAt: null,
    dryingTarget: null,
    stopDryingTarget: null,
    stopTargetId: null,
    pendingCommands: new Set(),
  };

  class SessionExpiredError extends Error {
    constructor() {
      super("Die Sitzung ist abgelaufen.");
      this.name = "SessionExpiredError";
    }
  }

  function redirectToLogin() {
    if (app.authRedirecting) return;
    app.authRedirecting = true;
    app.stopped = true;
    window.clearTimeout(app.reconnectTimer);
    app.websocket?.close();
    window.location.replace("/login?expired=1");
  }

  async function authenticatedFetch(resource, options) {
    if (app.authRedirecting) throw new SessionExpiredError();
    const response = await fetch(resource, options);
    if (response.status === 401) {
      redirectToLogin();
      throw new SessionExpiredError();
    }
    return response;
  }

  const STATUS_MAP = {
    CREATED: ["Wird erstellt", "idle"],
    FAILED: ["Fehler", "error"],
    FINISH: ["Fertig", "idle"],
    FINISHED: ["Fertig", "idle"],
    IDLE: ["Bereit", "idle"],
    INIT: ["Initialisiert", "idle"],
    OFFLINE: ["Offline", "error"],
    PAUSE: ["Pausiert", "paused"],
    PAUSED: ["Pausiert", "paused"],
    PREPARE: ["Vorbereitung", "printing"],
    PREPARING: ["Vorbereitung", "printing"],
    PRINTING: ["Druckt", "printing"],
    RUNNING: ["Druckt", "printing"],
    SLICING: ["Wird vorbereitet", "printing"],
    UNKNOWN: ["Unbekannt", "idle"],
  };

  const SPEED_LABELS = {
    1: "Silent",
    2: "Standard",
    3: "Sport",
    4: "Ludicrous",
    ludicrous: "Ludicrous",
    silent: "Silent",
    sport: "Sport",
    standard: "Standard",
  };

  const COMMAND_LABELS = {
    camera_recording: "Kameraaufnahme umschalten",
    camera_resolution: "Kameraauflösung ändern",
    camera_timelapse: "Zeitraffer umschalten",
    light: "Licht umschalten",
    pause: "Druck pausieren",
    refresh_rfid: "RFID einlesen",
    resume: "Druck fortsetzen",
    speed: "Geschwindigkeit ändern",
    start_drying: "AMS-Trocknung starten",
    stop: "Druck abbrechen",
    stop_drying: "AMS-Trocknung stoppen",
  };

  function numberOrNull(value) {
    if (value === null || value === undefined || value === "") return null;
    const result = Number(value);
    return Number.isFinite(result) ? result : null;
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function stringOr(value, fallback = "") {
    return value === null || value === undefined || value === "" ? fallback : String(value);
  }

  function formatTemperature(value) {
    const temperature = numberOrNull(value);
    return temperature === null ? "–" : `${Math.round(temperature)} °C`;
  }

  function formatTarget(value) {
    const temperature = numberOrNull(value);
    if (temperature === null) return "";
    return temperature > 0 ? `Ziel ${Math.round(temperature)} °C` : "Ziel aus";
  }

  function formatPercent(value) {
    const percent = numberOrNull(value);
    return percent === null ? "–" : `${Math.round(clamp(percent, 0, 100))} %`;
  }

  function formatDuration(minutesValue) {
    const minutes = numberOrNull(minutesValue);
    if (minutes === null || minutes < 0) return "–";
    if (minutes > 0 && minutes < 1) return "< 1 Min";
    const total = Math.round(minutes);
    const hours = Math.floor(total / 60);
    const rest = total % 60;
    if (hours === 0) return `${rest} Min`;
    return rest === 0 ? `${hours} Std` : `${hours} Std ${rest} Min`;
  }

  function formatRelativeTime(value) {
    if (!value) return "Keine aktuellen Daten";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Zeit unbekannt";
    const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
    if (seconds < 8) return "Gerade aktualisiert";
    if (seconds < 60) return `Vor ${seconds} Sek.`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `Vor ${minutes} Min.`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `Vor ${hours} Std.`;
    return new Intl.DateTimeFormat("de-DE", { dateStyle: "short", timeStyle: "short" }).format(date);
  }

  function getStatus(printer) {
    if (!printer.online) return { label: "Offline", key: "error", raw: "OFFLINE" };
    const raw = stringOr(printer.state?.status, "UNKNOWN").trim().toUpperCase();
    const [label, key] = STATUS_MAP[raw] || [humanize(raw), "idle"];
    return { label, key, raw };
  }

  function humanize(value) {
    return stringOr(value, "Unbekannt")
      .toLowerCase()
      .replaceAll("_", " ")
      .replace(/^./, (letter) => letter.toUpperCase());
  }

  function isActivePrinter(printer) {
    return ["printing", "paused"].includes(getStatus(printer).key);
  }

  function isDryingStartStateAllowed(printer) {
    return stringOr(printer.state?.status).trim().toLowerCase() === "idle";
  }

  function getAmsUnits(printer) {
    const units = printer.state?.ams?.units;
    return Array.isArray(units) ? units : [];
  }

  function getExternalSpool(printer) {
    const spool = printer.state?.ams?.external_spool;
    return spool && typeof spool === "object" ? spool : null;
  }

  function connectionState(printer) {
    return stringOr(printer.connection_state, printer.online ? "connected" : "disconnected").toLowerCase();
  }

  function hasPartialData(printer) {
    return Boolean(printer.stale) || ["partial", "degraded", "connecting", "reconnecting"].includes(connectionState(printer));
  }

  function hasConnectionError(printer) {
    return ["error", "failed"].includes(connectionState(printer));
  }

  function normalizeColor(value) {
    const raw = stringOr(value).trim().replace(/^#/, "");
    if (/^[0-9a-f]{8}$/i.test(raw)) return `#${raw.slice(0, 6)}`;
    if (/^[0-9a-f]{6}$/i.test(raw)) return `#${raw}`;
    if (/^[0-9a-f]{3}$/i.test(raw)) return `#${raw}`;
    return "#53625e";
  }

  function getCookie(name) {
    const encodedName = `${encodeURIComponent(name)}=`;
    const match = document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith(encodedName));
    if (!match) return "";
    try {
      return decodeURIComponent(match.slice(encodedName.length));
    } catch (_error) {
      return "";
    }
  }

  function capabilityAliases(command) {
    const aliases = {
      camera_recording: ["camera_recording"],
      camera_resolution: ["camera_resolution"],
      camera_timelapse: ["camera_timelapse"],
      light: ["light", "chamber_light"],
      pause: ["pause"],
      refresh_rfid: ["refresh_rfid", "rfid", "ams_rfid"],
      resume: ["resume"],
      speed: ["speed", "speed_level", "print_speed"],
      start_drying: ["start_drying", "ams_start_drying"],
      stop: ["stop", "abort", "cancel"],
      stop_drying: ["stop_drying", "ams_stop_drying"],
    };
    return aliases[command] || [command];
  }

  function supports(printer, command) {
    const capabilities = Array.isArray(printer.capabilities) ? printer.capabilities : [];
    const normalized = capabilities.map((item) => String(item).toLowerCase());
    return normalized.includes("*") || capabilityAliases(command).some((item) => normalized.includes(item));
  }

  function normalizedIdentifier(value) {
    return stringOr(value).trim().toUpperCase().replaceAll(/[^A-Z0-9]/g, "");
  }

  function printerBlocksDrying(printer) {
    const model = normalizedIdentifier(printer.model);
    return model.startsWith("P1") || model.startsWith("A1");
  }

  function isExperimentalDrying(printer, unit) {
    return unit?.experimental === true || normalizedIdentifier(printer.model).startsWith("X1");
  }

  function dryingModelInfo(unit) {
    const rawModel = stringOr(unit?.model);
    const normalized = normalizedIdentifier(rawModel);
    const slots = Array.isArray(unit?.slots) ? unit.slots : [];
    const numericId = numberOrNull(unit?.id);
    const inferredHt = !normalized && numericId === 128 && slots.length === 1;
    const inferred = inferredHt || ["inferred", "heuristic", "guessed"].includes(stringOr(unit?.model_source).toLowerCase());

    if (normalized.includes("AMS2PRO") || normalized === "AMS2") {
      return { kind: "ams2pro", label: "AMS 2 Pro", minimum: 45, maximum: 65, inferred };
    }
    if (normalized.includes("AMSHT") || normalized === "HT" || inferredHt) {
      return { kind: "amsht", label: "AMS HT", minimum: 45, maximum: 85, inferred };
    }
    if (normalized === "AMS" || normalized.includes("AMSLITE") || normalized.includes("CLASSIC")) {
      return { kind: "classic", label: rawModel || "AMS", minimum: null, maximum: null, inferred };
    }
    return {
      kind: "unknown",
      label: rawModel || "Trocknungsfähiges AMS",
      minimum: 45,
      maximum: 65,
      inferred,
    };
  }

  function isDryCapable(printer, unit) {
    if (printerBlocksDrying(printer)) return false;
    const model = dryingModelInfo(unit);
    if (model.kind === "classic") return false;
    if (unit?.dry_capable === true) return true;
    if (unit?.dry_capable === false) return false;
    const recognized = ["ams2pro", "amsht"].includes(model.kind);
    return recognized && (supports(printer, "start_drying") || supports(printer, "stop_drying"));
  }

  function dryingState(unit) {
    const value = unit?.drying && typeof unit.drying === "object" ? unit.drying : {};
    const rawStatus = stringOr(value.status ?? unit?.drying_status).toLowerCase();
    const active = value.active === true
      || unit?.drying_active === true
      || ["active", "drying", "running", "heating"].includes(rawStatus);
    return {
      active,
      remainingMinutes: numberOrNull(value.remaining_minutes ?? value.dry_time ?? unit?.dry_time ?? unit?.dry_remaining_minutes),
      temperature: numberOrNull(value.temperature ?? value.target_temperature ?? value.temp ?? unit?.dry_temperature),
      durationHours: numberOrNull(value.duration_hours ?? value.duration ?? unit?.dry_duration_hours),
      filament: stringOr(value.filament ?? unit?.dry_filament),
    };
  }

  function formatHours(value) {
    const hours = numberOrNull(value);
    if (hours === null || hours < 0) return "–";
    const rounded = Math.round(hours * 10) / 10;
    return `${rounded.toLocaleString("de-DE")} Std`;
  }

  function commandKey(printerId, command) {
    return `${printerId}:${command}`;
  }

  function isPending(printerId, command) {
    return app.pendingCommands.has(commandKey(printerId, command));
  }

  function setText(root, selector, value) {
    const element = root.querySelector(selector);
    if (element) element.textContent = stringOr(value, "–");
    return element;
  }

  function setConnectionState(kind, label) {
    dom.connectionStatus.classList.remove("is-live", "is-connecting", "is-error");
    dom.connectionStatus.classList.add(`is-${kind}`);
    dom.connectionLabel.textContent = label;
  }

  function updateConnectionDisplay() {
    if (!navigator.onLine) {
      setConnectionState("error", "Browser ist offline");
    } else if (app.websocketOpen) {
      setConnectionState("live", "Live verbunden");
    } else if (app.apiHealthy === false) {
      setConnectionState("error", "Dienst nicht erreichbar");
    } else {
      setConnectionState("connecting", app.loaded ? "Verbindung wird erneuert" : "Verbindung wird hergestellt");
    }

    dom.lastUpdate.textContent = app.lastMessageAt
      ? `Stand: ${formatRelativeTime(app.lastMessageAt)}`
      : "Noch keine Daten";
  }

  function showGlobalAlert(title, message) {
    dom.globalAlertTitle.textContent = title;
    dom.globalAlertMessage.textContent = message;
    dom.globalAlert.hidden = false;
  }

  function hideGlobalAlert() {
    dom.globalAlert.hidden = true;
  }

  function showToast(message, type = "success") {
    const toast = document.createElement("div");
    toast.className = `toast${type === "error" ? " is-error" : ""}`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    toast.textContent = message;
    dom.toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), type === "error" ? 7000 : 4200);
  }

  function setSnapshot(payload) {
    if (!payload || !Array.isArray(payload.printers)) return false;
    app.printers = payload.printers;
    app.loaded = true;
    app.lastMessageAt = new Date();
    app.apiHealthy = true;
    hideGlobalAlert();
    render();
    return true;
  }

  function applySocketMessage(message) {
    const data = message?.data ?? message;
    if (setSnapshot(data)) return;
    if (message?.type === "snapshot" && setSnapshot(data)) return;

    const printer = data?.printer ?? (data?.id && data?.state ? data : null);
    if (!printer) return;
    const index = app.printers.findIndex((item) => String(item.id) === String(printer.id));
    if (index === -1) app.printers.push(printer);
    else app.printers[index] = printer;
    app.loaded = true;
    app.lastMessageAt = new Date();
    render();
  }

  function render() {
    dom.loadingView.hidden = app.loaded;
    dom.loadingView.setAttribute("aria-busy", String(!app.loaded));

    if (!app.loaded) {
      updateConnectionDisplay();
      return;
    }

    const printers = app.printers.filter((printer) => printer && printer.id !== undefined);
    const online = printers.filter((printer) => Boolean(printer.online)).length;
    const active = printers.filter(isActivePrinter).length;
    const ams = printers.reduce((total, printer) => total + getAmsUnits(printer).length, 0);

    dom.onlineCount.textContent = `${online}/${printers.length}`;
    dom.activeCount.textContent = String(active);
    dom.amsCount.textContent = String(ams);
    dom.emptyView.hidden = printers.length !== 0;
    dom.printerGrid.hidden = printers.length === 0;

    const existingCameraPanels = new Map(
      [...dom.printerGrid.querySelectorAll(".camera-panel[data-printer-id]")]
        .map((panel) => [panel.dataset.printerId, panel]),
    );
    const fragment = document.createDocumentFragment();
    printers.forEach((printer) => fragment.append(renderPrinter(printer, existingCameraPanels.get(String(printer.id)))));
    dom.printerGrid.replaceChildren(fragment);
    updateConnectionDisplay();
  }

  function renderPrinter(printer, existingCameraPanel = null) {
    const card = dom.printerTemplate.content.firstElementChild.cloneNode(true);
    const state = printer.state || {};
    const status = getStatus(printer);
    const printerId = String(printer.id);
    const printerName = stringOr(printer.name, `Drucker ${printerId}`);

    card.dataset.printerId = printerId;
    card.setAttribute("aria-label", `${printerName}, ${printer.online ? "online" : "offline"}`);
    card.classList.toggle("is-offline", !printer.online);
    card.classList.toggle("is-stale", hasPartialData(printer));

    setText(card, ".printer-name", printerName);
    const metaParts = [printer.model, printerId].filter(Boolean);
    setText(card, ".printer-meta", metaParts.join(" · ") || "Bambu-Drucker");
    setText(card, ".printer-freshness", formatRelativeTime(printer.last_seen));

    let connectionLabel = "Online";
    let connectionClass = "is-online";
    if (!printer.online) {
      connectionLabel = "Offline";
      connectionClass = "is-offline";
    } else if (hasConnectionError(printer)) {
      connectionLabel = "Verbindungsfehler";
      connectionClass = "is-offline";
    } else if (hasPartialData(printer)) {
      connectionLabel = printer.stale ? "Daten veraltet" : "Teildaten";
      connectionClass = "is-stale";
    }
    const statusBadge = setText(card, ".status-badge", connectionLabel);
    statusBadge.classList.add(connectionClass);

    renderNotice(card, printer);
    renderJob(card, printer, state, status);
    renderTelemetry(card, state);
    let cameraPanel = card.querySelector(".camera-panel");
    if (existingCameraPanel) {
      existingCameraPanel.remove();
      cameraPanel.replaceWith(existingCameraPanel);
      cameraPanel = existingCameraPanel;
    }
    renderCameraPanel(cameraPanel, printer);
    renderControls(card, printer, status);
    renderAms(card, printer);
    return card;
  }

  function renderNotice(card, printer) {
    const notice = card.querySelector(".printer-notice");
    const errors = Array.isArray(printer.state?.errors) ? printer.state.errors : [];
    const seriousErrors = errors.filter((error) => String(error?.severity || "").toLowerCase() !== "info");

    if (seriousErrors.length) {
      const messages = seriousErrors.slice(0, 2).map((error) => {
        const code = error?.code ? ` (${error.code})` : "";
        return `${stringOr(error?.message, "Unbekannter Druckerfehler")}${code}`;
      });
      if (seriousErrors.length > 2) messages.push(`und ${seriousErrors.length - 2} weitere`);
      setText(notice, ".printer-notice-text", messages.join(" · "));
      notice.classList.add("is-error");
      notice.hidden = false;
      return;
    }

    if (!printer.online) {
      setText(notice, ".printer-notice-text", "Der Drucker ist nicht erreichbar. Angezeigte Werte können veraltet sein; Befehle sind gesperrt.");
      notice.hidden = false;
      return;
    }

    if (hasConnectionError(printer)) {
      setText(notice, ".printer-notice-text", "Die MQTT-Verbindung meldet einen Fehler. Live-Steuerung ist vorübergehend nicht verfügbar.");
      notice.classList.add("is-error");
      notice.hidden = false;
      return;
    }

    if (hasPartialData(printer)) {
      setText(notice, ".printer-notice-text", "Es liegen nur teilweise oder veraltete Live-Daten vor. Befehle bleiben bis zur nächsten vollständigen Meldung gesperrt.");
      notice.hidden = false;
    }
  }

  function renderJob(card, printer, state, status) {
    const taskName = state.task_name || state.gcode_file || (status.key === "idle" ? "Kein aktiver Druck" : "Unbekannter Auftrag");
    setText(card, ".job-name", taskName);

    const jobState = setText(card, ".job-state", status.label);
    jobState.classList.add(`is-${status.key}`);

    const progressValue = numberOrNull(state.progress);
    const progress = progressValue === null ? 0 : clamp(progressValue, 0, 100);
    const progressRing = card.querySelector(".progress-ring");
    progressRing.style.setProperty("--progress", String(progress));
    if (progressValue === null) {
      progressRing.removeAttribute("aria-valuenow");
      progressRing.setAttribute("aria-valuetext", "Fortschritt unbekannt");
    } else {
      progressRing.setAttribute("aria-valuenow", String(Math.round(progress)));
      progressRing.setAttribute("aria-valuetext", `${Math.round(progress)} Prozent`);
    }
    setText(card, ".progress-value", progressValue === null ? "–" : `${Math.round(progress)}%`);
    setText(card, ".remaining-time", formatDuration(state.remaining_minutes));

    const currentLayer = numberOrNull(state.current_layer);
    const totalLayers = numberOrNull(state.total_layers);
    const layerLabel = currentLayer === null
      ? "–"
      : totalLayers === null
        ? String(Math.round(currentLayer))
        : `${Math.round(currentLayer)} / ${Math.round(totalLayers)}`;
    setText(card, ".layer-count", layerLabel);
    setText(card, ".speed-level", speedLabel(state.speed_level));
  }

  function speedLabel(value) {
    const normalized = stringOr(value).toLowerCase();
    return SPEED_LABELS[normalized] || (normalized ? humanize(normalized) : "–");
  }

  function speedValue(value) {
    const normalized = stringOr(value).toLowerCase();
    const numeric = { 1: "silent", 2: "standard", 3: "sport", 4: "ludicrous" };
    return numeric[normalized] || (Object.hasOwn(SPEED_LABELS, normalized) ? normalized : "standard");
  }

  function renderTelemetry(card, state) {
    const temperatures = state.temperatures || {};
    const nozzle = temperatures.nozzle || {};
    const bed = temperatures.bed || {};
    const chamber = temperatures.chamber || {};
    setText(card, ".temp-nozzle", formatTemperature(nozzle.current));
    setText(card, ".target-nozzle", formatTarget(nozzle.target));
    setText(card, ".temp-bed", formatTemperature(bed.current));
    setText(card, ".target-bed", formatTarget(bed.target));
    setText(card, ".temp-chamber", formatTemperature(chamber.current));
    setText(card, ".target-chamber", formatTarget(chamber.target));

    const fans = state.fans || {};
    setText(card, ".fan-part", formatPercent(fans.part));
    setText(card, ".fan-aux", formatPercent(fans.aux));
    setText(card, ".fan-chamber", formatPercent(fans.chamber));
  }

  function cameraSnapshot(printer) {
    const snapshot = printer.camera && typeof printer.camera === "object"
      ? printer.camera
      : printer.state?.camera && typeof printer.state.camera === "object"
        ? printer.state.camera
        : {};
    const fallback = app.cameraStatusOverrides.get(String(printer.id)) || {};
    const merged = { ...fallback };
    Object.entries(snapshot).forEach(([key, value]) => {
      if (value !== null && value !== undefined) merged[key] = value;
      else if (!(key in merged)) merged[key] = value;
    });
    return merged;
  }

  function cameraSession(printerId) {
    const key = String(printerId);
    if (!app.cameraSessions.has(key)) {
      app.cameraSessions.set(key, {
        phase: "idle",
        streamUrl: "",
        error: "",
        requestId: 0,
      });
    }
    return app.cameraSessions.get(key);
  }

  function clearCameraImage(panel) {
    const image = panel?.querySelector(".camera-image");
    if (!image) return;
    image.removeAttribute("src");
    image.hidden = true;
  }

  function setCameraPanelState(panel, state, title, message) {
    panel.dataset.cameraState = state;
    panel.classList.toggle("is-live", state === "live");
    panel.classList.toggle("is-error", state === "error");
    setText(panel, ".camera-placeholder-title", title);
    setText(panel, ".camera-placeholder-message", message);
    const badge = setText(panel, ".camera-state-badge", title);
    badge.className = `camera-state-badge is-${state}`;
  }

  function renderCameraPanel(panel, printer) {
    const printerId = String(printer.id);
    const camera = cameraSnapshot(printer);
    const session = cameraSession(printerId);
    const image = panel.querySelector(".camera-image");
    const placeholder = panel.querySelector(".camera-placeholder");
    const liveIndicator = panel.querySelector(".camera-live-indicator");
    const startButton = panel.querySelector(".camera-start-button");
    const stopButton = panel.querySelector(".camera-stop-button");
    const startLabel = startButton.querySelector("span");
    const connectionError = hasConnectionError(printer);
    const partialData = hasPartialData(printer);
    const remoteActive = camera.active === true || camera.streaming === true;
    const externallyActive = session.phase === "idle" && remoteActive;
    const statusKnown = camera._status_checked === true;
    const streamAvailable = statusKnown && camera._stream_available === true;

    panel.dataset.printerId = printerId;
    panel.setAttribute("aria-label", `Kamera-Livebild von ${stringOr(printer.name, printerId)}`);
    image.alt = `Livebild von ${stringOr(printer.name, printerId)}`;
    setText(panel, ".camera-sidebar-title", camera.resolution ? `Livebild · ${camera.resolution}` : "Livebild nur bei Bedarf");
    startButton.dataset.printerId = printerId;
    stopButton.dataset.printerId = printerId;

    const availableKnown = typeof camera.available === "boolean";
    const statusCheckNeeded = !statusKnown;
    if (statusCheckNeeded && printer.online && !app.cameraStatusPending.has(printerId)) {
      queueMicrotask(() => ensureCameraStatus(printer));
    }

    if (!printer.online) {
      clearCameraImage(panel);
      session.phase = "idle";
      session.streamUrl = "";
      setCameraPanelState(panel, "offline", "Drucker offline", "Das Livebild ist verfügbar, sobald der Drucker wieder verbunden ist.");
    } else if (connectionError) {
      clearCameraImage(panel);
      session.phase = "idle";
      session.streamUrl = "";
      setCameraPanelState(panel, "error", "Verbindung gestört", "Der Kamerastream startet erst wieder bei einer stabilen lokalen Verbindung.");
    } else if (partialData) {
      clearCameraImage(panel);
      session.phase = "idle";
      session.streamUrl = "";
      setCameraPanelState(panel, "error", "Live-Daten unvollständig", "Warte auf einen vollständigen, aktuellen Druckerstatus.");
    } else if (camera._status_error) {
      setCameraPanelState(panel, "error", "Kamerastatus nicht erreichbar", stringOr(camera._status_error, "Die Kamera konnte nicht geprüft werden."));
    } else if (!statusKnown) {
      setCameraPanelState(panel, "loading", "Kamera wird geprüft", "Die lokale Verfügbarkeit wird geladen.");
    } else if (camera.supported === false) {
      clearCameraImage(panel);
      session.phase = "idle";
      session.streamUrl = "";
      setCameraPanelState(panel, "unsupported", "Modell nicht unterstützt", "Für dieses Druckermodell ist kein lokaler Kamerastream freigegeben.");
    } else if (camera.enabled === false) {
      clearCameraImage(panel);
      session.phase = "idle";
      session.streamUrl = "";
      setCameraPanelState(panel, "disabled", "Kamera nicht freigegeben", "Gib das lokale Livebild bei der Einrichtung beziehungsweise in der Serverkonfiguration frei.");
    } else if (camera.available === false) {
      clearCameraImage(panel);
      session.phase = "idle";
      session.streamUrl = "";
      setCameraPanelState(panel, "unsupported", "Kamera nicht verfügbar", "Der Drucker meldet derzeit keine lokal nutzbare Kamera.");
    } else if (externallyActive) {
      setCameraPanelState(panel, "disabled", "Kamera bereits aktiv", "Ein anderes Browserfenster nutzt den lokalen Stream. Dieses Fenster startet keine zweite Sitzung.");
    } else if (!streamAvailable) {
      const unsafeTls = camera.reason === "unsafe_tls";
      setCameraPanelState(
        panel,
        "error",
        unsafeTls ? "Sichere Verbindung fehlt" : "Lokaler Stream nicht verfügbar",
        unsafeTls
          ? "Der Server verweigert die Kamera, solange die Druckerverbindung nicht sicher per Zertifikat geprüft wird."
          : "Aktiviere den lokalen RTSPS-Livestream am Drucker und warte auf einen aktuellen Status.",
      );
    } else if (session.phase === "starting") {
      setCameraPanelState(panel, "loading", "Livebild startet", "Die erste lokale Kameraufnahme wird geladen …");
    } else if (session.phase === "live") {
      setCameraPanelState(panel, "live", "Live verbunden", "Das Livebild läuft nur in dieser Browser-Sitzung.");
    } else if (session.phase === "error") {
      setCameraPanelState(panel, "error", "Livebild nicht verfügbar", session.error || "Der Kamerastream wurde unerwartet beendet.");
    } else {
      setCameraPanelState(panel, "ready", "Bereit", "Starte das Livebild bei Bedarf. Es erfolgt keine automatische Verbindung.");
    }

    const live = session.phase === "live";
    const starting = session.phase === "starting";
    const canStart = printer.online && !connectionError && !partialData
      && camera.available === true && camera.supported !== false
      && camera.enabled !== false && streamAvailable && !remoteActive;
    const streamRequested = starting && Boolean(session.streamUrl);
    image.hidden = !live && !streamRequested;
    placeholder.hidden = live || streamRequested;
    liveIndicator.hidden = !live;
    startButton.hidden = live || starting;
    stopButton.hidden = !live && !starting;
    startButton.disabled = !canStart;
    startLabel.textContent = session.phase === "error" ? "Erneut versuchen" : "Kamera starten";
    panel.setAttribute("aria-busy", String(starting || !statusKnown || !availableKnown));

    if (starting && session.streamUrl && !image.hasAttribute("src")) {
      image.src = session.streamUrl;
    }
  }

  async function ensureCameraStatus(printer, force = false) {
    const printerId = String(printer.id);
    if (!printer.online || app.cameraStatusPending.has(printerId)) return;
    const existing = cameraSnapshot(printer);
    if (!force && typeof existing.available === "boolean"
      && (typeof existing.enabled === "boolean" || existing._status_checked === true)) return;
    app.cameraStatusPending.add(printerId);
    try {
      const response = await authenticatedFetch(`/api/printers/${encodeURIComponent(printerId)}/camera/status`, {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (response.status === 404) {
        app.cameraStatusOverrides.set(printerId, {
          _status_checked: true,
          ...(typeof existing.available === "boolean" ? {} : { available: false }),
        });
      } else if (!response.ok) {
        app.cameraStatusOverrides.set(printerId, { _status_checked: true, _status_error: await responseError(response) });
      } else {
        const payload = await response.json();
        const camera = payload?.camera && typeof payload.camera === "object" ? payload.camera : payload;
        const normalized = camera && typeof camera === "object" ? { ...camera } : null;
        if (normalized && typeof normalized.supported === "boolean") {
          normalized._stream_available = normalized.available;
          normalized.available = normalized.supported;
        }
        if (normalized && typeof normalized.active === "boolean") normalized.streaming = normalized.active;
        app.cameraStatusOverrides.set(
          printerId,
          normalized
            ? { ...normalized, _status_checked: true }
            : { _status_checked: true, _status_error: "Ungültige Kameraantwort." },
        );
      }
    } catch (error) {
      if (error instanceof SessionExpiredError) return;
      app.cameraStatusOverrides.set(printerId, { _status_checked: true, _status_error: readableError(error) });
    } finally {
      app.cameraStatusPending.delete(printerId);
      render();
    }
  }

  async function startCameraSession(printerId) {
    const printer = findPrinter(printerId);
    if (!printer) return;
    const camera = cameraSnapshot(printer);
    if (!printer.online || hasPartialData(printer) || hasConnectionError(printer)
      || camera.available !== true || camera.supported === false || camera.enabled === false
      || camera._stream_available !== true
      || camera.active === true || camera.streaming === true) return;
    const session = cameraSession(printerId);
    session.requestId += 1;
    const requestId = session.requestId;
    session.phase = "starting";
    session.error = "";
    session.streamUrl = "";
    const panel = [...dom.printerGrid.querySelectorAll(".camera-panel[data-printer-id]")]
      .find((candidate) => candidate.dataset.printerId === String(printerId));
    if (panel) {
      clearCameraImage(panel);
      renderCameraPanel(panel, printer);
    }

    const csrfToken = getCookie("bambu_csrf");
    if (!csrfToken) {
      session.phase = "error";
      session.error = "Sicherheitstoken fehlt. Bitte lade die Seite neu.";
      if (panel) renderCameraPanel(panel, printer);
      return;
    }

    try {
      const response = await authenticatedFetch(`/api/printers/${encodeURIComponent(printerId)}/camera/ticket`, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: "{}",
      });
      if (!response.ok) throw new Error(await responseError(response));
      if (session.requestId !== requestId || session.phase !== "starting") return;

      const currentPanel = [...dom.printerGrid.querySelectorAll(".camera-panel[data-printer-id]")]
        .find((candidate) => candidate.dataset.printerId === String(printerId));
      const currentPrinter = findPrinter(printerId);
      const currentImage = currentPanel?.querySelector(".camera-image");
      if (!currentPanel || !currentPrinter || !currentImage) {
        session.phase = "idle";
        return;
      }
      session.streamUrl = `/api/printers/${encodeURIComponent(printerId)}/camera/stream`;
      currentImage.src = session.streamUrl;
      session.phase = "live";
      renderCameraPanel(currentPanel, currentPrinter);
    } catch (error) {
      if (error instanceof SessionExpiredError) return;
      if (session.requestId !== requestId || session.phase !== "starting") return;
      session.phase = "error";
      session.streamUrl = "";
      session.error = `Der Kamerastart ist fehlgeschlagen: ${readableError(error)}`;
      const currentPanel = [...dom.printerGrid.querySelectorAll(".camera-panel[data-printer-id]")]
        .find((candidate) => candidate.dataset.printerId === String(printerId));
      const currentPrinter = findPrinter(printerId);
      if (currentPanel && currentPrinter) renderCameraPanel(currentPanel, currentPrinter);
    }
  }

  async function stopCameraSession(printerId) {
    const session = cameraSession(printerId);
    session.requestId += 1;
    session.phase = "idle";
    session.streamUrl = "";
    session.error = "";
    const statusOverride = app.cameraStatusOverrides.get(String(printerId));
    if (statusOverride) {
      app.cameraStatusOverrides.set(String(printerId), {
        ...statusOverride,
        active: false,
        streaming: false,
      });
    }
    const panel = [...dom.printerGrid.querySelectorAll(".camera-panel[data-printer-id]")]
      .find((candidate) => candidate.dataset.printerId === String(printerId));
    clearCameraImage(panel);
    const printer = findPrinter(printerId);
    if (panel && printer) renderCameraPanel(panel, printer);

    const csrfToken = getCookie("bambu_csrf");
    if (csrfToken) {
      try {
        const response = await authenticatedFetch(`/api/printers/${encodeURIComponent(printerId)}/camera/stop`, {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          keepalive: true,
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: "{}",
        });
        if (!response.ok) throw new Error(await responseError(response));
      } catch (error) {
        if (error instanceof SessionExpiredError) return;
        showToast(`Kamerastream konnte serverseitig nicht beendet werden: ${readableError(error)}`, "error");
      }
    }
    window.setTimeout(() => {
      const currentPrinter = findPrinter(printerId);
      if (currentPrinter?.online) ensureCameraStatus(currentPrinter, true);
    }, 500);
  }

  function renderControls(card, printer, status) {
    const printerId = String(printer.id);
    const camera = cameraSnapshot(printer);
    const recording = cameraToggleState(camera.recording);
    const timelapse = cameraToggleState(camera.timelapse);
    const actionable = Boolean(printer.online && !hasPartialData(printer) && !hasConnectionError(printer) && printer.writable);
    const readOnlyBadge = card.querySelector(".readonly-badge");
    readOnlyBadge.hidden = Boolean(printer.writable);

    card.querySelectorAll("[data-command]").forEach((control) => {
      const command = control.dataset.command;
      control.dataset.printerId = printerId;
      const supported = supports(printer, command);
      if (control.matches("select")) control.closest(".speed-control").hidden = !supported;
      else control.hidden = !supported;

      let availableForState = true;
      if (command === "pause") availableForState = status.key === "printing";
      if (command === "resume") availableForState = status.key === "paused";
      if (command === "stop") availableForState = ["printing", "paused"].includes(status.key);
      if (command === "speed") availableForState = ["printing", "paused"].includes(status.key);
      if (["camera_recording", "camera_timelapse", "camera_resolution"].includes(command)) {
        availableForState = camera.available === true
          && camera.supported !== false;
      }
      if (command === "camera_recording") availableForState = availableForState && recording !== null;
      if (command === "camera_timelapse") availableForState = availableForState && timelapse !== null;
      if (command === "camera_resolution") {
        const supportedResolutions = Array.isArray(camera.resolution_supported) ? camera.resolution_supported : [];
        availableForState = availableForState && (supportedResolutions.length > 0 || Boolean(camera.resolution));
      }
      const pending = isPending(printerId, command);
      control.disabled = !actionable || !availableForState || pending;
      control.classList.toggle("is-pending", pending);

      if (!supported) control.title = "Von diesem Modell nicht unterstützt";
      else if (!printer.online) control.title = "Drucker ist offline";
      else if (hasPartialData(printer)) control.title = "Live-Daten sind unvollständig oder veraltet";
      else if (hasConnectionError(printer)) control.title = "MQTT-Verbindung ist gestört";
      else if (!printer.writable) control.title = "Schreibzugriff ist deaktiviert";
      else if (!availableForState) control.title = "Im aktuellen Druckzustand nicht verfügbar";
      else control.removeAttribute("title");
    });

    const lightState = stringOr(printer.state?.lights?.chamber, "unknown").toLowerCase();
    const lightButton = card.querySelector(".light-button");
    lightButton.dataset.nextState = lightState === "on" ? "off" : "on";
    lightButton.setAttribute("aria-pressed", String(lightState === "on"));
    setText(lightButton, ".light-label", lightState === "on" ? "Licht aus" : lightState === "off" ? "Licht an" : "Licht");

    const speedSelect = card.querySelector(".speed-select");
    speedSelect.value = speedValue(printer.state?.speed_level);

    const recordingButton = card.querySelector(".camera-recording-button");
    recordingButton.dataset.nextState = String(recording === false);
    recordingButton.setAttribute("aria-pressed", recording === null ? "false" : String(recording));
    setText(
      recordingButton,
      ".camera-recording-label",
      recording === null ? "Aufnahmestatus unbekannt" : recording ? "Aufnahme stoppen" : "Aufnahme starten",
    );

    const timelapseButton = card.querySelector(".camera-timelapse-button");
    timelapseButton.dataset.nextState = String(timelapse === false);
    timelapseButton.setAttribute("aria-pressed", timelapse === null ? "false" : String(timelapse));
    setText(
      timelapseButton,
      ".camera-timelapse-label",
      timelapse === null ? "Zeitrafferstatus unbekannt" : timelapse ? "Zeitraffer deaktivieren" : "Zeitraffer aktivieren",
    );

    renderResolutionOptions(card.querySelector(".camera-resolution-select"), camera);

    const additionalCommands = ["light", "speed", "camera_recording", "camera_timelapse", "camera_resolution"];
    card.querySelector(".additional-controls-panel").hidden = !additionalCommands.some((command) => supports(printer, command));
  }

  function cameraToggleState(value) {
    if (value === true) return true;
    if (value === false) return false;
    const normalized = stringOr(value).toLowerCase();
    if (["true", "on", "enabled", "recording", "active"].includes(normalized)) return true;
    if (["false", "off", "disabled", "stopped", "inactive"].includes(normalized)) return false;
    return null;
  }

  function renderResolutionOptions(select, camera) {
    const values = Array.isArray(camera.resolution_supported)
      ? camera.resolution_supported.map((value) => stringOr(value).trim()).filter(Boolean)
      : [];
    const current = stringOr(camera.resolution).trim();
    if (current && !values.includes(current)) values.unshift(current);
    const unique = [...new Set(values)];
    const fragment = document.createDocumentFragment();
    unique.forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      fragment.append(option);
    });
    select.replaceChildren(fragment);
    if (current && unique.includes(current)) select.value = current;
  }

  function renderAms(card, printer) {
    const units = getAmsUnits(printer);
    const externalSpool = getExternalSpool(printer);
    const amsList = card.querySelector(".ams-list");
    const noAms = card.querySelector(".no-ams");
    const countBadge = card.querySelector(".ams-count-badge");
    const activeTray = printer.state?.active_tray;

    const externalLabel = externalSpool ? " · externe Spule" : "";
    countBadge.textContent = `${units.length} ${units.length === 1 ? "System" : "Systeme"}${externalLabel}`;
    noAms.hidden = units.length !== 0 || Boolean(externalSpool);
    amsList.hidden = units.length === 0 && !externalSpool;

    const fragment = document.createDocumentFragment();
    units.forEach((unit, unitIndex) => fragment.append(renderAmsUnit(printer, unit, unitIndex, activeTray)));
    if (externalSpool) fragment.append(renderExternalSpool(externalSpool, activeTray));
    amsList.replaceChildren(fragment);
  }

  function renderExternalSpool(spool, activeTray) {
    const spoolCard = dom.amsTemplate.content.firstElementChild.cloneNode(true);
    setText(spoolCard, ".ams-name", "Externe Spule");
    setText(spoolCard, ".ams-environment", "Externer Spulenhalter");
    spoolCard.querySelector(".ams-actions")?.remove();
    const activeBadge = spoolCard.querySelector(".ams-active-badge");
    activeBadge.hidden = !slotIsActive(spool, activeTray);
    const slot = renderSlot(spool, 0, activeTray);
    setText(slot, ".slot-number", "Externe Spule");
    spoolCard.querySelector(".slot-grid").replaceChildren(slot);
    return spoolCard;
  }

  function renderAmsUnit(printer, unit, unitIndex, activeTray) {
    const amsCard = dom.amsTemplate.content.firstElementChild.cloneNode(true);
    const unitId = stringOr(unit.id, String(unitIndex));
    const slots = Array.isArray(unit.slots) ? unit.slots : [];
    amsCard.classList.toggle("is-single-slot", slots.length === 1);
    const isActive = slots.some((slot) => slotIsActive(slot, activeTray));
    const numericUnitId = /^\d+$/.test(unitId) ? Number(unitId) : null;
    const unitName = numericUnitId === null
      ? `AMS ${unitId}`
      : numericUnitId <= 3
        ? `AMS ${numericUnitId + 1}`
        : `AMS ID ${numericUnitId}`;

    const dryCapable = isDryCapable(printer, unit);
    const modelInfo = dryingModelInfo(unit);
    const experimental = dryCapable && isExperimentalDrying(printer, unit);

    setText(amsCard, ".ams-name", unitName);
    const activeBadge = amsCard.querySelector(".ams-active-badge");
    activeBadge.hidden = !isActive;
    const modelBadge = setText(amsCard, ".ams-model-badge", modelInfo.label);
    modelBadge.hidden = !dryCapable;
    modelBadge.classList.toggle("is-inferred", modelInfo.inferred);
    const experimentalBadge = amsCard.querySelector(".ams-experimental-badge");
    experimentalBadge.hidden = !experimental;

    const environment = [];
    const humidityPercent = numberOrNull(unit.humidity_percent);
    const humidityIndex = numberOrNull(unit.humidity_index);
    if (humidityPercent !== null) environment.push(`Feuchte ${Math.round(humidityPercent)} %`);
    else if (humidityIndex !== null) environment.push(`Feuchte Stufe ${Math.round(humidityIndex)}`);
    const temperature = numberOrNull(unit.temperature);
    if (temperature !== null) environment.push(`${Math.round(temperature)} °C`);
    setText(amsCard, ".ams-environment", environment.join(" · ") || "Umgebungswerte nicht verfügbar");

    renderDryingPanel(amsCard, printer, unit, unitId, modelInfo, dryCapable, experimental);

    amsCard.querySelectorAll("[data-command]").forEach((button) => {
      const command = button.dataset.command;
      const supported = supports(printer, command);
      button.hidden = !supported;
      button.dataset.printerId = String(printer.id);
      button.dataset.amsId = unitId;
      const pending = isPending(String(printer.id), command);
      button.disabled = !printer.online || hasPartialData(printer) || hasConnectionError(printer) || !printer.writable || pending;
      button.classList.toggle("is-pending", pending);
      if (!printer.online) button.title = "Drucker ist offline";
      else if (hasPartialData(printer)) button.title = "Live-Daten sind unvollständig oder veraltet";
      else if (hasConnectionError(printer)) button.title = "MQTT-Verbindung ist gestört";
      else if (!printer.writable) button.title = "Schreibzugriff ist deaktiviert";
    });

    const slotGrid = amsCard.querySelector(".slot-grid");
    const fragment = document.createDocumentFragment();
    slots.forEach((slot, slotIndex) => fragment.append(renderSlot(slot, slotIndex, activeTray)));
    slotGrid.replaceChildren(fragment);
    return amsCard;
  }

  function renderDryingPanel(amsCard, printer, unit, unitId, modelInfo, dryCapable, experimental) {
    const panel = amsCard.querySelector(".drying-panel");
    panel.hidden = !dryCapable;
    if (!dryCapable) return;

    const drying = dryingState(unit);
    const printerId = String(printer.id);
    const activeView = panel.querySelector(".drying-active-view");
    const idleView = panel.querySelector(".drying-idle-view");
    const stateBadge = panel.querySelector(".drying-state-badge");
    const startButton = panel.querySelector(".drying-start-button");
    const stopButton = panel.querySelector(".drying-stop-button");
    const availabilityNote = panel.querySelector(".drying-availability-note");
    const experimentalNote = panel.querySelector(".drying-experimental-note");
    const startingAllowed = supports(printer, "start_drying");
    const stoppingAllowed = supports(printer, "stop_drying");

    panel.classList.toggle("is-active", drying.active);
    activeView.hidden = !drying.active;
    idleView.hidden = drying.active;
    stateBadge.textContent = drying.active ? "Trocknet" : "Bereit";
    stateBadge.classList.toggle("is-active", drying.active);
    setText(panel, ".drying-model-note", `${modelInfo.label} · ${modelInfo.minimum}–${modelInfo.maximum} °C`);
    setText(panel, ".drying-remaining", formatDuration(drying.remainingMinutes));
    setText(panel, ".drying-temperature", formatTemperature(drying.temperature));
    setText(panel, ".drying-duration", formatHours(drying.durationHours));
    setText(panel, ".drying-filament", drying.filament ? `Material: ${drying.filament}` : "Material nicht gemeldet");
    setText(
      panel,
      ".drying-stop-help",
      !stoppingAllowed
        ? "Der Sicherheits-Stopp ist in den lokalen Rechten nicht freigegeben."
        : drying.active
        ? "Beendet die aktuell gemeldete Trocknung sofort."
        : "Falls ein Start nicht bestätigt wurde, beendet dieser Sicherheits-Stopp einen möglicherweise trotzdem laufenden Vorgang.",
    );
    stopButton.textContent = drying.active ? "Trocknung stoppen" : "Sicherheits-Stopp senden";

    [startButton, stopButton].forEach((button) => {
      button.dataset.printerId = printerId;
      button.dataset.amsId = unitId;
    });
    startButton.dataset.minimum = String(modelInfo.minimum);
    startButton.dataset.maximum = String(modelInfo.maximum);
    startButton.dataset.experimental = String(experimental);

    const startStateAllowed = isDryingStartStateAllowed(printer);
    const developerModeConfirmed = printer.state?.developer_lan_mode === true;
    const commonUnavailable = !printer.online || hasPartialData(printer) || hasConnectionError(printer) || !printer.writable;
    const startPending = isPending(printerId, "start_drying");
    const stopPending = isPending(printerId, "stop_drying");
    startButton.disabled = !startingAllowed || commonUnavailable || !startStateAllowed || !developerModeConfirmed || startPending;
    stopButton.disabled = !stoppingAllowed || commonUnavailable || stopPending;
    startButton.classList.toggle("is-pending", startPending);
    stopButton.classList.toggle("is-pending", stopPending);

    let note = "";
    if (!printer.online) note = "Der Drucker ist offline. Die Trocknung kann derzeit nicht gesteuert werden.";
    else if (hasPartialData(printer) || hasConnectionError(printer)) note = "Die Live-Daten sind unvollständig. Warte auf eine stabile Verbindung.";
    else if (!printer.writable) note = "Dieser Drucker ist nur zur Überwachung eingerichtet.";
    else if (drying.active && !stoppingAllowed) note = "Das Stoppen der Trocknung ist in den lokalen Rechten nicht freigegeben.";
    else if (!drying.active && !startingAllowed) note = "Das Starten der Trocknung ist in den lokalen Rechten nicht freigegeben.";
    else if (!drying.active && !developerModeConfirmed) note = "Developer-LAN-Modus nicht bestätigt/erforderlich. Aktiviere ihn am Drucker und warte auf neue Live-Daten.";
    else if (!drying.active && !startStateAllowed) note = "Die Trocknung kann nur gestartet werden, wenn der Drucker untätig (Idle) ist.";
    availabilityNote.textContent = note;
    availabilityNote.hidden = !note;
    experimentalNote.hidden = !experimental;

    if (startButton.disabled) startButton.title = note || "Trocknung momentan nicht verfügbar";
    else startButton.removeAttribute("title");
    if (stopButton.disabled) stopButton.title = note || "Stoppen momentan nicht verfügbar";
    else stopButton.removeAttribute("title");
  }

  function slotIsActive(slot, activeTray) {
    if (slot?.active === true) return true;
    if (activeTray === null || activeTray === undefined || activeTray === "") return false;
    return slot?.global_id !== null && slot?.global_id !== undefined && String(slot.global_id) === String(activeTray);
  }

  function renderSlot(slot, slotIndex, activeTray) {
    const slotCard = dom.slotTemplate.content.firstElementChild.cloneNode(true);
    const slotId = stringOr(slot?.id, String(slotIndex));
    const active = slotIsActive(slot, activeTray);
    const empty = Boolean(slot?.empty) || !slot?.material;
    const remainingValue = numberOrNull(slot?.remaining_percent);
    const remaining = remainingValue === null ? null : clamp(remainingValue, 0, 100);
    const color = normalizeColor(slot?.color);

    slotCard.classList.toggle("is-active", active);
    slotCard.classList.toggle("is-empty", empty);
    slotCard.style.setProperty("--slot-color", color);
    slotCard.querySelector(".slot-color").style.backgroundColor = color;

    const localNumber = /^\d+$/.test(slotId) ? Number(slotId) + 1 : slotId;
    setText(slotCard, ".slot-number", `Slot ${localNumber}`);
    const activeBadge = slotCard.querySelector(".slot-active-badge");
    activeBadge.hidden = !active;
    setText(slotCard, ".slot-material", empty ? "Leer" : stringOr(slot?.material, "Unbekannt"));
    setText(slotCard, ".slot-brand", empty ? "Keine Spule erkannt" : stringOr(slot?.sub_brand, "Materialprofil unbekannt"));

    const remainingTrack = slotCard.querySelector(".remaining-track");
    remainingTrack.setAttribute("aria-valuenow", String(Math.round(remaining ?? 0)));
    remainingTrack.setAttribute("aria-valuetext", remaining === null ? "Restmenge unbekannt" : `${Math.round(remaining)} Prozent verbleibend`);
    slotCard.querySelector(".remaining-fill").style.width = `${remaining ?? 0}%`;
    setText(slotCard, ".slot-remaining", remaining === null ? "Rest –" : `${Math.round(remaining)} % übrig`);
    renderRfidState(slotCard, slot?.rfid_state);

    return slotCard;
  }

  function renderRfidState(slotCard, value) {
    const element = slotCard.querySelector(".slot-rfid");
    const state = stringOr(value, "unknown").toLowerCase();
    const labels = {
      error: "RFID Fehler",
      failed: "RFID Fehler",
      idle: "RFID bereit",
      ok: "RFID OK",
      ready: "RFID bereit",
      reading: "RFID liest…",
      success: "RFID OK",
      unknown: "RFID –",
    };
    element.textContent = labels[state] || `RFID ${humanize(state)}`;
    element.classList.toggle("is-reading", state === "reading");
    element.classList.toggle("is-ok", ["ok", "success"].includes(state));
  }

  async function fetchSnapshot({ silent = false } = {}) {
    try {
      const response = await authenticatedFetch("/api/printers", {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      if (!setSnapshot(payload)) throw new Error("Die API-Antwort enthält keine Druckerliste.");
    } catch (error) {
      if (error instanceof SessionExpiredError) return;
      app.apiHealthy = false;
      if (!silent || !app.loaded) {
        showGlobalAlert("Druckerdaten nicht erreichbar", readableError(error));
      }
      updateConnectionDisplay();
    }
  }

  async function checkHealth() {
    try {
      const response = await authenticatedFetch("/health", {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      app.apiHealthy = response.ok;
    } catch (error) {
      if (error instanceof SessionExpiredError) return;
      app.apiHealthy = false;
    }
    updateConnectionDisplay();
  }

  function websocketUrl() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${window.location.host}/ws`;
  }

  function connectWebsocket() {
    if (app.stopped || !navigator.onLine) return;
    if (app.websocket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(app.websocket.readyState)) return;
    window.clearTimeout(app.reconnectTimer);

    try {
      const socket = new WebSocket(websocketUrl());
      app.websocket = socket;
      updateConnectionDisplay();

      socket.addEventListener("open", () => {
        app.websocketOpen = true;
        app.reconnectAttempts = 0;
        app.apiHealthy = true;
        hideGlobalAlert();
        updateConnectionDisplay();
      });

      socket.addEventListener("message", (event) => {
        try {
          applySocketMessage(JSON.parse(event.data));
        } catch (error) {
          console.warn("Ungültige WebSocket-Nachricht", error);
        }
      });

      socket.addEventListener("close", (event) => {
        app.websocketOpen = false;
        if (app.websocket === socket) app.websocket = null;
        if (event.code === 4401) {
          redirectToLogin();
          return;
        }
        updateConnectionDisplay();
        scheduleReconnect();
      });

      socket.addEventListener("error", () => {
        socket.close();
      });
    } catch (_error) {
      app.websocketOpen = false;
      scheduleReconnect();
    }
  }

  function scheduleReconnect() {
    if (app.stopped || !navigator.onLine) return;
    window.clearTimeout(app.reconnectTimer);
    const delay = Math.min(30000, 1000 * (2 ** Math.min(app.reconnectAttempts, 5))) + Math.round(Math.random() * 500);
    app.reconnectAttempts += 1;
    app.reconnectTimer = window.setTimeout(connectWebsocket, delay);
  }

  function commandParams(control) {
    const command = control.dataset.command;
    if (["camera_recording", "camera_timelapse"].includes(command)) return { on: control.dataset.nextState === "true" };
    if (command === "camera_resolution") return { resolution: control.value };
    if (command === "speed") return { level: control.value };
    if (command === "light") return { on: control.dataset.nextState === "on" };
    if (command === "refresh_rfid") return { ams_id: control.dataset.amsId };
    return {};
  }

  async function sendCommand(printerId, command, params = {}) {
    const key = commandKey(printerId, command);
    if (app.pendingCommands.has(key)) return;
    const csrfToken = getCookie("bambu_csrf");
    if (!csrfToken) {
      showToast("Sicherheitstoken fehlt. Bitte lade die Seite neu.", "error");
      return;
    }

    app.pendingCommands.add(key);
    render();
    try {
      const response = await authenticatedFetch(`/api/printers/${encodeURIComponent(printerId)}/commands`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ command, params }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const result = await response.json();
      if (["timeout", "unconfirmed"].includes(result?.status)) {
        const subject = ["start_drying", "stop_drying"].includes(command) ? "Das AMS" : "Der Drucker";
        showToast(`${COMMAND_LABELS[command] || "Befehl"} wurde gesendet, aber ${subject} hat die Ausführung nicht bestätigt. Prüfe den Live-Status, bevor du es erneut versuchst.`, "error");
        window.setTimeout(() => fetchSnapshot({ silent: true }), 700);
        return;
      }
      if (result?.ok === false) throw new Error(result.detail || result.message || "Der Drucker hat den Befehl abgelehnt.");
      const sequence = result?.sequence_id ? ` · Vorgang ${result.sequence_id}` : "";
      const stateConfirmed = result?.status === "confirmed" && result?.acknowledged === false
        ? " · durch Live-Status bestätigt"
        : "";
      showToast(`${COMMAND_LABELS[command] || "Befehl"} gesendet${sequence}${stateConfirmed}.`);
      window.setTimeout(() => fetchSnapshot({ silent: true }), 700);
    } catch (error) {
      if (error instanceof SessionExpiredError) return;
      showToast(`${COMMAND_LABELS[command] || "Befehl"} fehlgeschlagen: ${readableError(error)}`, "error");
    } finally {
      app.pendingCommands.delete(key);
      render();
    }
  }

  async function responseError(response) {
    try {
      const data = await response.json();
      const errorCode = stringOr(data?.code ?? data?.error_code ?? data?.detail?.code).toLowerCase();
      const detailMessage = typeof data?.detail === "object" && data?.detail
        ? data.detail.message || data.detail.msg
        : data?.detail;
      const message = typeof detailMessage === "string" ? detailMessage : data?.message;
      const combined = `${errorCode} ${stringOr(message)}`.toLowerCase();
      if (combined.includes("unconfirmed") || combined.includes("not confirmed")) {
        return "Das AMS hat die Ausführung nicht bestätigt. Prüfe den Live-Status, bevor du den Befehl erneut sendest.";
      }
      if (combined.includes("experimental") && (combined.includes("ack") || combined.includes("confirm"))) {
        return "Die ausdrückliche Bestätigung für die experimentelle X1-Funktion fehlt.";
      }
      if (typeof detailMessage === "string") return detailMessage;
      if (Array.isArray(data?.detail)) return data.detail.map((item) => item.msg || String(item)).join(", ");
      if (typeof data?.message === "string") return data.message;
    } catch (_error) {
      // The status text below is more useful than a JSON parsing error.
    }
    if (response.status === 401) return "Anmeldung erforderlich.";
    if (response.status === 403) return "Aktion nicht erlaubt oder Sicherheitstoken ungültig.";
    if (response.status === 404) return "Drucker oder Funktion nicht gefunden.";
    if (response.status === 409) return "Der Befehl passt nicht zum aktuellen Druckzustand.";
    if (response.status === 422) return "Temperatur, Dauer oder AMS-Auswahl ist ungültig.";
    if (response.status === 429) return "Zu viele Befehle. Bitte kurz warten.";
    if (response.status === 504) return "Das AMS hat den Befehl nicht rechtzeitig bestätigt. Prüfe den Live-Status.";
    return `${response.status} ${response.statusText || "Serverfehler"}`;
  }

  function readableError(error) {
    if (error instanceof Error && error.message) return error.message;
    return "Unbekannter Verbindungsfehler.";
  }

  function findPrinter(printerId) {
    return app.printers.find((printer) => String(printer.id) === String(printerId));
  }

  function findAmsUnit(printer, amsId) {
    return getAmsUnits(printer).find((unit) => String(unit.id) === String(amsId));
  }

  function commandAmsId(value) {
    const numeric = numberOrNull(value);
    return numeric !== null && Number.isInteger(numeric) ? numeric : value;
  }

  function showDryingFormError(message) {
    dom.dryingFormError.textContent = message;
    dom.dryingFormError.hidden = !message;
  }

  function updateDryingTemperatureOutput() {
    dom.dryingTemperatureOutput.textContent = `${dom.dryingTemperature.value} °C`;
  }

  function requestDryingStart(printerId, amsId) {
    const printer = findPrinter(printerId);
    const unit = printer && findAmsUnit(printer, amsId);
    if (!printer || !unit || !isDryCapable(printer, unit)) return;
    if (!supports(printer, "start_drying") || !printer.writable) {
      showToast("Das Starten der Trocknung ist für diesen Drucker nicht freigegeben.", "error");
      return;
    }
    if (printer.state?.developer_lan_mode !== true) {
      showToast("Developer-LAN-Modus nicht bestätigt/erforderlich. Aktiviere ihn zuerst am Drucker.", "error");
      return;
    }
    if (!isDryingStartStateAllowed(printer)) {
      showToast("Die Trocknung kann nur gestartet werden, wenn der Drucker untätig (Idle) ist.", "error");
      return;
    }

    const model = dryingModelInfo(unit);
    const experimental = isExperimentalDrying(printer, unit);
    const previous = dryingState(unit);
    const suggested = model.kind === "amsht" ? 65 : 55;
    const initialTemperature = clamp(previous.temperature ?? suggested, model.minimum, model.maximum);
    const initialDuration = clamp(previous.durationHours ?? 6, 1, 24);
    app.dryingTarget = { printerId: String(printer.id), amsId: String(unit.id) };

    dom.dryingDialogSubtitle.textContent = `${model.label} an ${stringOr(printer.name, printer.id)} · ${model.minimum}–${model.maximum} °C`;
    dom.dryingTemperature.min = String(model.minimum);
    dom.dryingTemperature.max = String(model.maximum);
    dom.dryingTemperature.value = String(Math.round(initialTemperature));
    dom.dryingTemperatureMin.textContent = `${model.minimum} °C`;
    dom.dryingTemperatureMax.textContent = `${model.maximum} °C`;
    dom.dryingDuration.value = String(Math.round(initialDuration));
    dom.dryingFilament.value = "";
    dom.dryingRotate.checked = false;
    dom.dryingExperimentalAck.checked = false;
    dom.dryingExperimentalField.hidden = !experimental;
    showDryingFormError("");
    updateDryingTemperatureOutput();

    if (typeof dom.dryingDialog.showModal === "function") dom.dryingDialog.showModal();
    else showToast("Dieser Browser unterstützt den sicheren Trocknungsdialog nicht.", "error");
  }

  function requestDryingStop(printerId, amsId) {
    const printer = findPrinter(printerId);
    const unit = printer && findAmsUnit(printer, amsId);
    if (!printer || !unit || !isDryCapable(printer, unit)) return;
    if (!supports(printer, "stop_drying") || !printer.writable) {
      showToast("Das Stoppen der Trocknung ist für diesen Drucker nicht freigegeben.", "error");
      return;
    }
    if (!printer.online || hasPartialData(printer) || hasConnectionError(printer)) {
      showToast("Die Verbindung ist nicht stabil genug, um den Sicherheits-Stopp zu senden.", "error");
      return;
    }
    const model = dryingModelInfo(unit);
    const active = dryingState(unit).active;
    const targetName = `${model.label} an ${stringOr(printer.name, printer.id)}`;
    app.stopDryingTarget = { printerId: String(printer.id), amsId: String(unit.id) };
    dom.stopDryingDialogTitle.textContent = active ? "Trocknung beenden?" : "Sicherheits-Stopp senden?";
    dom.confirmStopDryingButton.textContent = active ? "Trocknung stoppen" : "Sicherheits-Stopp senden";
    dom.stopDryingDialogMessage.textContent = active
      ? `Die gemeldete Trocknung in ${targetName} wird sofort gestoppt.`
      : `Der Sicherheits-Stopp wird an ${targetName} gesendet, auch wenn der Live-Status derzeit keine aktive Trocknung meldet. Das ist nach einem unbestätigten Start sinnvoll.`;
    if (typeof dom.stopDryingDialog.showModal === "function") {
      dom.stopDryingDialog.showModal();
    } else if (window.confirm(dom.stopDryingDialogMessage.textContent)) {
      sendCommand(String(printer.id), "stop_drying", { ams_id: commandAmsId(unit.id) });
      app.stopDryingTarget = null;
    }
  }

  function requestStop(printerId) {
    const printer = findPrinter(printerId);
    if (!printer) return;
    app.stopTargetId = String(printerId);
    dom.stopPrinterName.textContent = stringOr(printer.name, `Drucker ${printerId}`);
    if (typeof dom.stopDialog.showModal === "function") {
      dom.stopDialog.showModal();
    } else if (window.confirm(`Druck auf ${dom.stopPrinterName.textContent} wirklich abbrechen?`)) {
      sendCommand(app.stopTargetId, "stop", {});
      app.stopTargetId = null;
    }
  }

  dom.printerGrid.addEventListener("click", (event) => {
    const cameraControl = event.target.closest("button[data-camera-action]");
    if (cameraControl) {
      if (cameraControl.disabled) return;
      const { cameraAction, printerId } = cameraControl.dataset;
      if (cameraAction === "start") startCameraSession(printerId);
      if (cameraAction === "stop") stopCameraSession(printerId);
      return;
    }
    const dryingControl = event.target.closest("button[data-dry-action]");
    if (dryingControl) {
      if (dryingControl.disabled) return;
      const { dryAction, printerId, amsId } = dryingControl.dataset;
      if (dryAction === "start") requestDryingStart(printerId, amsId);
      if (dryAction === "stop") requestDryingStop(printerId, amsId);
      return;
    }
    const control = event.target.closest("button[data-command]");
    if (!control || control.disabled) return;
    const { command, printerId } = control.dataset;
    if (!command || !printerId) return;
    if (command === "stop") {
      requestStop(printerId);
      return;
    }
    const params = commandParams(control);
    if (params !== null) sendCommand(printerId, command, params);
  });

  dom.printerGrid.addEventListener("change", (event) => {
    const control = event.target.closest("select[data-command]");
    if (!control || control.disabled) return;
    const { command, printerId } = control.dataset;
    if (command && printerId) sendCommand(printerId, command, commandParams(control));
  });

  dom.printerGrid.addEventListener("load", (event) => {
    const image = event.target;
    if (!(image instanceof HTMLImageElement) || !image.classList.contains("camera-image")) return;
    const panel = image.closest(".camera-panel");
    const printerId = panel?.dataset.printerId;
    const printer = printerId && findPrinter(printerId);
    if (!panel || !printer) return;
    const session = cameraSession(printerId);
    session.phase = "live";
    session.error = "";
    renderCameraPanel(panel, printer);
  }, true);

  dom.printerGrid.addEventListener("error", (event) => {
    const image = event.target;
    if (!(image instanceof HTMLImageElement) || !image.classList.contains("camera-image")) return;
    const panel = image.closest(".camera-panel");
    const printerId = panel?.dataset.printerId;
    const printer = printerId && findPrinter(printerId);
    if (!panel || !printer) return;
    const session = cameraSession(printerId);
    session.phase = "error";
    session.streamUrl = "";
    session.error = "Der lokale Kamerastream konnte nicht geladen werden. Prüfe Kameraeinstellung und Verbindung.";
    clearCameraImage(panel);
    renderCameraPanel(panel, printer);
  }, true);

  dom.stopDialog.addEventListener("close", () => {
    if (dom.stopDialog.returnValue === "confirm" && app.stopTargetId) {
      sendCommand(app.stopTargetId, "stop", {});
    }
    app.stopTargetId = null;
  });

  dom.dryingTemperature.addEventListener("input", updateDryingTemperatureOutput);

  dom.dryingDialogClose.addEventListener("click", () => dom.dryingDialog.close());
  document.querySelector("#cancel-drying-button").addEventListener("click", () => dom.dryingDialog.close());

  dom.dryingDialog.addEventListener("close", () => {
    app.dryingTarget = null;
    showDryingFormError("");
  });

  dom.dryingForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const target = app.dryingTarget;
    const printer = target && findPrinter(target.printerId);
    const unit = printer && findAmsUnit(printer, target.amsId);
    if (!target || !printer || !unit || !isDryCapable(printer, unit)) {
      showDryingFormError("Das AMS ist nicht mehr verfügbar. Schließe den Dialog und aktualisiere die Seite.");
      return;
    }
    if (!supports(printer, "start_drying") || !printer.writable) {
      showDryingFormError("Die Berechtigung zum Starten wurde zwischenzeitlich entzogen.");
      return;
    }
    if (printer.state?.developer_lan_mode !== true) {
      showDryingFormError("Developer-LAN-Modus nicht bestätigt/erforderlich. Aktiviere ihn am Drucker und warte auf neue Live-Daten.");
      return;
    }
    if (!printer.online || hasPartialData(printer) || hasConnectionError(printer)) {
      showDryingFormError("Die Verbindung ist nicht stabil genug, um die Trocknung sicher zu starten.");
      return;
    }
    if (!isDryingStartStateAllowed(printer)) {
      showDryingFormError("Die Trocknung kann nur gestartet werden, wenn der Drucker untätig (Idle) ist.");
      return;
    }

    const model = dryingModelInfo(unit);
    const temperature = numberOrNull(dom.dryingTemperature.value);
    const duration = numberOrNull(dom.dryingDuration.value);
    if (temperature === null || !Number.isInteger(temperature) || temperature < model.minimum || temperature > model.maximum) {
      showDryingFormError(`Wähle eine Temperatur zwischen ${model.minimum} und ${model.maximum} °C.`);
      return;
    }
    if (duration === null || !Number.isInteger(duration) || duration < 1 || duration > 24) {
      showDryingFormError("Wähle eine ganze Dauer zwischen 1 und 24 Stunden.");
      return;
    }
    const experimental = isExperimentalDrying(printer, unit);
    if (experimental && !dom.dryingExperimentalAck.checked) {
      showDryingFormError("Bestätige ausdrücklich, dass du die experimentelle X1-Funktion verstanden hast.");
      dom.dryingExperimentalAck.focus();
      return;
    }

    const params = {
      ams_id: commandAmsId(unit.id),
      temp: temperature,
      duration,
      rotate_tray: dom.dryingRotate.checked,
    };
    if (dom.dryingFilament.value) params.filament = dom.dryingFilament.value;
    if (experimental) params.experimental_ack = true;
    dom.dryingDialog.close();
    sendCommand(String(printer.id), "start_drying", params);
  });

  dom.stopDryingDialog.addEventListener("close", () => {
    const target = app.stopDryingTarget;
    if (dom.stopDryingDialog.returnValue === "confirm" && target) {
      const printer = findPrinter(target.printerId);
      const unit = printer && findAmsUnit(printer, target.amsId);
      const available = printer
        && unit
        && isDryCapable(printer, unit)
        && supports(printer, "stop_drying")
        && printer.writable
        && printer.online
        && !hasPartialData(printer)
        && !hasConnectionError(printer);
      if (available) {
        sendCommand(target.printerId, "stop_drying", { ams_id: commandAmsId(target.amsId) });
      } else {
        showToast("Der Sicherheits-Stopp kann wegen einer geänderten Verbindung oder Berechtigung nicht gesendet werden.", "error");
      }
    }
    app.stopDryingTarget = null;
  });

  dom.retryButton.addEventListener("click", () => {
    hideGlobalAlert();
    fetchSnapshot();
    checkHealth();
    connectWebsocket();
  });

  window.addEventListener("online", () => {
    app.reconnectAttempts = 0;
    fetchSnapshot({ silent: true });
    connectWebsocket();
    checkHealth();
  });

  window.addEventListener("offline", updateConnectionDisplay);

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      fetchSnapshot({ silent: true });
      connectWebsocket();
    }
  });

  window.addEventListener("beforeunload", () => {
    app.stopped = true;
    window.clearTimeout(app.reconnectTimer);
    app.websocket?.close();
  });

  fetchSnapshot();
  checkHealth();
  connectWebsocket();
  window.setInterval(() => fetchSnapshot({ silent: true }), 60000);
  window.setInterval(checkHealth, 30000);
  window.setInterval(updateConnectionDisplay, 10000);
})();
