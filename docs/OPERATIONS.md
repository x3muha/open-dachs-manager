# Betrieb und Fehlersuche

## Laufzeit seit Start (24-h-Zähler)

Der frühere optionale 24-h-Zähler-Autoreset ist entfernt. Es gibt keinen
Endpunkt `/api/settings/runtime-24h-auto` und keine automatischen
Auth-Level-3-/RAM-Writes mehr. Eine auf älteren Installationen vorhandene
`runtime_24h_auto_state.json` im Datenverzeichnis und die SQLite-Tabelle
`runtime_24h_auto_setting_tx` bleiben unangetastet liegen, werden aber weder
gelesen noch beschrieben. Alte Prüfprotokolleinträge von
`system:auto-runtime24h` bleiben im Audit erhalten.

Der manuelle Write des Zählers `0x0f0e` (Zahnrad der Kachel, Auth 3,
Readback, Deauth, Audit `runtime-24h-counter-write`) bleibt für Tests
unverändert. `0x0f0e` ist `Hka_Mw1.ulMotorlaufsekunden` und wird bei jedem
Start auf 0 gesetzt; ab 6 h kann die Nachtabschaltung greifen, ab 86.370 s
die 24-h-Abschaltung.

## Einstellbare SC123-Abgasgrenze

Der Adminpfad liegt unter `System → Firmware → Abgas-Abschaltgrenze`. Er ist
nur verfügbar, wenn die installierte Reglerfirmware in zwei unmittelbar
aufeinanderfolgenden Block-50-Lesungen an Offset 31 exakt einen +1-Heartbeat
zeigt, die übrigen stabilen Block-50-Bytes gleich bleiben und zwei frische
Block-60-Lesungen bytegenau übereinstimmen. Bei Stock-Firmware oder nach einem
Rückflash lautet der Status sinngemäß `Firmwarefähigkeit nicht erkannt`;
Vorschau und Schreiben bleiben gesperrt.

Die Statusroute ist `GET /api/sc123-temperature-limit`, die Vorschau
`POST /api/sc123-temperature-limit/preview` mit ausschließlich `value`, der
Schreibendpunkt `POST /api/sc123-temperature-limit` mit `value`, `confirm`,
`write_enabled` und dem Einmaltoken. Alle Routen sind Admin-only und laufen
unter dem konfigurierten Base Path, zum Beispiel
`/dachs/api/sc123-temperature-limit`. Adresse, Block, Auth-Level und PW4 sind
nicht clientseitig wählbar.

Ein bestätigter Vorgang verwendet Auth 3 und höchstens einen vollständigen
Block-60-Schreibvorgang. Der Datensatz an Offset 40 bis 45 lautet
`53 43 31 01 Wert Komplement`; anschließend werden Block 60 genau einmal aus
dem RAM und der persistente Datensatz begrenzt mit Lesedienst 0x04 geprüft,
dann folgt Auth 0. Das Audit heißt `sc123-temperature-limit-write`. Nach jedem
Schreibversuch greift 30 Sekunden eine Wiederholungssperre; bei unklarem
Ergebnis zuerst Zustand und Audit prüfen, nicht erneut klicken. Historisch
belegt sind nur 180 und 200 °C. Der Bereich 201–240 °C ist technisch codierbar,
aber nicht herstellerseitig oder thermisch live validiert. Diese Funktion
installiert oder flasht keine Firmware.

## Manueller Entstörpfad

Der flüchtige Servicecode-Reset ist standardmäßig fail-closed. Die dafür
verwendeten Stillstands- und Temperaturgrenzen sind keine allgemeine
Herstellerfreigabe. Erst nach installationsbezogener fachlicher Prüfung darf
in `/etc/open-dachs-manager/open-dachs-manager.env` bewusst gesetzt werden:

```text
OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=1
```

