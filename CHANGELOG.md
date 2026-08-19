# Änderungsverlauf

## V3 1.5.2 - 2026-08-18

- die Stabilitätsprüfung vor dem Schreiben von Block 50 behandelt ausschließlich
  die belegte Systemzeit an Byte 36 bis 39 als laufenden Wert; Lage, Breite,
  Blocknummer, CPU und Nutzdatenlänge müssen dafür exakt zur Felddefinition passen
- der gewünschte Leistungswert wird auf den unmittelbar zuvor frisch gelesenen
  Block gesetzt, sodass niemals eine veraltete Systemzeit zurückgeschrieben wird;
  jede gleichzeitige Änderung an einem anderen Bit bricht den Vorgang weiterhin ab
- Zielwert, positive Reglerbestätigung und Rückleseprüfung bleiben zwingend;
  falsche Bestätigungen, abweichende Zielwerte und veränderte Nachbarfelder werden
  weiterhin ohne stillschweigende Übernahme als Fehler protokolliert

## V3 1.5.1 - 2026-08-18

- der Leistungs-Sollwert im Hauptmenü verwendet fest Auth-Level 4 und lässt
  PW4 beim Schreiben serverseitig aus frisch gelesener Seriennummer und
  Betriebsstundenzahl berechnen; ein vorheriger Wechsel in die Einstellungen
  ist nicht mehr nötig
- die neue feste Übersichtskachel „Betriebsstunden je Start“ berechnet sich
  quellentreu aus den rohen Betriebssekunden und Starts desselben
  Block-22-Telegramms und zeigt das Ergebnis als `Bh/Start`
- bei null Starts, ungültigen Zählern oder zeitlich nicht zusammengehörigen
  Quelldaten zeigt die Kennzahl bewusst keinen Wert; zusätzliche serielle
  Lesezugriffe entstehen für die Berechnung nicht

## V3 1.5.0 - 2026-08-18

- jeder Wartungsstart erzeugt automatisch ein vollständiges Sicherungsabbild
  aller 38 freigegebenen CPU-/Blockziele; ein unvollständiges oder nicht
  prüfbares Abbild lässt keinen neuen Wartungsbericht entstehen
- Sicherungsabbilder werden atomar und mit Rechten `0600` im nur für den
  Dienstbenutzer zugänglichen Verzeichnis `backup-archive` abgelegt; Metadaten,
  Dateiinhalt und Abbild sind per SHA-256 gebunden
- der nur für Administratoren sichtbare Backup-Bereich zeigt Zeitpunkt,
  Ersteller, Herkunft, Wartungsbericht, Größe, Packrevision, Integrität und das
  Ergebnis 38/38; Abbilder können unverändert heruntergeladen werden
- „Für Wiederherstellung laden“ lädt die archivierte Datei erneut, prüft sie
  noch einmal über die bestehende Abbildprüfung und übernimmt sie mit leerer
  Zielauswahl sowie schreibfreiem Probelauf als sicherem Standard
- Wartungsansicht und Bericht nennen Backup-ID, Zeitpunkt, 38/38-Ergebnis und
  SHA-256; das Löschen eines Wartungsberichts lässt das getrennte
  Sicherungsabbild ausdrücklich erhalten
- die Zwischenzustände `Abschluss läuft` und `Zielzustand unklar` verhindern
  erneutes Abschließen und Löschen; ein unklarer Reglerzustand muss anhand von
  Bestätigung, Rückleseprüfung und Prüfprotokoll fachlich geklärt werden

## V3 1.4.0 - 2026-08-17

- Block 16 mit 18 Feldern und Block 20 mit 39 Feldern sind auf beiden
  Netzüberwachungs-CPUs schreibbar; damit stehen 114 eindeutig einer CPU und
  einem Block zugeordnete Feldinstanzen zur Verfügung
- die originale Layout-4-Datenzuordnung belegt für Block 20 den
  Vollblock-Schreibdienst 21; der geschützte Ablauf lautet Lesen, kodieren,
  authentifizieren, Ausgangszustand bytegenau vergleichen, Dienst 21 ausführen,
  positive Bestätigung prüfen und den vollständigen Block exakt zurücklesen
