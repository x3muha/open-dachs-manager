# Open Dachs Manager – Bedienungsanleitung

Stand: 03.08.2026 · Version V3 0.9.0

Diese Anleitung beschreibt den täglichen Betrieb der Weboberfläche, CLI und
TUI. Installation und Migration stehen ausführlich in
[INSTALLATION.md](INSTALLATION.md).

## 1. Schnellstart

Nach der Installation:

```bash
open-dachs doctor
open-dachs read block --block 20
sudo systemctl status \
  open-dachs-manager-serial.service \
  open-dachs-manager-web.service --no-pager
```

Weboberfläche:

```text
http://<IP-Adresse-des-Pi>:8084
```

Bei einer frischen Installation stehen die zufällig erzeugten Erstpasswörter
einmal im Installerauszug und im ersten Webdienst-Log. Das Adminpasswort nach
der ersten Anmeldung ändern. Unter `Einstellungen` kann der Admin auch das
Gastpasswort neu setzen. Der Gast kann sein Passwort nicht selbst ändern;
eine Änderung beendet alle offenen Gastsitzungen.

## 2. Gemeinsamer serieller Zugriff

Nur der Hintergrunddienst `open-dachs-manager-serial.service` öffnet den
Adapter. Web, CLI und TUI verbinden sich mit
`/run/open-dachs-manager/serial.sock`.

Eine Verbindung ist eine vollständige FIFO-Sitzung. Zusammengehörige Vorgänge
wie ein Wartungs-Gesamtsnapshot oder Authentifizierung, Write und Readback
bleiben ungeteilt. Mehrere Oberflächen dürfen deshalb gleichzeitig geöffnet
sein; ein großer Auftrag kann kleinere Aufträge lediglich verzögern.

Direkte alte Programme, KNX-Reader oder Diagnosewerkzeuge verwenden diese
Queue nicht. Sie dürfen nur bei gestopptem Worker auf den Adapter zugreifen.

## 3. Weboberfläche

### Rollen

- `gast`: lesen, Diagramme und Berichte ansehen
- `admin`: zusätzlich Einstellungen vorbereiten, Wartungsdaten pflegen und
  ausdrücklich freigegebene Schreibabläufe starten

Die Anmeldung läuft lokal. Sitzungen enden nach zwölf Stunden oder beim
Abmelden. Passwörter werden gesalzen und gehasht in der lokalen Benutzerdatei
gespeichert.

### Übersicht

Die Startseite zeigt Livewerte, Betriebszustand, Wartungsstatus,
Generatorleistung und das Anlagenbild. Der Leistungs-Sollwert ist für Gäste
nur lesbar. Ein Admin-Schreibvorgang benötigt weiterhin Authentifizierung,
Bestätigung und Readback.

### Überwachung

Die Überwachung schreibt ausgewählte Werte in die lokale SQLite-Datenbank und
stellt Zeitreihen dar. `Web-Serialzugriff pausieren` stoppt nur das
Web-Polling. Worker, CLI und TUI bleiben erreichbar.

### Wartung

`Wartung starten & Anlage einlesen` liest alle seriell adressierbaren Blöcke
in einer gemeinsamen Sitzung. Der Snapshot enthält Rohdaten, dekodierte
Felder, Lauf-/Servicehistorien und einzelne Lesefehler.

Der daraus erzeugte Entwurf wird lokal gespeichert. Checkliste, zusätzliche
lokale Arbeitsliste, Monteur, Messwerte und Bemerkungen lassen sich fortlaufend
ergänzen. HTML-, dreiseitiger PDF-Bericht und JSON-Export stehen zur Verfügung.

V3 0.9.0 wird zunächst im **Demomodus** ausgeliefert. Beim Abschluss verlangt
die Oberfläche die exakte Eingabe `DEMO ABSCHLIESSEN`. Danach wird der Bericht
lokal validiert, unveränderlich archiviert und deutlich als Demo gekennzeichnet.
Der Abschluss öffnet keine Serialworker-Sitzung, schreibt weder Block 100 noch
Block 104 und setzt kein Bestätigungsbit. Das gilt auch dann, wenn ein Browser
manipulierte API-Daten sendet, weil der Schreibschutz serverseitig aktiv ist.

