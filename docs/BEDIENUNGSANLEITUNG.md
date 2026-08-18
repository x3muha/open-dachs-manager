# Open Dachs Manager – Bedienungsanleitung

Stand: 18.08.2026 · Version V3 1.5.0

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

`Wartung starten & Pflichtbackup erstellen` liest alle 38 freigegebenen
Sicherungsziele genau einmal in einer gemeinsamen seriellen Sitzung. Derselbe
eingefrorene Lesezustand speist gleichzeitig das vollständige JSON-Abbild und
den Anlagenstand des Berichts; es folgt keine zweite Blockrunde. Nur wenn das
Abbild atomar gespeichert und über Abbild- sowie Datei-SHA-256 geprüft ist,
entsteht der Wartungsentwurf. Der Anlagenstand enthält Rohdaten, dekodierte
Felder, Lauf-/Servicehistorien und einzelne Lesefehler.

Der daraus erzeugte Entwurf wird lokal gespeichert. Checkliste, zusätzliche
lokale Arbeitsliste, Monteur, Messwerte und Bemerkungen lassen sich fortlaufend
ergänzen. HTML-, dreiseitiger PDF-Bericht und JSON-Export stehen zur Verfügung.

V3 1.5.0 wird standardmäßig im **Testmodus** ausgeliefert. Beim Abschluss verlangt
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

Admins können im lokalen Archiv offene Entwürfe und abgeschlossene Wartungen
über `Löschen` entfernen. Vorher erscheint eine Sicherheitsabfrage. Gelöscht
werden der lokale Anlagenstand, das Protokoll und seine Exporte. Das beim
Wartungsstart erzeugte Pflichtbackup bleibt getrennt im geschützten
Backup-Archiv erhalten. Ein bereits ausgeführter MSR2-Abschluss wird dadurch
nicht rückgängig gemacht; vorhandene Schreib-Audits bleiben erhalten.

Während eines echten Abschlusses zeigt die Liste `Abschluss läuft`. Dieser
Zustand kann weder erneut abgeschlossen noch gelöscht werden. Konnte nach
einem Schreibversuch kein eindeutiges Endergebnis belegt werden, erscheint
`Zielzustand unklar – prüfen` in Rot. Auch dieser Bericht bleibt gesperrt, bis
Reglerzustand, positive Bestätigung, Rückleseprüfung und Audit fachlich geklärt
sind.

### Backup und Wiederherstellung

Der Hauptbereich `Backup` ist für angemeldete Benutzer sichtbar. Beim Öffnen
sind alle 38 Sicherungsziele ausgewählt: 36 adressierbare Blöcke der
Regler-CPU 0 sowie Block 16 der Netzschutz-CPU 1 und der Netzschutz-CPU 2. Über
`Alle auswählen`, `Auswahl aufheben` und die einzelnen Blockkarten kann eine
beliebige Teilmenge zusammengestellt werden. `Backup erstellen und
herunterladen` liest nur diese Ziele in einer gemeinsamen Serialworker-Sitzung
und speichert ein JSON-Abbild. Einzelne Lesefehler bleiben darin sichtbar.

Administratoren sehen darüber das geschützte Wartungsbackup-Archiv. Jede Karte
nennt Backup-ID, Zeitpunkt und Ersteller, Herkunft, verknüpften
Wartungsbericht, Zustand, Integrität, Größe, Packrevision, das Ergebnis 38/38
sowie Abbild- und Datei-SHA-256. Es gibt bewusst weder Löschen noch einen Knopf
für manuelle Autoarchivierung. `JSON herunterladen` liefert die archivierte
V3-Datei unverändert. `Für Wiederherstellung laden` lädt dieselbe Datei und
prüft sie erneut über die bestehende Abbildprüfung. Nur 38/38 Ziele mit
Prüfsummen, passender Packrevision und Gerätebindung werden übernommen; die
Zielauswahl bleibt leer und der Dry-Run aktiv.

Im Abbild wird jedes Ziel als eindeutiges Paar aus CPU und Blocknummer geführt.
Dadurch bleiben `CPU 1 · Block 16` und `CPU 2 · Block 16` trotz gleicher
Blocknummer getrennt. Vollständige Abbilder können Anlagenkennung, Adresse,
Kontaktdaten und sicherheitsrelevante Netzschutzparameter enthalten und müssen
entsprechend vertraulich aufbewahrt werden. PW4 oder andere Passwörter werden
nicht gespeichert.

Die Wiederherstellung ist nur für Administratoren sichtbar. Zuerst wird das
JSON-Abbild eingelesen und serverseitig auf Schema, Prüfsummen, Packstand,
Reglerkennung, Blocknummern und Payloadlängen geprüft. Erst danach erscheinen
die gültigen Blöcke; aus Sicherheitsgründen ist zunächst keiner ausgewählt.

`Auswahl als Dry-Run prüfen` liest die aktuellen Zielblöcke und zeigt
`unverändert` oder `würde geschrieben`, sendet aber weder Authentifizierung noch
Schreibtelegramme. Für eine echte Wiederherstellung müssen zusätzlich
Hardwarefreigabe, Auth-Level beziehungsweise PW4 und exakt
`SICHERUNG WIEDERHERSTELLEN` bestätigt werden. Bytegleiche Blöcke werden auch
dann ohne Schreibtelegramm übersprungen. Abweichende Blöcke werden vollständig
geschrieben und nur nach positiver Bestätigung sowie bytegenauer vollständiger
Rückleseprüfung als wiederhergestellt gemeldet.