- profilabhängige und andere nicht eindeutig umkehrbare Anzeigen werden nicht
  geraten; für eine ausdrückliche Experteneingabe steht `raw:<Rohwert>` bereit
- Block 21 bleibt als laufender Messwertblock ohne Schreibdienst strikt nur
  lesbar; Block 20 bleibt bis zu einer realen Abnahme außerhalb von Sicherung
  und Wiederherstellung, deren Umfang unverändert 38 geprüfte Ziele umfasst
- Kodierung, Schutzprüfungen und schreibfreie Probeläufe wurden geprüft; ein
  physischer Schreibvorgang auf Block 20 wurde an der Anlage noch nicht
  ausgeführt
- die geprüften Standarddefinitionen enthalten keine weiteren Datenblöcke der
  Netzüberwachungs-CPUs; die zusätzlich live gelesenen Diagnosedienste 17 und
  18 bleiben mangels belegter Feldstruktur reine Rohdaten

## V3 1.3.0 - 2026-08-17

- beide Netzüberwachungs-CPUs zeigen neben dem bekannten Legacy-Block 16 jetzt
  auch die live bestätigten Layout-4-Blöcke 20 und 21 in getrennten roten
  Registerkarten
- Block 20 dekodiert 39 Schutzparameter aus 59 Byte: Schutzprofil,
  zweistufige Spannungs- und Frequenzgrenzen, profilabhängige Abschaltzeiten,
  10-Minuten-Spannungsgrenze, Frequenzreduktion, Impedanz- und LOM-Status
- Block 21 dekodiert 28 aktuelle Werte aus 56 Byte: dreiphasige Spannung, Strom und
  Frequenz, Impedanzen, Wirkleistung, Phasenlage, Cosinus Phi und
  Kalibrierfaktoren; signierte und Little-Endian-Werte werden quellentreu
  behandelt
- die neuen Blöcke sind bewusst nur lesbar: Herstellerdefinition und
  Alt-Oberfläche belegen ausschließlich Lesezugriff, daher existiert weder im
  Browser noch serverseitig ein Schreibpfad für Block 20 oder 21
- der vorhandene, ausdrücklich freigegebene Block-16-Schreibablauf bleibt
  unverändert; blockbezogene Lese-URLs verhindern eine Verwechslung zwischen
  den drei CPU-Blockräumen
- Backup und Wiederherstellung bleiben bei den geprüften 38 Zielen; Block 20
  und die flüchtigen Messwerte aus Block 21 werden nicht stillschweigend als
  restaurierbare Rohblöcke aufgenommen

## V3 1.2.1 - 2026-08-16

- die Backup-Auswahl umfasst jetzt neben den 36 Reglerblöcken auch die beiden
  getrennten Netzschutz-Ziele `CPU 1 · Block 16` und `CPU 2 · Block 16`
- Sicherungsabbilder und Prüfsummen unterscheiden Ziele eindeutig über das Paar
  aus CPU und Blocknummer; gleichnamige Blocknummern verschiedener CPUs können
  dadurch weder verwechselt noch als Duplikat zusammengefasst werden
- Prüfung, Dry-Run, Sicherheitsabbild, ACK, vollständige bytegenaue
  Rückleseprüfung und Prüfprotokoll gelten unverändert auch für ausgewählte
  Netzschutzblöcke
- bestehende CPU-0-Abbilder aus Version 1.2.0 bleiben einschließlich ihrer
  bereits gespeicherten SHA-256-Prüfsumme importierbar
- die reale Abnahme bleibt schreibfrei: Netzschutz wird nur gelesen und über
  Dry-Run beziehungsweise einen bytegleichen Überspringvorgang geprüft
- fehlende oder unpassende Packdaten sperren eine echte Wiederherstellung jetzt
  strikt; serielle Datenantworten werden zusätzlich an Quell-CPU und Zieladresse
  gebunden, damit kein verspätetes Telegramm der anderen Netzschutz-CPU gilt
