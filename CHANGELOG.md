# Changelog

## V3 1.1.0 - 2026-08-10

- eigener, ausschließlich für Administratoren sichtbarer Hauptbereich
  **System**, getrennt von den Dachs-Einstellungen, mit den Registern
  **Benutzer & Berechtigungen**, **API & Tokens** und **Wartungsabschluss**
- mehrere Benutzerkonten mit Rolle, Aktivstatus und eigenständigem
  Passwortwechsel; Schutz vor dem Löschen oder Deaktivieren des letzten
  Administrators
- lokale EDOMI-API mit nur einmal angezeigten, gehasht gespeicherten
  Bearer-Tokens sowie getrennten Lese-, Historien- und Schreibrechten
- API-Schreiben standardmäßig global deaktiviert; übergeben werden nur
  logische Aktionen, während Authentifizierung, PW4, Schreiben, ACK, Readback,
  Idempotenz und Audit vollständig im Server bleiben
- atomare, inhaltsgebundene `request_id`-Reservierung verhindert parallele
  Doppelwrites und lehnt die Wiederverwendung für andere Aktionen mit HTTP 409 ab
- kraftstoffabhängige Originalgrenzen der Generatornennleistung werden für die
  dokumentierten Anlagentypen vor Authentifizierung und Serialwrite geprüft
- adaptive Historie für alle regulären Livewerte: volle Auflösung zunächst
  24 Stunden sowie dauerhaft für eine Stunde vor Motorstart, die gesamte
  Laufzeit und eine Stunde Nachlauf; übrige Stillstandszeit wird in
  15-Minuten-Fenster mit Anfang, Ende, Minimum, Maximum und Mittelwert
  verdichtet
- Webdienst bleibt lokales HTTP; externes HTTPS ist Aufgabe eines
  vorgeschalteten nginx
- Block-20-Mapping anhand der Originalstruktur um das ausgelassene
  Genehmigungsbyte korrigiert: Display-Kontrast bleibt Offset 40,
  Zeitsynchronisierung liegt korrekt an Offset 41
- exakte Kraftstoffbezeichnungen, eindeutige Kapseltemperatur und dokumentierter
  Hinweis zum unskalierten barometrischen Luftdruck in Block 24; unbekannter
  Kraftstoff bricht den Wartungsstart sicher ab, statt als Gas zu gelten
- Abschaltcodes der gemeinsamen Laufhistorie als aktive-low Freigabemaske mit
  Hexwert und deutschen Original-Klartexten dekodiert

## V3 1.0.1 - 2026-08-09

- Historisierung auf 21 ausgewählte Betriebs- und Messwerte aus den beiden
  schnellen Blöcken 22/24 begrenzt und pro Livezyklus als ein gemeinsamer
  kompakter SQLite-Snapshot gespeichert; Block 20 mit seinen weitgehend
  statischen Geräteinformationen läuft nur noch im langsamen Zyklus
- das konfigurierte Intervall von 0,75 s ist jetzt der Zielabstand kompletter
  Zyklen statt einer zusätzlichen Pause nach sämtlichen seriellen Reads
- feste MSR2-Textfelder bewahren bei No-op- und geänderten Writes ihre bereits
  vorhandene Space- oder NUL-Paddingart, statt Space-Padding unbemerkt durch
  NUL-Bytes zu ersetzen

## V3 1.0 - 2026-08-09

- erster stabiler öffentlicher 1.0-Stand mit der seit V3 0.9.0 erweiterten
  Bedien-, Überwachungs- und Wartungsoberfläche
- zentrale serielle FIFO-Architektur, gemappte Registerbearbeitung,
  Wartungsarchiv, Fehlerkatalog, frei konfigurierbares Dashboard und beide
  HMI-Ansichten gemeinsam als Version 1.0 veröffentlicht
- kontrollierte Hardware-Schreibvorgänge bleiben unverändert an ausdrückliche
  Freigabe, Authentifizierung, positives ACK, Readback und Audit gebunden

## V3 0.9.10 - 2026-08-09

- bisherige generische Motorillustration durch eine eigens erzeugte technische
  3D-Darstellung in der charakteristischen kompakten Dachs-Bauform ersetzt:
  schwarzer Einzylindermotor, offene Schwungscheibe und silberne Seitendeckel
- transparentes, logo- und textfreies PNG in Anlagenübersicht und technischem
  Funktionsschema; Livewerte, Generator und Medienwege bleiben unverändert

## V3 0.9.9 - 2026-08-08

- eigene generische Motorillustration für die HMI erzeugt: liegendes,
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
- 0-%- und 100-%-Temperatur als lokale, adminpflichtige Einstellung im Web;
  die Kennlinie bleibt nach Neustarts und Updates erhalten und öffnet keinen
  seriellen Schreibvorgang

## V3 0.9.7 - 2026-08-08

