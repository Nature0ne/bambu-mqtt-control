(() => {
  "use strict";

  const dom = {
    alert: document.querySelector("#login-alert"),
    alertMessage: document.querySelector("#login-alert-message"),
    alertTitle: document.querySelector("#login-alert-title"),
    button: document.querySelector("#login-button"),
    expiredNotice: document.querySelector("#expired-notice"),
    form: document.querySelector("#login-form"),
    password: document.querySelector("#login-password"),
    setupNotice: document.querySelector("#setup-notice"),
    togglePassword: document.querySelector("#toggle-password"),
    username: document.querySelector("#login-username"),
  };

  let submitting = false;

  function hideAlert() {
    dom.alert.hidden = true;
  }

  function showAlert(title, message) {
    dom.alertTitle.textContent = title;
    dom.alertMessage.textContent = message;
    dom.alert.hidden = false;
    dom.alert.focus();
  }

  function setSubmitting(value) {
    submitting = value;
    dom.username.disabled = value;
    dom.password.disabled = value;
    dom.togglePassword.disabled = value;
    dom.button.disabled = value;
    dom.button.classList.toggle("is-loading", value);
    dom.button.querySelector(".button-label").textContent = value ? "Anmeldung wird geprüft …" : "Anmelden";
  }

  function validateForm() {
    dom.username.setCustomValidity(dom.username.value.trim() ? "" : "Gib deinen Web-Benutzer ein.");
    dom.password.setCustomValidity(dom.password.value ? "" : "Gib dein Passwort ein.");
    const invalid = [dom.username, dom.password].find((input) => !input.checkValidity());
    if (!invalid) return true;
    invalid.focus();
    invalid.reportValidity();
    return false;
  }

  async function submitLogin(event) {
    event.preventDefault();
    if (submitting || !validateForm()) return;

    hideAlert();
    setSubmitting(true);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);

    try {
      const response = await fetch("/api/login", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          username: dom.username.value.trim(),
          password: dom.password.value,
        }),
        signal: controller.signal,
      });

      if (response.status === 200 || response.status === 204) {
        dom.password.value = "";
        window.location.replace("/");
        return;
      }
      if (response.status === 401) {
        showAlert("Anmeldung fehlgeschlagen", "Web-Benutzer oder Passwort ist nicht korrekt.");
      } else if (response.status === 429) {
        showAlert("Zu viele Versuche", "Bitte warte kurz und versuche die Anmeldung anschließend erneut.");
      } else {
        showAlert("Anmeldung nicht möglich", `Der lokale Dienst hat die Anmeldung nicht angenommen (HTTP ${response.status}).`);
      }
    } catch (error) {
      if (error.name === "AbortError") {
        showAlert("Zeitüberschreitung", "Der lokale Dienst hat nicht rechtzeitig geantwortet. Versuche es bitte erneut.");
      } else {
        showAlert("Dienst nicht erreichbar", "Die Verbindung zum lokalen Dienst ist fehlgeschlagen. Prüfe dein Netzwerk und versuche es erneut.");
      }
    } finally {
      window.clearTimeout(timeout);
      setSubmitting(false);
    }
  }

  dom.togglePassword.addEventListener("click", () => {
    const reveal = dom.password.type === "password";
    dom.password.type = reveal ? "text" : "password";
    dom.togglePassword.textContent = reveal ? "Verbergen" : "Anzeigen";
    dom.togglePassword.setAttribute("aria-pressed", String(reveal));
    dom.password.focus();
  });

  dom.form.addEventListener("input", hideAlert);
  dom.form.addEventListener("submit", submitLogin);

  const query = new URLSearchParams(window.location.search);
  if (query.get("setup") === "1") {
    dom.setupNotice.hidden = false;
  }
  if (query.get("expired") === "1") {
    dom.expiredNotice.hidden = false;
  }
})();
