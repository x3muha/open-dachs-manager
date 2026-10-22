# Sicherheitsmodell

Open Dachs Manager kommuniziert mit einer realen Heizungs- und
BHKW-Steuerung. Ein falscher Schreibwert kann das Betriebsverhalten verändern.
Lese- und Schreibpfad sind deshalb bewusst unterschiedlich aufgebaut.

## Lesepfad

- der Serialworker ist alleiniger Besitzer des Adapters
- alle Clients verwenden FIFO-Sitzungen
- Frames mit ungültiger CRC werden verworfen
- Blocknummern außerhalb des Ein-Byte-Protokollbereichs werden abgewiesen und
  niemals still auf das niedrige Byte gekürzt

## Schreibpfad

Jeder echte Schreibvorgang erfordert:

```text
aktuellen Block lesen
→ Feld, Datentyp und Block validieren
→ authentifizieren / PW4 berechnen
→ Schreibfreigabe ausdrücklich aktivieren
→ vollständigen Block schreiben
→ positive Antwort erhalten
→ bytegenau zurücklesen
→ Ergebnis im Audit protokollieren
```

Dry-Run bleibt der Standard. Ein vorbereiteter Wert beweist nicht, dass eine
Änderung technisch unbedenklich ist. Vorher die Bedeutung anhand der richtigen
Geräteunterlagen prüfen und ein Backup erstellen.

## Authentifizierungsstufen

Das Telegramm besitzt ein Byte für den Auth-Level. Die Original-Java-
Oberfläche benennt Rollen 1 bis 4; die älteren V2-Schreib- und Backup-Werkzeuge
verwenden zusätzlich Level 5. Der getestete Regler spiegelt auch höhere
Bytewerte zurück; dieses Echo ist kein Beleg für weitere Rollen. CLI, TUI, Web
und API akzeptieren deshalb ausschließlich echte Integer von 1 bis 5, prüfen
vor jedem Auth-Lesevorgang und verlangen eine exakt passende Antwort.
Boolesche, textuelle, gebrochene und außerhalb liegende Werte werden nicht
still konvertiert.

## Backup-Wiederherstellung

Ein vollständiges aktuelles Sicherungsabbild umfasst 42 Ziele: die 36
adressierbaren Rohblöcke der Regler-CPU 0 sowie Block 16, 20 und 21 der
Netzschutz-CPU 1 und der Netzschutz-CPU 2. Wiederherstellbar bleiben davon nur
die 36 geprüften Konfigurationsziele auf CPU 0; alle Netzschutzblöcke bleiben
read/backup-only. Die Ziele werden immer als eindeutiges Paar aus CPU und
Blocknummer behandelt. Vor
dem Import werden Schema, Packrevision, Reglerkennung, eindeutige CPU-/Blockziele,
Payloadlängen und SHA-256-Prüfsummen geprüft. Alte Abbilder ohne Prüfsummen sind
nur für den Dry-Run zugelassen.

Die Wiederherstellung startet mit einer vollständigen Lesevorprüfung aller
ausgewählten Zielblöcke. Bytegleiche Blöcke werden ohne Authentifizierung und
ohne Schreibtelegramm übersprungen. Abweichende Blöcke erfordern eine
ausdrückliche Hardwarefreigabe, den exakten Bestätigungstext, eine einmalige
Authentifizierung der exklusiven Sitzung, eine erneute Stabilitätsprüfung,
positive Bestätigung und vollständige bytegenaue Rückleseprüfung. Fehler werden
je Ziel protokolliert; weitere ausgewählte Blöcke werden trotzdem versucht.
Softwareversionsfelder der Zielplatine werden nicht aus dem Abbild übertragen.

Vor der Authentifizierung eines abweichenden Live-Ablaufs wird der frisch
gelesene Ausgangszustand als SHA-256-gebundenes JSON-Sicherheitsabbild atomar im
geschützten Unterverzeichnis `restore-preimages` des lokalen Datenverzeichnisses
gespeichert. Damit bleibt auch nach einem Prozess- oder Stromausfall ein
bytegenaues Vorzustandsabbild erhalten. Dry-Run und bytegleicher Live-Vergleich
benötigen und erzeugen dieses lokale Sicherheitsabbild nicht.

SHA-256 schützt hier die Inhaltsbindung und erkennt unbeabsichtigte Änderungen;
es ist keine kryptografische Signatur und bestätigt nicht die Herkunft einer
Datei. Fehlende oder unpassende Packdaten sperren jede echte
Wiederherstellung. Die Seriennummer des Abbilds wird absichtlich nicht mit der
Zielregelung verglichen, weil der Ablauf auch für den Austausch einer Regelung
vorgesehen ist. Sie bleibt dennoch als hashgebundene Herkunftsinformation im
Abbild erhalten.

