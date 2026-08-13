# Open Dachs Manager – Bedienungsanleitung

Stand: 13.08.2026 · Version V3 1.1.1

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
der ersten Anmeldung ändern. Im eigenen Hauptbereich `System` können Admins
mehrere Konten anlegen, Rollen und Status ändern, Passwörter zurücksetzen und
Konten löschen. Änderungen an einem Konto beenden dessen offene Sitzungen.

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

Die Startseite beginnt mit `Wirkleistung Ist / Soll`. Der Wartungsstatus steht
oben neben `Seriell OK` als verbleibende Betriebsstunden und Tage; ein Klick
öffnet direkt die Wartungsansicht. Der Leistungs-Sollwert ist für Gäste nur
lesbar. Ein Admin-Schreibvorgang benötigt weiterhin Authentifizierung,
Bestätigung und Readback.

Admins können die Live-Kacheln über `Bearbeiten` frei sortieren, entfernen und
mit `+` aus allen gemappten Werten ergänzen. Die Pfeile funktionieren auch auf
Touch-Geräten. Die gemeinsame Auswahl wird lokal auf dem Pi gespeichert und
gilt auch für den Gastzugang. Werte aus zusätzlichen Blöcken werden alle zehn
Sekunden gelesen; sie vergrößern nicht automatisch die Zeitreihen-Datenbank.

Die technische Anlagenansicht zeigt am Waben-Rußfilter links die
Motorabgastemperatur und rechts die Dachs-Abgastemperatur nach dem Filter.
Darunter steht ein ausdrücklich als Schätzung gekennzeichneter Füllstand. Die
Standardkennlinie setzt 420 °C auf 0 % und 520 °C auf 100 %; dazwischen wird
linear interpoliert. Unter `Einstellungen → Geschätzter Füllstand` kann der
Admin beide Temperaturen lokal ändern. Die Anzeige schreibt nie in den Regler.

### Überwachung

Die Überwachung schreibt ausgewählte Werte in die lokale SQLite-Datenbank und
stellt Zeitreihen dar. `Web-Serialzugriff pausieren` stoppt nur das
Web-Polling. Worker, CLI und TUI bleiben erreichbar.

Alle regulär überwachten Livewerte werden zusätzlich zunächst 24 Stunden als
komprimierte Roh-Snapshots gehalten. Sobald die Drehzahl größer als null ist,
bleiben der komplette Motorlauf sowie je eine Stunde davor und danach in
voller Auflösung erhalten. Übrige Stillstandszeiten werden danach in
15-Minuten-Fenster mit Anfang, Ende, Minimum, Maximum, Mittelwert, Anzahl und
Änderungszahl verdichtet.

### Wartung

`Wartung starten & Anlage einlesen` liest alle seriell adressierbaren Blöcke
in einer gemeinsamen Sitzung. Der Snapshot enthält Rohdaten, dekodierte
Felder, Lauf-/Servicehistorien und einzelne Lesefehler.

Der daraus erzeugte Entwurf wird lokal gespeichert. Checkliste, zusätzliche
lokale Arbeitsliste, Monteur, Messwerte und Bemerkungen lassen sich fortlaufend
ergänzen. HTML-, dreiseitiger PDF-Bericht und JSON-Export stehen zur Verfügung.

V3 1.1.1 wird standardmäßig im **Testmodus** ausgeliefert. Beim Abschluss verlangt
die Oberfläche die exakte Eingabe `DEMO ABSCHLIESSEN`. Danach wird der Bericht
lokal validiert, unveränderlich archiviert und deutlich als Demo gekennzeichnet.
Der Abschluss öffnet keine Serialworker-Sitzung, schreibt weder Block 100 noch
Block 104 und setzt kein Bestätigungsbit. Das gilt auch dann, wenn ein Browser
manipulierte API-Daten sendet, weil der Schreibschutz serverseitig aktiv ist.

Unter `System → Wartungsabschluss` kann ausschließlich der Admin diesen Modus
umschalten. Die Wahl wird lokal auf dem Pi gespeichert und überlebt Neustarts
und Updates. Das Umschalten selbst öffnet keine Serialworker-Sitzung und
schreibt nichts in den Regler. Bei deaktiviertem Testmodus verlangt der echte
Hardwareabschluss weiterhin:

- vollständig bewertete Checkliste
- Adminrolle
- gültiges Auth-Level/PW4
- die exakte Bestätigung `WARTUNG ABSCHLIESSEN`
- positives ACK und vollständigen Readback

Erst in diesem ausdrücklich freigeschalteten Echtbetrieb würden die gemappten
Wartungswerte übertragen, zurückgelesen und danach das Bestätigungsbit separat
gesetzt und nochmals gelesen. Freitext, Zusatzarbeiten und vollständige
Historie verbleiben immer lokal.

