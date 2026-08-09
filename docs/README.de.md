# Bambu MQTT Control – Kurzanleitung

Dieses inoffizielle Projekt überwacht und steuert Bambu-Drucker und verbundene
AMS-Systeme direkt im lokalen Netz. Es benötigt weder eine Bambu-Cloud-Anmeldung
noch Teile eines anderen Raspberry-Pi-Projekts.

## Start mit Docker Compose

```sh
git clone https://github.com/Nature0ne/bambu-mqtt-control.git
cd bambu-mqtt-control
cp .env.example .env
docker compose up -d --build
./scripts/show-setup-token
```

Danach `https://localhost:9444/` öffnen und den einmaligen Token eingeben. Der
Assistent speichert Web-Anmeldung, Drucker-IP, Modell, Seriennummer und
LAN-Zugangscode in einem privaten Docker-Volume. Neue Drucker bleiben zunächst
schreibgeschützt.

Für den Zugriff von anderen Geräten vor dem ersten Start in `.env` einen
stabilen Hostnamen oder die LAN-IP setzen:

```dotenv
BAMBU_SITE_HOST=192.168.1.20
BAMBU_BIND_ADDRESS=192.168.1.20
BAMBU_HTTPS_PORT=9444
TZ=Europe/Berlin
```

Der mitgelieferte Caddy-Proxy erzeugt automatisch ein lokales HTTPS-Zertifikat.
Seine Stamm-CA kann mit dem in der englischen README beschriebenen Befehl
exportiert und auf den berechtigten Endgeräten als vertrauenswürdig installiert
werden.

## Wichtige Sicherheitsregeln

- Port `9444` niemals aus dem Internet weiterleiten; für Fernzugriff ein VPN
  verwenden.
- Zuerst nur beobachten und Schreibrechte einzeln freigeben.
- Keine Zugangscodes, Passwörter, Seriennummern oder rohen MQTT-Nachrichten in
  Issues oder Logs veröffentlichen.
- Freie MQTT-Payloads und freier G-Code sind absichtlich nicht verfügbar.
- Eine echte AMS-Trocknung ist ein Heizvorgang und darf nicht als automatischer
  Funktionstest gestartet werden.

Die vollständige Beschreibung, Backup-Anleitung und Entwicklerhinweise stehen
in der Hauptdatei `README.md`.

Der Projektquellcode steht unter der MIT-Lizenz. Hinweise zu Fremdkomponenten
und dem öffentlichen Drucker-CA-Bundle enthält `THIRD_PARTY_NOTICES.md`.