- Sicherheitsabbilder werden nach dem atomaren Umbenennen zusätzlich über das
  Elternverzeichnis auf den Datenträger synchronisiert; ihre SHA-256-Prüfsumme
  schützt die Inhaltsbindung, ist jedoch ausdrücklich keine kryptografische
  Signatur

## V3 1.2.0 - 2026-08-16

- eigener Hauptbereich **Backup** zum Sichern aller 36 adressierbaren
  Reglerblöcke oder einer frei gewählten Teilmenge in einem JSON-Abbild
- Wiederherstellungsdateien werden zunächst rein offline eingelesen und auf
  Schema, Packstand, Reglerkennung, eindeutige Blocknummern, Payloadlängen sowie
  SHA-256-Prüfsummen geprüft; erst danach wird die Blockauswahl freigeschaltet
- Dry-Run bleibt der Standard und führt ausschließlich Lesevergleiche aus;
  echte Wiederherstellung verlangt Administratorrolle, Hardwarefreigabe,
  Authentifizierung und den exakten Bestätigungstext
- bytegleiche Blöcke werden ohne Authentifizierung und ohne Schreibtelegramm
  übersprungen; geänderte Rohblöcke benötigen stabile Ausgangsdaten, eine
  positive Bestätigung (ACK) und eine vollständige bytegenaue Rückleseprüfung
- vollständiger Mehrblock-Vorlauf und anschließende Ausführung innerhalb einer
  exklusiven Serialworker-Sitzung; Block 20 und 22 werden wegen der PW4-Grundlage
  zuletzt bearbeitet und nach dem ersten Fehler folgen keine weiteren
  Schreibversuche
- vor der Authentifizierung eines tatsächlich abweichenden Live-Ablaufs wird
  der frisch gelesene Ausgangszustand als SHA-256-gebundenes Sicherheitsabbild mit
  lokalen Rechten `0600` atomar gespeichert
- altes `dachs-msr2-backup/v3` bleibt lesbar; neu erzeugte Abbilder ergänzen
  Produkt-, Pack-, Regler- und Blocknamen sowie Payload- und Abbildprüfsummen
- Regler-CPU 0 bleibt der Sicherungsumfang; die getrennten Netzschutzräume der
  CPU 1 und 2 sind nicht Bestandteil dieses Abbildformats

## V3 1.1.1 - 2026-08-13

- Wartungsversion des unveränderten Funktionsstands von V3 1.1.0 mit
  konsistenter Kennzeichnung als 1.1.1 in Paket, Kommandozeile,
  HTTP-Zustandsprüfung, Serverkennung, Weboberfläche, Wartungsberichten und
  Dokumentation
- zentrale Laufzeit-Versionsquelle in `open_dachs_manager.__version__`; Paketbau,
  Webdienst und Berichte leiten ihre Kennzeichnung daraus ab, damit sich
  installierte und sichtbare Version nicht mehr unbemerkt auseinanderentwickeln
- eigener Eintrag im direkt in der Weboberfläche erreichbaren Änderungsverlauf,
  der den reinen Wartungscharakter dieser Version vom Funktionsumfang
  der V3 1.1.0 trennt
- der technische und der in der Oberfläche angezeigte Änderungsverlauf wurden
  vollständig deutsch formuliert; feststehende Bezeichnungen technischer
  Schnittstellen und Dateiformate bleiben erhalten
- keine Änderung an MSR2-Protokoll, Feldzuordnung, Authentifizierung,
  Schreibfreigabe, positiver Bestätigung (ACK), Rückleseprüfung, Verhalten der
  API, Historisierung oder lokalen Betriebsdaten; die Aktualisierung selbst
  schreibt nichts in den Regler

## V3 1.1.0 - 2026-08-10

- eigener, ausschließlich für Administratoren sichtbarer Hauptbereich
  **System**, getrennt von den Dachs-Einstellungen, mit Bereichen für Benutzer,
  API-Zugänge und Wartungsabschluss