Vor der Authentifizierung eines tatsächlich abweichenden Live-Ablaufs liest der
Dienst die Auswahl nochmals und speichert diesen unmittelbaren Ausgangszustand
atomar als SHA-256-gebundenes JSON-Abbild unter
`/var/lib/open-dachs-manager/restore-preimages/`. Verzeichnis und Dateien sind
lokal mit `0700` beziehungsweise `0600` geschützt. Ein Dry-Run oder ein
bytegleicher Live-Vergleich legt kein solches Abbild an, weil dabei kein
Schreibtelegramm folgen kann. Die Prüfsumme bindet den Inhalt, stellt aber keine
kryptografische Signatur oder Herkunftsbestätigung dar.

Eine Mehrblock-Wiederherstellung ist nicht transaktional. Bei einem Fehler
stoppt der Ablauf; zuvor erfolgreich zurückgelesene Blöcke bleiben
wiederhergestellt. Laufzustände, Zähler oder Zeitwerte im Abbild können älter
sein und werden bei Auswahl des jeweiligen Rohblocks ebenfalls übernommen. Hat
der Dienst bereits ein Schreibtelegramm gesendet, aber keine eindeutige
Bestätigung oder Rückleseprüfung erhalten, wird der Block ausdrücklich als
`Zustand unklar` gekennzeichnet und muss vor jedem weiteren Versuch neu gelesen
und geprüft werden.

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

Zusätzlich erscheinen je Überwachungs-CPU drei rote Registerkarten. Die
Blocknummern der drei CPUs sind voneinander unabhängig; Block 20 der
Netzschutz-CPU ist also nicht Block 20 der Regler-CPU.

- `B16 · Netzschutz (Legacy)` zeigt das bekannte 18-Byte-Layout mit
  Ländercode, Schutzart, festen und variablen Spannungs-/Frequenzgrenzen,
  Abschaltzeiten und Impedanzschutz. Dieser bereits ausdrücklich freigegebene
  Block bleibt über den normalen Admin-/Auth-/ACK-/Readback-Ablauf schreibbar.
- `B20 · Schutzparameter` zeigt 39 Werte aus 59 Byte: Schutzprofil,
  zweistufige Grenzen und profilabhängige Abschaltzeiten, 10-Minuten-Spannung,
  Frequenzreduktion sowie Impedanz-/LOM-Status. Die originale
  Layout-4-Datenzuordnung belegt dafür den Vollblock-Schreibdienst 21; alle
  39 Felder sind über denselben geschützten Ablauf wie Block 16 editierbar.
- `B21 · Live-Netzwerte` zeigt 28 Werte aus 56 Byte: Spannung, Strom und
  Frequenz je Phase, Impedanzen, Wirkleistung, Phasenlage, Cosinus Phi und
  Kalibrierfaktoren. Dieser laufende Messwertblock besitzt keinen Schreibdienst
  und bleibt strikt nur lesbar.

Block 16 und Block 20 ergeben auf CPU 1 und CPU 2 zusammen 114 schreibbare
Feldinstanzen: je CPU 18 Legacy- und 39 Layout-4-Felder. Bei Block 20 lautet
der Ablauf `Lesen → kodieren → Authentifizierung → CAS-Prüfung → Dienst 21
→ positives ACK → exakte Vollblock-Rückleseprüfung`. Nicht eindeutig
umkehrbare Anzeigen werden nicht geraten. Eine bewusste Experteneingabe ist
als `raw:<Rohwert>` möglich; dadurch werden Skalierung und Auswahlprüfung
absichtlich umgangen, nicht aber Datentyp, Feldbreite, Authentifizierung,
Ausgangszustandsvergleich oder Rückleseprüfung.

Die Kodierung und der schreibfreie Dry-Run für Block 20 sind geprüft. Ein
physischer Schreibvorgang auf Block 20 wurde am Gerät noch nicht ausgeführt.
Deshalb bleibt Block 20 vorerst außerhalb von Backup und Wiederherstellung;
die Auswahl umfasst unverändert 38 geprüfte Ziele: 36 Reglerblöcke plus
Block 16 beider Netzschutz-CPUs. Block 21 darf als flüchtiger Messwertblock
niemals als Konfiguration zurückgeschrieben werden. Das reine Öffnen oder
Neuladen einer Netzschutzkarte sendet weiterhin nur ein Lesetelegramm.

Die geprüften Standarddefinitionen enthalten für die Netzüberwachungs-CPUs
keine weiteren Datenblöcke. Die zusätzlich live gelesenen Diagnosedienste 17
und 18 liegen nur als Rohbefund ohne belegte Feldstruktur vor und erscheinen
daher nicht als dekodierte Einstellfelder.

Profilbezeichnungen wie `VDE 4105` stammen aus der historischen
Controllerdefinition und sind kein Nachweis der Konformität mit einer heutigen
Normausgabe. Cosinus-Phi-Werte können bei stehender Anlage geringfügig über
1 liegen und sind dann als Diagnosewert zu verstehen.

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
veröffentlicht werden. Automatische Wartungsbackups liegen ausschließlich im
geschützten Serverarchiv; die Weboberfläche erlaubt dort nur Anzeige,
unveränderten Download und erneutes Prüfen für die Wiederherstellung.

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