Der spätere echte Hardwareabschluss ist bereits als kontrollierter Ablauf
vorbereitet, aber in der normalen V3-0.9.0-Installation deaktiviert. Seine
Freischaltung würde zusätzlich verlangen:

- vollständig bewertete Checkliste
- Adminrolle
- gültiges Auth-Level/PW4
- die exakte Bestätigung `WARTUNG ABSCHLIESSEN`
- positives ACK und vollständigen Readback

Erst in diesem ausdrücklich freigeschalteten Echtbetrieb würden die gemappten
Wartungswerte übertragen, zurückgelesen und danach das Bestätigungsbit separat
gesetzt und nochmals gelesen. Freitext, Zusatzarbeiten und vollständige
Historie verbleiben immer lokal.

### Einstellungen und Register

Die Registeransicht gruppiert alle adressierbaren Mapping-Felder nach Block.
Auswahllisten und Wertebereiche sind Eingabehilfen; ein Rohwert-Fallback bleibt
für unbekannte Varianten erhalten.

Ohne aktivierte Hardwarefreigabe erzeugt Speichern lediglich einen Dry-Run.
Vor jedem echten Write werden Block, Feld, Datentyp, Authentifizierung und der
aktuelle Payload geprüft.

Zusätzlich erscheinen zwei rote Registerkarten `CPU 1 · Netzschutz` und
`CPU 2 · Netzschutz`. Die Blocknummern der drei CPUs sind voneinander
unabhängig; Block 16 der beiden Überwachungs-CPUs ist daher nicht Block 16 der
Regler-CPU. Die Karten zeigen das zum alten Überwachungscontroller gehörende
18-Byte-Layout mit Ländercode, Schutzart, festen und variablen Spannungs- und
Frequenzgrenzen, Abschaltzeiten und Impedanzschutz.

Die Felder sind wie alle gemappten Register schreibbar. Wegen ihrer besonderen
Bedeutung für den Netzschutz werden sie dauerhaft rot dargestellt, damit sie
nicht mit normalen Reglerwerten verwechselt oder versehentlich geändert
werden. Ohne den Haken `Hardware-Schreiben aktivieren` entsteht nur ein
Dry-Run; mit Haken gelten PW4/Auth, vollständiger Block-Write, positives ACK,
bytegenauer Readback und Audit. Das angezeigte Legacy-Profil `VDE 4105` ist
kein Nachweis der Konformität mit einer heutigen Normausgabe.

Der Aufbau von Zieladressen und Block-Lesevorgängen ist in
[PROTOKOLL.md](PROTOKOLL.md) bytegenau beschrieben.

### Audit

Schreibversuche und deren Ergebnis werden lokal protokolliert. Ein fehlendes
ACK oder abweichendes Readback gilt als Fehler, nicht als erfolgreicher Write.

## 4. CLI-Grundlagen

Globale Optionen stehen vor dem Unterkommando:

```text
open-dachs [Optionen] <Kommando> [Kommandooptionen]
```

Wichtige globale Optionen:

| Option | Standard | Bedeutung |
|---|---:|---|
| `--serial-socket` | `/run/open-dachs-manager/serial.sock` | gemeinsamer Worker |
| `--port` | `/dev/ttyUSB0` | nur für direkten Wartungsmodus |
| `--baud` | `19200` | Baudrate |
| `--timeout` | `0.9` | Antworttimeout |
| `--direct-serial` | aus | Worker umgehen; nur bei gestopptem Worker |
| `--pack-rev` | `50` | Mappingrevision |

Diagnose:

```bash
open-dachs doctor
open-dachs doctor --json
```

Blöcke und Felder suchen:

```bash
open-dachs list-blocks --addressable-only
open-dachs list-keys --block 20
open-dachs list-keys --search temperatur
```

