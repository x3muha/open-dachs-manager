"""Fail-closed email notification primitives for Open Dachs Manager.

This module owns no serial transport and performs no background work by itself.
It validates administrator settings, detects edge-triggered controller events,
renders bounded templates and sends one message through an explicitly configured
SMTP endpoint.
"""

from __future__ import annotations

from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
import hashlib
import ipaddress
import math
import os
import re
import smtplib
import socket
import ssl
import string
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from .display_status import SERVICE_CODE_TEXTS


EMAIL_NOTIFICATION_SCHEMA = "open-dachs-email-notifications/v1"
EMAIL_NOTIFICATION_CATEGORIES = (
    "maintenance",
    "fault",
    "fault_cleared",
    "temperature",
)
EMAIL_SECURITY_MODES = ("starttls", "plain-internal")
EMAIL_MAX_ATTEMPTS = 8
EMAIL_OUTBOX_MAX_ACTIVE = 500
EMAIL_OUTBOX_RETENTION_DAYS = 90
EMAIL_AUDIT_RETENTION_DAYS = 365
EMAIL_TEST_RATE_LIMIT_SECONDS = 60
EMAIL_RETRY_SECONDS = (60, 300, 900, 3600, 10800, 21600, 43200, 86400)
EMAIL_TEMPERATURE_DWELL_SAMPLES = 3
EMAIL_TEMPERATURE_COOLDOWN_SECONDS = 3600
EMAIL_TEMPLATE_FIELDS = frozenset({
    "category",
    "event",
    "recorded_at",
    "details",
    "value",
    "threshold",
    "unit",
    "url",
    "service_code",
    "service_text",
    "maintenance_title",
    "remaining_hours",
    "remaining_days",
    "sensor",
})

TEMPERATURE_SOURCES = (
    {
        "id": "coolant",
        "label": "Kühlwasser Motor",
        "block": 24,
        "key": "Hka_Mw1.Temp.sbMotor",
        "minimum_c": -50.0,
        "maximum_c": 126.0,
    },
    {
        "id": "inlet",
        "label": "Dachs-Eintritt",
        "block": 24,
        "key": "Hka_Mw1.Temp.sbGen",
        "minimum_c": -50.0,
        "maximum_c": 126.0,
    },
    {
        "id": "motor_exhaust",
        "label": "Motorabgas",
        "block": 24,
        "key": "Hka_Mw1.Temp.sAbgasMotor",
        "minimum_c": 0.1,
        "maximum_c": 600.0,
    },
    {
        "id": "dachs_exhaust",
        "label": "Dachs-Abgas",
        "block": 24,
        "key": "Hka_Mw1.Temp.sAbgasHKA",
        "minimum_c": 0.1,
        "maximum_c": 200.0,
    },
    {
        "id": "capsule",
        "label": "Kapsel",
        "block": 24,
        "key": "Hka_Mw1.Temp.sKapsel",
        "minimum_c": -50.0,
        "maximum_c": 800.0,
    },
)
TEMPERATURE_SOURCE_BY_ID = {item["id"]: item for item in TEMPERATURE_SOURCES}

DEFAULT_TEMPLATES = {
    "maintenance": {
        "subject": "[ODM] {event}",
        "body": "{event}\n\n{details}\n\nZeit: {recorded_at}\nODM öffnen: {url}",
    },
    "fault": {
        "subject": "[ODM] Fehler {service_code}: {service_text}",
        "body": "{event}\n\n{details}\n\nZeit: {recorded_at}\nODM öffnen: {url}",
    },
    "fault_cleared": {
        "subject": "[ODM] Fehler {service_code} nicht mehr aktiv",
        "body": "{event}\n\n{details}\n\nZeit: {recorded_at}\nODM öffnen: {url}",
    },
    "temperature": {
        "subject": "[ODM] Temperaturgrenze: {sensor}",
        "body": "{event}\n\n{details}\n\nZeit: {recorded_at}\nODM öffnen: {url}",
    },
}


def default_email_notification_settings() -> dict:
    return {
        "schema": EMAIL_NOTIFICATION_SCHEMA,
        "revision": 0,
        "temperature_rules_revision": 0,
        "enabled": False,
        "smtp": {
            "host": "",
            "port": 587,
            "security": "starttls",
            "username": "",
            "password": "",
            "from_address": "",
            "from_name": "Open Dachs Manager",
            "timeout_seconds": 10,
        },
        "recipients": [],
        "public_base_url": "",
        "categories": {category: False for category in EMAIL_NOTIFICATION_CATEGORIES},
        "templates": deepcopy(DEFAULT_TEMPLATES),
        "temperature_rules": {
            source["id"]: {
                "enabled": False,
                "direction": "high",
                "threshold_c": None,
                "hysteresis_c": None,
            }
            for source in TEMPERATURE_SOURCES
        },
    }