Neue Installationen erhalten ausdrücklich den Wert `0`. Bei einem Update
bewahrt `install.sh` eine vorhandene explizite `1`, aktiviert die Freigabe aber
niemals selbst. Danach den Webdienst neu starten. Die Freigabe aktiviert
keinen automatischen Reset: Adminrolle, aktive Störung, frische kohärente
Doppellesung, Störereignisidentität, sicherer Stillstand,
Temperaturprüfung, Einmaltoken, Auth 3, exaktes ACK/Echo, stabiler Readback
und Auth 0 bleiben Pflicht.

Vor Authentifizierung und letzter Sicherheitslesung persistiert ODM einen
Datensatz in `service_reset_write_intents`. Die 30-s-Bestätigung wird nach dem
Warten auf den Worker, vor Auth und direkt vor dem Frame erneut geprüft; die
letzte vollständige Doppellesung darf beim Frame höchstens eine Sekunde alt
sein. Intent-Auflösung und vollständiges Pflichtaudit werden atomar in einer
SQLite-Transaktion gespeichert. Nur ein bestätigter Write mit stabilem
Servicecode-0-Readback wird als `write-confirmed` aufgelöst; ein nachweislich
nicht gesendeter Versuch wird als abgebrochen protokolliert. Bleibt ein Intent
`pending`, darf der Webdienst neu gestartet werden, aber es darf kein
weiterer Entstörversuch erfolgen. Zuerst das Audit `manual-servicecode-reset`
und den aktuellen Reglerzustand fachlich prüfen und das gesamte Datenverzeichnis
sichern. Es gibt absichtlich keinen HTTP-Endpunkt und keinen automatischen
Timeout zum Löschen dieser Sperre. Eine manuelle Auflösung ist ein
Support-/Wartungseingriff und erst zulässig, wenn geklärt ist, ob das einmalige
Telegramm wirksam war; die Datenbankzeile nicht blind löschen oder verändern.

## Dienste

```bash
sudo systemctl status \
  open-dachs-manager-serial.service \
  open-dachs-manager-web.service --no-pager
```

Besitzer des seriellen Adapters prüfen:

```bash
fuser -v /dev/ttyUSB0
```

Nur `open-dachs-manager-serial.service` darf das Gerät geöffnet haben.
Weboberfläche, CLI und TUI verbinden sich stattdessen mit
`/run/open-dachs-manager/serial.sock`.

Beide Komponenten neu starten:

```bash
sudo systemctl restart open-dachs-manager-serial.service
sudo systemctl restart open-dachs-manager-web.service
```

Protokolle verfolgen:

```bash
sudo journalctl \
  -u open-dachs-manager-serial.service \
  -u open-dachs-manager-web.service -f
```

## Zustandsprüfung

```bash
open-dachs doctor
open-dachs read block --block 20
curl http://127.0.0.1:8084/healthz
```

`doctor` muss `transport=serial-worker` und einen verfügbaren Worker-Socket
melden.

Block 22, 24 und 26 werden in einem Zielabstand von 0,75 Sekunden gelesen. Der
tatsächliche Abstand kann nur dann größer werden, wenn die seriellen Antworten
zusammen länger dauern. Block 20 und die weiteren langsam veränderlichen
Blöcke werden alle zehn Sekunden ergänzt.

Dauerhaft in SQLite landen weiterhin ausgewählte Betriebs- und Messwerte
aus Block 22, 24 und 26 gemeinsam in genau einer kompakten Snapshot-Zeile je
schnellem Zyklus. Zusätzlich zeichnet die adaptive Historie alle regulär
gelesenen Livewerte zunächst mit voller Auflösung auf. Maßgeblich ist
`Hka_Mw1.usDrehzahl`: Ab einer Drehzahl größer null bleiben die Stunde vor dem
Start, die vollständige Laufzeit und eine Stunde Nachlauf hochaufgelöst
erhalten. Ruhige Zeiträume bei Drehzahl null werden nach 24 Stunden in
15-Minuten-Fenster mit Anfang, Ende, Minimum, Maximum, Mittelwert,
Änderungszahl und Stichprobenzahl verdichtet. Dadurch bleiben Start- und
Stopvorgänge untersuchbar, ohne jahrelang jede unveränderte Sekunde abzulegen.

