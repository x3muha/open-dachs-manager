# Installation

## Unterstützte Umgebung

- Debian, Raspberry Pi OS oder eine vergleichbare Linux-Distribution mit systemd
- optischer USB-Lesekopf am Dachs-MSR2-Regler
- Root-Rechte über `sudo`
- TCP-Port `8084`, erreichbar nur aus dem vorgesehenen lokalen Netzwerk

Der Manager kommuniziert ausschließlich über den seriellen Adapter mit dem
Regler. Die Anlagenverbindung wird nicht auf TCP/IP umgestellt.

## 1. Lesekopf anschließen

Getestet ist die Anbindung über einen optischen USB-Lesekopf. Als günstiges
Beispiel für einen Lesekopf nach DIN EN 62056-21 beziehungsweise IEC
62056-21 eignet sich der
[ELV USB-IEC, Artikel 158713](https://de.elv.com/p/elv-lesekopf-mit-usb-schnittstelle-fuer-digitale-zaehler-usb-iec-P158713/).
Andere kompatible USB-Leseköpfe sollten ebenfalls funktionieren.

Die Normangabe beschreibt die optische Schnittstelle des Lesekopfs. Die
MSR2-Nutzdaten verwenden ein eigenes binäres Protokoll. Ein klassischer
RS232-Lesekopf könnte mit passendem Linux-Gerätepfad funktionieren, ist aber
noch nicht praktisch getestet.

Nach dem Anschließen den stabilen Gerätepfad prüfen:

```bash
ls -la /dev/ttyUSB* /dev/serial/by-id/* 2>/dev/null
```

`/dev/serial/by-id/...` ist `/dev/ttyUSB0` vorzuziehen, weil sich die
ttyUSB-Nummer nach dem Umstecken anderer USB-Geräte ändern kann.

## 2. Repository klonen

```bash
git clone https://github.com/x3muha/open-dachs-manager.git
cd open-dachs-manager
```

## 3. Installieren

Im Normalfall genügt:

```bash
sudo ./install.sh
```

Auf Debian und Raspberry Pi OS erkennt der Installer fehlende Systempakete
und installiert `python3`, `python3-venv`, `python3-pip`, `git` und
`ca-certificates` automatisch über `apt`. Danach erstellt er die isolierte
Python-Umgebung und installiert die benötigten Python-Pakete. Eine manuelle
Python-Vorbereitung ist nicht erforderlich.

Verfügbare Optionen:

```text
--serial-port PFAD  serielles Gerät ausdrücklich wählen
--web-host ADRESSE  Bind-Adresse, Standard 0.0.0.0
--web-port PORT     HTTP-Port, Standard 8084
--base-path PFAD    URL-Präfix, zum Beispiel /dachs
--service-codes-file PFAD
                    lokale Diagnoseergänzung mit Ursachen/Maßnahmen installieren
--no-start          installieren, Dienste aber nicht starten
--replace-legacy    aktive dachs-v3-Dienste ablösen und Daten übernehmen
```

Beispiel mit ausdrücklich gewähltem Adapter:

```bash
sudo ./install.sh \
  --serial-port /dev/serial/by-id/usb-FTDI_USB__-__Serial-if00-port0
```

Der Installer legt folgende Komponenten an:

| Pfad | Zweck |
|---|---|
| `/opt/open-dachs-manager/venv` | isolierte Python-Installation |
| `/etc/open-dachs-manager/open-dachs-manager.env` | von root verwaltete Konfiguration |
| `/var/lib/open-dachs-manager` | Benutzer, SQLite-Historie und Berichte |
| `/var/lib/open-dachs-manager/maintenance_settings.json` | persistenter Wartungs-Testmodus nach der ersten Änderung |
| `/var/lib/open-dachs-manager/dashboard_settings.json` | gemeinsame Auswahl und Reihenfolge der Übersichtskacheln |
| `/var/lib/open-dachs-manager/soot_filter_settings.json` | lokale Temperaturkennlinie der geschätzten Rußfilteranzeige |
| `/var/lib/open-dachs-manager/servicecodes_de.properties` | optionale lokale Diagnoseergänzung für Ursachen und Maßnahmen |
| `/run/open-dachs-manager/serial.sock` | Socket des gemeinsamen Serialworkers |
| `/etc/systemd/system/open-dachs-manager-*.service` | systemd-Dienste |

Die Dienste laufen als unprivilegierter Systembenutzer `open-dachs`. Der
Installer fügt diesen Benutzer der erkannten Gerätegruppe des Adapters hinzu.

## Integrierter Fehlerkatalog und optionale Diagnosedetails

Der Manager bringt seine deutschen Fehler-Klartexte in
`fault_catalog_de.json` mit. Die Datei verwendet das eigene Schema
`open-dachs-manager/fault-catalog/v1`; es wird keine komplette XML- oder
Properties-Datei des alten Systems in das Softwarepaket übernommen. Dadurch
erscheint beispielsweise Code 163 direkt als `SC 163 · Leistung zu klein`.

Wenn eine rechtmäßig vorhandene `Servicecodes_de.properties` aus der lokalen
Dachs-Web-Installation verfügbar ist, kann der Installer daraus zusätzlich
Ursachen und Maßnahmen als lokale Datendetails einbinden:

```bash
sudo ./install.sh \
  --service-codes-file /pfad/zu/Servicecodes_de.properties
```

Die Datei wird mit eingeschränkten Rechten nach
`/var/lib/open-dachs-manager/servicecodes_de.properties` kopiert und bei
Updates weiterverwendet. Sie gehört bewusst nicht zum öffentlichen
Open-Source-Paket. Die Code-Klartexte funktionieren auch ohne diese Datei.

## Reverse Proxy und Subpfad

Direktbetrieb bleibt standardmäßig unter `http://<pi>:8084/`. Soll ein Proxy
den Präfix an das Backend weiterreichen, wird er bei der Installation gesetzt:

```bash
sudo ./install.sh --base-path /dachs
curl http://127.0.0.1:8084/dachs/healthz
```

Öffentliche Beispiel-URL: `https://tools.example/dachs/`; intern lauscht der
Dienst weiter auf `0.0.0.0:8084`. Entfernt der Proxy den Präfix bereits vor
dem Weiterleiten, bleibt der Base Path leer. Der Healthcheck lautet dann
weiter `/healthz`.

## 4. Erste Anmeldung

Bei einem neuen Datenverzeichnis erzeugt der Webdienst zufällige Admin- und
Gastpasswörter. Der Installer zeigt sie einmalig an. Solange das erste
Startprotokoll noch vorhanden ist, lassen sie sich auch dort ablesen:

```bash
sudo journalctl -u open-dachs-manager-web.service | \
  sed -n '/Web-Erstzugang/,+2p'
```

Das anfängliche Admin-Passwort anschließend in den Web-Einstellungen ändern.
Der Admin kann dort außerdem das Gastpasswort festlegen. Das Gastkonto ist
nur lesend und kann sein Passwort nicht selbst ändern. Eine Änderung des
Gastpassworts beendet alle offenen Gastsitzungen. Im Datenverzeichnis werden
Passwort-Hashes und keine Klartextpasswörter gespeichert.

Der Wartungsabschluss startet im Testmodus. Unter `Einstellungen → Testmodus`
kann ein Admin später zwischen rein lokalem Testabschluss und kontrolliertem
Reglerabschluss umschalten. Die Auswahl bleibt bei Updates und Neustarts
erhalten; das Umschalten selbst schreibt nicht in den Regler.

## Migration von der Entwicklungsinstallation

```bash
sudo ./install.sh --replace-legacy
```

Der Installer stoppt und deaktiviert `dachs-v3-web.service` sowie
`dachs-v3-serial-worker.service`. Danach wird `/var/lib/dachs-v3-web` nur dann
in das neue Datenverzeichnis kopiert, wenn dieses noch leer ist. Die alten
Dateien bleiben als Rückfallquelle erhalten.

## Aktualisieren

```bash
cd open-dachs-manager
git pull --ff-only
sudo ./install.sh
```

Lokale Datenbank und Benutzerdatei bleiben erhalten. Die beiden Dienste werden
erst neu gestartet, nachdem Paket und Konfiguration vollständig installiert
wurden.

## Deinstallieren

Programm entfernen, Messwerte, Benutzer und Berichte aber behalten:

```bash
sudo ./uninstall.sh
```

Zusätzlich alle lokalen Anwendungsdaten löschen:

```bash
sudo ./uninstall.sh --purge-data
```

Das vollständige Löschen ist ohne vorherige Sicherung von
`/var/lib/open-dachs-manager` nicht rückgängig zu machen.