- mehrere Benutzerkonten mit Rolle, Aktivstatus und eigenständigem
  Passwortwechsel; Schutz vor dem Löschen oder Deaktivieren des letzten
  Administrators
- lokale EDOMI-API mit Zugangsschlüsseln, deren vollständiger Wert nur einmal
  angezeigt und anschließend ausschließlich als kryptografischer Hash gespeichert
  wird, sowie getrennten Lese-, Historien- und Schreibrechten
- API-Schreiben standardmäßig global deaktiviert; übergeben werden nur
  logische Aktionen, während Authentifizierung, PW4, Schreiben, positive
  Bestätigung (ACK), Rückleseprüfung, Idempotenz und Prüfprotokoll vollständig im
  Dienst bleiben
- eine atomare, an den Inhalt gebundene Reservierung der `request_id` verhindert
  parallele Doppelausführungen und weist die Wiederverwendung für eine abweichende
  Aktion mit HTTP 409 zurück
- kraftstoffabhängige Originalgrenzen der Generatornennleistung werden für die
  dokumentierten Anlagentypen vor Authentifizierung und seriellem Schreiben
  geprüft
- adaptive Historie für alle regulären aktuellen Werte: volle Auflösung zunächst
  24 Stunden sowie dauerhaft für eine Stunde vor Motorstart, die gesamte
  Laufzeit und eine Stunde Nachlauf; übrige Stillstandszeit wird in
  15-Minuten-Fenster mit Anfang, Ende, Minimum, Maximum und Mittelwert
  verdichtet
- Webdienst bleibt lokales HTTP; externes HTTPS ist Aufgabe eines
  vorgeschalteten nginx
- Block-20-Feldzuordnung anhand der Originalstruktur um das ausgelassene
  Genehmigungsbyte korrigiert: Anzeigekontrast bleibt bei Byte-Offset 40,
  Zeitsynchronisierung liegt korrekt bei Byte-Offset 41
- exakte Kraftstoffbezeichnungen, eindeutige Kapseltemperatur und dokumentierter
  Hinweis zum unskalierten barometrischen Luftdruck in Block 24; unbekannter
  Kraftstoff bricht den Wartungsstart sicher ab, statt als Gas zu gelten
- Abschaltcodes der gemeinsamen Laufhistorie als Freigabemaske, bei der gelöschte
  Bits aktiv sind, mit Hexwert und deutschen Original-Klartexten dekodiert

## V3 1.0.1 - 2026-08-09

- Historisierung auf 21 ausgewählte Betriebs- und Messwerte aus den beiden
  schnellen Blöcken 22/24 begrenzt und pro Messzyklus als ein gemeinsamer
  kompakter SQLite-Datensatz gespeichert; Block 20 mit seinen weitgehend
  statischen Geräteinformationen läuft nur noch im langsamen Zyklus
- das konfigurierte Intervall von 0,75 s ist jetzt der Zielabstand kompletter
  Zyklen statt einer zusätzlichen Pause nach sämtlichen seriellen Lesevorgängen
- feste MSR2-Textfelder bewahren bei unveränderten und geänderten Schreibvorgängen
  ihre bereits vorhandene Leerzeichen- oder NUL-Auffüllung, statt vorhandene
  Leerzeichen unbemerkt durch NUL-Bytes zu ersetzen

## V3 1.0 - 2026-08-09

- erster stabiler öffentlicher 1.0-Stand mit der seit V3 0.9.0 erweiterten
  Bedien-, Überwachungs- und Wartungsoberfläche
- zentrale serielle FIFO-Architektur, Bearbeitung der zugeordneten Register,
  Wartungsarchiv, Fehlerkatalog, frei konfigurierbare Übersichtsseite und beide
  Bedienansichten gemeinsam als Version 1.0 veröffentlicht
- kontrollierte Hardware-Schreibvorgänge bleiben unverändert an ausdrückliche
  Freigabe, Authentifizierung, positive Bestätigung (ACK), Rückleseprüfung und
  Prüfprotokoll gebunden

## V3 0.9.10 - 2026-08-09

