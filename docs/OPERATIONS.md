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
Wartungsberichte und Audit-Informationen.

Regler-Backup über den Serialworker:

```bash
open-dachs backup create \
  --all-blocks \
  --output open-dachs-controller-backup.json
```

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