Die Liveanzeige enthält weiterhin zusätzliche Werte. Ein optionaler
Mindestabstand für Historien-Snapshots kann in
`/etc/open-dachs-manager/open-dachs-manager.env` gesetzt werden:

```text
OPEN_DACHS_HISTORY_INTERVAL=0
```

`0` bedeutet: jeden erfolgreichen Livezyklus speichern. Ein positiver Wert
drosselt nur die klassische 21-Werte-Historie, nicht die Liveanzeige oder die
adaptive Historie.

## 250-h-Diagnose

Die Berechnung läuft im dedizierten Hintergrundworker
`dachs-250h-diagnosis` und ausschließlich gegen die lokale SQLite-Historie.
Der Cache `diagnosis_250h_cache.json` liegt im Produktdatenverzeichnis, ist
auf 4 MiB begrenzt und wird mit Modus `0600`, Datei- und Verzeichnis-`fsync`
sowie atomarem `os.replace` geschrieben. Ein gültiger Cache wird beim Start
sofort ausgeliefert und anschließend erneuert.

`GET /api/diagnosis/250h` liefert den Cachezustand; `?refresh=1` plant eine
manuelle Aktualisierung ein. Weitere Trigger sind Dienststart, die nächste
lokale Mitternacht in `Europe/Berlin` und ein nach fünf stabilen Sekunden
bestätigter Motorstopp. `running`, `stale`, `last_error`,
`generated_at`, `trigger_reason` und `next_midnight` dienen der
Betriebsdiagnose. Berechnungs- oder Persistenzfehler beenden den Worker nicht;
der letzte gültige Stand bleibt verfügbar. Die Funktion führt keine seriellen
Reads, Authentifizierungen oder Writes aus.

## Lokale EDOMI-API

Die Maschinen-API liegt unter `/api/v1/` beziehungsweise unter dem
konfigurierten Base Path. Sie verwendet Bearer-Tokens, die ausschließlich im
Adminbereich **System → API & Tokens** erstellt werden. Das vollständige Token
wird nur einmal beim Erstellen gezeigt; in der Datenbank liegt nur sein
SHA-256-Hash.

Lesen und Historie sind über getrennte Berechtigungen steuerbar. Schreiben ist
zusätzlich global deaktiviert und muss im Systembereich absichtlich
freigeschaltet werden. Die API nimmt nur logische Block-/Feld-/Wert-Aktionen
entgegen; Authentifizierung, PW4-Berechnung, Schreiben, ACK, Readback und Audit
bleiben vollständig im Server. Jeder Schreibaufruf benötigt eine eindeutige
`request_id`, damit EDOMI-Wiederholungen nicht doppelt schreiben. Der Server
reserviert sie atomar pro Token. Dieselbe ID mit anderem Inhalt oder während
einer noch nicht eindeutig abgeschlossenen Aktion ergibt HTTP 409.

Der eingebaute Dienst bleibt bewusst bei lokalem HTTP. TLS endet – falls
benötigt – am vorgeschalteten nginx. Port 8084 nicht direkt ins Internet
weiterleiten.

Bei konfiguriertem `OPEN_DACHS_BASE_PATH=/dachs` lauten Oberfläche und
Healthcheck intern `http://127.0.0.1:8084/dachs/` beziehungsweise
`/dachs/healthz`. Ein Reverse Proxy muss den Präfix in diesem Modus an den
Webdienst weiterreichen.

## SMTP- und E-Mail-Betrieb