Admins können im lokalen Archiv sowohl offene Entwürfe als auch abgeschlossene
Wartungen über `Löschen` entfernen. Vorher erscheint eine Sicherheitsabfrage.
Gelöscht werden der lokale Snapshot, das Protokoll und seine Exporte. Ein
bereits ausgeführter MSR2-Abschluss wird dadurch nicht rückgängig gemacht;
vorhandene Schreib-Audits bleiben erhalten.

### Einstellungen und Register

Der integrierte `Fehlerkatalog` löst bekannte Service- und Warncodes direkt in
deutschen Klartext auf. Die Suche nimmt Code oder Text an; beispielsweise wird
`SC 163` exakt als `Leistung zu klein` angezeigt. Aktive Meldungen im
Anlagenbild sowie die Servicehistorie verwenden denselben Katalog. Eine
optionale lokale Diagnosedatei kann Ursachen und Maßnahmen ergänzen. Für einen
noch nicht zugeordneten Code bleibt die Nummer sichtbar und wird ausdrücklich
als unbekannt gekennzeichnet.

Die Registeransicht gruppiert alle adressierbaren Mapping-Felder nach Block.
Auswahllisten und Wertebereiche sind Eingabehilfen; ein Rohwert-Fallback bleibt
für unbekannte Varianten erhalten.

In Block 20 liegt `Zeitsynchronisierung aktiv` physisch an Offset 41. Die
Originaloberfläche dokumentiert keine Wertetabelle; `0` und `1` werden deshalb
als plausibel inaktiv/aktiv beschriftet, andere Werte bleiben unbekannte
Rohwerte. Offset 40 ist der Display-Kontrast. In Block 24 wird der
Kraftstofftyp mit den exakten Originalbezeichnungen angezeigt, beispielsweise
`8 (Heizöl EL)`. `Luftdruck` ist ein barometrischer 16-Bit-Rohwert ohne
dokumentierte Einheit oder Skalierung und ist nicht auf Gasanlagen beschränkt;
ein gelesener Wert `0` wird nicht künstlich umgerechnet. Die Temperatur an
Offset 20 heißt `Kapseltemperatur` und bleibt in °C skaliert.

Die gemeinsame Dachs-Laufhistorie der Blöcke 28, 30, 31 und 32 zeigt die fünf
Abschaltwerte zusätzlich als Hexwert und Klartext. Dabei handelt es sich um
eine aktive-low 16-Bit-Freigabemaske, nicht um Servicecodes; mehrere gelöschte
Bits ergeben mehrere gleichzeitig angezeigte Abschaltgründe.

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

### System, Benutzer und API-Tokens

Der Hauptbereich `System` ist nur für Admins sichtbar und ausdrücklich von
den Dachs-Einstellungen getrennt. Unter `Benutzer & Berechtigungen` lassen
sich mehrere Gast- und Admin-Konten verwalten. Mindestens ein aktiver Admin
bleibt erzwungen; das aktuell verwendete Admin-Konto kann sich nicht selbst
deaktivieren oder löschen.

Unter `API & Tokens` wird die lokale EDOMI-API verwaltet. Ein Token erhält
getrennte Rechte:

- `read`: Livecache, Katalog und kontrollierte Block-Lesevorgänge
- `history`: adaptive Roh- und Verdichtungsdaten
- `write`: serverseitige Aktion `set-value`

Das vollständige Token wird nur unmittelbar nach der Erzeugung angezeigt;
dauerhaft liegt ausschließlich sein SHA-256-Hash vor. API-Schreiben ist
zusätzlich global standardmäßig deaktiviert. Erst wenn ein Admin es im
Systembereich freischaltet, kann ein aktives Token mit `write`-Recht eine
Aktion auslösen. EDOMI übergibt dabei weder PW4 noch rohe Blockbytes.

Beispiel:

```http
POST /api/v1/actions/set-value
Authorization: Bearer <Token>
Content-Type: application/json

{"block":50,"key":"Hka_Ew.usSollGenerator","value":4.7,"request_id":"edomi-4711"}
```

Die eindeutige `request_id` verhindert, dass eine bereits verarbeitete
Anfrage bei einer Wiederholung nochmals geschrieben wird. Sie ist fest an
Token, Block, CPU, Feld und Wert gebunden. Eine parallele Wiederholung oder die
Wiederverwendung mit anderem Inhalt liefert HTTP 409 und löst keinen zweiten
Write aus. Für die Generatornennleistung prüft der Server vor Auth und Write
zusätzlich den vom Regler gelesenen Kraftstofftyp und dessen dokumentierten
Leistungsbereich. HTTP bleibt für das lokale Netz verfügbar; eine externe
TLS-Terminierung erfolgt bei Bedarf vor dem Dienst durch nginx.

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
