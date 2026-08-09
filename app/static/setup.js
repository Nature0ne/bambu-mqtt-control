(() => {
  "use strict";

  const dom = {
    addPrinterButton: document.querySelector("#add-printer-button"),
    alert: document.querySelector("#setup-alert"),
    alertMessage: document.querySelector("#setup-alert-message"),
    alertTitle: document.querySelector("#setup-alert-title"),
    bootstrapToken: document.querySelector("#bootstrap-token"),
    form: document.querySelector("#setup-form"),
    mobileProgress: document.querySelector(".mobile-progress"),
    mobileProgressValue: document.querySelector("#mobile-progress-value"),
    mobileStepLabel: document.querySelector("#mobile-step-label"),
    password: document.querySelector("#web-password"),
    passwordConfirm: document.querySelector("#web-password-confirm"),
    passwordMatch: document.querySelector("#password-match"),
    passwordQuality: document.querySelector(".password-quality"),
    printerList: document.querySelector("#printer-list"),
    printerTemplate: document.querySelector("#printer-form-template"),
    qualityLabel: document.querySelector("#quality-label"),
    redirectNote: document.querySelector("#redirect-note"),
    reviewContent: document.querySelector("#review-content"),
    submitButton: document.querySelector("#submit-setup"),
    successView: document.querySelector("#success-view"),
    username: document.querySelector("#web-username"),
  };

  const COMMAND_LABELS = {
    camera_recording: "Kameraaufnahme",
    camera_resolution: "Kameraauflösung",
    camera_timelapse: "Zeitraffer",
    pause: "Pause",
    resume: "Fortsetzen",
    stop: "Abbrechen",
    speed: "Tempo",
    light: "Licht",
    refresh_rfid: "RFID aktualisieren",
    start_drying: "AMS-Trocknung starten",
    stop_drying: "AMS-Trocknung stoppen",
  };

  const LOGIN_URL = "/login?setup=1";

  const app = {
    currentStep: 1,
    printerSequence: 0,
    submitting: false,
    redirectTimer: null,
  };

  function printerCards() {
    return [...dom.printerList.querySelectorAll(".printer-form-card")];
  }

  function field(card, name) {
    return card.querySelector(`[data-field="${name}"]`);
  }

  function element(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function updateCardNumbers() {
    const cards = printerCards();
    cards.forEach((card, index) => {
      const ordinal = index + 1;
      card.querySelector(".printer-number").textContent = String(ordinal);
      const removeButton = card.querySelector(".remove-printer");
      removeButton.disabled = cards.length === 1;
      removeButton.setAttribute("aria-label", `Drucker ${ordinal} entfernen`);
      updateCardTitle(card);
    });
  }

  function updateCardTitle(card) {
    const name = field(card, "name").value.trim();
    card.querySelector(".printer-card-name").textContent = name || "Neuer Drucker";
  }

  function setCardIdentifiers(card, key) {
    card.querySelectorAll("[data-field]").forEach((input) => {
      const fieldName = input.dataset.field;
      input.id = `printer-${key}-${fieldName}`;
      input.name = `printer_${key}_${fieldName}`;
    });
    card.querySelectorAll("[data-for]").forEach((label) => {
      label.htmlFor = `printer-${key}-${label.dataset.for}`;
    });

    const idHelp = card.querySelector(".printer-id-help");
    idHelp.id = `printer-${key}-id-help`;
    field(card, "id").setAttribute("aria-describedby", idHelp.id);

    const lanHelp = card.querySelector(".lan-code-help");
    lanHelp.id = `printer-${key}-lan-code-help`;
    field(card, "access_code").setAttribute("aria-describedby", lanHelp.id);

    const writeWarning = card.querySelector(".permission-warning");
    writeWarning.id = `printer-${key}-write-warning`;
    field(card, "writable").setAttribute("aria-describedby", writeWarning.id);

    const cameraHelp = card.querySelector(".camera-setup-note");
    cameraHelp.id = `printer-${key}-camera-help`;
    field(card, "camera_enabled").setAttribute("aria-describedby", cameraHelp.id);
  }

  function addPrinter() {
    app.printerSequence += 1;
    const key = app.printerSequence;
    const card = dom.printerTemplate.content.firstElementChild.cloneNode(true);
    card.dataset.printerKey = String(key);
    setCardIdentifiers(card, key);
    field(card, "id").value = `drucker-${key}`;
    dom.printerList.append(card);
    syncWritable(card);
    syncTls(card);
    updateCardNumbers();
    if (key > 1) {
      field(card, "name").focus();
      card.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function removePrinter(card) {
    if (printerCards().length <= 1) return;
    const nextFocus = card.previousElementSibling?.querySelector("[data-field='name']") || dom.addPrinterButton;
    card.remove();
    updateCardNumbers();
    nextFocus.focus();
  }

  function syncWritable(card) {
    const writable = field(card, "writable").checked;
    const panel = card.querySelector(".permission-panel");
    panel.hidden = !writable;
    field(card, "writable").setAttribute("aria-expanded", String(writable));
    card.querySelectorAll("[data-command]").forEach((checkbox) => {
      checkbox.disabled = !writable;
    });
    syncDryingPermissions(card);
    syncCameraPermissions(card);
  }

  function normalizedModel(card) {
    return field(card, "model").value.trim().toUpperCase().replaceAll(/[^A-Z0-9]/g, "");
  }

  function syncDryingPermissions(card) {
    const model = normalizedModel(card);
    const writable = field(card, "writable").checked;
    const blocked = model.startsWith("P1") || model.startsWith("A1");
    const experimental = model.startsWith("X1");
    const info = card.querySelector(".drying-permission-info");
    const note = card.querySelector(".drying-permission-note");
    const startPermission = card.querySelector('[data-command="start_drying"]');
    const stopPermission = card.querySelector('[data-command="stop_drying"]');
    const stopCard = stopPermission.closest(".check-card");
    const stopHelp = stopCard.querySelector("small");

    if (blocked) {
      startPermission.checked = false;
      stopPermission.checked = false;
    }
    if (startPermission.checked) stopPermission.checked = true;
    startPermission.disabled = !writable || blocked;
    stopPermission.disabled = !writable || blocked || startPermission.checked;
    stopCard.classList.toggle("is-required-by-start", writable && !blocked && startPermission.checked);
    stopHelp.textContent = startPermission.checked
      ? "Pflichtrecht, solange Start aktiviert ist"
      : "Laufende AMS-Trocknung beenden";

    info.classList.toggle("is-blocked", blocked);
    info.classList.toggle("is-experimental", experimental && !blocked);
    if (blocked) {
      note.textContent = "Für P1- und A1-Modelle wird die lokale Trocknungssteuerung nicht angeboten.";
    } else if (experimental) {
      note.textContent = "Auf X1/X1C ist diese Funktion experimentell. Developer-LAN-Modus und eine ausdrückliche Bestätigung sind nötig; das Startrecht schließt das Stopprecht immer ein.";
    } else {
      note.textContent = "Aktiviere sie nur für AMS 2 Pro oder AMS HT. Das Startrecht schließt den Sicherheits-Stopp immer ein; Stop-only bleibt möglich.";
    }
  }

  function syncCameraPermissions(card) {
    const modelSupported = normalizedModel(card).startsWith("X1");
    const writable = field(card, "writable").checked;
    const cameraEnabled = field(card, "camera_enabled");
    const permissions = [...card.querySelectorAll("[data-camera-permission]")];
    const setupInfo = card.querySelector(".camera-setup-info");
    const setupNote = card.querySelector(".camera-setup-note");
    const commandInfo = card.querySelector(".camera-permission-info");
    const commandNote = card.querySelector(".camera-command-note");

    if (!modelSupported) {
      cameraEnabled.checked = false;
      permissions.forEach((permission) => {
        permission.checked = false;
      });
    }
    cameraEnabled.disabled = !modelSupported;
    permissions.forEach((permission) => {
      permission.disabled = !writable || !modelSupported;
    });

    setupInfo.classList.toggle("is-blocked", !modelSupported);
    setupInfo.classList.toggle("is-enabled", modelSupported && cameraEnabled.checked);
    commandInfo.classList.toggle("is-blocked", !modelSupported);
    if (!modelSupported) {
      setupNote.textContent = "Wähle ein X1-Modell, um das lokale Livebild freizugeben. Andere Modellreihen erhalten keine Kamerabedienung.";
      commandNote.textContent = "Kamerasteuerungen stehen nur für ein unterstütztes X1-Modell zur Verfügung.";
    } else {
      setupNote.textContent = cameraEnabled.checked
        ? "Das Livebild kann nach der Anmeldung manuell gestartet werden. Zugangsdaten werden ausschließlich serverseitig verwendet."
        : "Das Livebild bleibt ausgeschaltet. Zugangsdaten werden nicht an den Browser weitergegeben.";
      commandNote.textContent = writable
        ? "Aktiviere nur benötigte Kameraaktionen. Sie sind vom separaten Livebild-Opt-in unabhängig."
        : "Aktiviere Schreibzugriff nur, wenn Aufnahme, Zeitraffer oder Auflösung geändert werden sollen.";
    }
  }

  function syncTls(card) {
    const allowSelfSigned = field(card, "allow_self_signed_tls").checked;
    const warning = card.querySelector(".tls-warning");
    const fingerprintGroup = card.querySelector(".tls-fingerprint");
    card.querySelector(".tls-standard").classList.toggle("is-overridden", allowSelfSigned);
    warning.hidden = !allowSelfSigned;
    fingerprintGroup.hidden = !allowSelfSigned;
    field(card, "allow_self_signed_tls").setAttribute("aria-expanded", String(allowSelfSigned));
  }

  function togglePassword(input, button) {
    const shouldReveal = input.type === "password";
    input.type = shouldReveal ? "text" : "password";
    button.textContent = shouldReveal ? "Verbergen" : "Anzeigen";
    button.setAttribute("aria-pressed", String(shouldReveal));
    button.setAttribute("aria-label", `${input.labels?.[0]?.textContent?.trim() || "Geheimnis"} ${shouldReveal ? "verbergen" : "anzeigen"}`);
  }

  function updatePasswordQuality() {
    const password = dom.password.value;
    let score = 0;
    if (password.length >= 12) score += 1;
    if (password.length >= 16) score += 1;
    if (/[a-z]/.test(password) && /[A-Z]/.test(password) && /\d/.test(password) && /[^A-Za-z0-9]/.test(password)) score += 1;

    dom.passwordQuality.classList.remove("level-1", "level-2", "level-3");
    if (!password) {
      dom.qualityLabel.textContent = "Mindestens 12 Zeichen";
      return;
    }
    if (score <= 1) {
      dom.passwordQuality.classList.add("level-1");
      dom.qualityLabel.textContent = password.length < 12 ? `Noch ${12 - password.length} Zeichen` : "Ausreichend";
    } else if (score === 2) {
      dom.passwordQuality.classList.add("level-2");
      dom.qualityLabel.textContent = "Gut";
    } else {
      dom.passwordQuality.classList.add("level-3");
      dom.qualityLabel.textContent = "Sehr gut";
    }
  }

  function syncPasswordConfirmation() {
    const confirmation = dom.passwordConfirm.value;
    const matches = confirmation && confirmation === dom.password.value;
    dom.passwordConfirm.setCustomValidity(confirmation && !matches ? "Die Passwörter stimmen nicht überein." : "");
    dom.passwordMatch.classList.toggle("is-valid", Boolean(matches));
    dom.passwordMatch.classList.toggle("is-error", Boolean(confirmation && !matches));
    dom.passwordMatch.textContent = matches
      ? "Passwörter stimmen überein."
      : confirmation
        ? "Die Passwörter stimmen nicht überein."
        : "Beide Eingaben müssen übereinstimmen.";
  }

  function prepareAccessValidation() {
    dom.bootstrapToken.setCustomValidity(dom.bootstrapToken.value.trim() ? "" : "Gib den Bootstrap-Code ein.");
    const username = dom.username.value.trim();
    dom.username.setCustomValidity(
      username && !username.includes(":") ? "" : "Der Web-Benutzer darf nicht leer sein oder einen Doppelpunkt enthalten.",
    );
    dom.password.setCustomValidity(
      dom.password.value === dom.password.value.trim()
        ? ""
        : "Das Passwort darf nicht mit Leerzeichen beginnen oder enden.",
    );
    syncPasswordConfirmation();
  }

  function normalizeFingerprint(value) {
    return value.trim().replaceAll(":", "").toLowerCase();
  }

  function preparePrinterValidation() {
    const cards = printerCards();
    const idCounts = new Map();
    const serialCounts = new Map();

    cards.forEach((card) => {
      const idInput = field(card, "id");
      const nameInput = field(card, "name");
      const modelInput = field(card, "model");
      const serialInput = field(card, "serial");
      const accessCodeInput = field(card, "access_code");
      const fingerprintInput = field(card, "tls_fingerprint_sha256");
      const writable = field(card, "writable").checked;
      idInput.setCustomValidity("");
      nameInput.setCustomValidity(nameInput.value.trim() ? "" : "Gib einen Anzeigenamen ein.");
      modelInput.setCustomValidity(modelInput.value.trim() ? "" : "Gib das Druckermodell ein.");
      serialInput.setCustomValidity("");
      accessCodeInput.setCustomValidity(
        accessCodeInput.value === accessCodeInput.value.trim()
          ? ""
          : "Der LAN-Code darf nicht mit Leerzeichen beginnen oder enden.",
      );
      fingerprintInput.setCustomValidity("");

      const id = idInput.value.trim().toLowerCase();
      const serial = serialInput.value.trim().toUpperCase();
      if (id) idCounts.set(id, (idCounts.get(id) || 0) + 1);
      if (serial) serialCounts.set(serial, (serialCounts.get(serial) || 0) + 1);

      const fingerprint = normalizeFingerprint(fingerprintInput.value);
      if (fingerprint && !/^[a-f0-9]{64}$/.test(fingerprint)) {
        fingerprintInput.setCustomValidity("Der SHA-256-Fingerprint muss aus genau 64 Hex-Zeichen bestehen.");
      }

      const commandInputs = [...card.querySelectorAll("[data-command]")];
      commandInputs.forEach((input) => input.setCustomValidity(""));
      if (writable && !commandInputs.some((input) => input.checked)) {
        commandInputs[0].setCustomValidity("Wähle mindestens eine erlaubte Aktion oder deaktiviere den Schreibzugriff.");
      }
    });

    cards.forEach((card) => {
      const idInput = field(card, "id");
      const serialInput = field(card, "serial");
      if ((idCounts.get(idInput.value.trim().toLowerCase()) || 0) > 1) {
        idInput.setCustomValidity("Jeder Drucker benötigt eine eindeutige interne ID.");
      }
      if ((serialCounts.get(serialInput.value.trim().toUpperCase()) || 0) > 1) {
        serialInput.setCustomValidity("Diese Seriennummer wurde bereits verwendet.");
      }
    });
  }

  function firstInvalidInput(step) {
    const section = document.querySelector(`.setup-step[data-step="${step}"]`);
    return [...section.querySelectorAll("input")].find((input) => !input.checkValidity()) || null;
  }

  function revealInvalidInput(input) {
    input.closest("details")?.setAttribute("open", "");
    input.classList.add("is-invalid");
    input.focus({ preventScroll: true });
    input.scrollIntoView({ behavior: "smooth", block: "center" });
    input.reportValidity();
  }

  function validateStep(step) {
    if (step === 1) prepareAccessValidation();
    if (step === 2) preparePrinterValidation();
    const invalid = firstInvalidInput(step);
    if (!invalid) return true;
    revealInvalidInput(invalid);
    return false;
  }

  function hideAlert() {
    dom.alert.hidden = true;
  }

  function showAlert(title, message) {
    dom.alertTitle.textContent = title;
    dom.alertMessage.textContent = message;
    dom.alert.hidden = false;
    dom.alert.focus();
    dom.alert.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function goToStep(step) {
    if (step < 1 || step > 3) return;
    hideAlert();
    app.currentStep = step;
    document.querySelectorAll(".setup-step").forEach((section) => {
      section.hidden = Number(section.dataset.step) !== step;
    });
    document.querySelectorAll("[data-step-indicator]").forEach((indicator) => {
      const indicatorStep = Number(indicator.dataset.stepIndicator);
      indicator.classList.toggle("is-current", indicatorStep === step);
      indicator.classList.toggle("is-complete", indicatorStep < step);
      if (indicatorStep === step) indicator.setAttribute("aria-current", "step");
      else indicator.removeAttribute("aria-current");
      indicator.querySelector(".step-number").textContent = indicatorStep < step ? "✓" : String(indicatorStep);
    });
    dom.mobileProgressValue.className = `mobile-progress-value step-${step}`;
    dom.mobileStepLabel.textContent = `Schritt ${step} von 3`;
    if (step === 3) renderReview();
    document.querySelector(`.setup-step[data-step="${step}"] h2`)?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function addReviewDefinition(list, term, description) {
    const wrapper = element("div");
    wrapper.append(element("dt", "", term), element("dd", "", description));
    list.append(wrapper);
  }

  function reviewHeader(title, step) {
    const header = element("header", "review-section-header");
    const heading = element("h3", "", title);
    const editButton = element("button", "", "Bearbeiten");
    editButton.type = "button";
    editButton.dataset.editStep = String(step);
    editButton.setAttribute("aria-label", `${title} bearbeiten`);
    header.append(heading, editButton);
    return header;
  }

  function selectedCommands(card) {
    const commands = [...card.querySelectorAll("[data-command]:checked")].map((input) => input.dataset.command);
    if (commands.includes("start_drying") && !commands.includes("stop_drying")) commands.push("stop_drying");
    return commands;
  }

  function renderReview() {
    const fragment = document.createDocumentFragment();
    const account = element("section", "review-section");
    account.append(reviewHeader("Webzugang", 1));
    const accountList = element("dl", "review-list");
    addReviewDefinition(accountList, "Benutzer", dom.username.value.trim());
    addReviewDefinition(accountList, "Passwort", "Sicher gesetzt");
    account.append(accountList);
    fragment.append(account);

    const printersSection = element("section", "review-section");
    const cards = printerCards();
    printersSection.append(reviewHeader(`${cards.length} ${cards.length === 1 ? "Drucker" : "Drucker"}`, 2));
    const stack = element("div", "review-printer-stack");
    cards.forEach((card) => {
      const writable = field(card, "writable").checked;
      const selfSigned = field(card, "allow_self_signed_tls").checked;
      const printer = element("article", "review-printer");
      const header = element("div", "review-printer-header");
      const name = field(card, "name").value.trim();
      const mode = element("span", `mode-badge${writable ? " is-write" : ""}`, writable ? "Schreibzugriff" : "Nur lesen");
      header.append(element("strong", "", name), mode);
      printer.append(header);

      const facts = element("dl");
      addReviewDefinition(facts, "Modell / ID", `${field(card, "model").value.trim()} · ${field(card, "id").value.trim().toLowerCase()}`);
      addReviewDefinition(facts, "Adresse", `${field(card, "host").value.trim()}:${field(card, "port").value}`);
      addReviewDefinition(facts, "Seriennummer", field(card, "serial").value.trim());
      addReviewDefinition(facts, "Lokale Kamera", field(card, "camera_enabled").checked ? "Livebild freigegeben" : "Ausgeschaltet");
      addReviewDefinition(facts, "TLS", selfSigned ? "Selbstsigniert zugelassen" : "Bambu-CA");
      addReviewDefinition(facts, "Veraltet nach", `${field(card, "stale_after_seconds").value} Sek.`);
      addReviewDefinition(facts, "Vollabgleich", `${field(card, "full_refresh_seconds").value} Sek.`);
      printer.append(facts);

      const commands = element("div", "review-commands");
      if (writable) {
        selectedCommands(card).forEach((command) => commands.append(element("span", "", COMMAND_LABELS[command] || command)));
      } else {
        commands.append(element("span", "", "Keine Steuerbefehle aktiv"));
      }
      printer.append(commands);
      stack.append(printer);
    });
    printersSection.append(stack);
    fragment.append(printersSection);
    dom.reviewContent.replaceChildren(fragment);
  }

  function collectPayload() {
    return {
      bootstrap_token: dom.bootstrapToken.value.trim(),
      web: {
        username: dom.username.value.trim(),
        password: dom.password.value,
      },
      printers: printerCards().map((card) => ({
        id: field(card, "id").value.trim().toLowerCase(),
        name: field(card, "name").value.trim(),
        model: field(card, "model").value.trim(),
        host: field(card, "host").value.trim(),
        port: Number.parseInt(field(card, "port").value, 10),
        serial: field(card, "serial").value.trim(),
        access_code: field(card, "access_code").value,
        writable: field(card, "writable").checked,
        camera_enabled: field(card, "camera_enabled").checked,
        allowed_commands: selectedCommands(card),
        allow_self_signed_tls: field(card, "allow_self_signed_tls").checked,
        tls_fingerprint_sha256: normalizeFingerprint(field(card, "tls_fingerprint_sha256").value) || null,
        stale_after_seconds: Number.parseInt(field(card, "stale_after_seconds").value, 10),
        full_refresh_seconds: Number.parseInt(field(card, "full_refresh_seconds").value, 10),
      })),
    };
  }

  function errorMessage(status, payload) {
    const detail = payload?.detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const messages = detail.map((item) => item?.msg).filter(Boolean);
      if (messages.length) return messages.join(" · ");
    }
    if (typeof payload?.message === "string" && payload.message.trim()) return payload.message;
    if (status === 401 || status === 403) return "Der Bootstrap-Code ist ungültig oder nicht mehr gültig.";
    if (status === 409) return "Dieses Kontrollzentrum wurde bereits eingerichtet. Öffne die Startseite, um dich anzumelden.";
    if (status === 422) return "Mindestens eine Eingabe wurde vom Server abgelehnt. Bitte prüfe die Konfiguration.";
    if (status === 429) return "Zu viele Versuche. Warte kurz und versuche es anschließend erneut.";
    return `Der Server konnte die Konfiguration nicht speichern (HTTP ${status}).`;
  }

  function setSubmitting(submitting) {
    app.submitting = submitting;
    dom.submitButton.disabled = submitting;
    dom.submitButton.classList.toggle("is-loading", submitting);
    dom.submitButton.querySelector(".button-label").textContent = submitting ? "Wird sicher gespeichert …" : "Einrichtung abschließen";
    dom.form.querySelectorAll("button, input").forEach((control) => {
      if (control !== dom.submitButton) control.disabled = submitting;
    });
    if (!submitting) {
      printerCards().forEach((card) => {
        syncWritable(card);
        syncTls(card);
      });
      updateCardNumbers();
    }
  }

  function clearSecrets() {
    dom.bootstrapToken.value = "";
    dom.password.value = "";
    dom.passwordConfirm.value = "";
    printerCards().forEach((card) => {
      field(card, "access_code").value = "";
    });
  }

  function showSuccess() {
    if (app.redirectTimer !== null) window.clearInterval(app.redirectTimer);
    clearSecrets();
    dom.form.hidden = true;
    dom.mobileProgress.hidden = true;
    hideAlert();
    dom.successView.hidden = false;
    dom.successView.focus();
    window.scrollTo({ top: 0, behavior: "smooth" });

    let seconds = 5;
    app.redirectTimer = window.setInterval(() => {
      seconds -= 1;
      if (seconds <= 0) {
        window.clearInterval(app.redirectTimer);
        app.redirectTimer = null;
        window.location.replace(LOGIN_URL);
        return;
      }
      dom.redirectNote.textContent = `Du wirst in ${seconds} ${seconds === 1 ? "Sekunde" : "Sekunden"} weitergeleitet.`;
    }, 1000);
  }

  async function setupCompletedOnServer() {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("/api/setup/status", {
        method: "GET",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) return false;
      const payload = await response.json();
      return payload?.configured === true;
    } catch (_error) {
      return false;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function submitSetup(event) {
    event.preventDefault();
    if (app.submitting) return;

    prepareAccessValidation();
    preparePrinterValidation();
    for (const step of [1, 2]) {
      if (firstInvalidInput(step)) {
        goToStep(step);
        window.setTimeout(() => {
          const invalid = firstInvalidInput(step);
          if (invalid) revealInvalidInput(invalid);
        }, 0);
        return;
      }
    }

    hideAlert();
    setSubmitting(true);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch("/api/setup", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(collectPayload()),
        signal: controller.signal,
      });
      let payload = null;
      try {
        payload = await response.json();
      } catch (_error) {
        payload = null;
      }
      if (!response.ok) throw new SetupResponseError(response.status, errorMessage(response.status, payload));
      showSuccess();
    } catch (error) {
      if (error instanceof SetupResponseError) {
        const configured = error.status === 409 && await setupCompletedOnServer();
        if (configured) {
          showSuccess();
        } else {
          showAlert("Konfiguration nicht gespeichert", error.message);
        }
      } else {
        const configured = await setupCompletedOnServer();
        if (configured) {
          showSuccess();
        } else if (error.name === "AbortError") {
          showAlert("Zeitüberschreitung", "Der lokale Dienst hat nicht rechtzeitig geantwortet. Prüfe, ob er noch läuft, und versuche es erneut.");
        } else {
          showAlert("Dienst nicht erreichbar", "Die Verbindung zum lokalen Einrichtungsdienst ist fehlgeschlagen. Prüfe Netzwerk und Dienststatus.");
        }
      }
    } finally {
      window.clearTimeout(timeout);
      if (!dom.successView.hidden) return;
      setSubmitting(false);
    }
  }

  class SetupResponseError extends Error {
    constructor(status, message) {
      super(message);
      this.name = "SetupResponseError";
      this.status = status;
    }
  }

  dom.form.addEventListener("click", (event) => {
    const nextButton = event.target.closest("[data-next-step]");
    if (nextButton) {
      if (validateStep(app.currentStep)) goToStep(app.currentStep + 1);
      return;
    }
    if (event.target.closest("[data-previous-step]")) {
      goToStep(app.currentStep - 1);
      return;
    }
    const passwordButton = event.target.closest("[data-password-toggle]");
    if (passwordButton) {
      const input = document.getElementById(passwordButton.dataset.passwordToggle);
      if (input) togglePassword(input, passwordButton);
      return;
    }
    const editButton = event.target.closest("[data-edit-step]");
    if (editButton) goToStep(Number(editButton.dataset.editStep));
  });

  dom.printerList.addEventListener("click", (event) => {
    const card = event.target.closest(".printer-form-card");
    if (!card) return;
    if (event.target.closest(".remove-printer")) {
      removePrinter(card);
      return;
    }
    const passwordButton = event.target.closest("[data-card-password-toggle]");
    if (passwordButton) {
      const input = field(card, passwordButton.dataset.cardPasswordToggle);
      if (input) togglePassword(input, passwordButton);
    }
  });

  dom.form.addEventListener("input", (event) => {
    if (event.target instanceof HTMLInputElement) event.target.classList.remove("is-invalid");
    if (event.target === dom.password) {
      updatePasswordQuality();
      syncPasswordConfirmation();
    }
    if (event.target === dom.passwordConfirm) syncPasswordConfirmation();
    const card = event.target.closest(".printer-form-card");
    if (card && event.target === field(card, "name")) updateCardTitle(card);
    if (card && event.target === field(card, "model")) {
      syncDryingPermissions(card);
      syncCameraPermissions(card);
    }
  });

  dom.form.addEventListener("change", (event) => {
    const card = event.target.closest(".printer-form-card");
    if (!card) return;
    if (event.target === field(card, "writable")) syncWritable(card);
    if (event.target === field(card, "camera_enabled")) syncCameraPermissions(card);
    if (event.target === field(card, "allow_self_signed_tls")) syncTls(card);
    if (event.target.matches("[data-drying-permission]")) syncDryingPermissions(card);
  });

  dom.addPrinterButton.addEventListener("click", addPrinter);
  dom.form.addEventListener("submit", submitSetup);
  addPrinter();
  updatePasswordQuality();
})();