Unter `System → E-Mail` werden SMTP-Host, Port, Sicherheitsmodus, Benutzer,
Passwort, Absender, Empfänger, öffentliche ODM-Adresse, Kategorien, Temperaturregeln und
Textvorlagen gepflegt. Neue Installationen und Updates aktivieren den Versand
niemals automatisch. Eine Testmail wird ebenfalls zunächst persistent in die
Outbox eingereiht und erst vom separaten Dispatcher versendet.

Die öffentliche Adresse muss eine kanonische HTTPS-Adresse einschließlich
Base Path sein, zum Beispiel `https://tools.example.de/dachs`. Sie wird nicht
aus `Host`- oder Proxy-Headern geraten. Das SMTP-Passwort steht ausschließlich
in einer versionierten Datei `/var/lib/open-dachs-manager/secrets/smtp-<generation>.json`; Verzeichnis und Datei
müssen 0700/0600 und Eigentum des Webdienstes besitzen. Konfiguration, Audit,
Backups und HTTP-Antworten enthalten das Passwort nicht.

Standard ist STARTTLS mit TLS mindestens 1.2 und normaler
Zertifikatsprüfung. Ein privates TLS-Relay wird ausschließlich durch root in
`/etc/open-dachs-manager/open-dachs-manager.env` exakt freigegeben:

```text
OPEN_DACHS_SMTP_PRIVATE_ALLOWLIST=relay.intern:587
```

Für ein internes Relay ohne TLS muss in der Oberfläche der Modus
`plain-internal` gewählt und eine getrennte root-Freigabe gesetzt werden:

```text
OPEN_DACHS_SMTP_PLAIN_ALLOWLIST=relay.intern:25
```

Dieser Modus akzeptiert nur nicht öffentlich routbare Zieladressen und
verbietet SMTP-Benutzer sowie Passwort. Mehrere Werte werden mit Komma
getrennt. `install.sh` bewahrt beide syntaktisch gültigen Allowlists, erzeugt
aber standardmäßig leere Werte.
Jeder Empfänger erhält eine eigene Mail. Die SQLite-Outbox wiederholt
vorübergehende Fehler begrenzt und markiert nach acht Versuchen `Aufgegeben`.
Die gleiche Message-ID bleibt über Retries stabil; eine doppelte Zustellung
nach dem engen Relay-250/SQLite-Crashfenster bleibt SMTP-bedingt möglich.
Konfigurationsfehler pausieren den Dispatcher auch über einen Webdienst-Neustart
hinweg bis zum nächsten Speichern; entfernte Empfänger und Kategorien widerrufen
wartende Einträge. Settingswechsel und Versand sind so serialisiert, dass nach
einem bestätigten Speichern kein alter Relay-/Empfängerstand mehr verwendet wird. Maximal 500
aktive Outboxzeilen, eine Testmail je 60 Sekunden, drei frische
Temperatur-Grenzmessungen und eine Stunde Temperatur-Cooldown begrenzen Flooding.
Terminale Inhalte bleiben 90 Tage, das redigierte Benachrichtigungsprotokoll
365 Tage erhalten; Pending/Failed wird nicht durch Retention gelöscht. Die
Bereinigung läuft unabhängig vom Serialmonitor im SMTP-Dispatcher.

## Lokaler Touch-Bedienplatz

Unter `System → Benutzer & Berechtigungen → Lokaler Benutzer` kann ein zweiter
HTTP-Listener aktiviert werden. Er bindet unveränderlich nur
`127.0.0.1`; Port 1 bis 65535 und die Rolle `admin` oder `guest` sind frei
wählbar. Standard sind Port 80 und Adminrechte, der Listener selbst ist
standardmäßig aus. Änderungen werden laufend übernommen; ein Portkonflikt
lässt den bisherigen Listener und die gespeicherte Einstellung unverändert.

