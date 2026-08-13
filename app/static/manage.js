(() => {
  "use strict";

  const COMMANDS = [
    ["pause", "Druck pausieren"],
    ["resume", "Druck fortsetzen"],
    ["stop", "Druck abbrechen"],
    ["speed", "Geschwindigkeit"],
    ["light", "Licht"],
    ["refresh_rfid", "AMS-RFID lesen"],
    ["start_drying", "AMS-Trocknung starten"],
    ["stop_drying", "AMS-Trocknung stoppen"],
    ["camera_recording", "Kameraaufnahme"],
    ["camera_timelapse", "Zeitraffer"],
    ["camera_resolution", "Kameraauflösung"],
  ];
  const COMMAND_LABELS = new Map(COMMANDS);
  const RESULT_LABELS = new Map([
    ["acknowledged", "Bestätigt"],
    ["applied", "Übernommen"],
    ["confirmed", "Bestätigt"],
    ["failed", "Fehler"],
    ["rejected", "Abgelehnt"],
    ["timeout", "Zeitüberschreitung"],
    ["unconfirmed", "Unbestätigt"],
  ]);
  const VALID_TABS = new Set(["configuration", "history", "diagnostics"]);

  const dom = {
    addPrinter: document.querySelector("#add-printer-button"),
    alert: document.querySelector("#page-alert"),
    alertMessage: document.querySelector("#page-alert-message"),
    alertTitle: document.querySelector("#page-alert-title"),
    allowedOrigins: document.querySelector("#allowed-origins"),
    cancelSave: document.querySelector("#cancel-save"),
    configForm: document.querySelector("#config-form"),
    configLoading: document.querySelector("#config-loading"),
    confirmSave: document.querySelector("#confirm-save"),
    currentPassword: document.querySelector("#current-password"),
    diagnosticPrinters: document.querySelector("#diagnostic-printers"),
    diagnosticsContent: document.querySelector("#diagnostics-content"),
    diagnosticsLoading: document.querySelector("#diagnostics-loading"),
    filterCommand: document.querySelector("#filter-command"),
    filterFrom: document.querySelector("#filter-from"),
    filterPrinter: document.querySelector("#filter-printer"),
    filterResult: document.querySelector("#filter-result"),
    filterTo: document.querySelector("#filter-to"),
    historyEmpty: document.querySelector("#history-empty"),
    historyFilters: document.querySelector("#history-filters"),
    historyLoading: document.querySelector("#history-loading"),
    historyNext: document.querySelector("#history-next"),
    historyPage: document.querySelector("#history-page"),
    historyPrevious: document.querySelector("#history-previous"),
    historyRows: document.querySelector("#history-rows"),
    historyTableWrap: document.querySelector("#history-table-wrap"),
    logout: document.querySelector("#logout-button"),
    pageStatus: document.querySelector("#page-status"),
    printerList: document.querySelector("#printer-list"),
    printerTemplate: document.querySelector("#printer-template"),
    refreshDiagnostics: document.querySelector("#refresh-diagnostics"),
    refreshHistory: document.querySelector("#refresh-history"),
    saveButton: document.querySelector("#save-button"),
    saveDialog: document.querySelector("#save-dialog"),
    saveDialogForm: document.querySelector("#save-dialog-form"),
    systemFacts: document.querySelector("#system-facts"),
    webPassword: document.querySelector("#web-password"),
    webUsername: document.querySelector("#web-username"),
  };

  const state = {
    config: null,
    configLoaded: false,
    diagnosticsLoaded: false,
    historyLoaded: false,
    historyCursor: null,
    historyNextCursor: null,
    historyPrevious: [],
    historyPage: 1,
    printerSequence: 0,
    redirecting: false,
    saving: false,
  };

  class SessionExpiredError extends Error {}

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function valueText(value, fallback = "–") {
    if (value === null || value === undefined || value === "") return fallback;
    if (value === true) return "Ja";
    if (value === false) return "Nein";
    return String(value);
  }

  function getCookie(name) {
    const prefix = `${encodeURIComponent(name)}=`;
    const match = document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith(prefix));
    if (!match) return "";
    try {
      return decodeURIComponent(match.slice(prefix.length));
    } catch (_error) {
      return "";
    }
  }

  function redirectToLogin() {
    if (state.redirecting) return;
    state.redirecting = true;
    window.location.replace("/login?expired=1");
  }

  async function apiFetch(url, options = {}) {
    const response = await fetch(url, { ...options, credentials: "same-origin" });
    if (response.status === 401 || response.status === 4401) {
      redirectToLogin();
      throw new SessionExpiredError();
    }
    return response;
  }

  async function responseError(response) {
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") return body.detail;
    } catch (_error) {
      // Deliberately fall back to a generic, non-sensitive message.
    }
    return `Anfrage fehlgeschlagen (${response.status})`;
  }

  function showAlert(title, message, success = false) {
    dom.alertTitle.textContent = title;
    dom.alertMessage.textContent = message;
    dom.alert.classList.toggle("is-success", success);
    dom.alert.hidden = false;
    dom.alert.focus({ preventScroll: true });
    dom.alert.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function hideAlert() {
    dom.alert.hidden = true;
    dom.alert.classList.remove("is-success");
  }

  function announce(message) {
    dom.pageStatus.textContent = "";
    window.setTimeout(() => { dom.pageStatus.textContent = message; }, 20);
  }

  function activateTab() {
    const candidate = window.location.hash.slice(1);
    const tab = VALID_TABS.has(candidate) ? candidate : "configuration";
    if (candidate !== tab) window.history.replaceState(null, "", `#${tab}`);
    document.querySelectorAll("[data-tab]").forEach((link) => {
      const active = link.dataset.tab === tab;
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.panel !== tab;
    });
    hideAlert();
    if (tab === "history" && !state.historyLoaded) loadHistory(true);
    if (tab === "diagnostics" && !state.diagnosticsLoaded) loadDiagnostics();
  }

  function printerCards() {
    return [...dom.printerList.querySelectorAll(".printer-card")];
  }

  function field(card, name) {
    return card.querySelector(`[data-field="${name}"]`);
  }

  function updatePrinterNumbers() {
    const cards = printerCards();
    dom.addPrinter.disabled = cards.length >= 16;
    dom.addPrinter.title = cards.length >= 16 ? "Maximal 16 Drucker" : "";
    cards.forEach((card, index) => {
      card.querySelector(".printer-number").textContent = String(index + 1);
      card.querySelector(".remove-printer").disabled = cards.length === 1;
      const name = field(card, "name").value.trim();
      card.querySelector(".printer-card-title").textContent = name || "Neuer Drucker";
    });
  }

  function buildPermissionOptions(card, allowed = []) {
    const container = card.querySelector(".permission-grid");
    const fragment = document.createDocumentFragment();
    COMMANDS.forEach(([command, label]) => {
      const option = element("label", "permission-option");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.dataset.command = command;
      checkbox.checked = allowed.includes(command);
      option.append(checkbox, document.createTextNode(label));
      fragment.append(option);
    });
    container.replaceChildren(fragment);
  }

  function syncDryingInvariant(card, changedCommand = "") {
    const writable = field(card, "writable").checked;
    const start = card.querySelector('[data-command="start_drying"]');
    const stop = card.querySelector('[data-command="stop_drying"]');
    if (changedCommand === "stop_drying" && start.checked && !stop.checked) stop.checked = true;
    if (start.checked) stop.checked = true;
    card.querySelectorAll("[data-command]").forEach((checkbox) => { checkbox.disabled = !writable; });
    stop.disabled = !writable || start.checked;
    card.querySelector(".permissions").classList.toggle("is-disabled", !writable);
  }

  function configurePrinterCard(card, printer = null) {
    state.printerSequence += 1;
    const sequence = state.printerSequence;
    const defaults = {
      id: `drucker-${sequence}`,
      name: "",
      model: "unknown",
      host: "",
      port: 8883,
      serial: "",
      writable: false,
      camera_enabled: false,
      allowed_commands: ["pause", "resume", "stop", "speed", "light", "refresh_rfid"],
      allow_self_signed_tls: false,
      tls_ca_file: null,
      tls_fingerprint_sha256: null,
      stale_after_seconds: 90,
      full_refresh_seconds: 300,
      access_code_configured: false,
    };
    const value = { ...defaults, ...(printer || {}) };
    card.dataset.initialId = printer?.id || "";
    card.dataset.initialSerial = printer?.serial || "";
    card.dataset.secretConfigured = String(Boolean(value.access_code_configured));
    ["id", "name", "model", "host", "port", "serial", "tls_ca_file", "tls_fingerprint_sha256", "stale_after_seconds", "full_refresh_seconds"].forEach((name) => {
      field(card, name).value = value[name] ?? "";
    });
    ["writable", "camera_enabled", "allow_self_signed_tls"].forEach((name) => {
      field(card, name).checked = Boolean(value[name]);
    });
    const accessCode = field(card, "access_code");
    const secretState = card.querySelector(".printer-secret-state");
    const accessHelp = card.querySelector(".access-help");
    if (value.access_code_configured) {
      secretState.textContent = "LAN-Code ist hinterlegt";
      accessHelp.textContent = "Leer lassen, um den bestehenden Code zu behalten. Bei geänderter Kennung oder Seriennummer ist ein neuer Code nötig.";
      accessCode.required = false;
    } else {
      secretState.textContent = "Neuer Drucker – LAN-Code erforderlich";
      accessHelp.textContent = "Für einen neuen Drucker ist der LAN-Zugangscode erforderlich.";
      accessCode.required = true;
    }
    buildPermissionOptions(card, value.allowed_commands || []);
    syncDryingInvariant(card);
    card.querySelectorAll("input").forEach((input) => {
      input.id = `printer-${sequence}-${input.dataset.field || input.dataset.command || "option"}`;
    });
  }

  function addPrinter(printer = null, focus = false) {
    if (printerCards().length >= 16) return;
    const card = dom.printerTemplate.content.firstElementChild.cloneNode(true);
    configurePrinterCard(card, printer);
    dom.printerList.append(card);
    updatePrinterNumbers();
    if (focus) {
      field(card, "name").focus();
      card.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  function renderConfig(config) {
    state.config = config;
    dom.webUsername.value = config.web?.username || "";
    dom.webPassword.value = "";
    dom.allowedOrigins.value = Array.isArray(config.web?.allowed_origins) ? config.web.allowed_origins.join("\n") : "";
    dom.printerList.replaceChildren();
    state.printerSequence = 0;
    (Array.isArray(config.printers) ? config.printers : []).forEach((printer) => addPrinter(printer));
    if (!printerCards().length) addPrinter();
    dom.configLoading.hidden = true;
    dom.configForm.hidden = false;
  }

  async function loadConfig() {
    try {
      const response = await apiFetch("/api/admin/config", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(await responseError(response));
      const config = await response.json();
      renderConfig(config);
      state.configLoaded = true;
    } catch (error) {
      if (error instanceof SessionExpiredError) return;
      dom.configLoading.hidden = true;
      showAlert("Konfiguration nicht verfügbar", error.message || "Die Einstellungen konnten nicht geladen werden.");
    }
  }

  function parseOrigins() {
    const origins = dom.allowedOrigins.value.split(/\r?\n/).map((value) => value.trim().replace(/\/$/, "")).filter(Boolean);
    if (!origins.length || new Set(origins).size !== origins.length) throw new Error("Trage mindestens eine eindeutige HTTPS-Adresse ein.");
    if (origins.length > 16) throw new Error("Es können höchstens 16 HTTPS-Adressen freigegeben werden.");
    for (const origin of origins) {
      if (origin.length > 512) throw new Error("Eine freigegebene HTTPS-Adresse ist zu lang.");
      let parsed;
      try { parsed = new URL(origin); } catch (_error) { throw new Error(`Ungültige Adresse: ${origin}`); }
      if (parsed.protocol !== "https:" || parsed.origin !== origin) throw new Error(`Nur vollständige HTTPS-Adressen sind erlaubt: ${origin}`);
    }
    if (!origins.includes(window.location.origin)) throw new Error(`Die aktuell geöffnete Adresse ${window.location.origin} muss freigegeben bleiben.`);
    return origins;
  }

  function numberValue(card, name) {
    return Number(field(card, name).value);
  }

  function collectPrinter(card) {
    const writable = field(card, "writable").checked;
    const fullRefresh = numberValue(card, "full_refresh_seconds");
    if (fullRefresh !== 0 && (fullRefresh < 300 || fullRefresh > 3600)) throw new Error("Die vollständige Abfrage muss 0 oder 300 bis 3600 Sekunden betragen.");
    const startDrying = card.querySelector('[data-command="start_drying"]');
    const stopDrying = card.querySelector('[data-command="stop_drying"]');
    if (startDrying.checked && !stopDrying.checked) throw new Error("Das Recht zum Starten einer Trocknung benötigt immer auch das Stopprecht.");
    const accessValue = field(card, "access_code").value;
    const fingerprint = field(card, "tls_fingerprint_sha256").value.trim().replaceAll(":", "").toLowerCase();
    if (field(card, "allow_self_signed_tls").checked && !fingerprint) throw new Error("Selbstsigniertes TLS benötigt den SHA-256-Fingerprint des Druckerzertifikats.");
    const printerId = field(card, "id").value.trim().toLowerCase();
    const serial = field(card, "serial").value.trim();
    if (!accessValue && card.dataset.secretConfigured === "true" && (printerId !== card.dataset.initialId || serial !== card.dataset.initialSerial)) {
      throw new Error(`Für ${field(card, "name").value.trim() || printerId} ist nach einer geänderten Kennung oder Seriennummer ein neuer LAN-Zugangscode nötig.`);
    }
    if (fingerprint && !/^[0-9a-f]{64}$/.test(fingerprint)) throw new Error("Ein TLS-Fingerprint muss aus genau 64 hexadezimalen Zeichen bestehen.");
    return {
      id: printerId,
      name: field(card, "name").value.trim(),
      model: field(card, "model").value.trim(),
      host: field(card, "host").value.trim(),
      port: numberValue(card, "port"),
      serial,
      writable,
      camera_enabled: field(card, "camera_enabled").checked,
      allowed_commands: writable ? [...card.querySelectorAll("[data-command]:checked")].map((input) => input.dataset.command).sort() : [],
      allow_self_signed_tls: field(card, "allow_self_signed_tls").checked,
      tls_ca_file: field(card, "allow_self_signed_tls").checked
        ? null
        : field(card, "tls_ca_file").value || null,
      tls_fingerprint_sha256: field(card, "allow_self_signed_tls").checked
        ? fingerprint || null
        : null,
      stale_after_seconds: numberValue(card, "stale_after_seconds"),
      full_refresh_seconds: fullRefresh,
      access_code: accessValue || null,
    };
  }

  function collectConfig(currentPassword) {
    if (!dom.configForm.reportValidity()) throw new Error("Prüfe bitte die markierten Pflichtfelder.");
    const printers = printerCards().map(collectPrinter);
    const ids = printers.map((printer) => printer.id);
    const serials = printers.map((printer) => printer.serial);
    if (new Set(ids).size !== ids.length) throw new Error("Jede Druckerkennung darf nur einmal vorkommen.");
    if (new Set(serials).size !== serials.length) throw new Error("Jede Seriennummer darf nur einmal vorkommen.");
    const newPassword = dom.webPassword.value;
    return {
      current_password: currentPassword,
      web: {
        username: dom.webUsername.value.trim(),
        allowed_origins: parseOrigins(),
        password: newPassword || null,
      },
      printers,
    };
  }

  function clearSecrets() {
    dom.currentPassword.value = "";
    dom.webPassword.value = "";
    printerCards().forEach((card) => { field(card, "access_code").value = ""; });
  }

  function clearCurrentPassword() {
    dom.currentPassword.value = "";
  }

  function openSaveDialog() {
    if (state.saving) return;
    hideAlert();
    try {
      if (!dom.configForm.reportValidity()) return;
      parseOrigins();
      printerCards().forEach(collectPrinter);
    } catch (error) {
      showAlert("Eingaben prüfen", error.message);
      return;
    }
    dom.currentPassword.value = "";
    dom.saveDialog.showModal();
    dom.currentPassword.focus();
  }

  async function saveConfig(event) {
    event.preventDefault();
    if (state.saving || !dom.currentPassword.reportValidity()) return;
    let payload;
    try {
      payload = collectConfig(dom.currentPassword.value);
    } catch (error) {
      clearSecrets();
      dom.saveDialog.close();
      showAlert("Eingaben prüfen", error.message);
      return;
    }
    // Geheimnisse bleiben nur für den unmittelbaren Request im Speicher und
    // verschwinden noch bevor wir auf die Serverantwort warten.
    clearSecrets();
    dom.saveDialog.close();
    state.saving = true;
    dom.saveButton.disabled = true;
    dom.confirmSave.disabled = true;
    announce("Konfiguration wird übernommen.");
    try {
      const csrf = getCookie("bambu_csrf");
      if (!csrf) throw new Error("Sicherheitstoken fehlt. Lade die Seite neu und versuche es erneut.");
      const response = await apiFetch("/api/admin/config", {
        method: "PUT",
        headers: { Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const config = await response.json();
      renderConfig(config);
      showAlert("Änderungen übernommen", `${config.printers?.length || 0} Drucker werden mit der neuen Konfiguration verbunden.`, true);
      announce("Konfiguration erfolgreich übernommen.");
    } catch (error) {
      if (error instanceof SessionExpiredError) return;
      showAlert("Speichern nicht möglich", error.message || "Die bisherige Konfiguration bleibt aktiv.");
      announce("Konfiguration konnte nicht gespeichert werden.");
    } finally {
      payload = null;
      state.saving = false;
      dom.saveButton.disabled = false;
      dom.confirmSave.disabled = false;
    }
  }

  function dateQuery(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toISOString();
  }

  function resultLabel(value) {
    return RESULT_LABELS.get(value) || valueText(value);
  }

  function renderHistory(body) {
    const items = Array.isArray(body.items) ? body.items : [];
    const fragment = document.createDocumentFragment();
    items.forEach((item) => {
      const row = document.createElement("tr");
      const timestamp = new Date(item.created_at);
      const result = element("span", "result-pill", resultLabel(item.result));
      if (["acknowledged", "applied", "confirmed"].includes(item.result)) result.classList.add("is-good");
      if (["failed", "rejected", "timeout", "unconfirmed"].includes(item.result)) result.classList.add("is-bad");
      const values = [
        Number.isNaN(timestamp.getTime()) ? valueText(item.created_at) : timestamp.toLocaleString("de-DE"),
        valueText(item.printer_id),
        COMMAND_LABELS.get(item.command) || valueText(item.command),
      ];
      values.forEach((value) => row.append(element("td", "", value)));
      const resultCell = document.createElement("td");
      resultCell.append(result);
      row.append(resultCell, element("td", "", valueText(item.actor)));
      fragment.append(row);
    });
    dom.historyRows.replaceChildren(fragment);
    dom.historyEmpty.hidden = items.length !== 0;
    dom.historyTableWrap.hidden = items.length === 0;
    state.historyNextCursor = body.next_cursor || null;
    dom.historyNext.disabled = !state.historyNextCursor;
    dom.historyPrevious.disabled = state.historyPrevious.length === 0;
    dom.historyPage.textContent = `Seite ${state.historyPage}`;
  }

  async function loadHistory(reset = false) {
    if (reset) {
      state.historyCursor = null;
      state.historyNextCursor = null;
      state.historyPrevious = [];
      state.historyPage = 1;
    }
    dom.historyLoading.hidden = false;
    dom.historyEmpty.hidden = true;
    dom.historyTableWrap.hidden = true;
    const query = new URLSearchParams({ limit: "50" });
    if (state.historyCursor) query.set("cursor", state.historyCursor);
    const filters = [
      ["printer_id", dom.filterPrinter.value.trim()],
      ["command", dom.filterCommand.value.trim()],
      ["result", dom.filterResult.value],
      ["from", dateQuery(dom.filterFrom.value)],
      ["to", dateQuery(dom.filterTo.value)],
    ];
    filters.forEach(([key, value]) => { if (value) query.set(key, value); });
    try {
      const response = await apiFetch(`/api/admin/audit?${query.toString()}`, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(await responseError(response));
      renderHistory(await response.json());
      state.historyLoaded = true;
    } catch (error) {
      if (!(error instanceof SessionExpiredError)) showAlert("Verlauf nicht verfügbar", error.message);
    } finally {
      dom.historyLoading.hidden = true;
    }
  }

  function addFact(label, value) {
    const card = element("div", "fact");
    card.append(element("span", "", label), element("strong", "", valueText(value)));
    dom.systemFacts.append(card);
  }

  function addDefinition(list, label, value) {
    list.append(element("dt", "", label), element("dd", "", valueText(value)));
  }

  function diagnosticSection(title, facts) {
    const section = element("section", "diagnostic-section");
    section.append(element("h4", "", title));
    const list = element("dl", "diagnostic-list");
    facts.forEach(([label, value]) => addDefinition(list, label, value));
    section.append(list);
    return section;
  }

  function renderDiagnostics(data) {
    dom.systemFacts.replaceChildren();
    const hours = Math.floor(Number(data.uptime_seconds || 0) / 3600);
    addFact("Build", data.build_version);
    addFact("Laufzeit", `${hours} Std.`);
    addFact("Drucker", Array.isArray(data.printers) ? data.printers.length : 0);
    addFact("Audit-Aufbewahrung", `${valueText(data.audit_retention?.days)} Tage`);
    const cards = document.createDocumentFragment();
    (Array.isArray(data.printers) ? data.printers : []).forEach((printer) => {
      const card = element("article", "diagnostic-card");
      const header = document.createElement("header");
      const identity = document.createElement("div");
      identity.append(element("h3", "", valueText(printer.name, "Drucker")), element("p", "", `${valueText(printer.model)} · ${valueText(printer.id)}`));
      const status = element("span", "status-pill", printer.online ? (printer.stale ? "Online · veraltet" : "Online") : "Offline");
      if (!printer.online) status.classList.add("is-offline");
      header.append(identity, status);
      const sections = element("div", "diagnostic-sections");
      sections.append(
        diagnosticSection("Verbindung", [["Status", printer.connection_state], ["Letztes Signal", printer.last_seen ? new Date(printer.last_seen).toLocaleString("de-DE") : "–"], ["Schreibzugriff", printer.writable], ["Developer LAN", printer.developer_lan_mode]]),
        diagnosticSection("Gerät", [["WLAN", printer.device?.wifi_signal_dbm === null || printer.device?.wifi_signal_dbm === undefined ? "–" : `${printer.device.wifi_signal_dbm} dBm`], ["Tür offen", printer.device?.door_open], ["SD-Karte", printer.device?.sd_card?.status || printer.device?.sd_card?.present], ["HMS-Meldungen", printer.device?.hms?.count ?? 0]]),
        diagnosticSection("Kamera & AMS", [["Kamera eingerichtet", printer.camera?.configured], ["Kamera gemeldet", printer.camera?.reported_available], ["AMS-Einheiten", Array.isArray(printer.ams) ? printer.ams.length : 0], ["Trocknung aktiv", Array.isArray(printer.ams) && printer.ams.some((unit) => unit.drying_active)]]),
        diagnosticSection("Firmware", [["Software", printer.device?.firmware?.printer?.software], ["Hardware", printer.device?.firmware?.printer?.hardware], ["Module", Array.isArray(printer.device?.firmware?.modules) ? printer.device.firmware.modules.length : 0]]),
        diagnosticSection("Druckphase", [["Phase", printer.device?.print_stage?.phase_id], ["Stufe", printer.device?.print_stage?.stage_id], ["Unterstufe", printer.device?.print_stage?.substage_id]]),
        diagnosticSection("Berechtigungen", [["Freigegebene Befehle", Array.isArray(printer.allowed_commands) ? printer.allowed_commands.length : 0], ["AMS-Trocknung", Array.isArray(printer.allowed_commands) && printer.allowed_commands.includes("start_drying") ? "Start & Stopp" : (printer.allowed_commands || []).includes("stop_drying") ? "Nur Stopp" : "Aus"]]),
      );
      card.append(header, sections);
      cards.append(card);
    });
    dom.diagnosticPrinters.replaceChildren(cards);
    dom.diagnosticsLoading.hidden = true;
    dom.diagnosticsContent.hidden = false;
  }

  async function loadDiagnostics() {
    dom.diagnosticsLoading.hidden = false;
    dom.diagnosticsContent.hidden = true;
    try {
      const response = await apiFetch("/api/admin/diagnostics", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(await responseError(response));
      renderDiagnostics(await response.json());
      state.diagnosticsLoaded = true;
    } catch (error) {
      dom.diagnosticsLoading.hidden = true;
      if (!(error instanceof SessionExpiredError)) showAlert("Diagnose nicht verfügbar", error.message);
    }
  }

  async function logout() {
    dom.logout.disabled = true;
    try {
      const csrf = getCookie("bambu_csrf");
      const response = await apiFetch("/api/logout", { method: "POST", headers: { "X-CSRF-Token": csrf } });
      if (!response.ok) throw new Error(await responseError(response));
      window.location.replace("/login?logout=1");
    } catch (error) {
      if (!(error instanceof SessionExpiredError)) {
        dom.logout.disabled = false;
        showAlert("Abmeldung nicht möglich", error.message);
      }
    }
  }

  window.addEventListener("hashchange", activateTab);
  dom.addPrinter.addEventListener("click", () => addPrinter(null, true));
  dom.printerList.addEventListener("input", (event) => {
    const card = event.target.closest(".printer-card");
    if (!card) return;
    if (event.target.dataset.field === "name") updatePrinterNumbers();
    if (event.target.dataset.field === "writable") syncDryingInvariant(card);
    if (event.target.dataset.command) syncDryingInvariant(card, event.target.dataset.command);
  });
  dom.printerList.addEventListener("click", (event) => {
    const button = event.target.closest(".remove-printer");
    if (!button || printerCards().length <= 1) return;
    const card = button.closest(".printer-card");
    const name = field(card, "name").value.trim() || "Diesen Drucker";
    if (!window.confirm(`${name} aus der Konfiguration entfernen? Die Änderung wird erst beim Speichern aktiv.`)) return;
    card.remove();
    updatePrinterNumbers();
    dom.addPrinter.focus();
  });
  dom.configForm.addEventListener("submit", (event) => { event.preventDefault(); openSaveDialog(); });
  dom.saveButton.addEventListener("click", openSaveDialog);
  dom.cancelSave.addEventListener("click", () => { clearCurrentPassword(); dom.saveDialog.close(); });
  dom.saveDialog.addEventListener("cancel", clearCurrentPassword);
  dom.saveDialogForm.addEventListener("submit", saveConfig);
  dom.historyFilters.addEventListener("submit", (event) => { event.preventDefault(); loadHistory(true); });
  dom.historyFilters.addEventListener("input", () => { dom.historyNext.disabled = true; });
  dom.refreshHistory.addEventListener("click", () => loadHistory(true));
  dom.historyNext.addEventListener("click", () => {
    if (!state.historyNextCursor) return;
    state.historyPrevious.push(state.historyCursor);
    state.historyCursor = state.historyNextCursor;
    state.historyPage += 1;
    loadHistory();
  });
  dom.historyPrevious.addEventListener("click", () => {
    if (!state.historyPrevious.length) return;
    state.historyCursor = state.historyPrevious.pop();
    state.historyPage = Math.max(1, state.historyPage - 1);
    loadHistory();
  });
  dom.refreshDiagnostics.addEventListener("click", loadDiagnostics);
  dom.logout.addEventListener("click", logout);

  activateTab();
  loadConfig();
})();