## 5. Lesen

Rohblock:

```bash
open-dachs read block --block 20
open-dachs read block --block 20 --json
```

Dekodierte Blöcke:

```bash
open-dachs read decoded --blocks 20,22,24,26
open-dachs read decoded --blocks 20,22 --json
```

Link beobachten:

```bash
open-dachs watch --count 10 --interval 0.5
```

CRC- und Protokollfehler werden getrennt gezählt. Ungültige Frames werden
verworfen.

## 6. Backups

Ausgewählte Blöcke:

```bash
open-dachs backup create \
  --blocks 20,22,24,26 \
  --output open-dachs-backup.json
```

Alle über das Einbyte-Protokoll adressierbaren Blöcke:

```bash
open-dachs backup create \
  --all-blocks \
  --output open-dachs-full-backup.json
```

Backups enthalten Geräte- und Konfigurationsdaten und dürfen nicht ungeprüft
veröffentlicht werden.

## 7. Authentifizierung

```bash
open-dachs auth --level 4
```

Das Programm liest die benötigten Gerätedaten und berechnet die vierstellige
PW4. Eine manuelle Vorgabe ist möglich:

```bash
open-dachs auth --level 4 --pass4 1234
```

PW4 nur bewusst mit `--show-secret` anzeigen und nie in gemeinsame Logs
kopieren.

## 8. Schreiben mit der CLI

Dry-Run:

```bash
open-dachs write plan \
  --block <BLOCK> \
  --key '<TECHNISCHER_KEY>' \
  --value '<NEUER_WERT>'
```

Echter Write:

```bash
open-dachs write apply \
  --block <BLOCK> \
  --key '<TECHNISCHER_KEY>' \
  --value '<NEUER_WERT>' \
  --auth-level <LEVEL> \
  --write-enabled
```

`--write-enabled` allein reicht nicht: Auth-Level, ACK und bytegenaues
Readback müssen ebenfalls erfolgreich sein.

## 9. TUI

Nur lesen bzw. Änderungen vorbereiten:

```bash
open-dachs tui --block 20 --all-blocks --dry-run
```

Schreibfähiger Start:

```bash
open-dachs tui \
  --block 20 \
  --all-blocks \
  --auth-level <LEVEL> \
  --write-enabled
```

Tasten:

| Taste | Funktion |
|---|---|
| Pfeil hoch/runter oder `k`/`j` | Feld wählen |
| Pfeil links/rechts | Block wechseln |
| `Enter` | Feld bearbeiten |
| `F2` oder `s` | Dry-Run bzw. Speichern starten |
| `r` | Block neu laden |
| `q`, `Esc`, `F10` | beenden |

Die TUI hält die Queue nicht während der gesamten Bedienzeit. Laden und
Speichern sind jeweils eigene kurze Sitzungen.

## 10. Updates und Datensicherung

Update:

```bash
cd open-dachs-manager
git pull --ff-only
sudo ./install.sh
```

Dateisicherung der lokalen Webdaten:

```bash
sudo systemctl stop open-dachs-manager-web.service
sudo tar -C /var/lib -czf open-dachs-manager-data.tgz open-dachs-manager
sudo systemctl start open-dachs-manager-web.service
```

Die Daten liegen standardmäßig unter `/var/lib/open-dachs-manager`.

## 11. Fehlerbehebung

Worker und Web prüfen:

```bash
sudo systemctl status \
  open-dachs-manager-serial.service \
  open-dachs-manager-web.service --no-pager

sudo journalctl \
  -u open-dachs-manager-serial.service \
  -u open-dachs-manager-web.service -n 100 --no-pager
```

Portbesitzer prüfen:

```bash
fuser -v /dev/ttyUSB0
```

Im Normalbetrieb darf nur der Open-Dachs-Worker erscheinen. Weitere
Fehlerbilder und der direkte Wartungsfallback stehen in
[OPERATIONS.md](OPERATIONS.md).
