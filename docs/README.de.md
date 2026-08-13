# Bambu MQTT Control – Kurzanleitung

Dieses inoffizielle Standalone-Projekt überwacht und steuert Bambu-Drucker und
verbundene AMS-Systeme direkt im lokalen Netz. Es benötigt weder eine
Bambu-Cloud-Anmeldung noch Teile eines anderen Raspberry-Pi-Projekts.

## Installation

Vorausgesetzt werden Docker Engine und Docker Compose V2. Der Drucker muss vom
Docker-System auf Port `8883` erreichbar sein; für das X1-Livebild zusätzlich
auf Port `322`.

```sh
git clone https://github.com/Nature0ne/bambu-mqtt-control.git
cd bambu-mqtt-control
cp .env.example .env
docker compose up -d
./scripts/show-setup-token
```

Danach `https://localhost:9444/` öffnen und den einmaligen Token eingeben. Der
Assistent speichert Web-Anmeldung, Drucker-IP, Modell, Seriennummer und
LAN-Zugangscode in einem privaten Docker-Volume. Neue Drucker bleiben zunächst
schreibgeschützt.

Das Standard-Setup lädt ein fertiges Image für AMD64 oder ARM64. Eine lokale
Entwicklerversion wird so gebaut:

```sh
docker compose -f compose.yaml -f deploy/compose.build.yaml up -d --build
```

## Zugriff im lokalen Netz

Vor dem ersten Start in `.env` einen stabilen Hostnamen oder die LAN-IP setzen:

```dotenv
BAMBU_SITE_HOST=192.168.1.20
BAMBU_BIND_ADDRESS=192.168.1.20
BAMBU_HTTPS_PORT=9444
TZ=Europe/Berlin
```

Der mitgelieferte Caddy-Proxy erzeugt automatisch ein lokales HTTPS-Zertifikat.
Port `9444` niemals ins Internet weiterleiten; für Fernzugriff ein VPN nutzen.
Die interne Zustandsprüfung des Proxys läuft ausschließlich auf
`127.0.0.1:2018` innerhalb des Containers und ist von außen nicht erreichbar.

## Verwaltung und Funktionen

Über **Verwalten** können nach der Anmeldung Drucker ergänzt, Zugangsdaten
erneuert und Steuerrechte einzeln freigeschaltet werden. Gespeicherte
Passwörter und LAN-Codes werden dabei nie an den Browser zurückgegeben. Für das
Speichern ist das aktuelle Web-Passwort erforderlich.

Zusätzlich bietet die Seite:

- einen filterbaren Befehlsverlauf ohne Geheimnisse;
- Diagnosewerte wie Firmware, Wi-Fi, Tür, SD-Karte, Druckstufe und begrenzte
  HMS-Codes;
- dynamische Lichtsteuerung für tatsächlich gemeldete Lichtknoten;
- AMS-2-Pro- und AMS-HT-Trocknung mit Modell-, Temperatur-, Zeit- und
  Zustandsprüfung;
- X1-Kamerastream sowie einzeln freigebbare Aufnahme-, Zeitraffer- und
  Auflösungsbefehle.

Normale Schreibbefehle werden abgelehnt, solange der Drucker den Developer-LAN-
Modus nicht eindeutig meldet. Das Stoppen einer gemeldeten Trocknung ist die
bewusste Sicherheitsausnahme und benötigt weiterhin die konfigurierte Erlaubnis.

## Backup und Wiederherstellung

```sh
mkdir -p backup
chmod 700 backup
./scripts/backup backup/bambu-control-$(date +%F).tar.gz
```

Das Archiv enthält Passwörter und Zugangscodes und muss privat sowie möglichst
verschlüsselt aufbewahrt werden. Wiederherstellung:

```sh
./scripts/restore backup/bambu-control-2026-08-13.tar.gz
```

Die Skripte verwenden dasselbe Compose-Modell wie die laufende Installation.
Bei einem externen Proxy wird beiden Befehlen
`BAMBU_COMPOSE_FILES=deploy/compose.external-proxy.yaml` vorangestellt. Beim
lokalen Build lautet der Präfix
`BAMBU_COMPOSE_FILES=compose.yaml:deploy/compose.build.yaml`. Die übliche
Compose-Variable `COMPOSE_FILE` wird ebenfalls unterstützt.

Vor dem Austausch werden Archiv, Konfiguration und Datenbank geprüft. Bei einem
Fehler wird bytegenau zurückgerollt. Falls selbst diese Rücknahme ausnahmsweise
nicht vollständig gelingt, bleibt der Dienst gestoppt und bewahrt versteckte
Rettungskopien für die manuelle Prüfung auf.

## Aktualisierung

Verwende bei allen Befehlen dieselbe Compose-Auswahl wie bei der Installation:

```sh
# Mitgelieferter Caddy / veröffentlichtes Image
docker compose pull
docker compose up -d

# Vorhandener externer HTTPS-Proxy
COMPOSE_FILE=deploy/compose.external-proxy.yaml docker compose pull
COMPOSE_FILE=deploy/compose.external-proxy.yaml docker compose up -d

# Lokaler Build
COMPOSE_FILE=compose.yaml:deploy/compose.build.yaml docker compose build
COMPOSE_FILE=compose.yaml:deploy/compose.build.yaml docker compose up -d
```

`docker compose down` behält die Daten. `docker compose down -v` löscht dagegen
Konfiguration, Zugangsdaten, Verlauf und lokale Zertifizierungsstelle endgültig.

## Sicherheitsregeln

- Zuerst nur beobachten und Schreibrechte einzeln freigeben.
- Keine Zugangscodes, Passwörter, Seriennummern, privaten Kamera-URLs oder rohen
  MQTT-Nachrichten in Issues und Logs veröffentlichen.
- Freie MQTT-Payloads und freier G-Code sind absichtlich nicht verfügbar.
- Eine echte AMS-Trocknung ist ein Heizvorgang und darf nie als automatischer
  Funktionstest gestartet werden.
- HTTPS-Warnungen nur nach bewusster Prüfung akzeptieren; alternativ die lokale
  Caddy-Stamm-CA wie in der englischen README beschrieben installieren.

Weitere Angaben zu Reverse Proxy, Sicherheitsmodell, Metriken und Entwicklung
stehen in der [Hauptdokumentation](../README.md). Der Projektquellcode steht
unter der MIT-Lizenz.