- bisherige generische Motorillustration durch eine eigens erzeugte technische
  3D-Darstellung in der charakteristischen kompakten Dachs-Bauform ersetzt:
  schwarzer Einzylindermotor, offene Schwungscheibe und silberne Seitendeckel
- transparentes, logo- und textfreies PNG in Anlagenübersicht und technischem
  Funktionsschema; aktuelle Werte, Generator und Medienwege bleiben unverändert

## V3 0.9.9 - 2026-08-08

- eigene generische Motorillustration für die Bedienoberfläche erzeugt: liegendes,
  stationäres Einzylinder-Aggregat in horizontaler Seitenansicht statt des
  bisherigen abstrakten Linienrasters
- transparent freigestelltes PNG ohne Herstellerlogo, Markenbezug oder
  übernommenes Fremdmaterial; Einbindung in Anlagenübersicht und technisches
  Funktionsschema bei weiterhin getrennt dargestelltem Generator

## V3 0.9.8 - 2026-08-08

- Rußfilter in der technischen Funktionsansicht als Wabenkörper neu gezeichnet;
  Motorabgastemperatur steht links vor dem Filter und Dachs-Abgastemperatur
  rechts nach dem Filter, ohne doppelte Temperaturzeile darunter
- geschätzter Rußfilter-Füllstand linear aus der Motorabgastemperatur:
  standardmäßig 0 % bis 420 °C und 100 % ab 520 °C; unter 60 % grün,
  von 60 bis 89 % orange und ab 90 % rot
- 0-%- und 100-%-Temperatur als lokale, nur durch Administratoren änderbare
  Einstellung in der Weboberfläche; die Kennlinie bleibt nach Neustarts und
  Aktualisierungen erhalten, und ihre Änderung löst keinen seriellen
  Schreibvorgang aus

## V3 0.9.7 - 2026-08-08

- technische Funktionsansicht überarbeitet: Beschriftung und aktueller Wert stehen
  in Motor-, Generator-, Kühlkreis-, Regler- und Netzgruppen jeweils eindeutig
  in derselben Tabellenzeile
- Kapseltemperatur fachlich vom Generator zum Regler verschoben; Dachs-Eintritt,
  Dachs-Abgas nach Rußfilter und elektrische Messstelle eindeutiger benannt
- Versionsnummer in Anmeldung und Kopfzeile anklickbar; der Änderungsverlauf
  öffnet als zugängliches Dialogfenster direkt in der Weboberfläche

## V3 0.9.6 - 2026-08-08

- Fehlerkatalog aus den Einstellungen herausgelöst und als eigener Hauptbereich
  neben Übersicht, Überwachung, Wartung, Einstellungen und dem Bereich für
  Schreibprotokolle verfügbar
- Anlagenmeldung springt weiterhin direkt zum passenden Servicecode, jetzt aber
  in den neuen Fehlerkatalog

## V3 0.9.5 - 2026-08-08

- neues, aufgeräumtes Anlagenbild mit eindeutigen Flussrichtungen für roten
  Dachs-Austritt, blauen Dachs-Eintritt und den außen ansetzenden Abgasweg
- zusätzliche technische Funktionsansicht mit Motor, Generator, Kühlkreis,
  MSR2-Regler, Sensorpunkten, Rußfilter und Netzseite
- Störungs- und Warnstatus direkt über beiden Ansichten; Zustand wird durch
  Form, Text und Farbe statt allein durch Farbe dargestellt
- eigener kompakter Fehlerkatalog im JSON-Schema
  `open-dachs-manager/fault-catalog/v1` mit 222 deutschen Klartexten
- aktive Meldungen zeigen Code und Beschreibung gemeinsam, zum Beispiel
  `SC 163 · Leistung zu klein`; optionale lokale Diagnosedaten können weiterhin
  Ursachen und Maßnahmen ergänzen

## V3 0.9.4 - 2026-08-08

- Anlagenbild durch eine übersichtliche, farbcodierte Anlagentafel mit klaren
  Tabellenwerten und eindeutigen Flussrichtungen ersetzt
