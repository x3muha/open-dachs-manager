# Betrieb und Fehlersuche

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

Block 22 und 24 werden in einem Zielabstand von 0,75 Sekunden gelesen. Der
tatsächliche Abstand kann nur dann größer werden, wenn die seriellen Antworten
zusammen länger dauern. Block 20 und die weiteren langsam veränderlichen
Blöcke werden alle zehn Sekunden ergänzt.

Dauerhaft in SQLite landen weiterhin 21 ausgewählte Betriebs- und Messwerte
aus Block 22 und 24 gemeinsam in genau einer kompakten Snapshot-Zeile je
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
mit exakt 38 Restore-Zielen ausweisen. Geprüfte ältere Archive behalten ihren
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