Der Ablauf ist nicht transaktional: Bereits erfolgreich wiederhergestellte
Blöcke werden bei einem späteren Fehler nicht automatisch zurückgerollt.
Dynamische Zustände, Zähler und Zeitwerte können im Abbild veraltet sein. Das
Abbild kann außerdem vertrauliche Anlagen-, Adress- und Kontaktdaten enthalten;
PW4 wird dagegen nie darin gespeichert. Nach einem gesendeten Schreibtelegramm
ohne eindeutige positive Bestätigung und vollständige Rückleseprüfung gilt der
Zielzustand als unklar; vor einem weiteren Versuch ist der Block frisch zu
lesen und fachlich zu prüfen.

## Geschütztes Wartungsbackup-Archiv

Jeder Wartungsstart verwendet einen einzigen exklusiven Lesevorgang für alle
42 Sicherungsziele. Erst wenn daraus ein vollständiges
`dachs-msr2-backup/v3`-Abbild entstanden ist, dessen Abbild- und
Datei-SHA-256 geprüft wurden und dessen Datei atomar gespeichert ist, darf der
zugehörige Wartungsbericht angelegt werden. Der Anlagenstand des Berichts wird
aus denselben CPU-0-Antworten gebildet; eine zweite Blockrunde könnte bereits
einen anderen Reglerzeitpunkt abbilden und wird deshalb vermieden.

`/var/lib/open-dachs-manager/backup-archive` gehört dem Dienstbenutzer und hat
Modus `0700`; Dateien haben `0600`. Der Installer öffnet Eltern- und
Archivverzeichnis mit `O_DIRECTORY|O_NOFOLLOW` und setzt Eigentümer sowie
Rechte nur über den geöffneten Dateideskriptor. Ein vorgelegter symbolischer
Link oder ein anderes Nicht-Verzeichnis führt zum Abbruch, statt als root ein
fremdes Ziel umzueignen oder dessen Rechte zu ändern.

Listen- und Download-API sind nur für Administratoren erreichbar. Es gibt
bewusst keinen Archiv-Löschweg. Das Löschen eines Wartungsberichts lässt sein
Sicherungsabbild bestehen. Vor der Übernahme in die Wiederherstellung werden
Download, Abbildprüfsumme, Packrevision, Gerätebindung, 42/42 erfolgreiche
Sicherungsziele und exakt 36 Restore-Ziele erneut geprüft. Geprüfte ältere
38/38-Wartungsarchive bleiben unter ihrem unveränderten Altvertrag nutzbar.
Danach bleibt die Zielauswahl leer und der schreibfreie Probelauf aktiv.

Ein echter Wartungsabschluss wird vor dem ersten möglichen Reglerwrite als
`completing` gespeichert. Dieser Zustand ist gegen Wiederholung und Löschen
gesperrt. Kann ein gesendeter Auftrag nicht eindeutig per ACK und Readback
bestätigt werden, bleibt der Bericht als `uncertain` gesperrt und verlangt eine
fachliche Prüfung; die Oberfläche bietet keinen Wiederholen- oder Löschknopf.

## Netzschutz-CPUs

CPU 0 (Regler), CPU 1 und CPU 2 (Netzüberwachung) haben voneinander getrennte
Blockräume. Die live bestätigten Blöcke 16, 20 und 21 von CPU 1 und CPU 2 sind
wegen ihrer Netzschutzbedeutung rot markiert, damit sie nicht mit normalen
Reglerwerten verwechselt werden.

Alle Netzschutzblöcke sind serverseitig read/backup-only. B16 wurde nach
fachlicher Ablehnung `7F 11 01` und unveränderten Rücklesungen aus Editor, API
und Restore entfernt. Block 20 bleibt nach der physischen Abnahme nur lesbar: Beide
U-3.050-CPUs gewährten Auth 5 und quittierten den nominalen Dienst 21 positiv,
ignorierten den minimalen Testwert aber in vier Rücklesungen.

```text
Lesen → kodieren → authentifizieren → bytegenauer CAS
→ Dienst 21 → positives ACK → exakte Vollblock-Rückleseprüfung → Audit
```

Ein positiver ACK ohne passenden Readback gilt ausdrücklich als Fehler. B20
enthält Netzschutzgrenzen und Abschaltzeiten und wird deshalb weder editiert
noch restauriert oder durch einen vermeintlichen Erfolg stillschweigend
bestätigt.

Block 21 besitzt keinen Schreibdienst und bleibt als laufender Messwertblock
serverseitig strikt nur lesbar. Das Öffnen oder Aktualisieren einer roten Seite
schreibt weiterhin nichts. Block 16, 20 und 21 werden im 42-Ziele-Abbild gelesen,
auf exakte Länge und Dekodierbarkeit geprüft und gehasht, bleiben aber außerhalb
der 36 Restore-Ziele. Block 20 ist nach der physischen Abnahme nicht bestätigt schreibbar;
flüchtige Messwerte aus Block 21 dürfen niemals als Konfiguration
zurückgeschrieben werden.