- roter Dachs-Austritt, blauer Dachs-Eintritt und ein ausschließlich außen am
  Dachs ansetzender schwarz-silberner Abgasweg
- Rückleseprüfung kontrolliert nach positivem ACK gezielt die geänderten Feldbytes;
  laufende Zähler im selben Block erzeugen keinen falschen Schreibfehler mehr
- verzögerte Wiederholungen der Rückleseprüfung für Regler, die einen neuen
  Sollwert erst nach einer kurzen Verarbeitungszeit zurückmelden
- Prüfprotokoll unterscheidet vollständige Block-Rückleseprüfung und gezielte
  Feld-Rückleseprüfung und nennt die Zahl der benötigten Versuche

## V3 0.9.3 - 2026-08-08

- Wartungsrestzeit direkt im Kopf neben dem seriellen Verbindungsstatus; ein
  Klick öffnet die Wartungsansicht
- Wirkleistung Ist/Soll steht als erstes Element der Übersicht
- frei auswählbare und sortierbare Übersichtskacheln mit allen zugeordneten
  Anlagenwerten; Auswahl bleibt lokal auf dem Pi gespeichert
- optionaler lokaler Original-Fehlerkatalog mit vollständiger Suche nach
  Servicecodes, Ursachen und Maßnahmen; SC 163 wird als „Leistung zu klein“
  aufgelöst
- konfigurierbarer URL-Grundpfad für Unterpfade eines vorgeschalteten Webservers

## V3 0.9.2 - 2026-08-08

- Administratoren können offene und abgeschlossene Wartungen aus dem lokalen
  Archiv löschen
- Sicherheitsabfrage vor dem Entfernen von Zustandsabbild, Protokoll und Exporten
- ein bereits erfolgter Reglerabschluss sowie das Schreibprotokoll bleiben unberührt

## V3 0.9.1 - 2026-08-08

- dauerhafter Administratorschalter für den Wartungs-Testmodus in den Einstellungen
- standardmäßig weiterhin rein lokaler Wartungsabschluss ohne Schreibvorgang
  am Regler
- bei deaktiviertem Testmodus kontrollierter Echtabschluss mit Authentifizierung,
  exakter Bestätigung, positiver Bestätigung (ACK) und Rückleseprüfung
- Test-/Echtmodus wird in `/var/lib/open-dachs-manager/maintenance_settings.json`
  gespeichert und bleibt nach Neustarts und Aktualisierungen erhalten

## V3 0.9.0 - 2026-08-03

- neuer kompakter dreiseitiger Wartungsnachweis in HTML und PDF
- zusätzliche lokale Arbeitsliste neben den MSR2-Prüfpunkten
- serverseitig gesperrter Wartungs-Demomodus ohne Register-Schreibvorgang
- gemeinsames Anlagen-Zustandsabbild und lokales Berichtsarchiv
- korrigierte physische Byte-Offsets in Block 24; Gas/Heizöl wird beim
  Wartungsstart wieder automatisch aus `Hka_Mw1.bKraftstofftyp` erkannt
- getrennte Adressierung der beiden Netzüberwachungs-CPUs und geprüfte
  Block-16-Feldzuordnung für den eingesetzten Überwachungscontroller
- rot gekennzeichneter Netzschutzeditor mit dem bestehenden Administrator-,
  Authentifizierungs- und Hardware-Freigabeschalter sowie positiver Bestätigung
  (ACK), Rückleseprüfung und Prüfprotokoll

## 0.1.0 - 2026-08-03

- erstmalige Veröffentlichung des Open-Dachs-Manager-Quellcodes
- installierbares Python-Paket mit Kommandozeile, Terminal- und Weboberfläche
- zentraler FIFO-Dienst als alleiniger Besitzer des seriellen Adapters
- lokale Messwerthistorie und Wartungsberichte
- kontrollierter Schreibpfad mit Authentifizierung, positiver Bestätigung (ACK),
  Rückleseprüfung und Prüfprotokoll
- systemd-Dienste und `install.sh`