Der lokale Browser verwendet keinen Login-Cookie. Der normale Listener bleibt
loginpflichtig, und die Maschinen-API `/api/v1/*` verlangt auf beiden Ports
weiter ihren Bearer-Token. Im Audit heißt der lokale Akteur eindeutig
`localhost-override`. nginx oder ein anderer Reverse Proxy darf niemals auf
den lokalen Port zeigen; sein Upstream bleibt der normale Web-Port 8084.

## CLI und TUI gleichzeitig mit der Weboberfläche

Folgende Befehle dürfen bei laufender Weboberfläche verwendet werden:

```bash
open-dachs read decoded --blocks 20,22,24,26
open-dachs tui --block 20 --all-blocks --dry-run
```

Die Warteschlange schützt vollständige Sitzungen. Ein großes Backup oder ein
Wartungssnapshot verzögert deshalb kurze Leseaufträge, aber kein serielles
Telegramm wird mit einem anderen Auftrag vermischt.

## Direkter serieller Notbetrieb

Der direkte Modus umgeht die Warteschlange und darf niemals gleichzeitig mit
dem Worker laufen:

```bash
sudo systemctl stop open-dachs-manager-web.service
sudo systemctl stop open-dachs-manager-serial.service

sudo /opt/open-dachs-manager/venv/bin/open-dachs \
  --direct-serial \
  --port /dev/serial/by-id/<adapter> \
  read block --block 20
```

Anschließend den normalen Betrieb wiederherstellen:

```bash
sudo systemctl start open-dachs-manager-serial.service
sudo systemctl start open-dachs-manager-web.service
```

## Datensicherung

Für eine konsistente Kopie auf Dateisystemebene nur den Webdienst stoppen:

```bash
sudo systemctl stop open-dachs-manager-web.service
sudo tar -C /var/lib -czf open-dachs-manager-data.tgz open-dachs-manager
sudo systemctl start open-dachs-manager-web.service
```

Das Archiv geschützt aufbewahren. Es enthält Passwort-Hashes, Anlagenwerte,
Wartungsberichte, automatische Reglerabbilder und Audit-Informationen.

Automatische Wartungsbackups liegen einzeln unter
`/var/lib/open-dachs-manager/backup-archive/`. Das Verzeichnis gehört
`open-dachs:open-dachs` und hat Modus `0700`; jede JSON-Datei hat Modus `0600`.
Jeder neue Eintrag muss Metadaten, Datei, Abbild-SHA-256 und Datei-SHA-256
konsistent binden und 42 erfolgreiche von 42 angeforderten Sicherungszielen
mit exakt 36 Restore-Zielen ausweisen. Geprüfte ältere Archive behalten ihren
38/38-Altvertrag.
Das Löschen eines Wartungsberichts löscht dieses Sicherungsabbild nicht.
Die Dateien werden nicht automatisch ausgedünnt. Das lokale Archiv liegt auf
demselben Datenträger wie die Anwendung und ersetzt daher keine regelmäßige
Sicherung des gesamten Datenverzeichnisses auf ein anderes System oder Medium.

Prüfen, ohne Inhalte auszugeben:

```bash
sudo stat -c '%U:%G %a %n' /var/lib/open-dachs-manager/backup-archive
sudo find /var/lib/open-dachs-manager/backup-archive -maxdepth 1 -type f \
  -printf '%u:%g %m %f\n'
```

Regler-Backup über den Serialworker:

```bash
open-dachs backup create \
  --all-blocks \
  --output open-dachs-controller-backup.json
```

Ein Wartungsabschluss mit Status `completing` darf nicht erneut gestartet
oder gelöscht werden. `uncertain` bedeutet, dass ein möglicher Schreibversuch
nicht eindeutig durch ACK und Rückleseprüfung abgeschlossen wurde. In diesem
Fall Reglerzustand und Audit zuerst fachlich prüfen; keinen blinden Wiederholungs-
oder Löschversuch ausführen.

## Fehlerbehebung

### Worker-Socket fehlt

