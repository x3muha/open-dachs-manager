"""Standalone MSR2 field decoder for Open Dachs Manager.

Controller-specific details that are not expressible in the JSON pack alone
remain explicit here: padding bytes, label aliases, numeric scaling, dates,
ring buffers and status-bit presentation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from pathlib import Path


def base_key(key: str) -> str:
    return re.sub(r"\[\d+\]$", "", str(key))


def needs_pre_motor_pad(key: str) -> bool:
    """Return whether the HKA_MW1 diagnostic byte precedes this field.

    ``HKA_MW1`` contains a one-byte ``DIAGFLAGS`` bitset directly after
    ``bMotorStatus`` and before ``usDrehzahl``.  The versioned pack describes
    logical fields but not this bitset, so the sequential decoder accounts
    for the byte at this boundary.
    """
    b = base_key(key)
    return bool(
        b == "Hka_Mw1.usDrehzahl"
        or re.match(r"^Hka_BZbeiSC_Mw1_\d+L\.usDrehzahl$", b)
    )


def decode_fields(payload: bytes, fields: list[dict]) -> dict[str, object]:
    """Decode a pack layout using the physical offsets from the MSR2 pack.

    Most layouts happen to be in wire order, which made a sequential decoder
    look sufficient.  Version variants and a few controller structs are not:
    their entries can be appended in JSON order while retaining explicit
    physical offsets.  Ignoring those offsets shifts values in blocks 22, 114
    and 500-506.  Fields without an offset still use the running cursor so
    hand-written/minimal layouts keep the old behaviour.
    """
    out: dict[str, object] = {}
    cursor = 0
    pad_shift = 0
    diag_pad_applied = False

    for field in fields:
        if field.get("kind") == "space":
            length = int(field.get("length", 0) or 0)
            raw_offset = field.get("offset")
            offset = cursor if raw_offset is None else (
                int(raw_offset) if field.get("_physical_offset") else int(raw_offset) + pad_shift
            )
            cursor = max(cursor, offset + length)
            continue
        if field.get("kind") != "data":
            continue

        typ = str(field.get("type") or "Byte").lower()
        key = str(field.get("key") or "")
        if needs_pre_motor_pad(key) and not diag_pad_applied:
            pad_shift += 1
            diag_pad_applied = True
        array_length = int(field.get("length", 0) or 0)
        repeat = int(field.get("repeat", 1) or 1)
        count = repeat if repeat > 1 else (array_length if array_length > 1 else 1)
        raw_offset = field.get("offset")
        offset = cursor if raw_offset is None else (
            int(raw_offset) if field.get("_physical_offset") else int(raw_offset) + pad_shift
        )

        def put(item_key: str, value: object) -> None:
            out[item_key] = value

        if typ == "byte":
            step = int(field.get("stride", 1) or 1)
            for index in range(count):
                item_key = f"{key}[{index}]" if count > 1 else key
                item_offset = offset + index * step
                if item_offset + 1 > len(payload):
                    break
                value = payload[item_offset]
                if not bool(field.get("unsigned")) and value >= 128:
                    value -= 256
                put(item_key, value)
            cursor = max(cursor, offset + (count - 1) * step + 1)
        elif typ == "short":
            step = int(field.get("stride", 2) or 2)
            for index in range(count):
                item_key = f"{key}[{index}]" if count > 1 else key
                item_offset = offset + index * step
                if item_offset + 2 > len(payload):
                    break
                value = int.from_bytes(
                    payload[item_offset:item_offset + 2],
                    "little",
                    signed=not bool(field.get("unsigned")),
                )
                put(item_key, value)
            cursor = max(cursor, offset + (count - 1) * step + 2)
        elif typ == "long":
            step = int(field.get("stride", 4) or 4)
            for index in range(count):
                item_offset = offset + index * step
                if item_offset + 4 > len(payload):
                    break
                value = int.from_bytes(
                    payload[item_offset:item_offset + 4],
                    "little",
                    signed=not bool(field.get("unsigned")),
                )
                put(f"{key}[{index}]" if count > 1 else key, value)
            cursor = max(cursor, offset + (count - 1) * step + 4)
        elif typ == "string":
            length = array_length if array_length > 0 else 1
            if offset + length <= len(payload):
                value = payload[offset:offset + length].decode("latin1", errors="ignore")
                put(key, value.rstrip("\x00").strip())
            cursor = max(cursor, offset + length)
        else:
            if offset + 1 <= len(payload):
                put(key, payload[offset])
            cursor = max(cursor, offset + 1)

    return out


MOTOR_STATUS_MAP = {
    0: "OK",
    10: "Störabschaltung",
    11: "Abschaltroutine 1",
    12: "Abschaltroutine 2",
    13: "Drehz. 200 U/min",
    14: "Drehz 200 U/min NICHT erreicht",
    15: "Dachs >1 Minute AUS",
    16: "Dachs >4 Minuten AUS",
    20: "Startvorbereitung",
    21: "Starteinleitung",
    22: "Anlasser ein",
    23: "Anlasser läuft 1,5 Sekunden",
    24: "Anlasser aus",
    30: "Dachs läuft hoch",
    31: "Dachs im Synchrondrehzahlfenster",
    32: "Generator am Netz",
    33: "Stellmotorbewegung ZU",
    34: "Stellmotorbewegung AUF",
    35: "KEINE Stellmotorbewegung",
}


def _java_properties_escapes(value: str) -> str:
    value = re.sub(r"\\u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), value)
    value = value.replace("\\t", "\t").replace("\\n", "\n").replace("\\r", "\r")
    value = value.replace("\\:", ":").replace("\\=", "=").replace("\\\\", "\\")
    return value


def load_properties(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="latin-1", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = _java_properties_escapes(value.strip())
    return result


def _strip_html_label(value: str) -> str:
    value = re.sub(r"(?i)</?html>", "", value)
    value = re.sub(r"(?i)<br\s*/?>", " / ", value)
    value = re.sub(r"<[^>]+>", "", value)
    return " ".join(value.split()).strip()


def _humanize_key(key: str) -> str:
    value = base_key(key).split(".")[-1]
    value = re.sub(r"^(sb|us|ul|uch|b|a)(?=[A-Z])", "", value)
    value = value.replace("_", " ")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return value.strip()


def _phase_suffix(key: str) -> str:
    match = re.match(r"^(.*)\[(\d+)\]$", key)
    if not match:
        return ""
    base = re.sub(r"^Hka_BZbeiSC_Mw2_\d+L\.", "Hka_Mw2.", match.group(1))
    phase = {0: "L1", 1: "L2", 2: "L3"}.get(int(match.group(2)))
    if phase and base in {
        "Hka_Mw2.Hka_UC.ausVoltage1",
        "Hka_Mw2.Hka_UC.ausCurrent1",
        "Hka_Mw2.Hka_UC.ausImpedanz",
        "Hka_Mw2.Hka_UC.ausPhi",
    }:
        return f" ({phase})"
    return ""


FIELD_LABEL_OVERRIDES = {
    "Hka_Mw1.Temp.sbRuecklauf": "Rücklauffühler RF (externer Heizkreis)",
    "Hka_Mw1.Temp.sbVorlauf": "Vorlauffühler VF (externer Heizkreis)",
    "Hka_Mw1.Temp.sbGen": "Dachs-Eintritt",
    "Hka_Mw1.Temp.sbZS_Warmwasser": "Warmwasser-Isttemperatur",
    "Hka_Mw1.Temp.sAbgasMotor": "Motorabgastemperatur",
    "Hka_Mw1.Temp.sAbgasHKA": "Dachs-Abgastemperatur",
    "Hka_Mw1.Temp.sKapsel": "Kapseltemperatur / Kondenser-Abgastemperatur",
    "Hka_Bd.MaxTemp.sbMotor": "Maximale Kühlwassertemperatur Motor",
    "Hka_Bd.MaxTemp.sbGen": "Maximale Dachs-Eintrittstemperatur",
    "Hka_Bd.MaxTemp.sAbgasHKA": "Maximale Dachs-Abgastemperatur",
    "Hka_Bd.MaxTemp.sKapsel": "Maximale Kapsel-/Kondenser-Abgastemperatur",
    "Hka_Mw1.bKraftstofftyp": "Kraftstofftyp / Status 2",
    "Hka_Mw1.Bivschalt.bZeitBisUmschaltung": "Zeit bis zur Bivalenz-Umschaltung",
    "Hka_Ew.HydraulikNr.b2_Waermeerzeuger": "Wärmeerzeuger 2",
    "Motbel250.bDachsEintritt": "Dachs-Eintritt",
    "Motbel250.bKuehlwassertempMotor": "Kühlwasser Motor",
    "Motbel250.usAbgastemperaturMotor": "Motorabgastemperatur",
}


def _canonical_label_key(key: str) -> str:
    value = base_key(key)
    value = re.sub(r"^Motbel250\[\d+\]\.", "Motbel250.", value)
    value = re.sub(r"^Hka_BZbeiSC_Mw1_\d+L\.", "Hka_Mw1.", value)
    value = re.sub(r"^Hka_BZbeiSC_Mw2_\d+L\.", "Hka_Mw2.", value)
    return value


def _array_suffix(key: str) -> str:
    match = re.search(r"\[(\d+)\]", str(key))
    if not match:
        return ""
    index = int(match.group(1))
    base = base_key(key)
    phase = _phase_suffix(key)
    if phase:
        return phase
    if "Motbel250[" in str(key):
        return f" (Datensatz {index + 1})"
    if "Laufraster15Min" in base:
        return f" (Element {index + 1})"
    if "bSoftwareVersion" in base:
        return f" (Byte {index + 1})"
    if "ulZeitstempel" in base or "aNachfuellOel" in base:
        return f" (Eintrag {index + 1})"
    if ".bEin" in base or ".bAus" in base:
        return f" (Tag {index + 1})"
    return f" (Element {index + 1})"


def _contextualize_label(key: str, label: str) -> str:
    canonical = _canonical_label_key(key)
    override = FIELD_LABEL_OVERRIDES.get(canonical)
    value = override or label
    if override and str(key).startswith("Hka_BZbeiSC_Mw"):
        value = "MW1 " + value if "Mw1" in str(key) else "MW2 " + value

    base = base_key(key)
    if base == "AktuelleRingnummer_MeldeHist":
        value = "Aktuelle Ringnummer – Meldungshistorie"
    elif base == "MeldeHIST.bWert":
        value = "Meldungswert"
    elif base == "MeldeHIST.bMeldecodeTypModul":
        value = "Meldetyp und Modul (gepackt)"
    elif base == "MeldeHIST.ulZeitstempel":
        value = "Meldungszeitstempel"
    elif base == "AktuelleRingnummer_Lauf":
        value = "Aktuelle Ringnummer – Laufhistorie"
    elif base == "AktuelleRingnummer_Abschalt":
        value = "Aktuelle Ringnummer – Abschalthistorie"
    elif base == "AktuelleRingnummer_LaufZB":
        value = "Aktuelle Ringnummer – Zusatzbrenner-Laufhistorie"
    elif base == "AktuelleRingnummer_AbschaltZB":
        value = "Aktuelle Ringnummer – Zusatzbrenner-Abschalthistorie"
    else:
        match = re.match(r"AnzahlStarts24h(?:ZB)?_(\d+L)$", base)
        if match:
            suffix = "Zusatzbrenner " if "ZB" in base else ""
            value = f"Starts der letzten 24 Stunden ({suffix}{match.group(1)})"
    if "Heizkreis" in base:
        match = re.search(r"Heizkreis(\d+)", base)
        if match and f"Heizkreis {match.group(1)}" not in value:
            value += f" (Heizkreis {match.group(1)})"
    if "LastZaehler" in base:
        match = re.search(r"LastZaehler(\d+)", base)
        if match and f"Zähler {match.group(1)}" not in value:
            value += f" (Lastzähler {match.group(1)})"
    schedule_context = (
        (r"Schaltzeiten1\.aHKA(\d+)", "HKA {}") ,
        (r"Schaltzeiten1\.aWaermef(\d+)", "Wärmeführung {}"),
        (r"Schaltzeiten2\.aStromf(\d+)", "Stromführung {}"),
        (r"Schaltzeiten2\.aZirkulation(\d+)", "Zirkulationspumpe {}"),
        (r"Schaltzeiten3\.aHK(\d+)_(\d+)", "Heizkreis {} – Zeitblock {}"),
    )
    for pattern, template in schedule_context:
        match = re.search(pattern, base)
        if match:
            value = f"{template.format(*match.groups())} – {value}"
            break
    if "Mm_MinMax.ModulBhMin" in base:
        value += " (Betriebsstunden min)"
    elif "Mm_MinMax.ModulBhMax" in base:
        value += " (Betriebsstunden max)"
    elif "Mm_MinMax.ModulStartMin" in base:
        value += " (Starts min)"
    elif "Mm_MinMax.ModulStartMax" in base:
        value += " (Starts max)"
    if "Waermef_Ew.SoWi.Umschaltzeit" in base:
        match = re.search(r"Umschaltzeit([^\.]+)", base)
        if match and match.group(1) not in value:
            value += f" ({match.group(1)})"
    if "Waermef_Ew.Urlaub.Beginn" in base and "Urlaub Beginn" not in value:
        value += " (Urlaub Beginn)"
    if "Waermef_Ew.Urlaub.Ende" in base and "Urlaub Ende" not in value:
        value += " (Urlaub Ende)"
    if "Hk_Ew.Heizkreis" in base and "Schaltzeiten" in base:
        match = re.search(r"Schaltzeiten(\d+)", base)
        if match and f"Zeitblock {match.group(1)}" not in value:
            value += f" (Zeitblock {match.group(1)})"
    if "Ww_Bd.sbWwSollTemp" in base:
        value += " (Istwert)"
    elif "Ww_Ew.bWwSollTemp" in base:
        value += " (Einstellung)"
    if "Wartung_Cache.USoll" in base:
        value += " (Soll)"
    elif "Wartung_Cache.UAbgesetzt" in base:
        value += " (abgesetzt)"
    if "Hka_Abschaltgrund_" in base or "Hka_BZbeiSC_Hist_" in base or "Hka_BzbeiWarnHist_" in base:
        match = re.search(r"_(\d+L)\.", base)
        if match and match.group(1) not in value:
            value += f" (Eintrag {match.group(1)})"
    if "Wartung_Ew1.Vorher." in base and "vor Wartung" not in value:
        value += " (vor Wartung)"
    if "Wartung_Ew1.Nachher." in base and "nach Wartung" not in value:
        value += " (nach Wartung)"
    if "Wartung_Ew1.LetzteWIntvall." in base and "letztes Intervall" not in value:
        value += " (letztes Intervall)"
    if "Wartung_Ew1.VorLetzteWIntvall." in base and "vorletztes Intervall" not in value:
        value += " (vorletztes Intervall)"
    if "Hka_Ew.EinAus" in base and "Programm" not in value:
        match = re.search(r"EinAus(\d+)", base)
        if match:
            value += f" (Programm {match.group(1)})"
    if "Hka_Ew.EinAus" in base:
        value += " (Ein)" if base.endswith(".bEin") else " (Aus)" if base.endswith(".bAus") else ""
    if re.match(r"^Laufraster15Min(?:ZB)?_", base):
        match = re.search(r"^Laufraster15Min(ZB)?_([^\.]+)", base)
        if match:
            equipment = "Zusatzbrenner" if match.group(1) else "Dachs"
            period = "aktueller Tag" if match.group(2) == "aktTag" else match.group(2)
            value = f"{equipment}-Laufzeit im 15-Minuten-Raster ({period})"
    oil_match = re.search(r"^Schmieroel\.Tag(\d+)\.", base)
    if oil_match:
        value = f"Schmieröl Tag {oil_match.group(1)} – {value}"
    if base == "ModemGsm.ulZeitGsmSignal":
        value = "Zeitpunkt der GSM-Signalmessung (aktuell)"
    elif base == "ModemGsm.ulZeitGsmSignalMax":
        value = "Zeitpunkt der GSM-Signalmessung (Maximum)"
    elif base == "ModemGsm.ulZeitGsmSignalMin":
        value = "Zeitpunkt der GSM-Signalmessung (Minimum)"
    if base.endswith(".bPartyEndeZeit"):
        value = value.replace("bis", "Party-Endezeit")
    if base.endswith(".RaumSoll.bTempTag"):
        value = value.replace("Temp Tag", "Tag-Solltemperatur")
    elif base.endswith(".RaumSoll.bTempNacht"):
        value = value.replace("Temp Nacht", "Nacht-Solltemperatur")
    elif base.endswith(".RaumSoll.bTempUrlaub"):
        value = value.replace("Temp Urlaub", "Urlaubs-Solltemperatur")
    if base.endswith(".Heizkurve.bUntereHeizkBegr"):
        value = value.replace("Untere Heizk Begr", "Untere Heizkurvenbegrenzung")
    elif base.endswith(".Heizkurve.bObereHeizkBegr"):
        value = value.replace("Obere Heizk Begr", "Obere Heizkurvenbegrenzung")
    elif base.endswith(".Heizkurve.bSollVorlauft_TAP15"):
        value = value.replace("Soll Vorlauft TAP15", "Soll-Vorlauftemperatur bei +15 °C Außentemperatur")
    elif base.endswith(".Heizkurve.bSollVorlauft_TAM10"):
        value = value.replace("Soll Vorlauft TAM10", "Soll-Vorlauftemperatur bei −10 °C Außentemperatur")
    if base == "Hk_Ew.fHkObjekt":
        value = "Heizkreis 1 und 2 im Objekt"
    if base == "Waermef_Ew.SoWi.bTempWiBetriebbeiTag":
        value = "Winterbetrieb bei Tag, wenn Außentemperatur <"
    elif base == "Waermef_Ew.SoWi.bTempWiBetriebbeiNacht":
        value = "Winterbetrieb bei Nacht, wenn Außentemperatur <"
    elif base == "Waermef_Ew.SoWi.UmschaltzeitWiSoF.usDatum":
        value = "Umschaltung Winter → Sommer (frühester Termin)"
    elif base == "Waermef_Ew.SoWi.UmschaltzeitWiSoS.usDatum":
        value = "Umschaltung Winter → Sommer (spätester Termin)"
    elif base == "Waermef_Ew.SoWi.UmschaltzeitSoWiF.usDatum":
        value = "Umschaltung Sommer → Winter (frühester Termin)"
    elif base == "Waermef_Ew.SoWi.UmschaltzeitSoWiS.usDatum":
        value = "Umschaltung Sommer → Winter (spätester Termin)"
    elif base == "Waermef_Ew.FreigWaermeerz.bBivUmschaltzeitWw":
        value = "Bivalenz-Umschaltzeit bei Warmwasser"
    elif base == "Waermef_Ew.FreigWaermeerz.bSollTempSchnelleUmschaltungWw":
        value = "Solltemperatur für Schnellumschaltung bei Warmwasser"
    return value + _array_suffix(key)


def _cleanup_label(key: str, label: str) -> str:
    base = base_key(key)
    value = (label or "").strip()
    if re.match(r"^Hka_BZbeiSC_Mw1_\d+L\.", base) and not value.startswith("MW1 "):
        value = "MW1 " + value
    elif re.match(r"^Hka_BZbeiSC_Mw2_\d+L\.", base) and not value.startswith("MW2 "):
        value = "MW2 " + value
    value = re.sub(r"^(ub|aus|sb|us|ul|uch|s|b|a|f|l)\s+", "", value)
    value = value.replace(" Mc1", " MC1").replace(" Mc2", " MC2")
    value = value.replace(" / ", " ")
    replacements = {
        "Stoerungbei": "Störung bei",
        "Startsbei": "Starts bei",
        "Regelungsgrundlage / Programmwahl": "Regelungsgrundlage Programmwahl",
        "Max. Rücklauftemp für / Dachs-Betrieb": "Max. Rücklauftemp für Dachs-Betrieb",
        "Hydaulische Einbindung": "Hydraulische Einbindung",
        "Laufzeit bei / Tastbetätigung": "Laufzeit bei Tastbetätigung",
        "Wi bei Tagbetrieb wenn Aussentemp <": "Wi bei Tagbetrieb wenn Außentemp <",
        "Wi bei Nachtbetrieb wenn Aussentemp <": "Wi bei Nachtbetrieb wenn Außentemp <",
        "Vorlaufsollwert bei +15C Aussentemperatur": "Vorlaufsollwert bei +15°C Außentemperatur",
        "Vorlaufsollwert bei -10C Aussentemperatur": "Vorlaufsollwert bei -10°C Außentemperatur",
        "Uhrzeit NT-Zeit aus": "Uhrzeit NT-Zeit Ende",
        "fuer": "für",
        "Ruecklauf": "Rücklauf",
        "Aussen": "Außen",
        "Fuehler": "Fühler",
        "Stoerung": "Störung",
        "Waerm": "Wärme",
        "WIntvall": "W-Intervall",
        "beiSC": "bei SC",
        "Tel.-Nr.1 für Meldung Service: Tel.-Nr. 1": "Tel.-Nr.1 für Meldung/Service",
        "Tel.-Nr.2 für Meldung Service: Tel.-Nr. 2": "Tel.-Nr.2 für Meldung/Service",
        "Vorlaufansteig": "Vorlaufanstieg",
        "Disp Helligkeit": "Display-Helligkeit",
        "Disp Kontrast": "Display-Kontrast",
        "Brenner Anf": "Brenneranforderung",
        "Zahl der angeforderten Module": "Anzahl angeforderter Module",
        "Hka Abschaltgrund": "Dachs-Abschaltgrund",
        "Anzahl ausgeführtes Schmieröl nachfüllen": "Anzahl der Schmieröl-Nachfüllungen",
        "Email": "E-Mail",
        "Tag Alt": "Alter Tag",
        "Tag Halten": "Halte-Tag",
        "b2 Wärmeeerzeuger": "Wärmeerzeuger 2",
        "Istwert WW": "Warmwasser-Isttemperatur",
        "WW-Durchfluss": "Warmwasser-Durchfluss",
        "WW-Menge pro Jahr": "Warmwassermenge pro Jahr",
        "Solltemperatur WW": "Warmwasser-Solltemperatur",
        "Schaltdiff. WW-Fühler": "Schaltdifferenz Warmwasserfühler",
        "Biv.-Umschaltung": "Bivalenz-Umschaltung",
        "Zyklischer Dachs Lauf": "Zyklischer Dachs-Lauf",
        "Res1": "Reserve 1",
        "Signalstrke": "Signalstärke",
        "Whlart": "Wählart",
        "zurcksetzen": "zurücksetzen",
        "Zeitsync Aktiv": "Zeitsynchronisation aktiv",
        "Zeitzonen Index": "Zeitzonenindex",
        "Status Ethernetmodul": "Status des Ethernetmoduls",
        "Test Ethernetmodul": "Ethernetmodul-Test",
        "Meldecode Typ Return": "Meldecode-Typ (Rückgabe)",
        "Oelstand": "Ölstand",
        "Oeltemp": "Öltemperatur",
        "Laufzeit Vor Messung": "Laufzeit vor Messung",
        "Oelverbrauch": "Ölverbrauch",
        "Anz Störung bei Wartung": "Anzahl Störungen bei Wartung",
        "Betriebssek Vorl Wart": "Betriebssekunden vor Wartung",
        "Dachslauf Tag im Viertelstundentakt": "Dachs-Laufzeit im 15-Minuten-Raster",
        "Laenderkonfiguration": "Länderkonfiguration",
        "Startverzoegerung": "Startverzögerung",
        "Max. Dachslaufzeit": "Maximale Dachs-Laufzeit",
        "Max. Rücklauftemp für Dachs-Betrieb": "Maximale Rücklauftemperatur für Dachs-Betrieb",
        "Rücklaufsolltemp. bei hoher Sollwert": "Rücklauf-Solltemperatur bei hohem Sollwert",
        "Dachs Cos Phi Soll": "Dachs-Cos-Phi-Sollwert",
        "Stromf.": "Stromführung",
        "laenger": "länger",
        "Uhrzeit NT-Zeit ein": "Beginn der Niedertarifzeit",
        "Uhrzeit NT-Zeit Ende": "Ende der Niedertarifzeit",
        "NT-Zeit Wochenende Beginn": "Beginn der Niedertarifzeit am Wochenende",
        "NT-Zeit Wochenende Ende": "Ende der Niedertarifzeit am Wochenende",
        "Preis pro kWh für Bezug HT": "Bezugspreis Hochtarif pro kWh",
        "Preis pro kWh für Bezug NT": "Bezugspreis Niedertarif pro kWh",
        "Preis pro kWh für Lieferung HT": "Lieferpreis Hochtarif pro kWh",
        "Preis pro kWh für Lieferung NT": "Lieferpreis Niedertarif pro kWh",
        "f Server Sperr Aktiv": "Server-Sperre aktiv",
        "Server Sperr Aktiv": "Server-Sperre aktiv",
        "gewählte Nummer": "Gewählte Rufnummer",
        "Av Spiel": "Auslassventil-Spiel",
        "Ev Spiel": "Einlassventil-Spiel",
        "Av Ventilplatte": "Auslassventil-Ventilplatte",
        "Ev Ventilplatte": "Einlassventil-Ventilplatte",
        "Sum Abgas Motor": "Summe Motorabgastemperatur",
        "Sum Abgas Dachs": "Summe Dachs-Abgastemperatur",
        "Sum Dachs Eintritt": "Summe Dachs-Eintrittstemperatur",
        "Sum Kuehlw Motor": "Summe Motor-Kühlwassertemperatur",
        "Sum Int Reglertemp": "Summe interne Reglertemperatur",
        "Sum Gen Leistung": "Summe Generatorleistung",
        "Sum Abgas Kond": "Summe Kondenser-Abgastemperatur",
        "Cnt Abgas Motor": "Anzahl Motorabgastemperatur",
        "Cnt Abgas Dachs": "Anzahl Dachs-Abgastemperatur",
        "Cnt Dachs Eintritt": "Anzahl Dachs-Eintrittstemperatur",
        "Cnt Int Reglertemp": "Anzahl interne Reglertemperatur",
        "Cnt Abgas Kond": "Anzahl Kondenser-Abgastemperatur",
        "Sum Kuehlw Mot Start": "Summe Kühlwassertemperatur bei Motorstart",
        "Cnt Kuehlw Mot Start": "Anzahl Kühlwassertemperatur bei Motorstart",
        "l Sum Kurbel Druck": "Summe Kurbel-Druck",
        "Sum Kurbel Druck": "Summe Kurbel-Druck",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = re.sub(r"^(?:RESERVE|Reserve|Res)(\d+)$", r"Reserve \1", value)
    return re.sub(r"\s{2,}", " ", value).strip()


def label_for_key(key: str, labels: dict[str, str]) -> str:
    base = base_key(key)
    candidates = [key, key + ".presenter", key + ".Short", base, base + ".presenter", base + ".Short"]
    last = base.split(".")[-1]
    parent = base.rsplit(".", 1)[0] if "." in base else ""
    stem = re.sub(r"^(sb|us|ul|uch|b|a)(?=[A-Z])", "", last)
    if stem and parent:
        alias = parent + ".ul" + stem
        candidates += [alias, alias + ".presenter", alias + ".Short"]

    normalized = re.sub(r"^Hka_BZbeiSC_Mw[12]_\d+L\.", lambda m: "Hka_Mw1." if "Mw1" in m.group(0) else "Hka_Mw2.", base)
    normalized_key = re.sub(r"^Hka_BZbeiSC_Mw[12]_\d+L\.", lambda m: "Hka_Mw1." if "Mw1" in m.group(0) else "Hka_Mw2.", key)
    if normalized != base or normalized_key != key:
        candidates += [normalized_key, normalized_key + ".presenter", normalized_key + ".Short", normalized, normalized + ".presenter", normalized + ".Short"]

    for label_key, label in labels.items():
        suffix = label_key[1:] if label_key.startswith("*.") else label_key if label_key.startswith(".") else None
        if suffix and label.strip() and base.endswith(suffix):
            return _contextualize_label(key, _cleanup_label(key, _strip_html_label(label)))
    for candidate in candidates:
        value = labels.get(candidate)
        if value and value.strip():
            return _contextualize_label(key, _cleanup_label(key, _strip_html_label(value)))
    return _contextualize_label(key, _cleanup_label(key, _strip_html_label(_humanize_key(key))))


def _format_lookup_key(key: str, formats: dict) -> str | None:
    base = base_key(key)
    if base in formats:
        return base
    normalized = re.sub(r"^BD3112\.", "", base)
    normalized = re.sub(r"^Hka_BZbeiSC_Mw[12]_\d+L\.", lambda m: "Hka_Mw1." if "Mw1" in m.group(0) else "Hka_Mw2.", normalized)
    normalized = re.sub(r"^Hka_BZbeiSC_Mw1_XXL\.", "Hka_Mw1.", normalized)
    normalized = re.sub(r"^Hka_BZbeiSC_Hist_\d+L\.", "Hka_Bd.", normalized)
    normalized = re.sub(r"^Hka_BzbeiWarnHist_\d+L\.", "Hka_Bd.", normalized)
    if normalized in formats:
        return normalized
    matches = [candidate for candidate in formats if base.endswith(candidate) or normalized.endswith(candidate)]
    return max(matches, key=len) if matches else None


def format_key(key: str, formats: dict) -> str | None:
    """Return the canonical format key used for a mapped field.

    Encoding must use the same alias/variant resolution as decoding.  Keeping
    this tiny public wrapper avoids making the mapping layer depend on a
    decoder implementation detail.
    """
    return _format_lookup_key(key, formats)


def _service_code_label(code: object, service_labels: dict[str, str]) -> str:
    try:
        value = int(code)
    except (TypeError, ValueError):
        return "-"
    return service_labels.get(f"sc.{value}", "-")


def _format_usdatum(value: object) -> str | None:
    try:
        number = int(value) & 0xFFFF
    except (TypeError, ValueError):
        return None
    day, month = number & 0xFF, (number >> 8) & 0xFF
    if day == 0 and month == 0:
        return "--.--"
    return f"{day:02d}.{month:02d}" if 1 <= day <= 31 and 1 <= month <= 12 else None


def _format_timestamp(seconds: object, date_only: bool = False) -> str:
    return (datetime(2000, 1, 1) + timedelta(seconds=int(seconds))).strftime("%d.%m.%Y" if date_only else "%d.%m.%Y %H:%M:%S")


def _format_history_timestamp(seconds: object) -> tuple[str, bool]:
    """Format a history timestamp and flag values outside a plausible window.

    Current MSR2 data normally counts seconds from 2000-01-01.  Live block-18
    history on a tested controller also contained older entries encoded as
    Unix seconds.  Prefer the epoch that produces a plausible controller date
    and retain the MSR epoch as fallback for invalid or synthetic values.
    """
    value = int(seconds)
    msr_date = datetime(2000, 1, 1) + timedelta(seconds=value)
    unix_date = datetime(1970, 1, 1) + timedelta(seconds=value)
    lower = datetime(2010, 1, 1)
    upper = datetime.now() + timedelta(days=365 * 3)
    plausible_dates = [date for date in (msr_date, unix_date) if lower <= date <= upper]
    date = plausible_dates[0] if plausible_dates else msr_date
    rendered = date.strftime("%d.%m.%Y %H:%M:%S")
    plausible = bool(plausible_dates)
    return (f"{rendered} (!)" if not plausible else rendered), plausible


# Block 18 stores the message value and the type-specific message-code
# modifier in a compact six-byte ring entry.  These modifiers are part of the
# MSR2 data contract and are kept here as standalone V3 data, rather than
# loading the old XML definition at runtime.
MELDECODE_MODIFIERS: dict[int, tuple[str, int | None]] = {
    0: ("add", 100),
    1: ("add", 600),
    2: ("mul", 2),
    3: ("mul", 2),
    4: ("raw", None),
    5: ("add", 600),
    6: ("add", 600),
    7: ("mul", 2),
    8: ("mul", 2),
    9: ("zero", 0),
    10: ("add", 100),
    11: ("mul", 2),
    12: ("add", 600),
    13: ("zero", 0),
    14: ("raw", None),
    15: ("raw", None),
    16: ("raw", None),
    17: ("raw", None),
}


def meldecode_message_id(melde_type: object, raw_value: object) -> int | None:
    """Return the message ID represented by one block-18 ring entry."""
    try:
        typ = int(melde_type)
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    operation, argument = MELDECODE_MODIFIERS.get(typ, ("raw", None))
    if operation == "add" and argument is not None:
        return value + argument
    if operation == "mul" and argument is not None:
        return value * argument
    if operation == "zero":
        return 0
    return value


def decode_block18_history(
    payload: bytes,
    type_labels: dict[str, str],
    service_labels: dict[str, str],
) -> dict:
    """Decode the ten packed message-history entries in block 18.

    Wire layout: one current-ring byte, nine reserved bytes, then ten
    entries of ``bWert`` (byte), packed type/module (byte), and timestamp
    (little-endian Long).  Empty timestamps are retained as empty rows so the
    ring position remains visible and the UI can identify the active slot.
    """
    current_ring = payload[0] if payload else None
    entries: list[dict] = []
    offset = 10
    for index in range(10):
        entry_offset = offset + index * 6
        if entry_offset + 6 > len(payload):
            break
        raw_value = int(payload[entry_offset])
        packed = int(payload[entry_offset + 1])
        melde_type = (packed >> 4) & 0x0F
        module = packed & 0x0F
        timestamp = int.from_bytes(payload[entry_offset + 2:entry_offset + 6], "little", signed=False)
        message_id = meldecode_message_id(melde_type, raw_value)
        type_label = _strip_html_label(
            type_labels.get(f"MeldeHIST.bMeldecodeTyp.option.{melde_type}", "Unbekannter Meldetyp")
        )
        message_label = service_labels.get(f"sc.{message_id}", "") if message_id is not None else ""
        raw_label = service_labels.get(f"sc.{raw_value}", "")
        has_event = bool(timestamp or raw_value or packed)
        if not has_event:
            message_id = None
            message_label = ""
        if not message_label:
            message_label = raw_label
        if not message_label:
            message_label = f"Code {message_id}" if has_event and message_id is not None else "Kein Eintrag"
        if module == 0:
            module_label = "kein Modul"
        elif module == 1:
            module_label = "Dachs"
        else:
            module_label = f"Modul {module}"
        timestamp_text, timestamp_plausible = _format_history_timestamp(timestamp) if timestamp else (None, False)
        entries.append({
            "index": index,
            "active": current_ring is not None and int(current_ring) == index,
            "timestamp": timestamp or None,
            "timestamp_text": timestamp_text,
            "timestamp_plausible": timestamp_plausible,
            "message": message_label,
            "message_id": message_id,
            "type": melde_type,
            "type_label": type_label,
            "raw_value": raw_value,
            # ``bWert`` is an operand for the type-specific modifier, not an
            # independent service code.  Showing sc.<bWert> beside a different
            # resolved message ID is misleading (for example 120 -> 220).
            "raw_value_label": raw_label if message_id == raw_value else None,
            "module": module,
            "module_label": module_label,
            "has_event": has_event,
            "offset": entry_offset,
        })
    return {
        "current_ring": current_ring,
        "entry_count": len(entries),
        "entries": entries,
        "layout": "10 × (Wert, Meldetyp/Modul, Zeitstempel)",
    }


def _ring_recency(current_ring: int | None, length: int) -> dict[int, int]:
    """Return one-based ring slots mapped to recency ranks.

    Rank one is the newest completed entry; the ring value points at the next
    position rather than at that completed entry.
    """
    if current_ring is None or length <= 0:
        return {}
    return {
        ((int(current_ring) - rank - 1 + length) % length) + 1: rank
        for rank in range(1, length + 1)
    }


def _code_list(properties: dict[str, str], key: str) -> list[str]:
    return [item.strip() for item in str(properties.get(key, "")).split(",") if item.strip()]


def _service_code_details(code: int, service_labels: dict[str, str]) -> tuple[str, list[dict], list[dict]]:
    """Resolve optional locally maintained text, causes and measures."""
    text = _strip_html_label(service_labels.get(f"sc.{code}", ""))
    causes = []
    measures = []
    seen_measures: set[str] = set()
    for cause_code in _code_list(service_labels, f"sc.{code}.uc"):
        causes.append({
            "code": cause_code,
            "text": _strip_html_label(service_labels.get(f"uc.{cause_code}", "")) or "Kein Klartext hinterlegt",
        })
        for measure_code in _code_list(service_labels, f"uc.{cause_code}.mc"):
            if measure_code in seen_measures:
                continue
            seen_measures.add(measure_code)
            measures.append({
                "code": measure_code,
                "text": _strip_html_label(service_labels.get(f"mc.{measure_code}", "")) or "Kein Klartext hinterlegt",
            })
    return text, causes, measures


def decode_service_history(
    payloads: dict[int, bytes],
    type_labels: dict[str, str],
    service_labels: dict[str, str],
) -> dict:
    """Combine blocks 80 and 82 into service and warning history.

    Block 80 holds ring counters plus service entries 1..8.  Block 82 holds
    service entries 9..13 and five warning entries.  Service-code identifiers
    use offset +100; warning-code identifiers use +600.
    """
    block80 = payloads.get(80, b"")
    block82 = payloads.get(82, b"")
    service_ring = int(block80[0]) if len(block80) > 0 else None
    snapshot_ring = int(block80[1]) if len(block80) > 1 else None
    warning_ring = int(block80[2]) if len(block80) > 2 else None
    service_recency = _ring_recency(service_ring, 13)
    warning_recency = _ring_recency(warning_ring, 5)

    services = []
    for slot in range(1, 14):
        payload = block80 if slot <= 8 else block82
        offset = 6 + (slot - 1) * 8 if slot <= 8 else (slot - 9) * 8
        if offset + 8 > len(payload):
            continue
        raw_code = int(payload[offset])
        reserve = int(payload[offset + 1])
        timestamp = int.from_bytes(payload[offset + 2:offset + 6], "little", signed=False)
        delta_runtime = int(payload[offset + 6])
        status_flags = int(payload[offset + 7])
        has_event = bool(raw_code or timestamp or delta_runtime or status_flags)
        code = raw_code + 100 if has_event else None
        text, causes, measures = _service_code_details(code, service_labels) if code is not None else ("", [], [])
        timestamp_text, plausible = _format_history_timestamp(timestamp) if timestamp else (None, False)
        services.append({
            "slot": slot,
            "source_block": 80 if slot <= 8 else 82,
            "offset": offset,
            "recency": service_recency.get(slot),
            "active": service_recency.get(slot) == 1,
            "raw_code": raw_code,
            "code": code,
            "text": text or ("Keine Beschreibung hinterlegt" if has_event else "Kein Eintrag"),
            "timestamp": timestamp or None,
            "timestamp_text": timestamp_text,
            "timestamp_plausible": plausible,
            "delta_motor_runtime": delta_runtime,
            "reserve": reserve,
            "status_flags": status_flags,
            "disturbance_reset": bool(status_flags & 0x01),
            "auto_reset": bool(status_flags & 0x02),
            "causes": causes,
            "measures": measures,
            "has_event": has_event,
        })

    warnings = []
    for slot in range(1, 6):
        offset = 40 + (slot - 1) * 6
        if offset + 6 > len(block82):
            continue
        raw_code = int(block82[offset])
        packed = int(block82[offset + 1])
        timestamp = int.from_bytes(block82[offset + 2:offset + 6], "little", signed=False)
        warn_type = (packed >> 4) & 0x0F
        module = packed & 0x0F
        has_event = bool(raw_code or packed or timestamp)
        code = raw_code + 600 if has_event else None
        text = _strip_html_label(service_labels.get(f"sc.{code}", "")) if code is not None else ""
        type_label = _strip_html_label(
            type_labels.get(f"MeldeHIST.bMeldecodeTyp.option.{warn_type}", "Unbekannter Warntyp")
        )
        module_label = "kein Modul" if module == 0 else "Dachs" if module == 1 else f"Modul {module}"
        timestamp_text, plausible = _format_history_timestamp(timestamp) if timestamp else (None, False)
        warnings.append({
            "slot": slot,
            "source_block": 82,
            "offset": offset,
            "recency": warning_recency.get(slot),
            "active": warning_recency.get(slot) == 1,
            "raw_code": raw_code,
            "code": code,
            "text": text or ("Keine Beschreibung hinterlegt" if has_event else "Kein Eintrag"),
            "timestamp": timestamp or None,
            "timestamp_text": timestamp_text,
            "timestamp_plausible": plausible,
            "type": warn_type,
            "type_label": type_label,
            "module": module,
            "module_label": module_label,
            "packed_type_module": packed,
            "has_event": has_event,
        })

    services.sort(key=lambda entry: entry["recency"] or 999)
    warnings.sort(key=lambda entry: entry["recency"] or 999)
    return {
        "available": bool(block80 and block82),
        "service_ring": service_ring,
        "snapshot_ring": snapshot_ring,
        "warning_ring": warning_ring,
        "services": services,
        "warnings": warnings,
        "layout": "Block 80: Ringzähler + Service 1–8; Block 82: Service 9–13 + Warnung 1–5",
    }


def decode_block102_history(payload: bytes) -> dict:
    """Decode the three interleaved NACHFUELL records in block 102.

    The wire layout is an array of three ten-byte structs.  Each struct is
    ``Long timestamp, Long operating-seconds, Byte amount, Byte reserve``;
    treating it as four parallel arrays shifts every entry after the first.
    """
    entries: list[dict] = []
    for index in range(3):
        offset = index * 10
        if offset + 10 > len(payload):
            break
        timestamp = int.from_bytes(payload[offset:offset + 4], "little", signed=False)
        operating_seconds = int.from_bytes(payload[offset + 4:offset + 8], "little", signed=False)
        amount = int(payload[offset + 8])
        has_event = bool(timestamp or operating_seconds or amount)
        timestamp_text, plausible = _format_history_timestamp(timestamp) if timestamp else (None, False)
        entries.append({
            "index": index + 1,
            "offset": offset,
            "timestamp": timestamp or None,
            "timestamp_text": timestamp_text,
            "timestamp_plausible": plausible,
            "operating_seconds": operating_seconds,
            "operating_hours": round(operating_seconds / 3600.0, 2),
            "amount": amount,
            "has_event": has_event,
        })
    counter = int(payload[30]) if len(payload) > 30 else None
    return {
        "counter": counter,
        "entry_count": len(entries),
        "entries": entries,
        "layout": "3 × (Zeitstempel, Betriebssekunden, Menge, Reserve)",
        "amount_unit": None,
    }


MC_FLAG_FIELDS: tuple[tuple[int, int, str, str], ...] = (
    (0, 1, "Spannungsüberwachung Schutzfunktion 1", "ok"),
    (1, 1, "Schnelle Spannungsüberwachung", "ok"),
    (2, 1, "Frequenzüberwachung Schutzfunktion 1", "ok"),
    (3, 1, "Rückleistungsüberwachung", "ok"),
    (4, 1, "Rückleistungs-Abschaltung", "ok"),
    (5, 1, "Rückmeldungen", "ok"),
    (6, 1, "Anlasser-Einschaltzeit", "ok"),
    (7, 1, "Startpause", "ok"),
    (8, 1, "Impedanzüberwachung", "ok"),
    (9, 1, "Messkanäle", "ok"),
    (10, 1, "Drehfeld", "ok"),
    (11, 1, "EEPROM", "ok"),
    (12, 1, "Multiplexer", "ok"),
    (13, 1, "Leitung", "ok"),
    (14, 1, "Hauptrelais", "ok"),
    (15, 1, "Maximaldrehzahl", "ok"),
    (16, 1, "Freigabe Drehzahl/MV1", "ok"),
    (17, 1, "Laufzeit", "ok"),
    (18, 1, "Betriebsart", "ok"),
    (19, 1, "Magnetventile", "ok"),
    (20, 1, "Identifizierung", "ok"),
    (21, 1, "Durchlauf", "ok"),
    (22, 1, "Synchronisierung", "ok"),
    (23, 1, "Ergebnisse", "ok"),
    (24, 1, "A/D-Wandler", "ok"),
    (25, 1, "Versorgung", "ok"),
    (26, 1, "Selbsttest vor Start", "ok"),
    (27, 1, "Winkel", "ok"),
    (28, 1, "Freigabe Sicherheitskette Motor", "ok"),
    (29, 1, "Freigabe Sicherheitskette Generator", "ok"),
    (30, 1, "Freigabe Sicherheitskette Anlasser", "ok"),
    (31, 1, "ENS", "ok"),
    (32, 1, "Motor gestartet", "bool"),
    (33, 1, "Generatordrehzahl im Zuschaltfenster", "ok"),
    (34, 1, "Startverzögerung", "delay"),
    (35, 1, "CAN-Kommunikation", "ok"),
    (36, 1, "Gasdrucküberwachung", "ok"),
    (37, 1, "Spannungsüberwachung Schutzfunktion 2", "ok"),
    (38, 1, "Frequenzüberwachung Schutzfunktion 2", "ok"),
    (39, 1, "LOM", "ok"),
    (40, 1, "Rückmeldung Generator", "feedback"),
    (41, 1, "Rückmeldung Kuppelschalter", "feedback"),
    (42, 1, "Rückmeldung MV1", "feedback"),
    (43, 1, "Rückmeldung Hauptrelais", "feedback"),
    (44, 1, "Rückmeldung Sicherheitskette EIN", "feedback"),
    (45, 1, "Rückmeldung Überwachung EIN", "feedback"),
    (46, 1, "Rückmeldung MV2", "feedback"),
    (47, 1, "Rückmeldung Start-Magnetventil", "feedback-inverse"),
    (48, 1, "Rückmeldung Netzersatz", "feedback"),
    (49, 1, "Rückmeldung Anlasser", "feedback"),
    (50, 1, "Rückmeldung 1 / hoher Sollwert", "feedback"),
    (51, 1, "Rückmeldung 2 (programmierbar)", "feedback"),
    (52, 1, "Rückmeldung Gasmangel", "feedback"),
    (53, 1, "Rückmeldung Sanftanlauf", "feedback"),
    (54, 1, "Rückmeldung 3 (programmierbar)", "feedback"),
    (55, 1, "Reserve 6/7", "reserved"),
    (56, 2, "Betriebsart (2 Bit)", "number"),
    (58, 1, "Netz", "ok"),
    (59, 1, "Motor", "ok"),
    (60, 1, "Startfreigabe", "ok"),
    (61, 1, "Regler-Initialisierung", "ok"),
    (62, 1, "Regler", "ok"),
    (63, 1, "Datensatz gültig", "ok"),
)

MC_ACTOR_LABELS = (
    "Hauptrelais", "Generator", "Magnetventil 1", "Magnetventil 2",
    "Start-Magnetventil", "Anlasser", "Zündung", "Hubmagnet",
)


def _mc_state(value: int, mode: str) -> tuple[str, str]:
    if mode == "ok":
        return ("OK", "ok") if value else ("Fehler", "error")
    if mode == "feedback":
        return ("vorhanden", "active") if value else ("nicht vorhanden", "neutral")
    if mode == "feedback-inverse":
        return ("nicht vorhanden", "neutral") if value else ("vorhanden", "active")
    if mode == "delay":
        return ("Startverzögerung", "active") if value else ("keine", "neutral")
    if mode == "bool":
        return ("ja", "active") if value else ("nein", "neutral")
    if mode == "reserved":
        return ("gesetzt", "neutral") if value else ("nicht gesetzt", "neutral")
    return str(value), "neutral"


def decode_mc_status(payload: bytes) -> dict:
    """Expand both 64-bit FLAGS structures and adjacent UC fields."""
    if len(payload) < 52:
        return {"available": False, "flags": [], "controllers": []}
    flag_bytes = (payload[20:28], payload[28:36])
    flags = []
    for bit, width, label, mode in MC_FLAG_FIELDS:
        row = {"bit": bit, "width": width, "label": label, "reserved": mode == "reserved"}
        for index, raw_bytes in enumerate(flag_bytes, start=1):
            raw_number = int.from_bytes(raw_bytes, "little", signed=False)
            value = (raw_number >> bit) & ((1 << width) - 1)
            text, state = _mc_state(value, mode)
            row[f"mc{index}"] = {"value": value, "text": text, "state": state}
        flags.append(row)
    controllers = [
        {
            "name": f"MC{index}",
            "error_reason": int(payload[44 + index * 2]),
            "error_code": int(payload[45 + index * 2]),
            "protection_type": int(payload[49 + index]),
            "flags_hex": flag_bytes[index - 1].hex(" ").upper(),
            "actors": [
                {"label": label, "active": bool(payload[37 + index] & (1 << bit))}
                for bit, label in enumerate(MC_ACTOR_LABELS)
            ],
        }
        for index in (1, 2)
    ]
    return {
        "available": True,
        "controllers": controllers,
        "flags": flags,
        "state": {
            "oil_pressure": bool(payload[36] & 0x01),
            "liquid_switch": bool(payload[36] & 0x02),
            "raw": int(payload[36]),
        },
        "raw_error_lookup_available": False,
    }


def _quarter_hours(raw: bytes) -> list[bool]:
    """Decode the 12-byte day raster, most-significant bit first."""
    return [bool(byte & (0x80 >> bit)) for byte in raw[:12] for bit in range(8)]


def decode_run_history(payloads: dict[int, bytes]) -> dict:
    """Combine day-chart blocks 28, 30, 31 and 32."""
    p28 = payloads.get(28, b"")
    p30 = payloads.get(30, b"")
    p31 = payloads.get(31, b"")
    p32 = payloads.get(32, b"")
    if min(len(p28), len(p30), len(p31), len(p32)) == 0:
        return {"available": False, "days": [], "shutdowns": [], "summary": {}}

    ring = int(p28[0]) if p28 else 0
    starts = [int(value) for value in p28[3:10]]
    profiles = [
        _quarter_hours(p28[offset:offset + 12]) for offset in (10, 22, 34, 46, 58)
    ] + [
        _quarter_hours(p30[offset:offset + 12]) for offset in (0, 12)
    ]
    today_live = _quarter_hours(p31[2:14])
    now = datetime.now().astimezone()
    boundary = (now.hour // 2) * 8
    current_slot = ring % 7
    today = list(profiles[current_slot])
    for index in range(boundary, min(96, len(today_live))):
        today[index] = today_live[index] if index * 15 <= now.hour * 60 + now.minute else False

    days = []
    for age in range(7):
        slot = (ring - age) % 7
        points = today if age == 0 else profiles[slot]
        day_date = now.date() - timedelta(days=age)
        days.append({
            "age": age,
            "date": day_date.isoformat(),
            "date_text": day_date.strftime("%d.%m.%Y"),
            "day_label": "Heute" if age == 0 else day_date.strftime("%a"),
            "ring_slot": slot + 1,
            "starts": starts[slot],
            "runtime_hours": round(sum(points) * 0.25, 2),
            "quarters": [1 if point else 0 for point in points],
        })

    shutdowns = []
    for index, offset in enumerate((26, 32, 38, 44, 50), start=1):
        if offset + 6 > len(p30):
            break
        code = int.from_bytes(p30[offset:offset + 2], "little", signed=False)
        timestamp = int.from_bytes(p30[offset + 2:offset + 6], "little", signed=False)
        timestamp_text, plausible = _format_history_timestamp(timestamp) if timestamp else (None, False)
        shutdowns.append({
            "index": index,
            "code": code,
            "timestamp": timestamp or None,
            "timestamp_text": timestamp_text,
            "timestamp_plausible": plausible,
            "has_event": bool(code or timestamp),
        })

    def ulong(offset: int) -> int:
        return int.from_bytes(p32[offset:offset + 4], "little", signed=False) if len(p32) >= offset + 4 else 0

    summary = {
        "operating_hours": round(ulong(0) / 3600.0, 2),
        "starts": ulong(4),
        "electric_work_kwh": round(ulong(8) / 1000.0, 3),
        "thermal_work_hka_kwh": round(ulong(12) / 1000.0, 3),
        "thermal_work_condenser_kwh": round(ulong(16) / 1000.0, 3),
        "hot_water_m3": round(ulong(20) / 1_000_000.0, 6),
    }
    return {
        "available": True,
        "ring": ring,
        "days": days,
        "shutdowns": shutdowns,
        "summary": summary,
        "source_blocks": [28, 30, 31, 32],
    }


def apply_format(key: str, value: object, formats: dict, service_labels: dict[str, str]) -> tuple[object, str]:
    if isinstance(value, str):
        return value, ""
    format_key = _format_lookup_key(key, formats)
    fmt = dict(formats.get(format_key, {}) if format_key else {})
    base = base_key(key).lower()
    live = re.sub(r"^hka_bzbeisc_mw[12]_\d+l\.", lambda m: "hka_mw1." if "mw1" in m.group(0) else "hka_mw2.", base)
    live = re.sub(r"^hka_bzbeisc_mw1_xxl\.", "hka_mw1.", live)

    for invalid in fmt.get("invalidvals") or []:
        try:
            if float(value) == float(invalid):
                if live == "hka_bd.bstoerung":
                    return "Kein aktiver Servicecode", ""
                if live == "hka_bd.bwarnung":
                    return "Keine aktive Warnung", ""
                return fmt.get("invaliddisplay") or "n.a.", fmt.get("unit", "")
        except (TypeError, ValueError):
            pass

    number = float(value) + float(fmt.get("adder", 0) or 0)
    divisor = float(fmt.get("divisor", 1) or 1)
    if divisor not in (0, 1):
        number /= divisor
    display_format = str(fmt.get("format", "") or "")

    if live == "hka_mw1.bmotorstatus":
        integer = int(number)
        return f"{integer} ({MOTOR_STATUS_MAP.get(integer, 'unbekannt')})", ""
    if live.endswith(".usdatum"):
        date = _format_usdatum(number)
        if date is not None:
            return date, ""
    if "{0,date}" in display_format or "inbetriebnahmedatum" in live or "ulzeitstempel" in live or live.endswith(".uldatum"):
        try:
            if "inbetriebnahmedatum" in live:
                return _format_timestamp(round(number), True), ""
            rendered, _plausible = _format_history_timestamp(round(number))
            return rendered, ""
        except (TypeError, ValueError, OverflowError):
            pass

    thresholds = {
        "hka_bd.uhka_frei.usfreigabe": (65535, "nein", "ja"),
        "hka_bd.ubrenner_frei.bfreigabe": (255, "nein", "ja"),
        "hka_bd.ustromf_frei.bfreigabe": (255, "nein", "ja"),
        "hka_mw1.temp.sbfreigabemodul": (127, "nein", "ja"),
    }
    if live in thresholds:
        threshold, low, high = thresholds[live]
        integer = int(round(number))
        return f"{integer} ({high if integer >= threshold else low})", ""
    if live.endswith(".bstoercode") or live.endswith(".bwarncode"):
        raw_code = int(round(number))
        if raw_code == 0:
            return "Keine aktive Meldung", ""
        base_code = raw_code + (100 if live.endswith(".bstoercode") else 600)
        label = _service_code_label(base_code, service_labels)
        kind = "SC" if live.endswith(".bstoercode") else "WARN"
        return f"{kind} {base_code} · {label if label != '-' else 'Unbekannter Code'}", ""
    if live.endswith(".bstoerung") or live.endswith(".bwarnung"):
        raw_code = int(round(float(value)))
        if raw_code == 0:
            return "Kein aktiver Servicecode" if live.endswith(".bstoerung") else "Keine aktive Warnung", ""
        if live.startswith("mm.moduldaten."):
            # Multi-module status bytes use these offsets; they are not part
            # of the generic field format.
            base_code = raw_code + (100 if live.endswith(".bstoerung") else 600)
        else:
            base_code = int(round(number))
        label = _service_code_label(base_code, service_labels)
        kind = "SC" if live.endswith(".bstoerung") else "WARN"
        return f"{kind} {base_code} · {label if label != '-' else 'Unbekannter Code'}", ""
    if live == "hka_mw1.bkraftstofftyp":
        integer = int(round(number))
        kind = "Öl" if integer in (8, 9, 10, 11) else "Gas" if integer in (128, 144, 160, 176, 192, 208) else "-" if integer == 0 else "unbekannt"
        return f"{integer} ({kind})", ""
    if live == "hka_ew.uchprogrammwahl":
        integer = int(round(number))
        return f"{integer} ({ {65: 'A', 66: 'B', 69: 'E', 83: 'S'}.get(integer, 'unbekannt') })", ""
    if live.endswith("_min"):
        minutes = int(round(number))
        return (f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes} min"), ""

    value_map = fmt.get("value_map") or fmt.get("enum") or fmt.get("choices") or {}
    if isinstance(value_map, dict) and value_map:
        integer = int(round(number))
        text = value_map.get(str(integer), value_map.get(integer))
        return f"{integer} ({text if text is not None and str(text) else 'unknown-index'})", ""

    if "integer" in display_format:
        rendered: object = int(round(number))
    elif "#.##" in display_format:
        rendered = round(number, 2)
        if abs(rendered - int(rendered)) < 1e-9:
            rendered = int(rendered)
    elif abs(number - int(number)) < 1e-9:
        rendered = int(number)
    else:
        rendered = number
    return rendered, str(fmt.get("unit", "") or "")
