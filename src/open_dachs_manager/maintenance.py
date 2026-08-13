"""Standalone maintenance workflow helpers for Open Dachs Manager.

Every regulator checklist answer is connected to its byte-safe packed field
in block 100.  The confirmation bit in block 104 is written only after block
100 has passed its readback check.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import hashlib
from io import BytesIO
import json

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import __version__


MAINTENANCE_CONFIRMATION = "WARTUNG ABSCHLIESSEN"
MAINTENANCE_DEMO_CONFIRMATION = "DEMO ABSCHLIESSEN"
MAINTENANCE_REQUIRED_BLOCKS = frozenset({20, 22, 24, 100, 104})
MAINTENANCE_CACHE_KEYS = frozenset({
    "Wartung_Cache.bZaehler",
    "Wartung_Cache.ulBetriebssekundenBei",
    "Wartung_Cache.usIntervall",
    "Wartung_Cache.ulZeitstempel",
    "Wartung_Cache.sDeltaBh",
    "Wartung_Cache.sDeltaTage",
})


BOOLEAN_STATUS = ("yes", "no")
CORRECTABLE_STATUS = ("yes", "no", "corrected")

COMMON_CHECKLIST = (
    ("geraeusch", "Geräuschverhalten in Ordnung", "Wartung_Ew1.Dicht_Wart.bGeraeusch", CORRECTABLE_STATUS),
    ("kraftstoff_abgas_dicht", "Kraftstoff-/Abgassystem dicht", "Wartung_Ew1.Dicht_Wart.bKraftstoff_Abgas", CORRECTABLE_STATUS),
    ("schmieroel_dicht", "Schmierölsystem dicht", "Wartung_Ew1.Dicht_Wart.bSchmieroel", CORRECTABLE_STATUS),
    ("heizwasser_dicht", "Heizwassersystem dicht", "Wartung_Ew1.Dicht_Wart.bHeizwasser", CORRECTABLE_STATUS),
    ("kabelbaum", "Isolierung des internen Kabelbaums in Ordnung", "Wartung_Ew1.Flags_Allg1.fIsoIntKabelbaum", BOOLEAN_STATUS),
    ("schmieroelfilter", "Schmierölfiltereinsatz und O-Ring gewechselt", "Wartung_Ew1.Flags_Allg1.fSchmieroelfilter", BOOLEAN_STATUS),
    ("schmieroel_abgesaugt", "Schmieröl abgesaugt", "Wartung_Ew1.Flags_Allg1.fOelAbgesaugt", BOOLEAN_STATUS),
    ("luftfilter", "Luftfilter gewechselt", "Wartung_Ew1.Flags_Allg1.fLuftfilter", BOOLEAN_STATUS),
    ("generatorlager", "Generatorlager nachgefettet", "Wartung_Ew1.Flags_Allg1.fGeneratorlager", BOOLEAN_STATUS),
    ("abgaswaermetauscher", "Abgaswärmetauscher gemäß Wartungsanleitung gereinigt/geprüft", "Wartung_Ew1.Flags_Allg1.fAbgasWTauscherGer", BOOLEAN_STATUS),
    ("kondenser", "Kondenser gemäß Wartungsanleitung gewartet", "Wartung_Ew1.Flags_Allg1.fKondenser", BOOLEAN_STATUS),
    ("dichtheitskontrolle", "Dichtheitskontrolle durchgeführt", "Wartung_Ew1.Flags_Allg1.fDichtheitskontrolle", BOOLEAN_STATUS),
    ("wartungsplan", "Wartung im Wartungsplan eingetragen", "Wartung_Ew1.Flags_Allg2.fEintragWartungsplan", BOOLEAN_STATUS),
    ("heizwasserschlaeuche", "Interne/externe Heizwasserschläuche geprüft bzw. turnusgemäß getauscht", "Wartung_Ew1.Flags_Allg2.fHeizwasserschlaeuche", BOOLEAN_STATUS),
    ("abgaskompensator", "Interner/externer Abgaskompensator geprüft bzw. turnusgemäß getauscht", "Wartung_Ew1.Flags_Allg2.fAbgaskompensator", BOOLEAN_STATUS),
)

GAS_CHECKLIST = COMMON_CHECKLIST + (
    ("zuendkerze", "Zündkerze und Zündkerzenstecker geprüft bzw. gewechselt", "Wartung_Ew1.Flags_Gas.fZuendkerze", BOOLEAN_STATUS),
    ("feder_oxikat", "Abgaswärmetauscher gereinigt, Leitblech und Feder geprüft/erneuert", "Wartung_Ew1.Flags_Gas.fFederOxiKat", BOOLEAN_STATUS),
    ("gas_luft_schlauch", "Gas-Luft-Gemischschlauch geprüft bzw. turnusgemäß getauscht", "Wartung_Ew1.Flags_Gas.fGasLuftSchlauch", BOOLEAN_STATUS),
)

OIL_CHECKLIST = COMMON_CHECKLIST + (
    ("kraftstofffilter", "Interner/externer Kraftstofffilter gewechselt", "Wartung_Ew1.Flags_Oel.fKraftstofffilter", BOOLEAN_STATUS),
    ("duesenhalter", "Düsenhalter geprüft bzw. turnusgemäß gewechselt", "Wartung_Ew1.Flags_Oel.fDuesenhalter", BOOLEAN_STATUS),
    ("russfilter", "Wechselrußfilter geprüft bzw. erneuert", "Wartung_Ew1.Flags_Oel.fRussfilterGewechselt", BOOLEAN_STATUS),
    ("abschaltpruefung_hubmagnet", "Abschaltprüfung des Hubmagneten durchgeführt", "Wartung_Ew1.Flags_Oel.fAbschaltpruefHubmagnet", BOOLEAN_STATUS),
    ("spindel", "Spindel der Leistungsnachführung gefettet", "Wartung_Ew1.Flags_Oel.fSpindelGefettet", BOOLEAN_STATUS),
    ("schwimmerschalter", "Schwimmerschalter überprüft", "Wartung_Ew1.Flags_Oel.fSchwimmerschalter", BOOLEAN_STATUS),
    ("kraftstoffschlaeuche", "Interne/externe Kraftstoffschläuche geprüft bzw. turnusgemäß getauscht", "Wartung_Ew1.Flags_Oel.fKraftstoffschlaeuche", BOOLEAN_STATUS),
)

CHECKLIST_STATUS = (
    {"value": "yes", "label": "Ja / in Ordnung"},
    {"value": "no", "label": "Nein / nicht in Ordnung"},
    {"value": "corrected", "label": "Korrigiert"},
)
CHECKLIST_RAW_VALUES = {"yes": 1, "no": 0, "corrected": 2}

SUPPLEMENTAL_WORK = (
    ("snapshot_review", "Gemeinsamen Anlagenzustand und Meldungshistorien beurteilt"),
    ("safe_shutdown", "Anlage nach gültigen Sicherheits- und Wartungsvorgaben außer Betrieb genommen"),
    ("valve_measurement", "Ventilspiel und Ventilplatten gemessen und bei Bedarf eingestellt"),
    ("oil_fill", "Schmieröl-Endstand geprüft und dokumentiert"),
    ("test_run", "Probelauf und Sicherheitsfunktionen gemäß gültiger Wartungsanleitung geprüft"),
    ("parts_documented", "Verwendete Teile und Besonderheiten im lokalen Bericht dokumentiert"),
)

SUPPLEMENTAL_STATUS = (
    {"value": "done", "label": "Erledigt"},
    {"value": "not_applicable", "label": "Nicht erforderlich"},
)

MAINTENANCE_MEASUREMENTS = (
    {"key": "Wartung_Ew1.Vorher.bOelstand", "label": "Schmierölstand vor Wartung", "unit": "l"},
    {"key": "Wartung_Ew1.Nachher.bOelstand", "label": "Schmierölstand nach Wartung", "unit": "l"},
    {"key": "Wartung_Ew1.Vorher.bAbgasgegendruck", "label": "Abgasgegendruck vor Wartung", "unit": "mbar"},
    {"key": "Wartung_Ew1.Nachher.bAbgasgegendruck", "label": "Abgasgegendruck nach Wartung", "unit": "mbar"},
    {"key": "Wartung_Ew1.Vorher.bEvSpiel", "label": "Einlassventil-Spiel vor Wartung", "unit": "mm"},
    {"key": "Wartung_Ew1.Nachher.bEvSpiel", "label": "Einlassventil-Spiel nach Wartung", "unit": "mm"},
    {"key": "Wartung_Ew1.Vorher.bAvSpiel", "label": "Auslassventil-Spiel vor Wartung", "unit": "mm"},
    {"key": "Wartung_Ew1.Nachher.bAvSpiel", "label": "Auslassventil-Spiel nach Wartung", "unit": "mm"},
    {"key": "Wartung_Ew1.Vorher.bEvVentilplatte", "label": "Einlassventilplatte vor Wartung", "unit": "mm"},
    {"key": "Wartung_Ew1.Nachher.bEvVentilplatte", "label": "Einlassventilplatte nach Wartung", "unit": "mm"},
    {"key": "Wartung_Ew1.Vorher.bAvVentilplatte", "label": "Auslassventilplatte vor Wartung", "unit": "mm"},
    {"key": "Wartung_Ew1.Nachher.bAvVentilplatte", "label": "Auslassventilplatte nach Wartung", "unit": "mm"},
    {"key": "Wartung_Ew1.bAbgasgegendnachRFTausch", "label": "Abgasgegendruck nach Rußfiltertausch", "unit": "mbar"},
)
MAINTENANCE_MEASUREMENT_KEYS = frozenset(item["key"] for item in MAINTENANCE_MEASUREMENTS)

MEASUREMENT_GROUPS = (
    ("Schmierölstand", "Wartung_Ew1.Vorher.bOelstand", "Wartung_Ew1.Nachher.bOelstand", "l"),
    ("Abgasgegendruck", "Wartung_Ew1.Vorher.bAbgasgegendruck", "Wartung_Ew1.Nachher.bAbgasgegendruck", "mbar"),
    ("Ventilspiel Einlass", "Wartung_Ew1.Vorher.bEvSpiel", "Wartung_Ew1.Nachher.bEvSpiel", "mm"),
    ("Ventilspiel Auslass", "Wartung_Ew1.Vorher.bAvSpiel", "Wartung_Ew1.Nachher.bAvSpiel", "mm"),
    ("Ventilplatte Einlass", "Wartung_Ew1.Vorher.bEvVentilplatte", "Wartung_Ew1.Nachher.bEvVentilplatte", "mm"),
    ("Ventilplatte Auslass", "Wartung_Ew1.Vorher.bAvVentilplatte", "Wartung_Ew1.Nachher.bAvVentilplatte", "mm"),
    ("Abgasgegendruck nach Rußfiltertausch", None, "Wartung_Ew1.bAbgasgegendnachRFTausch", "mbar"),
)


def fuel_type_from_raw(value: object) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "unknown"
    if number in {8, 9, 10, 11}:
        return "oil"
    if number in {128, 144, 160, 176, 192, 208}:
        return "gas"
    return "unknown"


def checklist_definition(fuel_type: str) -> list[dict]:
    items = OIL_CHECKLIST if fuel_type == "oil" else GAS_CHECKLIST
    return [
        {
            "id": item_id,
            "label": label,
            "controller_key": controller_key,
            "allowed_status": list(allowed_status),
        }
        for item_id, label, controller_key, allowed_status in items
    ]


def supplemental_definition() -> list[dict]:
    return [{"id": item_id, "label": label} for item_id, label in SUPPLEMENTAL_WORK]


def maintenance_status(values: dict[str, object]) -> dict:
    """Build the dashboard traffic light from the block-104 cache values."""
    def number(key: str):
        try:
            return float(values[key])
        except (KeyError, TypeError, ValueError):
            return None

    hours = number("Wartung_Cache.sDeltaBh")
    days = number("Wartung_Cache.sDeltaTage")
    due = bool(values.get("Wartung_Cache.fStehtAn", False))
    confirmed = bool(values.get("Wartung_Cache.fBestaetigt", False))
    if hours is None and days is None and not due:
        level, title = "unknown", "Wartungsstatus noch nicht gelesen"
    elif due or (hours is not None and hours <= 0) or (days is not None and days <= 0):
        level, title = "red", "Wartung fällig"
    elif (hours is not None and hours <= 200) or (days is not None and days <= 30):
        level, title = "yellow", "Wartung nähert sich"
    else:
        level, title = "green", "Wartung im Plan"
    return {
        "level": level,
        "title": title,
        "due": due,
        "confirmed": confirmed,
        "remaining_hours": hours,
        "remaining_days": days,
        "interval_hours": number("Wartung_Cache.usIntervall"),
        "maintenance_count": number("Wartung_Cache.bZaehler"),
        "last_maintenance": values.get("Wartung_Cache.ulZeitstempel"),
        "last_maintenance_hours": number("Wartung_Cache.ulBetriebssekundenBei"),
    }


def new_protocol(fuel_type: str, block100_fields: list[dict]) -> dict:
    current = {str(item.get("key")): item.get("edit_value", item.get("value")) for item in block100_fields}
    measurements = {
        item["key"]: current.get(item["key"], "")
        for item in MAINTENANCE_MEASUREMENTS
    }
    return {
        "fuel_type": fuel_type if fuel_type in {"gas", "oil"} else "gas",
        "technician": str(current.get("Wartung_Ew1.abFaNameMonteur") or ""),
        "notes": "",
        "checklist": {},
        "supplemental": {},
        "measurements": measurements,
    }


def validate_protocol(protocol: dict, *, complete: bool = False) -> dict:
    fuel_type = str(protocol.get("fuel_type", "gas"))
    if fuel_type not in {"gas", "oil"}:
        raise ValueError("Kraftstoffart muss Gas oder Heizöl sein")
    technician = str(protocol.get("technician", "")).strip()
    if len(technician) > 19:
        raise ValueError("Monteur/Firma darf höchstens 19 Zeichen haben (Reglerfeld)")
    notes = str(protocol.get("notes", "")).strip()
    if len(notes) > 8000:
        raise ValueError("Bemerkung ist zu lang")
    checklist = protocol.get("checklist") or {}
    if not isinstance(checklist, dict):
        raise ValueError("ungültiges Wartungsprotokoll")
    definitions = checklist_definition(fuel_type)
    definitions_by_id = {item["id"]: item for item in definitions}
    clean_checklist = {}
    for key, value in checklist.items():
        item_id = str(key)
        status = str(value)
        definition = definitions_by_id.get(item_id)
        if definition is None:
            continue
        if status not in set(definition["allowed_status"]):
            raise ValueError(f"{definition['label']}: unzulässiger Wartungsstatus {status!r}")
        clean_checklist[item_id] = status
    if complete:
        if not technician:
            raise ValueError("Monteur/Firma muss vor dem Abschluss eingetragen sein")
        missing = [
            item["label"]
            for item in definitions
            if clean_checklist.get(item["id"]) not in set(item["allowed_status"])
        ]
        if missing:
            raise ValueError(f"Noch nicht bewertete Wartungspunkte: {len(missing)}")
    measurements = protocol.get("measurements") or {}
    if not isinstance(measurements, dict):
        raise ValueError("ungültige Vorher-/Nachher-Werte")
    clean_measurements = {}
    for key, value in measurements.items():
        key = str(key)
        if key not in MAINTENANCE_MEASUREMENT_KEYS:
            continue
        text = str(value).strip()
        if text:
            try:
                float(text.replace(",", "."))
            except ValueError as exc:
                raise ValueError(f"{key}: keine gültige Zahl") from exc
        clean_measurements[key] = text
    supplemental = protocol.get("supplemental") or {}
    if not isinstance(supplemental, dict):
        raise ValueError("ungültige Zusatzarbeitsliste")
    supplemental_ids = {item[0] for item in SUPPLEMENTAL_WORK}
    supplemental_values = {item["value"] for item in SUPPLEMENTAL_STATUS}
    clean_supplemental = {
        str(key): str(value)
        for key, value in supplemental.items()
        if str(key) in supplemental_ids and str(value) in supplemental_values
    }
    return {
        "fuel_type": fuel_type,
        "technician": technician,
        "notes": notes,
        "checklist": clean_checklist,
        "supplemental": clean_supplemental,
        "measurements": clean_measurements,
    }


def _field_map(snapshot: dict) -> dict[str, dict]:
    return {str(item.get("key")): item for item in snapshot.get("fields", [])}


def report_summary(report: dict) -> dict:
    blocks = report.get("blocks", {})
    info = _field_map(blocks.get("20", {}))
    operation = _field_map(blocks.get("22", {}))
    current = _field_map(blocks.get("24", {}))
    cache = _field_map(blocks.get("104", {}))
    def value(mapping, key):
        return (mapping.get(key) or {}).get("value")
    return {
        "serial_number": value(info, "Hka_Bd_Stat.uchSeriennummer"),
        "software_regler": value(info, "Hka_Bd_Stat.bSoftwareVersionRegler"),
        "software_ueberw": value(info, "Hka_Bd_Stat.bSoftwareVersionUeberw"),
        "software_messen": value(info, "Hka_Bd_Stat.bSoftwareVersionMessen"),
        "operating_hours": value(operation, "Hka_Bd.ulBetriebssekunden"),
        "starts": value(operation, "Hka_Bd.ulAnzahlStarts"),
        "fault_count": value(operation, "Hka_Bd.usAnzahlStoerungenHka"),
        "electric_work_kwh": value(operation, "Hka_Bd.ulArbeitElektr"),
        "thermal_work_kwh": value(operation, "Hka_Bd.ulArbeitThermHka"),
        "condenser_work_kwh": value(operation, "Hka_Bd.ulArbeitThermKon"),
        "motor_status": value(current, "Hka_Mw1.bMotorStatus"),
        "power_kw": value(current, "Hka_Mw1.sWirkleistung"),
        "speed_rpm": value(current, "Hka_Mw1.usDrehzahl"),
        "motor_temperature": value(current, "Hka_Mw1.Temp.sbMotor"),
        "flow_temperature": value(current, "Hka_Mw1.Temp.sbVorlauf"),
        "return_temperature": value(current, "Hka_Mw1.Temp.sbRuecklauf"),
        "maintenance_count": value(cache, "Wartung_Cache.bZaehler"),
        "last_maintenance": value(cache, "Wartung_Cache.ulZeitstempel"),
    }


def report_comparison(current: dict, previous: dict) -> dict:
    """Compare the durable counters of two archived plant snapshots."""
    current_summary = report_summary(current)
    previous_summary = report_summary(previous)
    labels = {
        "operating_hours": "Betriebsstunden",
        "starts": "Starts",
        "electric_work_kwh": "Elektrische Arbeit",
        "thermal_work_kwh": "Thermische Arbeit Dachs",
        "condenser_work_kwh": "Thermische Energie Kondenser",
    }
    rows = []
    for key, label in labels.items():
        before, after = previous_summary.get(key), current_summary.get(key)
        try:
            delta = float(after) - float(before)
            if delta.is_integer():
                delta = int(delta)
        except (TypeError, ValueError):
            delta = None
        rows.append({"key": key, "label": label, "previous": before, "current": after, "delta": delta})
    return {"rows": rows}


def _snapshot_counts(report: dict) -> tuple[int, int, int]:
    snapshot = report.get("snapshot") or {}
    captured = len(snapshot.get("captured_blocks") or report.get("blocks") or {})
    attempted = len(snapshot.get("attempted_blocks") or report.get("blocks") or {})
    failed = len(snapshot.get("failed_blocks") or [])
    return captured, attempted, failed


def _document_id(report: dict) -> str:
    generated = str(report.get("generated_at") or "")
    try:
        year = datetime.fromisoformat(generated.replace("Z", "+00:00")).year
    except ValueError:
        year = datetime.now(timezone.utc).year
    identity = f"{generated}|{report_summary(report).get('serial_number')}|{report.get('pack_rev')}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:6].upper()
    return f"ODM-W-{year}-{digest}"


def _date_text(value: object) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return parsed.astimezone().strftime("%d.%m.%Y · %H:%M")


def _text(value: object, fallback: str = "—") -> str:
    return fallback if value in (None, "") else str(value)


def _number(value: object) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _measurement_rows(protocol: dict) -> list[dict]:
    values = protocol.get("measurements") or {}
    rows = []
    for label, before_key, after_key, unit in MEASUREMENT_GROUPS:
        before = values.get(before_key, "") if before_key else ""
        after = values.get(after_key, "") if after_key else ""
        before_number, after_number = _number(before), _number(after)
        delta = "—"
        if before_number is not None and after_number is not None:
            difference = after_number - before_number
            delta = f"{difference:+g} {unit}"
        elif after not in (None, "") and before_key is None:
            delta = "Einzelwert"
        rows.append({
            "label": label,
            "before": f"{before} {unit}" if before not in (None, "") else "—",
            "after": f"{after} {unit}" if after not in (None, "") else "—",
            "delta": delta,
            "status": "erfasst" if before not in (None, "") or after not in (None, "") else "offen",
        })
    return rows


def _completion_view(completion: dict | None) -> dict:
    if not completion:
        return {
            "mode": "draft",
            "label": "ENTWURF",
            "detail": "Noch nicht abgeschlossen",
            "controller": "Keine Reglerdaten geschrieben",
            "class": "warn",
        }
    if completion.get("mode") == "demo" or completion.get("controller_written") is False:
        return {
            "mode": "demo",
            "label": "DEMO ABGESCHLOSSEN",
            "detail": "Testlauf lokal archiviert",
            "controller": "Keine Reglerdaten geschrieben · kein Bestätigungsbit gesetzt",
            "class": "demo",
        }
    return {
        "mode": "live",
        "label": "ABGESCHLOSSEN",
        "detail": "ACK und Readback bestätigt",
        "controller": "Block 100 und Bestätigungsbit Block 104 geschrieben",
        "class": "good",
    }


def report_text_lines(report: dict, protocol: dict, completion: dict | None = None) -> list[str]:
    """Compact text representation used for diagnostics and accessibility."""
    summary = report_summary(report)
    status = report.get("maintenance_status") or {}
    captured, attempted, failed = _snapshot_counts(report)
    completion_view = _completion_view(completion)
    lines = [
        f"Open Dachs Manager Wartungsnachweis {_document_id(report)}",
        f"Erfasst: {_date_text(report.get('generated_at'))}",
        f"Anlage: Dachs {'Heizöl' if protocol.get('fuel_type') == 'oil' else 'Gas'}",
        f"Seriennummer: {_text(summary.get('serial_number'))}",
        f"Snapshot: {captured}/{attempted} Blöcke, {failed} Lesefehler",
        f"Betriebsstunden: {_text(summary.get('operating_hours'))}; Starts: {_text(summary.get('starts'))}",
        f"Wartungsstatus: {_text(status.get('title'))}",
        f"Monteur/Firma: {_text(protocol.get('technician'))}",
        f"Abschluss: {completion_view['label']} – {completion_view['controller']}",
        "",
        "Regler-Prüfpunkte",
    ]
    status_labels = {item["value"]: item["label"] for item in CHECKLIST_STATUS}
    answers = protocol.get("checklist") or {}
    for item in checklist_definition(protocol.get("fuel_type", "gas")):
        lines.append(f"- {item['label']}: {status_labels.get(answers.get(item['id']), 'offen')}")
    lines.extend(["", "Ergänzende lokale Arbeiten"])
    supplemental_labels = {item["value"]: item["label"] for item in SUPPLEMENTAL_STATUS}
    supplemental = protocol.get("supplemental") or {}
    for item in supplemental_definition():
        lines.append(f"- {item['label']}: {supplemental_labels.get(supplemental.get(item['id']), 'offen')}")
    lines.extend(["", "Messwerte"])
    for row in _measurement_rows(protocol):
        lines.append(f"- {row['label']}: {row['before']} -> {row['after']} ({row['delta']})")
    lines.extend(["", "Bemerkung", _text(protocol.get("notes"))])
    return lines


_REPORT_CSS = """
@page{size:A4;margin:0}*{box-sizing:border-box}body{margin:0;background:#dce4e1;color:#15231f;font:9.1pt/1.3 Arial,sans-serif}.page{position:relative;width:210mm;min-height:297mm;margin:10mm auto;padding:10mm 11mm 14mm;background:#fff;page-break-after:always;overflow:hidden}.page:last-child{page-break-after:auto}.doc-head{display:grid;grid-template-columns:47mm 1fr 42mm;min-height:23mm;border:.45mm solid #14473d}.brand{display:flex;align-items:center;gap:3mm;padding:3mm;background:#14473d;color:#fff}.brand-mark{display:grid;place-items:center;width:13mm;height:13mm;border:.7mm solid #74d1bd;border-radius:3mm 50% 50% 50%;transform:rotate(45deg);font-size:6pt;font-weight:900}.brand-mark span{transform:rotate(-45deg)}.brand-name{font-size:10pt;font-weight:850;line-height:1.05}.brand-name small{display:block;color:#9fe0d2;font-size:6.4pt;letter-spacing:.12em}.doc-title{display:flex;flex-direction:column;justify-content:center;padding:3mm 5mm;border-right:.3mm solid #14473d}.doc-title h1{margin:0;font-size:18pt;line-height:1}.doc-title p{margin:1.5mm 0 0;color:#61706b;font-size:7.6pt}.doc-meta{display:grid;align-content:center;gap:1mm;padding:3mm;font-size:7pt}.doc-meta strong{font-size:8pt}.folio{display:flex;justify-content:space-between;margin-bottom:4mm;padding-bottom:2mm;border-bottom:.55mm solid #14473d;color:#61706b}.folio strong{color:#14473d}.band{display:grid;grid-template-columns:1.1fr .9fr .8fr 1fr;margin-top:3mm;border:.35mm solid #b9c5c1;border-left:1.6mm solid #087d68}.datum{min-height:13mm;padding:2mm 2.5mm;border-right:.25mm solid #b9c5c1}.datum:last-child{border:0}.label{display:block;margin-bottom:.7mm;color:#61706b;font-size:6.2pt;font-weight:750;letter-spacing:.06em;text-transform:uppercase}.value{font-size:9.5pt;font-weight:760}.section-title{display:flex;justify-content:space-between;align-items:baseline;margin:4mm 0 2mm;color:#14473d;font-size:10.5pt;font-weight:850}.section-title small{color:#61706b;font-size:6.6pt;font-weight:550}.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:3mm}.card{border:.3mm solid #b9c5c1;border-radius:1.3mm;overflow:hidden}.card-title{padding:1.7mm 2.4mm;background:#eef3f1;border-bottom:.25mm solid #b9c5c1;color:#14473d;font-size:7pt;font-weight:800;text-transform:uppercase}.kv{display:grid;grid-template-columns:40% 60%;min-height:6.7mm;border-bottom:.2mm solid #dce3e0}.kv:last-child{border:0}.kv>*{padding:1.4mm 2.2mm}.kv dt{margin:0;color:#61706b;font-size:7pt}.kv dd{margin:0;font-weight:700}.states{display:grid;grid-template-columns:repeat(4,1fr);gap:2mm;margin-top:3mm}.state{min-height:16mm;padding:2.2mm;border:.3mm solid #b9c5c1;border-top:1.2mm solid #087d68}.state b{display:block;font-size:15pt;line-height:1}.state p{margin:1mm 0 0;color:#61706b;font-size:6.5pt}table{width:100%;border-collapse:collapse}.data-table,.task-table{border:.3mm solid #b9c5c1;font-size:7pt}.data-table th,.task-table th{padding:1.5mm;background:#14473d;color:#fff;text-align:left;font-size:6.2pt;text-transform:uppercase}.data-table td{padding:1.35mm 1.7mm;border:.2mm solid #d8e0dd}.data-table tbody tr:nth-child(even),.task-table tbody tr:nth-child(even){background:#f6f8f7}.right{text-align:right}.badge{display:inline-block;padding:.45mm 1.4mm;border-radius:20mm;font-size:6pt;font-weight:800}.good{background:#e0f2e9;color:#19734e}.warn{background:#fff1cc;color:#9b6500}.bad{background:#f9e4e4;color:#a23939}.demo{background:#e7edff;color:#2f4c92}.neutral{background:#edf0ef;color:#4f5a57}.result{display:grid;grid-template-columns:1fr 52mm;border:.4mm solid #14473d;border-left:1.7mm solid #087d68}.result-copy{padding:2.5mm 3mm}.result-copy strong{font-size:9pt}.result-copy p{margin:1mm 0 0;color:#61706b;font-size:7pt;white-space:pre-wrap}.result-state{display:grid;place-items:center;padding:2mm;border-left:.3mm solid #14473d;text-align:center;font-size:9pt;font-weight:850}.result-state.demo{background:#e7edff}.result-state.warn{background:#fff1cc}.result-state.good{background:#e0f2e9}.audit{display:grid;grid-template-columns:1fr 7mm 1fr 7mm 1fr;align-items:center;gap:1mm}.audit-step{min-height:17mm;padding:2.2mm;background:#eef3f1;border:.3mm solid #b9c5c1}.audit-step strong{display:block;font-size:7.4pt}.audit-step span{color:#61706b;font-size:6.3pt}.arrow{text-align:center;color:#087d68;font-size:15pt;font-weight:900}.signatures{display:grid;grid-template-columns:1fr 1fr;gap:4mm;margin-top:3mm}.signature{min-height:18mm;padding:2.3mm;border:.3mm solid #b9c5c1}.signature-line{margin-top:7mm;padding-top:1mm;border-top:.25mm solid #15231f;color:#61706b;font-size:6.3pt}.task-intro{display:grid;grid-template-columns:1.2fr .8fr;gap:3mm;margin-bottom:3mm}.callout{padding:2.2mm 2.6mm;background:#dff0eb;border-left:1.3mm solid #087d68;font-size:7pt}.legend{text-align:right}.task-table{font-size:6.55pt;line-height:1.12}.task-table th{padding:1.35mm}.task-table td{padding:1mm 1.25mm;border:.2mm solid #d4ddda}.task-table .group td{background:#dce9e5;color:#14473d;font-weight:850;text-transform:uppercase}.task-table .supplement td{background:#fbfaf4}.nr{width:8mm;text-align:center;color:#61706b;font-weight:750}.source{width:25mm}.status{width:26mm}.note{width:34mm}.source-tag{color:#61706b;font-size:6pt;font-weight:700}.note-box{margin-top:3mm;padding:2mm 2.5mm;border:.3mm solid #b9c5c1;color:#61706b;font-size:6.6pt}.provenance{display:grid;grid-template-columns:repeat(4,1fr);gap:2mm}.prov{min-height:17mm;padding:2mm;background:#eef3f1;border-top:1mm solid #087d68}.prov strong{display:block;font-size:9pt}.prov small{color:#61706b;font-size:6.2pt}.sequence{display:grid;grid-template-columns:repeat(7,1fr);gap:1.3mm}.sequence div{min-height:27mm;padding:1.8mm;background:#eef3f1;border:.25mm solid #b9c5c1;font-size:6.1pt}.sequence b{display:grid;place-items:center;width:5mm;height:5mm;margin-bottom:1mm;border-radius:50%;background:#14473d;color:#fff}.split{display:grid;grid-template-columns:1fr 1fr;gap:3mm}.storage{padding:2.4mm;border:.35mm solid #b9c5c1;border-top:1.2mm solid #087d68}.storage ul{margin:1mm 0 0;padding-left:4mm;color:#61706b;font-size:6.6pt}.footer{position:absolute;right:11mm;bottom:5mm;left:11mm;display:flex;justify-content:space-between;padding-top:1.3mm;border-top:.25mm solid #b9c5c1;color:#61706b;font-size:6pt}.ribbon{position:absolute;top:7mm;right:-18mm;width:76mm;padding:1.5mm 0;transform:rotate(31deg);background:#e7edff;border:.3mm solid #8ba1d4;color:#2f4c92;text-align:center;font-size:7pt;font-weight:900;z-index:4}@media print{body{background:#fff}.page{margin:0}}
"""


def _h(value: object) -> str:
    return escape(_text(value))


def _html_header(document_id: str, page: int, title: str) -> str:
    if page == 1:
        return f"<header class='doc-head'><div class='brand'><div class='brand-mark'><span>ODM</span></div><div class='brand-name'>OPEN DACHS<small>MANAGER</small></div></div><div class='doc-title'><h1>{escape(title)}</h1><p>Digitales Anlagen- und Arbeitsprotokoll</p></div><div class='doc-meta'><span class='label'>Dokument</span><strong>{document_id}</strong><span>Revision 01 · Seite 1/3</span></div></header>"
    return f"<header class='folio'><strong>OPEN DACHS MANAGER · {document_id}</strong><span>Seite {page}/3</span></header>"


def report_html(report: dict, protocol: dict, completion: dict | None = None) -> bytes:
    summary = report_summary(report)
    status = report.get("maintenance_status") or {}
    document_id = _document_id(report)
    captured, attempted, failed = _snapshot_counts(report)
    fuel = "Heizöl" if protocol.get("fuel_type") == "oil" else "Gas"
    checklist = checklist_definition(protocol.get("fuel_type", "gas"))
    answers = protocol.get("checklist") or {}
    supplemental = protocol.get("supplemental") or {}
    status_labels = {item["value"]: item["label"] for item in CHECKLIST_STATUS}
    extra_labels = {item["value"]: item["label"] for item in SUPPLEMENTAL_STATUS}
    completion_view = _completion_view(completion)
    measurement_rows = "".join(
        f"<tr><td>{_h(row['label'])}</td><td class='right'>{_h(row['before'])}</td><td class='right'>{_h(row['after'])}</td><td class='right'>{_h(row['delta'])}</td><td><span class='badge {'good' if row['status'] == 'erfasst' else 'neutral'}'>{_h(row['status'])}</span></td></tr>"
        for row in _measurement_rows(protocol)
    )
    checklist_rows = "".join(
        f"<tr><td class='nr'>{index:02d}</td><td>{_h(item['label'])}</td><td class='source'><span class='source-tag'>MSR2 · Block 100</span></td><td class='status'><span class='badge {'good' if answers.get(item['id']) == 'yes' else 'warn' if answers.get(item['id']) == 'corrected' else 'bad' if answers.get(item['id']) == 'no' else 'neutral'}'>{_h(status_labels.get(answers.get(item['id']), 'offen'))}</span></td><td class='note'>—</td></tr>"
        for index, item in enumerate(checklist, 1)
    )
    supplemental_rows = "".join(
        f"<tr class='supplement'><td class='nr'>Z{index}</td><td>{_h(item['label'])}</td><td class='source'><span class='source-tag'>Pi-Archiv</span></td><td class='status'><span class='badge {'good' if supplemental.get(item['id']) == 'done' else 'neutral'}'>{_h(extra_labels.get(supplemental.get(item['id']), 'offen'))}</span></td><td class='note'>lokal</td></tr>"
        for index, item in enumerate(supplemental_definition(), 1)
    )
    failed_text = ", ".join(str(item.get("block")) for item in (report.get("snapshot") or {}).get("failed_blocks", [])) or "keine"
    controller_step = "Demo – übersprungen" if completion_view["mode"] == "demo" else "Ausstehend" if completion_view["mode"] == "draft" else "geschrieben + Readback"
    software = " · ".join(_text(summary.get(key)) for key in ("software_regler", "software_ueberw", "software_messen"))
    html = f"""<!doctype html><html lang='de'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>{document_id} · Wartungsnachweis</title><style>{_REPORT_CSS}</style></head><body>
<section class='page'>{"<div class='ribbon'>DEMOLAUF · KEIN REGLERWRITE</div>" if completion_view['mode'] == 'demo' else ""}{_html_header(document_id,1,'Wartungsnachweis')}
<div class='band'><div class='datum'><span class='label'>Anlage</span><span class='value'>Dachs · {fuel}</span></div><div class='datum'><span class='label'>Seriennummer</span><span class='value'>{_h(summary.get('serial_number'))}</span></div><div class='datum'><span class='label'>Wartung</span><span class='value'>Nr. {_h(summary.get('maintenance_count'))}</span></div><div class='datum'><span class='label'>Erfasst</span><span class='value'>{_h(_date_text(report.get('generated_at')))}</span></div></div>
<div class='section-title'>Anlage und Auftrag <small>Automatische Werte beim Wartungsstart eingefroren</small></div><div class='grid-2'><article class='card'><div class='card-title'>Anlagenstand</div><dl style='margin:0'><div class='kv'><dt>Anlagentyp</dt><dd>Dachs {fuel} · MSR2</dd></div><div class='kv'><dt>Softwarestände</dt><dd>{_h(software)}</dd></div><div class='kv'><dt>Wartungsstatus</dt><dd>{_h(status.get('title'))}</dd></div><div class='kv'><dt>Restzeit</dt><dd>{_h(status.get('remaining_hours'))} Bh · {_h(status.get('remaining_days'))} Tage</dd></div></dl></article><article class='card'><div class='card-title'>Wartungsauftrag</div><dl style='margin:0'><div class='kv'><dt>Monteur / Firma</dt><dd>{_h(protocol.get('technician'))}</dd></div><div class='kv'><dt>Betriebsstunden</dt><dd>{_h(summary.get('operating_hours'))} Bh</dd></div><div class='kv'><dt>Starts / Störungen</dt><dd>{_h(summary.get('starts'))} / {_h(summary.get('fault_count'))}</dd></div><div class='kv'><dt>Erstellt von</dt><dd>{_h(report.get('generated_by'))}</dd></div></dl></article></div>
<div class='states'><article class='state'><span class='label'>Anlagen-Snapshot</span><b>{captured}/{attempted}</b><p>Blöcke gemeinsam gelesen</p></article><article class='state'><span class='label'>Regler-Prüfpunkte</span><b>{len(checklist)}</b><p>für {fuel}</p></article><article class='state'><span class='label'>Zusatzarbeiten</span><b>{len(SUPPLEMENTAL_WORK)}</b><p>nur lokal dokumentiert</p></article><article class='state'><span class='label'>Messfelder</span><b>{len(MAINTENANCE_MEASUREMENTS)}</b><p>Vorher-/Nachher-Werte</p></article></div>
<div class='section-title'>Messwerte <small>Vorher-/Nachher-Vergleich</small></div><table class='data-table'><thead><tr><th>Messgröße</th><th class='right'>Vorher</th><th class='right'>Nachher</th><th class='right'>Änderung</th><th>Bewertung</th></tr></thead><tbody>{measurement_rows}</tbody></table>
<div class='section-title'>Ergebnis und Besonderheiten</div><div class='result'><div class='result-copy'><strong>{_h(completion_view['detail'])}</strong><p>{_h(protocol.get('notes'))}</p></div><div class='result-state {completion_view['class']}'>{_h(completion_view['label'])}<br><small>{_h(completion_view['controller'])}</small></div></div>
<div class='section-title'>Abschlussweg <small>Demo und echter Reglerabschluss bleiben eindeutig getrennt</small></div><div class='audit'><div class='audit-step'><strong>1 · Protokoll</strong><span>Prüfpunkte und Messwerte lokal validiert.</span></div><div class='arrow'>›</div><div class='audit-step'><strong>2 · Block 100</strong><span>{_h(controller_step)}</span></div><div class='arrow'>›</div><div class='audit-step'><strong>3 · Block 104</strong><span>{_h('Demo – Bestätigungsbit unverändert' if completion_view['mode']=='demo' else controller_step)}</span></div></div>
<div class='signatures'><div class='signature'><span class='label'>Ausführender Betrieb</span><strong>{_h(protocol.get('technician'))}</strong><div class='signature-line'>Name / digitale Freigabe / Datum</div></div><div class='signature'><span class='label'>Betreiber</span><strong>Kenntnisnahme optional</strong><div class='signature-line'>Name / Unterschrift / Datum</div></div></div><footer class='footer'><span>Open Dachs Manager · V3 {__version__} · unabhängiges Open-Source-Projekt</span><span>{document_id} · 1/3</span></footer></section>
<section class='page'>{_html_header(document_id,2,'')}<h1 style='margin:0;color:#14473d'>Arbeitsliste · Anlagenvariante {fuel}</h1><p style='color:#61706b'>Reglergestützte Prüfpunkte und ergänzende, ausschließlich lokal protokollierte Arbeitsschritte.</p><div class='task-intro'><div class='callout'><strong>Digitale Arbeitsliste</strong><br>Die Liste dokumentiert Ergebnisse, ersetzt jedoch keine gültige Wartungsanleitung oder fachliche Sicherheitsprüfung.</div><div class='legend'><span class='badge good'>Ja / i. O.</span> <span class='badge warn'>Korrigiert</span> <span class='badge bad'>Nein</span> <span class='badge neutral'>offen</span></div></div><table class='task-table'><thead><tr><th>Nr.</th><th>Arbeitsgang / Prüfumfang</th><th>Datenziel</th><th>Ergebnis</th><th>Befund</th></tr></thead><tbody><tr class='group'><td colspan='5'>A · MSR2-Prüfpunkte</td></tr>{checklist_rows}<tr class='group'><td colspan='5'>B · Ergänzende lokale Arbeiten</td></tr>{supplemental_rows}</tbody></table><div class='note-box'><strong>Hinweis:</strong> Nur die MSR2-Prüfpunkte und vorhandenen Wartungsmesswerte besitzen definierte Reglerfelder. Zusatzarbeiten, Bemerkungen und Signaturen bleiben ausschließlich im lokalen Pi-Archiv.</div><footer class='footer'><span>Open Dachs Manager · Arbeitsliste</span><span>{document_id} · 2/3</span></footer></section>
<section class='page'>{_html_header(document_id,3,'')}<h1 style='margin:0;color:#14473d'>Technischer Anlagenanhang</h1><p style='color:#61706b'>Kompakter Nachweis des gemeinsamen Anlagenzustands und des Abschlusswegs.</p><div class='provenance'><div class='prov'><span class='label'>Snapshot</span><strong>{captured}/{attempted}</strong><small>adressierbare Blöcke</small></div><div class='prov'><span class='label'>Lesefehler</span><strong>{failed}</strong><small>Blöcke: {_h(failed_text)}</small></div><div class='prov'><span class='label'>Packrevision</span><strong>{_h(report.get('pack_rev'))}</strong><small>Mappingstand</small></div><div class='prov'><span class='label'>Abschluss</span><strong>{_h(completion_view['mode'].upper())}</strong><small>{_h(completion_view['controller'])}</small></div></div>
<div class='section-title'>Ausgewählte Snapshot-Werte</div><table class='data-table'><tbody><tr><td>Motorstatus beim Start</td><td>{_h(summary.get('motor_status'))}</td><td>Generatorleistung</td><td>{_h(summary.get('power_kw'))} kW</td></tr><tr><td>Drehzahl</td><td>{_h(summary.get('speed_rpm'))} U/min</td><td>Motortemperatur</td><td>{_h(summary.get('motor_temperature'))} °C</td></tr><tr><td>Vorlauf / Rücklauf</td><td>{_h(summary.get('flow_temperature'))} / {_h(summary.get('return_temperature'))} °C</td><td>Elektrische Arbeit</td><td>{_h(summary.get('electric_work_kwh'))} kWh</td></tr><tr><td>Thermische Arbeit Dachs</td><td>{_h(summary.get('thermal_work_kwh'))} kWh</td><td>Kondenser</td><td>{_h(summary.get('condenser_work_kwh'))} kWh</td></tr></tbody></table>
<div class='section-title'>Ablauf der Wartungsroutine <small>Schreiben nur in explizit freigeschaltetem Echtbetrieb</small></div><div class='sequence'><div><b>1</b><strong>Alles lesen</strong><br>Gesamtsnapshot</div><div><b>2</b><strong>Entwurf</strong><br>lokal anlegen</div><div><b>3</b><strong>Bearbeiten</strong><br>Werte ergänzen</div><div><b>4</b><strong>Validieren</strong><br>Vollständigkeit</div><div><b>5</b><strong>Block 100</strong><br>{_h('Demo: aus' if completion_view['mode']=='demo' else 'kontrolliert')}</div><div><b>6</b><strong>Block 104</strong><br>{_h('Demo: aus' if completion_view['mode']=='demo' else 'Bestätigung')}</div><div><b>7</b><strong>Archiv</strong><br>Exporte sperren</div></div>
<div class='section-title'>Bewusste Datentrennung</div><div class='split'><div class='storage'><strong>MSR2-Regler</strong><ul><li>im Demolauf unverändert</li><li>später nur gemappte Wartungsfelder</li><li>echter Write nur mit Auth, ACK und Readback</li></ul></div><div class='storage'><strong>Lokaler Pi</strong><ul><li>vollständiger Anlagen-Snapshot</li><li>Prüfpunkte, Zusatzarbeiten und Bemerkung</li><li>HTML-, PDF- und JSON-Bericht</li></ul></div></div><div class='section-title'>Prüfvermerk</div><div class='result'><div class='result-copy'><strong>{_h(completion_view['label'])}</strong><p>Erfasst am {_h(_date_text((completion or {}).get('completed_at') or report.get('generated_at')))} durch {_h((completion or {}).get('username') or report.get('generated_by'))}.</p></div><div class='result-state {completion_view['class']}'>{_h(completion_view['controller'])}</div></div><footer class='footer'><span>Open Dachs Manager · technischer Anlagenanhang</span><span>{document_id} · 3/3</span></footer></section></body></html>"""
    return html.encode("utf-8")


def report_pdf(report: dict, protocol: dict, completion: dict | None = None) -> bytes:
    """Create the three-page compact A4 report without browser dependencies."""
    summary = report_summary(report)
    status = report.get("maintenance_status") or {}
    document_id = _document_id(report)
    captured, attempted, failed = _snapshot_counts(report)
    fuel = "Heizöl" if protocol.get("fuel_type") == "oil" else "Gas"
    completion_view = _completion_view(completion)
    checklist = checklist_definition(protocol.get("fuel_type", "gas"))
    answers = protocol.get("checklist") or {}
    supplemental = protocol.get("supplemental") or {}
    checklist_labels = {item["value"]: item["label"] for item in CHECKLIST_STATUS}
    supplemental_labels = {item["value"]: item["label"] for item in SUPPLEMENTAL_STATUS}
    palette = {
        "ink": colors.HexColor("#15231f"), "muted": colors.HexColor("#61706b"),
        "line": colors.HexColor("#b9c5c1"), "soft": colors.HexColor("#eef3f1"),
        "brand": colors.HexColor("#087d68"), "brand_dark": colors.HexColor("#14473d"),
        "good": colors.HexColor("#e0f2e9"), "warn": colors.HexColor("#fff1cc"),
        "demo": colors.HexColor("#e7edff"), "bad": colors.HexColor("#f9e4e4"),
    }
    sample = getSampleStyleSheet()
    normal = ParagraphStyle("ODMNormal", parent=sample["Normal"], fontName="Helvetica", fontSize=7.2, leading=9, textColor=palette["ink"])
    small = ParagraphStyle("ODMSmall", parent=normal, fontSize=6.1, leading=7.3, textColor=palette["muted"])
    heading = ParagraphStyle("ODMHeading", parent=sample["Heading2"], fontName="Helvetica-Bold", fontSize=11, leading=13, textColor=palette["brand_dark"], spaceBefore=7, spaceAfter=4)
    title = ParagraphStyle("ODMTitle", parent=sample["Heading1"], fontName="Helvetica-Bold", fontSize=19, leading=21, textColor=palette["brand_dark"], spaceAfter=3)
    white = ParagraphStyle("ODMWhite", parent=normal, fontName="Helvetica-Bold", textColor=colors.white)
    center = ParagraphStyle("ODMCenter", parent=normal, alignment=TA_CENTER)
    right = ParagraphStyle("ODMRight", parent=normal, alignment=TA_RIGHT)

    def p(value: object, style=normal):
        return Paragraph(escape(_text(value)), style)

    def pm(value: str, style=normal):
        """Paragraph containing only markup assembled in this function."""
        return Paragraph(value, style)

    def styled_table(data, widths, commands=(), repeat=0):
        table = Table(data, colWidths=widths, repeatRows=repeat, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.35, palette["line"]),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            *commands,
        ]))
        return table

    def page_footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(palette["line"])
        canvas.line(11 * mm, 9 * mm, A4[0] - 11 * mm, 9 * mm)
        canvas.setFont("Helvetica", 6)
        canvas.setFillColor(palette["muted"])
        canvas.drawString(11 * mm, 5.5 * mm, f"Open Dachs Manager · V3 {__version__} · unabhängiges Open-Source-Projekt")
        text = f"{document_id} · {doc.page}/3"
        canvas.drawRightString(A4[0] - 11 * mm, 5.5 * mm, text)
        if completion_view["mode"] == "demo":
            canvas.setFillColor(palette["demo"])
            canvas.rect(A4[0] - 74 * mm, A4[1] - 13 * mm, 63 * mm, 6 * mm, fill=1, stroke=0)
            canvas.setFillColor(colors.HexColor("#2f4c92"))
            canvas.setFont("Helvetica-Bold", 7)
            canvas.drawCentredString(A4[0] - 42.5 * mm, A4[1] - 10.8 * mm, "DEMOLAUF · KEIN REGLERWRITE")
        canvas.restoreState()

    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=11 * mm, leftMargin=11 * mm, topMargin=11 * mm, bottomMargin=13 * mm, title=f"{document_id} Wartungsnachweis", author="Open Dachs Manager")
    story = []
    brand_cell = Table([[p("OPEN DACHS", white)], [Paragraph("MANAGER", ParagraphStyle("brandSub", parent=white, fontSize=6.5, textColor=colors.HexColor("#9fe0d2")))]] , colWidths=[42 * mm])
    brand_cell.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), palette["brand_dark"]), ("LEFTPADDING",(0,0),(-1,-1),9), ("TOPPADDING",(0,0),(-1,-1),5), ("BOTTOMPADDING",(0,0),(-1,-1),4)]))
    header = Table([[brand_cell, Paragraph("<b>Wartungsnachweis</b><br/><font size='7' color='#61706b'>Digitales Anlagen- und Arbeitsprotokoll</font>", title), pm(f"Dokument<br/><b>{document_id}</b><br/>Revision 01 · Seite 1/3", small)]], colWidths=[43 * mm, 91 * mm, 54 * mm])
    header.setStyle(TableStyle([("BOX",(0,0),(-1,-1),0.8,palette["brand_dark"]), ("VALIGN",(0,0),(-1,-1),"MIDDLE"), ("LEFTPADDING",(1,0),(-1,-1),9), ("RIGHTPADDING",(0,0),(-1,-1),7)]))
    story.extend([header, Spacer(1, 4)])
    band = styled_table([[pm("<font size='6' color='#61706b'>ANLAGE</font><br/><b>Dachs · " + escape(fuel) + "</b>"), pm("<font size='6' color='#61706b'>SERIENNUMMER</font><br/><b>" + escape(_text(summary.get("serial_number"))) + "</b>"), pm("<font size='6' color='#61706b'>WARTUNG</font><br/><b>Nr. " + escape(_text(summary.get("maintenance_count"))) + "</b>"), pm("<font size='6' color='#61706b'>ERFASST</font><br/><b>" + escape(_date_text(report.get("generated_at"))) + "</b>")]], [48*mm,42*mm,35*mm,63*mm], [("LINEBEFORE",(0,0),(0,-1),3,palette["brand"]), ("TOPPADDING",(0,0),(-1,-1),7), ("BOTTOMPADDING",(0,0),(-1,-1),7)])
    story.extend([band, Paragraph("Anlage und Auftrag", heading)])
    software = " · ".join(_text(summary.get(key)) for key in ("software_regler", "software_ueberw", "software_messen"))
    left_data = [[p("Anlagentyp", small), p(f"Dachs {fuel} · MSR2")], [p("Softwarestände", small), p(software)], [p("Wartungsstatus", small), p(status.get("title"))], [p("Restzeit", small), p(f"{_text(status.get('remaining_hours'))} Bh · {_text(status.get('remaining_days'))} Tage")]]
    right_data = [[p("Monteur / Firma", small), p(protocol.get("technician"))], [p("Betriebsstunden", small), p(f"{_text(summary.get('operating_hours'))} Bh")], [p("Starts / Störungen", small), p(f"{_text(summary.get('starts'))} / {_text(summary.get('fault_count'))}")], [p("Erstellt von", small), p(report.get("generated_by"))]]
    cards = Table([[styled_table([[p("ANLAGENSTAND", white), ""]] + left_data, [29*mm,61*mm], [("BACKGROUND",(0,0),(-1,0),palette["brand_dark"]), ("SPAN",(0,0),(-1,0))]), styled_table([[p("WARTUNGSAUFTRAG", white), ""]] + right_data, [29*mm,61*mm], [("BACKGROUND",(0,0),(-1,0),palette["brand_dark"]), ("SPAN",(0,0),(-1,0))])]], colWidths=[92*mm,92*mm], hAlign="LEFT")
    cards.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"), ("LEFTPADDING",(0,0),(-1,-1),0), ("RIGHTPADDING",(0,0),(-1,-1),3)]))
    story.extend([cards, Spacer(1, 5)])
    states = [[p("ANLAGEN-SNAPSHOT", small), p("REGLER-PRÜFPUNKTE", small), p("ZUSATZARBEITEN", small), p("MESSFELDER", small)], [pm(f"<b><font size='15'>{captured}/{attempted}</font></b>", center), pm(f"<b><font size='15'>{len(checklist)}</font></b>", center), pm(f"<b><font size='15'>{len(SUPPLEMENTAL_WORK)}</font></b>", center), pm(f"<b><font size='15'>{len(MAINTENANCE_MEASUREMENTS)}</font></b>", center)]]
    story.append(styled_table(states, [47*mm]*4, [("BACKGROUND",(0,0),(-1,-1),palette["soft"]), ("LINEABOVE",(0,0),(-1,0),2,palette["brand"]), ("ALIGN",(0,0),(-1,-1),"CENTER")]))
    story.append(Paragraph("Messwerte", heading))
    measurement_data = [[p("Messgröße", white), p("Vorher", white), p("Nachher", white), p("Änderung", white), p("Bewertung", white)]]
    for row in _measurement_rows(protocol):
        measurement_data.append([p(row["label"]), p(row["before"], right), p(row["after"], right), p(row["delta"], right), p(row["status"])])
    story.append(styled_table(measurement_data, [61*mm,31*mm,31*mm,31*mm,34*mm], [("BACKGROUND",(0,0),(-1,0),palette["brand_dark"]), ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,palette["soft"]])], repeat=1))
    story.append(Paragraph("Ergebnis und Abschluss", heading))
    state_color = palette.get(completion_view["class"], palette["soft"])
    result = styled_table([[Paragraph(f"<b>{escape(completion_view['detail'])}</b><br/><font size='7' color='#61706b'>{escape(_text(protocol.get('notes')))}</font>", normal), Paragraph(f"<b>{escape(completion_view['label'])}</b><br/><font size='6'>{escape(completion_view['controller'])}</font>", center)]], [132*mm,56*mm], [("BACKGROUND",(1,0),(1,0),state_color), ("LINEBEFORE",(0,0),(0,-1),3,palette["brand"]), ("TOPPADDING",(0,0),(-1,-1),7), ("BOTTOMPADDING",(0,0),(-1,-1),7)])
    story.extend([result, Spacer(1, 5)])
    controller_step = "Demo: übersprungen" if completion_view["mode"] == "demo" else "noch nicht ausgeführt" if completion_view["mode"] == "draft" else "geschrieben + Readback"
    audit = styled_table([[p("1 · Protokoll", normal), p("2 · Block 100", normal), p("3 · Block 104", normal)], [p("Prüfpunkte und Messwerte lokal validiert.", small), p(controller_step, small), p("Demo: Bestätigungsbit unverändert" if completion_view["mode"] == "demo" else controller_step, small)]], [62.7*mm]*3, [("BACKGROUND",(0,0),(-1,-1),palette["soft"]), ("LINEABOVE",(0,0),(-1,0),2,palette["brand"])])
    story.extend([audit, Spacer(1, 5), styled_table([[p("Ausführender Betrieb", small), p("Betreiber / Kenntnisnahme", small)], [p(protocol.get("technician")), p("____________________________")], [p("Name / digitale Freigabe / Datum", small), p("Name / Unterschrift / Datum", small)]], [94*mm,94*mm], [("TOPPADDING",(0,1),(-1,1),8), ("BOTTOMPADDING",(0,1),(-1,1),8)])])

    story.append(PageBreak())
    story.extend([Paragraph("Arbeitsliste · Anlagenvariante " + fuel, title), p("Reglergestützte Prüfpunkte und ergänzende, ausschließlich lokal protokollierte Arbeitsschritte.", small), Spacer(1, 5)])
    task_data = [[p("Nr.", white), p("Arbeitsgang / Prüfumfang", white), p("Datenziel", white), p("Ergebnis", white), p("Befund", white)], [p("A", white), p("MSR2-Prüfpunkte", white), "", "", ""]]
    for index, item in enumerate(checklist, 1):
        answer = answers.get(item["id"])
        task_data.append([p(f"{index:02d}", center), p(item["label"], small), p("Block 100", small), p(checklist_labels.get(answer, "offen"), small), p("—", small)])
    task_data.append([p("B", white), p("Ergänzende lokale Arbeiten", white), "", "", ""])
    for index, item in enumerate(supplemental_definition(), 1):
        task_data.append([p(f"Z{index}", center), p(item["label"], small), p("Pi-Archiv", small), p(supplemental_labels.get(supplemental.get(item["id"]), "offen"), small), p("lokal", small)])
    task_commands = [("BACKGROUND",(0,0),(-1,0),palette["brand_dark"]), ("BACKGROUND",(0,1),(-1,1),palette["brand"]), ("SPAN",(1,1),(-1,1)), ("ROWBACKGROUNDS",(0,2),(-1,-1),[colors.white,palette["soft"]])]
    second_group_row = len(checklist) + 2
    task_commands.extend([("BACKGROUND",(0,second_group_row),(-1,second_group_row),palette["brand"]), ("SPAN",(1,second_group_row),(-1,second_group_row))])
    task_table = styled_table(task_data, [10*mm,91*mm,25*mm,31*mm,31*mm], task_commands, repeat=1)
    task_table.setStyle(TableStyle([("FONTSIZE",(0,0),(-1,-1),6), ("TOPPADDING",(0,0),(-1,-1),2.2), ("BOTTOMPADDING",(0,0),(-1,-1),2.2)]))
    story.extend([task_table, Spacer(1, 6), styled_table([[p("Hinweis", normal), p("Die Arbeitsliste dokumentiert den Ablauf, ersetzt aber keine gültige Wartungsanleitung, Sicherheitsprüfung oder fachliche Beurteilung. Zusatzarbeiten, Bemerkungen und Signaturen bleiben ausschließlich lokal.", small)]], [25*mm,163*mm], [("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#dff0eb")), ("LINEBEFORE",(0,0),(0,-1),3,palette["brand"])])])

    story.append(PageBreak())
    story.extend([Paragraph("Technischer Anlagenanhang", title), p("Kompakter Nachweis des gemeinsamen Anlagenzustands und des Abschlusswegs.", small), Spacer(1, 5)])
    failed_blocks = ", ".join(str(item.get("block")) for item in (report.get("snapshot") or {}).get("failed_blocks", [])) or "keine"
    provenance = [[p("SNAPSHOT", small), p("LESEFEHLER", small), p("PACKREVISION", small), p("ABSCHLUSS", small)], [pm(f"<b><font size='13'>{captured}/{attempted}</font></b>", center), pm(f"<b><font size='13'>{failed}</font></b>", center), pm(f"<b><font size='13'>{escape(_text(report.get('pack_rev')))}</font></b>", center), pm(f"<b>{escape(completion_view['mode'].upper())}</b>", center)], [p("adressierbare Blöcke", small), p("Blöcke: " + failed_blocks, small), p("Mappingstand", small), p(completion_view["controller"], small)]]
    story.extend([styled_table(provenance, [47*mm]*4, [("BACKGROUND",(0,0),(-1,-1),palette["soft"]), ("LINEABOVE",(0,0),(-1,0),2,palette["brand"]), ("ALIGN",(0,0),(-1,-1),"CENTER")]), Paragraph("Ausgewählte Snapshot-Werte", heading)])
    snapshot_data = [
        [p("Motorstatus", small), p(summary.get("motor_status")), p("Generatorleistung", small), p(f"{_text(summary.get('power_kw'))} kW")],
        [p("Drehzahl", small), p(f"{_text(summary.get('speed_rpm'))} U/min"), p("Motortemperatur", small), p(f"{_text(summary.get('motor_temperature'))} °C")],
        [p("Vorlauf / Rücklauf", small), p(f"{_text(summary.get('flow_temperature'))} / {_text(summary.get('return_temperature'))} °C"), p("Elektrische Arbeit", small), p(f"{_text(summary.get('electric_work_kwh'))} kWh")],
        [p("Thermische Arbeit", small), p(f"{_text(summary.get('thermal_work_kwh'))} kWh"), p("Kondenser", small), p(f"{_text(summary.get('condenser_work_kwh'))} kWh")],
    ]
    story.extend([styled_table(snapshot_data, [38*mm,56*mm,38*mm,56*mm], [("ROWBACKGROUNDS",(0,0),(-1,-1),[colors.white,palette["soft"]])]), Paragraph("Ablauf der Wartungsroutine", heading)])
    sequence_data = [[p(str(index), center) for index in range(1,8)], [p(label, center) for label in ("Alles lesen","Entwurf","Bearbeiten","Validieren","Block 100","Block 104","Archiv")], [p(detail, center) for detail in ("Gesamtsnapshot","lokal anlegen","Werte ergänzen","Vollständigkeit","Demo: aus" if completion_view["mode"]=="demo" else "kontrolliert","Demo: aus" if completion_view["mode"]=="demo" else "Bestätigung","Exporte sperren")]]
    story.extend([styled_table(sequence_data, [26.85*mm]*7, [("BACKGROUND",(0,0),(-1,-1),palette["soft"]), ("BACKGROUND",(0,0),(-1,0),palette["brand_dark"]), ("TEXTCOLOR",(0,0),(-1,0),colors.white), ("ALIGN",(0,0),(-1,-1),"CENTER")]), Paragraph("Bewusste Datentrennung", heading)])
    split = styled_table([[pm("<b>MSR2-Regler</b><br/>Im Demolauf unverändert. Später nur gemappte Wartungsfelder; echter Write nur mit Auth, ACK und Readback."), pm("<b>Lokaler Pi</b><br/>Vollständiger Snapshot, Prüfpunkte, Zusatzarbeiten, Bemerkungen sowie HTML-, PDF- und JSON-Bericht.")]], [94*mm,94*mm], [("BACKGROUND",(0,0),(-1,-1),palette["soft"]), ("LINEABOVE",(0,0),(-1,0),2,palette["brand"]), ("TOPPADDING",(0,0),(-1,-1),8), ("BOTTOMPADDING",(0,0),(-1,-1),8)])
    story.extend([split, Paragraph("Prüfvermerk", heading), styled_table([[pm(f"<b>{escape(completion_view['label'])}</b><br/>{escape(completion_view['detail'])}"), p(completion_view["controller"], center)]], [126*mm,62*mm], [("BACKGROUND",(1,0),(1,0),state_color), ("LINEBEFORE",(0,0),(0,-1),3,palette["brand"]), ("TOPPADDING",(0,0),(-1,-1),9), ("BOTTOMPADDING",(0,0),(-1,-1),9)])])
    doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    return output.getvalue()


def json_export(report: dict, protocol: dict, completion: dict | None = None) -> bytes:
    return (json.dumps({"report": report, "protocol": protocol, "completion": completion}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
