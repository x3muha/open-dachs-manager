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

## Backup-Wiederherstellung

Ein vollständiges Sicherungsabbild umfasst die 36 adressierbaren Rohblöcke der
Regler-CPU 0 sowie Block 16 der Netzschutz-CPU 1 und der Netzschutz-CPU 2. Die
Ziele werden immer als eindeutiges Paar aus CPU und Blocknummer behandelt. Vor
dem Import werden Schema, Packrevision, Reglerkennung, eindeutige CPU-/Blockziele,
Payloadlängen und SHA-256-Prüfsummen geprüft. Alte Abbilder ohne Prüfsummen sind
nur für den Dry-Run zugelassen.

Die Wiederherstellung startet mit einer vollständigen Lesevorprüfung aller
ausgewählten Zielblöcke. Bytegleiche Blöcke werden ohne Authentifizierung und
ohne Schreibtelegramm übersprungen. Abweichende Blöcke erfordern eine
ausdrückliche Hardwarefreigabe, den exakten Bestätigungstext, eine einmalige
Authentifizierung der exklusiven Sitzung, eine erneute Stabilitätsprüfung,
positive Bestätigung und vollständige bytegenaue Rückleseprüfung. Nach dem
ersten Fehler werden keine weiteren Blöcke versucht.

Vor der Authentifizierung eines abweichenden Live-Ablaufs wird der frisch
gelesene Ausgangszustand als SHA-256-gebundenes JSON-Sicherheitsabbild atomar im
geschützten Unterverzeichnis `restore-preimages` des lokalen Datenverzeichnisses
gespeichert. Damit bleibt auch nach einem Prozess- oder Stromausfall ein
bytegenaues Vorzustandsabbild erhalten. Dry-Run und bytegleicher Live-Vergleich
benötigen und erzeugen dieses lokale Sicherheitsabbild nicht.

SHA-256 schützt hier die Inhaltsbindung und erkennt unbeabsichtigte Änderungen;
es ist keine kryptografische Signatur und bestätigt nicht die Herkunft einer
Datei. Fehlende Packdaten sperren deshalb ebenso wie ein Pack- oder
Regleridentitätskonflikt jede echte Wiederherstellung.

Der Ablauf ist nicht transaktional: Bereits erfolgreich wiederhergestellte
Blöcke werden bei einem späteren Fehler nicht automatisch zurückgerollt.
Dynamische Zustände, Zähler und Zeitwerte können im Abbild veraltet sein. Das
Abbild kann außerdem vertrauliche Anlagen-, Adress- und Kontaktdaten enthalten;
PW4 wird dagegen nie darin gespeichert. Nach einem gesendeten Schreibtelegramm
ohne eindeutige positive Bestätigung und vollständige Rückleseprüfung gilt der
Zielzustand als unklar; vor einem weiteren Versuch ist der Block frisch zu
lesen und fachlich zu prüfen.

## Netzschutz-CPUs

CPU 0 (Regler), CPU 1 und CPU 2 (Netzüberwachung) haben voneinander getrennte
Blockräume. Die live bestätigten Blöcke 16, 20 und 21 von CPU 1 und CPU 2 sind
wegen ihrer Netzschutzbedeutung rot markiert, damit sie nicht mit normalen
Reglerwerten verwechselt werden.

Legacy-Block 16 und Schutzkonfigurationsblock 20 sind über den globalen
Admin-/Auth-/Schreib-Haken veränderbar. Je CPU sind das 18 plus 39 Felder,
insgesamt also 114 eindeutig adressierte schreibbare Feldinstanzen. Für
Block 20 belegt die originale Layout-4-Datenzuordnung den
Vollblock-Schreibdienst 21. Dort gilt zwingend:

```text
Lesen → kodieren → authentifizieren → bytegenauer CAS
→ Dienst 21 → positives ACK → exakte Vollblock-Rückleseprüfung → Audit
```

Nicht eindeutig umkehrbare profilabhängige Anzeigen werden bei einer normalen
Eingabe abgewiesen. `raw:<Rohwert>` ist ein ausdrücklicher Expertenweg, der
Skalierungs- und Auswahllogik umgeht. Feldbreite, Wertebereich,
Authentifizierung, CAS, ACK und exakte Rückleseprüfung bleiben wirksam. Die
Kodierung und schreibfreie Probeläufe sind geprüft; ein physischer Schreibtest
von Block 20 an der Anlage steht noch aus. Das ist besonders relevant, weil
dieser Block unmittelbar Netzschutzgrenzen und Abschaltzeiten enthält.

Block 21 besitzt keinen Schreibdienst und bleibt als laufender Messwertblock
serverseitig strikt nur lesbar. Das Öffnen oder Aktualisieren einer roten Seite
schreibt weiterhin nichts. Block 20 bleibt bis zu seiner realen Abnahme
ebenfalls außerhalb der restaurierbaren Backupziele; der Sicherungsumfang
bleibt bei 38 Zielen. Flüchtige Messwerte aus Block 21 dürfen niemals als
Konfiguration zurückgeschrieben werden.

## Webzugriff

Der eingebaute Webserver liefert HTTP und kein TLS. Er gehört in ein
vertrauenswürdiges lokales Netzwerk oder hinter einen korrekt eingerichteten
HTTPS-Reverse-Proxy. Den Standardport nicht direkt ins Internet weiterleiten.

Anfangspasswörter sofort ändern und `/var/lib/open-dachs-manager` als
vertrauliche Betriebsinformation schützen.

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

Version 1.4.0 basiert auf dem ersten stabilen öffentlichen Stand und wird ohne
Garantie bereitgestellt. Gerätevarianten und Packstände können abweichen.
Leseergebnisse und Feldlagen am tatsächlichen Zielgerät verifizieren, bevor
Schreibvorgänge freigegeben werden.