- technische Funktionsansicht überarbeitet: Beschriftung und Livewert stehen
  in Motor-, Generator-, Kühlkreis-, Regler- und Netzgruppen jeweils eindeutig
  in derselben Tabellenzeile
- Kapseltemperatur fachlich vom Generator zum Regler verschoben; Dachs-Eintritt,
  Dachs-Abgas nach Rußfilter und elektrische Messstelle eindeutiger benannt
- Versionsnummer in Anmeldung und Kopfzeile anklickbar; der Änderungsverlauf
  öffnet als zugängliches Popup direkt in der Weboberfläche

## V3 0.9.6 - 2026-08-08

- Fehlerkatalog aus den Einstellungen herausgelöst und als eigener Haupt-Tab
  neben Übersicht, Überwachung, Wartung, Einstellungen und Audit verfügbar
- Anlagenmeldung springt weiterhin direkt zum passenden Servicecode, jetzt aber
  in den neuen Fehlerkatalog-Tab

## V3 0.9.5 - 2026-08-08

- neues, aufgeräumtes Anlagenbild mit eindeutigen Flussrichtungen für roten
  Dachs-Austritt, blauen Dachs-Eintritt und den außen ansetzenden Abgasweg
- zusätzliche technische Funktionsansicht mit Motor, Generator, Kühlkreis,
  MSR2-Regler, Sensorpunkten, Rußfilter und Netzseite
- Alarm- und Warnstatus direkt über beiden Ansichten; Zustand wird durch
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
- Schreib-Readback prüft nach positivem ACK gezielt die geänderten Feldbytes;
  laufende Zähler im selben Block erzeugen keinen falschen Schreibfehler mehr
- verzögerte Readback-Wiederholungen für Regler, die einen neuen Sollwert erst
  nach einer kurzen Verarbeitungszeit zurückmelden
- Audit unterscheidet vollständigen Block-Readback und Feld-Readback und nennt
  die Zahl der benötigten Versuche

## V3 0.9.3 - 2026-08-08

- Wartungsrestzeit direkt im Kopf neben dem seriellen Verbindungsstatus; ein
  Klick öffnet die Wartungsansicht
- Wirkleistung Ist/Soll steht als erstes Element der Übersicht
- frei auswählbare und sortierbare Übersichtskacheln mit allen gemappten
  Anlagenwerten; Auswahl bleibt lokal auf dem Pi gespeichert
- optionaler lokaler Original-Fehlerkatalog mit vollständiger Suche nach
  Servicecodes, Ursachen und Maßnahmen; SC 163 wird als „Leistung zu klein“
  aufgelöst
- konfigurierbarer Web-Base-Path für Reverse-Proxy-Subpfade

## V3 0.9.2 - 2026-08-08

- Admins können offene und abgeschlossene Wartungen aus dem lokalen Archiv löschen
- Sicherheitsabfrage vor dem Entfernen von Snapshot, Protokoll und Exporten
- ein bereits erfolgter Reglerabschluss sowie das Schreib-Audit bleiben unberührt

## V3 0.9.1 - 2026-08-08

- persistenter Admin-Schalter für den Wartungs-Testmodus in den Einstellungen
- standardmäßig weiterhin rein lokaler Wartungsabschluss ohne Regler-Write
- bei deaktiviertem Testmodus kontrollierter Echtabschluss mit Authentifizierung,
  exakter Bestätigung, positivem ACK und Readback
- Test-/Echtmodus wird in `/var/lib/open-dachs-manager/maintenance_settings.json`
  gespeichert und bleibt nach Neustarts und Updates erhalten

## V3 0.9.0 - 2026-08-03

- neuer kompakter dreiseitiger Wartungsnachweis in HTML und PDF
- zusätzliche lokale Arbeitsliste neben den MSR2-Prüfpunkten
- serverseitig gesperrter Wartungs-Demomodus ohne Register-Write
- gemeinsamer Anlagen-Snapshot und lokales Berichtsarchiv
- korrigierte physische Block-24-Offsets; Gas/Heizöl wird beim Wartungsstart
  wieder automatisch aus `Hka_Mw1.bKraftstofftyp` erkannt
- getrennte Adressierung der beiden Netzüberwachungs-CPUs und geprüftes
  Block-16-Mapping für den eingesetzten Überwachungscontroller
- rot gekennzeichneter Netzschutzeditor mit dem bestehenden Admin-/Auth-/
  Hardware-Haken sowie ACK, Readback und Audit

## 0.1.0 - 2026-08-03

- erstes Open-Dachs-Manager-Repository
- installierbares Python-Paket mit CLI, TUI und Weboberfläche
- zentraler FIFO-Serialworker als alleiniger Besitzer des Adapters
- lokale Messwerthistorie und Wartungsberichte
- kontrollierter Schreibpfad mit Authentifizierung, ACK, Readback und Audit
- systemd-Dienste und `install.sh`