def _control_free(value: object, label: str, *, maximum: int, allow_newlines: bool = False) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError(f"{label} ist zu lang")
    forbidden = "\r" if allow_newlines else "\r\n"
    if any(character in text for character in forbidden):
        raise ValueError(f"{label} enthält unzulässige Steuerzeichen")
    if any(ord(character) < 32 and character not in ("\n", "\t") for character in text):
        raise ValueError(f"{label} enthält unzulässige Steuerzeichen")
    return text


def _secret_value(value: object) -> str:
    text = str(value or "")
    if len(text) > 1024:
        raise ValueError("SMTP-Passwort ist zu lang")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError("SMTP-Passwort enthält unzulässige Steuerzeichen")
    return text


def _email_address(value: object, label: str) -> str:
    address = _control_free(value, label, maximum=254).lower()
    if not address or not re.fullmatch(
        r"[a-z0-9.!#$%&'*+/=?^_\x60{|}~-]{1,64}@[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?",
        address,
        flags=re.IGNORECASE,
    ):
        raise ValueError(f"{label} ist keine gültige E-Mail-Adresse")
    if ".." in address or address.endswith("."):
        raise ValueError(f"{label} ist keine gültige E-Mail-Adresse")
    return address


def _smtp_host(value: object) -> str:
    host = _control_free(value, "SMTP-Server", maximum=253)
    if not host:
        return ""
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        pass
    if not re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*", candidate):
        raise ValueError("SMTP-Server muss ein Hostname oder eine IP-Adresse sein")
    return candidate.lower()


def _public_url(value: object) -> str:
    text = _control_free(value, "Öffentliche ODM-Adresse", maximum=2048)
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Öffentliche ODM-Adresse muss mit https:// beginnen")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Öffentliche ODM-Adresse darf keine Zugangsdaten, Query oder Fragment enthalten")
    port = parsed.port
    netloc = parsed.hostname
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = "/" + parsed.path.strip("/") if parsed.path.strip("/") else ""
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _template(value: object, label: str, *, subject: bool) -> str:
    text = _control_free(value, label, maximum=200 if subject else 8000, allow_newlines=not subject)
    if not text:
        raise ValueError(f"{label} darf nicht leer sein")
    formatter = string.Formatter()
    try:
        parsed = list(formatter.parse(text))
    except ValueError as exc:
        raise ValueError(f"{label} besitzt ungültige Platzhalter") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in EMAIL_TEMPLATE_FIELDS or format_spec or conversion:
            raise ValueError(f"{label}: unbekannter Platzhalter {{{field_name}}}")
    return text


def _normalize_recipients(value: object) -> list[str]:
    if isinstance(value, str):
        items = re.split(r"[,;\n]", value)
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError("Empfänger müssen eine Liste oder getrennte Adressen sein")
    recipients: list[str] = []
    for item in items:
        if not str(item).strip():
            continue
        address = _email_address(item, "Empfänger")
        if address not in recipients:
            recipients.append(address)
    if len(recipients) > 20:
        raise ValueError("höchstens 20 Empfänger sind erlaubt")
    return recipients