## 250-h-Diagnose

Die 250-h-Auswertung ist ein reiner Offline-Lesepfad über bereits gespeicherte
SQLite-Snapshots. API, Browser und Hintergrundworker öffnen dafür keine
serielle Sitzung und führen weder Authentifizierung noch Controllerwrites aus.
Cachedatei und Fehlerzustand sind lokal und privat; eine fehlgeschlagene
Neuberechnung ersetzt niemals das letzte gültige Ergebnis. Fehlende Daten und
nicht belegbare Fehlstarts werden sichtbar als Einschränkung geführt und nicht
zu vermeintlichen Messwerten ergänzt.

## Webzugriff

Der eingebaute Webserver liefert HTTP und kein TLS. Er gehört in ein
vertrauenswürdiges lokales Netzwerk oder hinter einen korrekt eingerichteten
HTTPS-Reverse-Proxy. Den Standardport nicht direkt ins Internet weiterleiten.

Anfangspasswörter sofort ändern und `/var/lib/open-dachs-manager` als
vertrauliche Betriebsinformation schützen.

### Optionaler lokaler Bedienplatz

Der Login-Override wird niemals aus Quell-IP- oder Proxy-Headern abgeleitet.
Er existiert ausschließlich als Eigenschaft einer zweiten HTTP-Serverinstanz,
deren Socket hart an `127.0.0.1` gebunden ist. Der normale Listener kennt
diesen Principal nicht und bleibt auch bei gefälschten `Forwarded`,
`X-Forwarded-For` oder `X-Real-IP` loginpflichtig.

HTTP-Cookies sind nicht portgebunden. Der lokale Listener erzeugt und
akzeptiert deshalb keinen lokalen Admin-Cookie, sondern verwendet den
cookie-losen Principal `localhost-override` mit der konfigurierten Rolle. Ein
normaler Admin-Cookie kann eine lokale Gastrolle nicht hochstufen. Exakter
`Host`, `Origin`, Same-Origin-Request-Marker, Fetch-Metadaten, CSP und
Frame-Schutz sperren DNS-Rebinding, Cross-Site-Requests und normale
Proxyweiterleitungen. `/api/v1/*` bleibt unabhängig davon ausschließlich mit
passendem Bearer-Token zugänglich.

Ein Reverse Proxy auf demselben Rechner muss ausschließlich den normalen
Web-Port als Upstream verwenden. Würde er den Loopback-Port absichtlich
weiterleiten und Host, Origin sowie Proxyspuren passend umschreiben, könnte der
Webdienst diesen Proxy technisch nicht von einem lokalen Browser
unterscheiden. Diese Upstream-Konfiguration ist daher ausdrücklich unzulässig.

Der Override ersetzt nur die Webanmeldung. Ein lokaler Admin muss weiterhin
alle getrennten Schutzstufen eines Reglerwrites erfüllen: explizite
Schreibfreigabe, fachliche Validierung, MSR2-Authentifizierung/PW4, stabiler
Ausgangszustand, positives ACK, Readback und Audit. Im Audit erscheint er als
`localhost-override`, niemals als regulärer Passwortbenutzer `admin`.

API-Tokens werden nur beim Erstellen vollständig angezeigt und danach nur als
SHA-256-Hash gespeichert. Berechtigungen für Lesen, Historie und Schreiben
sind getrennt. Ein Schreibrecht im Token allein genügt nicht: API-Schreiben
bleibt standardmäßig global gesperrt und muss von einem Administrator bewusst
aktiviert werden. Die API akzeptiert keine fertigen Rohtelegramme oder
externen PW4-Werte. Schreibaktionen laufen durch denselben validierten
Read-/Auth-/Write-/ACK-/Readback-/Audit-Pfad wie die Weboberfläche und
benötigen eine eindeutige `request_id`. Diese wird vor dem Serialzugriff
atomar reserviert und an den konkreten Aktionsinhalt gebunden. Bei der
Generatornennleistung wird außerdem die kraftstoffabhängige Originalgrenze
vor Authentifizierung und Write geprüft.

Tokenwerte gehören wie Passwörter behandelt. Sie dürfen nicht in EDOMI-Logs,
URLs, Browser-Lesezeichen oder öffentliche Fehlerberichte gelangen.

## Reifegrad

Version 1.8.0 basiert auf dem ersten stabilen öffentlichen Stand und wird ohne
Garantie bereitgestellt. Gerätevarianten und Packstände können abweichen.
Leseergebnisse und Feldlagen am tatsächlichen Zielgerät verifizieren, bevor
Schreibvorgänge freigegeben werden.