```bash
sudo systemctl status open-dachs-manager-serial.service --no-pager
sudo journalctl -u open-dachs-manager-serial.service -n 50 --no-pager
ls -l /run/open-dachs-manager/serial.sock
```

### Serielles Gerät ist belegt

```bash
fuser -v /dev/ttyUSB0
ps aux | grep -i '[d]achs\|[t]tyUSB\|[p]yserial'
```

Direkte V2-, KNX- oder Diagnoseprogramme stoppen. Einen unbekannten Prozess
nicht beenden, bevor sein Zweck geklärt wurde.

### Webdienst läuft, kann aber nicht lesen

Zuerst den Worker prüfen, danach das konfigurierte Gerät:

```bash
sudo sed -n '1,20p' /etc/open-dachs-manager/open-dachs-manager.env
ls -la /dev/ttyUSB* /dev/serial/by-id/* 2>/dev/null
```

### Fehlercode erscheint ohne Klartext

Der integrierte Katalog liegt im installierten Python-Paket als
`fault_catalog_de.json` und verwendet das Schema
`open-dachs-manager/fault-catalog/v1`. Ein bekannter aktiver Code muss in der
API und Oberfläche gemeinsam mit seinem Text erscheinen, zum Beispiel
`SC 163 · Leistung zu klein`.

```bash
/opt/open-dachs-manager/venv/bin/python -c \
  'from open_dachs_manager.mapping import PackRepository; print(PackRepository().service_catalog("163")["items"])'
```

Fehlt nur die ausführliche Ursachen-/Maßnahmenliste, ist das kein Defekt des
Klartextkatalogs. Diese Details stammen weiterhin aus der optionalen lokalen
`servicecodes_de.properties`.

Konfigurationen, Regler-Backups und Webdaten vor dem Einfügen in öffentliche
Issues immer auf vertrauliche Anlageninformationen prüfen.

## Signierte Produktupdates

Status und Einstellungen stehen unter **System → Updates**. Manuell: prüfen,
Admin-Passwort für eine frische Vorschau eingeben, angezeigte Bestätigung exakt
übernehmen und anwenden. Ein kurzer Browser-Abbruch beim Dienstwechsel ist
normal; die Oberfläche fragt den persistenten Jobstatus nach dem Neustart ab.
Recovery: update-helper-state.json und root-eigene update-evidence prüfen.
current muss ein root-eigener Symlink direkt auf ein Releaseverzeichnis sein.

### Signierter Updater: Snapshot und Recovery

Der Root-Helfer hält Status, Evidenz und Backups ausschließlich unter
`/var/lib/open-dachs-manager-updater` (root:root, 0700). Vor dem Dienststopp
erzeugt er eine konsistente SQLite-Onlinekopie und eine begrenzte Root-Custody
der nicht in SQLite liegenden veränderlichen Produktdaten (Benutzer,
Einstellungen, Secrets und kritische lokale Archive). Jede Phase wird atomar
und mit `fsync` in globalem Status und anfragebezogener Evidenz gespeichert.
Bei unverändertem DB-Schema wird bei einem Code-Rollback **nicht** die
Datenbank zurückgespielt; dadurch gehen seit dem Snapshot eingegangene Daten
nicht verloren. Ein Schemawechsel ist für automatische Releases gesperrt und
benötigt einen eigenen geprüften Migrations-/Rollbackplan.

Die Release-Kompatibilität stammt aus `release-metadata.json`; der Workflow
verlangt für die konkrete Paketversion einen exakten Eintrag. Damit können
zukünftige Tags nicht still die heutigen Schema-/Rollback-Aussagen erben. Der
Release-Job benennt `release-approval` als geschützte GitHub-Environment; der
Repository-Administrator muss die erforderlichen Reviewer/Branchregeln in
GitHub tatsächlich konfigurieren. Alle verwendeten Actions sind auf die im
Workflow kommentierten unveränderlichen Upstream-Commit-SHAs gepinnt.