def normalize_email_notification_settings(
    payload: object,
    *,
    existing_password: str = "",
    strict: bool = True,
) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("E-Mail-Einstellungen müssen ein Objekt sein")
    allowed = {
        "schema", "version", "revision", "temperature_rules_revision", "enabled", "smtp", "recipients", "public_base_url",
        "categories", "templates", "temperature_rules",
        "temperature_sources", "categories_available",
    }
    if strict and set(payload) - allowed:
        raise ValueError("unbekannte E-Mail-Einstellungsfelder")
    defaults = default_email_notification_settings()
    revision = payload.get("revision", 0)
    if type(revision) is not int or not 0 <= revision <= 0x7FFFFFFF:
        raise ValueError("E-Mail-Konfigurationsrevision ist ungültig")
    temperature_rules_revision = payload.get("temperature_rules_revision", 0)
    if (
        type(temperature_rules_revision) is not int
        or not 0 <= temperature_rules_revision <= 0x7FFFFFFF
    ):
        raise ValueError("Temperaturregelrevision ist ungültig")
    enabled = payload.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled muss true oder false sein")

    smtp_payload = payload.get("smtp") or {}
    if not isinstance(smtp_payload, dict):
        raise ValueError("SMTP-Einstellungen müssen ein Objekt sein")
    smtp_allowed = {
        "host", "port", "security", "username", "password", "clear_password",
        "from_address", "from_name", "timeout_seconds", "password_configured",
    }
    if strict and set(smtp_payload) - smtp_allowed:
        raise ValueError("unbekannte SMTP-Einstellungsfelder")
    clear_password = smtp_payload.get("clear_password", False)
    if not isinstance(clear_password, bool):
        raise ValueError("clear_password muss true oder false sein")
    supplied_password = str(smtp_payload.get("password") or "")
    if supplied_password:
        password = _secret_value(supplied_password)
    elif clear_password:
        password = ""
    else:
        password = str(existing_password or "")
    port = smtp_payload.get("port", defaults["smtp"]["port"])
    timeout = smtp_payload.get("timeout_seconds", defaults["smtp"]["timeout_seconds"])
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("SMTP-Port muss zwischen 1 und 65535 liegen")
    if type(timeout) not in {int, float} or isinstance(timeout, bool) or not 3 <= float(timeout) <= 60:
        raise ValueError("SMTP-Timeout muss zwischen 3 und 60 Sekunden liegen")
    security = str(smtp_payload.get("security", defaults["smtp"]["security"]))
    if security not in EMAIL_SECURITY_MODES:
        raise ValueError("SMTP-Sicherheit ist ungültig")
    smtp = {
        "host": _smtp_host(smtp_payload.get("host", "")),
        "port": port,
        "security": security,
        "username": _control_free(smtp_payload.get("username", ""), "SMTP-Benutzer", maximum=256),
        "password": password,
        "from_address": "",
        "from_name": _control_free(
            smtp_payload.get("from_name", defaults["smtp"]["from_name"]),
            "Absendername",
            maximum=128,
        ),
        "timeout_seconds": int(timeout) if float(timeout).is_integer() else float(timeout),
    }
    from_value = smtp_payload.get("from_address", "")
    if str(from_value or "").strip():
        smtp["from_address"] = _email_address(from_value, "Absenderadresse")
    if security == "plain-internal" and (smtp["username"] or smtp["password"]):
        raise ValueError(
            "Unverschlüsseltes internes SMTP darf keine Zugangsdaten übertragen; "
            "Benutzer und gespeichertes Passwort müssen leer sein"
        )

    categories_payload = payload.get("categories") or {}
    if not isinstance(categories_payload, dict):
        raise ValueError("Kategorien müssen ein Objekt sein")
    if strict and set(categories_payload) - set(EMAIL_NOTIFICATION_CATEGORIES):
        raise ValueError("unbekannte E-Mail-Kategorie")
    categories = {}
    for category in EMAIL_NOTIFICATION_CATEGORIES:
        selected = categories_payload.get(category, False)
        if not isinstance(selected, bool):
            raise ValueError(f"Kategorie {category} muss true oder false sein")
        categories[category] = selected

    templates_payload = payload.get("templates") or {}
    if not isinstance(templates_payload, dict):
        raise ValueError("Textvorlagen müssen ein Objekt sein")
    if strict and set(templates_payload) - set(EMAIL_NOTIFICATION_CATEGORIES):
        raise ValueError("unbekannte Textvorlage")
    templates = {}
    for category in EMAIL_NOTIFICATION_CATEGORIES:
        candidate = templates_payload.get(category) or defaults["templates"][category]
        if not isinstance(candidate, dict):
            raise ValueError(f"Textvorlage {category} muss ein Objekt sein")
        if strict and set(candidate) - {"subject", "body"}:
            raise ValueError(f"Textvorlage {category} enthält unbekannte Felder")
        templates[category] = {
            "subject": _template(candidate.get("subject"), f"Betreff {category}", subject=True),
            "body": _template(candidate.get("body"), f"Text {category}", subject=False),
        }

    rules_payload = payload.get("temperature_rules") or {}
    if not isinstance(rules_payload, dict):
        raise ValueError("Temperaturregeln müssen ein Objekt sein")
    if strict and set(rules_payload) - set(TEMPERATURE_SOURCE_BY_ID):
        raise ValueError("unbekannte Temperaturregel")
    rules = {}
    for source in TEMPERATURE_SOURCES:
        candidate = rules_payload.get(source["id"]) or defaults["temperature_rules"][source["id"]]
        if not isinstance(candidate, dict):
            raise ValueError(f"Temperaturregel {source['label']} muss ein Objekt sein")
        if strict and set(candidate) - {"enabled", "direction", "threshold_c", "hysteresis_c"}:
            raise ValueError(f"Temperaturregel {source['label']} enthält unbekannte Felder")
        rule_enabled = candidate.get("enabled", False)
        if not isinstance(rule_enabled, bool):
            raise ValueError(f"Temperaturregel {source['label']}: enabled ist ungültig")
        direction = str(candidate.get("direction", "high"))
        if direction not in {"high", "low"}:
            raise ValueError(f"Temperaturregel {source['label']}: Richtung ist ungültig")
        minimum_c = float(source["minimum_c"])
        maximum_c = float(source["maximum_c"])
        threshold_value = candidate.get("threshold_c")
        threshold = None
        if threshold_value not in (None, ""):
            if isinstance(threshold_value, bool):
                raise ValueError(
                    f"Temperaturregel {source['label']}: Schwelle ist keine Zahl"
                )
            try:
                threshold = float(threshold_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Temperaturregel {source['label']}: Schwelle ist keine Zahl") from exc
            if not math.isfinite(threshold) or not minimum_c <= threshold <= maximum_c:
                raise ValueError(
                    f"Temperaturregel {source['label']}: Schwelle muss "
                    f"{minimum_c:g} bis {maximum_c:g} °C betragen"
                )
        hysteresis_value = candidate.get("hysteresis_c")
        hysteresis = None
        if hysteresis_value not in (None, ""):
            if isinstance(hysteresis_value, bool):
                raise ValueError(
                    f"Temperaturregel {source['label']}: Hysterese ist keine Zahl"
                )
            try:
                hysteresis = float(hysteresis_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Temperaturregel {source['label']}: Hysterese ist keine Zahl") from exc
            if not math.isfinite(hysteresis) or not 0.1 <= hysteresis <= 50:
                raise ValueError(f"Temperaturregel {source['label']}: Hysterese muss 0,1 bis 50 °C betragen")
        if rule_enabled and threshold is None:
            raise ValueError(f"Temperaturregel {source['label']} benötigt eine Schwelle")
        if rule_enabled and hysteresis is None:
            raise ValueError(f"Temperaturregel {source['label']} benötigt eine Hysterese")
        if threshold is not None and hysteresis is not None:
            clear_limit = (
                threshold - hysteresis if direction == "high" else threshold + hysteresis
            )
            if not minimum_c <= clear_limit <= maximum_c:
                raise ValueError(
                    f"Temperaturregel {source['label']}: Hysterese erzeugt eine "
                    "unerreichbare Rücksetzgrenze"
                )
        rules[source["id"]] = {
            "enabled": rule_enabled,
            "direction": direction,
            "threshold_c": threshold,
            "hysteresis_c": hysteresis,
        }

    recipients = _normalize_recipients(payload.get("recipients") or [])
    public_base_url = _public_url(payload.get("public_base_url", ""))
    settings = {
        "schema": EMAIL_NOTIFICATION_SCHEMA,
        "revision": revision,
        "temperature_rules_revision": temperature_rules_revision,
        "enabled": enabled,
        "smtp": smtp,
        "recipients": recipients,
        "public_base_url": public_base_url,
        "categories": categories,
        "templates": templates,
        "temperature_rules": rules,
    }
    if enabled:
        if not smtp["host"] or not smtp["from_address"] or not recipients:
            raise ValueError("Aktive E-Mail-Benachrichtigung benötigt SMTP-Server, Absender und Empfänger")
        if smtp["username"] and not smtp["password"]:
            raise ValueError("SMTP-Benutzer benötigt ein gespeichertes Passwort")
        if not public_base_url:
            raise ValueError("Aktive E-Mail-Benachrichtigung benötigt eine öffentliche ODM-Adresse")
        if not any(categories.values()):
            raise ValueError("Mindestens eine E-Mail-Kategorie muss aktiviert sein")
        if categories["temperature"] and not any(rule["enabled"] for rule in rules.values()):
            raise ValueError("Temperaturbenachrichtigung benötigt mindestens eine aktive Schwelle")
    return settings


def public_email_notification_settings(settings: dict) -> dict:
    result = deepcopy(settings)
    password = str(result["smtp"].pop("password", ""))
    result["smtp"].pop("secret_ref", None)
    result["smtp"]["password_configured"] = bool(password)
    result["temperature_sources"] = [dict(item) for item in TEMPERATURE_SOURCES]
    result["categories_available"] = list(EMAIL_NOTIFICATION_CATEGORIES)
    return result


def email_settings_ready(settings: dict) -> bool:
    smtp = settings.get("smtp") or {}
    return bool(
        smtp.get("host")
        and smtp.get("from_address")
        and settings.get("recipients")
        and settings.get("public_base_url")
        and (not smtp.get("username") or smtp.get("password"))
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: object, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def _row_index(values: list[dict]) -> dict[tuple[int, str], dict]:
    result = {}
    for row in values:
        if not isinstance(row, dict):
            continue
        try:
            identity = (int(row.get("block")), str(row.get("key") or ""))
        except (TypeError, ValueError):
            continue
        result[identity] = row
    return result


def _service_text(raw: int, exact_text: object = None) -> str:
    supplied = str(exact_text or "").strip()
    if supplied:
        return re.sub(r"^[0-9]{3}\s*", "", supplied).strip() or f"Servicecode {raw + 100}"
    # SC122/SC124 have firmware-selected 95/100 °C and 77/82 °C variants.
    # Without the fresh display-status result, never guess the active limit.
    neutral = {
        0x16: "Kühlwasser-Motortemperatur über Grenzwert",
        0x18: "Kühlwasser-Generatortemperatur über Grenzwert",
    }
    if raw in neutral:
        return neutral[raw]
    if 0 <= raw < len(SERVICE_CODE_TEXTS):
        text = str(SERVICE_CODE_TEXTS[raw]).strip()
        return re.sub(r"^[0-9]{3}\s*", "", text).strip() or f"Servicecode {raw + 100}"
    return f"Servicecode {raw + 100}"


def _maintenance_details(status: dict, title: str | None = None) -> str:
    parts = [str(title or status.get("title") or "Wartungsstatus")]
    hours = _number(status.get("remaining_hours"))
    days = _number(status.get("remaining_days"))
    if hours is not None:
        parts.append(f"Restbetriebsstunden: {hours:g} Bh")
    if days is not None:
        parts.append(f"Resttage: {days:g}")
    return "; ".join(parts)


def _maintenance_phase(status: dict) -> str:
    level = str(status.get("level") or "unknown")
    if level != "red":
        return level
    remaining = [
        value
        for value in (
            _number(status.get("remaining_hours")),
            _number(status.get("remaining_days")),
        )
        if value is not None
    ]
    return "overdue" if any(value < 0 for value in remaining) else "due"


def detect_notification_events(
    *,
    values: list[dict],
    maintenance: dict | None,
    settings: dict,
    previous_state: dict | None,
    recorded_at: str,
    manual_fault_clear: bool = False,
    service_text: str | None = None,
) -> tuple[dict, list[dict]]:
    """Return durable edge state and events; never sends or touches serial IO."""
    state = deepcopy(previous_state) if isinstance(previous_state, dict) else {}
    state.setdefault("version", 1)
    previous_categories = dict(state.get("categories") or {})
    categories = (
        dict(settings.get("categories") or {})
        if settings.get("enabled") is True
        else {category: False for category in EMAIL_NOTIFICATION_CATEGORIES}
    )
    index = _row_index(values)
    events: list[dict] = []

    service_row = index.get((22, "Hka_Bd.bStoerung"))
    service_raw = _integer((service_row or {}).get("raw"), 0, 255)
    fault_count = _integer((index.get((22, "Hka_Bd.usAnzahlStoerungenHka")) or {}).get("raw"), 0, 65535)
    fault_runtime = _integer((index.get((22, "Hka_Bd.ulBetriebssekundenLetzteStoerung")) or {}).get("raw"), 0, 0xFFFFFFFF)
    previous_fault = dict(state.get("fault") or {})
    previous_raw = _integer(previous_fault.get("raw"), 0, 255)
    if service_raw is not None:
        episode = max(0, int(previous_fault.get("episode") or 0))
        fault_transition = bool(service_raw and (service_raw != previous_raw or episode == 0))
        episode_started_at = str(previous_fault.get("episode_started_at") or "") or None
        if fault_transition:
            episode += 1
            episode_started_at = recorded_at
        fault_enabled_now = bool(categories.get("fault"))
        fault_activated = fault_enabled_now and not bool(previous_categories.get("fault"))
        current_text = _service_text(service_raw, service_text) if service_raw else None
        if service_raw and fault_enabled_now and (fault_transition or fault_activated):
            code = service_raw + 100
            text = str(current_text)
            events.append({
                "event_key": f"fault:{episode}",
                "category": "fault",
                "context": {
                    "event": f"Fehler {code} ist aktiv",
                    "details": f"{text}; Störungszähler: {fault_count if fault_count is not None else 'unbekannt'}",
                    "service_code": str(code),
                    "service_text": text,
                },
            })
        cleared_enabled = bool(categories.get("fault_cleared"))
        if previous_raw and service_raw == 0 and cleared_enabled and not manual_fault_clear:
            code = previous_raw + 100
            text = str(previous_fault.get("text") or _service_text(previous_raw))
            events.append({
                "event_key": f"fault-cleared:{episode}",
                "category": "fault_cleared",
                "context": {
                    "event": f"Fehler {code} ist nicht mehr aktiv",
                    "details": "Der Fehler verschwand ohne einen von ODM bestätigten manuellen Entstörwrite.",
                    "service_code": str(code),
                    "service_text": text,
                },
            })
        state["fault"] = {
            "raw": service_raw,
            "episode": episode,
            "episode_started_at": episode_started_at if service_raw else None,
            "text": current_text,
            "fault_count": fault_count,
            "fault_runtime_seconds": fault_runtime,
            "updated_at": recorded_at,
        }
        previous_categories["fault"] = bool(categories.get("fault"))
        previous_categories["fault_cleared"] = bool(categories.get("fault_cleared"))

    if isinstance(maintenance, dict):
        level = str(maintenance.get("level") or "unknown")
        phase = _maintenance_phase(maintenance)
        previous_phase = str((state.get("maintenance") or {}).get("phase") or "unknown")
        maintenance_enabled = bool(categories.get("maintenance"))
        maintenance_activated = maintenance_enabled and not bool(previous_categories.get("maintenance"))
        if level in {"yellow", "red"} and maintenance_enabled and (
            phase != previous_phase or maintenance_activated
        ):
            title = (
                "Wartung überfällig"
                if phase == "overdue"
                else str(maintenance.get("title") or "Wartung")
            )
            count = _number(maintenance.get("maintenance_count"))
            events.append({
                "event_key": f"maintenance:{phase}:{count if count is not None else '-'}",
                "category": "maintenance",
                "context": {
                    "event": title,
                    "details": _maintenance_details(maintenance, title),
                    "maintenance_title": title,
                    "remaining_hours": "" if maintenance.get("remaining_hours") is None else str(maintenance.get("remaining_hours")),
                    "remaining_days": "" if maintenance.get("remaining_days") is None else str(maintenance.get("remaining_days")),
                },
            })
        state["maintenance"] = {"level": level, "phase": phase, "updated_at": recorded_at}
        previous_categories["maintenance"] = bool(categories.get("maintenance"))

    temperature_state = dict(state.get("temperature") or {})
    previous_rules = dict(state.get("temperature_rules_enabled") or {})
    previous_signatures = dict(state.get("temperature_rule_signatures") or {})
    rules_revision = max(0, int(settings.get("temperature_rules_revision") or 0))
    previous_rules_revision = max(0, int(state.get("temperature_rules_revision") or 0))
    rules_revision_changed = rules_revision != previous_rules_revision
    if rules_revision_changed:
        for source_state in temperature_state.values():
            if isinstance(source_state, dict):
                source_state["active"] = False
                source_state["breach_count"] = 0
    temperature_category = bool(categories.get("temperature"))
    temperature_observed = False
    for source in TEMPERATURE_SOURCES:
        rule = dict((settings.get("temperature_rules") or {}).get(source["id"]) or {})
        rule_enabled = bool(temperature_category and rule.get("enabled"))
        row = index.get((int(source["block"]), str(source["key"])))
        value = _number((row or {}).get("value"))
        if value is not None and not float(source["minimum_c"]) <= value <= float(source["maximum_c"]):
            value = None
        previous = dict(temperature_state.get(source["id"]) or {})
        active = bool(previous.get("active"))
        sequence = int(previous.get("sequence") or 0)
        breach_count = int(previous.get("breach_count") or 0)
        last_notified_at = str(previous.get("last_notified_at") or "")
        threshold = _number(rule.get("threshold_c"))
        hysteresis = _number(rule.get("hysteresis_c"))
        direction = str(rule.get("direction") or "high")
        rule_was_enabled = bool(previous_rules.get(source["id"]))
        signature = f"{direction}:{threshold!r}:{hysteresis!r}"
        signature_changed = previous_signatures.get(source["id"]) != signature
        if value is not None:
            temperature_observed = True
            previous_rules[source["id"]] = rule_enabled
            if rule_enabled:
                previous_signatures[source["id"]] = signature
            else:
                previous_signatures.pop(source["id"], None)
                active = False
                breach_count = 0
        if (
            value is not None
            and threshold is not None
            and hysteresis is not None
            and rule_enabled
        ):
            if not rule_was_enabled or signature_changed:
                active = False
                breach_count = 0
            triggered = value >= threshold if direction == "high" else value <= threshold
            cleared = value <= threshold - hysteresis if direction == "high" else value >= threshold + hysteresis
            if not active and triggered:
                breach_count += 1
            elif not active:
                breach_count = 0
            cooldown_elapsed = True
            if last_notified_at:
                try:
                    cooldown_elapsed = (
                        datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
                        - datetime.fromisoformat(last_notified_at.replace("Z", "+00:00"))
                    ).total_seconds() >= EMAIL_TEMPERATURE_COOLDOWN_SECONDS
                except ValueError:
                    cooldown_elapsed = False
            if not active and triggered and breach_count >= EMAIL_TEMPERATURE_DWELL_SAMPLES:
                sequence += 1
                operator = "≥" if direction == "high" else "≤"
                if cooldown_elapsed:
                    events.append({
                        "event_key": f"temperature:{source['id']}:{sequence}:{recorded_at}",
                        "category": "temperature",
                        "context": {
                            "event": f"{source['label']} erreicht die konfigurierte Grenze",
                            "details": f"{source['label']}: {value:g} °C {operator} {threshold:g} °C; Hysterese {hysteresis:g} °C; {EMAIL_TEMPERATURE_DWELL_SAMPLES} frische Messungen",
                            "value": f"{value:g}",
                            "threshold": f"{threshold:g}",
                            "unit": "°C",
                            "sensor": source["label"],
                        },
                    })
                    last_notified_at = recorded_at
                active = True
                breach_count = 0
            elif active and cleared:
                active = False
                breach_count = 0
        temperature_state[source["id"]] = {
            "active": active,
            "sequence": sequence,
            "breach_count": breach_count,
            "last_notified_at": last_notified_at or None,
            "value": value,
            "updated_at": recorded_at,
        }
    state["temperature"] = temperature_state
    state["temperature_rules_enabled"] = previous_rules
    state["temperature_rule_signatures"] = previous_signatures
    state["temperature_rules_revision"] = rules_revision
    if temperature_observed:
        previous_categories["temperature"] = temperature_category
    state["categories"] = {
        category: bool(previous_categories.get(category))
        for category in EMAIL_NOTIFICATION_CATEGORIES
    }
    state["updated_at"] = recorded_at
    return state, events


def notification_context(event: dict, *, recorded_at: str, public_base_url: str) -> dict:
    context = {field: "" for field in EMAIL_TEMPLATE_FIELDS}
    context.update({
        "category": str(event.get("category") or ""),
        "recorded_at": recorded_at,
        "url": public_base_url.rstrip("/") + "/" if public_base_url else "",
    })
    supplied = event.get("context") or {}
    for key, value in supplied.items():
        if key in context:
            context[key] = "" if value is None else str(value)
    return context


def render_notification(event: dict, settings: dict, *, recorded_at: str) -> dict:
    category = str(event.get("category") or "")
    if category not in EMAIL_NOTIFICATION_CATEGORIES:
        raise ValueError("unbekannte Benachrichtigungskategorie")
    context = notification_context(
        event,
        recorded_at=recorded_at,
        public_base_url=str(settings.get("public_base_url") or ""),
    )
    template = settings["templates"][category]
    subject = template["subject"].format_map(context)
    body = template["body"].format_map(context)
    return {
        **event,
        "created_at": recorded_at,
        "subject": _control_free(subject, "Gerenderter Betreff", maximum=200),
        "body": _control_free(body, "Gerenderter Nachrichtentext", maximum=8000, allow_newlines=True),
        "context": context,
    }


def smtp_error_policy(exc: Exception) -> str:
    """Return retry, dead or pause; pause lasts until config revision changes."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = []
        for _recipient, detail in (exc.recipients or {}).items():
            try:
                codes.append(int(detail[0]))
            except (TypeError, ValueError, IndexError):
                return "pause"
        if codes and all(400 <= code < 500 for code in codes):
            return "retry"
        return "dead"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "pause"
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError, smtplib.SMTPNotSupportedError, ValueError)):
        return "pause"
    if isinstance(exc, smtplib.SMTPResponseException):
        code = int(getattr(exc, "smtp_code", 0) or 0)
        return "retry" if 400 <= code < 500 else "dead"
    if isinstance(exc, (TimeoutError, OSError, smtplib.SMTPServerDisconnected)):
        return "retry"
    return "pause"


def sanitized_smtp_error(exc: Exception, settings: dict) -> str:
    """Return a bounded operator message without relay text that may echo secrets."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = sorted({
            int(detail[0])
            for detail in (exc.recipients or {}).values()
            if isinstance(detail, (tuple, list)) and detail
            and str(detail[0]).isdigit()
        })
        text = f"SMTPRecipientsRefused (SMTP {','.join(map(str, codes)) or 'unbekannt'})"
    elif isinstance(exc, smtplib.SMTPResponseException):
        code = int(getattr(exc, "smtp_code", 0) or 0)
        text = f"{exc.__class__.__name__} (SMTP {code or 'unbekannt'})"
    else:
        text = str(exc) or exc.__class__.__name__
    smtp = settings.get("smtp") if isinstance(settings, dict) else {}
    for secret in (
        str((smtp or {}).get("password") or ""),
        str((smtp or {}).get("username") or ""),
    ):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = "".join(character if character >= " " else " " for character in text)
    return text[:1000]


def _smtp_private_allowlist() -> set[str]:
    return {
        item.strip().casefold()
        for item in os.environ.get("OPEN_DACHS_SMTP_PRIVATE_ALLOWLIST", "").split(",")
        if item.strip()
    }


def _smtp_plain_allowlist() -> set[str]:
    return {
        item.strip().casefold()
        for item in os.environ.get("OPEN_DACHS_SMTP_PLAIN_ALLOWLIST", "").split(",")
        if item.strip()
    }


def resolve_smtp_target(
    host: str,
    port: int,
    *,
    resolver: Callable = socket.getaddrinfo,
    private_allowlist: set[str] | None = None,
    require_private: bool = False,
    allowlist_name: str = "OPEN_DACHS_SMTP_PRIVATE_ALLOWLIST",
) -> str:
    try:
        rows = resolver(host, int(port), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"SMTP-Server kann nicht aufgelöst werden: {exc}") from exc
    addresses = sorted({str(row[4][0]).split("%", 1)[0] for row in rows})
    if not addresses:
        raise ValueError("SMTP-Server liefert keine Adresse")
    allowlist = private_allowlist if private_allowlist is not None else _smtp_private_allowlist()
    allowed_private = f"{host}:{int(port)}".casefold() in allowlist
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("SMTP-DNS-Antwort enthält keine gültige IP-Adresse") from exc
        if require_private and parsed.is_global:
            raise ValueError(
                "Unverschlüsseltes SMTP ist ausschließlich zu einem internen, "
                "nicht öffentlich routbaren Relay erlaubt"
            )
        if not parsed.is_global and not allowed_private:
            raise ValueError(
                "SMTP-Ziel ist nicht öffentlich; private Relays benötigen "
                f"eine rootverwaltete {allowlist_name}"
            )
    return addresses[0]


def stable_message_id(event_key: str, recipient: str, from_address: str) -> str:
    digest = hashlib.sha256(
        f"{event_key}\x00{recipient.casefold()}".encode("utf-8")
    ).hexdigest()[:32]
    domain = from_address.split("@", 1)[1]
    return f"<odm-{digest}@{domain}>"


def send_smtp_email(
    settings: dict,
    *,
    subject: str,
    body: str,
    message_id: str | None = None,
    smtp_factory: Callable | None = None,
    resolver: Callable = socket.getaddrinfo,
    private_allowlist: set[str] | None = None,
    plain_allowlist: set[str] | None = None,
) -> dict:
    if not email_settings_ready(settings):
        raise ValueError("SMTP, Absender, Empfänger und öffentliche ODM-Adresse sind nicht vollständig")
    smtp = settings["smtp"]
    subject = _control_free(subject, "Betreff", maximum=200)
    body = _control_free(body, "Nachricht", maximum=8000, allow_newlines=True)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((str(smtp.get("from_name") or ""), smtp["from_address"]))
    if len(settings["recipients"]) != 1:
        raise ValueError("SMTP-Dispatcher erwartet genau einen Empfänger pro Outbox-Eintrag")
    recipient = settings["recipients"][0]
    message["To"] = recipient
    supplied_message_id = str(message_id or make_msgid(
        domain=smtp["from_address"].split("@", 1)[1]
    ))
    message["Message-ID"] = _control_free(
        supplied_message_id, "Message-ID", maximum=254
    )
    message.set_content(body)

    host = smtp["host"]
    port = int(smtp["port"])
    timeout = float(smtp["timeout_seconds"])
    security = str(smtp.get("security") or "starttls")
    if security not in EMAIL_SECURITY_MODES:
        raise ValueError("SMTP-Sicherheit ist ungültig")
    if security == "plain-internal":
        if smtp.get("username") or smtp.get("password"):
            raise ValueError(
                "Unverschlüsseltes internes SMTP darf keine Zugangsdaten übertragen"
            )
        selected_allowlist = (
            plain_allowlist if plain_allowlist is not None else _smtp_plain_allowlist()
        )
        target = resolve_smtp_target(
            host,
            port,
            resolver=resolver,
            private_allowlist=selected_allowlist,
            require_private=True,
            allowlist_name="OPEN_DACHS_SMTP_PLAIN_ALLOWLIST",
        )
    else:
        target = resolve_smtp_target(
            host, port, resolver=resolver, private_allowlist=private_allowlist
        )
    context = None
    if security == "starttls":
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    if smtp_factory is None:
        client = smtplib.SMTP(timeout=timeout)
        client.connect(target, port)
    else:
        client = smtp_factory(target, port, timeout=timeout)
    # SMTP.starttls() uses _host for SNI and hostname verification. Connect
    # to the prevalidated exact IP, then bind TLS identity to the configured
    # hostname to avoid DNS-rebinding between policy check and socket open.
    client._host = host
    relay_accepted = False
    try:
        client.ehlo()
        if security == "starttls":
            if hasattr(client, "has_extn") and not client.has_extn("starttls"):
                raise smtplib.SMTPNotSupportedError("SMTP-Server bietet kein STARTTLS")
            client.starttls(context=context)
            client.ehlo()
            if smtp.get("username"):
                client.login(smtp["username"], smtp["password"])
        refused = client.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        relay_accepted = True
    finally:
        try:
            client.quit()
        except Exception:
            with suppress(Exception):
                client.close()
    if not relay_accepted:
        raise RuntimeError("SMTP-Relay hat die Nachricht nicht bestätigt")
    return {
        "sent": True,
        "recipient_count": 1,
        "message_id": str(message["Message-ID"]),
        "relay_accepted": True,
        "security": security,
    }
