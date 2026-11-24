"""Dependency-light web interface for Open Dachs Manager.

The web layer deliberately sits above :mod:`open_dachs_manager.service`: reads,
authentication, guarded writes and readback keep the same semantics as the
CLI/TUI.  Production access goes through the shared FIFO serial worker, so
separate processes never open or fight over the optical adapter.
"""

from __future__ import annotations

import argparse
import base64
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import heapq
import hashlib
import hmac
import http.server
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlparse
import zlib
from zoneinfo import ZoneInfo

from . import __version__
from .diagnosis_250h import Diagnosis250hCache, build_250h_diagnosis
from .display_status import (
    DISPLAY_STATUS_BLOCK_INPUTS,
    DISPLAY_STATUS_FAST_BLOCKS,
    DISPLAY_STATUS_RAM_ADDRESSES,
    DISPLAY_STATUS_SLOW_BLOCKS,
    compute_display_status,
    display_status_unavailable,
    inputs_from_payloads,
    ram_reads_needed,
)
from .firmware import MUC_MAX_FILE_BYTES, inspect_muc
from .auth import (
    auth_inputs_from_payloads,
    calculate_pw4,
    validate_auth_level as _validated_auth_level,
)
from .maintenance import (
    CHECKLIST_RAW_VALUES,
    CHECKLIST_STATUS,
    MAINTENANCE_CACHE_KEYS,
    MAINTENANCE_CONFIRMATION,
    MAINTENANCE_DEMO_CONFIRMATION,
    MAINTENANCE_MEASUREMENT_KEYS,
    MAINTENANCE_MEASUREMENTS,
    MAINTENANCE_REQUIRED_BLOCKS,
    SUPPLEMENTAL_STATUS,
    checklist_definition,
    FUEL_TYPE_ADDRESS,
    FUEL_TYPE_KEY,
    fuel_type_from_block24,
    maintenance_fuel_check,
    json_export,
    maintenance_status,
    new_protocol,
    report_html,
    report_comparison,
    report_pdf,
    report_summary,
    supplemental_definition,
    validate_protocol,
)
from .mapping import PackRepository, WriteAllowlist, is_reserved_key
from .email_notifications import (
    EMAIL_AUDIT_RETENTION_DAYS,
    EMAIL_MAX_ATTEMPTS,
    EMAIL_OUTBOX_MAX_ACTIVE,
    EMAIL_OUTBOX_RETENTION_DAYS,
    EMAIL_RETRY_SECONDS,
    EMAIL_TEST_RATE_LIMIT_SECONDS,
    default_email_notification_settings,
    detect_notification_events,
    email_settings_ready,
    normalize_email_notification_settings,
    public_email_notification_settings,
    render_notification,
    sanitized_smtp_error,
    send_smtp_email,
    smtp_error_policy,
    stable_message_id,
)
from .network_protection import (
    NETWORK_PROTECTION_BACKUP_BLOCKS,
    NETWORK_PROTECTION_BLOCK,
    NETWORK_PROTECTION_CPUS,
    decode_network_protection,
    encode_network_protection_value,
    network_protection_name,
    network_protection_payload_length,
    network_protection_schema,
    network_protection_schemas,
    validate_network_block,
    validate_network_cpu,
)
from .phase6_verify import SLOW_FAST_CYCLE_SECONDS, Phase6Verifier
from .serial_worker import DEFAULT_SERIAL_WORKER_SOCKET
from .service import (
    BACKUP_LEGACY_TARGETS,
    BACKUP_PAYLOAD_LENGTHS,
    BACKUP_RESTORE_TARGETS,
    BACKUP_TARGETS,
    DachsService,
    restore_target_preserving_firmware_owned,
    sc123_firmware_owned_offsets,
    validate_generic_write_payload,
    write_json_atomic,
    write_json_exclusive,
)
from .transport import TransportError, validate_block
from .updater import UpdateManager
from .controller_automation import (
    CLOCK_SYNC_AUTH_LEVEL,
    CLOCK_SYNC_BLOCK,
    CLOCK_SYNC_COMMAND_BIT,
    CLOCK_SYNC_COMMAND_OFFSET,
    CLOCK_SYNC_DEFAULTS,
    CLOCK_SYNC_GUARD_OFFSET,
    CLOCK_SYNC_GUARD_REFUSED_VALUE,
    CLOCK_SYNC_PAYLOAD_LENGTH,
    CLOCK_SYNC_READBACK_TOLERANCE_SECONDS,
    CLOCK_SYNC_TIME_OFFSET,
    REAL_RUNTIME_RUNNING_STATUS,
    REAL_RUNTIME_SAMPLE_WINDOW_SECONDS,
    RUNTIME_AUTO_DEFAULTS,
    RUNTIME_AUTO_MAX_RESETS,
    RUNTIME_AUTO_MAX_THRESHOLD_SECONDS,
    RUNTIME_AUTO_MIN_GAP_SECONDS,
    RUNTIME_AUTO_RATE_LIMIT_SECONDS,
    RUNTIME_AUTO_UNIVERSAL_TARGET_SECONDS,
    clock_sync_due,
    clock_sync_payload,
    clock_sync_window_reason,
    controller_naive,
    controller_raw,
    decode_controller_time,
    next_clock_sync,
    night_shutdown_second,
    normalize_clock_sync_settings,
    normalize_runtime_auto_rule,
    odm_local_naive,
    real_runtime_status,
    runtime_auto_eligibility,
    runtime_auto_min_target,
    runtime_auto_precheck,
    stable_block50_equal,
)
from .sc123_limit import (
    SC123_LIMIT_AUTH_LEVEL,
    SC123_LIMIT_CAPABILITY_BLOCK,
    SC123_LIMIT_NVM_POLL_ATTEMPTS,
    SC123_LIMIT_NVM_POLL_DELAY_SECONDS,
    SC123_LIMIT_NVM_READ_REQUEST,
    SC123_LIMIT_PAYLOAD_LENGTH,
    SC123_LIMIT_RECORD_LENGTH,
    SC123_LIMIT_RECORD_OFFSET,
    SC123_LIMIT_RETRY_GUARD_SECONDS,
    SC123_LIMIT_SCHEMA,
    SC123_LIMIT_STORAGE_BLOCK,
    SC123_LIMIT_THRESHOLD_OFFSET,
    SC123_LIMIT_TOKEN_TTL_SECONDS,
    parse_sc123_limit_c,
    sc123_limit_capability,
    sc123_limit_nvm_record,
    sc123_limit_payload,
    sc123_limit_pending_commands,
    sc123_limit_readback,
)


WEB_DIR = Path(__file__).with_name("web")
OPERATING_HOURS_PER_START_BLOCK = 38
OPERATING_HOURS_PER_START_KEY = "Motbel250[0].bQuotientBh_Start"
DEFAULT_MONITOR_BLOCKS = (20, 22, 24, 26, 38, 50, 104)
DEFAULT_FAST_MONITOR_BLOCKS = (22, 24, 26)
DEFAULT_SLOW_MONITOR_BLOCKS = (20, 38, 50, 104)
DEFAULT_SLOW_MONITOR_INTERVAL = 10.0
DEFAULT_HISTORY_SAMPLE_INTERVAL = 0.0
RUNTIME_24H_COUNTER_ADDRESS = 0x0F0E
RUNTIME_24H_COUNTER_LENGTH = 4
RUNTIME_24H_SHUTDOWN_SECONDS = 0x15162
# 0x0f0e is Hka_Mw1.ulMotorlaufsekunden ("Laufzeit seit Start"). From 6 h on
# the firmware's daily night shutdown (03:00 + Starts mod 100 min controller
# time) can clear release bit 3; read-only display threshold, never a write gate.
RUNTIME_NIGHT_SHUTDOWN_GATE_SECONDS = 0x5460
RUNTIME_24H_MAX_SETTABLE_SECONDS = RUNTIME_24H_SHUTDOWN_SECONDS - 1
RUNTIME_24H_PREWRITE_TOLERANCE_SECONDS = 30
RUNTIME_24H_READBACK_TOLERANCE_SECONDS = 1
RUNTIME_24H_WRITE_AUTH_LEVEL = 3
RUNTIME_24H_REQUEST = bytes([
    0x02,
    (RUNTIME_24H_COUNTER_ADDRESS >> 8) & 0xFF,
    RUNTIME_24H_COUNTER_ADDRESS & 0xFF,
    RUNTIME_24H_COUNTER_LENGTH,
])
RUNTIME_24H_WRITE_PREFIX = bytes([
    0x03,
    (RUNTIME_24H_COUNTER_ADDRESS >> 8) & 0xFF,
    RUNTIME_24H_COUNTER_ADDRESS & 0xFF,
    RUNTIME_24H_COUNTER_LENGTH,
])
# Manual service-code acknowledgement.  The live-confirmed firmware contract
# is one volatile CPU-0 RAM write; it is deliberately kept separate from all
# mapped block/NVM writes and cannot be parameterised by an HTTP client.
SERVICE_RESET_SCHEMA = "open-dachs-servicecode-reset/v1"
SERVICE_RESET_ADDRESS = 0x1C9E
SERVICE_RESET_AUTH_LEVEL = 3
SERVICE_RESET_REQUEST = bytes([0x03, 0x1C, 0x9E, 0x01, 0x01])
SERVICE_RESET_RESPONSE = bytes([0x83, 0x1C, 0x9E, 0x01])
SERVICE_RESET_SAFE_MOTOR_STATUSES = frozenset({15, 16})
SERVICE_RESET_TEMPERATURE_COHERENCE_C = 2.0
SERVICE_RESET_MAX_STATE_READ_SECONDS = 10.0
SERVICE_RESET_MAX_PREWRITE_AGE_SECONDS = 1.0
SERVICE_RESET_READBACK_ATTEMPTS = 6
SERVICE_RESET_READBACK_DELAY_SECONDS = 0.25
SERVICE_RESET_STABLE_ZERO_SAMPLES = 2
SERVICE_RESET_TOKEN_TTL_SECONDS = 30.0
SERVICE_RESET_RETRY_GUARD_SECONDS = 30.0
# Conservative standstill limits below the evidenced firmware trip points.
# A broken/unavailable sensor blocks acknowledgement instead of being ignored.
SERVICE_RESET_TEMPERATURE_LIMITS = (
    ("Hka_Mw1.Temp.sbMotor", "Kühlwasser Motor", 0.0, 90.0),
    ("Hka_Mw1.Temp.sbGen", "Dachs-Eintritt", 0.0, 72.0),
    ("Hka_Mw1.Temp.sAbgasMotor", "Motorabgas", 0.0, 450.0),
    ("Hka_Mw1.Temp.sAbgasHKA", "Dachs-Abgas", 0.0, 170.0),
    ("Hka_Mw1.Temp.sKapsel", "Kapsel", 0.0, 110.0),
)
# Manual Dachs on/off control.  This is the firmware's volatile operator
# release bit (fTasteOnOff), not a forced motor start and not an emergency
# stop.  The browser can choose only the semantic action; CPU, address, mask,
# authentication level and telegrams remain fixed server-side.
DACHS_CONTROL_SCHEMA = "open-dachs-on-off/v1"
DACHS_CONTROL_ADDRESS = 0x0EA1
DACHS_CONTROL_LENGTH = 1
DACHS_CONTROL_MASK = 0x40
DACHS_CONTROL_AUTH_LEVEL = 3
DACHS_CONTROL_READ_REQUEST = bytes([0x02, 0x0E, 0xA1, 0x01])
DACHS_CONTROL_WRITE_PREFIX = bytes([0x03, 0x0E, 0xA1, 0x01])
DACHS_CONTROL_WRITE_RESPONSE = bytes([0x83, 0x0E, 0xA1, 0x01])
DACHS_CONTROL_MANUAL_MODE_ADDRESS = 0x1B5A
DACHS_CONTROL_MANUAL_MODE_REQUEST = bytes([0x02, 0x1B, 0x5A, 0x01])
DACHS_CONTROL_RELEASE_FIELD = "Hka_Bd.UHka_Frei.usFreigabe"
DACHS_CONTROL_RELEASE_WORD_MASK = 0x4000
DACHS_CONTROL_SAFE_START_STATUSES = SERVICE_RESET_SAFE_MOTOR_STATUSES
DACHS_CONTROL_TOKEN_TTL_SECONDS = 30.0
DACHS_CONTROL_RETRY_GUARD_SECONDS = 30.0
# Read-only CPU-0 RAM telemetry over service 0x02. This fixed allowlist is the
# only set of addresses/lengths the slow poller may request; each entry comes
# from reports/msr2-ram-telemetrie-kandidaten-2026-10-24.md.
RAM_TELEMETRY_FIELDS = (
    {
        "key": "CPU0.RAM.0x00222e",
        "address": 0x222E,
        "length": 2,
        "signed": False,
        "label": "Internes Leistungsziel",
        "unit": "W",
        "confidence": "hoch",
        "note": "Ergebnis der aktuell wirksamen internen Leistungsbegrenzung",
    },
    {
        "key": "CPU0.RAM.0x000dca",
        "address": 0x0DCA,
        "length": 2,
        "signed": True,
        "label": "Abgas-Regelkorrektur",
        "unit": "W",
        "confidence": "mittel",
        "note": "Additive Motorabgas-Korrektur; Vorzeichenwirkung hängt vom noch nicht benannten Zustand 0x000db3 ab",
    },
    {
        "key": "CPU0.RAM.0x001ad4",
        "address": 0x1AD4,
        "length": 2,
        "signed": False,
        "label": "Verfügbare Leistungsgrenze",
        "unit": "W",
        "confidence": "hoch",
        "note": "Vorgelagerte Obergrenze P0; kann durch Schutz-/Deratingpfade reduziert sein",
    },
)
RAM_TELEMETRY_SCHEMA = "open-dachs-ram-telemetry/v1"
# Read-only soot-filter regeneration indicator (CPU-0 RAM, service 0x02).
# Addresses, widths and thresholds come from
# reports/russabbrand-anzeigeindikator-firmwareanalyse-2026-10-24.md. The
# whole feature is gated by the Dachs type code 0x000f0a bit 3 (oil type with
# soot filter); that byte is already mapped as Block 24 offset 54
# ``Hka_Mw1.bKraftstofftyp`` (block base 0x0ed4: sbMotor offset 14 = 0x0ee2,
# sAbgasMotor offset 16 = 0x0ee4), so it is not read a second time.
SOOT_REGENERATION_FIELDS = (
    {
        "key": "CPU0.RAM.0x000db3",
        "address": 0x0DB3,
        "length": 1,
        "signed": False,
        "label": "Regenerationszustand",
        "unit": "",
        "confidence": "hoch",
        "note": "0 inaktiv, 1 Regeneration läuft, 2 durch Abschaltung unterbrochen",
    },
    {
        "key": "CPU0.RAM.0x000de6",
        "address": 0x0DE6,
        "length": 2,
        "signed": False,
        "label": "Regenerationsminuten",
        "unit": "min",
        "confidence": "hoch",
        "note": "Byte 0: Betriebsminuten seit letzter Regeneration (0..180); Byte 1: Betriebsminuten in der Regeneration (0..12)",
    },
    {
        "key": "CPU0.RAM.0x000d82",
        "address": 0x0D82,
        "length": 1,
        "signed": False,
        "label": "Unterbrechungszähler",
        "unit": "",
        "confidence": "hoch",
        "note": "Bits 0..6 aufeinanderfolgende Unterbrechungen, Bit 7 Latch für den aktuellen Stillstand",
    },
)
SOOT_REGENERATION_SCHEMA = "open-dachs-soot-regeneration/v1"
SOOT_REGENERATION_TYPE_KEY = "Hka_Mw1.bKraftstofftyp"
SOOT_REGENERATION_TYPE_BLOCK = 24
SOOT_REGENERATION_TYPE_BIT = 3
SOOT_REGENERATION_INTERVAL_MINUTES = 180
SOOT_REGENERATION_DURATION_MINUTES = 12
SOOT_REGENERATION_INTERRUPTION_LIMIT = 5
SOOT_REGENERATION_STATE_LABELS = {
    0: "inaktiv",
    1: "Regeneration läuft",
    2: "unterbrochen, wird beim nächsten Lauf fortgesetzt",
}
# Read-only inputs of the MMI start-page status lines that no polled block
# carries (reports/display-statuszeilen-firmwareanalyse-2026-10-24.md, read
# plan variant A). 0x0db3 is the same request as the soot-regeneration read
# and is reused from it whenever that read ran in the same cycle.
DISPLAY_STATUS_RAM_FIELDS = (
    SOOT_REGENERATION_FIELDS[0],
    {
        "key": "CPU0.RAM.0x000f96",
        "address": 0x0F96,
        "length": 1,
        "signed": False,
        "label": "Warmwasser-Desinfektion",
        "unit": "",
        "confidence": "hoch",
        "note": "Bit 1: thermische Warmwasser-Desinfektion aktiv (Statuszeile 2)",
    },
    {
        "key": "CPU0.RAM.0x001bcc",
        "address": 0x1BCC,
        "length": 1,
        "signed": False,
        "label": "Platine getauscht",
        "unit": "",
        "confidence": "hoch",
        "note": "== 1: Statuszeile 1 „Platine getauscht !“",
    },
    {
        "key": "CPU0.RAM.0x001c9f",
        "address": 0x1C9F,
        "length": 1,
        "signed": False,
        "label": "Wartezustand",
        "unit": "",
        "confidence": "hoch",
        "note": "nur bei Servicecode: == 1 Wartezustand, sonst Anlagenstörung (Statuszeile 1)",
    },
    {
        "key": "CPU0.RAM.0x001b5c",
        "address": 0x1B5C,
        "length": 1,
        "signed": False,
        "label": "Servicecode-Alternativtext",
        "unit": "",
        "confidence": "hoch",
        "note": "nur bei Servicecode 122/124: Bit 0 wählt den erhöhten Grenzwerttext",
    },
    {
        "key": "CPU0.RAM.0x001ed4",
        "address": 0x1ED4,
        "length": 1,
        "signed": False,
        "label": "Busfehler",
        "unit": "",
        "confidence": "mittel",
        "note": "nur im Mehrmodul-Slavefall mit Anforderung: == 1 Busfehler Notbetrieb",
    },
)
DISPLAY_STATUS_RAM_FIELD_BY_ADDRESS = {
    int(field["address"]): field for field in DISPLAY_STATUS_RAM_FIELDS
}
DISPLAY_STATUS_EVENT_LIMIT = 50
# Controller automation (clock sync, 0x0f0e auto-reset, real runtime).
CONTROLLER_AUTOMATION_USER = "system-automation"
AUTOMATION_TICK_SECONDS = 5.0
RUNTIME_AUTO_MIN_RUNTIME_OFFSET = 3       # Block 50 Hka_Ew.bMindestlaufzeit = RAM 0x1acf
RUNTIME_AUTO_COUNTER_MAX_AGE_SECONDS = 30.0
RUNTIME_AUTO_BACKOFF_SECONDS = 60.0
REAL_RUNTIME_LIVE_MAX_AGE_SECONDS = 10.0
REAL_RUNTIME_CACHE_SECONDS = 5.0
# Handbetrieb/Aktortest flag (reports/firmware-deep/phase6-live-leseplan.md P1.6,
# motor.md: 0x1b5a == 1); read-only 1-byte service-0x02 read.
RUNTIME_AUTO_MANUAL_MODE_FIELD = {
    "key": "CPU0.RAM.0x001b5a",
    "address": 0x1B5A,
    "length": 1,
    "signed": False,
    "label": "Handbetrieb",
    "unit": "",
    "confidence": "hoch",
    "note": "== 1: Handbetrieb/Aktortest aktiv",
}
DISPLAY_STATUS_EVENT_MAX_LIMIT = 500


def soot_regeneration_supported(type_code: object):
    """Return True/False for bit 3 of the Dachs type code, None when unknown."""
    if isinstance(type_code, bool):
        return None
    try:
        number = int(type_code)
    except (TypeError, ValueError):
        return None
    if not 0 <= number <= 0xFF:
        return None
    return bool(number & (1 << SOOT_REGENERATION_TYPE_BIT))


def soot_regeneration_unavailable(type_code=None, error=None, recorded_at=None) -> dict:
    supported = soot_regeneration_supported(type_code)
    return {
        "schema": SOOT_REGENERATION_SCHEMA,
        "supported": supported,
        "type_code": None if supported is None else int(type_code),
        "type_code_key": SOOT_REGENERATION_TYPE_KEY,
        "available": False,
        "state": None,
        "state_label": None,
        "minutes_since_regeneration": None,
        "minutes_until_next": None,
        "regeneration_due": None,
        "minutes_in_regeneration": None,
        "minutes_remaining": None,
        "interruptions": None,
        "interruption_latched": None,
        "interval_minutes": SOOT_REGENERATION_INTERVAL_MINUTES,
        "duration_minutes": SOOT_REGENERATION_DURATION_MINUTES,
        "interruption_limit": SOOT_REGENERATION_INTERRUPTION_LIMIT,
        "semantics": "active_runtime_minutes",
        "confidence": "hoch",
        "raw": {},
        "recorded_at": recorded_at,
        "error": None if error is None else str(error),
    }


def soot_regeneration_status(type_code: object, fields: dict, recorded_at: str) -> dict:
    """Combine the three gated RAM reads into one fail-closed status dict.

    ``fields`` maps the allowlist keys to ``ram_telemetry_status``-style
    dicts. Any unavailable field, an unsupported type code or an out-of-range
    state byte yields ``available: False`` without partial derived values.
    """
    supported = soot_regeneration_supported(type_code)
    if supported is None:
        return soot_regeneration_unavailable(
            None, error="Dachs-Typcode nicht lesbar", recorded_at=str(recorded_at)
        )
    if supported is False:
        return soot_regeneration_unavailable(
            type_code, error="nicht unterstützt (Typcode ohne Rußfilterbit)", recorded_at=str(recorded_at)
        )
    raw = {}
    for field in SOOT_REGENERATION_FIELDS:
        item = fields.get(field["key"]) or {}
        if item.get("available") is not True or not isinstance(item.get("raw"), int):
            return soot_regeneration_unavailable(
                type_code,
                error=f"{field['label']}: {item.get('error') or 'nicht gelesen'}",
                recorded_at=str(recorded_at),
            )
        raw[field["address"]] = int(item["raw"])
    state = raw[0x0DB3]
    if state not in SOOT_REGENERATION_STATE_LABELS:
        return {
            **soot_regeneration_unavailable(
                type_code,
                error=f"Regenerationszustand {state} ist nicht belegt",
                recorded_at=str(recorded_at),
            ),
            "raw": {f"0x{address:06x}": value for address, value in raw.items()},
        }
    minutes_since = raw[0x0DE6] & 0xFF
    minutes_in = (raw[0x0DE6] >> 8) & 0xFF
    counter = raw[0x0D82]
    return {
        **soot_regeneration_unavailable(type_code, recorded_at=str(recorded_at)),
        "available": True,
        "state": state,
        "state_label": SOOT_REGENERATION_STATE_LABELS[state],
        "minutes_since_regeneration": minutes_since,
        "minutes_until_next": max(0, SOOT_REGENERATION_INTERVAL_MINUTES - minutes_since),
        "regeneration_due": minutes_since >= SOOT_REGENERATION_INTERVAL_MINUTES,
        "minutes_in_regeneration": minutes_in,
        "minutes_remaining": max(0, SOOT_REGENERATION_DURATION_MINUTES - minutes_in),
        "interruptions": counter & 0x7F,
        "interruption_latched": bool(counter & 0x80),
        "raw": {
            "0x000db3": state,
            "0x000de6": minutes_since,
            "0x000de7": minutes_in,
            "0x000d82": counter,
        },
        "error": None,
    }


def ram_telemetry_request(field: dict) -> bytes:
    """Build the exact four-byte service-0x02 read for one allowlisted field."""
    address = int(field["address"])
    length = int(field["length"])
    if not 0 <= address <= 0xFFFF or not 1 <= length <= 64:
        raise ValueError(f"RAM-Telemetrie: ungültige Anfrage für {field.get('key')}")
    return bytes([0x02, (address >> 8) & 0xFF, address & 0xFF, length])


CONTROLLER_CLOCK_BLOCK = 50
# Hka_Ew.ulSystemTime: Reglerzeit (lokal) in Sekunden seit 2000-01-01.
CONTROLLER_CLOCK_OFFSET = 36
CONTROLLER_CLOCK_TIMEZONE = ZoneInfo("Europe/Berlin")


def controller_clock_status(
    payload: bytes | None, read_age_seconds: float | None, max_age_seconds: float,
    now: datetime | None = None,
) -> dict:
    """Controller clock against ODM time from the last fresh block-50 read.

    Read-only and fail-closed: a missing, failed or stale read yields no
    value at all, never an older one.
    """
    unavailable = {
        "available": False,
        "controller_time": None,
        "odm_time": None,
        "offset_seconds": None,
        "source": "Block 50 Hka_Ew.ulSystemTime",
    }
    if payload is None or read_age_seconds is None:
        return {**unavailable, "error": "Block 50 nicht gelesen"}
    if read_age_seconds < 0 or read_age_seconds > max_age_seconds:
        return {**unavailable, "error": "veraltet"}
    if len(payload) < CONTROLLER_CLOCK_OFFSET + 4:
        return {**unavailable, "error": "Block 50 zu kurz"}
    raw = int.from_bytes(
        payload[CONTROLLER_CLOCK_OFFSET:CONTROLLER_CLOCK_OFFSET + 4], "little", signed=False
    )
    if raw in {0, 0xFFFFFFFF}:
        return {**unavailable, "error": f"ungültige Reglerzeit (Rohwert {raw})"}
    read_at = (now or datetime.now(timezone.utc)) - timedelta(seconds=float(read_age_seconds))
    odm_local = read_at.astimezone(CONTROLLER_CLOCK_TIMEZONE)
    controller = datetime(2000, 1, 1) + timedelta(seconds=raw)
    offset = (controller - odm_local.replace(tzinfo=None)).total_seconds()
    return {
        "available": True,
        "controller_time": controller.isoformat(),
        "odm_time": odm_local.isoformat(timespec="seconds"),
        "offset_seconds": int(round(offset)),
        "source": unavailable["source"],
        "error": None,
    }


def ram_telemetry_unavailable(field: dict, error=None, recorded_at=None) -> dict:
    return {
        "schema": RAM_TELEMETRY_SCHEMA,
        "key": field["key"],
        "label": field["label"],
        "address": f"0x{int(field['address']):06x}",
        "length": int(field["length"]),
        "signed": bool(field["signed"]),
        "unit": field["unit"],
        "confidence": field["confidence"],
        "note": field["note"],
        "available": False,
        "raw": None,
        "value": None,
        "recorded_at": recorded_at,
        "rtt_ms": None,
        "error": None if error is None else str(error),
    }


ADAPTIVE_RAW_RETENTION_HOURS = 24
ADAPTIVE_EVENT_MARGIN_SECONDS = 3600
ADAPTIVE_ROLLUP_SECONDS = 900
ADAPTIVE_COMPACTION_BUCKETS = 16
MOTOR_SPEED_BLOCK = 24
MOTOR_SPEED_KEY = "Hka_Mw1.usDrehzahl"
API_TOKEN_SCOPES = frozenset({"read", "history", "write"})
API_DEFAULT_AUTH_LEVEL = 4
LOCAL_ACCESS_BIND_HOST = "127.0.0.1"
LOCAL_ACCESS_DEFAULT_PORT = 80
LOCAL_ACCESS_MIN_PORT = 1
LOCAL_ACCESS_PRINCIPAL = "localhost-override"
LOCAL_ACCESS_ROLES = frozenset({"guest", "admin"})
LOCAL_ACCESS_MUTATION_HEADER = "X-Open-Dachs-Request"
LOCAL_ACCESS_FORWARDED_HEADERS = (
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Forwarded-Port",
    "X-Real-IP",
    "Via",
)
BACKUP_RESTORE_CONFIRMATION = "SICHERUNG WIEDERHERSTELLEN"
BACKUP_RESTORE_PRESERVED_PREFIXES = (
    "Hka_Bd_Stat.bSoftwareVersion",
    "Rvc_SwVersion",
)
BACKUP_ARCHIVE_MAX_BYTES = 2 * 1024 * 1024
BACKUP_ARCHIVE_FILENAME = re.compile(
    r"maintenance-[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{16}\.json"
)
# The browser accepts a 1 MiB image. Leave bounded room for the surrounding
# JSON request object and restore selection without rejecting that image.
# A base64-encoded 1 MiB MUC needs about 1.34 MiB plus its JSON envelope.
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
POWER_TARGET_BLOCK = 50
POWER_TARGET_KEY = "Hka_Ew.usSollGenerator"
POWER_TARGET_AUTH_LEVEL = 4
POWER_TARGET_FUEL_BLOCK = 24
POWER_TARGET_FUEL_KEY = "Hka_Mw1.bKraftstofftyp"
POWER_TARGET_LIMITS_KW = {
    8: (3.8, 5.3),
    10: (3.8, 5.3),
    128: (4.0, 5.5),
    144: (3.5, 5.0),
    160: (4.0, 5.5),
}
RUN_HISTORY_BLOCKS = (28, 30, 31, 32)
SERVICE_HISTORY_BLOCKS = (80, 82)
MOTOR_SNAPSHOT_BLOCKS = (84, 88, 92)
MC_STATUS_BLOCKS = (26, 86, 90, 94)
HISTORY_RETENTION_DAYS = 30
HISTORY_MAX_POINTS = 2000
HISTORY_CLEANUP_MARKER = "dashboard-cleanup-v1.done"
MOTOR_STATUS_MIGRATION = "motor-status-v1"
MOTOR_STATUS_BLOCK = 24
MOTOR_STATUS_KEY = "Hka_Mw1.bMotorStatus"
# Store every transition plus a sparse heartbeat.  The heartbeat makes real
# recording outages distinguishable from a long, unchanged controller state
# without duplicating the 0.75-second snapshot stream.
MOTOR_STATUS_HEARTBEAT_SECONDS = 30.0
MOTOR_STATUS_GAP_SECONDS = 75.0
MOTOR_STATUS_OBSERVATION_GAP_SECONDS = 5.0
MOTOR_STATUS_LEGACY_GAP_SECONDS = 30.0
INVALID_SENSOR_VALUES = (0, -1, 90, 127, 255)
DASHBOARD_TEMPERATURE_KEYS = (
    "Hka_Mw1.Temp.sbMotor",
    "Hka_Mw1.Temp.sbGen",
    "Hka_Mw1.Temp.sbVorlauf",
    "Hka_Mw1.Temp.sbRuecklauf",
    "Hka_Mw1.Temp.sKapsel",
    "Hka_Mw1.Temp.sbRegler",
    "Hka_Mw1.Temp.sAbgasMotor",
    "Hka_Mw1.Temp.sAbgasHKA",
)


class APIRequestConflictError(RuntimeError):
    """An API request id is already bound to another or unfinished action."""


class LocalAccessConflictError(RuntimeError):
    """The configured loopback listener cannot be activated transactionally."""


def _local_access_origin(port: int) -> str:
    authority = LOCAL_ACCESS_BIND_HOST if int(port) == 80 else f"{LOCAL_ACCESS_BIND_HOST}:{int(port)}"
    return f"http://{authority}"


class PowerTargetRangeError(ValueError):
    """The generator target is outside the controller variant's source range."""
DEFAULT_DASHBOARD_SERIES = (
    ("dachs_austritt", "Dachs-Austritt", 24, "Hka_Mw1.Temp.DachsAustritt", "°C", "#dc2626"),
    ("dachs_eintritt", "Dachs-Eintritt", 24, "Hka_Mw1.Temp.sbGen", "°C", "#2563eb"),
    ("vorlauf", "Heizkreis Vorlauf (VF)", 24, "Hka_Mw1.Temp.sbVorlauf", "°C", "#d97706"),
    ("ruecklauf", "Heizkreis Rücklauf (RF)", 24, "Hka_Mw1.Temp.sbRuecklauf", "°C", "#2563eb"),
    ("kuehlwasser", "Kühlwasser Motor / Dachs-Vorlauf", 24, "Hka_Mw1.Temp.sbMotor", "°C", "#059669"),
    ("regler", "Reglerfühler MSR2", 24, "Hka_Mw1.Temp.sbRegler", "°C", "#f59e0b"),
    ("abgas_motor", "Motorabgastemperatur", 24, "Hka_Mw1.Temp.sAbgasMotor", "°C", "#dc2626"),
    ("kapsel", "Kapseltemperatur", 24, "Hka_Mw1.Temp.sKapsel", "°C", "#b45309"),
    ("abgas_hka", "Dachsabgastemperatur nach Rußfilter", 24, "Hka_Mw1.Temp.sAbgasHKA", "°C", "#9333ea"),
    ("drehzahl", "Drehzahl", 24, "Hka_Mw1.usDrehzahl", "1/min", "#0f766e"),
    ("wirkleistung", "Wirkleistung Ist", 24, "Hka_Mw1.sWirkleistung", "kW", "#0891b2"),
    ("wirkleistung_soll", "Wirkleistung Soll", 50, "Hka_Ew.usSollGenerator", "kW", "#d97706"),
    # 0x0f0e is the controller's own, deliberately resettable runtime counter.
    # The id and the tuple position stay unchanged (HISTORY_SERIES index);
    # the real runtime since start comes from ``real_runtime``.
    ("betriebsstunden", "Reglerzähler (0x0f0e)", 24, "Hka_Mw1.ulMotorlaufsekunden", "h", "#7c3aed"),
    ("motorstatus", "Motorstatus", 24, "Hka_Mw1.bMotorStatus", "", "#475569"),
    ("betriebsstunden_gesamt", "Betriebsstunden gesamt", 22, "Hka_Bd.ulBetriebssekunden", "h", "#7c3aed"),
    ("starts", "Anzahl Starts", 22, "Hka_Bd.ulAnzahlStarts", "", "#ea580c"),
    ("arbeit_elektr", "Erzeugte elektrische Arbeit", 22, "Hka_Bd.ulArbeitElektr", "kWh", "#0891b2"),
    ("arbeit_therm_hka", "Erzeugte thermische Arbeit (Dachs)", 22, "Hka_Bd.ulArbeitThermHka", "kWh", "#dc2626"),
    ("arbeit_therm_kon", "Erzeugte thermische Energie (Kondenser)", 22, "Hka_Bd.ulArbeitThermKon", "kWh", "#9333ea"),
    ("servicecode", "Aktueller Fehlercode", 22, "Hka_Bd.bStoerung", "", "#b91c1c"),
    ("warncode", "Aktueller Warncode", 22, "Hka_Bd.bWarnung", "", "#ca8a04"),
    ("anzahl_warnungen", "Anzahl Warnungen", 22, "Hka_Bd.usAnzahlWarnungen", "", "#ca8a04"),
    ("anzahl_stoerungen", "Anzahl Störungen", 22, "Hka_Bd.usAnzahlStoerungenHka", "", "#b91c1c"),
    ("spannung_l1", "Spannung L1", 26, "Hka_Mw2.Hka_UC.ausVoltage1[0]", "V", "#2563eb"),
    ("spannung_l2", "Spannung L2", 26, "Hka_Mw2.Hka_UC.ausVoltage1[1]", "V", "#2563eb"),
    ("spannung_l3", "Spannung L3", 26, "Hka_Mw2.Hka_UC.ausVoltage1[2]", "V", "#2563eb"),
    ("strom_l1", "Strom L1", 26, "Hka_Mw2.Hka_UC.ausCurrent1[0]", "A", "#dc2626"),
    ("strom_l2", "Strom L2", 26, "Hka_Mw2.Hka_UC.ausCurrent1[1]", "A", "#dc2626"),
    ("strom_l3", "Strom L3", 26, "Hka_Mw2.Hka_UC.ausCurrent1[2]", "A", "#dc2626"),
    ("impedanz_l1", "Impedanz L1", 26, "Hka_Mw2.Hka_UC.ausImpedanz[0]", "Ohm", "#7c3aed"),
    ("impedanz_l2", "Impedanz L2", 26, "Hka_Mw2.Hka_UC.ausImpedanz[1]", "Ohm", "#7c3aed"),
    ("impedanz_l3", "Impedanz L3", 26, "Hka_Mw2.Hka_UC.ausImpedanz[2]", "Ohm", "#7c3aed"),
    ("frequenz", "Netzfrequenz", 26, "Hka_Mw2.Hka_UC.usFrequency1", "Hz", "#059669"),
)
HISTORY_SERIES = tuple(
    item
    for item in DEFAULT_DASHBOARD_SERIES
    if (item[2] in (22, 24) or item[0] == "frequenz")
    and item[3] != "Hka_Mw1.Temp.DachsAustritt"
)
HISTORY_MEASUREMENT_KEYS = frozenset(
    (block, key)
    for _series_id, _title, block, key, _unit, _color in HISTORY_SERIES
)
HISTORY_SERIES_INDEX = {
    (block, key): index
    for index, (_series_id, _title, block, key, _unit, _color) in enumerate(HISTORY_SERIES)
}
DEFAULT_OVERVIEW_SERIES_IDS = (
    "kuehlwasser",
    "dachs_eintritt",
    "abgas_motor",
    "abgas_hka",
    "kapsel",
    "regler",
    "betriebsstunden_gesamt",
    "starts",
    "arbeit_elektr",
    "arbeit_therm_hka",
    "arbeit_therm_kon",
    "servicecode",
)
# Retired synthetic tile key; retained solely to migrate existing selections
# to the controller-provided Block-38 value.
DASHBOARD_OPERATING_HOURS_PER_START_KEY = "derived.operating_hours_per_start"
DASHBOARD_RUNTIME_24H_KEY = "derived.runtime_24h_shutdown"
DASHBOARD_SOOT_REGENERATION_KEY = "derived.soot_filter_regeneration"
DASHBOARD_RAM_TELEMETRY_KEY = "derived.ram_telemetry"
DASHBOARD_DISPLAY_STATUS_KEY = "derived.display_status"
# Retired tile: the real runtime is now the main value of the runtime tile.
DASHBOARD_REAL_RUNTIME_KEY = "derived.real_runtime"
DASHBOARD_RETIRED_CARDS = {
    (24, DASHBOARD_REAL_RUNTIME_KEY): (22, DASHBOARD_RUNTIME_24H_KEY),
    (22, DASHBOARD_OPERATING_HOURS_PER_START_KEY): (
        OPERATING_HOURS_PER_START_BLOCK,
        OPERATING_HOURS_PER_START_KEY,
    ),
}
# Derived history series, reconstructed from motor_status_points at query
# time; it has no HISTORY_SERIES slot and needs no database migration.
HISTORY_REAL_RUNTIME_SERIES = {
    "id": "laufzeit_real",
    "title": "Laufzeit seit Start (real)",
    "block": 24,
    "key": DASHBOARD_REAL_RUNTIME_KEY,
    "unit": "h",
    "color": "#7c3aed",
    "derived": True,
}
DASHBOARD_DERIVED_FIELDS = (
    (
        "runtime_24h_shutdown",
        "Laufzeit seit Start",
        22,
        DASHBOARD_RUNTIME_24H_KEY,
        "",
    ),
    # Optional tiles: never part of the defaults and never migrated into
    # existing user selections. Operators add them via "Bearbeiten".
    (
        "soot_filter_regeneration",
        "Rußfilterregeneration",
        24,
        DASHBOARD_SOOT_REGENERATION_KEY,
        "",
    ),
    (
        "ram_telemetry",
        "Interne Reglerwerte",
        24,
        DASHBOARD_RAM_TELEMETRY_KEY,
        "",
    ),
    (
        "display_status",
        "Display-Status",
        22,
        DASHBOARD_DISPLAY_STATUS_KEY,
        "",
    ),
)
MAX_DASHBOARD_CARDS = 24
MAX_DASHBOARD_EXTRA_BLOCKS = 12
DASHBOARD_SETTINGS_VERSION = 2
SOOT_FILTER_SOURCE_KEY = "Hka_Mw1.Temp.sAbgasMotor"
DEFAULT_SOOT_FILTER_ZERO_C = 420.0
DEFAULT_SOOT_FILTER_FULL_C = 520.0


def normalize_base_path(value: str | None) -> str:
    """Return a safe external URL prefix such as ``/dachs`` or ``""``."""
    text = str(value or "").strip()
    if not text or text == "/":
        return ""
    text = "/" + text.strip("/")
    if "//" in text or any(part in {"", ".", ".."} for part in text.split("/")[1:]):
        raise ValueError("Base Path enthält ungültige Segmente")
    if not re.fullmatch(r"/[A-Za-z0-9._~/-]+", text):
        raise ValueError("Base Path darf nur URL-Pfadzeichen enthalten")
    return text


def soot_filter_estimate(
    temperature_c: object,
    zero_temperature_c: float = DEFAULT_SOOT_FILTER_ZERO_C,
    full_temperature_c: float = DEFAULT_SOOT_FILTER_FULL_C,
) -> dict:
    """Estimate soot-filter loading from the configured motor-exhaust curve."""
    try:
        temperature = float(temperature_c)
        zero = float(zero_temperature_c)
        full = float(full_temperature_c)
    except (TypeError, ValueError):
        temperature = float("nan")
        zero = float(zero_temperature_c)
        full = float(full_temperature_c)
    available = all(math.isfinite(item) for item in (temperature, zero, full)) and full > zero
    if not available:
        return {
            "available": False,
            "percent": None,
            "level": "unknown",
            "source_temperature_c": None,
            "zero_temperature_c": zero,
            "full_temperature_c": full,
        }
    percent = round(max(0.0, min(100.0, (temperature - zero) * 100.0 / (full - zero))))
    level = "red" if percent >= 90 else "orange" if percent >= 60 else "green"
    return {
        "available": True,
        "percent": int(percent),
        "level": level,
        "source_temperature_c": temperature,
        "zero_temperature_c": zero,
        "full_temperature_c": full,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def operating_hours_per_start(values: list[dict]) -> dict:
    """Return the controller-provided Block-38 Bh/Start value."""
    result = {
        "available": False,
        "value": None,
        "unit": "Bh/Start",
        "source_block": OPERATING_HOURS_PER_START_BLOCK,
        "source_key": OPERATING_HOURS_PER_START_KEY,
        "raw": None,
        "recorded_at": None,
    }
    row = next(
        (
            item
            for item in values
            if isinstance(item, dict)
            and item.get("block") == OPERATING_HOURS_PER_START_BLOCK
            and item.get("key") == OPERATING_HOURS_PER_START_KEY
        ),
        None,
    )
    if row is None:
        return result
    raw = row.get("raw")
    value = row.get("value")
    recorded_at = str(row.get("recorded_at") or "")
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 0xFF:
        return result
    if isinstance(value, bool):
        return result
    try:
        number = float(value)
    except (TypeError, ValueError):
        return result
    if (
        not math.isfinite(number)
        or abs(number - raw / 10.0) > 1e-9
        or not recorded_at
    ):
        return result
    return {
        **result,
        "available": True,
        "value": number,
        "raw": raw,
        "recorded_at": recorded_at,
    }


def runtime_24h_status(response, recorded_at: str) -> dict:
    """Decode one exact CPU-0 RAM-read response for the 24-hour counter."""
    ack = getattr(response, "ack", None)
    data = getattr(response, "data", None)
    if ack is None or getattr(ack, "positive", None) is not True:
        raise TransportError("24-h-Zähler: kein positives Transport-ACK")
    payload = bytes(getattr(data, "payload", b"")) if data is not None else b""
    expected = bytes([0x82]) + RUNTIME_24H_REQUEST[1:]
    if len(payload) != len(expected) + RUNTIME_24H_COUNTER_LENGTH or not payload.startswith(expected):
        raise TransportError(
            "24-h-Zähler: unerwartete RAM-Read-Antwort "
            f"{payload.hex() or 'leer'}"
        )
    counter = int.from_bytes(payload[len(expected):], "little", signed=False)
    remaining = max(0, RUNTIME_24H_SHUTDOWN_SECONDS - counter)
    night_remaining = max(0, RUNTIME_NIGHT_SHUTDOWN_GATE_SECONDS - counter)
    return {
        "schema": "open-dachs-runtime-24h/v1",
        "available": True,
        "counter_seconds": counter,
        "remaining_run_seconds": remaining,
        "shutdown_threshold_seconds": RUNTIME_24H_SHUTDOWN_SECONDS,
        "threshold_reached": counter >= RUNTIME_24H_SHUTDOWN_SECONDS,
        "night_shutdown_gate_seconds": RUNTIME_NIGHT_SHUTDOWN_GATE_SECONDS,
        "remaining_to_night_gate_seconds": night_remaining,
        "night_shutdown_gate_reached": counter >= RUNTIME_NIGHT_SHUTDOWN_GATE_SECONDS,
        "counter_address": f"0x{RUNTIME_24H_COUNTER_ADDRESS:04x}",
        "recorded_at": str(recorded_at),
        "rtt_ms": round(float(getattr(response, "elapsed_ms", 0.0)), 1),
        "semantics": "active_runtime_seconds",
        "wall_clock_countdown": False,
    }


def ram_telemetry_status(field: dict, response, recorded_at: str) -> dict:
    """Decode one exact CPU-0 RAM-read response for an allowlisted field."""
    label = field["label"]
    ack = getattr(response, "ack", None)
    data = getattr(response, "data", None)
    if ack is None or getattr(ack, "positive", None) is not True:
        raise TransportError(f"{label}: kein positives Transport-ACK")
    payload = bytes(getattr(data, "payload", b"")) if data is not None else b""
    request = ram_telemetry_request(field)
    expected = bytes([0x82]) + request[1:]
    length = int(field["length"])
    if len(payload) != len(expected) + length or not payload.startswith(expected):
        raise TransportError(
            f"{label}: unerwartete RAM-Read-Antwort {payload.hex() or 'leer'}"
        )
    raw_bytes = payload[len(expected):]
    raw = int.from_bytes(raw_bytes, "little", signed=False)
    value = int.from_bytes(raw_bytes, "little", signed=bool(field["signed"]))
    return {
        **ram_telemetry_unavailable(field, recorded_at=str(recorded_at)),
        "available": True,
        "raw": raw,
        "value": value,
        "rtt_ms": round(float(getattr(response, "elapsed_ms", 0.0)), 1),
        "error": None,
    }


def runtime_24h_write_seconds(value: object) -> int:
    """Parse seconds or HH:MM:SS and reject the shutdown threshold."""
    if isinstance(value, bool):
        raise ValueError("24-h-Zähler muss als HH:MM:SS oder ganze Sekundenzahl angegeben werden")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[0-9]+", text):
            seconds = int(text)
        else:
            match = re.fullmatch(r"([0-9]{1,2}):([0-9]{2}):([0-9]{2})", text)
            if match is None:
                raise ValueError("24-h-Zähler benötigt HH:MM:SS oder ganze Sekunden")
            hours, minutes, remainder = (int(part) for part in match.groups())
            if minutes > 59 or remainder > 59:
                raise ValueError("24-h-Zähler enthält ungültige Minuten oder Sekunden")
            seconds = hours * 3600 + minutes * 60 + remainder
    else:
        raise ValueError("24-h-Zähler muss als HH:MM:SS oder ganze Sekundenzahl angegeben werden")
    if not 0 <= seconds <= RUNTIME_24H_MAX_SETTABLE_SECONDS:
        raise ValueError(
            f"24-h-Zähler muss zwischen 0 und {RUNTIME_24H_MAX_SETTABLE_SECONDS} Sekunden liegen; "
            f"{RUNTIME_24H_SHUTDOWN_SECONDS} oder mehr würde unmittelbar die Sicherheitsabschaltung anfordern"
        )
    return seconds


def validate_runtime_24h_write_response(response) -> None:
    """Accept only the live-confirmed exact service-0x03 response."""
    ack = getattr(response, "ack", None)
    data = getattr(response, "data", None)
    if ack is None or getattr(ack, "positive", None) is not True:
        raise TransportError("24-h-Zähler: RAM-Write erhielt kein positives Transport-ACK")
    payload = bytes(getattr(data, "payload", b"")) if data is not None else b""
    expected = bytes([0x83]) + RUNTIME_24H_WRITE_PREFIX[1:]
    if payload != expected:
        raise TransportError(
            "24-h-Zähler: unerwartete RAM-Write-Antwort "
            + (payload.hex() or "leer")
        )


def validate_service_reset_write_response(response) -> None:
    """Require the exact ACK and echo observed for RAM 0x1c9e."""
    ack = getattr(response, "ack", None)
    data = getattr(response, "data", None)
    if ack is None or getattr(ack, "positive", None) is not True:
        raise TransportError("Entstörung: RAM-Write erhielt kein positives Transport-ACK")
    payload = bytes(getattr(data, "payload", b"")) if data is not None else b""
    if payload != SERVICE_RESET_RESPONSE:
        raise TransportError(
            "Entstörung: unerwartete redigierte RAM-Write-Antwort "
            f"(Länge={len(payload)}, Präfix={payload[:4].hex() or 'leer'})"
        )


def dachs_control_read_byte(response, request: bytes, label: str) -> int:
    """Decode one exact one-byte CPU-0 RAM response, fail-closed."""
    ack = getattr(response, "ack", None)
    data = getattr(response, "data", None)
    if ack is None or getattr(ack, "positive", None) is not True:
        raise TransportError(f"Dachs {label}: RAM-Read erhielt kein positives Transport-ACK")
    payload = bytes(getattr(data, "payload", b"")) if data is not None else b""
    expected = bytes([0x82]) + bytes(request[1:])
    if len(payload) != 5 or not payload.startswith(expected):
        raise TransportError(
            f"Dachs {label}: unerwartete RAM-Read-Antwort "
            f"(Länge={len(payload)}, Präfix={payload[:4].hex() or 'leer'})"
        )
    return int(payload[4])


def validate_dachs_control_write_response(response) -> None:
    """Require the exact ACK and address/length echo for RAM 0x0ea1."""
    ack = getattr(response, "ack", None)
    data = getattr(response, "data", None)
    if ack is None or getattr(ack, "positive", None) is not True:
        raise TransportError("Dachs Ein/Aus: RAM-Write erhielt kein positives Transport-ACK")
    payload = bytes(getattr(data, "payload", b"")) if data is not None else b""
    if payload != DACHS_CONTROL_WRITE_RESPONSE:
        raise TransportError(
            "Dachs Ein/Aus: unerwartete RAM-Write-Antwort "
            f"(Länge={len(payload)}, Präfix={payload[:4].hex() or 'leer'})"
        )


def _field_edit_value(field) -> object:
    """Keep code descriptions readable while retaining a numeric write value."""
    if str(field.metadata.get("type", "")).lower() == "version":
        return field.value
    if field.metadata.get("choices"):
        return field.raw
    if re.search(r"(?i)(\.bstoerung$|\.bwarnung$|\.bstoercode$|\.bwarncode$|bmeldecodetypereturn$|\.bstatusflags$|\.bwarntypmodul$)", field.key):
        return field.raw
    return field.value


def _history_datetime(value: str, label: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"ungültiges {label}-Datum") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _history_bounds(query: dict[str, list[str]]) -> tuple[datetime, datetime, float]:
    """Parse one bounded history window for single and batched requests."""
    if "from" in query or "to" in query:
        if "from" not in query or "to" not in query:
            raise ValueError("from und to müssen gemeinsam gesetzt werden")
        start = _history_datetime(query["from"][0], "Start")
        end = _history_datetime(query["to"][0], "Ende")
        duration = (end - start).total_seconds()
        if duration <= 0:
            raise ValueError("Ende muss nach dem Start liegen")
        if duration > HISTORY_RETENTION_DAYS * 86400:
            raise ValueError("der Zeitraum darf höchstens 30 Tage umfassen")
        return start, end, duration
    try:
        hours = int(query.get("hours", ["24"])[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("Stunden müssen eine ganze Zahl sein") from exc
    if not 1 <= hours <= HISTORY_RETENTION_DAYS * 24:
        raise ValueError(
            f"Stunden müssen zwischen 1 und {HISTORY_RETENTION_DAYS * 24} liegen"
        )
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    return start, end, hours * 3600.0


def web_field_visible(block: int, key: str, metadata: dict | None = None) -> bool:
    """Return whether a correctly mapped field may be shown in the UI."""
    return True


def generic_web_field_writable(block: int, metadata: dict) -> bool:
    """Hide firmware-owned SC123 bytes from generic edit affordances."""
    try:
        offset = int(metadata.get("offset", -1))
        size = int(metadata.get("size", -1))
    except (TypeError, ValueError):
        return False
    if offset < 0 or size < 1:
        return False
    protected = set(sc123_firmware_owned_offsets(0, int(block)))
    return protected.isdisjoint(range(offset, offset + size))


def web_measurement_valid(block: int, key: str, value, raw=None) -> bool:
    """Reject known sensor sentinels and pre-fix decoder outliers."""
    if int(block) != 24:
        return True
    dashboard_keys = set(DASHBOARD_TEMPERATURE_KEYS) | {
        "Hka_Mw1.Temp.DachsAustritt",
        "Hka_Mw1.usDrehzahl",
        "Hka_Mw1.sWirkleistung",
    }
    if key not in dashboard_keys:
        return True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if key in DASHBOARD_TEMPERATURE_KEYS or key == "Hka_Mw1.Temp.DachsAustritt":
        if number in INVALID_SENSOR_VALUES:
            return False
        if key == "Hka_Mw1.Temp.DachsAustritt":
            try:
                if float(raw) in INVALID_SENSOR_VALUES:
                    return False
            except (TypeError, ValueError):
                return False
    if key == "Hka_Mw1.usDrehzahl":
        return 0 <= number <= 3000
    if key == "Hka_Mw1.sWirkleistung":
        return -6 <= number <= 6
    if key == "Hka_Mw1.Temp.sAbgasMotor":
        return 0 <= number <= 600
    if key == "Hka_Mw1.Temp.sAbgasHKA":
        return 0 <= number <= 200
    return True


def web_monitor_field_visible(block: int, key: str) -> bool:
    """Keep slow polling lightweight and omit Block 104 reserve bytes."""
    block = int(block)
    if block == POWER_TARGET_BLOCK:
        return key == POWER_TARGET_KEY
    if block == OPERATING_HOURS_PER_START_BLOCK:
        return key == OPERATING_HOURS_PER_START_KEY
    if block == 104:
        return key in MAINTENANCE_CACHE_KEYS
    return True


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return base64.urlsafe_b64encode(salt).decode("ascii"), base64.urlsafe_b64encode(digest).decode("ascii")


def _check_password(password: str, salt_text: str, digest_text: str) -> bool:
    try:
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return hmac.compare_digest(actual, expected)


def _validate_username(value: object) -> str:
    username = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", username):
        raise ValueError("Benutzername benötigt 3–32 Zeichen aus Buchstaben, Ziffern, Punkt, _ oder -")
    if username.casefold() == LOCAL_ACCESS_PRINCIPAL.casefold():
        raise ValueError("Benutzername ist für den lokalen Bedienplatz reserviert")
    return username


def _normalize_api_scopes(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("API-Rechte müssen eine Liste sein")
    scopes = sorted({str(item).strip().lower() for item in value if str(item).strip()})
    unknown = set(scopes) - API_TOKEN_SCOPES
    if unknown:
        raise ValueError(f"unbekannte API-Rechte: {', '.join(sorted(unknown))}")
    if not scopes:
        raise ValueError("mindestens ein API-Recht ist erforderlich")
    return scopes


def _compressed_json(payload: object) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return zlib.compress(encoded, level=1)


def _uncompressed_json(payload: bytes) -> object:
    return json.loads(zlib.decompress(payload).decode("utf-8"))


def _api_request_fingerprint(payload: dict) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _public_user(user: dict) -> dict:
    return {
        "username": str(user.get("username", "")),
        "role": str(user.get("role", "guest")),
        "enabled": bool(user.get("enabled", True)),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
    }


def init_users(data_dir: str | Path, admin_password: str | None = None, guest_password: str | None = None) -> tuple[Path, dict[str, str]]:
    """Create the two local accounts once and return the one-time passwords."""
    root = Path(data_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "users.json"
    if path.exists():
        return path, {}
    admin_password = admin_password or os.environ.get("OPEN_DACHS_WEB_ADMIN_PASSWORD") or secrets.token_urlsafe(15)
    guest_password = guest_password or os.environ.get("OPEN_DACHS_WEB_GUEST_PASSWORD") or secrets.token_urlsafe(15)
    users = []
    now = _now()
    for username, role, password in (("admin", "admin", admin_password), ("gast", "guest", guest_password)):
        salt, digest = _hash_password(password)
        users.append({
            "username": username,
            "role": role,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
            "salt": salt,
            "digest": digest,
        })
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": 2, "users": users}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path, {"admin": admin_password, "gast": guest_password}


class DachsStore:
    """SQLite persistence for monitoring values and the write audit."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.path.touch(mode=0o600, exist_ok=True)
            self.path.chmod(0o600)
        with self.database() as db:
            # WAL keeps chart reads available while the monitor commits new
            # samples or the background retention job removes old rows.
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA wal_autocheckpoint=1000")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS measurements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    block INTEGER NOT NULL,
                    field_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    raw TEXT,
                    value TEXT,
                    unit TEXT,
                    rtt_ms REAL
                );
                CREATE INDEX IF NOT EXISTS idx_measurements_key_time
                    ON measurements(block, field_key, recorded_at);
                CREATE TABLE IF NOT EXISTS history_samples (
                    recorded_at TEXT PRIMARY KEY,
                    sample_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS motor_status_points (
                    recorded_at TEXT PRIMARY KEY,
                    status INTEGER CHECK(status IS NULL OR (status>=0 AND status<=255))
                );
                CREATE TABLE IF NOT EXISTS display_status_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    line1 TEXT NOT NULL,
                    line2 TEXT NOT NULL,
                    line1_index INTEGER,
                    line2_index INTEGER,
                    service_code INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_display_status_events_time
                    ON display_status_events(recorded_at);
                CREATE TABLE IF NOT EXISTS history_migrations (
                    name TEXT PRIMARY KEY,
                    completed_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS adaptive_series (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    block INTEGER NOT NULL,
                    field_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    UNIQUE(block, field_key)
                );
                CREATE TABLE IF NOT EXISTS adaptive_raw_samples (
                    recorded_at TEXT PRIMARY KEY,
                    motor_rpm REAL NOT NULL,
                    preserve INTEGER NOT NULL DEFAULT 0,
                    sample_blob BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_adaptive_raw_preserve_time
                    ON adaptive_raw_samples(preserve, recorded_at);
                CREATE TABLE IF NOT EXISTS adaptive_rollups (
                    bucket_start TEXT PRIMARY KEY,
                    bucket_seconds INTEGER NOT NULL,
                    source_count INTEGER NOT NULL,
                    rollup_blob BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS adaptive_history_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    motor_running INTEGER NOT NULL,
                    last_motor_active_at TEXT
                );
                INSERT OR IGNORE INTO adaptive_history_state(singleton, motor_running, last_motor_active_at)
                    VALUES(1, 0, NULL);
                CREATE TABLE IF NOT EXISTS write_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    username TEXT NOT NULL,
                    block INTEGER NOT NULL,
                    audit_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS update_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    request_id TEXT,
                    version TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_update_audit_time
                    ON update_audit(recorded_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS notification_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    updated_at TEXT NOT NULL,
                    state_json TEXT NOT NULL
                );
                INSERT OR IGNORE INTO notification_state(singleton,updated_at,state_json)
                    VALUES(1,'','{}');
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','dead','canceled')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
                    next_attempt_at TEXT,
                    last_attempt_at TEXT,
                    sent_at TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_notification_outbox_status_next
                    ON notification_outbox(status,next_attempt_at,id);
                CREATE TABLE IF NOT EXISTS notification_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notification_audit_time
                    ON notification_audit(recorded_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS notification_dispatch_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    paused_revision INTEGER,
                    paused_reason TEXT,
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO notification_dispatch_state(
                    singleton,paused_revision,paused_reason,updated_at
                ) VALUES(1,NULL,NULL,'1970-01-01T00:00:00+00:00');
                CREATE TABLE IF NOT EXISTS service_reset_write_intents (
                    intent_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    username TEXT NOT NULL,
                    service_code_raw INTEGER NOT NULL CHECK(service_code_raw BETWEEN 1 AND 255),
                    fault_count INTEGER NOT NULL CHECK(fault_count BETWEEN 0 AND 65535),
                    fault_runtime_seconds INTEGER NOT NULL CHECK(fault_runtime_seconds BETWEEN 0 AND 4294967295),
                    status TEXT NOT NULL CHECK(status IN ('pending','resolved')),
                    resolution TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_service_reset_intents_status_created
                    ON service_reset_write_intents(status, created_at);
                CREATE TABLE IF NOT EXISTS maintenance_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fuel_type TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    protocol_json TEXT NOT NULL,
                    completed_at TEXT,
                    completion_json TEXT,
                    maintenance_kind TEXT NOT NULL DEFAULT 'regular',
                    maintenance_number TEXT,
                    local_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_maintenance_reports_created
                    ON maintenance_reports(created_at DESC);
                CREATE TABLE IF NOT EXISTS backup_archives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    pack_compatible INTEGER NOT NULL DEFAULT 0,
                    filename TEXT NOT NULL UNIQUE,
                    file_sha256 TEXT NOT NULL,
                    image_sha256 TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    pack_rev TEXT NOT NULL,
                    controller_serial TEXT NOT NULL,
                    target_count INTEGER NOT NULL,
                    successful_count INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    maintenance_report_id INTEGER UNIQUE,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(maintenance_report_id)
                        REFERENCES maintenance_reports(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_backup_archives_created
                    ON backup_archives(created_at DESC);
                CREATE TABLE IF NOT EXISTS api_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    owner_username TEXT NOT NULL,
                    token_prefix TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    scopes_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS api_requests (
                    token_id INTEGER NOT NULL,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(token_id, request_id),
                    FOREIGN KEY(token_id) REFERENCES api_tokens(id) ON DELETE CASCADE
                );
                """
            )
            notification_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(notification_outbox)").fetchall()
            }
            if "recipient" not in notification_columns:
                db.execute(
                    "ALTER TABLE notification_outbox ADD COLUMN recipient TEXT NOT NULL DEFAULT ''"
                )
            if "message_id" not in notification_columns:
                db.execute(
                    "ALTER TABLE notification_outbox ADD COLUMN message_id TEXT NOT NULL DEFAULT ''"
                )
            db.execute(
                "UPDATE notification_outbox SET status='dead',next_attempt_at=NULL,"
                "last_error=COALESCE(last_error,'Prozessabbruch beim letzten SMTP-Versuch') "
                "WHERE status='sending' AND attempts>=?",
                (EMAIL_MAX_ATTEMPTS,),
            )
            db.execute(
                "UPDATE notification_outbox SET status='pending',"
                "next_attempt_at=COALESCE(next_attempt_at,created_at),"
                "last_error=COALESCE(last_error,'Prozessabbruch während SMTP-Versand') "
                "WHERE status='sending' AND attempts<?",
                (EMAIL_MAX_ATTEMPTS,),
            )
            request_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(api_requests)").fetchall()
            }
            if "request_hash" not in request_columns:
                db.execute(
                    "ALTER TABLE api_requests ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''"
                )
            if "status" not in request_columns:
                db.execute(
                    "ALTER TABLE api_requests ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
                )
            maintenance_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(maintenance_reports)").fetchall()
            }
            # SQLite DDL is transactional here: all v1.8.1 maintenance metadata
            # columns become visible together or the connection rolls back.
            if "maintenance_kind" not in maintenance_columns:
                db.execute(
                    "ALTER TABLE maintenance_reports ADD COLUMN maintenance_kind "
                    "TEXT NOT NULL DEFAULT 'regular'"
                )
            if "maintenance_number" not in maintenance_columns:
                db.execute("ALTER TABLE maintenance_reports ADD COLUMN maintenance_number TEXT")
            if "local_reason" not in maintenance_columns:
                db.execute("ALTER TABLE maintenance_reports ADD COLUMN local_reason TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_maintenance_reports_number "
                "ON maintenance_reports(maintenance_number) "
                "WHERE maintenance_number IS NOT NULL"
            )
            archive_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(backup_archives)").fetchall()
            }
            if "pack_compatible" not in archive_columns:
                db.execute(
                    "ALTER TABLE backup_archives ADD COLUMN pack_compatible INTEGER NOT NULL DEFAULT 0"
                )
            interrupted_completion = {
                "mode": "live",
                "completed_at": _now(),
                "username": "system-recovery",
                "controller_written": None,
                "hardware_unchanged": False,
                "confirmation_bit_set": None,
                "uncertain": True,
                "error": "Prozessabbruch; möglicher Reglerwrite, Zustand prüfen",
                "audits": [],
            }
            db.execute(
                "UPDATE maintenance_reports SET status='uncertain',updated_at=?,"
                "completed_at=COALESCE(completed_at,?),completion_json=? "
                "WHERE status='completing'",
                (
                    interrupted_completion["completed_at"],
                    interrupted_completion["completed_at"],
                    json.dumps(interrupted_completion, ensure_ascii=False),
                ),
            )
            self._adaptive_series_cache = {
                (int(row["block"]), str(row["field_key"])): int(row["id"])
                for row in db.execute("SELECT id,block,field_key FROM adaptive_series").fetchall()
            }
            self._reload_motor_status_cache_db(db)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    @contextmanager
    def database(self):
        """Commit/rollback and close one short-lived SQLite connection."""
        db = self.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def measurements(self, block: int, key: str, hours: int, limit: int) -> list[dict]:
        hours = max(1, int(hours))
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        return self.measurements_between(block, key, start, end, limit)

    def measurements_between(
        self,
        block: int,
        key: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[dict]:
        # Read-only: WAL readers never block the monitor's commits, so the
        # writer lock is not taken (a long chart query held it for seconds).
        db = self.connect()
        try:
            return self._measurements_between_db(db, block, key, start, end, limit)
        finally:
            db.close()

    @staticmethod
    def _merge_measurement_rows(
        legacy: list[dict],
        snapshot: list[dict],
        limit: int,
    ) -> list[dict]:
        """Merge old per-field rows with compact snapshots at one point limit."""
        limit = min(HISTORY_MAX_POINTS, max(1, int(limit)))
        if not snapshot:
            return legacy
        merged = {str(row["recorded_at"]): row for row in legacy}
        # New compact snapshots supersede a legacy row from the same cycle.
        merged.update({str(row["recorded_at"]): row for row in snapshot})
        rows = [merged[timestamp] for timestamp in sorted(merged)]
        if len(rows) <= limit:
            return rows
        if limit == 1:
            return [rows[-1]]
        step = (len(rows) - 1) / (limit - 1)
        return [rows[round(index * step)] for index in range(limit)]

    @staticmethod
    def _measurements_between_db(
        db: sqlite3.Connection,
        block: int,
        key: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[dict]:
        limit = min(HISTORY_MAX_POINTS, max(1, int(limit)))
        legacy = DachsStore._legacy_measurements_between_db(db, block, key, start, end, limit)
        snapshot = DachsStore._snapshot_measurements_between_db(db, block, key, start, end, limit)
        return DachsStore._merge_measurement_rows(legacy, snapshot, limit)

    @staticmethod
    def _legacy_measurements_between_db(
        db: sqlite3.Connection,
        block: int,
        key: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[dict]:
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        seconds = max(1.0, (end - start).total_seconds())
        start_text = start.isoformat()
        end_text = end.isoformat()
        # A 30-day chart must not receive only the newest 2,000 high-frequency
        # samples. Pick the first row from each time bucket so the complete
        # requested interval remains visible while the browser gets a bounded
        # response size. The stored measurements themselves stay untouched.
        bucket_seconds = max(1.0, seconds / limit)
        rows = db.execute(
            "WITH sampled AS ("
            " SELECT MIN(id) AS id FROM measurements"
            " WHERE block=? AND field_key=? AND recorded_at>=? AND recorded_at<=?"
            " GROUP BY CAST(((julianday(recorded_at)-julianday(?))*86400.0)/? AS INTEGER)"
            ") "
            "SELECT m.recorded_at, m.block, m.field_key, m.label, m.raw, m.value, m.unit, m.rtt_ms "
            "FROM measurements AS m JOIN sampled AS s ON s.id=m.id "
            "ORDER BY m.recorded_at ASC",
            (block, key, start_text, end_text, start_text, bucket_seconds),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _snapshot_measurements_between_db(
        db: sqlite3.Connection,
        block: int,
        key: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[dict]:
        result = DachsStore._snapshot_measurements_batch_db(
            db,
            [("requested", int(block), str(key))],
            start,
            end,
            limit,
        )
        return result.get("requested", [])

    @staticmethod
    def _snapshot_measurements_batch_db(
        db: sqlite3.Connection,
        requests: list[tuple[str, int, str]],
        start: datetime,
        end: datetime,
        limit: int,
    ) -> dict[str, list[dict]]:
        """Sample compact snapshots once and fan every row out to all series."""
        targets: dict[int, list[tuple[str, int, str]]] = {}
        output: dict[str, list[dict]] = {}
        for requested_id, block, key in requests:
            index = HISTORY_SERIES_INDEX.get((int(block), str(key)))
            if index is None:
                continue
            output[requested_id] = []
            targets.setdefault(index, []).append((requested_id, int(block), str(key)))
        if not targets:
            return output

        limit = min(HISTORY_MAX_POINTS, max(1, int(limit)))
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        seconds = (end - start).total_seconds()
        if seconds <= 0:
            return output
        bucket_seconds = max(1.0, seconds / limit)
        bucket_count = max(1, min(limit, math.ceil(seconds / bucket_seconds)))
        rows: list[sqlite3.Row] = []

        # recorded_at is the table's primary key. Repeated bounded seeks are
        # substantially faster on the Pi than GROUP BY over every sample in a
        # 30-day window (about 0.3 s instead of 7 s per scan on the production
        # database). One read transaction keeps all buckets on one WAL view.
        began_transaction = not db.in_transaction
        if began_transaction:
            db.execute("BEGIN")
        try:
            for bucket_index in range(bucket_count):
                lower = start + timedelta(seconds=bucket_seconds * bucket_index)
                upper = (
                    end
                    if bucket_index == bucket_count - 1
                    else min(
                        end,
                        start
                        + timedelta(seconds=bucket_seconds * (bucket_index + 1)),
                    )
                )
                if bucket_index == bucket_count - 1:
                    sql = (
                        "SELECT recorded_at,sample_json FROM history_samples "
                        "WHERE recorded_at>=? AND recorded_at<=? "
                        "ORDER BY recorded_at LIMIT 1"
                    )
                else:
                    sql = (
                        "SELECT recorded_at,sample_json FROM history_samples "
                        "WHERE recorded_at>=? AND recorded_at<? "
                        "ORDER BY recorded_at LIMIT 1"
                    )
                row = db.execute(
                    sql, (lower.isoformat(), upper.isoformat())
                ).fetchone()
                if row is not None:
                    rows.append(row)
        finally:
            if began_transaction:
                db.rollback()

        for row in rows:
            try:
                values = json.loads(row["sample_json"])["values"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            for index, requested_targets in targets.items():
                try:
                    sample = values[index]
                    if sample is None:
                        continue
                    raw, value, rtt_ms = sample
                except (IndexError, KeyError, TypeError, ValueError):
                    continue
                series_id, title, _block, _key, unit, _color = HISTORY_SERIES[index]
                for requested_id, block, key in requested_targets:
                    output[requested_id].append({
                        "recorded_at": row["recorded_at"],
                        "block": block,
                        "field_key": key,
                        "label": title,
                        "raw": raw,
                        "value": value,
                        "unit": unit,
                        "rtt_ms": rtt_ms,
                        "series_id": series_id,
                    })
        return output

    def measurements_batch(
        self,
        requests: list[tuple[str, int, str]],
        start: datetime,
        end: datetime,
        limit: int,
    ) -> dict[str, list[dict]]:
        """Load chart series with one snapshot fanout and bounded legacy reads."""
        if not requests:
            return {}

        limit = min(HISTORY_MAX_POINTS, max(1, int(limit)))
        snapshot_db = self.connect()
        try:
            snapshots = self._snapshot_measurements_batch_db(
                snapshot_db, requests, start, end, limit
            )
        finally:
            snapshot_db.close()

        def load_legacy(item: tuple[str, int, str]) -> tuple[str, list[dict]]:
            series_id, block, key = item
            db = self.connect()
            try:
                legacy = self._legacy_measurements_between_db(
                    db, block, key, start, end, limit
                )
            finally:
                db.close()
            return series_id, self._merge_measurement_rows(
                legacy, snapshots.get(series_id, []), limit
            )

        # Only the much smaller legacy per-field table still needs individual
        # reads. WAL permits two of them while the monitor commits samples.
        with ThreadPoolExecutor(max_workers=min(2, len(requests))) as pool:
            return dict(pool.map(load_legacy, requests))

    def diagnosis_250h(self) -> dict:
        """Build 250-Bh intervals from the retained compact SQLite history."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
        ).isoformat()
        indexes = {
            series_id: index
            for index, (series_id, *_rest) in enumerate(HISTORY_SERIES)
        }
        wanted = {
            "operating_seconds": (indexes["betriebsstunden_gesamt"], "raw"),
            "starts": (indexes["starts"], "raw"),
            "motor_exhaust": (indexes["abgas_motor"], "value"),
            "dachs_exhaust": (indexes["abgas_hka"], "value"),
            "coolant": (indexes["kuehlwasser"], "value"),
            "inlet": (indexes["dachs_eintritt"], "value"),
            "controller": (indexes["regler"], "value"),
            "power": (indexes["wirkleistung"], "value"),
            "warning_code": (indexes["warncode"], "raw"),
            "service_code": (indexes["servicecode"], "raw"),
        }

        def decoded(rows: list[sqlite3.Row]) -> dict | None:
            sample: dict | None = None
            for row in rows:
                try:
                    values = json.loads(row["sample_json"])["values"]
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                candidate = {"recorded_at": str(row["recorded_at"])}
                for name, (index, source) in wanted.items():
                    try:
                        stored = values[index]
                        value = stored[0 if source == "raw" else 1] if stored else None
                    except (IndexError, TypeError):
                        value = None
                    if value is not None:
                        candidate[name] = value
                if sample is None and {
                    "operating_seconds", "starts"
                }.issubset(candidate):
                    sample = candidate
                elif sample is not None:
                    for name, value in candidate.items():
                        if name not in sample or sample[name] is None:
                            sample[name] = value
                if sample is not None:
                    break
            return sample

        db = self.connect()
        try:
            bounds = db.execute(
                "SELECT MIN(recorded_at) AS oldest,MAX(recorded_at) AS newest "
                "FROM history_samples WHERE recorded_at>=?",
                (cutoff,),
            ).fetchone()
            if not bounds or not bounds["oldest"] or not bounds["newest"]:
                return build_250h_diagnosis([])
            oldest = _history_datetime(str(bounds["oldest"]), "älteste Messung")
            newest = _history_datetime(str(bounds["newest"]), "neueste Messung")
            bucket_seconds = 15 * 60
            cursor = oldest.replace(second=0, microsecond=0)
            cursor -= timedelta(minutes=cursor.minute % 15)
            samples: list[dict] = []
            while cursor <= newest:
                upper = cursor + timedelta(seconds=bucket_seconds)
                rows = db.execute(
                    "SELECT recorded_at,sample_json FROM history_samples "
                    "WHERE recorded_at>=? AND recorded_at<? ORDER BY recorded_at LIMIT 24",
                    (cursor.isoformat(), upper.isoformat()),
                ).fetchall()
                sample = decoded(rows)
                if sample is not None:
                    samples.append(sample)
                cursor = upper
            latest_rows = db.execute(
                "SELECT recorded_at,sample_json FROM history_samples "
                "WHERE recorded_at>=? ORDER BY recorded_at DESC LIMIT 24",
                ((newest - timedelta(minutes=2)).isoformat(),),
            ).fetchall()
            latest_sample = decoded(list(reversed(latest_rows)))
            if latest_sample is not None and (
                not samples or latest_sample["recorded_at"] > samples[-1]["recorded_at"]
            ):
                samples.append(latest_sample)
            event_rows = db.execute(
                "SELECT recorded_at,status FROM motor_status_points "
                "WHERE recorded_at>=? AND recorded_at<=? ORDER BY recorded_at",
                (str(bounds["oldest"]), str(bounds["newest"])),
            ).fetchall()
        finally:
            db.close()
        # Block 50 is deliberately absent from the old compact chart snapshot.
        # Its locally persisted adaptive-history series supplies the dynamic
        # target without causing any new controller read.
        target_points = self.adaptive_history_between(
            POWER_TARGET_BLOCK,
            POWER_TARGET_KEY,
            oldest,
            newest,
            HISTORY_MAX_POINTS,
        )
        target_times: list[datetime] = []
        target_values: list[object] = []
        for point in target_points:
            at = _history_datetime(str(point["recorded_at"]), "Sollwertmessung")
            first = point.get("first") if point.get("kind") == "rollup" else point.get("value")
            last = point.get("last") if point.get("kind") == "rollup" else point.get("value")
            if first is not None:
                target_times.append(at)
                target_values.append(first)
            if point.get("kind") == "rollup" and last is not None and last != first:
                target_times.append(at + timedelta(seconds=int(point.get("bucket_seconds") or 0)))
                target_values.append(last)
        ordered_targets = sorted(zip(target_times, target_values), key=lambda item: item[0])
        target_times = [item[0] for item in ordered_targets]
        target_values = [item[1] for item in ordered_targets]
        for sample in samples:
            sample_time = _history_datetime(sample["recorded_at"], "Messung")
            position = bisect_right(target_times, sample_time) - 1
            if position < 0 and target_values:
                position = 0
            if position >= 0:
                sample["power_target"] = target_values[position]

        migration_complete = self.history_migration(MOTOR_STATUS_MIGRATION) is not None
        return build_250h_diagnosis(
            samples,
            status_events=[dict(row) for row in event_rows],
            history_complete=migration_complete,
        )

    def record(self, recorded_at: str, rows: list[dict]) -> None:
        if not rows:
            return
        with self.lock, self.database() as db:
            db.executemany(
                "INSERT INTO measurements(recorded_at, block, field_key, label, raw, value, unit, rtt_ms) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        recorded_at,
                        int(row["block"]),
                        str(row["key"]),
                        str(row["label"]),
                        str(row.get("raw", "")),
                        str(row.get("value", "")),
                        str(row.get("unit", "")),
                        float(row.get("rtt_ms", 0) or 0),
                    )
                    for row in rows
                ],
            )

    def record_history_snapshot(self, recorded_at: str, rows: list[dict]) -> None:
        values: list[list | None] = [None] * len(HISTORY_SERIES)
        for row in rows:
            index = HISTORY_SERIES_INDEX.get((int(row["block"]), str(row["key"])))
            if index is None:
                continue
            values[index] = [
                row.get("raw"),
                row.get("value"),
                float(row.get("rtt_ms", 0) or 0),
            ]
        if not any(item is not None for item in values):
            return
        payload = json.dumps({"version": 1, "values": values}, ensure_ascii=False, separators=(",", ":"))
        with self.lock, self.database() as db:
            db.execute(
                "INSERT OR REPLACE INTO history_samples(recorded_at,sample_json) VALUES(?,?)",
                (recorded_at, payload),
            )
            status_index = HISTORY_SERIES_INDEX.get((MOTOR_STATUS_BLOCK, MOTOR_STATUS_KEY))
            status_sample = values[status_index] if status_index is not None else None
            status = self._motor_status_code(status_sample[0] if status_sample else None)
            if status is None and status_sample:
                status = self._motor_status_code(status_sample[1])
            if status is not None:
                self._record_motor_status_observation_db(db, recorded_at, status)

    @staticmethod
    def _parse_recorded_at(value: object) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _motor_status_code(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            if isinstance(value, str):
                match = re.match(r"^\s*([+-]?\d+)", value)
                if match is None:
                    return None
                status = int(match.group(1))
            else:
                status = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return status if 0 <= status <= 255 else None

    def _reload_motor_status_cache_db(
        self,
        db: sqlite3.Connection,
        *,
        preserve_newer_observation: bool = False,
    ) -> None:
        """Rebuild the live status cache from durable recorder state."""
        latest_point = db.execute(
            "SELECT recorded_at,status FROM motor_status_points "
            "ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        latest_status = db.execute(
            "SELECT recorded_at,status FROM motor_status_points "
            "WHERE status IS NOT NULL ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        status = int(latest_status["status"]) if latest_status is not None else None
        status_at = (
            self._parse_recorded_at(latest_status["recorded_at"])
            if latest_status is not None
            else None
        )
        latest_point_at = (
            self._parse_recorded_at(latest_point["recorded_at"])
            if latest_point is not None
            else None
        )
        gap_open = bool(
            latest_point is not None
            and latest_point["status"] is None
            and latest_point_at is not None
            and (status_at is None or latest_point_at >= status_at)
        )

        # Status transitions/heartbeats are deliberately sparse.  The compact
        # history sample is written on every normal fast cycle, so it preserves
        # the actual last successful Block-24 observation across a web-service
        # restart without adding another high-frequency SQLite write stream.
        observed_at = status_at
        snapshots = db.execute(
            "SELECT recorded_at,sample_json FROM history_samples "
            "ORDER BY recorded_at DESC LIMIT 256"
        )
        for snapshot in snapshots:
            snapshot_at = self._parse_recorded_at(snapshot["recorded_at"])
            snapshot_status = None
            try:
                status_index = HISTORY_SERIES_INDEX[(MOTOR_STATUS_BLOCK, MOTOR_STATUS_KEY)]
                sample = json.loads(snapshot["sample_json"])["values"][status_index]
                if sample:
                    snapshot_status = self._motor_status_code(sample[0])
                    if snapshot_status is None and len(sample) > 1:
                        snapshot_status = self._motor_status_code(sample[1])
            except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                snapshot_status = None
            if snapshot_at is None or snapshot_status is None:
                continue
            if observed_at is None or snapshot_at >= observed_at:
                observed_at = snapshot_at
                if not gap_open or latest_point_at is None or snapshot_at > latest_point_at:
                    status = snapshot_status
                    gap_open = False
            break

        current_observed_at = getattr(self, "_last_motor_status_observed_at", None)
        if (
            preserve_newer_observation
            and current_observed_at is not None
            and (observed_at is None or current_observed_at > observed_at)
        ):
            return
        self._motor_status_gap_open = gap_open
        self._last_motor_status = None if gap_open else status
        self._last_motor_status_at = None if gap_open else status_at
        self._last_motor_status_observed_at = observed_at

    def _record_motor_status_observation_db(
        self,
        db: sqlite3.Connection,
        recorded_at: str,
        status: int,
    ) -> bool:
        point_time = self._parse_recorded_at(recorded_at)
        if point_time is None:
            return False
        observed_time = self._last_motor_status_observed_at
        if observed_time is not None and point_time < observed_time:
            return False
        observation_gap = (
            not self._motor_status_gap_open
            and observed_time is not None
            and (point_time - observed_time).total_seconds()
            > MOTOR_STATUS_OBSERVATION_GAP_SECONDS
        )
        if observation_gap:
            gap_time = observed_time + timedelta(
                seconds=MOTOR_STATUS_OBSERVATION_GAP_SECONDS
            )
            db.execute(
                "INSERT OR IGNORE INTO motor_status_points(recorded_at,status) VALUES(?,NULL)",
                (gap_time.isoformat(),),
            )
            self._motor_status_gap_open = True
            self._last_motor_status = None
            self._last_motor_status_at = None
        last_time = self._last_motor_status_at
        if last_time is not None and point_time < last_time:
            return False
        heartbeat_due = (
            last_time is None
            or (point_time - last_time).total_seconds() >= MOTOR_STATUS_HEARTBEAT_SECONDS
        )
        self._last_motor_status_observed_at = point_time
        if self._last_motor_status == status and not heartbeat_due and not observation_gap:
            return False
        db.execute(
            "INSERT OR REPLACE INTO motor_status_points(recorded_at,status) VALUES(?,?)",
            (recorded_at, int(status)),
        )
        self._last_motor_status = int(status)
        self._last_motor_status_at = point_time
        self._motor_status_gap_open = False
        return True

    def record_motor_status_observation(
        self,
        recorded_at: str,
        value: object,
    ) -> bool:
        """Record a B24 status independently from curve-history sampling."""
        status = self._motor_status_code(value)
        point_time = self._parse_recorded_at(recorded_at)
        if status is None or point_time is None:
            return False
        with self.lock:
            observed_time = self._last_motor_status_observed_at
            observation_gap = (
                observed_time is not None
                and (point_time - observed_time).total_seconds()
                > MOTOR_STATUS_OBSERVATION_GAP_SECONDS
            )
            heartbeat_due = (
                self._last_motor_status_at is None
                or (point_time - self._last_motor_status_at).total_seconds()
                >= MOTOR_STATUS_HEARTBEAT_SECONDS
            )
            if (
                observed_time is not None
                and point_time >= observed_time
                and not observation_gap
                and not self._motor_status_gap_open
                and self._last_motor_status == status
                and not heartbeat_due
            ):
                self._last_motor_status_observed_at = point_time
                return False
            with self.database() as db:
                return self._record_motor_status_observation_db(
                    db,
                    recorded_at,
                    status,
                )

    def record_motor_status_missing(self, recorded_at: str) -> bool:
        """Close the current status band once Block 24 has been absent for 5 s."""
        point_time = self._parse_recorded_at(recorded_at)
        if point_time is None:
            return False
        with self.lock:
            observed_time = self._last_motor_status_observed_at
            if (
                observed_time is None
                or self._motor_status_gap_open
                or point_time <= observed_time
                or (point_time - observed_time).total_seconds()
                <= MOTOR_STATUS_OBSERVATION_GAP_SECONDS
            ):
                return False
            gap_time = observed_time + timedelta(
                seconds=MOTOR_STATUS_OBSERVATION_GAP_SECONDS
            )
            with self.database() as db:
                db.execute(
                    "INSERT OR IGNORE INTO motor_status_points(recorded_at,status) VALUES(?,NULL)",
                    (gap_time.isoformat(),),
                )
            self._motor_status_gap_open = True
            self._last_motor_status = None
            self._last_motor_status_at = None
            return True

    def history_migration(self, name: str) -> dict | None:
        db = self.connect()
        try:
            row = db.execute(
                "SELECT completed_at,details_json FROM history_migrations WHERE name=?",
                (str(name),),
            ).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        try:
            details = json.loads(row["details_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {}
        return {"completed_at": row["completed_at"], "details": details}

    def complete_history_migration(self, name: str, details: dict) -> None:
        with self.lock, self.database() as db:
            db.execute(
                "INSERT OR REPLACE INTO history_migrations(name,completed_at,details_json) "
                "VALUES(?,?,?)",
                (
                    str(name),
                    _now(),
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def backfill_motor_status_points(
        self,
        stop_event: threading.Event | None = None,
    ) -> dict:
        """Build the compact transition/heartbeat history from old snapshots."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
        ).isoformat()
        source_db = self.connect()
        source_db.execute("PRAGMA query_only=ON")
        scanned = inserted = 0
        complete = True
        batch: list[tuple[str, int | None]] = []
        last_status: int | None = None
        last_time: datetime | None = None
        last_source_time: datetime | None = None
        last_source_gap_seconds: float | None = None

        def flush() -> None:
            nonlocal inserted
            if not batch:
                return
            with self.lock, self.database() as target_db:
                before = target_db.total_changes
                target_db.executemany(
                    "INSERT OR IGNORE INTO motor_status_points(recorded_at,status) VALUES(?,?)",
                    batch,
                )
                inserted += target_db.total_changes - before
            batch.clear()

        try:
            # Merge two already indexed streams in Python. A SQL UNION with a
            # global ORDER BY makes SQLite sort more than a million large JSON
            # rows into a temporary table on existing installations.
            legacy_rows = (
                (
                    row["recorded_at"],
                    row["raw"],
                    None,
                    MOTOR_STATUS_LEGACY_GAP_SECONDS,
                )
                for row in source_db.execute(
                    "SELECT recorded_at,raw FROM measurements "
                    "WHERE block=? AND field_key=? AND recorded_at>=? "
                    "ORDER BY recorded_at",
                    (MOTOR_STATUS_BLOCK, MOTOR_STATUS_KEY, cutoff),
                )
            )
            snapshot_rows = (
                (
                    row["recorded_at"],
                    None,
                    row["sample_json"],
                    MOTOR_STATUS_OBSERVATION_GAP_SECONDS,
                )
                for row in source_db.execute(
                    "SELECT recorded_at,sample_json FROM history_samples "
                    "WHERE recorded_at>=? ORDER BY recorded_at",
                    (cutoff,),
                )
            )
            status_index = HISTORY_SERIES_INDEX[(MOTOR_STATUS_BLOCK, MOTOR_STATUS_KEY)]
            for recorded_at, value, sample_json, source_gap_seconds in heapq.merge(
                legacy_rows,
                snapshot_rows,
                key=lambda item: item[0],
            ):
                if stop_event is not None and stop_event.is_set():
                    complete = False
                    break
                scanned += 1
                if value is None:
                    try:
                        sample = json.loads(sample_json)["values"][status_index]
                        value = sample[0] if sample else None
                        if value is None and sample:
                            value = sample[1]
                    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                status = self._motor_status_code(value)
                point_time = self._parse_recorded_at(recorded_at)
                if status is None or point_time is None:
                    continue
                gap_seconds = max(
                    source_gap_seconds,
                    last_source_gap_seconds or source_gap_seconds,
                )
                source_gap = (
                    last_source_time is not None
                    and (point_time - last_source_time).total_seconds() > gap_seconds
                )
                if source_gap:
                    batch.append((
                        (
                            last_source_time
                            + timedelta(seconds=gap_seconds)
                        ).isoformat(),
                        None,
                    ))
                    last_status = None
                    last_time = None
                heartbeat_due = (
                    last_time is None
                    or (point_time - last_time).total_seconds() >= MOTOR_STATUS_HEARTBEAT_SECONDS
                )
                if status != last_status or heartbeat_due:
                    batch.append((str(recorded_at), status))
                    last_status = status
                    last_time = point_time
                    if len(batch) >= 500:
                        flush()
                last_source_time = point_time
                last_source_gap_seconds = source_gap_seconds
            flush()
        finally:
            source_db.close()
        with self.lock, self.database() as db:
            self._reload_motor_status_cache_db(
                db,
                preserve_newer_observation=True,
            )
        return {"complete": complete, "scanned": scanned, "inserted": inserted}

    def real_runtime_series(
        self,
        start: datetime,
        end: datetime,
        limit: int = HISTORY_MAX_POINTS,
    ) -> list[dict]:
        """Reconstruct "Laufzeit seit Start (real)" from motor_status_points.

        The real runtime grows linearly from 0 at each observed run start, so
        only the run boundaries are emitted (start 0, last running point,
        drop to 0 at the stop). Runs whose beginning lies in a recording gap
        are skipped instead of guessed. Read-only, no schema change.
        """
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if end <= start:
            return []
        running = REAL_RUNTIME_RUNNING_STATUS
        db = self.connect()
        try:
            anchor = db.execute(
                "SELECT recorded_at FROM motor_status_points WHERE recorded_at<=? "
                "AND status IS NOT NULL AND status<? ORDER BY recorded_at DESC LIMIT 1",
                (start.isoformat(), running),
            ).fetchone()
            if anchor is not None:
                rows = db.execute(
                    "SELECT recorded_at,status FROM motor_status_points "
                    "WHERE recorded_at>=? AND recorded_at<=? ORDER BY recorded_at",
                    (anchor["recorded_at"], end.isoformat()),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT recorded_at,status FROM motor_status_points "
                    "WHERE recorded_at<=? ORDER BY recorded_at",
                    (end.isoformat(),),
                ).fetchall()
        finally:
            db.close()

        runs: list[tuple[datetime, datetime, datetime | None]] = []
        run_start: datetime | None = None
        last_running: datetime | None = None
        previous: datetime | None = None
        unknown = False
        for row in rows:
            point = self._parse_recorded_at(row["recorded_at"])
            if point is None:
                continue
            status = int(row["status"]) if row["status"] is not None else None
            if previous is not None and (point - previous).total_seconds() > MOTOR_STATUS_GAP_SECONDS:
                if run_start is not None:
                    runs.append((run_start, last_running or run_start, None))
                run_start = None
                unknown = True
            if status is None:
                if run_start is not None:
                    runs.append((run_start, last_running or run_start, None))
                run_start = None
                unknown = True
            elif status < running:
                if run_start is not None:
                    runs.append((run_start, point, point))
                run_start = None
                unknown = False
            elif run_start is None and not unknown:
                run_start = point
            if status is not None and status >= running and run_start is not None:
                last_running = point
            previous = point
        if run_start is not None:
            last = last_running or run_start
            if (end - last).total_seconds() <= MOTOR_STATUS_GAP_SECONDS:
                last = end
            runs.append((run_start, last, None))

        points: list[tuple[datetime, int]] = []
        for run_from, run_to, stopped_at in runs:
            if run_to < start or run_from > end:
                continue
            first = max(start, run_from)
            last = min(end, run_to)
            points.append((first, int((first - run_from).total_seconds())))
            if last > first:
                points.append((last, int((last - run_from).total_seconds())))
            if stopped_at is not None and start <= stopped_at <= end:
                points.append((stopped_at, 0))
        if len(points) > limit:
            points = points[-limit:]
        title = HISTORY_REAL_RUNTIME_SERIES["title"]
        return [
            {
                "recorded_at": moment.isoformat(),
                "block": HISTORY_REAL_RUNTIME_SERIES["block"],
                "field_key": HISTORY_REAL_RUNTIME_SERIES["key"],
                "label": title,
                "raw": seconds,
                "value": round(seconds / 3600.0, 3),
                "unit": HISTORY_REAL_RUNTIME_SERIES["unit"],
                "rtt_ms": None,
                "series_id": HISTORY_REAL_RUNTIME_SERIES["id"],
            }
            for moment, seconds in points
        ]

    def motor_status_segments(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        """Return event-faithful status intervals while leaving outages empty."""
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if end <= start:
            return []
        db = self.connect()
        try:
            previous = db.execute(
                "SELECT recorded_at,status FROM motor_status_points "
                "WHERE recorded_at<? ORDER BY recorded_at DESC LIMIT 1",
                (start.isoformat(),),
            ).fetchone()
            rows = db.execute(
                "SELECT recorded_at,status FROM motor_status_points "
                "WHERE recorded_at>=? AND recorded_at<=? ORDER BY recorded_at",
                (start.isoformat(), end.isoformat()),
            )

            def parsed_points():
                if previous is not None:
                    yield previous
                yield from rows

            def normalized_points():
                for row in parsed_points():
                    point_time = self._parse_recorded_at(row["recorded_at"])
                    if point_time is None:
                        continue
                    status = (
                        int(row["status"])
                        if row["status"] is not None
                        else None
                    )
                    yield point_time, status

            iterator = iter(normalized_points())
            current = next(iterator, None)
            following = next(iterator, None)
            segments: list[dict] = []
            while current is not None:
                point_time, status = current
                next_time = following[0] if following is not None else None
                next_status = following[1] if following is not None else None
                if status is not None and not (
                    point_time < start
                    and (start - point_time).total_seconds() > MOTOR_STATUS_GAP_SECONDS
                ):
                    segment_start = max(start, point_time)
                    if next_time is not None and next_status is None:
                        segment_end = min(end, next_time)
                    elif (
                        next_time is not None
                        and (next_time - point_time).total_seconds()
                        <= MOTOR_STATUS_GAP_SECONDS
                    ):
                        segment_end = min(end, next_time)
                    elif (
                        next_time is None
                        and (end - point_time).total_seconds()
                        <= MOTOR_STATUS_GAP_SECONDS
                    ):
                        segment_end = end
                    else:
                        segment_end = min(
                            end,
                            point_time
                            + timedelta(seconds=MOTOR_STATUS_HEARTBEAT_SECONDS),
                        )
                    if segment_end > segment_start:
                        previous_segment = segments[-1] if segments else None
                        if (
                            previous_segment is not None
                            and previous_segment["status"] == status
                            and previous_segment["to"] == segment_start.isoformat()
                        ):
                            previous_segment["to"] = segment_end.isoformat()
                        else:
                            segments.append({
                                "from": segment_start.isoformat(),
                                "to": segment_end.isoformat(),
                                "status": status,
                            })
                current = following
                following = next(iterator, None)
            return segments
        finally:
            db.close()

    def _operating_seconds_near(
        self, db: sqlite3.Connection, recorded_at: str, *, before: bool
    ) -> int | None:
        """Hka_Bd.ulBetriebssekunden from the history sample closest to a point."""
        index = HISTORY_SERIES_INDEX.get((22, "Hka_Bd.ulBetriebssekunden"))
        point = self._parse_recorded_at(recorded_at)
        if index is None or point is None:
            return None
        query = (
            "SELECT recorded_at,sample_json FROM history_samples WHERE recorded_at<=? "
            "ORDER BY recorded_at DESC LIMIT 8"
            if before
            else "SELECT recorded_at,sample_json FROM history_samples WHERE recorded_at>=? "
            "ORDER BY recorded_at LIMIT 8"
        )
        for row in db.execute(query, (recorded_at,)):
            sample_at = self._parse_recorded_at(row["recorded_at"])
            if sample_at is None or abs((sample_at - point).total_seconds()) > REAL_RUNTIME_SAMPLE_WINDOW_SECONDS:
                return None
            try:
                sample = json.loads(row["sample_json"])["values"][index]
                raw = int(sample[0]) if sample and not isinstance(sample[0], bool) else None
            except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                raw = None
            if raw is not None and raw >= 0:
                return raw
        return None

    def real_runtime_history(self) -> dict:
        """Reconstruct the current run/stop boundaries from motor_status_points.

        Read-only on its own WAL connection (never ``self.lock``), so a web
        request can not delay the monitor commit.
        """
        running = REAL_RUNTIME_RUNNING_STATUS
        db = self.connect()
        try:
            standstill = db.execute(
                "SELECT recorded_at FROM motor_status_points WHERE status IS NOT NULL "
                "AND status<? ORDER BY recorded_at DESC LIMIT 1",
                (running,),
            ).fetchone()
            if standstill is not None:
                start = db.execute(
                    "SELECT recorded_at FROM motor_status_points WHERE recorded_at>? "
                    "AND status>=? ORDER BY recorded_at LIMIT 1",
                    (standstill["recorded_at"], running),
                ).fetchone()
                gap = db.execute(
                    "SELECT 1 FROM motor_status_points WHERE recorded_at>? AND status IS NULL LIMIT 1",
                    (standstill["recorded_at"],),
                ).fetchone() is not None
            else:
                start = db.execute(
                    "SELECT recorded_at FROM motor_status_points WHERE status>=? "
                    "ORDER BY recorded_at LIMIT 1",
                    (running,),
                ).fetchone()
                gap = start is not None and db.execute(
                    "SELECT 1 FROM motor_status_points WHERE recorded_at>? AND status IS NULL LIMIT 1",
                    (start["recorded_at"],),
                ).fetchone() is not None
            last_running = db.execute(
                "SELECT recorded_at FROM motor_status_points WHERE status>=? "
                "ORDER BY recorded_at DESC LIMIT 1",
                (running,),
            ).fetchone()
            stopped = None
            stop_gap = False
            if last_running is not None:
                stopped = db.execute(
                    "SELECT recorded_at FROM motor_status_points WHERE recorded_at>? "
                    "AND status IS NOT NULL AND status<? ORDER BY recorded_at LIMIT 1",
                    (last_running["recorded_at"], running),
                ).fetchone()
                if stopped is not None:
                    stop_gap = db.execute(
                        "SELECT 1 FROM motor_status_points WHERE recorded_at>? AND recorded_at<? "
                        "AND status IS NULL LIMIT 1",
                        (last_running["recorded_at"], stopped["recorded_at"]),
                    ).fetchone() is not None
            operating_at_standstill = (
                self._operating_seconds_near(db, standstill["recorded_at"], before=True)
                if standstill is not None and gap
                else None
            )
        finally:
            db.close()
        return {
            "standstill_at": self._parse_recorded_at(standstill["recorded_at"]) if standstill else None,
            "start_at": self._parse_recorded_at(start["recorded_at"]) if start else None,
            "gap": bool(gap),
            "operating_seconds_at_standstill": operating_at_standstill,
            "stopped_since": self._parse_recorded_at(stopped["recorded_at"]) if stopped else None,
            "stop_gap": stop_gap,
        }

    def _adaptive_series_ids_db(self, db: sqlite3.Connection, rows: list[dict]) -> dict[tuple[int, str], int]:
        identities: list[tuple[int, str]] = []
        for row in rows:
            block = int(row["block"])
            key = str(row["key"])
            identities.append((block, key))
            if (block, key) in self._adaptive_series_cache:
                continue
            db.execute(
                "INSERT INTO adaptive_series(block,field_key,label,unit,value_type) VALUES(?,?,?,?,?) "
                "ON CONFLICT(block,field_key) DO UPDATE SET label=excluded.label,unit=excluded.unit,value_type=excluded.value_type",
                (
                    block,
                    key,
                    str(row.get("label", key)),
                    str(row.get("unit", "")),
                    str(row.get("type", type(row.get("value")).__name__)),
                ),
            )
        if not identities:
            return {}
        missing_blocks = sorted({item[0] for item in identities if item not in self._adaptive_series_cache})
        rows_db = []
        if missing_blocks:
            rows_db = db.execute(
                "SELECT id,block,field_key FROM adaptive_series WHERE block IN (%s)"
                % ",".join("?" for _ in missing_blocks),
                tuple(missing_blocks),
            ).fetchall()
            self._adaptive_series_cache.update({
                (int(row["block"]), str(row["field_key"])): int(row["id"])
                for row in rows_db
            })
        wanted = set(identities)
        return {
            identity: self._adaptive_series_cache[identity]
            for identity in wanted
            if identity in self._adaptive_series_cache
        }

    def record_adaptive_snapshot(self, recorded_at: str, rows: list[dict]) -> dict:
        """Store one compressed all-live snapshot and tag motor event windows."""
        if not rows:
            return {"stored": False, "series": 0, "preserve": False, "motor_rpm": 0.0}
        timestamp = _history_datetime(recorded_at, "Messzeit").astimezone(timezone.utc)
        rpm = 0.0
        for row in rows:
            if int(row["block"]) == MOTOR_SPEED_BLOCK and str(row["key"]) == MOTOR_SPEED_KEY:
                try:
                    rpm = max(0.0, float(row.get("value", row.get("raw", 0)) or 0))
                except (TypeError, ValueError):
                    rpm = 0.0
                break
        with self.lock, self.database() as db:
            series_ids = self._adaptive_series_ids_db(db, rows)
            values = []
            for row in rows:
                identity = (int(row["block"]), str(row["key"]))
                series_id = series_ids.get(identity)
                if series_id is None:
                    continue
                values.append([
                    series_id,
                    _json_value(row.get("raw")),
                    _json_value(row.get("value")),
                ])
            values.sort(key=lambda item: item[0])
            state = db.execute(
                "SELECT motor_running,last_motor_active_at FROM adaptive_history_state WHERE singleton=1"
            ).fetchone()
            was_running = bool(state["motor_running"]) if state else False
            last_active_text = state["last_motor_active_at"] if state else None
            motor_running = rpm > 0
            if motor_running and not was_running:
                preserve_from = (timestamp - timedelta(seconds=ADAPTIVE_EVENT_MARGIN_SECONDS)).isoformat()
                db.execute(
                    "UPDATE adaptive_raw_samples SET preserve=1 WHERE recorded_at>=? AND recorded_at<=?",
                    (preserve_from, timestamp.isoformat()),
                )
            if motor_running:
                last_active_text = timestamp.isoformat()
            preserve = motor_running
            if not preserve and last_active_text:
                last_active = _history_datetime(str(last_active_text), "letzter Motorlauf")
                preserve = (timestamp - last_active).total_seconds() <= ADAPTIVE_EVENT_MARGIN_SECONDS
            db.execute(
                "INSERT OR REPLACE INTO adaptive_raw_samples(recorded_at,motor_rpm,preserve,sample_blob) VALUES(?,?,?,?)",
                (timestamp.isoformat(), rpm, int(preserve), _compressed_json({"version": 1, "values": values})),
            )
            db.execute(
                "UPDATE adaptive_history_state SET motor_running=?,last_motor_active_at=? WHERE singleton=1",
                (int(motor_running), last_active_text),
            )
        return {"stored": True, "series": len(values), "preserve": bool(preserve), "motor_rpm": rpm}

    @staticmethod
    def _rollup_payload(sample_rows: list[sqlite3.Row]) -> tuple[dict, int]:
        series: dict[int, dict] = {}
        source_count = 0
        for sample_row in sample_rows:
            try:
                payload = _uncompressed_json(bytes(sample_row["sample_blob"]))
                values = payload.get("values", []) if isinstance(payload, dict) else []
            except (KeyError, TypeError, ValueError, zlib.error, json.JSONDecodeError):
                continue
            source_count += 1
            for item in values:
                if not isinstance(item, list) or len(item) < 3:
                    continue
                series_id, value = int(item[0]), item[2]
                stat = series.setdefault(series_id, {
                    "id": series_id,
                    "first": value,
                    "last": value,
                    "minimum": None,
                    "maximum": None,
                    "sum": 0.0,
                    "number_count": 0,
                    "count": 0,
                    "changes": 0,
                })
                if stat["count"] and value != stat["last"]:
                    stat["changes"] += 1
                stat["last"] = value
                stat["count"] += 1
                try:
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError
                except (TypeError, ValueError):
                    continue
                stat["minimum"] = number if stat["minimum"] is None else min(stat["minimum"], number)
                stat["maximum"] = number if stat["maximum"] is None else max(stat["maximum"], number)
                stat["sum"] += number
                stat["number_count"] += 1
        output = []
        for series_id in sorted(series):
            stat = series[series_id]
            average = (
                stat["sum"] / stat["number_count"]
                if stat["number_count"]
                else None
            )
            output.append({
                "id": series_id,
                "first": stat["first"],
                "last": stat["last"],
                "min": stat["minimum"],
                "max": stat["maximum"],
                "avg": average,
                "count": stat["count"],
                "changes": stat["changes"],
            })
        return {"version": 1, "series": output}, source_count

    def compact_adaptive_history(self, now: datetime | None = None, max_buckets: int = ADAPTIVE_COMPACTION_BUCKETS) -> dict:
        """Roll idle raw data older than 24 h into 15-minute statistics."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        raw_cutoff = (now - timedelta(hours=ADAPTIVE_RAW_RETENTION_HOURS)).isoformat()
        retention_cutoff = (now - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
        compacted_rows = compacted_buckets = 0
        with self.lock, self.database() as db:
            buckets = db.execute(
                "SELECT CAST(CAST(strftime('%s',recorded_at) AS INTEGER)/? AS INTEGER) AS bucket "
                "FROM adaptive_raw_samples WHERE preserve=0 AND recorded_at<? "
                "GROUP BY bucket ORDER BY bucket LIMIT ?",
                (ADAPTIVE_ROLLUP_SECONDS, raw_cutoff, max(1, int(max_buckets))),
            ).fetchall()
            for bucket_row in buckets:
                bucket = int(bucket_row["bucket"])
                start = datetime.fromtimestamp(bucket * ADAPTIVE_ROLLUP_SECONDS, timezone.utc)
                end = start + timedelta(seconds=ADAPTIVE_ROLLUP_SECONDS)
                samples = db.execute(
                    "SELECT recorded_at,sample_blob FROM adaptive_raw_samples "
                    "WHERE preserve=0 AND recorded_at>=? AND recorded_at<? ORDER BY recorded_at",
                    (start.isoformat(), end.isoformat()),
                ).fetchall()
                if not samples:
                    continue
                payload, source_count = self._rollup_payload(samples)
                if source_count:
                    db.execute(
                        "INSERT OR REPLACE INTO adaptive_rollups(bucket_start,bucket_seconds,source_count,rollup_blob) VALUES(?,?,?,?)",
                        (start.isoformat(), ADAPTIVE_ROLLUP_SECONDS, source_count, _compressed_json(payload)),
                    )
                db.execute(
                    "DELETE FROM adaptive_raw_samples WHERE preserve=0 AND recorded_at>=? AND recorded_at<?",
                    (start.isoformat(), end.isoformat()),
                )
                compacted_rows += len(samples)
                compacted_buckets += 1
            expired_raw = db.execute(
                "DELETE FROM adaptive_raw_samples WHERE recorded_at<?", (retention_cutoff,)
            ).rowcount
            expired_rollups = db.execute(
                "DELETE FROM adaptive_rollups WHERE bucket_start<?", (retention_cutoff,)
            ).rowcount
            db.execute(
                "DELETE FROM api_requests WHERE status='completed' AND created_at<?",
                ((now - timedelta(days=7)).isoformat(),),
            )
        return {
            "compacted_rows": compacted_rows,
            "compacted_buckets": compacted_buckets,
            "expired_raw": max(0, int(expired_raw)),
            "expired_rollups": max(0, int(expired_rollups)),
        }

    def adaptive_catalog(self) -> list[dict]:
        with self.database() as db:
            rows = db.execute(
                "SELECT id,block,field_key,label,unit,value_type FROM adaptive_series ORDER BY block,field_key"
            ).fetchall()
        return [dict(row) for row in rows]

    def adaptive_status(self) -> dict:
        # Read-only full-table aggregate; must not stall the monitor's writes.
        with self.database() as db:
            raw = db.execute(
                "SELECT COUNT(*) AS count,SUM(preserve) AS preserved,MIN(recorded_at) AS oldest,"
                "MAX(recorded_at) AS newest,COALESCE(SUM(length(sample_blob)),0) AS bytes FROM adaptive_raw_samples"
            ).fetchone()
            rollup = db.execute(
                "SELECT COUNT(*) AS count,MIN(bucket_start) AS oldest,MAX(bucket_start) AS newest,"
                "COALESCE(SUM(length(rollup_blob)),0) AS bytes FROM adaptive_rollups"
            ).fetchone()
        return {
            "raw": dict(raw),
            "rollups": dict(rollup),
            "raw_retention_hours": ADAPTIVE_RAW_RETENTION_HOURS,
            "event_margin_seconds": ADAPTIVE_EVENT_MARGIN_SECONDS,
            "rollup_seconds": ADAPTIVE_ROLLUP_SECONDS,
        }

    def adaptive_history_between(
        self,
        block: int,
        key: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[dict]:
        block = int(block)
        key = str(key)
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        limit = min(HISTORY_MAX_POINTS, max(1, int(limit)))
        # Read-only: no writer lock. On ODM the sampling query alone takes
        # 0.36 s (24 h) to 1.8 s (30 d) and used to block monitor commits.
        with self.database() as db:
            series = db.execute(
                "SELECT id,label,unit,value_type FROM adaptive_series WHERE block=? AND field_key=?",
                (block, key),
            ).fetchone()
            if series is None:
                return []
            seconds = max(1.0, (end - start).total_seconds())
            bucket_seconds = max(1.0, seconds / limit)
            raw_rows = db.execute(
                "WITH sampled AS (SELECT MIN(recorded_at) AS recorded_at FROM adaptive_raw_samples "
                "WHERE recorded_at>=? AND recorded_at<=? "
                "GROUP BY CAST(((julianday(recorded_at)-julianday(?))*86400.0)/? AS INTEGER)) "
                "SELECT r.recorded_at,r.sample_blob FROM adaptive_raw_samples AS r "
                "JOIN sampled AS s ON s.recorded_at=r.recorded_at ORDER BY r.recorded_at",
                (start.isoformat(), end.isoformat(), start.isoformat(), bucket_seconds),
            ).fetchall()
            rollup_rows = db.execute(
                "SELECT bucket_start,bucket_seconds,source_count,rollup_blob FROM adaptive_rollups "
                "WHERE bucket_start>=? AND bucket_start<=? ORDER BY bucket_start",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        series_id = int(series["id"])
        points: list[dict] = []
        for row in raw_rows:
            try:
                payload = _uncompressed_json(bytes(row["sample_blob"]))
                item = next(item for item in payload.get("values", []) if int(item[0]) == series_id)
            except (StopIteration, KeyError, TypeError, ValueError, zlib.error, json.JSONDecodeError):
                continue
            points.append({
                "recorded_at": row["recorded_at"],
                "kind": "raw",
                "value": item[2],
                "raw": item[1],
            })
        for row in rollup_rows:
            try:
                payload = _uncompressed_json(bytes(row["rollup_blob"]))
                item = next(item for item in payload.get("series", []) if int(item["id"]) == series_id)
            except (StopIteration, KeyError, TypeError, ValueError, zlib.error, json.JSONDecodeError):
                continue
            points.append({
                "recorded_at": row["bucket_start"],
                "kind": "rollup",
                "value": item.get("avg") if item.get("avg") is not None else item.get("last"),
                "first": item.get("first"),
                "last": item.get("last"),
                "min": item.get("min"),
                "max": item.get("max"),
                "avg": item.get("avg"),
                "count": item.get("count"),
                "changes": item.get("changes"),
                "bucket_seconds": int(row["bucket_seconds"]),
            })
        points.sort(key=lambda item: str(item["recorded_at"]))
        if len(points) > limit:
            if limit == 1:
                points = [points[-1]]
            else:
                step = (len(points) - 1) / (limit - 1)
                points = [points[round(index * step)] for index in range(limit)]
        return points

    def api_tokens(self) -> list[dict]:
        with self.lock, self.database() as db:
            rows = db.execute(
                "SELECT id,name,owner_username,token_prefix,scopes_json,enabled,created_at,updated_at,last_used_at "
                "FROM api_tokens ORDER BY name"
            ).fetchall()
        return [{
            **{key: row[key] for key in ("id", "name", "owner_username", "token_prefix", "created_at", "updated_at", "last_used_at")},
            "scopes": json.loads(row["scopes_json"]),
            "enabled": bool(row["enabled"]),
        } for row in rows]

    def create_api_token(self, owner_username: str, name: object, scopes: object) -> dict:
        token_name = str(name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9 ._-]{3,64}", token_name):
            raise ValueError("Tokenname benötigt 3–64 lesbare Zeichen")
        clean_scopes = _normalize_api_scopes(scopes)
        secret = "odm_" + secrets.token_urlsafe(32)
        now = _now()
        try:
            with self.lock, self.database() as db:
                cursor = db.execute(
                    "INSERT INTO api_tokens(name,owner_username,token_prefix,token_hash,scopes_json,enabled,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,1,?,?)",
                    (
                        token_name,
                        str(owner_username),
                        secret[:16],
                        hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                        json.dumps(clean_scopes, separators=(",", ":")),
                        now,
                        now,
                    ),
                )
                token_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Tokenname ist bereits vergeben") from exc
        return {
            "id": token_id,
            "name": token_name,
            "owner_username": str(owner_username),
            "token_prefix": secret[:16],
            "token": secret,
            "scopes": clean_scopes,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
            "last_used_at": None,
        }

    def update_api_token(self, token_id: int, *, scopes: object, enabled: object) -> dict:
        clean_scopes = _normalize_api_scopes(scopes)
        if not isinstance(enabled, bool):
            raise ValueError("enabled muss true oder false sein")
        now = _now()
        with self.lock, self.database() as db:
            changed = db.execute(
                "UPDATE api_tokens SET scopes_json=?,enabled=?,updated_at=? WHERE id=?",
                (json.dumps(clean_scopes, separators=(",", ":")), int(enabled), now, int(token_id)),
            ).rowcount
        if not changed:
            raise KeyError("API-Token nicht gefunden")
        return next(item for item in self.api_tokens() if int(item["id"]) == int(token_id))

    def delete_api_token(self, token_id: int) -> dict:
        token_id = int(token_id)
        with self.lock, self.database() as db:
            pending = db.execute(
                "SELECT 1 FROM api_requests WHERE token_id=? AND status='pending' LIMIT 1",
                (token_id,),
            ).fetchone()
            if pending is not None:
                raise APIRequestConflictError(
                    "API-Token kann während einer laufenden oder unklaren Schreibaktion nicht gelöscht werden"
                )
            db.execute("DELETE FROM api_requests WHERE token_id=?", (token_id,))
            changed = db.execute("DELETE FROM api_tokens WHERE id=?", (token_id,)).rowcount
        if not changed:
            raise KeyError("API-Token nicht gefunden")
        return {"id": token_id}

    def authenticate_api_token(self, secret: str) -> dict | None:
        digest = hashlib.sha256(str(secret).encode("utf-8")).hexdigest()
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT id,name,owner_username,token_prefix,scopes_json,enabled,created_at,updated_at,last_used_at "
                "FROM api_tokens WHERE token_hash=?",
                (digest,),
            ).fetchone()
            if row is None or not bool(row["enabled"]):
                return None
            now = datetime.now(timezone.utc)
            last_used = row["last_used_at"]
            if not last_used or (now - _history_datetime(str(last_used), "Token-Nutzung")).total_seconds() >= 60:
                db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now.isoformat(), int(row["id"])))
        return {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "owner_username": str(row["owner_username"]),
            "token_prefix": str(row["token_prefix"]),
            "scopes": json.loads(row["scopes_json"]),
        }

    def reserve_api_request(
        self,
        token_id: int,
        request_id: str,
        request_hash: str,
    ) -> dict | None:
        """Atomically reserve one idempotent action or return its prior result."""
        token_id = int(token_id)
        request_id = str(request_id)
        request_hash = str(request_hash)
        with self.lock, self.database() as db:
            inserted = db.execute(
                "INSERT INTO api_requests("
                "token_id,request_id,created_at,request_hash,status,response_json"
                ") VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(token_id,request_id) DO NOTHING",
                (token_id, request_id, _now(), request_hash, "pending", "{}"),
            ).rowcount
            if inserted == 1:
                return None
            row = db.execute(
                "SELECT request_hash,status,response_json FROM api_requests "
                "WHERE token_id=? AND request_id=?",
                (token_id, request_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("API-Anfrage konnte nicht atomar reserviert werden")
        if not row["request_hash"] or not hmac.compare_digest(
            str(row["request_hash"]), request_hash
        ):
            raise APIRequestConflictError(
                "request_id wurde bereits für eine andere Aktion verwendet"
            )
        if str(row["status"]) != "completed":
            raise APIRequestConflictError(
                "request_id wird bereits verarbeitet oder ihr Ergebnis ist noch unklar"
            )
        return json.loads(str(row["response_json"]))

    def complete_api_request(
        self,
        token_id: int,
        request_id: str,
        request_hash: str,
        response: dict,
    ) -> None:
        response_json = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        with self.lock, self.database() as db:
            changed = db.execute(
                "UPDATE api_requests SET status='completed',response_json=? "
                "WHERE token_id=? AND request_id=? AND request_hash=? AND status='pending'",
                (response_json, int(token_id), str(request_id), str(request_hash)),
            ).rowcount
        if changed != 1:
            raise RuntimeError("reservierte API-Anfrage konnte nicht abgeschlossen werden")

    def cancel_api_request(self, token_id: int, request_id: str, request_hash: str) -> None:
        """Release a reservation only when validation proved no write was attempted."""
        with self.lock, self.database() as db:
            db.execute(
                "DELETE FROM api_requests WHERE token_id=? AND request_id=? "
                "AND request_hash=? AND status='pending'",
                (int(token_id), str(request_id), str(request_hash)),
            )

    def audit(self, recorded_at: str, username: str, block: int, audit: dict) -> None:
        with self.lock, self.database() as db:
            db.execute(
                "INSERT INTO write_audit(recorded_at, username, block, audit_json) VALUES(?,?,?,?)",
                (recorded_at, username, block, json.dumps(audit, ensure_ascii=False)),
            )

    def update_audit(self, recorded_at: str, username: str, action: str,
                     request_id: str | None, version: str | None, details: dict) -> None:
        with self.lock, self.database() as db:
            db.execute(
                "INSERT INTO update_audit(recorded_at,username,action,request_id,version,details_json) "
                "VALUES(?,?,?,?,?,?)",
                (str(recorded_at), str(username), str(action), request_id, version,
                 json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
            )

    def notification_audit(
        self, recorded_at: str, actor: str, action: str, details: dict
    ) -> None:
        with self.lock, self.database() as db:
            db.execute(
                "INSERT INTO notification_audit(recorded_at,actor,action,details_json) "
                "VALUES(?,?,?,?)",
                (
                    str(recorded_at), str(actor), str(action),
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def notification_audits(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(200, int(limit)))
        with self.lock, self.database() as db:
            rows = db.execute(
                "SELECT id,recorded_at,actor,action,details_json FROM notification_audit "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(str(item.pop("details_json")))
            except (TypeError, ValueError, json.JSONDecodeError):
                item["details"] = {}
            result.append(item)
        return result

    def notification_state(self) -> dict:
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT state_json FROM notification_state WHERE singleton=1"
            ).fetchone()
        try:
            state = json.loads(str(row["state_json"])) if row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return state if isinstance(state, dict) else {}

    def notification_dispatch_pause(self) -> dict:
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT paused_revision,paused_reason,updated_at "
                "FROM notification_dispatch_state WHERE singleton=1"
            ).fetchone()
        if row is None or row["paused_revision"] is None:
            return {"paused_revision": None, "paused_reason": None, "updated_at": None}
        return {
            "paused_revision": int(row["paused_revision"]),
            "paused_reason": str(row["paused_reason"] or ""),
            "updated_at": str(row["updated_at"]),
        }

    def set_notification_dispatch_pause(
        self, revision: int, reason: str, recorded_at: str
    ) -> None:
        with self.lock, self.database() as db:
            db.execute(
                "UPDATE notification_dispatch_state SET "
                "paused_revision=?,paused_reason=?,updated_at=? WHERE singleton=1",
                (int(revision), str(reason)[:1000], str(recorded_at)),
            )

    def clear_notification_dispatch_pause(self, recorded_at: str) -> None:
        with self.lock, self.database() as db:
            db.execute(
                "UPDATE notification_dispatch_state SET "
                "paused_revision=NULL,paused_reason=NULL,updated_at=? WHERE singleton=1",
                (str(recorded_at),),
            )

    def record_notification_observation(
        self,
        recorded_at: str,
        state: dict | None,
        messages: list[dict],
        audit: dict | None = None,
    ) -> int:
        """Atomically advance optional edge state and enqueue deduplicated messages."""
        state_json = (
            json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            if isinstance(state, dict)
            else None
        )
        inserted = 0
        dropped = 0
        with self.lock, self.database() as db:
            active = int(db.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE status IN ('pending','sending','failed')"
            ).fetchone()[0])
            remaining = max(0, EMAIL_OUTBOX_MAX_ACTIVE - active)
            if state_json is not None:
                db.execute(
                    "UPDATE notification_state SET updated_at=?,state_json=? WHERE singleton=1",
                    (str(recorded_at), state_json),
                )
            for message in messages:
                if remaining <= 0:
                    dropped += 1
                    continue
                changed = db.execute(
                    "INSERT INTO notification_outbox("
                    "event_key,category,recipient,created_at,subject,body,message_id,"
                    "event_json,status,attempts,next_attempt_at,last_attempt_at,sent_at,last_error"
                    ") VALUES(?,?,?,?,?,?,?,?,'pending',0,?,NULL,NULL,NULL) "
                    "ON CONFLICT(event_key) DO NOTHING",
                    (
                        str(message["event_key"]),
                        str(message["category"]),
                        str(message["recipient"]),
                        str(recorded_at),
                        str(message["subject"]),
                        str(message["body"]),
                        str(message["message_id"]),
                        json.dumps(message.get("context") or {}, ensure_ascii=False),
                        str(recorded_at),
                    ),
                ).rowcount
                inserted += changed
                remaining -= changed
            if audit is not None:
                details = dict(audit.get("details") or {})
                details["queued"] = inserted
                details["dropped"] = dropped
                db.execute(
                    "INSERT INTO notification_audit(recorded_at,actor,action,details_json) "
                    "VALUES(?,?,?,?)",
                    (
                        str(recorded_at),
                        str(audit.get("actor") or "system"),
                        str(audit.get("action") or "enqueue"),
                        json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
            if dropped and audit is None:
                db.execute(
                    "INSERT INTO notification_audit(recorded_at,actor,action,details_json) "
                    "VALUES(?,?,?,?)",
                    (
                        str(recorded_at), "system", "queue-overflow",
                        json.dumps({"dropped": dropped}, separators=(",", ":")),
                    ),
                )
        return inserted

    def enqueue_notification_messages(
        self,
        recorded_at: str,
        messages: list[dict],
        audit: dict | None = None,
    ) -> int:
        """Queue messages without reading or replacing the detector edge state."""
        return self.record_notification_observation(recorded_at, None, messages, audit)

    def claim_notification(
        self, recorded_at: str, *, test_only: bool = False
    ) -> dict | None:
        category_clause = " AND category='test'" if test_only else ""
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT * FROM notification_outbox WHERE status IN ('pending','failed') "
                "AND attempts<? AND COALESCE(next_attempt_at,created_at)<=?"
                + category_clause + " ORDER BY id LIMIT 1",
                (EMAIL_MAX_ATTEMPTS, str(recorded_at)),
            ).fetchone()
            if row is None:
                return None
            changed = db.execute(
                "UPDATE notification_outbox SET status='sending',attempts=attempts+1,"
                "last_attempt_at=?,last_error=NULL WHERE id=? AND status IN ('pending','failed')",
                (str(recorded_at), int(row["id"])),
            ).rowcount
            if changed != 1:
                return None
            row = db.execute(
                "SELECT * FROM notification_outbox WHERE id=?",
                (int(row["id"]),),
            ).fetchone()
        item = dict(row)
        try:
            item["context"] = json.loads(str(item.pop("event_json")))
        except (TypeError, ValueError, json.JSONDecodeError):
            item["context"] = {}
        return item

    def finish_notification(
        self,
        notification_id: int,
        *,
        sent: bool,
        recorded_at: str,
        error: str | None = None,
        next_attempt_at: str | None = None,
        terminal: bool = False,
    ) -> None:
        with self.lock, self.database() as db:
            changed = db.execute(
                "UPDATE notification_outbox SET status=?,sent_at=?,next_attempt_at=?,"
                "last_error=? WHERE id=? AND status='sending'",
                (
                    "sent" if sent else ("dead" if terminal else "failed"),
                    str(recorded_at) if sent else None,
                    None if sent or terminal else next_attempt_at,
                    None if sent else str(error or "SMTP-Versand fehlgeschlagen")[:1000],
                    int(notification_id),
                ),
            ).rowcount
        if changed != 1:
            raise RuntimeError("E-Mail-Ausgang konnte nicht atomar abgeschlossen werden")

    def cancel_notification(
        self, notification_id: int, recorded_at: str, reason: str
    ) -> None:
        with self.lock, self.database() as db:
            changed = db.execute(
                "UPDATE notification_outbox SET status='canceled',sent_at=NULL,"
                "next_attempt_at=NULL,last_error=? WHERE id=? AND status='sending'",
                (str(reason)[:1000], int(notification_id)),
            ).rowcount
        if changed != 1:
            raise RuntimeError("E-Mail-Ausgang konnte nicht widerrufen werden")

    def cancel_obsolete_notifications(
        self,
        recorded_at: str,
        recipients: list[str],
        categories: dict,
        enabled: bool,
    ) -> int:
        active_recipients = set(recipients)
        canceled = 0
        with self.lock, self.database() as db:
            rows = db.execute(
                "SELECT id,recipient,category FROM notification_outbox "
                "WHERE status IN ('pending','failed')"
            ).fetchall()
            for row in rows:
                category = str(row["category"])
                allowed = str(row["recipient"]) in active_recipients and (
                    category == "test" or (bool(enabled) and bool(categories.get(category)))
                )
                if allowed:
                    continue
                canceled += db.execute(
                    "UPDATE notification_outbox SET status='canceled',"
                    "next_attempt_at=NULL,last_error='Empfänger oder Kategorie entfernt' "
                    "WHERE id=? AND status IN ('pending','failed')",
                    (int(row["id"]),),
                ).rowcount
        return canceled

    def notification_outbox(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(200, int(limit)))
        with self.lock, self.database() as db:
            rows = db.execute(
                "SELECT id,event_key,category,recipient,created_at,subject,message_id,"
                "status,attempts,next_attempt_at,last_attempt_at,sent_at,last_error "
                "FROM notification_outbox ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def notification_summary(self) -> dict:
        with self.lock, self.database() as db:
            counts = {
                str(row["status"]): int(row["count"])
                for row in db.execute(
                    "SELECT status,COUNT(*) AS count FROM notification_outbox GROUP BY status"
                ).fetchall()
            }
            last_sent = db.execute(
                "SELECT sent_at,subject FROM notification_outbox WHERE status='sent' "
                "ORDER BY sent_at DESC,id DESC LIMIT 1"
            ).fetchone()
            last_error = db.execute(
                "SELECT last_attempt_at,last_error,subject FROM notification_outbox "
                "WHERE status IN ('failed','dead') AND last_error IS NOT NULL "
                "ORDER BY last_attempt_at DESC,id DESC LIMIT 1"
            ).fetchone()
        return {
            "counts": {status: counts.get(status, 0) for status in ("pending", "sending", "sent", "failed", "dead", "canceled")},
            "last_sent": dict(last_sent) if last_sent else None,
            "last_error": dict(last_error) if last_error else None,
        }

    def notification_test_request_exists(self, request_id: str) -> bool:
        prefix = f"test:{str(request_id)}:to:%"
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT 1 FROM notification_outbox WHERE event_key LIKE ? LIMIT 1",
                (prefix,),
            ).fetchone()
        return row is not None

    def recent_test_notification(self, cutoff: str) -> bool:
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT 1 FROM notification_outbox WHERE category='test' "
                "AND created_at>=? LIMIT 1",
                (str(cutoff),),
            ).fetchone()
        return row is not None

    def purge_notifications(self, now: datetime) -> dict:
        outbox_cutoff = (now - timedelta(days=EMAIL_OUTBOX_RETENTION_DAYS)).isoformat()
        audit_cutoff = (now - timedelta(days=EMAIL_AUDIT_RETENTION_DAYS)).isoformat()
        with self.lock, self.database() as db:
            outbox = db.execute(
                "DELETE FROM notification_outbox WHERE status IN ('sent','dead','canceled') "
                "AND created_at<?",
                (outbox_cutoff,),
            ).rowcount
            audits = db.execute(
                "DELETE FROM notification_audit WHERE recorded_at<?",
                (audit_cutoff,),
            ).rowcount
        return {"outbox": outbox, "audit": audits}

    def recent_confirmed_manual_reset(
        self,
        service_code: int,
        episode_started_at: str,
        recorded_at: str,
        window_seconds: int = 120,
    ) -> bool:
        try:
            at = _history_datetime(str(recorded_at), "Benachrichtigungszeit")
            episode_start = _history_datetime(
                str(episode_started_at), "Beginn der Störungsepisode"
            )
        except ValueError:
            return False
        if episode_start > at:
            return False
        cutoff_dt = max(
            episode_start,
            at - timedelta(seconds=max(1, int(window_seconds))),
        )
        with self.lock, self.database() as db:
            rows = db.execute(
                "SELECT service_code_raw,details_json FROM service_reset_write_intents "
                "WHERE created_at>=? AND created_at<=? "
                "ORDER BY created_at DESC LIMIT 50",
                (cutoff_dt.isoformat(), at.isoformat()),
            ).fetchall()
        for row in rows:
            try:
                details = json.loads(str(row["details_json"]))
                confirmed_at = _history_datetime(
                    str(details.get("clear_confirmed_at") or ""),
                    "Bestätigte Entstörzeit",
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                int(row["service_code_raw"]) + 100 == int(service_code)
                and details.get("clear_confirmed") is True
                and cutoff_dt <= confirmed_at <= at
            ):
                return True
        return False

    def begin_service_reset_write_intent(
        self,
        intent_id: str,
        recorded_at: str,
        username: str,
        service_code_raw: int,
        fault_count: int,
        fault_runtime_seconds: int,
        details: dict,
    ) -> None:
        """Persist the at-most-once guard before a reset telegram may leave."""
        with self.lock, self.database() as db:
            db.execute(
                "INSERT INTO service_reset_write_intents("
                "intent_id,created_at,updated_at,username,service_code_raw,"
                "fault_count,fault_runtime_seconds,status,resolution,details_json"
                ") VALUES(?,?,?,?,?,?,?,'pending',NULL,?)",
                (
                    str(intent_id), str(recorded_at), str(recorded_at), str(username),
                    int(service_code_raw), int(fault_count), int(fault_runtime_seconds),
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def mark_service_reset_clear_confirmed(
        self, intent_id: str, confirmed_at: str
    ) -> None:
        """Publish a durable clear marker before the serial lease is released."""
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT details_json FROM service_reset_write_intents "
                "WHERE intent_id=? AND status='pending'",
                (str(intent_id),),
            ).fetchone()
            if row is None:
                raise RuntimeError("Entstörmarker besitzt keinen offenen Schreibschutz")
            try:
                details = json.loads(str(row["details_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
            if not isinstance(details, dict):
                details = {}
            details.update({
                "clear_confirmed": True,
                "clear_confirmed_at": str(confirmed_at),
            })
            changed = db.execute(
                "UPDATE service_reset_write_intents SET updated_at=?,details_json=? "
                "WHERE intent_id=? AND status='pending'",
                (
                    str(confirmed_at),
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                    str(intent_id),
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError(
                    "Bestätigter Entstörmarker konnte nicht gespeichert werden"
                )

    def resolve_service_reset_write_intent(
        self,
        intent_id: str,
        recorded_at: str,
        resolution: str,
        details: dict,
    ) -> None:
        """Resolve only a confirmed intent; uncertain attempts remain pending."""
        with self.lock, self.database() as db:
            changed = db.execute(
                "UPDATE service_reset_write_intents SET status='resolved',updated_at=?,"
                "resolution=?,details_json=? WHERE intent_id=? AND status='pending'",
                (
                    str(recorded_at), str(resolution),
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                    str(intent_id),
                ),
            ).rowcount
        if changed != 1:
            raise RuntimeError("persistenter Entstör-Schreibschutz konnte nicht aufgelöst werden")

    def record_service_reset_outcome(
        self,
        intent_id: str,
        recorded_at: str,
        username: str,
        block: int,
        audit: dict,
        *,
        resolve: bool,
        resolution: str,
        details: dict,
    ) -> None:
        """Persist final intent state and mandatory audit in one transaction."""
        audit_json = json.dumps(audit, ensure_ascii=False)
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT details_json FROM service_reset_write_intents WHERE intent_id=?",
                (str(intent_id),),
            ).fetchone()
            merged_details = dict(details)
            if row is not None:
                try:
                    existing_details = json.loads(str(row["details_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_details = {}
                if isinstance(existing_details, dict) and existing_details.get("clear_confirmed") is True:
                    merged_details.setdefault("clear_confirmed", True)
                    merged_details.setdefault(
                        "clear_confirmed_at", existing_details.get("clear_confirmed_at")
                    )
            details_json = json.dumps(
                merged_details, ensure_ascii=False, separators=(",", ":")
            )
            if resolve:
                changed = db.execute(
                    "UPDATE service_reset_write_intents SET status='resolved',updated_at=?,"
                    "resolution=?,details_json=? WHERE intent_id=? AND status='pending'",
                    (str(recorded_at), str(resolution), details_json, str(intent_id)),
                ).rowcount
            else:
                changed = db.execute(
                    "UPDATE service_reset_write_intents SET updated_at=?,resolution=?,"
                    "details_json=? WHERE intent_id=? AND status='pending'",
                    (str(recorded_at), str(resolution), details_json, str(intent_id)),
                ).rowcount
            if changed != 1:
                raise RuntimeError(
                    "persistenter Entstör-Schreibschutz konnte nicht atomar dokumentiert werden"
                )
            db.execute(
                "INSERT INTO write_audit(recorded_at, username, block, audit_json) "
                "VALUES(?,?,?,?)",
                (str(recorded_at), str(username), int(block), audit_json),
            )

    def pending_service_reset_write_intent(self) -> dict | None:
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT intent_id,created_at,updated_at,username,service_code_raw,"
                "fault_count,fault_runtime_seconds,status,resolution,details_json "
                "FROM service_reset_write_intents WHERE status='pending' "
                "ORDER BY created_at,intent_id LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            details = json.loads(str(item.pop("details_json")))
        except (TypeError, ValueError):
            details = {}
        item["details"] = details if isinstance(details, dict) else {}
        item["service_code_raw"] = int(item["service_code_raw"])
        item["service_code"] = int(item["service_code_raw"]) + 100
        item["fault_count"] = int(item["fault_count"])
        item["fault_runtime_seconds"] = int(item["fault_runtime_seconds"])
        return item

    def write_audit_max_id(self) -> int | None:
        with self.lock, self.database() as db:
            row = db.execute("SELECT MAX(id) FROM write_audit").fetchone()
        return None if row is None else row[0]

    def audits(self, limit: int = 100) -> list[dict]:
        with self.lock, self.database() as db:
            rows = db.execute(
                "SELECT id, recorded_at, username, block, audit_json FROM write_audit "
                "ORDER BY id DESC LIMIT ?", (min(500, max(1, limit)),)
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            with suppress(Exception):
                item["audit"] = json.loads(item.pop("audit_json"))
            output.append(item)
        return output

    @staticmethod
    def _backup_archive_row(row: sqlite3.Row) -> dict:
        columns = dict(row)
        metadata_text = columns.pop("metadata_json", "{}")
        try:
            item = json.loads(metadata_text)
        except (TypeError, ValueError):
            item = {}
        if not isinstance(item, dict):
            item = {}
        item.update(columns)
        item["verified"] = bool(item.get("verified"))
        item["pack_compatible"] = bool(item.get("pack_compatible"))
        item["state"] = str(item.get("status") or "corrupt")
        item["integrity"] = "verified" if item["verified"] else "unverified"
        item["requested_targets"] = int(item.get("target_count") or 0)
        item["successful_targets"] = int(item.get("successful_count") or 0)
        item["failed_targets"] = max(
            0, item["requested_targets"] - item["successful_targets"]
        )
        item["requested_count"] = item["requested_targets"]
        item["failed_count"] = item["failed_targets"]
        item["pack_revision"] = str(item.get("pack_rev") or "")
        complete_contract_counts = {len(BACKUP_LEGACY_TARGETS), len(BACKUP_TARGETS)}
        item["ready"] = bool(
            item.get("status") == "ready"
            and item["verified"]
            and int(item.get("target_count") or 0) in complete_contract_counts
            and int(item.get("successful_count") or 0)
            == int(item.get("target_count") or 0)
        )
        return item

    @staticmethod
    def _maintenance_row(row: sqlite3.Row, *, full: bool) -> dict:
        item = dict(row)
        report = json.loads(item.pop("report_json"))
        protocol = json.loads(item.pop("protocol_json"))
        completion_text = item.pop("completion_json")
        completion = json.loads(completion_text) if completion_text else None
        item["summary"] = report_summary(report)
        item["snapshot"] = dict(report.get("snapshot") or {})
        item["technician"] = str(protocol.get("technician") or "")
        item["completion_mode"] = (completion or {}).get("mode")
        item["controller_written"] = (completion or {}).get("controller_written")
        item["maintenance_kind"] = str(item.get("maintenance_kind") or "regular")
        item["maintenance_number"] = item.get("maintenance_number")
        item["local_reason"] = item.get("local_reason") or (completion or {}).get("reason")
        item["backup_archive"] = report.get("backup_archive")
        item["fuel_check"] = maintenance_fuel_check(report, protocol, item.get("fuel_type"))
        if full:
            item.update({"report": report, "protocol": protocol, "completion": completion})
        return item

    @staticmethod
    def _insert_backup_archive_db(
        db: sqlite3.Connection,
        metadata: dict,
        maintenance_report_id: int | None,
    ) -> dict:
        payload = dict(metadata)
        cursor = db.execute(
            "INSERT INTO backup_archives("
            "created_at,created_by,source,status,verified,pack_compatible,filename,file_sha256,image_sha256,"
            "schema_name,schema_version,pack_rev,controller_serial,target_count,successful_count,"
            "size_bytes,maintenance_report_id,metadata_json"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(payload["created_at"]),
                str(payload["created_by"]),
                str(payload["source"]),
                str(payload["status"]),
                1 if payload.get("verified") else 0,
                1 if payload.get("pack_compatible") else 0,
                str(payload["filename"]),
                str(payload["file_sha256"]),
                str(payload["image_sha256"]),
                str(payload["schema"]),
                int(payload["schema_version"]),
                str(payload["pack_rev"]),
                str(payload["controller_serial"]),
                int(payload["target_count"]),
                int(payload["successful_count"]),
                int(payload["size_bytes"]),
                int(maintenance_report_id) if maintenance_report_id is not None else None,
                "{}",
            ),
        )
        payload.update({
            "id": int(cursor.lastrowid),
            "maintenance_report_id": (
                int(maintenance_report_id) if maintenance_report_id is not None else None
            ),
        })
        db.execute(
            "UPDATE backup_archives SET metadata_json=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), payload["id"]),
        )
        return payload

    def create_maintenance_report(self, username: str, fuel_type: str, report: dict, protocol: dict) -> dict:
        now = _now()
        with self.lock, self.database() as db:
            cursor = db.execute(
                "INSERT INTO maintenance_reports(created_at,updated_at,created_by,status,fuel_type,report_json,protocol_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (now, now, username, "draft", fuel_type, json.dumps(report, ensure_ascii=False), json.dumps(protocol, ensure_ascii=False)),
            )
            report_id = int(cursor.lastrowid)
        return self.maintenance_report(report_id)

    def create_maintenance_report_with_archive(
        self,
        username: str,
        fuel_type: str,
        report: dict,
        protocol: dict,
        archive: dict,
    ) -> dict:
        """Atomically publish the report row and its already durable archive."""
        now = _now()
        report_payload = json.loads(json.dumps(report, ensure_ascii=False))
        with self.lock, self.database() as db:
            cursor = db.execute(
                "INSERT INTO maintenance_reports(created_at,updated_at,created_by,status,fuel_type,report_json,protocol_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    now,
                    now,
                    username,
                    "draft",
                    fuel_type,
                    json.dumps(report_payload, ensure_ascii=False),
                    json.dumps(protocol, ensure_ascii=False),
                ),
            )
            report_id = int(cursor.lastrowid)
            archive_payload = self._insert_backup_archive_db(db, archive, report_id)
            report_payload["backup_archive"] = archive_payload
            db.execute(
                "UPDATE maintenance_reports SET report_json=? WHERE id=?",
                (json.dumps(report_payload, ensure_ascii=False), report_id),
            )
        return self.maintenance_report(report_id)

    def create_orphan_backup_archive(self, archive: dict) -> dict:
        """Index a verified immutable file left before its DB transaction."""
        with self.lock, self.database() as db:
            payload = self._insert_backup_archive_db(db, archive, None)
        return self.backup_archive(payload["id"])

    def backup_archives(self, limit: int = 200) -> list[dict]:
        with self.lock, self.database() as db:
            rows = db.execute(
                "SELECT * FROM backup_archives ORDER BY id DESC LIMIT ?",
                (min(1000, max(1, int(limit))),),
            ).fetchall()
        return [self._backup_archive_row(row) for row in rows]

    def backup_archive(self, archive_id: int) -> dict:
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT * FROM backup_archives WHERE id=?", (int(archive_id),)
            ).fetchone()
        if row is None:
            raise KeyError(f"Backup-Archiv {archive_id} nicht gefunden")
        return self._backup_archive_row(row)

    def backup_archive_for_report(self, report_id: int) -> dict:
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT * FROM backup_archives WHERE maintenance_report_id=?",
                (int(report_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"Wartungsbericht {report_id} besitzt kein Backup-Archiv")
        return self._backup_archive_row(row)

    def backup_archive_filenames(self) -> set[str]:
        with self.lock, self.database() as db:
            rows = db.execute("SELECT filename FROM backup_archives").fetchall()
        return {str(row["filename"]) for row in rows}

    def set_backup_archive_status(
        self,
        archive_id: int,
        status: str,
        *,
        verified: bool,
        pack_compatible: bool | None = None,
    ) -> None:
        if status not in {"ready", "missing", "corrupt"}:
            raise ValueError("ungültiger Backup-Archivstatus")
        with self.lock, self.database() as db:
            if pack_compatible is None:
                cursor = db.execute(
                    "UPDATE backup_archives SET status=?,verified=? WHERE id=?",
                    (status, 1 if verified else 0, int(archive_id)),
                )
            else:
                cursor = db.execute(
                    "UPDATE backup_archives SET status=?,verified=?,pack_compatible=? WHERE id=?",
                    (
                        status,
                        1 if verified else 0,
                        1 if pack_compatible else 0,
                        int(archive_id),
                    ),
                )
            if cursor.rowcount != 1:
                raise KeyError(f"Backup-Archiv {archive_id} nicht gefunden")

    def maintenance_reports(self, limit: int = 50) -> list[dict]:
        with self.lock, self.database() as db:
            rows = db.execute(
                "SELECT * FROM maintenance_reports ORDER BY id DESC LIMIT ?",
                (min(200, max(1, int(limit))),),
            ).fetchall()
        return [self._maintenance_row(row, full=False) for row in rows]

    def maintenance_report(self, report_id: int) -> dict:
        with self.lock, self.database() as db:
            row = db.execute("SELECT * FROM maintenance_reports WHERE id=?", (int(report_id),)).fetchone()
        if row is None:
            raise KeyError(f"Wartungsbericht {report_id} nicht gefunden")
        return self._maintenance_row(row, full=True)

    def delete_maintenance_report(self, report_id: int) -> dict:
        report_id = int(report_id)
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT id, status FROM maintenance_reports WHERE id=?", (report_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Wartungsbericht {report_id} nicht gefunden")
            if str(row["status"]) not in {"draft", "completed"}:
                raise ValueError(
                    "Wartungsbericht mit unklarem Abschlusszustand darf nicht gelöscht werden"
                )
            db.execute("DELETE FROM maintenance_reports WHERE id=?", (report_id,))
        return {"id": report_id, "status": str(row["status"])}

    def update_maintenance_protocol(self, report_id: int, protocol: dict) -> dict:
        now = _now()
        with self.lock, self.database() as db:
            cursor = db.execute(
                "UPDATE maintenance_reports SET updated_at=?,fuel_type=?,protocol_json=? WHERE id=? AND status='draft'",
                (now, protocol["fuel_type"], json.dumps(protocol, ensure_ascii=False), int(report_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("nur ein offener Wartungsbericht kann bearbeitet werden")
        return self.maintenance_report(report_id)

    @staticmethod
    def _snapshot_maintenance_base(report: dict) -> int | None:
        value = report_summary(report).get("maintenance_count")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not number.is_integer() or number < 0:
            return None
        return int(number)

    def complete_local_intermediate_maintenance(
        self, report_id: int, protocol: dict, username: str, reason: str
    ) -> dict:
        """Atomically allocate and persist a local-only intermediate number."""
        now = _now()
        report_id = int(report_id)
        reason = str(reason).strip()
        if not reason:
            raise ValueError("Grund für die Zwischenwartung ist erforderlich")
        if len(reason) > 500:
            raise ValueError("Grund für die Zwischenwartung darf höchstens 500 Zeichen haben")
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT status,report_json FROM maintenance_reports WHERE id=?",
                (report_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Wartungsbericht {report_id} nicht gefunden")
            if str(row["status"]) != "draft":
                raise ValueError("nur ein offener Wartungsentwurf kann als Zwischenwartung gespeichert werden")
            report = json.loads(row["report_json"])
            base = self._snapshot_maintenance_base(report)
            if base is None:
                # Stable fallback: latest persisted local base, then highest valid
                # regulator counter captured in any report, finally zero.
                bases = []
                for candidate in db.execute(
                    "SELECT maintenance_number,report_json FROM maintenance_reports ORDER BY id"
                ).fetchall():
                    number = str(candidate["maintenance_number"] or "")
                    if "." in number and number.split(".", 1)[0].isdigit():
                        bases.append(int(number.split(".", 1)[0]))
                    captured = self._snapshot_maintenance_base(json.loads(candidate["report_json"]))
                    if captured is not None:
                        bases.append(captured)
                base = max(bases, default=0)
            prefix = f"{base}."
            suffixes = []
            for candidate in db.execute(
                "SELECT maintenance_number FROM maintenance_reports "
                "WHERE maintenance_kind='intermediate' AND maintenance_number LIKE ?",
                (prefix + "%",),
            ).fetchall():
                text = str(candidate["maintenance_number"] or "")
                tail = text[len(prefix):]
                if tail.isdigit() and int(tail) > 0:
                    suffixes.append(int(tail))
            maintenance_number = f"{base}.{max(suffixes, default=0) + 1}"
            completion = {
                "mode": "local_intermediate",
                "completed_at": now,
                "username": str(username),
                "reason": reason,
                "maintenance_number": maintenance_number,
                "controller_written": False,
                "hardware_unchanged": True,
                "confirmation_bit_set": False,
                "auth_used": False,
                "serial_session_opened": False,
                "audits": [],
                "note": "Zwischenwartung ausschließlich lokal archiviert; MSR2 unverändert.",
            }
            cursor = db.execute(
                "UPDATE maintenance_reports SET updated_at=?,fuel_type=?,protocol_json=?,"
                "status='completed',completed_at=?,completion_json=?,maintenance_kind='intermediate',"
                "maintenance_number=?,local_reason=? WHERE id=? AND status='draft'",
                (now, protocol["fuel_type"], json.dumps(protocol, ensure_ascii=False), now,
                 json.dumps(completion, ensure_ascii=False), maintenance_number, reason, report_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Wartungsentwurf wurde zwischenzeitlich verändert")
        return self.maintenance_report(report_id)

    def complete_maintenance_report(self, report_id: int, completion: dict) -> dict:
        now = _now()
        with self.lock, self.database() as db:
            cursor = db.execute(
                "UPDATE maintenance_reports SET updated_at=?,status='completed',completed_at=?,completion_json=? "
                "WHERE id=? AND status='draft'",
                (now, now, json.dumps(completion, ensure_ascii=False), int(report_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("Wartungsbericht ist nicht mehr offen")
        return self.maintenance_report(report_id)

    def complete_demo_maintenance_report(
        self, report_id: int, protocol: dict, completion: dict
    ) -> dict:
        now = _now()
        with self.lock, self.database() as db:
            cursor = db.execute(
                "UPDATE maintenance_reports SET updated_at=?,fuel_type=?,protocol_json=?,"
                "status='completed',completed_at=?,completion_json=? "
                "WHERE id=? AND status='draft'",
                (
                    now,
                    protocol["fuel_type"],
                    json.dumps(protocol, ensure_ascii=False),
                    now,
                    json.dumps(completion, ensure_ascii=False),
                    int(report_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Wartungsbericht ist nicht mehr offen")
        return self.maintenance_report(report_id)

    def claim_live_maintenance_completion(
        self,
        report_id: int,
        protocol: dict,
        archive: dict,
    ) -> None:
        """Atomically reserve one live completion with its current 42/42 image."""
        now = _now()
        with self.lock, self.database() as db:
            row = db.execute(
                "SELECT r.status AS report_status,a.* FROM maintenance_reports r "
                "LEFT JOIN backup_archives a ON a.maintenance_report_id=r.id "
                "WHERE r.id=?",
                (int(report_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"Wartungsbericht {report_id} nicht gefunden")
            if str(row["report_status"]) != "draft":
                raise ValueError("Wartungsbericht ist nicht mehr offen")
            if row["id"] is None:
                raise ValueError("Live-Abschluss benötigt ein verknüpftes Backup-Archiv")
            immutable_keys = (
                "id",
                "created_at",
                "created_by",
                "source",
                "filename",
                "file_sha256",
                "image_sha256",
                "schema_name",
                "schema_version",
                "pack_rev",
                "controller_serial",
                "target_count",
                "successful_count",
                "size_bytes",
            )
            for key in immutable_keys:
                expected = archive.get(key)
                actual = row[key]
                if key in {
                    "id",
                    "schema_version",
                    "target_count",
                    "successful_count",
                    "size_bytes",
                }:
                    matches = int(expected or -1) == int(actual or -1)
                else:
                    matches = hmac.compare_digest(
                        str(expected or ""), str(actual or "")
                    )
                if not matches:
                    raise ValueError(
                        "Backup-Archiv wurde zwischen Prüfung und Reservierung verändert"
                    )
            if not (
                str(row["status"]) == "ready"
                and bool(row["verified"])
                and bool(row["pack_compatible"])
                and int(row["target_count"]) == len(BACKUP_TARGETS)
                and int(row["successful_count"]) == len(BACKUP_TARGETS)
            ):
                raise ValueError("Live-Abschluss benötigt ein bereites, verifiziertes 42/42-Backup")
            cursor = db.execute(
                "UPDATE maintenance_reports SET updated_at=?,fuel_type=?,protocol_json=?,status='completing' "
                "WHERE id=? AND status='draft'",
                (
                    now,
                    protocol["fuel_type"],
                    json.dumps(protocol, ensure_ascii=False),
                    int(report_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Wartungsbericht wird bereits abgeschlossen")

    def release_live_maintenance_completion(self, report_id: int) -> None:
        with self.lock, self.database() as db:
            db.execute(
                "UPDATE maintenance_reports SET updated_at=?,status='draft' "
                "WHERE id=? AND status='completing'",
                (_now(), int(report_id)),
            )

    def fail_live_maintenance_completion(self, report_id: int, completion: dict) -> None:
        now = _now()
        with self.lock, self.database() as db:
            cursor = db.execute(
                "UPDATE maintenance_reports SET updated_at=?,status='uncertain',"
                "completed_at=?,completion_json=? WHERE id=? AND status='completing'",
                (
                    now,
                    now,
                    json.dumps(completion, ensure_ascii=False),
                    int(report_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Wartungsabschluss konnte nicht als unklar gesichert werden")

    def finish_live_maintenance_completion(self, report_id: int, completion: dict) -> dict:
        now = _now()
        with self.lock, self.database() as db:
            cursor = db.execute(
                "UPDATE maintenance_reports SET updated_at=?,status='completed',"
                "completed_at=?,completion_json=? WHERE id=? AND status='completing'",
                (
                    now,
                    now,
                    json.dumps(completion, ensure_ascii=False),
                    int(report_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Wartungsbericht ist nicht reserviert")
        return self.maintenance_report(report_id)

    def purge(self, days: int = 90) -> None:
        cutoff = datetime.fromtimestamp(time.time() - max(1, days) * 86400, timezone.utc).isoformat()
        with self.lock, self.database() as db:
            db.execute("DELETE FROM measurements WHERE recorded_at < ?", (cutoff,))
            db.execute("DELETE FROM history_samples WHERE recorded_at < ?", (cutoff,))
            db.execute("DELETE FROM motor_status_points WHERE recorded_at < ?", (cutoff,))
            # Keep the newest display change: it still describes the current state.
            db.execute(
                "DELETE FROM display_status_events WHERE recorded_at < ? "
                "AND id < (SELECT MAX(id) FROM display_status_events)",
                (cutoff,),
            )

    def record_display_status_change(self, recorded_at: str, status: dict) -> bool:
        """Insert one row only when the displayed text of line 1 or 2 changes.

        Unavailable lines are never stored, so a read gap does not create a
        pair of artificial transitions.
        """
        if not isinstance(status, dict) or status.get("available") is not True:
            return False
        line1 = status.get("line1")
        line2 = status.get("line2")
        if not isinstance(line1, str) or not isinstance(line2, str):
            return False
        with self.lock, self.database() as db:
            last = db.execute(
                "SELECT line1,line2 FROM display_status_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last is not None and (str(last["line1"]), str(last["line2"])) == (line1, line2):
                return False
            db.execute(
                "INSERT INTO display_status_events"
                "(recorded_at,line1,line2,line1_index,line2_index,service_code) "
                "VALUES(?,?,?,?,?,?)",
                (
                    str(recorded_at),
                    line1,
                    line2,
                    status.get("line1_index"),
                    status.get("line2_index"),
                    status.get("service_code"),
                ),
            )
        return True

    def display_status_events(self, limit: int = DISPLAY_STATUS_EVENT_LIMIT) -> list[dict]:
        """Newest display-line changes first."""
        limit = min(DISPLAY_STATUS_EVENT_MAX_LIMIT, max(1, int(limit)))
        with self.database() as db:
            rows = db.execute(
                "SELECT recorded_at,line1,line2,line1_index,line2_index,service_code "
                "FROM display_status_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "recorded_at": str(row["recorded_at"]),
                "line1": str(row["line1"]),
                "line2": str(row["line2"]),
                "line1_index": row["line1_index"],
                "line2_index": row["line2_index"],
                "service_code": row["service_code"],
            }
            for row in rows
        ]

    def purge_invalid_dashboard(self) -> int:
        """Remove only known-invalid historical dashboard samples."""
        placeholders = ",".join("?" for _ in DASHBOARD_TEMPERATURE_KEYS)
        sensor_values = list(INVALID_SENSOR_VALUES)
        with self.lock, self.database() as db:
            cursor = db.execute(
                f"DELETE FROM measurements WHERE block=24 AND ("
                f"(field_key IN ({placeholders}) AND CAST(value AS REAL) IN ({','.join('?' for _ in sensor_values)})) OR "
                "(field_key='Hka_Mw1.Temp.DachsAustritt' AND CAST(raw AS REAL) IN (?,?,?,?,?)) OR "
                "(field_key='Hka_Mw1.usDrehzahl' AND (CAST(value AS REAL)<0 OR CAST(value AS REAL)>3000)) OR "
                "(field_key='Hka_Mw1.sWirkleistung' AND (CAST(value AS REAL)<-6 OR CAST(value AS REAL)>6)) OR "
                "(field_key='Hka_Mw1.Temp.sAbgasMotor' AND (CAST(value AS REAL)<0 OR CAST(value AS REAL)>600)) OR "
                "(field_key='Hka_Mw1.Temp.sAbgasHKA' AND (CAST(value AS REAL)<0 OR CAST(value AS REAL)>200))"
                ")",
                [*DASHBOARD_TEMPERATURE_KEYS, *sensor_values, *sensor_values],
            )
            return int(cursor.rowcount if cursor.rowcount is not None else 0)

class DachsWebApp:
    def __init__(self, *, port: str = "/dev/ttyUSB0", baud: int = 19200, timeout: float = 0.9,
                 pack_rev: str = "50", data_dir: str | Path = "./dachs-web-data",
                 interval: float = 0.75, serial_socket: str | Path | None = None,
                 maintenance_live_writes: bool = False,
                 service_reset_temperature_policy_approved: bool = False,
                 history_interval: float = DEFAULT_HISTORY_SAMPLE_INTERVAL):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.users_path, credentials = init_users(self.data_dir)
        with suppress(OSError):
            self.users_path.chmod(0o600)
        self.initial_credentials = credentials
        self.pack = PackRepository(pack_rev=pack_rev)
        self.service = DachsService(
            port, baud, timeout, self.pack, serial_socket=serial_socket
        )
        self.store = DachsStore(self.data_dir / "dachs-web.db")
        self.diagnosis_250h_cache = Diagnosis250hCache(
            self.data_dir / "diagnosis_250h_cache.json",
            self.store.diagnosis_250h,
        )
        self.backup_archive_dir = self.data_dir / "backup-archive"
        self.backup_archive_errors: list[str] = []
        archive_fd = self._open_backup_archive_directory()
        os.close(archive_fd)
        self._reconcile_backup_archive()
        self.interval = max(0.3, float(interval))
        self.history_interval = max(0.0, float(history_interval))
        self.maintenance_settings_path = self.data_dir / "maintenance_settings.json"
        self.dashboard_settings_path = self.data_dir / "dashboard_settings.json"
        self.soot_filter_settings_path = self.data_dir / "soot_filter_settings.json"
        self.api_settings_path = self.data_dir / "api_settings.json"
        self.local_access_settings_path = self.data_dir / "local_access_settings.json"
        self.email_notification_settings_path = (
            self.data_dir / "email_notification_settings.json"
        )
        self.email_notification_secret_dir = self.data_dir / "secrets"
        # The environment value is only the initial default. Once an admin
        # changes the test mode in the web UI, the local setting survives
        # service restarts and later package updates.
        self.maintenance_live_writes_enabled = self._load_maintenance_live_writes(
            bool(maintenance_live_writes)
        )
        # Manual reset can cause an automatic restart.  The temperature
        # limits are therefore not enabled merely because they are below
        # firmware trip points: an operator must explicitly attest that the
        # installation-specific restart policy has been reviewed.
        self.service_reset_temperature_policy_approved = bool(
            service_reset_temperature_policy_approved
        )
        self.dashboard_cards = self._load_dashboard_cards()
        self.soot_filter_config = self._load_soot_filter_settings()
        self.api_config = self._load_api_settings()
        self.local_access_config = self._load_local_access_settings()
        self._email_notification_flow_lock = threading.RLock()
        self.email_notification_config = self._load_email_notification_settings()
        persisted_email_pause = self.store.notification_dispatch_pause()
        if (
            persisted_email_pause["paused_revision"] is not None
            and persisted_email_pause["paused_revision"]
            != int(self.email_notification_config.get("revision") or 0)
        ):
            self.store.clear_notification_dispatch_pause(_now())
            persisted_email_pause = {
                "paused_revision": None, "paused_reason": None, "updated_at": None
            }
        self.email_notification_runtime = {
            "last_observation_at": None,
            "last_observation_error": None,
            "last_send_at": None,
            "last_send_error": None,
            "paused_revision": persisted_email_pause["paused_revision"],
            "paused_reason": persisted_email_pause["paused_reason"],
            "dispatcher_alive": False,
        }
        self._send_smtp_email = send_smtp_email
        self.local_access_runtime = {
            "active": False,
            "last_error": None,
            "restart_required": bool(self.local_access_config["enabled"]),
            "url": None,
        }
        self.local_access_manager: LocalAccessManager | None = None
        # Network-only checker: it never acquires the serial lock and can only
        # stage a verified request for the separately installed root helper.
        self.update_manager = UpdateManager(self.data_dir, __version__)
        self.slow_monitor_interval = DEFAULT_SLOW_MONITOR_INTERVAL
        self._last_slow_poll = 0.0
        self._last_history_sample = float("-inf")
        # Do not run the first full-table retention scan during startup. The
        # monitor loop performs the same maintenance hourly after the web
        # listener is already responsive.
        self._last_retention_purge = time.monotonic()
        self._last_notification_retention_purge = time.monotonic() - 3600.0
        self.serial_state_path = self.data_dir / "serial_state.json"
        self.dashboard_cleanup_marker = self.data_dir / HISTORY_CLEANUP_MARKER
        self.serial_enabled = self._load_serial_enabled()
        self.monitor_enabled = self.serial_enabled
        self.stop_event = threading.Event()
        # Tests replace this with a no-wait function.  Production uses the
        # stop event so shutdown can interrupt only the bounded readback wait,
        # never trigger a second write.
        self._service_reset_wait = self.stop_event.wait
        # Serialize preview issuance and execution as one controller-wide
        # capability flow.  This prevents an older preview from overwriting a
        # newer token after either request waited for the shared serial lock.
        self._service_reset_flow_lock = threading.Lock()
        self._service_reset_token_lock = threading.Lock()
        self._service_reset_token: dict | None = None
        self._service_reset_last_attempt: dict | None = None
        self._dachs_control_flow_lock = threading.Lock()
        self._dachs_control_token_lock = threading.Lock()
        self._dachs_control_token: dict | None = None
        self._dachs_control_last_attempt: dict | None = None
        self._sc123_limit_flow_lock = threading.Lock()
        self._sc123_limit_token_lock = threading.Lock()
        self._sc123_limit_token: dict | None = None
        self._sc123_limit_last_attempt: dict | None = None
        self.serial_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.sessions: dict[str, tuple[str, str, float, int]] = {}
        self.live_values: dict[tuple[int, str], dict] = {}
        self.runtime_24h = {
            "schema": "open-dachs-runtime-24h/v1",
            "available": False,
            "counter_seconds": None,
            "remaining_run_seconds": None,
            "shutdown_threshold_seconds": RUNTIME_24H_SHUTDOWN_SECONDS,
            "threshold_reached": False,
            "night_shutdown_gate_seconds": RUNTIME_NIGHT_SHUTDOWN_GATE_SECONDS,
            "remaining_to_night_gate_seconds": None,
            "night_shutdown_gate_reached": False,
            "counter_address": f"0x{RUNTIME_24H_COUNTER_ADDRESS:04x}",
            "recorded_at": None,
            "semantics": "active_runtime_seconds",
            "wall_clock_countdown": False,
            "error": None,
        }
        self.ram_telemetry = {
            field["key"]: ram_telemetry_unavailable(field)
            for field in RAM_TELEMETRY_FIELDS
        }
        self.soot_regeneration = soot_regeneration_unavailable()
        self.display_status = display_status_unavailable(error="noch nicht berechnet")
        self._display_status_mono: float | None = None
        # Slow block payloads (50/104) and RAM-only bytes with the monotonic
        # time of their last read attempt; a failed attempt stores None.
        self._display_slow_payloads: dict[int, tuple[bytes, float]] = {}
        self._display_ram: dict[int, dict] = {}
        self._display_last_recorded_lines: tuple[str, str] | None = None
        self.monitor_state = {
            "enabled": self.serial_enabled,
            "serial_enabled": self.serial_enabled,
            "connection_state": "verbunden" if self.serial_enabled else "getrennt",
            "running": self.serial_enabled,
            "last_cycle": None,
            "last_error": None,
            "cycles": 0,
            "ok_blocks": 0,
            "failed_blocks": 0,
            "polled_blocks": 0,
            "interval_seconds": self.interval,
            "slow_interval_seconds": self.slow_monitor_interval,
            "last_slow_cycle": None,
        }
        # Temporary read-only Phase-6 verification logger; off unless
        # <data_dir>/phase6_verify.json enables it with an end date.
        self.phase6 = Phase6Verifier(
            self.data_dir,
            version=__version__,
            serial_socket=str(serial_socket or ""),
            write_audit_count=self.store.write_audit_max_id,
        )
        self._utcnow = lambda: datetime.now(timezone.utc)
        self._ntp_status = self._timedatectl_ntp_status
        self.automation_lock = threading.Lock()
        self.clock_sync_settings_path = self.data_dir / "controller_clock_sync.json"
        self.clock_sync_state_path = self.data_dir / "controller_clock_sync_state.json"
        self.runtime_auto_settings_path = self.data_dir / "runtime_auto_reset.json"
        self.runtime_auto_state_path = self.data_dir / "runtime_auto_reset_state.json"
        self.clock_sync_config = self._load_clock_sync_settings()
        self.clock_sync_state = self._load_automation_json(self.clock_sync_state_path) or {}
        self.runtime_auto_config = self._load_runtime_auto_settings()
        self.runtime_auto_state = self._load_automation_json(self.runtime_auto_state_path) or {}
        self.runtime_auto_live = {
            "phase": "watching" if self.runtime_auto_config["enabled"] else "disabled",
            "reason": None,
            "checked_at": None,
        }
        self._runtime_auto_backoff_until = 0.0
        self._real_runtime_history_cache: tuple | None = None
        self.automation_thread = threading.Thread(
            target=self._automation_loop, name="dachs-web-automation", daemon=True
        )
        self.email_notification_thread = threading.Thread(
            target=self._email_notification_loop,
            name="dachs-web-email-notifications",
            daemon=True,
        )
        self._previous_cycle: dict = {}
        self.monitor_thread = threading.Thread(target=self._monitor_loop, name="dachs-web-monitor", daemon=True)
        self.maintenance_thread = threading.Thread(
            target=self._startup_maintenance,
            name="dachs-web-maintenance",
            daemon=True,
        )

    def _open_backup_archive_directory(self) -> int:
        """Open/create the private archive without following a symlink."""
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(self.data_dir, directory_flags)
        created = False
        try:
            try:
                os.mkdir("backup-archive", 0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            archive_fd = os.open("backup-archive", directory_flags, dir_fd=parent_fd)
            archive_stat = os.fstat(archive_fd)
            if not stat.S_ISDIR(archive_stat.st_mode):
                os.close(archive_fd)
                raise OSError("backup-archive ist kein Verzeichnis")
            if stat.S_IMODE(archive_stat.st_mode) != 0o700:
                os.fchmod(archive_fd, 0o700)
            os.fsync(archive_fd)
            if created:
                os.fsync(parent_fd)
            return archive_fd
        finally:
            os.close(parent_fd)

    @staticmethod
    def _validate_backup_archive_filename(filename: object) -> str:
        name = str(filename or "")
        if not BACKUP_ARCHIVE_FILENAME.fullmatch(name) or Path(name).name != name:
            raise ValueError("ungültiger Backup-Archivdateiname")
        return name

    def _read_backup_archive_file(self, filename: object) -> bytes:
        """Read one held regular-file descriptor; symlinks fail closed."""
        name = self._validate_backup_archive_filename(filename)
        directory_fd = self._open_backup_archive_directory()
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("Backup-Archiv ist keine reguläre Datei")
                if stat.S_IMODE(info.st_mode) != 0o600:
                    raise PermissionError("Backup-Archiv besitzt nicht Modus 0600")
                if info.st_size <= 0 or info.st_size > BACKUP_ARCHIVE_MAX_BYTES:
                    raise ValueError("Backup-Archiv besitzt eine ungültige Dateigröße")
                chunks = []
                remaining = int(info.st_size)
                while remaining:
                    chunk = os.read(file_fd, min(65536, remaining))
                    if not chunk:
                        raise OSError("Backup-Archiv wurde unvollständig gelesen")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(file_fd, 1):
                    raise ValueError("Backup-Archiv wuchs während des Lesens")
                return b"".join(chunks)
            finally:
                os.close(file_fd)
        finally:
            os.close(directory_fd)

    def _strict_backup_archive_inspection(
        self, image: dict, *, require_current_pack: bool = False
    ) -> dict:
        context = image.get("maintenance_archive")
        archive_version = context.get("version") if isinstance(context, dict) else None
        if (
            not isinstance(context, dict)
            or type(archive_version) is not int
            or archive_version not in (1, 2)
        ):
            raise ValueError("Datei ist kein Wartungs-Backup-Archiv")
        created_by = context.get("created_by")
        if (
            context.get("source") != "maintenance"
            or not isinstance(created_by, str)
            or not created_by.strip()
            or len(created_by.strip()) > 128
        ):
            raise ValueError("Wartungs-Backup besitzt keinen gebundenen Ursprung")
        inspection = self.service.inspect_backup(image)
        target_contract = BACKUP_LEGACY_TARGETS if archive_version == 1 else BACKUP_TARGETS
        restore_contract = set(BACKUP_RESTORE_TARGETS)
        expected_targets = [
            {"cpu": cpu, "block": block} for cpu, block in target_contract
        ]
        if inspection.get("requested_targets") != expected_targets:
            raise ValueError(
                f"Wartungs-Backup enthält nicht den exakten {len(target_contract)}-Ziele-Satz"
            )
        if (
            int(inspection.get("successful_blocks") or 0) != len(target_contract)
            or int(inspection.get("failed_blocks") or 0) != 0
            or inspection.get("digest_present") is not True
            or inspection.get("digest_verified") is not True
        ):
            raise ValueError("Wartungs-Backup ist nicht vollständig verifiziert")
        if require_current_pack and inspection.get("pack_compatible") is not True:
            raise ValueError("Wartungs-Backup ist nicht mit dem aktuellen Pack kompatibel")
        records = list(inspection.get("records") or [])
        if len(records) != len(target_contract):
            raise ValueError(
                f"Wartungs-Backup enthält nicht exakt {len(target_contract)} Datensätze"
            )
        for target, record in zip(target_contract, records, strict=True):
            if (int(record.get("cpu", -1)), int(record.get("block", -1))) != target:
                raise ValueError("Wartungs-Backup-Zielreihenfolge ist nicht kanonisch")
            expected_restorable = target in restore_contract
            if bool(record.get("restorable")) is not expected_restorable:
                raise ValueError(
                    "Wartungs-Backup besitzt einen falschen Restore-Status für "
                    f"CPU {target[0]}, Block {target[1]}"
                )
            if int(record.get("payload_len") or -1) != BACKUP_PAYLOAD_LENGTHS[target]:
                raise ValueError(
                    f"Wartungs-Backup CPU {target[0]}, Block {target[1]} hat eine falsche Länge"
                )
        controller = dict(inspection.get("controller") or {})
        if controller.get("available") is not True or not str(
            controller.get("serial_number") or ""
        ).strip():
            raise ValueError("Wartungs-Backup besitzt keine Regleridentität")
        if inspection.get("pack_compatible") is True:
            records_by_target = {
                (int(record["cpu"]), int(record["block"])): record
                for record in records
            }
            try:
                identity = auth_inputs_from_payloads(
                    self.pack,
                    bytes.fromhex(str(records_by_target[(0, 20)]["payload_hex"])),
                    bytes.fromhex(str(records_by_target[(0, 22)]["payload_hex"])),
                )
            except Exception as exc:
                raise ValueError(
                    "Wartungs-Backup-Regleridentität kann nicht aus B20/B22 abgeleitet werden"
                ) from exc
            declared_serial = str(controller.get("serial_number") or "").strip()
            if not hmac.compare_digest(identity.serial_number, declared_serial):
                raise ValueError(
                    "Wartungs-Backup-Regleridentität widerspricht den B20/B22-Payloads"
                )
            declared_hours = controller.get("operating_hours")
            if (
                type(declared_hours) is not int
                or declared_hours != identity.operating_hours
            ):
                raise ValueError(
                    "Wartungs-Backup-Betriebsstunden widersprechen dem B22-Payload"
                )
        return inspection

    def _verified_backup_archive_bytes(
        self, raw: bytes, expected: dict | None = None
    ) -> tuple[dict, dict, str]:
        try:
            image = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Backup-Archiv ist kein gültiges UTF-8-JSON") from exc
        if not isinstance(image, dict):
            raise ValueError("Backup-Archiv muss ein JSON-Objekt sein")
        inspection = self._strict_backup_archive_inspection(image)
        file_sha256 = hashlib.sha256(raw).hexdigest()
        if expected is not None:
            comparisons = {
                "file_sha256": file_sha256,
                "image_sha256": str(inspection.get("image_sha256") or ""),
                "size_bytes": len(raw),
            }
            for key, actual in comparisons.items():
                declared = expected.get(key)
                if key == "size_bytes":
                    matches = int(declared or -1) == actual
                else:
                    matches = hmac.compare_digest(str(declared or ""), str(actual))
                if not matches:
                    raise ValueError(f"Backup-Archiv {key} stimmt nicht mit dem Index überein")
            controller = dict(inspection.get("controller") or {})
            pack = dict(inspection.get("pack") or {})
            context = dict(image.get("maintenance_archive") or {})
            immutable = {
                "schema_name": str(inspection.get("schema") or ""),
                "schema_version": int(inspection.get("schema_version") or -1),
                "pack_rev": str(pack.get("revision") or ""),
                "controller_serial": str(controller.get("serial_number") or ""),
                "target_count": int(inspection.get("requested_blocks") or 0),
                "successful_count": int(inspection.get("successful_blocks") or 0),
                "created_by": str(context.get("created_by") or ""),
                "source": str(context.get("source") or ""),
                "created_at": str(inspection.get("created_utc") or ""),
            }
            for key, actual in immutable.items():
                declared = expected.get(key)
                if key in {"schema_version", "target_count", "successful_count"}:
                    matches = int(declared or -1) == actual
                else:
                    matches = hmac.compare_digest(str(declared or ""), str(actual))
                if not matches:
                    raise ValueError(
                        f"Backup-Archivmetadatum {key} stimmt nicht mit der Datei überein"
                    )
        return image, inspection, file_sha256

    def _archive_metadata(
        self,
        filename: str,
        raw: bytes,
        image: dict,
        inspection: dict,
        file_sha256: str,
    ) -> dict:
        controller = dict(inspection.get("controller") or {})
        context = dict(image.get("maintenance_archive") or {})
        requested_count = int(inspection.get("requested_blocks") or 0)
        successful_count = int(inspection.get("successful_blocks") or 0)
        restorable_count = int(inspection.get("restorable_blocks") or 0)
        return {
            "created_at": str(inspection.get("created_utc") or _now()),
            "created_by": str(context.get("created_by") or "recovery"),
            "source": "maintenance",
            "status": "ready",
            "state": "ready",
            "verified": True,
            "integrity": "verified",
            "pack_compatible": bool(inspection.get("pack_compatible")),
            "filename": filename,
            "file_sha256": file_sha256,
            "image_sha256": str(inspection["image_sha256"]),
            "schema": str(inspection["schema"]),
            "schema_version": int(inspection["schema_version"]),
            "pack_rev": str((inspection.get("pack") or {}).get("revision") or ""),
            "controller_serial": str(controller["serial_number"]),
            "archive_contract_version": int(context.get("version") or 0),
            "target_count": requested_count,
            "successful_count": successful_count,
            "restorable_count": restorable_count,
            "requested_targets": requested_count,
            "successful_targets": successful_count,
            "failed_targets": max(0, requested_count - successful_count),
            "requested_count": requested_count,
            "failed_count": max(0, requested_count - successful_count),
            "pack_revision": str((inspection.get("pack") or {}).get("revision") or ""),
            "size_bytes": len(raw),
        }

    def _persist_maintenance_backup_archive(self, image: dict) -> dict:
        inspection = self._strict_backup_archive_inspection(
            image, require_current_pack=True
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        filename = f"maintenance-{stamp}-{secrets.token_hex(8)}.json"
        raw = write_json_exclusive(self.backup_archive_dir / filename, image)
        # Reopen through O_NOFOLLOW and verify the exact bytes that future
        # downloads/completions will use before any database row is created.
        stored = self._read_backup_archive_file(filename)
        if not hmac.compare_digest(hashlib.sha256(raw).digest(), hashlib.sha256(stored).digest()):
            raise OSError("Backup-Archiv änderte sich während der Veröffentlichung")
        stored_image, stored_inspection, file_sha256 = self._verified_backup_archive_bytes(stored)
        return self._archive_metadata(
            filename, stored, stored_image, stored_inspection, file_sha256
        )

    def _load_verified_backup_archive(
        self, archive_id: int
    ) -> tuple[dict, bytes, dict, dict]:
        archive = self.store.backup_archive(archive_id)
        try:
            raw = self._read_backup_archive_file(archive["filename"])
        except FileNotFoundError:
            self.store.set_backup_archive_status(
                archive_id, "missing", verified=False, pack_compatible=False
            )
            raise ValueError("Backup-Archivdatei fehlt")
        except Exception as exc:
            self.store.set_backup_archive_status(
                archive_id, "corrupt", verified=False, pack_compatible=False
            )
            raise ValueError(f"Backup-Archiv ist nicht sicher lesbar: {exc}") from exc
        try:
            image, inspection, _file_sha256 = self._verified_backup_archive_bytes(
                raw, archive
            )
        except Exception as exc:
            self.store.set_backup_archive_status(
                archive_id, "corrupt", verified=False, pack_compatible=False
            )
            raise ValueError(f"Backup-Archivprüfung fehlgeschlagen: {exc}") from exc
        pack_compatible = bool(inspection.get("pack_compatible"))
        if (
            archive.get("status") != "ready"
            or not archive.get("verified")
            or archive.get("pack_compatible") is not pack_compatible
        ):
            self.store.set_backup_archive_status(
                archive_id,
                "ready",
                verified=True,
                pack_compatible=pack_compatible,
            )
            archive = self.store.backup_archive(archive_id)
        return archive, raw, image, inspection

    def _reconcile_backup_archive(self) -> None:
        """Reindex valid crash-orphans; never follow or silently trust files."""
        self.backup_archive_errors = []
        indexed = {item["filename"]: item for item in self.store.backup_archives(1000)}
        for item in list(indexed.values()):
            try:
                self._load_verified_backup_archive(int(item["id"]))
            except Exception as exc:
                self.backup_archive_errors.append(f"{item['filename']}: {exc}")
        known_names = self.store.backup_archive_filenames()
        directory_fd = self._open_backup_archive_directory()
        try:
            entries = list(os.scandir(directory_fd))
        finally:
            os.close(directory_fd)
        for entry in entries:
            name = entry.name
            if name in known_names or name.startswith("."):
                continue
            if not BACKUP_ARCHIVE_FILENAME.fullmatch(name):
                self.backup_archive_errors.append(f"{name}: unbekannter Archivdateiname")
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                self.backup_archive_errors.append(f"{name}: kein reguläres Archiv")
                continue
            try:
                raw = self._read_backup_archive_file(name)
                image, inspection, file_sha256 = self._verified_backup_archive_bytes(raw)
                metadata = self._archive_metadata(
                    name, raw, image, inspection, file_sha256
                )
                self.store.create_orphan_backup_archive(metadata)
            except Exception as exc:
                self.backup_archive_errors.append(f"{name}: {exc}")

    def _load_serial_enabled(self) -> bool:
        try:
            return bool(json.loads(self.serial_state_path.read_text(encoding="utf-8")).get("enabled", True))
        except (OSError, ValueError, TypeError):
            return True

    def _save_serial_enabled(self, enabled: bool) -> None:
        temporary = self.serial_state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"enabled": bool(enabled)}) + "\n", encoding="utf-8")
        with suppress(OSError):
            temporary.chmod(0o600)
        os.replace(temporary, self.serial_state_path)

    def _load_maintenance_live_writes(self, default: bool) -> bool:
        try:
            payload = json.loads(self.maintenance_settings_path.read_text(encoding="utf-8"))
            test_mode = payload.get("test_mode")
            if isinstance(test_mode, bool):
                return not test_mode
        except (OSError, ValueError, TypeError):
            pass
        return bool(default)

    def _save_maintenance_test_mode(self, test_mode: bool) -> None:
        temporary = self.maintenance_settings_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "test_mode": bool(test_mode)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with suppress(OSError):
            temporary.chmod(0o600)
        os.replace(temporary, self.maintenance_settings_path)

    def maintenance_settings(self) -> dict:
        with self.state_lock:
            live_writes_enabled = bool(self.maintenance_live_writes_enabled)
        return {
            "test_mode": not live_writes_enabled,
            "maintenance_live_writes_enabled": live_writes_enabled,
        }

    def set_maintenance_test_mode(self, test_mode: bool) -> dict:
        if not isinstance(test_mode, bool):
            raise ValueError("test_mode muss true oder false sein")
        with self.state_lock:
            self._save_maintenance_test_mode(test_mode)
            self.maintenance_live_writes_enabled = not test_mode
        return self.maintenance_settings()

    @staticmethod
    def _normalize_soot_filter_settings(payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Rußfilter-Einstellungen müssen ein Objekt sein")
        try:
            zero = float(payload.get("zero_temperature_c"))
            full = float(payload.get("full_temperature_c"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Rußfilter-Temperaturen müssen Zahlen sein") from exc
        if not (math.isfinite(zero) and math.isfinite(full) and 100 <= zero <= 800 and 100 <= full <= 800):
            raise ValueError("Rußfilter-Temperaturen müssen zwischen 100 und 800 °C liegen")
        if full - zero < 10:
            raise ValueError("100-%-Temperatur muss mindestens 10 °C über der 0-%-Temperatur liegen")
        return {
            "zero_temperature_c": int(zero) if zero.is_integer() else zero,
            "full_temperature_c": int(full) if full.is_integer() else full,
        }

    def _load_soot_filter_settings(self) -> dict:
        defaults = {
            "zero_temperature_c": int(DEFAULT_SOOT_FILTER_ZERO_C),
            "full_temperature_c": int(DEFAULT_SOOT_FILTER_FULL_C),
        }
        try:
            return self._normalize_soot_filter_settings(
                json.loads(self.soot_filter_settings_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError):
            return defaults

    def _save_soot_filter_settings(self, settings: dict) -> None:
        temporary = self.soot_filter_settings_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 1, **settings}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with suppress(OSError):
            temporary.chmod(0o600)
        os.replace(temporary, self.soot_filter_settings_path)

    def soot_filter_settings(self) -> dict:
        with self.state_lock:
            settings = dict(self.soot_filter_config)
        return {
            **settings,
            "source": "Motorabgastemperatur",
            "source_key": SOOT_FILTER_SOURCE_KEY,
            "green_below_percent": 60,
            "red_from_percent": 90,
        }

    def set_soot_filter_settings(self, payload: object) -> dict:
        settings = self._normalize_soot_filter_settings(payload)
        self._save_soot_filter_settings(settings)
        with self.state_lock:
            self.soot_filter_config = settings
        return self.soot_filter_settings()

    @staticmethod
    def _normalize_api_settings(payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("API-Einstellungen müssen ein Objekt sein")
        write_enabled = payload.get("write_enabled", False)
        if not isinstance(write_enabled, bool):
            raise ValueError("write_enabled muss true oder false sein")
        auth_level = _validated_auth_level(
            payload.get("auth_level", API_DEFAULT_AUTH_LEVEL), "API-Auth-Level"
        )
        return {"write_enabled": write_enabled, "auth_level": auth_level}

    def _load_api_settings(self) -> dict:
        try:
            return self._normalize_api_settings(
                json.loads(self.api_settings_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError):
            return {"write_enabled": False, "auth_level": API_DEFAULT_AUTH_LEVEL}

    def _save_api_settings(self, settings: dict) -> None:
        temporary = self.api_settings_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 1, **settings}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with suppress(OSError):
            temporary.chmod(0o600)
        os.replace(temporary, self.api_settings_path)

    def api_settings(self) -> dict:
        with self.state_lock:
            settings = dict(self.api_config)
        return {
            **settings,
            "transport": "http",
            "tls_termination": "external-nginx",
            "actions": ["set-value"],
            "scopes": sorted(API_TOKEN_SCOPES),
        }

    def set_api_settings(self, payload: object) -> dict:
        settings = self._normalize_api_settings(payload)
        self._save_api_settings(settings)
        with self.state_lock:
            self.api_config = settings
        return self.api_settings()

    def _email_notification_secret_directory(self) -> Path:
        try:
            self.email_notification_secret_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = self.email_notification_secret_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise PermissionError("SMTP-Secretpfad ist kein sicheres Verzeichnis")
        if info.st_uid != os.geteuid():
            raise PermissionError("SMTP-Secretverzeichnis besitzt einen falschen Eigentümer")
        if stat.S_IMODE(info.st_mode) != 0o700:
            self.email_notification_secret_dir.chmod(0o700)
        return self.email_notification_secret_dir

    @staticmethod
    def _validate_email_secret_ref(secret_ref: object) -> str:
        value = str(secret_ref or "")
        if not re.fullmatch(r"smtp-[0-9a-f]{32}\.json", value):
            raise ValueError("ungültige SMTP-Secretreferenz")
        return value

    def _load_email_notification_password(self, secret_ref: object) -> str:
        if not secret_ref:
            return ""
        name = self._validate_email_secret_ref(secret_ref)
        directory = self._email_notification_secret_directory()
        directory_fd = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size <= 0
                or info.st_size > 4096
            ):
                raise PermissionError("SMTP-Secretdatei besitzt unsichere Eigenschaften")
            raw = os.read(fd, info.st_size + 1)
            if len(raw) != info.st_size:
                raise OSError("SMTP-Secretdatei wurde nicht exakt gelesen")
            payload = json.loads(raw.decode("utf-8"))
            password = str(payload.get("password") or "")
            if len(password) > 1024:
                raise ValueError("SMTP-Passwort ist zu lang")
            return password
        finally:
            os.close(fd)

    def _write_email_notification_password(self, password: str) -> str:
        self._email_notification_secret_directory()
        secret_ref = f"smtp-{secrets.token_hex(16)}.json"
        destination = self.email_notification_secret_dir / secret_ref
        write_json_exclusive(
            destination, {"version": 1, "password": str(password)}
        )
        destination.chmod(0o600)
        return secret_ref

    def _delete_email_notification_password(self, secret_ref: object) -> None:
        if not secret_ref:
            return
        name = self._validate_email_secret_ref(secret_ref)
        (self._email_notification_secret_directory() / name).unlink(missing_ok=True)

    def _prune_email_notification_passwords(self, keep_ref: object) -> None:
        keep = str(keep_ref or "")
        directory = self._email_notification_secret_directory()
        for path in directory.glob("smtp-*.json"):
            if path.name == keep or not re.fullmatch(r"smtp-[0-9a-f]{32}\.json", path.name):
                continue
            with suppress(OSError):
                if path.is_file() and not path.is_symlink():
                    path.unlink()

    def _load_email_notification_settings(self) -> dict:
        try:
            payload = json.loads(
                self.email_notification_settings_path.read_text(encoding="utf-8")
            )
            secret_ref = str((payload.get("smtp") or {}).get("secret_ref") or "")
            settings = normalize_email_notification_settings(
                payload,
                existing_password=self._load_email_notification_password(secret_ref),
                strict=False,
            )
            settings["smtp"]["secret_ref"] = secret_ref or None
            self._prune_email_notification_passwords(secret_ref)
            return settings
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return default_email_notification_settings()

    def _save_email_notification_settings(
        self, settings: dict, secret_ref: str | None
    ) -> None:
        stored = json.loads(json.dumps(settings))
        stored["smtp"].pop("password", None)
        stored["smtp"]["secret_ref"] = secret_ref
        write_json_atomic(
            self.email_notification_settings_path,
            {"version": 1, **stored},
        )
        with suppress(OSError):
            self.email_notification_settings_path.chmod(0o600)

    def email_notification_settings(self) -> dict:
        with self.state_lock:
            settings = json.loads(json.dumps(self.email_notification_config))
            runtime = dict(self.email_notification_runtime)
        return {
            **public_email_notification_settings(settings),
            "ready": email_settings_ready(settings),
            "runtime": runtime,
            "outbox": self.store.notification_summary(),
        }

    def set_email_notification_settings(self, payload: object, username: str) -> dict:
        with self._email_notification_flow_lock:
            return self._set_email_notification_settings_locked(payload, username)

    def _set_email_notification_settings_locked(
        self, payload: object, username: str
    ) -> dict:
        with self.state_lock:
            previous = json.loads(json.dumps(self.email_notification_config))
        smtp_payload = payload.get("smtp") if isinstance(payload, dict) else {}
        supplied_password = str((smtp_payload or {}).get("password") or "")
        clear_password = bool((smtp_payload or {}).get("clear_password") is True)
        old_secret_ref = str((previous.get("smtp") or {}).get("secret_ref") or "")
        settings = normalize_email_notification_settings(
            payload,
            existing_password=str((previous.get("smtp") or {}).get("password") or ""),
        )
        previous_revision = int(previous.get("revision") or 0)
        settings["revision"] = previous_revision + 1 if previous_revision < 0x7FFFFFFF else 1
        previous_rules_revision = int(previous.get("temperature_rules_revision") or 0)
        temperature_contract_changed = bool(
            settings["temperature_rules"] != previous.get("temperature_rules")
            or bool(settings["categories"].get("temperature"))
            != bool((previous.get("categories") or {}).get("temperature"))
        )
        settings["temperature_rules_revision"] = (
            previous_rules_revision + 1
            if temperature_contract_changed and previous_rules_revision < 0x7FFFFFFF
            else (1 if temperature_contract_changed else previous_rules_revision)
        )
        new_secret_ref = old_secret_ref or None
        created_secret_ref = None
        if supplied_password:
            created_secret_ref = self._write_email_notification_password(
                settings["smtp"]["password"]
            )
            new_secret_ref = created_secret_ref
        elif clear_password:
            new_secret_ref = None
        settings["smtp"]["secret_ref"] = new_secret_ref
        try:
            self._save_email_notification_settings(settings, new_secret_ref)
        except Exception:
            if created_secret_ref:
                with suppress(Exception):
                    self._delete_email_notification_password(created_secret_ref)
            raise
        if old_secret_ref and old_secret_ref != new_secret_ref:
            with suppress(Exception):
                self._delete_email_notification_password(old_secret_ref)
        changed_at = _now()
        with suppress(Exception):
            self.store.clear_notification_dispatch_pause(changed_at)
        with self.state_lock:
            self.email_notification_config = settings
            self.email_notification_runtime.update({
                "paused_revision": None,
                "paused_reason": None,
                "last_send_error": None,
            })
        cancellation_error = None
        try:
            canceled = self.store.cancel_obsolete_notifications(
                changed_at, settings["recipients"], settings["categories"],
                settings["enabled"],
            )
        except Exception as exc:
            canceled = 0
            cancellation_error = f"Outbox-Widerruf fehlgeschlagen: {exc}"[:1000]
            with self.state_lock:
                self.email_notification_runtime["last_send_error"] = cancellation_error
        try:
            self.store.notification_audit(changed_at, username, "settings", {
                "enabled": settings["enabled"],
                "categories": settings["categories"],
                "smtp_host": settings["smtp"]["host"],
                "smtp_port": settings["smtp"]["port"],
                "smtp_security": settings["smtp"]["security"],
                "recipient_count": len(settings["recipients"]),
                "canceled_outbox_items": canceled,
                "cancellation_error": bool(cancellation_error),
                "password_configured": bool(settings["smtp"]["password"]),
                "secret_in_audit": False,
            })
        except Exception as exc:
            with self.state_lock:
                self.email_notification_runtime["last_send_error"] = (
                    f"E-Mail-Einstellung aktiv; Audit fehlgeschlagen: {exc}"[:1000]
                )
        return self.email_notification_settings()

    def test_email_notification(self, username: str, request_id: str) -> dict:
        with self._email_notification_flow_lock:
            return self._test_email_notification_locked(username, request_id)

    def _test_email_notification_locked(self, username: str, request_id: str) -> dict:
        with self.state_lock:
            settings = json.loads(json.dumps(self.email_notification_config))
        if not email_settings_ready(settings):
            raise ValueError(
                "SMTP, Absender, Empfänger und öffentliche ODM-Adresse sind nicht vollständig"
            )
        request_id = str(request_id or "").strip().lower()
        if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            request_id,
        ):
            raise ValueError("request_id muss eine gültige UUID sein")
        if self.store.notification_test_request_exists(request_id):
            return {"ok": True, "queued": 0, "request_id": request_id}
        recorded_at = _now()
        cutoff = (
            _history_datetime(recorded_at, "Testmail-Zeit")
            - timedelta(seconds=EMAIL_TEST_RATE_LIMIT_SECONDS)
        ).isoformat()
        if self.store.recent_test_notification(cutoff):
            raise APIRequestConflictError(
                f"Testmail ist auf eine Nachricht je {EMAIL_TEST_RATE_LIMIT_SECONDS} Sekunden begrenzt"
            )
        base_key = f"test:{request_id}"
        body = (
            "Diese Testnachricht bestätigt die SMTP-Einstellungen des "
            "Open Dachs Managers.\n\n"
            f"Zeit: {recorded_at}\nODM öffnen: {settings.get('public_base_url') or ''}"
        )
        messages = []
        for recipient in settings["recipients"]:
            recipient_key = hashlib.sha256(recipient.casefold().encode()).hexdigest()[:16]
            event_key = f"{base_key}:to:{recipient_key}"
            messages.append({
                "event_key": event_key,
                "category": "test",
                "recipient": recipient,
                "subject": "[ODM] Testnachricht",
                "body": body,
                "message_id": stable_message_id(
                    event_key, recipient, settings["smtp"]["from_address"]
                ),
                "context": {"test": True},
            })
        queued = self.store.enqueue_notification_messages(
            recorded_at,
            messages,
            audit={
                "actor": username,
                "action": "test-queued",
                "details": {
                    "request_id": request_id,
                    "recipient_count": len(settings["recipients"]),
                    "secret_in_audit": False,
                },
            },
        )
        return {"ok": True, "queued": queued, "request_id": request_id}

    def email_notification_outbox(self, limit: int = 50) -> dict:
        return {
            "items": self.store.notification_outbox(limit),
            "summary": self.store.notification_summary(),
        }

    def _observe_email_notifications(
        self, recorded_at: str, display_status_result: dict | None = None
    ) -> None:
        with self._email_notification_flow_lock:
            self._observe_email_notifications_locked(recorded_at, display_status_result)

    def _observe_email_notifications_locked(
        self, recorded_at: str, display_status_result: dict | None = None
    ) -> None:
        with self.state_lock:
            settings = json.loads(json.dumps(self.email_notification_config))
            cached_values = [dict(row) for row in self.live_values.values()]
        values = [
            row for row in cached_values
            if str(row.get("recorded_at") or "") == str(recorded_at)
        ]
        cache_values = {
            str(row["key"]): row.get("value")
            for row in values
            if int(row["block"]) == 104
        }
        current_maintenance = maintenance_status(cache_values) if cache_values else None
        previous = self.store.notification_state()
        previous_fault = dict(previous.get("fault") or {})
        previous_raw = previous_fault.get("raw")
        current_row = next(
            (
                row for row in values
                if int(row["block"]) == 22 and row["key"] == "Hka_Bd.bStoerung"
            ),
            None,
        )
        try:
            current_raw = int(current_row.get("raw")) if current_row else None
        except (TypeError, ValueError):
            current_raw = None
        episode_started_at = str(previous_fault.get("episode_started_at") or "")
        manual_clear = bool(
            previous_raw
            and current_raw == 0
            and episode_started_at
            and self.store.recent_confirmed_manual_reset(
                int(previous_raw) + 100,
                episode_started_at,
                recorded_at,
            )
        )
        exact_service_text = None
        status = display_status_result if isinstance(display_status_result, dict) else {}
        if (
            current_raw
            and status.get("line2_kind") == "servicecode"
            and status.get("line2_available") is True
            and int(status.get("service_code") or 0) == int(current_raw) + 100
            and str(status.get("recorded_at") or "") == str(recorded_at)
        ):
            exact_service_text = str(status.get("line2") or "") or None
        state, events = detect_notification_events(
            values=values,
            maintenance=current_maintenance,
            settings=settings,
            previous_state=previous,
            recorded_at=recorded_at,
            manual_fault_clear=manual_clear,
            service_text=exact_service_text,
        )
        messages = []
        for event in events:
            rendered = render_notification(event, settings, recorded_at=recorded_at)
            for recipient in settings["recipients"]:
                recipient_key = hashlib.sha256(
                    recipient.casefold().encode("utf-8")
                ).hexdigest()[:16]
                event_key = f"{rendered['event_key']}:to:{recipient_key}"
                messages.append({
                    **rendered,
                    "event_key": event_key,
                    "recipient": recipient,
                    "message_id": stable_message_id(
                        event_key, recipient, settings["smtp"]["from_address"]
                    ),
                })
        self.store.record_notification_observation(recorded_at, state, messages)
        with self.state_lock:
            self.email_notification_runtime.update({
                "last_observation_at": recorded_at,
                "last_observation_error": None,
            })

    def _email_notification_dispatch_once(self) -> float:
        with self._email_notification_flow_lock:
            return self._email_notification_dispatch_once_locked()

    def _email_notification_dispatch_once_locked(self) -> float:
        with self.state_lock:
            settings = json.loads(json.dumps(self.email_notification_config))
            runtime = dict(self.email_notification_runtime)
        revision = int(settings.get("revision") or 0)
        if runtime.get("paused_revision") == revision:
            return 2.0
        if not email_settings_ready(settings):
            return 2.0
        claimed_at = _now()
        item = self.store.claim_notification(
            claimed_at, test_only=not bool(settings.get("enabled"))
        )
        if item is None:
            return 2.0

        recipient = str(item["recipient"])
        category = str(item["category"])
        recipient_id = hashlib.sha256(
            recipient.casefold().encode("utf-8")
        ).hexdigest()[:16]
        category_active = category == "test" or bool(
            settings.get("enabled") and settings.get("categories", {}).get(category)
        )
        configured_recipients = set(settings.get("recipients") or [])
        if recipient not in configured_recipients or not category_active:
            reason = (
                "Empfänger wurde entfernt"
                if recipient not in configured_recipients
                else "Kategorie wurde deaktiviert"
            )
            self.store.cancel_notification(int(item["id"]), claimed_at, reason)
            with suppress(Exception):
                self.store.notification_audit(claimed_at, "system", "canceled", {
                    "outbox_id": int(item["id"]),
                    "category": category,
                    "recipient_id": recipient_id,
                    "reason": reason,
                })
            return 0.0

        try:
            recipient_settings = json.loads(json.dumps(settings))
            recipient_settings["recipients"] = [recipient]
            self._send_smtp_email(
                recipient_settings,
                subject=str(item["subject"]),
                body=str(item["body"]),
                message_id=str(item["message_id"]),
            )
        except Exception as exc:
            failed_at = datetime.now(timezone.utc)
            attempts = max(1, int(item.get("attempts") or 1))
            delay = EMAIL_RETRY_SECONDS[min(attempts - 1, len(EMAIL_RETRY_SECONDS) - 1)]
            policy = smtp_error_policy(exc)
            terminal = policy == "dead" or attempts >= EMAIL_MAX_ATTEMPTS
            paused = policy == "pause" and not terminal
            error = sanitized_smtp_error(exc, settings)
            next_attempt_at = (
                failed_at.isoformat()
                if paused
                else (failed_at + timedelta(seconds=delay)).isoformat()
            )
            if paused:
                with self.state_lock:
                    self.email_notification_runtime.update({
                        "last_send_error": error[:1000],
                        "paused_revision": revision,
                        "paused_reason": error[:1000],
                    })
                self.store.set_notification_dispatch_pause(
                    revision, error[:1000], failed_at.isoformat()
                )
            try:
                self.store.finish_notification(
                    int(item["id"]),
                    sent=False,
                    recorded_at=failed_at.isoformat(),
                    error=error,
                    next_attempt_at=next_attempt_at,
                    terminal=terminal,
                )
            except Exception as persist_exc:
                error += f"; Outbox-Abschluss fehlgeschlagen: {persist_exc}"
            else:
                with suppress(Exception):
                    self.store.notification_audit(
                        failed_at.isoformat(), "system", "failed", {
                            "outbox_id": int(item["id"]),
                            "category": category,
                            "recipient_id": recipient_id,
                            "attempt": attempts,
                            "error_type": exc.__class__.__name__,
                            "policy": policy,
                            "retry_seconds": delay if policy == "retry" and not terminal else None,
                            "dead_letter": terminal,
                            "paused_revision": revision if paused else None,
                        },
                    )
            with self.state_lock:
                self.email_notification_runtime.update({
                    "last_send_error": error[:1000],
                    "paused_revision": revision if paused else None,
                    "paused_reason": error[:1000] if paused else None,
                })
            return 2.0 if paused else 0.0

        sent_at = _now()
        try:
            self.store.finish_notification(
                int(item["id"]), sent=True, recorded_at=sent_at
            )
        except Exception as exc:
            error = f"SMTP-Relay nahm Nachricht an; lokaler Abschluss unklar: {exc}"
            with self.state_lock:
                self.email_notification_runtime["last_send_error"] = error[:1000]
            return 2.0
        with suppress(Exception):
            self.store.notification_audit(sent_at, "system", "sent", {
                "outbox_id": int(item["id"]),
                "category": category,
                "recipient_id": recipient_id,
                "attempt": int(item["attempts"]),
                "relay_accepted": True,
            })
        with self.state_lock:
            self.email_notification_runtime.update({
                "last_send_at": sent_at,
                "last_send_error": None,
                "paused_revision": None,
                "paused_reason": None,
            })
        return 0.0

    def _email_notification_loop(self) -> None:
        with self.state_lock:
            self.email_notification_runtime["dispatcher_alive"] = True
        try:
            while not self.stop_event.is_set():
                try:
                    if (
                        time.monotonic() - self._last_notification_retention_purge
                        >= 3600.0
                    ):
                        self.store.purge_notifications(datetime.now(timezone.utc))
                        self._last_notification_retention_purge = time.monotonic()
                    delay = self._email_notification_dispatch_once()
                except Exception as exc:
                    error = str(exc) or exc.__class__.__name__
                    with self.state_lock:
                        self.email_notification_runtime.update({
                            "last_send_error": error[:1000],
                            "paused_reason": "Dispatcherfehler; automatischer Wiederanlauf",
                        })
                    with suppress(Exception):
                        self.store.notification_audit(
                            _now(), "system", "dispatcher-error", {
                                "error_type": exc.__class__.__name__,
                            },
                        )
                    delay = 5.0
                self.stop_event.wait(max(0.0, float(delay)))
        finally:
            with self.state_lock:
                self.email_notification_runtime["dispatcher_alive"] = False

    @staticmethod
    def _normalize_local_access_settings(payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Lokaler-Zugang-Einstellungen müssen ein Objekt sein")
        enabled = payload.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("enabled muss true oder false sein")
        port = payload.get("port", LOCAL_ACCESS_DEFAULT_PORT)
        if type(port) is not int:
            raise ValueError("Port muss eine ganze Zahl sein")
        if not LOCAL_ACCESS_MIN_PORT <= port <= 65535:
            raise ValueError(
                f"Port muss zwischen {LOCAL_ACCESS_MIN_PORT} und 65535 liegen"
            )
        role = payload.get("role", "admin")
        if not isinstance(role, str) or role not in LOCAL_ACCESS_ROLES:
            raise ValueError("Rechte müssen admin oder guest sein")
        return {"enabled": enabled, "port": port, "role": role}

    def _load_local_access_settings(self) -> dict:
        try:
            return self._normalize_local_access_settings(
                json.loads(self.local_access_settings_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError):
            return {
                "enabled": False,
                "port": LOCAL_ACCESS_DEFAULT_PORT,
                "role": "admin",
            }

    def _save_local_access_settings(self, settings: dict) -> None:
        destination = self.local_access_settings_path
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"version": 1, **settings},
                        ensure_ascii=False,
                        indent=2,
                    ) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())

            # This replace is the transaction's commit point. Before it, an
            # exception leaves the old file and listener untouched. After it,
            # runtime must adopt the new listener even if the best-effort
            # directory fsync is unavailable; rolling back after replacement
            # would itself have an ambiguous failure window.
            os.replace(temporary, destination)
            with suppress(OSError):
                directory_fd = os.open(
                    destination.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _commit_local_access_settings(self, settings: dict) -> None:
        self._save_local_access_settings(settings)
        with self.state_lock:
            self.local_access_config = dict(settings)

    def _set_local_access_runtime(self, **changes: object) -> None:
        with self.state_lock:
            self.local_access_runtime.update(changes)

    def local_access_settings(self) -> dict:
        with self.state_lock:
            settings = dict(self.local_access_config)
            runtime = dict(self.local_access_runtime)
        return {
            **settings,
            "bind_host": LOCAL_ACCESS_BIND_HOST,
            **runtime,
        }

    def set_local_access_manager(self, manager: LocalAccessManager) -> None:
        self.local_access_manager = manager

    def set_local_access_settings(self, payload: object) -> dict:
        settings = self._normalize_local_access_settings(payload)
        manager = self.local_access_manager
        if manager is not None:
            return manager.reconfigure(settings)
        self._commit_local_access_settings(settings)
        self._set_local_access_runtime(
            active=False,
            last_error=None,
            restart_required=bool(settings["enabled"]),
            url=None,
        )
        return self.local_access_settings()

    @staticmethod
    def _default_dashboard_cards() -> list[dict]:
        wanted = set(DEFAULT_OVERVIEW_SERIES_IDS)
        cards = [
            {"block": int(block), "key": str(key)}
            for series_id, _title, block, key, _unit, _color in DEFAULT_DASHBOARD_SERIES
            if series_id in wanted
        ]
        cards.append({"block": 22, "key": DASHBOARD_RUNTIME_24H_KEY})
        return cards

    @staticmethod
    def _derived_dashboard_fields() -> list[dict]:
        return [
            {
                "source": source,
                "label": label,
                "block": int(block),
                "key": key,
                "unit": unit,
                "derived": True,
            }
            for source, label, block, key, unit in DASHBOARD_DERIVED_FIELDS
        ]

    def _dashboard_available_keys(self, block: int) -> set[str]:
        block = int(block)
        if block == 18 or block not in self.pack.addressable_blocks():
            return set()
        groups = self.pack.presentation_groups(block)
        component_to_base = {
            component: base
            for base, group in groups.items()
            for component in group["components"]
        }
        keys: set[str] = set()
        for key, metadata in self.pack.field_map(block).items():
            if not web_field_visible(block, key, metadata):
                continue
            base = component_to_base.get(key)
            if base:
                if key == groups[base]["components"][0]:
                    keys.add(base)
                continue
            keys.add(key)
        if block == 24:
            keys.add("Hka_Mw1.Temp.DachsAustritt")
        keys.update(
            key
            for _source, _label, source_block, key, _unit in DASHBOARD_DERIVED_FIELDS
            if int(source_block) == block
        )
        return keys

    def _normalize_dashboard_cards(self, cards: object) -> list[dict]:
        if not isinstance(cards, list):
            raise ValueError("Dashboard-Karten müssen eine Liste sein")
        if len(cards) > MAX_DASHBOARD_CARDS:
            raise ValueError(f"höchstens {MAX_DASHBOARD_CARDS} Dashboard-Karten sind erlaubt")
        normalized: list[dict] = []
        seen: set[tuple[int, str]] = set()
        addressable = set(self.pack.addressable_blocks())
        for card in cards:
            if not isinstance(card, dict):
                raise ValueError("ungültige Dashboard-Karte")
            try:
                block = int(card.get("block"))
            except (TypeError, ValueError) as exc:
                raise ValueError("Dashboard-Karte benötigt einen gültigen Block") from exc
            key = str(card.get("key", "")).strip()
            if block not in addressable or key not in self._dashboard_available_keys(block):
                raise KeyError(f"unbekanntes Dashboard-Feld: Block {block} · {key}")
            identity = (block, key)
            if identity in seen:
                continue
            seen.add(identity)
            normalized.append({"block": block, "key": key})
        extra_blocks = {card["block"] for card in normalized} - set(DEFAULT_MONITOR_BLOCKS)
        if len(extra_blocks) > MAX_DASHBOARD_EXTRA_BLOCKS:
            raise ValueError(
                f"Dashboard darf höchstens {MAX_DASHBOARD_EXTRA_BLOCKS} zusätzliche Blöcke überwachen"
            )
        return normalized

    def _migrate_stored_dashboard_cards(self, cards: object) -> tuple[list, bool]:
        """Map retired tiles and drop unknown ones from a stored selection.

        A single stale key (retired tile, removed pack field) must not throw
        away the whole user selection; the remaining order is preserved.
        """
        if not isinstance(cards, list):
            raise ValueError("Dashboard-Karten müssen eine Liste sein")
        migrated: list = []
        changed = False
        for card in cards:
            if not isinstance(card, dict):
                changed = True
                continue
            try:
                identity = (int(card.get("block")), str(card.get("key", "")).strip())
            except (TypeError, ValueError):
                changed = True
                continue
            replacement = DASHBOARD_RETIRED_CARDS.get(identity)
            if replacement is not None:
                identity = replacement
                changed = True
            block, key = identity
            if block not in self.pack.addressable_blocks() or key not in self._dashboard_available_keys(block):
                changed = True
                continue
            migrated.append({"block": block, "key": key})
        return migrated[:MAX_DASHBOARD_CARDS], changed

    def _load_dashboard_cards(self) -> list[dict]:
        try:
            payload = json.loads(self.dashboard_settings_path.read_text(encoding="utf-8"))
            stored, changed = self._migrate_stored_dashboard_cards(payload.get("cards"))
            cards = self._normalize_dashboard_cards(stored)
            changed = changed or len(cards) != len(stored)
            version = int(payload.get("version", 1))
            runtime_card = {"block": 22, "key": DASHBOARD_RUNTIME_24H_KEY}
            if version < DASHBOARD_SETTINGS_VERSION and runtime_card not in cards:
                if len(cards) < MAX_DASHBOARD_CARDS:
                    cards.append(runtime_card)
                changed = True
            if changed:
                with suppress(OSError):
                    self._save_dashboard_cards(cards)
            return cards
        except (OSError, ValueError, TypeError, KeyError):
            return self._default_dashboard_cards()

    def _save_dashboard_cards(self, cards: list[dict]) -> None:
        temporary = self.dashboard_settings_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"version": DASHBOARD_SETTINGS_VERSION, "cards": cards},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        with suppress(OSError):
            temporary.chmod(0o600)
        os.replace(temporary, self.dashboard_settings_path)

    def dashboard_settings(self) -> dict:
        with self.state_lock:
            cards = [dict(card) for card in self.dashboard_cards]
        return {
            "cards": cards,
            "default_cards": self._default_dashboard_cards(),
            "derived_fields": self._derived_dashboard_fields(),
            "max_cards": MAX_DASHBOARD_CARDS,
            "max_extra_blocks": MAX_DASHBOARD_EXTRA_BLOCKS,
        }

    def set_dashboard_settings(self, cards: object) -> dict:
        normalized = self._normalize_dashboard_cards(cards)
        self._save_dashboard_cards(normalized)
        selected = {(card["block"], card["key"]) for card in normalized}
        with self.state_lock:
            self.dashboard_cards = normalized
            for identity in list(self.live_values):
                block, key = identity
                default_visible = block in DEFAULT_MONITOR_BLOCKS and web_monitor_field_visible(block, key)
                if not default_visible and identity not in selected:
                    self.live_values.pop(identity, None)
        return self.dashboard_settings()

    def start(self) -> None:
        if self.initial_credentials:
            print("Open Dachs Manager Web-Erstzugang (nur einmal anzeigen):", flush=True)
            print(f"  admin / {self.initial_credentials['admin']}", flush=True)
            print(f"  gast  / {self.initial_credentials['gast']}", flush=True)
        self.monitor_thread.start()
        self.diagnosis_250h_cache.start()
        # A large SQLite history can make either cleanup query take minutes on
        # the Pi's SD card.  Keep the HTTP listener responsive and run the
        # maintenance work in the background after startup.
        self.maintenance_thread.start()
        self.automation_thread.start()
        self.email_notification_thread.start()
        self.update_manager.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.diagnosis_250h_cache.stop()
        self.monitor_thread.join(timeout=2)
        if self.automation_thread.is_alive():
            self.automation_thread.join(timeout=2)
        if self.email_notification_thread.is_alive():
            self.email_notification_thread.join(timeout=2)
        self.update_manager.stop()

    def _load_users(self) -> list[dict]:
        try:
            loaded = list(json.loads(self.users_path.read_text(encoding="utf-8")).get("users", []))
        except Exception:
            return []
        normalized = []
        for item in loaded:
            if not isinstance(item, dict) or not item.get("username"):
                continue
            user = dict(item)
            user["role"] = "admin" if user.get("role") == "admin" else "guest"
            user["enabled"] = bool(user.get("enabled", True))
            user.setdefault("created_at", None)
            user.setdefault("updated_at", None)
            normalized.append(user)
        return normalized

    def _save_users(self, users: list[dict]) -> None:
        temporary = self.users_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 2, "users": users}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.users_path)

    def users(self) -> list[dict]:
        return [_public_user(user) for user in sorted(self._load_users(), key=lambda item: str(item["username"]).lower())]

    @staticmethod
    def _require_password(value: object) -> str:
        password = str(value or "")
        if len(password) < 12:
            raise ValueError("Passwort muss mindestens 12 Zeichen haben")
        return password

    def create_user(self, username: object, password: object, role: object) -> dict:
        clean_username = _validate_username(username)
        clean_password = self._require_password(password)
        clean_role = str(role or "guest").strip().lower()
        if clean_role not in {"admin", "guest"}:
            raise ValueError("Rolle muss admin oder guest sein")
        users = self._load_users()
        if any(str(user["username"]).lower() == clean_username.lower() for user in users):
            raise ValueError("Benutzername ist bereits vergeben")
        now = _now()
        salt, digest = _hash_password(clean_password)
        user = {
            "username": clean_username,
            "role": clean_role,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
            "salt": salt,
            "digest": digest,
        }
        users.append(user)
        self._save_users(users)
        return _public_user(user)

    def update_user(self, actor_username: str, username: str, payload: dict) -> dict:
        users = self._load_users()
        user = next((item for item in users if item.get("username") == username), None)
        if user is None:
            raise KeyError("Benutzer nicht gefunden")
        role = str(payload.get("role", user.get("role", "guest"))).strip().lower()
        if role not in {"admin", "guest"}:
            raise ValueError("Rolle muss admin oder guest sein")
        enabled = payload.get("enabled", bool(user.get("enabled", True)))
        if not isinstance(enabled, bool):
            raise ValueError("enabled muss true oder false sein")
        if username == actor_username and (role != "admin" or not enabled):
            raise PermissionError("das eigene aktive Admin-Konto kann nicht herabgestuft oder deaktiviert werden")
        other_enabled_admins = [
            item for item in users
            if item is not user and item.get("role") == "admin" and bool(item.get("enabled", True))
        ]
        if user.get("role") == "admin" and bool(user.get("enabled", True)) and (role != "admin" or not enabled) and not other_enabled_admins:
            raise PermissionError("mindestens ein aktiver Admin muss erhalten bleiben")
        password = payload.get("password")
        if password not in (None, ""):
            salt, digest = _hash_password(self._require_password(password))
            user["salt"] = salt
            user["digest"] = digest
        user["role"] = role
        user["enabled"] = enabled
        user["updated_at"] = _now()
        self._save_users(users)
        self._invalidate_user_sessions(username)
        return _public_user(user)

    def delete_user(self, actor_username: str, username: str) -> dict:
        if username == actor_username:
            raise PermissionError("das eigene Konto kann nicht gelöscht werden")
        users = self._load_users()
        user = next((item for item in users if item.get("username") == username), None)
        if user is None:
            raise KeyError("Benutzer nicht gefunden")
        if user.get("role") == "admin" and bool(user.get("enabled", True)):
            remaining = [
                item for item in users
                if item is not user and item.get("role") == "admin" and bool(item.get("enabled", True))
            ]
            if not remaining:
                raise PermissionError("der letzte aktive Admin kann nicht gelöscht werden")
        users.remove(user)
        self._save_users(users)
        self._invalidate_user_sessions(username)
        return {"username": username}

    def _invalidate_user_sessions(self, username: str) -> None:
        with self.state_lock:
            expired = [token for token, item in self.sessions.items() if item[0] == username]
            for token in expired:
                self.sessions.pop(token, None)

    def _startup_maintenance(self) -> None:
        """Run one-time history maintenance off the HTTP start path."""
        if self.stop_event.wait(1.0):
            return
        try:
            if not self.dashboard_cleanup_marker.exists():
                removed_invalid = self.store.purge_invalid_dashboard()
                self.dashboard_cleanup_marker.write_text(
                    json.dumps({"completed_at": _now(), "removed": removed_invalid}) + "\n",
                    encoding="utf-8",
                )
                with suppress(OSError):
                    self.dashboard_cleanup_marker.chmod(0o600)
                if removed_invalid:
                    print(
                        f"Open Dachs Manager: {removed_invalid} ungültige Diagrammwerte aus der Historie entfernt",
                        flush=True,
                    )
        except Exception as exc:
            # Maintenance must not take down the web interface. The hourly
            # retention pass in the monitor loop remains a later retry point.
            print(f"Open Dachs Manager: Historienbereinigung fehlgeschlagen: {exc}", flush=True)
        try:
            if self.store.history_migration(MOTOR_STATUS_MIGRATION) is None:
                result = self.store.backfill_motor_status_points(self.stop_event)
                if result["complete"]:
                    self.store.complete_history_migration(
                        MOTOR_STATUS_MIGRATION,
                        result,
                    )
                    print(
                        "Open Dachs Manager: Motorstatus-Historie aufgebaut "
                        f"({result['inserted']} Punkte aus {result['scanned']} Snapshots)",
                        flush=True,
                    )
        except Exception as exc:
            # A later service start retries the idempotent INSERT OR IGNORE
            # backfill. Live transitions continue to be recorded meanwhile.
            print(f"Open Dachs Manager: Motorstatus-Backfill fehlgeschlagen: {exc}", flush=True)

    def login(self, username: str, password: str) -> tuple[str, str] | None:
        if str(username).strip().casefold() == LOCAL_ACCESS_PRINCIPAL.casefold():
            return None
        for user in self._load_users():
            if (
                user.get("username") == username
                and bool(user.get("enabled", True))
                and _check_password(password, user.get("salt", ""), user.get("digest", ""))
            ):
                token = secrets.token_urlsafe(32)
                with self.state_lock:
                    self.sessions[token] = (
                        username, str(user.get("role", "guest")),
                        time.time() + 12 * 3600, time.time_ns(),
                    )
                return token, str(user.get("role", "guest"))
        return None

    def verify_admin_password(self, username: str, password: str) -> bool:
        """Re-authenticate a real enabled admin without creating a session."""
        for user in self._load_users():
            if (
                user.get("username") == username
                and user.get("role") == "admin"
                and bool(user.get("enabled", True))
                and _check_password(
                    password, user.get("salt", ""), user.get("digest", "")
                )
            ):
                return True
        return False

    def session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.state_lock:
            item = self.sessions.get(token)
            if item is None:
                return None
            if item[2] < time.time():
                self.sessions.pop(token, None)
                return None
            return {"username": item[0], "role": item[1]}

    def session_update_identity(self, token: str | None) -> tuple[str, int]:
        if not token:
            raise PermissionError("gültige Sitzung erforderlich")
        with self.state_lock:
            item = self.sessions.get(token)
            if item is None or item[2] < time.time():
                self.sessions.pop(token, None)
                raise PermissionError("gültige Sitzung erforderlich")
            return hashlib.sha256(token.encode("utf-8")).hexdigest(), int(item[3])

    def logout(self, token: str | None) -> None:
        if token:
            with self.state_lock:
                self.sessions.pop(token, None)
        self.update_manager.preview = None

    def change_password(self, username: str, current_password: str, new_password: str) -> None:
        new_password = self._require_password(new_password)
        users = self._load_users()
        found = False
        for user in users:
            if user.get("username") != username:
                continue
            if not _check_password(current_password, user.get("salt", ""), user.get("digest", "")):
                raise PermissionError("bisheriges Passwort ist falsch")
            salt, digest = _hash_password(new_password)
            user["salt"] = salt
            user["digest"] = digest
            user["updated_at"] = _now()
            found = True
            break
        if not found:
            raise KeyError("Benutzer nicht gefunden")
        self._save_users(users)
        self._invalidate_user_sessions(username)

    def change_guest_password(self, admin_username: str, current_password: str, new_password: str) -> None:
        new_password = self._require_password(new_password)
        users = self._load_users()
        admin = next((item for item in users if item.get("username") == admin_username), None)
        guest = next((item for item in users if item.get("username") == "gast"), None)
        if admin is None or admin.get("role") != "admin":
            raise PermissionError("Admin-Berechtigung erforderlich")
        if not _check_password(current_password, admin.get("salt", ""), admin.get("digest", "")):
            raise PermissionError("Admin-Passwort ist falsch")
        if guest is None or guest.get("role") != "guest":
            raise KeyError("Gastbenutzer nicht gefunden")
        salt, digest = _hash_password(new_password)
        guest["salt"] = salt
        guest["digest"] = digest
        guest["updated_at"] = _now()
        self._save_users(users)
        self._invalidate_user_sessions("gast")

    @staticmethod
    def _read_runtime_24h(session, recorded_at: str) -> dict:
        """Read twice and reject a byte-tear that is not stable on retry."""
        samples = []

        def read_sample() -> dict:
            response = session.request(
                RUNTIME_24H_REQUEST,
                session.next_packet(),
                timeout=0.9,
                cpu=0,
            )
            sample = runtime_24h_status(response, recorded_at)
            samples.append(sample)
            return sample

        first = read_sample()
        second = read_sample()

        def coherent(left: dict, right: dict) -> bool:
            before = int(left["counter_seconds"])
            after = int(right["counter_seconds"])
            return 0 <= after - before <= 1

        accepted = second
        if not coherent(first, second):
            third = read_sample()
            # A real stop/reset is confirmed by two zero samples. A torn read
            # around a byte carry is resolved when first and third are coherent.
            if not coherent(second, third) and not coherent(first, third):
                raise TransportError(
                    "24-h-Zähler: drei nicht kohärente RAM-Lesungen "
                    + ", ".join(str(item["counter_seconds"]) for item in samples)
                )
            accepted = third
        return {**accepted, "validated_samples": len(samples)}

    @staticmethod
    def _read_ram_telemetry(session, recorded_at: str) -> dict:
        """Read every allowlisted CPU-0 RAM field once; failures stay per field.

        Each field fails closed on its own: a transport or validation error is
        reported as ``available: False`` with the error text instead of an old
        value. Only the fixed allowlist is requested, over the shared session.
        """
        return DachsWebApp._read_ram_fields(session, RAM_TELEMETRY_FIELDS, recorded_at)

    @staticmethod
    def _read_ram_fields(session, fields, recorded_at: str) -> dict:
        results = {}
        for field in fields:
            try:
                response = session.request(
                    ram_telemetry_request(field),
                    session.next_packet(),
                    timeout=0.9,
                    cpu=0,
                )
                results[field["key"]] = ram_telemetry_status(field, response, recorded_at)
            except Exception as exc:
                results[field["key"]] = ram_telemetry_unavailable(
                    field, error=exc, recorded_at=str(recorded_at)
                )
        return results

    def _current_soot_type_code(self):
        """Raw Block-24 fuel/type byte from the current live cache, else None."""
        with self.state_lock:
            row = self.live_values.get(
                (SOOT_REGENERATION_TYPE_BLOCK, SOOT_REGENERATION_TYPE_KEY)
            )
        if not row:
            return None
        return row.get("raw")

    def _read_soot_regeneration(self, session, recorded_at: str) -> dict:
        """Gate on the mapped type code, then read the three allowlisted bytes.

        Without a readable type code or without bit 3 no RAM read is issued at
        all; the status reports unsupported/unavailable instead of an old value.
        """
        return self._read_soot_regeneration_fields(session, recorded_at)[0]

    def _read_soot_regeneration_fields(self, session, recorded_at: str) -> tuple[dict, dict]:
        """Like ``_read_soot_regeneration`` but also return the raw field reads."""
        type_code = self._current_soot_type_code()
        if soot_regeneration_supported(type_code) is not True:
            return soot_regeneration_status(type_code, {}, recorded_at), {}
        fields = self._read_ram_fields(session, SOOT_REGENERATION_FIELDS, recorded_at)
        return soot_regeneration_status(type_code, fields, recorded_at), fields

    def _display_status_max_age(self) -> float:
        """Inputs older than two slow cycles plus margin count as stale."""
        return 2.0 * float(self.slow_monitor_interval) + 5.0

    def _store_display_ram_sample(self, address: int, item: dict, now_mono: float) -> None:
        value = item.get("raw") if item.get("available") is True else None
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
            value = None
        self._display_ram[int(address)] = {
            "value": value,
            "attempt_mono": float(now_mono),
            "error": None if value is not None else str(item.get("error") or "nicht gelesen"),
        }

    def _update_display_slow_payloads(self, payloads: dict, failed_blocks: set, now_mono: float) -> None:
        for block in DISPLAY_STATUS_SLOW_BLOCKS:
            if block in payloads:
                self._display_slow_payloads[block] = (bytes(payloads[block]), float(now_mono))
            elif block in failed_blocks:
                # A failed read invalidates the cached payload immediately.
                self._display_slow_payloads.pop(block, None)

    def _refresh_display_status(
        self,
        session,
        payloads: dict,
        now_mono: float,
        recorded_at: str,
        slow_poll_due: bool,
        refreshed: set | None = None,
    ) -> dict:
        """Compute both display lines from this cycle's blocks plus fresh RAM bytes.

        Fast blocks 22/24/26 must come from the current cycle. Blocks 50/104
        and RAM-only bytes come from their last read if it is not stale.
        RAM bytes are read in the 10-s cycle; a conditional byte that becomes
        relevant between slow cycles is read once immediately and then again
        only after a full slow interval, so a persistent failure never turns
        into per-cycle retries. ``refreshed`` lists addresses already read in
        this cycle (0x0db3 from the soot-regeneration read).
        """
        refreshed = set(refreshed or ())
        max_age = self._display_status_max_age()
        block_payloads = {
            block: payloads[block] for block in DISPLAY_STATUS_FAST_BLOCKS if block in payloads
        }
        input_errors: dict[int, str] = {}
        for block in DISPLAY_STATUS_SLOW_BLOCKS:
            cached = self._display_slow_payloads.get(block)
            if cached is not None and now_mono - cached[1] <= max_age:
                block_payloads[block] = cached[0]
        values = inputs_from_payloads(block_payloads)
        to_read = []
        for address in ram_reads_needed(values):
            if address not in DISPLAY_STATUS_RAM_FIELD_BY_ADDRESS or address in refreshed:
                continue
            cached = self._display_ram.get(address)
            if (
                slow_poll_due
                or cached is None
                or now_mono - float(cached["attempt_mono"]) >= float(self.slow_monitor_interval)
            ):
                to_read.append(DISPLAY_STATUS_RAM_FIELD_BY_ADDRESS[address])
        if to_read:
            fields = self._read_ram_fields(session, to_read, recorded_at)
            for field in to_read:
                self._store_display_ram_sample(
                    int(field["address"]), fields.get(field["key"]) or {}, now_mono
                )
        for address in DISPLAY_STATUS_RAM_ADDRESSES:
            cached = self._display_ram.get(address)
            if cached is None:
                continue
            if cached["value"] is None:
                input_errors[address] = str(cached["error"])
            elif now_mono - float(cached["attempt_mono"]) > max_age:
                input_errors[address] = "veraltet"
            else:
                values[address] = int(cached["value"])
        for block in DISPLAY_STATUS_FAST_BLOCKS + DISPLAY_STATUS_SLOW_BLOCKS:
            if block not in block_payloads:
                for address in DISPLAY_STATUS_BLOCK_INPUTS[block]:
                    input_errors[address] = f"Block {block} nicht aktuell gelesen"
        return compute_display_status(values, recorded_at=recorded_at, input_errors=input_errors)

    def _publish_display_status(self, status: dict, now_mono: float) -> None:
        with self.state_lock:
            self.display_status = status
            self._display_status_mono = float(now_mono)
        if status.get("available") is not True:
            return
        lines = (str(status["line1"]), str(status["line2"]))
        if lines == self._display_last_recorded_lines:
            return
        self.store.record_display_status_change(str(status["recorded_at"]), status)
        self._display_last_recorded_lines = lines

    def _current_display_status(self) -> dict:
        """Published status, or unavailable when the monitor stopped updating it."""
        with self.state_lock:
            status = json.loads(json.dumps(self.display_status))
            computed = self._display_status_mono
        limit = max(5.0, 4.0 * float(self.interval) + 2.0)
        if computed is not None and time.monotonic() - computed > limit:
            return display_status_unavailable(
                error="veraltet: keine aktuelle Berechnung",
                recorded_at=status.get("recorded_at"),
            )
        return status

    def _monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            with self.state_lock:
                serial_enabled = self.serial_enabled
                enabled = self.monitor_enabled and serial_enabled
                dashboard_cards = tuple(
                    (int(card["block"]), str(card["key"])) for card in self.dashboard_cards
                )
                self.monitor_state["enabled"] = enabled
                self.monitor_state["serial_enabled"] = serial_enabled
                self.monitor_state["connection_state"] = "verbunden" if serial_enabled else "getrennt"
                self.monitor_state["running"] = enabled
            if not enabled:
                with suppress(Exception):
                    self.store.record_motor_status_missing(_now())
                self.stop_event.wait(0.5)
                continue
            cycle_started = time.monotonic()
            serial_done: float | None = None
            housekeeping = False
            recorded_at = _now()
            rows: list[dict] = []
            display_payloads: dict[int, bytes] = {}
            cycle_payloads: dict[int, bytes] = {}
            ram_telemetry_available: bool | None = None
            slow_poll_due = False
            display_failed_blocks: set[int] = set()
            display_status_result: dict | None = None
            soot_fields: dict = {}
            motor_status_observation: object | None = None
            motor_speed_observation: object | None = None
            ok_blocks = failed_blocks = 0
            error_text = None
            dashboard_fields = set(dashboard_cards)
            dashboard_extra_blocks = {
                block for block, _key in dashboard_fields if block not in DEFAULT_MONITOR_BLOCKS
            }
            try:
                with self.serial_lock:
                    with self.state_lock:
                        if not self.serial_enabled:
                            continue
                    # Timestamp the observation only after acquiring the serial
                    # lease. A reset that completed while this cycle waited
                    # must not appear newer than the Block-22 observation.
                    recorded_at = _now()
                    blocks = list(DEFAULT_FAST_MONITOR_BLOCKS)
                    now_mono = cycle_started
                    history_sample_due = self._history_sample_due(now_mono)
                    slow_poll_due = now_mono - self._last_slow_poll >= self.slow_monitor_interval
                    if slow_poll_due:
                        blocks.extend(DEFAULT_SLOW_MONITOR_BLOCKS)
                        blocks.extend(sorted(dashboard_extra_blocks))
                        self._last_slow_poll = now_mono
                    blocks = list(dict.fromkeys(blocks))
                    with self.service.session() as session:
                        for block in blocks:
                            try:
                                result, fields = self.service.decoded_block(session, block)
                                if not result.ok:
                                    failed_blocks += 1
                                    display_failed_blocks.add(block)
                                    error_text = f"Block {block}: Status {result.status!r}"
                                    continue
                                ok_blocks += 1
                                cycle_payloads[block] = bytes(result.payload)
                                if block in DISPLAY_STATUS_BLOCK_INPUTS:
                                    display_payloads[block] = bytes(result.payload)
                                for field in fields:
                                    if (
                                        block == MOTOR_STATUS_BLOCK
                                        and field.key == MOTOR_STATUS_KEY
                                    ):
                                        motor_status_observation = field.raw
                                    if (
                                        block == MOTOR_SPEED_BLOCK
                                        and field.key == MOTOR_SPEED_KEY
                                    ):
                                        motor_speed_observation = field.raw
                                    dashboard_selected = (block, field.key) in dashboard_fields
                                    default_visible = (
                                        block in DEFAULT_MONITOR_BLOCKS
                                        and web_monitor_field_visible(block, field.key)
                                    )
                                    if not default_visible and not dashboard_selected:
                                        continue
                                    if not web_field_visible(block, field.key, field.metadata):
                                        continue
                                    if not web_measurement_valid(block, field.key, field.value):
                                        self.live_values.pop((block, field.key), None)
                                        continue
                                    row = {
                                        "block": block,
                                        "key": field.key,
                                        "label": field.label,
                                        "raw": field.raw,
                                        "value": _json_value(field.value),
                                        "unit": field.unit or "",
                                        "type": field.metadata.get("type", "byte"),
                                        "rtt_ms": round(result.response.elapsed_ms, 1),
                                        "recorded_at": recorded_at,
                                    }
                                    if history_sample_due and (block, field.key) in HISTORY_MEASUREMENT_KEYS:
                                        rows.append(row)
                                    self.live_values[(block, field.key)] = row
                                # DachsAustritt is a calculated transient value,
                                # not a separate raw register:
                                # Hka_Mw1.Temp.DachsAustritt = sbMotor + 3.
                                if block == 24:
                                    motor_value = next(
                                        (field.value for field in fields if field.key == "Hka_Mw1.Temp.sbMotor"),
                                        None,
                                    )
                                    try:
                                        outlet_value = float(motor_value) + 3.0 if web_measurement_valid(24, "Hka_Mw1.Temp.sbMotor", motor_value) else None
                                    except (TypeError, ValueError):
                                        outlet_value = None
                                    if outlet_value is not None:
                                        if outlet_value.is_integer():
                                            outlet_value = int(outlet_value)
                                        outlet_row = {
                                            "block": 24,
                                            "key": "Hka_Mw1.Temp.DachsAustritt",
                                            "label": "Dachs-Austritt",
                                            "raw": motor_value,
                                            "value": outlet_value,
                                            "unit": "°C",
                                            "type": "derived-number",
                                            "rtt_ms": round(result.response.elapsed_ms, 1),
                                            "recorded_at": recorded_at,
                                        }
                                        self.live_values[(24, "Hka_Mw1.Temp.DachsAustritt")] = outlet_row
                                if block == 104 and result.payload:
                                    # WARTUNG_CACHE begins with a one-byte
                                    # bitset: bit 0 due, bit 1 confirmed.
                                    for key, label, mask in (
                                        ("Wartung_Cache.fStehtAn", "Wartung steht an", 0x01),
                                        ("Wartung_Cache.fBestaetigt", "Wartung bestätigt", 0x02),
                                    ):
                                        flag_row = {
                                            "block": 104,
                                            "key": key,
                                            "label": label,
                                            "raw": 1 if result.payload[0] & mask else 0,
                                            "value": bool(result.payload[0] & mask),
                                            "unit": "",
                                            "type": "bool",
                                            "rtt_ms": round(result.response.elapsed_ms, 1),
                                            "recorded_at": recorded_at,
                                        }
                                        self.live_values[(104, key)] = flag_row
                                if block in DEFAULT_SLOW_MONITOR_BLOCKS or block in dashboard_extra_blocks:
                                    with self.state_lock:
                                        self.monitor_state["last_slow_cycle"] = recorded_at
                            except Exception as exc:
                                failed_blocks += 1
                                display_failed_blocks.add(block)
                                display_payloads.pop(block, None)
                                error_text = f"Block {block}: {exc}"
                        if slow_poll_due:
                            try:
                                runtime_24h = self._read_runtime_24h(session, recorded_at)
                                with self.state_lock:
                                    self.runtime_24h = {**runtime_24h, "error": None}
                            except Exception as exc:
                                error_text = error_text or f"24-h-Zähler: {exc}"
                                with self.state_lock:
                                    self.runtime_24h = {
                                        **self.runtime_24h,
                                        "available": False,
                                        "error": str(exc),
                                        "recorded_at": recorded_at,
                                    }
                            ram_telemetry = self._read_ram_telemetry(session, recorded_at)
                            with self.state_lock:
                                self.ram_telemetry = ram_telemetry
                            first_error = next(
                                (item["error"] for item in ram_telemetry.values() if item["error"]),
                                None,
                            )
                            if first_error:
                                error_text = error_text or f"RAM-Telemetrie: {first_error}"
                            ram_telemetry_available = any(
                                item.get("available") for item in ram_telemetry.values()
                            )
                            soot_regeneration, soot_fields = self._read_soot_regeneration_fields(
                                session, recorded_at
                            )
                            with self.state_lock:
                                self.soot_regeneration = soot_regeneration
                            if soot_regeneration["supported"] is True and soot_regeneration["error"]:
                                error_text = error_text or f"Rußfilterregeneration: {soot_regeneration['error']}"
                        display_refreshed: set[int] = set()
                        if slow_poll_due:
                            # Reuse the soot-regeneration read of 0x0db3 instead
                            # of requesting the same byte twice in one cycle.
                            soot_state = soot_fields.get(SOOT_REGENERATION_FIELDS[0]["key"])
                            if soot_state is not None:
                                self._store_display_ram_sample(0x0DB3, soot_state, now_mono)
                                display_refreshed.add(0x0DB3)
                        self._update_display_slow_payloads(
                            display_payloads, display_failed_blocks, now_mono
                        )
                        try:
                            display_status_result = self._refresh_display_status(
                                session,
                                display_payloads,
                                now_mono,
                                recorded_at,
                                slow_poll_due,
                                display_refreshed,
                            )
                        except Exception as exc:
                            display_status_result = display_status_unavailable(
                                error=f"Display-Status: {exc}", recorded_at=recorded_at
                            )
                        try:
                            self.phase6.cycle(
                                session,
                                self.service,
                                cycle_payloads,
                                now_mono,
                                {
                                    "slow": slow_poll_due,
                                    "previous_elapsed": self._previous_cycle.get("elapsed"),
                                    "previous_slow": self._previous_cycle.get("slow"),
                                    "previous_housekeeping": self._previous_cycle.get("housekeeping"),
                                    "previous_serial_elapsed": self._previous_cycle.get("serial_elapsed"),
                                    "display_available": bool(
                                        (display_status_result or {}).get("available")
                                    ),
                                    "ram_telemetry_available": ram_telemetry_available,
                                },
                            )
                        except Exception as exc:
                            error_text = error_text or f"Phase-6-Logger: {exc}"
                # Serial part (lease incl. Phase-6 reads) ends here; the rest
                # of the cycle is local SQLite work.
                serial_done = time.monotonic()
                self.store.record_history_snapshot(recorded_at, rows)
                try:
                    self._observe_email_notifications(recorded_at, display_status_result)
                except Exception as exc:
                    with self.state_lock:
                        self.email_notification_runtime.update({
                            "last_observation_at": recorded_at,
                            "last_observation_error": str(exc)[:1000],
                        })
                if motor_status_observation is not None:
                    self.store.record_motor_status_observation(
                        recorded_at,
                        motor_status_observation,
                    )
                if (
                    motor_status_observation is not None
                    and motor_speed_observation is not None
                ):
                    self.diagnosis_250h_cache.observe_motor(
                        recorded_at,
                        status=motor_status_observation,
                        rpm=motor_speed_observation,
                    )
                with self.state_lock:
                    adaptive_rows = [
                        dict(row)
                        for (block, key), row in self.live_values.items()
                        if block in DEFAULT_MONITOR_BLOCKS and web_monitor_field_visible(block, key)
                    ]
                adaptive_result = self.store.record_adaptive_snapshot(recorded_at, adaptive_rows)
                with self.state_lock:
                    self.monitor_state["adaptive_history"] = adaptive_result
                if rows:
                    self._last_history_sample = now_mono
                if time.monotonic() - self._last_retention_purge >= 3600:
                    housekeeping = True
                    self.store.purge(HISTORY_RETENTION_DAYS)
                    compacted = self.store.compact_adaptive_history()
                    with self.state_lock:
                        self.monitor_state["adaptive_compaction"] = compacted
                    self._last_retention_purge = time.monotonic()
            except Exception as exc:
                error_text = str(exc)
                failed_blocks = len(DEFAULT_FAST_MONITOR_BLOCKS)
            if display_status_result is None:
                display_status_result = display_status_unavailable(
                    error=f"Display-Status: {error_text or 'Zyklus nicht abgeschlossen'}",
                    recorded_at=recorded_at,
                )
            try:
                self._publish_display_status(display_status_result, cycle_started)
            except Exception as exc:
                error_text = error_text or f"Display-Status-Historie: {exc}"
            if motor_status_observation is None:
                try:
                    self.store.record_motor_status_missing(recorded_at)
                except Exception as exc:
                    error_text = error_text or f"Motorstatus-Historie: {exc}"
            with self.state_lock:
                if self.serial_enabled:
                    self.monitor_state.update({
                        "last_cycle": recorded_at,
                        "last_error": error_text,
                        "cycles": int(self.monitor_state["cycles"]) + 1,
                        "ok_blocks": ok_blocks,
                        "failed_blocks": failed_blocks,
                        "polled_blocks": ok_blocks + failed_blocks,
                        "connection_state": "verbunden",
                    })
                else:
                    self.monitor_state.update({
                        "enabled": False,
                        "running": False,
                        "serial_enabled": False,
                        "connection_state": "getrennt",
                    })
            cycle_elapsed = time.monotonic() - cycle_started
            serial_elapsed = None if serial_done is None else serial_done - cycle_started
            self._previous_cycle = {
                "elapsed": cycle_elapsed,
                "slow": slow_poll_due,
                "housekeeping": housekeeping,
                "serial_elapsed": serial_elapsed,
            }
            self._record_cycle_timing(cycle_elapsed, serial_elapsed, slow_poll_due, housekeeping)
            self.stop_event.wait(max(0.0, self.interval - cycle_elapsed))

    def _record_cycle_timing(
        self,
        elapsed: float,
        serial_elapsed: float | None,
        slow: bool,
        housekeeping: bool,
    ) -> None:
        """Expose the split of the last cycle (serial lease vs. local SQLite work)."""
        kind = "housekeeping" if housekeeping else "slow" if slow else "fast"
        with self.state_lock:
            timing = dict(self.monitor_state.get("cycle_timing") or {
                "fast_over_limit": 0, "fast_max_s": None, "fast_max_at": None,
            })
            timing.update({
                "kind": kind,
                "last_s": round(elapsed, 3),
                "serial_s": None if serial_elapsed is None else round(serial_elapsed, 3),
                "store_s": None if serial_elapsed is None else round(elapsed - serial_elapsed, 3),
                "fast_limit_s": SLOW_FAST_CYCLE_SECONDS,
            })
            if kind == "fast":
                if elapsed > SLOW_FAST_CYCLE_SECONDS:
                    timing["fast_over_limit"] = int(timing.get("fast_over_limit") or 0) + 1
                if timing.get("fast_max_s") is None or elapsed > float(timing["fast_max_s"]):
                    timing["fast_max_s"] = round(elapsed, 3)
                    timing["fast_max_at"] = _now()
            self.monitor_state["cycle_timing"] = timing

    def _history_sample_due(self, now_mono: float) -> bool:
        return self.history_interval <= 0 or float(now_mono) - self._last_history_sample >= self.history_interval

    def live(self) -> dict:
        with self.state_lock:
            state = dict(self.monitor_state)
            values = list(self.live_values.values())
            soot_settings = dict(self.soot_filter_config)
            runtime_24h = dict(self.runtime_24h)
            ram_telemetry = [
                dict(self.ram_telemetry.get(field["key"]) or ram_telemetry_unavailable(field))
                for field in RAM_TELEMETRY_FIELDS
            ]
            soot_regeneration = json.loads(json.dumps(self.soot_regeneration))
            clock_cache = self._display_slow_payloads.get(CONTROLLER_CLOCK_BLOCK)
        controller_clock = controller_clock_status(
            clock_cache[0] if clock_cache else None,
            time.monotonic() - clock_cache[1] if clock_cache else None,
            self._display_status_max_age(),
        )
        display_status = self._current_display_status()
        values.sort(key=lambda item: (int(item["block"]), str(item["key"])))
        cache_values = {
            str(item["key"]): item.get("value")
            for item in values
            if int(item["block"]) == 104
        }
        motor_exhaust = next(
            (
                item.get("value")
                for item in values
                if int(item["block"]) == 24 and str(item["key"]) == SOOT_FILTER_SOURCE_KEY
            ),
            None,
        )
        soot_filter = soot_filter_estimate(
            motor_exhaust,
            soot_settings["zero_temperature_c"],
            soot_settings["full_temperature_c"],
        )
        return {
            "monitor": state,
            "values": values,
            "maintenance": maintenance_status(cache_values),
            "soot_filter": soot_filter,
            "operating_hours_per_start": operating_hours_per_start(values),
            "runtime_24h": runtime_24h,
            "ram_telemetry": ram_telemetry,
            "soot_filter_regeneration": soot_regeneration,
            "display_status": display_status,
            "controller_clock": controller_clock,
            "controller_clock_sync": self._controller_clock_sync_summary(),
            "real_runtime": self.real_runtime(),
            "runtime_auto_reset": self._runtime_auto_reset_summary(),
            "phase6_verify": self.phase6.status(),
        }

    def schema(self) -> dict:
        blocks = []
        for block in self.pack.addressable_blocks():
            if block == 18:
                blocks.append({
                    "block": block,
                    "name": self.pack.block_name(block),
                    "fields": [],
                    "special": "message-history",
                })
                continue
            fields = []
            presentation_groups = self.pack.presentation_groups(block)
            presentation_components = {
                component: base
                for base, group in presentation_groups.items()
                for component in group["components"]
            }
            for key, meta in self.pack.field_map(block).items():
                if not web_field_visible(block, key, meta):
                    continue
                presentation_base = presentation_components.get(key)
                if presentation_base:
                    group = presentation_groups[presentation_base]
                    components = group["components"]
                    if key != components[0]:
                        continue
                    meta = dict(meta)
                    meta.update({"type": group["type"], "size": len(components), "components": components})
                    key = presentation_base
                meta = dict(meta)
                meta.update(self.pack.field_ui_metadata(block, key))
                fields.append({
                    "key": key,
                    "label": self.pack.label(key),
                    "type": meta.get("type", "byte"),
                    "unit": meta.get("unit", ""),
                    "size": meta.get("size", 1),
                    "offset": meta.get("offset", 0),
                    "write": generic_web_field_writable(block, meta),
                    "reserved": is_reserved_key(key),
                    "choices": _json_value(meta.get("choices") or []),
                    "min": meta.get("min"),
                    "max": meta.get("max"),
                    "step": meta.get("step"),
                    "help": meta.get("help", ""),
                })
            if fields:
                special = (
                    "run-history" if block in RUN_HISTORY_BLOCKS
                    else "service-history" if block in SERVICE_HISTORY_BLOCKS
                    else "motor-snapshot" if block in MOTOR_SNAPSHOT_BLOCKS
                    else "oil-refill-history" if block == 102
                    else "mc-status" if block in MC_STATUS_BLOCKS
                    else None
                )
                item = {"block": block, "name": self.pack.block_name(block), "fields": fields}
                if special:
                    item["special"] = special
                blocks.append(item)
        series = []
        for series_id, title, block, key, unit, color in DEFAULT_DASHBOARD_SERIES:
            if key in self.pack.field_map(block) or key == "Hka_Mw1.Temp.DachsAustritt":
                series.append({"id": series_id, "title": title, "block": block, "key": key, "unit": unit, "color": color})
        service_catalog = self.pack.service_catalog(limit=1)
        return {
            "version": "dachs-msr2-web/v1",
            "pack_rev": self.pack.pack_rev,
            "monitor_blocks": list(DEFAULT_MONITOR_BLOCKS),
            "fast_monitor_blocks": list(DEFAULT_FAST_MONITOR_BLOCKS),
            "slow_monitor_blocks": list(DEFAULT_SLOW_MONITOR_BLOCKS),
            "slow_interval_seconds": self.slow_monitor_interval,
            "history_interval_seconds": self.history_interval,
            "history_retention_days": HISTORY_RETENTION_DAYS,
            "dashboard": {
                "max_cards": MAX_DASHBOARD_CARDS,
                "max_extra_blocks": MAX_DASHBOARD_EXTRA_BLOCKS,
                "derived_fields": self._derived_dashboard_fields(),
            },
            "service_catalog": {
                "available": bool(service_catalog["available"]),
                "count": int(service_catalog["count"]),
                "schema": service_catalog["schema"],
                "details_available": bool(service_catalog["details_available"]),
            },
            "blocks": blocks,
            "network_protection": network_protection_schemas(),
            "series": series,
            "derived_series": [dict(HISTORY_REAL_RUNTIME_SERIES)],
            "roles": {"guest": "lesen", "admin": "lesen und schreiben"},
        }

    def api_catalog(self) -> dict:
        schema = self.schema()
        fields = []
        for block in schema["blocks"]:
            for field in block.get("fields", []):
                fields.append({
                    **field,
                    "block": int(block["block"]),
                    "block_name": str(block["name"]),
                    "cpu": 0,
                })
        for target in schema.get("network_protection", []):
            for field in target.get("fields", []):
                fields.append({
                    **field,
                    "block": int(target["block"]),
                    "block_name": str(target["name"]),
                    "cpu": int(target["cpu"]),
                })
        return {
            "schema": "open-dachs-api-catalog/v1",
            "pack_rev": schema["pack_rev"],
            "fields": fields,
            "adaptive_series": self.store.adaptive_catalog(),
        }

    def _resolve_api_field(self, block_value: object, key_value: object) -> tuple[int, str]:
        key = str(key_value or "").strip()
        if not key:
            raise ValueError("key ist erforderlich")
        if block_value not in (None, ""):
            block = validate_block(int(block_value), writable=True)
            if key not in self.pack.field_map(block) and key not in self.pack.presentation_groups(block):
                raise KeyError(f"Feld {key!r} ist in Block {block} nicht vorhanden")
            return block, key
        matches = [
            block for block in self.pack.addressable_blocks()
            if key in self.pack.field_map(block) or key in self.pack.presentation_groups(block)
        ]
        if not matches:
            raise KeyError(f"Feld {key!r} wurde nicht gefunden")
        if len(matches) != 1:
            raise ValueError(f"Feld {key!r} ist nicht eindeutig; block muss angegeben werden")
        return int(matches[0]), key

    def _validate_api_value_offline(
        self,
        block: int,
        cpu: int,
        key: str,
        value: object,
    ) -> None:
        """Reject malformed values before reserving an idempotency key."""
        if cpu:
            probe = bytearray(network_protection_payload_length(block))
            encode_network_protection_value(probe, cpu, key, value, block=block)
            return
        fields = self.pack.field_map(block)
        payload_size = max(
            70,
            max(
                (int(metadata["offset"]) + int(metadata["size"]) for metadata in fields.values()),
                default=0,
            ),
        )
        probe = bytearray(payload_size)
        self.pack.encode_value(probe, key, str(value), block=block)

    def _validate_power_target_changes(
        self,
        session,
        block: int,
        changes: list[dict],
        encoded_payload: bytes,
    ) -> None:
        if int(block) != POWER_TARGET_BLOCK:
            return
        if not any(str(change.get("key", "")) == POWER_TARGET_KEY for change in changes):
            return
        target_field = next(
            (
                field for field in self.pack.decode(POWER_TARGET_BLOCK, encoded_payload)
                if field.key == POWER_TARGET_KEY
            ),
            None,
        )
        if target_field is None:
            raise RuntimeError("Generatornennleistung konnte nach dem Encoding nicht dekodiert werden")
        target_kw = float(target_field.value)

        fuel_result = self.service.read_block(session, POWER_TARGET_FUEL_BLOCK)
        if not fuel_result.ok:
            raise RuntimeError(
                f"Block {POWER_TARGET_FUEL_BLOCK} für die Kraftstoffprüfung konnte nicht gelesen werden"
            )
        fuel_field = next(
            (
                field for field in self.pack.decode(POWER_TARGET_FUEL_BLOCK, fuel_result.payload)
                if field.key == POWER_TARGET_FUEL_KEY
            ),
            None,
        )
        if fuel_field is None:
            raise RuntimeError("Kraftstofftyp konnte nicht dekodiert werden")
        fuel_raw = int(fuel_field.raw)
        limits = POWER_TARGET_LIMITS_KW.get(fuel_raw)
        if limits is None:
            return
        minimum, maximum = limits
        if target_kw < minimum or target_kw > maximum:
            raise PowerTargetRangeError(
                f"Generatornennleistung {target_kw:g} kW liegt außerhalb des dokumentierten "
                f"Bereichs {minimum:g}–{maximum:g} kW für {fuel_field.value}"
            )

    def execute_api_set_value(self, principal: dict, payload: dict) -> dict:
        request_id = str(payload.get("request_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", request_id):
            raise ValueError("request_id benötigt 1–100 Zeichen aus Buchstaben, Ziffern, . _ : oder -")
        with self.state_lock:
            settings = dict(self.api_config)
        if not settings.get("write_enabled"):
            raise PermissionError("API-Schreibaktionen sind in den Systemeinstellungen deaktiviert")
        if "value" not in payload:
            raise ValueError("value ist erforderlich")
        cpu = payload.get("cpu", 0)
        if type(cpu) is not int or cpu not in (0, *NETWORK_PROTECTION_CPUS):
            raise ValueError("cpu muss die ganze Zahl 0, 1 oder 2 sein")
        key = str(payload.get("key", "")).strip()
        if cpu:
            cpu = validate_network_cpu(cpu)
            block = validate_network_block(
                payload.get("block", NETWORK_PROTECTION_BLOCK)
            )
            target_schema = network_protection_schema(cpu, block)
            if not target_schema["writable"]:
                raise PermissionError(
                    f"Netzschutz CPU {cpu}, Block {block} ist nur lesbar"
                )
            if key not in {item["key"] for item in target_schema["fields"]}:
                raise KeyError(
                    f"Netzschutzfeld {key!r} ist für CPU {cpu}, Block {block} nicht vorhanden"
                )
        else:
            block, key = self._resolve_api_field(payload.get("block"), key)
        self._validate_api_value_offline(block, cpu, key, payload.get("value"))
        request_hash = _api_request_fingerprint({
            "action": "set-value",
            "block": block,
            "cpu": cpu,
            "key": key,
            "value": payload.get("value"),
        })
        token_id = int(principal["id"])
        cached = self.store.reserve_api_request(token_id, request_id, request_hash)
        if cached is not None:
            return {**cached, "replayed": True}
        try:
            if cpu:
                audit = self.write_network_protection(
                    f"api:{principal['name']}",
                    cpu,
                    [{"key": key, "value": payload.get("value")}],
                    int(settings.get("auth_level", API_DEFAULT_AUTH_LEVEL)),
                    "",
                    True,
                    block=block,
                )
            else:
                audit = self.write_block(
                    f"api:{principal['name']}",
                    block,
                    [{"key": key, "value": payload.get("value")}],
                    int(settings.get("auth_level", API_DEFAULT_AUTH_LEVEL)),
                    "",
                    True,
                )
        except Exception:
            self.store.cancel_api_request(token_id, request_id, request_hash)
            raise
        response = {
            # A byte-identical live request is a successful no-op: the write
            # path deliberately emits neither authentication nor a controller
            # write, but confirms the already matching full-block state.
            # Failed ACK/readback verification always carries an error and is
            # therefore never reported as successful.
            "ok": bool(
                audit.get("readback_ok") is True
                and not audit.get("error")
            ),
            "action": "set-value",
            "request_id": request_id,
            "replayed": False,
            "block": block,
            "cpu": cpu,
            "key": key,
            "value": payload.get("value"),
            "audit": audit,
        }
        self.store.complete_api_request(token_id, request_id, request_hash, response)
        return response

    def _normalize_backup_blocks(
        self,
        value: object,
        *,
        available: set[tuple[int, int]] | None = None,
    ) -> list[tuple[int, int]]:
        """Validate an ordered target selection without silent truncation.

        Plain integers retain the original API meaning of regulator CPU 0.
        Target objects may additionally select reviewed block 16, 20 or 21 on
        network-monitor CPU 1 or 2. Restore callers pass a narrower available
        set, so capture-only targets fail before any serial lease.
        """
        if not isinstance(value, list):
            raise ValueError("Blöcke müssen als Liste übergeben werden")
        if not value:
            raise ValueError("Mindestens ein Block muss ausgewählt werden")
        mapped = {
            int(block)
            for block in self.pack.addressable_blocks()
            if 0 <= int(block) < 255
        }
        selectable = {(0, block) for block in mapped}
        selectable.update(
            (cpu, block)
            for cpu in NETWORK_PROTECTION_CPUS
            for block in NETWORK_PROTECTION_BACKUP_BLOCKS
        )
        allowed = selectable if available is None else selectable & set(available)
        selected: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for item in value:
            if isinstance(item, bool):
                raise ValueError("CPU und Blocknummern müssen ganze Zahlen sein")
            if isinstance(item, int):
                cpu = 0
                raw_block = item
            elif isinstance(item, dict):
                raw_cpu = item.get("cpu")
                raw_block = item.get("block")
                if isinstance(raw_cpu, bool) or not isinstance(raw_cpu, int):
                    raise ValueError("CPU und Blocknummern müssen ganze Zahlen sein")
                if isinstance(raw_block, bool) or not isinstance(raw_block, int):
                    raise ValueError("CPU und Blocknummern müssen ganze Zahlen sein")
                cpu = raw_cpu
            else:
                raise ValueError("Blocknummern müssen ganze Zahlen oder Zielobjekte sein")
            try:
                block = validate_block(raw_block, writable=True)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Ungültige Blocknummer: {raw_block!r}") from exc
            target = (cpu, block)
            if target not in selectable:
                if cpu == 0:
                    raise ValueError(
                        f"Block {block} ist im aktuellen Pack nicht wiederherstellbar"
                    )
                if cpu not in NETWORK_PROTECTION_CPUS:
                    raise ValueError("CPU muss 0, 1 oder 2 sein")
                raise ValueError(
                    f"Netzschutz CPU {cpu} unterstützt im Backup nur Block "
                    f"{', '.join(str(item) for item in NETWORK_PROTECTION_BACKUP_BLOCKS)}"
                )
            if target not in allowed:
                raise ValueError(
                    f"CPU {cpu}, Block {block} ist im Sicherungsabbild nicht wiederherstellbar"
                )
            if target in seen:
                raise ValueError(f"CPU {cpu}, Block {block} wurde mehrfach ausgewählt")
            selected.append(target)
            seen.add(target)
        return selected

    def _backup_target_name(self, cpu: int, block: int) -> str:
        if cpu:
            return network_protection_name(cpu, block)
        return self.pack.block_name(block)

    @staticmethod
    def _backup_target_object(target: tuple[int, int]) -> dict:
        cpu, block = target
        return {"cpu": cpu, "block": block}

    def _public_backup_inspection(self, inspection: dict) -> dict:
        records = list(inspection.get("records") or inspection.get("blocks") or [])
        blocks = []
        for record in records:
            cpu = int(record.get("cpu", 0))
            block = int(record["block"])
            blocks.append({
                "cpu": cpu,
                "block": block,
                "target_key": f"{cpu}:{block}",
                "name": str(
                    record.get("block_name")
                    or record.get("name")
                    or self._backup_target_name(cpu, block)
                ),
                "ok": bool(record.get("ok")),
                "restorable": bool(record.get("restorable")),
                "payload_len": int(record.get("payload_len") or 0),
                "payload_sha256": record.get("payload_sha256"),
                "error": record.get("error"),
                "critical": bool(record.get("critical") or cpu),
            })
        pack = dict(inspection.get("pack") or {})
        controller = dict(inspection.get("controller") or {})
        digest_present = bool(inspection.get("digest_present"))
        digest_verified = bool(inspection.get("digest_verified"))
        pack_revision = str(pack.get("revision") or inspection.get("pack_rev") or "")
        inspected_pack_compatible = inspection.get("pack_compatible")
        # Live restore is fail-closed: missing pack metadata is not evidence
        # of compatibility, even when an older image has no revision string.
        same_pack = inspected_pack_compatible is True
        controller_identified = bool(
            controller.get("available") is True and controller.get("serial_number")
        )
        return {
            "schema": "open-dachs-manager-backup-inspection/v1",
            "backup_schema": inspection.get("schema"),
            "schema_version": inspection.get("schema_version"),
            "image_sha256": inspection.get("image_sha256"),
            "created_utc": inspection.get("created_utc"),
            "pack_rev": pack_revision or None,
            "controller": {
                "available": controller_identified,
                "serial_number": controller.get("serial_number") if controller_identified else None,
                "operating_hours": controller.get("operating_hours") if controller_identified else None,
                "error": controller.get("error") if not controller_identified else None,
            },
            "requested_blocks": int(inspection.get("requested_blocks") or len(records)),
            "requested_targets": [
                {"cpu": item["cpu"], "block": item["block"]} for item in blocks
            ],
            "successful_blocks": int(
                inspection.get("successful_blocks")
                if inspection.get("successful_blocks") is not None
                else sum(bool(record.get("ok")) for record in records)
            ),
            "failed_blocks": int(
                inspection.get("failed_blocks")
                if inspection.get("failed_blocks") is not None
                else sum(not bool(record.get("ok")) for record in records)
            ),
            "restorable_blocks": int(
                inspection.get("restorable_blocks")
                if inspection.get("restorable_blocks") is not None
                else sum(bool(record.get("restorable")) for record in records)
            ),
            "integrity": "verified" if digest_present and digest_verified else "legacy-unverified",
            "digest_present": digest_present,
            "digest_verified": digest_verified,
            "pack_compatible": same_pack,
            "live_restore_compatible": bool(
                same_pack and digest_present and digest_verified and controller_identified
            ),
            "blocks": blocks,
        }

    def create_backup(self, blocks: object) -> dict:
        """Read selected regulator and network-protection targets in one lease."""
        selected = self._normalize_backup_blocks(blocks)
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        with self.serial_lock:
            with self.state_lock:
                if not self.serial_enabled:
                    raise TransportError("serielle Verbindung ist getrennt")
            with self.service.session() as session:
                image = self.service.backup(
                    session,
                    [self._backup_target_object(target) for target in selected],
                    decode=True,
                    include_identity=True,
                )
        inspection = self._public_backup_inspection(self.service.inspect_backup(image))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return {
            "schema": "open-dachs-manager-backup-response/v1",
            "ok": inspection["failed_blocks"] == 0,
            "filename": f"open-dachs-backup-{stamp}.json",
            "image": image,
            "inspection": inspection,
            "summary": {
                "requested_blocks": inspection["requested_blocks"],
                "successful_blocks": inspection["successful_blocks"],
                "failed_blocks": inspection["failed_blocks"],
            },
        }

    def inspect_backup(self, image: object) -> dict:
        """Validate a browser-loaded image without touching the controller."""
        return self._public_backup_inspection(self.service.inspect_backup(image))

    def _persist_restore_preimage(self, image: dict) -> dict:
        """Store a SHA-256-bound current-state image before the first write."""
        inspection = self.service.inspect_backup(image)
        digest = str(inspection.get("image_sha256") or "")
        if not digest:
            raise ValueError("Sicherheitsabbild besitzt keine Prüfsumme")
        directory = self.data_dir / "restore-preimages"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(OSError):
            directory.chmod(0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        filename = f"restore-preimage-{stamp}-{digest[:12]}.json"
        destination = directory / filename
        write_json_atomic(destination, image)
        with suppress(OSError):
            destination.chmod(0o600)
        return {
            "filename": filename,
            "image_sha256": digest,
            "blocks": int(inspection.get("successful_blocks") or 0),
        }

    def _restore_target_preserving_device_fields(
        self,
        cpu: int,
        block: int,
        before: bytes,
        target: bytes,
    ) -> tuple[bytes, list[str]]:
        """Keep firmware/version identity bytes owned by the destination PCB.

        Senertec's original ``Platinentausch`` workflow copies matching data-map
        values but explicitly skips ``Hka_Bd_Stat.bSoftwareVersion*`` and
        ``Rvc_SwVersion*``.  Raw backup images still retain those bytes as
        diagnostic source metadata; restore must not transfer them to another
        controller and thereby misreport its actual firmware generation.
        """
        current = bytes(before)
        restored = bytearray(target)
        if cpu != 0 or len(current) != len(restored):
            return bytes(restored), []
        preserved: set[str] = set()
        for key, metadata in self.pack.field_map(block).items():
            base_key = str(metadata.get("base_key") or key)
            if not base_key.startswith(BACKUP_RESTORE_PRESERVED_PREFIXES):
                continue
            offset = int(metadata["offset"])
            size = int(metadata["size"])
            if offset < 0 or size < 1 or offset + size > len(restored):
                raise ValueError(
                    f"Restore-Schutzfeld {key!r} liegt außerhalb von Block {block}"
                )
            if restored[offset:offset + size] != current[offset:offset + size]:
                preserved.add(base_key)
            restored[offset:offset + size] = current[offset:offset + size]
        firmware_rebased = restore_target_preserving_firmware_owned(
            cpu, block, bytes(restored), current
        )
        if firmware_rebased != bytes(restored):
            if block == SC123_LIMIT_CAPABILITY_BLOCK:
                preserved.add("Hka_Ew.Reserve_2/SC123-Heartbeat")
            elif block == SC123_LIMIT_STORAGE_BLOCK:
                preserved.add("Waermef_Ew.Reserve[0..5]/SC123-Datensatz")
        return firmware_rebased, sorted(preserved)

    def restore_backup(
        self,
        username: str,
        image: object,
        image_sha256: object,
        blocks: object,
        auth_level: int,
        pass4: str,
        write_enabled: bool,
        confirmation: str,
    ) -> dict:
        """Plan or execute an exact multi-block restore in one worker lease.

        Every selected block is read and length-checked before authentication.
        Live restore authenticates once, preserves destination-owned firmware
        version fields, skips identical payloads without a wire write and
        attempts every selected target.  Per-target failures are collected;
        successful blocks are deliberately not rolled back.
        """
        if not isinstance(write_enabled, bool):
            raise ValueError("write_enabled muss true oder false sein")
        if isinstance(auth_level, bool) or not isinstance(auth_level, int):
            raise ValueError("Auth-Level muss eine ganze Zahl sein")
        raw_inspection = self.service.inspect_backup(image)
        inspection = self._public_backup_inspection(raw_inspection)
        calculated_hash = str(inspection.get("image_sha256") or "")
        supplied_hash = str(image_sha256 or "")
        if supplied_hash and calculated_hash and not hmac.compare_digest(
            supplied_hash, calculated_hash
        ):
            raise ValueError("Das Sicherungsabbild wurde nach der Prüfung verändert")
        record_list = list(raw_inspection.get("records") or raw_inspection.get("blocks") or [])
        records = {
            (int(record.get("cpu", 0)), int(record["block"])): record
            for record in record_list
            if bool(record.get("restorable"))
        }
        selected = self._normalize_backup_blocks(blocks, available=set(records))
        if write_enabled:
            auth_level = _validated_auth_level(
                auth_level, "Auth-Level für Hardware-Wiederherstellung"
            )
            if str(confirmation or "").strip() != BACKUP_RESTORE_CONFIRMATION:
                raise ValueError(
                    f"Bestätigung muss exakt {BACKUP_RESTORE_CONFIRMATION!r} lauten"
                )
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")

        preflight: dict[tuple[int, int], bytes] = {}
        targets: dict[tuple[int, int], bytes] = {}
        preserved_fields: dict[tuple[int, int], list[str]] = {}
        auth = None
        preimage_metadata = None
        results: list[dict] = []
        with self.serial_lock:
            with self.state_lock:
                if not self.serial_enabled:
                    raise TransportError("serielle Verbindung ist getrennt")
            with self.service.session() as session:
                for target_key in selected:
                    cpu, block = target_key
                    record = records[target_key]
                    target = bytes.fromhex(str(record.get("payload_hex") or ""))
                    current = self.service.read_block(session, block, cpu=cpu)
                    if not current.ok:
                        raise RuntimeError(
                            f"Restore-Vorprüfung: CPU {cpu}, Block {block} konnte nicht gelesen werden"
                        )
                    before = bytes(current.payload)
                    if len(before) != len(target):
                        raise ValueError(
                            f"Restore-Vorprüfung: CPU {cpu}, Block {block} hat am Regler {len(before)} Byte, "
                            f"im Abbild aber {len(target)} Byte"
                        )
                    if cpu:
                        try:
                            decode_network_protection(cpu, before)
                        except ValueError as exc:
                            raise ValueError(
                                f"Restore-Vorprüfung: Netzschutz CPU {cpu}, Block {block} ist ungültig: {exc}"
                            ) from exc
                    preflight[target_key] = before
                    target, preserved = self._restore_target_preserving_device_fields(
                        cpu, block, before, target
                    )
                    targets[target_key] = target
                    preserved_fields[target_key] = preserved

                differing = [
                    target for target in selected
                    if preflight[target] != targets[target]
                ]
                if write_enabled and differing:
                    if not re.fullmatch(r"[0-9a-f]{64}", supplied_hash):
                        raise ValueError(
                            "Live-Wiederherstellung benötigt den Hash der geprüften Datei"
                        )
                    if not calculated_hash or not hmac.compare_digest(
                        supplied_hash, calculated_hash
                    ):
                        raise ValueError("Das Sicherungsabbild wurde nach der Prüfung verändert")
                    controller = dict(raw_inspection.get("controller") or {})
                    if (
                        controller.get("available") is not True
                        or not str(controller.get("serial_number") or "").strip()
                    ):
                        raise ValueError(
                            "Live-Wiederherstellung benötigt ein neues Abbild mit Gerätekennung"
                        )
                    if not inspection.get("live_restore_compatible"):
                        raise ValueError(
                            "Live-Wiederherstellung ist wegen Packstand oder fehlender Prüfsumme gesperrt"
                        )
                    # Create a fresh, SHA-256-bound recovery image inside the same
                    # exclusive session. Its payloads become the authoritative
                    # CAS baseline and are persisted before authentication or
                    # the first wire-write.
                    preimage = self.service.backup(
                        session,
                        [self._backup_target_object(target) for target in selected],
                        decode=False,
                        include_identity=True,
                    )
                    preimage_inspection = self.service.inspect_backup(preimage)
                    if int(preimage_inspection.get("successful_blocks") or 0) != len(selected):
                        raise RuntimeError(
                            "Restore-Vorprüfung: Sicherheitsabbild konnte nicht vollständig gelesen werden"
                        )
                    preimage_records = {
                        (int(record.get("cpu", 0)), int(record["block"])): record
                        for record in preimage_inspection.get("records") or []
                        if bool(record.get("restorable"))
                    }
                    for target_key in selected:
                        cpu, block = target_key
                        before = bytes.fromhex(
                            str(preimage_records[target_key]["payload_hex"])
                        )
                        if len(before) != len(targets[target_key]):
                            raise ValueError(
                                f"Restore-Vorprüfung: CPU {cpu}, Block {block} hat am Regler {len(before)} Byte, "
                                f"im Abbild aber {len(targets[target_key])} Byte"
                            )
                        preflight[target_key] = before
                        target, preserved = self._restore_target_preserving_device_fields(
                            cpu, block, before, targets[target_key]
                        )
                        targets[target_key] = target
                        preserved_fields[target_key] = sorted(
                            set(preserved_fields[target_key]) | set(preserved)
                        )
                    differing = [
                        target for target in selected
                        if preflight[target] != targets[target]
                    ]
                    if differing:
                        preimage_metadata = self._persist_restore_preimage(preimage)

                    current_controller = dict(preimage_inspection.get("controller") or {})
                    if (
                        current_controller.get("available") is not True
                        or not str(current_controller.get("serial_number") or "").strip()
                    ):
                        raise RuntimeError(
                            "Restore-Vorprüfung: aktuelle Reglerkennung konnte nicht gelesen werden"
                        )
                    if differing:
                        auth = self.service.authenticate(session, int(auth_level), pass4 or None)
                        if not auth.ok:
                            raise PermissionError(f"Auth-Level {auth_level} wurde nicht gewährt")

                # Blocks 20 and 22 provide the current device inputs for PW4.
                # Keep them until the end so a multi-block restore cannot
                # invalidate the authentication basis before later writes.
                auth_targets = {(0, 20), (0, 22)}
                execution_order = [
                    target for target in selected if target not in auth_targets
                ]
                execution_order.extend(
                    target for target in selected if target in auth_targets
                )
                for target_key in execution_order:
                    cpu, block = target_key
                    changed = preflight[target_key] != targets[target_key]
                    changed_bytes = sum(
                        left != right
                        for left, right in zip(
                            preflight[target_key], targets[target_key]
                        )
                    )
                    restore_arguments = (
                        session,
                        block,
                        preflight[target_key],
                        targets[target_key],
                    )
                    if cpu:
                        audit = self.service.restore_payload(
                            *restore_arguments,
                            dry_run=not write_enabled,
                            cpu=cpu,
                        )
                    else:
                        # Retain compatibility with services/fakes that
                        # implement the pre-target optional-CPU signature.
                        audit = self.service.restore_payload(
                            *restore_arguments,
                            dry_run=not write_enabled,
                        )
                    audit_payload = audit.as_dict()
                    audit_payload.update({
                        "operation": "backup-restore",
                        "image_sha256": calculated_hash,
                        "cpu": cpu,
                        "target_key": f"{cpu}:{block}",
                        "block_name": self._backup_target_name(cpu, block),
                        "critical": bool(cpu),
                        "changed_bytes": changed_bytes,
                        "preserved_fields": preserved_fields[target_key],
                        "auth_level_requested": int(auth_level),
                        "auth_level_granted": getattr(auth, "granted_level", None),
                        "preimage": preimage_metadata,
                    })
                    self.store.audit(_now(), username, block, audit_payload)
                    if audit_payload.get("error"):
                        status = "failed"
                    elif not changed:
                        status = "unchanged"
                    elif not write_enabled:
                        status = "planned"
                    else:
                        status = "restored"
                    results.append({
                        "cpu": cpu,
                        "block": block,
                        "target_key": f"{cpu}:{block}",
                        "name": self._backup_target_name(cpu, block),
                        "critical": bool(cpu),
                        "status": status,
                        "changed": changed,
                        "changed_bytes": changed_bytes,
                        "preserved_fields": preserved_fields[target_key],
                        "dry_run": bool(audit_payload.get("dry_run")),
                        "written": bool(audit_payload.get("written")),
                        "write_attempted": bool(audit_payload.get("write_attempted")),
                        "ack_positive": audit_payload.get("ack_positive"),
                        "readback_ok": audit_payload.get("readback_ok"),
                        "readback_scope": audit_payload.get("readback_scope"),
                        "error": audit_payload.get("error"),
                    })

        failed = sum(item["status"] == "failed" for item in results)
        summary = {
            "requested": len(selected),
            "planned": sum(item["status"] == "planned" for item in results),
            "restored": sum(item["status"] == "restored" for item in results),
            "unchanged": sum(item["status"] == "unchanged" for item in results),
            "failed": failed,
            "not_attempted": sum(item["status"] == "not-attempted" for item in results),
            "write_attempted": sum(bool(item.get("write_attempted")) for item in results),
            "uncertain": sum(
                bool(item.get("write_attempted"))
                and not (bool(item.get("written")) and item.get("readback_ok") is True)
                for item in results
            ),
        }
        return {
            "schema": "open-dachs-manager-restore-result/v1",
            "ok": failed == 0,
            "mode": "live" if write_enabled else "dry-run",
            "dry_run": not write_enabled,
            "image_sha256": calculated_hash,
            "requested_blocks": len(selected),
            "requested_targets": [
                self._backup_target_object(target) for target in selected
            ],
            "differing_blocks": sum(
                preflight[target] != targets[target] for target in selected
            ),
            "written_blocks": sum(bool(item.get("written")) for item in results),
            "write_attempted_blocks": summary["write_attempted"],
            "uncertain_blocks": summary["uncertain"],
            "unchanged_blocks": summary["unchanged"],
            "failed_blocks": failed,
            "auth_level_requested": int(auth_level),
            "auth_level_granted": getattr(auth, "granted_level", None),
            "stopped_at_block": None,
            "stopped_at_target": None,
            "preimage": preimage_metadata,
            "results": results,
            "summary": summary,
        }

    def read_block(self, block: int) -> dict:
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        block = validate_block(block)
        run_payloads: dict[int, bytes] = {}
        service_payloads: dict[int, bytes] = {}
        with self.serial_lock, self.service.session() as session:
            if block in RUN_HISTORY_BLOCKS:
                selected = None
                for source_block in RUN_HISTORY_BLOCKS:
                    source_result, source_fields = self.service.decoded_block(session, source_block)
                    if not source_result.ok:
                        raise RuntimeError(f"Laufhistorie: Block {source_block} konnte nicht gelesen werden")
                    run_payloads[source_block] = source_result.payload
                    if source_block == block:
                        selected = (source_result, source_fields)
                if selected is None:
                    raise RuntimeError(f"Block {block} fehlt in der Laufhistorie")
                result, fields = selected
            elif block in SERVICE_HISTORY_BLOCKS or block in MOTOR_SNAPSHOT_BLOCKS:
                selected = None
                source_blocks = list(SERVICE_HISTORY_BLOCKS)
                if block in MOTOR_SNAPSHOT_BLOCKS:
                    source_blocks.insert(0, block)
                for source_block in source_blocks:
                    source_result, source_fields = self.service.decoded_block(session, source_block)
                    if not source_result.ok:
                        raise RuntimeError(f"Serviceauswertung: Block {source_block} konnte nicht gelesen werden")
                    if source_block in SERVICE_HISTORY_BLOCKS:
                        service_payloads[source_block] = source_result.payload
                    if source_block == block:
                        selected = (source_result, source_fields)
                if selected is None:
                    raise RuntimeError(f"Block {block} fehlt in der Serviceauswertung")
                result, fields = selected
            else:
                result, fields = self.service.decoded_block(session, block)
        history = self.pack.meldehist(result.payload) if block == 18 and result.ok else None
        oil_refill_history = self.pack.oil_refill_history(result.payload) if block == 102 and result.ok else None
        run_history = self.pack.run_history(run_payloads) if run_payloads else None
        service_history = self.pack.service_history(service_payloads) if service_payloads else None
        motor_snapshot = (
            self.pack.motor_snapshot(block, result.payload, service_history)
            if block in MOTOR_SNAPSHOT_BLOCKS and result.ok
            else None
        )
        mc_status = self.pack.mc_status(result.payload) if block in MC_STATUS_BLOCKS and result.ok else None
        return {
            "block": block,
            "name": self.pack.block_name(block),
            "ok": result.ok,
            "status": result.status,
            "payload_hex": result.payload.hex(" ").upper(),
            "rtt_ms": round(result.response.elapsed_ms, 1),
            "history": history,
            "oil_refill_history": oil_refill_history,
            "run_history": run_history,
            "service_history": service_history,
            "motor_snapshot": motor_snapshot,
            "mc_status": mc_status,
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "raw": _json_value(f.raw),
                    "value": _json_value(f.value),
                    "edit_value": _json_value(_field_edit_value(f)),
                    "unit": f.unit or "",
                    "type": f.metadata.get("type", "byte"),
                    "size": f.metadata.get("size", 1),
                    "offset": f.metadata.get("offset", 0),
                    "write": generic_web_field_writable(block, f.metadata),
                    "reserved": is_reserved_key(f.key),
                    "choices": _json_value(f.metadata.get("choices") or []),
                    "min": f.metadata.get("min"),
                    "max": f.metadata.get("max"),
                    "step": f.metadata.get("step"),
                    "help": f.metadata.get("help", ""),
                }
                for f in fields
                if web_field_visible(block, f.key, f.metadata)
            ],
        }

    def read_network_protection(
        self,
        cpu: int,
        block: int = NETWORK_PROTECTION_BLOCK,
    ) -> dict:
        """Read one reviewed block from network-monitor CPU 1 or 2."""
        cpu = validate_network_cpu(cpu)
        block = validate_network_block(block)
        target_schema = network_protection_schema(cpu, block)
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        with self.serial_lock, self.service.session() as session:
            result = self.service.read_block(session, block, cpu=cpu)
        if not result.ok:
            raise RuntimeError(
                f"Netzschutz CPU {cpu}, Block {block} konnte nicht gelesen werden"
            )
        fields = decode_network_protection(cpu, result.payload, block=block)
        return {
            "cpu": cpu,
            "block": block,
            "target_key": f"{cpu}:{block}",
            "name": network_protection_name(cpu, block),
            "ok": True,
            "status": result.status,
            "payload_hex": result.payload.hex(" ").upper(),
            "payload_len": len(result.payload),
            "rtt_ms": round(result.response.elapsed_ms, 1),
            "critical": True,
            "writable": target_schema["writable"],
            "live_values": target_schema["live_values"],
            "read_only_reason": target_schema["read_only_reason"],
            "fields": fields,
        }

    @staticmethod
    def _report_field(field) -> dict:
        return {
            "key": field.key,
            "label": field.label,
            "raw": _json_value(field.raw),
            "value": _json_value(field.value),
            "edit_value": _json_value(_field_edit_value(field)),
            "unit": field.unit or "",
            "reserved": is_reserved_key(field.key),
        }

    def maintenance_reports(self) -> dict:
        items = self.store.maintenance_reports()
        archives = {
            int(item["maintenance_report_id"]): item
            for item in self.store.backup_archives(1000)
            if item.get("maintenance_report_id") is not None
        }
        for item in items:
            item["backup_archive"] = archives.get(int(item["id"]))
        return {"items": items, "status": self.live()["maintenance"]}

    def backup_archives(self) -> dict:
        for item in self.store.backup_archives(1000):
            try:
                self._load_verified_backup_archive(int(item["id"]))
            except Exception:
                pass
        return {
            "schema": "open-dachs-manager-backup-archive/v1",
            "items": self.store.backup_archives(1000),
            "errors": list(self.backup_archive_errors),
        }

    def backup_archive_download(self, archive_id: int) -> tuple[bytes, str]:
        archive, raw, _image, _inspection = self._load_verified_backup_archive(
            int(archive_id)
        )
        return raw, str(archive["filename"])

    def maintenance_report(self, report_id: int) -> dict:
        item = self.store.maintenance_report(report_id)
        try:
            archive = self.store.backup_archive_for_report(report_id)
            try:
                archive, _raw, _image, _inspection = self._load_verified_backup_archive(
                    int(archive["id"])
                )
            except Exception:
                archive = self.store.backup_archive(int(archive["id"]))
            item["backup_archive"] = archive
            item["report"]["backup_archive"] = archive
        except KeyError:
            item["backup_archive"] = None
            item["report"]["backup_archive"] = None
        settings_reader = getattr(self, "maintenance_settings", None)
        live_writes_enabled = (
            settings_reader()["maintenance_live_writes_enabled"]
            if callable(settings_reader)
            else bool(getattr(self, "maintenance_live_writes_enabled", False))
        )
        previous = next(
            (candidate for candidate in self.store.maintenance_reports(200) if int(candidate["id"]) < int(report_id)),
            None,
        )
        if previous:
            previous_full = self.store.maintenance_report(previous["id"])
            item["comparison"] = {
                "report_id": previous["id"],
                "created_at": previous["created_at"],
                **report_comparison(item["report"], previous_full["report"]),
            }
        else:
            item["comparison"] = None
        item.update({
            "checklist_definition": checklist_definition(item["protocol"].get("fuel_type", "gas")),
            "checklist_status": list(CHECKLIST_STATUS),
            "supplemental_definition": supplemental_definition(),
            "supplemental_status": list(SUPPLEMENTAL_STATUS),
            "measurement_definition": list(MAINTENANCE_MEASUREMENTS),
            "maintenance_live_writes_enabled": live_writes_enabled,
            "confirmation_text": (
                MAINTENANCE_CONFIRMATION
                if live_writes_enabled
                else MAINTENANCE_DEMO_CONFIRMATION
            ),
        })
        return item

    def create_maintenance_report(self, username: str) -> dict:
        """Create one report and immutable 42/42 image from the same capture."""
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        started_at = _now()
        snapshots: dict[str, dict] = {}
        payloads: dict[int, bytes] = {}
        failed_blocks: list[dict] = []
        with self.serial_lock:
            with self.state_lock:
                if not self.serial_enabled:
                    raise TransportError("serielle Verbindung ist getrennt")
            with self.service.session() as session:
                image, capture = self.service.maintenance_backup(
                    session, decode=True, created_by=username
                )
        completed_at = _now()
        for cpu, block in BACKUP_TARGETS:
            target = capture[(cpu, block)]
            key = str(block) if cpu == 0 else f"{cpu}:{block}"
            payload = bytes(target.get("payload") or b"")
            if cpu == 0 and target.get("ok"):
                payloads[block] = payload
            if not target.get("ok"):
                failed_blocks.append({
                    "cpu": cpu,
                    "block": block,
                    "target_key": f"{cpu}:{block}",
                    "status": target.get("status"),
                    "error": target.get("error") or "keine gültige serielle Antwort",
                })
            if cpu == 0:
                report_fields = [
                    self._report_field(field)
                    for field in target.get("fields") or []
                    if web_field_visible(block, field.key, field.metadata)
                ]
            else:
                report_fields = [
                    {
                        "key": str(field.get("key") or ""),
                        "label": str(field.get("label") or field.get("key") or ""),
                        "raw": _json_value(field.get("raw")),
                        "value": _json_value(field.get("value")),
                        "edit_value": _json_value(field.get("edit_value")),
                        "unit": str(field.get("unit") or ""),
                        "reserved": False,
                    }
                    for field in target.get("fields") or []
                    if isinstance(field, dict)
                ]
            snapshots[key] = {
                "cpu": cpu,
                "block": block,
                "target_key": f"{cpu}:{block}",
                "name": target.get("name") or self._backup_target_name(cpu, block),
                "ok": bool(target.get("ok")),
                "status": target.get("status"),
                "captured_at": completed_at if target.get("ok") else None,
                "error": target.get("error"),
                "payload_hex": payload.hex(" ").upper(),
                "payload_len": len(payload),
                "rtt_ms": target.get("rtt_ms"),
                "fields": report_fields,
            }
        inspection = self._strict_backup_archive_inspection(
            image, require_current_pack=True
        )
        if failed_blocks:
            failed = ", ".join(
                f"CPU {item['cpu']} Block {item['block']}" for item in failed_blocks
            )
            raise RuntimeError(f"Wartungsstart nicht möglich; Backup unvollständig: {failed}")
        missing_required = sorted(MAINTENANCE_REQUIRED_BLOCKS - payloads.keys())
        if missing_required:
            blocks = ", ".join(str(block) for block in missing_required)
            raise RuntimeError(f"Wartungsstart nicht möglich; Pflichtblöcke fehlen: {blocks}")
        fuel_type, raw_fuel = fuel_type_from_block24(
            snapshots["24"]["fields"], payloads.get(24)
        )
        if fuel_type == "unknown":
            raise RuntimeError(
                "Wartungsstart nicht möglich; Kraftstoffart wurde nicht eindeutig "
                f"ermittelt (Rohwert {raw_fuel!r})"
            )
        cache_values = {
            item["key"]: item.get("value")
            for item in snapshots["104"]["fields"]
            if item["key"] in MAINTENANCE_CACHE_KEYS
        }
        cache_values.update({
            "Wartung_Cache.fStehtAn": bool(payloads[104][0] & 0x01),
            "Wartung_Cache.fBestaetigt": bool(payloads[104][0] & 0x02),
        })
        service_history = None
        if {80, 82}.issubset(payloads):
            with suppress(Exception):
                service_history = self.pack.service_history({80: payloads[80], 82: payloads[82]})
                snapshots["80"]["service_history"] = service_history
        if 18 in payloads:
            with suppress(Exception):
                snapshots["18"]["message_history"] = self.pack.meldehist(payloads[18])
        if {28, 30, 31, 32}.issubset(payloads):
            with suppress(Exception):
                snapshots["28"]["run_history"] = self.pack.run_history({
                    block: payloads[block] for block in RUN_HISTORY_BLOCKS
                })
        if 102 in payloads:
            with suppress(Exception):
                snapshots["102"]["oil_refill_history"] = self.pack.oil_refill_history(payloads[102])
        for block in MC_STATUS_BLOCKS:
            if block in payloads:
                with suppress(Exception):
                    snapshots[str(block)]["mc_status"] = self.pack.mc_status(payloads[block])
        for block in MOTOR_SNAPSHOT_BLOCKS:
            if block in payloads:
                with suppress(Exception):
                    snapshots[str(block)]["motor_snapshot"] = self.pack.motor_snapshot(
                        block, payloads[block], service_history
                    )
        report = {
            "version": 3,
            "generated_at": completed_at,
            "generated_by": username,
            "pack_rev": self.pack.pack_rev,
            "fuel_type": fuel_type,
            "fuel_source": {
                "key": FUEL_TYPE_KEY,
                "address": FUEL_TYPE_ADDRESS,
                "raw": raw_fuel,
            },
            "snapshot": {
                "started_at": started_at,
                "completed_at": completed_at,
                # Retain CPU-0 lists for old report consumers; target-aware
                # fields are the authoritative 42-target provenance.
                "attempted_blocks": [block for cpu, block in BACKUP_TARGETS if cpu == 0],
                "captured_blocks": sorted(payloads),
                "failed_blocks": failed_blocks,
                "attempted_targets": [
                    {"cpu": cpu, "block": block} for cpu, block in BACKUP_TARGETS
                ],
                "captured_targets": [
                    {"cpu": cpu, "block": block}
                    for cpu, block in BACKUP_TARGETS
                    if capture[(cpu, block)].get("ok")
                ],
                "complete": True,
            },
            "maintenance_status": maintenance_status(cache_values),
            "blocks": snapshots,
        }
        protocol = new_protocol(fuel_type, snapshots["100"]["fields"])
        archive = self._persist_maintenance_backup_archive(image)
        if not (
            archive.get("status") == "ready"
            and archive.get("verified") is True
            and int(archive.get("successful_count") or 0) == len(BACKUP_TARGETS)
            and hmac.compare_digest(
                str(archive.get("image_sha256") or ""),
                str(inspection.get("image_sha256") or ""),
            )
        ):
            raise RuntimeError("Wartungsstart nicht möglich; Archivprüfung fehlgeschlagen")
        created = self.store.create_maintenance_report_with_archive(
            username, fuel_type, report, protocol, archive
        )
        return self.maintenance_report(created["id"])

    def save_maintenance_report(self, report_id: int, protocol: dict) -> dict:
        current = self.store.maintenance_report(report_id)
        if current["status"] != "draft":
            raise ValueError("abgeschlossene Wartungsberichte sind unveränderlich")
        clean = validate_protocol(protocol, report=current["report"])
        self.store.update_maintenance_protocol(report_id, clean)
        return self.maintenance_report(report_id)

    def delete_maintenance_report(self, report_id: int) -> dict:
        return self.store.delete_maintenance_report(report_id)

    def complete_local_intermediate_maintenance(
        self, username: str, report_id: int, protocol: dict, reason: str
    ) -> dict:
        """Close a draft locally; deliberately has no transport/auth/write path."""
        current = self.store.maintenance_report(report_id)
        if current["status"] != "draft":
            raise ValueError("nur ein offener Wartungsentwurf kann als Zwischenwartung gespeichert werden")
        # A Zwischenwartung is deliberately a partial, local notebook entry.
        # It must not pretend that the complete regulator checklist was done.
        clean = validate_protocol(protocol, report=current["report"])
        clean_reason = str(reason).strip()
        if not clean_reason:
            raise ValueError("Grund für die Zwischenwartung ist erforderlich")
        if len(clean_reason) > 500:
            raise ValueError("Grund für die Zwischenwartung darf höchstens 500 Zeichen haben")
        self.store.complete_local_intermediate_maintenance(
            report_id, clean, username, clean_reason
        )
        return self.maintenance_report(report_id)

    def complete_maintenance(self, username: str, report_id: int, protocol: dict,
                             auth_level: int, pass4: str, confirmation: str,
                             *, demo: bool = False) -> dict:
        """Write all mapped maintenance values, verify them, then confirm."""
        if demo:
            if str(confirmation).strip() != MAINTENANCE_DEMO_CONFIRMATION:
                raise ValueError(f"zur Demo-Bestätigung exakt {MAINTENANCE_DEMO_CONFIRMATION!r} eingeben")
            clean = validate_protocol(
                protocol, complete=True,
                report=self.store.maintenance_report(report_id)["report"],
            )
            completion = {
                "mode": "demo",
                "completed_at": _now(),
                "username": username,
                "controller_written": False,
                "hardware_unchanged": True,
                "confirmation_bit_set": False,
                "audits": [],
                "note": "Demolauf lokal abgeschlossen; Block 100 und Block 104 blieben unverändert.",
            }
            self.store.complete_demo_maintenance_report(report_id, clean, completion)
            return self.maintenance_report(report_id)
        if str(confirmation).strip() != MAINTENANCE_CONFIRMATION:
            raise ValueError(f"zur Bestätigung exakt {MAINTENANCE_CONFIRMATION!r} eingeben")
        auth_level = _validated_auth_level(auth_level, "Auth-Level für Wartungsabschluss")
        clean = validate_protocol(
            protocol, complete=True,
            report=self.store.maintenance_report(report_id)["report"],
        )
        archive_index = self.store.backup_archive_for_report(report_id)
        archive, _archive_raw, archive_image, archive_inspection = (
            self._load_verified_backup_archive(int(archive_index["id"]))
        )
        self._strict_backup_archive_inspection(
            archive_image, require_current_pack=True
        )
        if (
            not archive.get("ready")
            or archive.get("pack_compatible") is not True
            or int(archive.get("target_count") or 0) != len(BACKUP_TARGETS)
            or int(archive.get("successful_count") or 0) != len(BACKUP_TARGETS)
        ):
            raise ValueError(
                "Live-Abschluss benötigt ein bereites, verifiziertes, pack-kompatibles 42/42-Backup"
            )
        self.store.claim_live_maintenance_completion(report_id, clean, archive)
        claimed = True
        audits: list[dict] = []
        auth = None
        try:
            with self.state_lock:
                if not self.serial_enabled:
                    raise TransportError("serielle Verbindung ist getrennt")
            allowlist = WriteAllowlist()
            with self.serial_lock:
                with self.state_lock:
                    if not self.serial_enabled:
                        raise TransportError("serielle Verbindung ist getrennt")
                with self.service.session() as session:
                    auth = self.service.authenticate(session, auth_level, pass4 or None)
                    if not auth.ok:
                        raise PermissionError(f"Auth-Level {auth_level} wurde nicht gewährt")
                    authenticated_serial = str(
                        getattr(auth, "serial_number", "") or ""
                    ).strip()
                    archived_serial = str(
                        (archive_inspection.get("controller") or {}).get("serial_number")
                        or ""
                    ).strip()
                    if not authenticated_serial or not archived_serial or not hmac.compare_digest(
                        authenticated_serial, archived_serial
                    ):
                        raise PermissionError(
                            "Regleridentität stimmt nicht mit dem Wartungs-Backup überein"
                        )

                    current100 = self.service.read_block(session, 100)
                    if not current100.ok:
                        raise RuntimeError("Wartungsdatenblock 100 konnte nicht gelesen werden")
                    before100 = bytes(current100.payload)
                    after100 = bytearray(before100)
                    changed100 = ["Wartung_Ew1.abFaNameMonteur"]
                    self.pack.encode_value(after100, "Wartung_Ew1.abFaNameMonteur", clean["technician"], block=100)
                    for definition in checklist_definition(clean["fuel_type"]):
                        status = clean["checklist"][definition["id"]]
                        key = definition["controller_key"]
                        self.pack.encode_value(
                            after100,
                            key,
                            str(CHECKLIST_RAW_VALUES[status]),
                            raw_mode=True,
                            block=100,
                        )
                        changed100.append(key)
                    for key, value in clean["measurements"].items():
                        if value == "":
                            continue
                        if key not in MAINTENANCE_MEASUREMENT_KEYS:
                            raise KeyError(f"unzulässiges Wartungsmessfeld {key}")
                        self.pack.encode_value(after100, key, value, block=100)
                        changed100.append(key)
                    audit100 = self.service.write_payload(
                        session, 100, before100, bytes(after100), changed100, allowlist, dry_run=False
                    ).as_dict()
                    audit100.update({"auth_level_requested": auth_level, "auth_level_granted": auth.granted_level})
                    audits.append(audit100)
                    self.store.audit(_now(), username, 100, audit100)
                    if not audit100.get("written") or not audit100.get("readback_ok"):
                        raise RuntimeError(audit100.get("error") or "Wartungsdaten konnten nicht bestätigt geschrieben werden")

                    current104 = self.service.read_block(session, 104)
                    if not current104.ok or not current104.payload:
                        raise RuntimeError("Wartungscache Block 104 konnte nicht gelesen werden")
                    before104 = bytes(current104.payload)
                    after104 = bytearray(before104)
                    self.pack.encode_value(
                        after104, "Wartung_Cache.fBestaetigt", "1", raw_mode=True, block=104
                    )
                    audit104 = self.service.write_payload(
                        session, 104, before104, bytes(after104),
                        ["Wartung_Cache.fBestaetigt"], allowlist, dry_run=False,
                    ).as_dict()
                    audit104.update({"auth_level_requested": auth_level, "auth_level_granted": auth.granted_level})
                    audits.append(audit104)
                    self.store.audit(_now(), username, 104, audit104)
                    if not audit104.get("written") or not audit104.get("readback_ok"):
                        raise RuntimeError(audit104.get("error") or "Wartungsbestätigung konnte nicht geschrieben werden")

            completion = {
                "mode": "live",
                "completed_at": _now(),
                "username": username,
                "controller_written": True,
                "hardware_unchanged": False,
                "confirmation_bit_set": True,
                "auth_level_requested": auth_level,
                "auth_level_granted": getattr(auth, "granted_level", None),
                "backup_archive_id": int(archive["id"]),
                "audits": audits,
            }
            self.store.finish_live_maintenance_completion(report_id, completion)
            claimed = False
            return self.maintenance_report(report_id)
        except Exception as exc:
            possible_write = any(
                bool(audit.get("write_attempted") or audit.get("written"))
                for audit in audits
            )
            if claimed:
                if possible_write:
                    failure = {
                        "mode": "live",
                        "completed_at": _now(),
                        "username": username,
                        "controller_written": any(
                            bool(audit.get("written")) for audit in audits
                        ),
                        "hardware_unchanged": False,
                        "confirmation_bit_set": None,
                        "auth_level_requested": auth_level,
                        "auth_level_granted": getattr(auth, "granted_level", None),
                        "backup_archive_id": int(archive["id"]),
                        "uncertain": True,
                        "error": str(exc) or exc.__class__.__name__,
                        "audits": audits,
                    }
                    self.store.fail_live_maintenance_completion(report_id, failure)
                else:
                    self.store.release_live_maintenance_completion(report_id)
            raise

    def maintenance_export(self, report_id: int, export_type: str) -> tuple[bytes, str, str]:
        item = self.maintenance_report(report_id)
        if item.get("status") == "completing":
            raise ValueError(
                "Wartungsabschluss läuft; Export nach Abschluss erneut aufrufen"
            )
        report, protocol, completion = item["report"], item["protocol"], item["completion"]
        if export_type == "json":
            return json_export(report, protocol, completion), "application/json; charset=utf-8", f"dachs-wartung-{report_id}.json"
        if export_type == "html":
            return report_html(report, protocol, completion), "text/html; charset=utf-8", f"dachs-wartung-{report_id}.html"
        if export_type == "pdf":
            return report_pdf(report, protocol, completion), "application/pdf", f"dachs-wartung-{report_id}.pdf"
        raise ValueError("Exportformat muss html, pdf oder json sein")

    def auth_preview(self) -> dict:
        """Read the current PW4 inputs and return a non-writing admin preview."""
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        with self.serial_lock, self.service.session() as session:
            inputs = self.service.authentication_inputs(session)
        pw4 = calculate_pw4(inputs.serial_number, inputs.operating_hours)
        valid = len(pw4) == 4 and pw4.isdigit()
        return {
            "ok": valid,
            "serial_number": inputs.serial_number,
            "operating_hours": inputs.operating_hours,
            "pw4": pw4,
            "formula": "letzte 3 Seriennummern + 2749 + halbe Betriebsstunden; letzte 4 Stellen",
            "read_at": _now(),
        }

    @staticmethod
    def _required_decoded_field(
        fields, key: str, block: int, operation: str = "Entstörung"
    ):
        field = next((item for item in fields if item.key == key), None)
        if field is None:
            raise TransportError(f"{operation}: {key} fehlt in Block {block}")
        return field

    def _service_reset_sample(self, session) -> dict:
        result22, fields22 = self.service.decoded_block(session, 22)
        if not result22.ok:
            raise TransportError("Entstörung: Block 22 konnte nicht frisch gelesen werden")
        result24, fields24 = self.service.decoded_block(session, 24)
        if not result24.ok:
            raise TransportError("Entstörung: Block 24 konnte nicht frisch gelesen werden")

        service = self._required_decoded_field(fields22, "Hka_Bd.bStoerung", 22)
        fault_count = self._required_decoded_field(
            fields22, "Hka_Bd.usAnzahlStoerungenHka", 22
        )
        fault_runtime = self._required_decoded_field(
            fields22, "Hka_Bd.ulBetriebssekundenLetzteStoerung", 22
        )
        motor = self._required_decoded_field(fields24, MOTOR_STATUS_KEY, 24)
        if (
            isinstance(service.raw, bool)
            or not isinstance(service.raw, int)
            or not 0 <= service.raw <= 0xFF
        ):
            raise TransportError("Entstörung: Servicecode besitzt keinen gültigen Rohwert")
        if (
            isinstance(motor.raw, bool)
            or not isinstance(motor.raw, int)
            or not 0 <= motor.raw <= 0xFF
        ):
            raise TransportError("Entstörung: Motorstatus besitzt keinen gültigen Rohwert")
        if (
            isinstance(fault_count.raw, bool)
            or not isinstance(fault_count.raw, int)
            or not 0 <= fault_count.raw <= 0xFFFF
        ):
            raise TransportError("Entstörung: Störungszähler besitzt keinen gültigen Rohwert")
        if (
            isinstance(fault_runtime.raw, bool)
            or not isinstance(fault_runtime.raw, int)
            or not 0 <= fault_runtime.raw <= 0xFFFFFFFF
        ):
            raise TransportError(
                "Entstörung: Betriebssekunden der letzten Störung besitzen keinen gültigen Rohwert"
            )

        temperatures = {}
        for key, label, minimum, maximum in SERVICE_RESET_TEMPERATURE_LIMITS:
            field = self._required_decoded_field(fields24, key, 24)
            try:
                value = float(field.value)
            except (TypeError, ValueError) as exc:
                raise TransportError(f"Entstörung: {label} ist nicht numerisch") from exc
            if not math.isfinite(value):
                raise TransportError(f"Entstörung: {label} ist nicht endlich")
            temperatures[key] = {
                "key": key,
                "label": label,
                "value_c": value,
                "raw": _json_value(field.raw),
                "minimum_c": minimum,
                "maximum_c": maximum,
                "valid": web_measurement_valid(24, key, field.value, field.raw),
            }
        return {
            "service_code_raw": int(service.raw),
            "service_text": str(service.value),
            "fault_count": int(fault_count.raw),
            "fault_runtime_seconds": int(fault_runtime.raw),
            "motor_status": int(motor.raw),
            "motor_status_text": str(motor.value),
            "temperatures": temperatures,
        }

    def _service_reset_state(self, session) -> dict:
        """Read Block 22/24 twice and derive a fail-closed reset preview."""
        read_started = time.monotonic()
        first = self._service_reset_sample(session)
        second = self._service_reset_sample(session)
        read_duration = time.monotonic() - read_started
        if read_duration > SERVICE_RESET_MAX_STATE_READ_SECONDS:
            raise TransportError(
                "Entstörung: Doppellesung dauerte zu lange für einen frischen Zustand"
            )
        if first["service_code_raw"] != second["service_code_raw"]:
            raise TransportError("Entstörung: Servicecode änderte sich während der Direktlesung")
        if first["fault_count"] != second["fault_count"]:
            raise TransportError("Entstörung: Störungszähler änderte sich während der Direktlesung")
        if first["fault_runtime_seconds"] != second["fault_runtime_seconds"]:
            raise TransportError("Entstörung: Störereignis änderte sich während der Direktlesung")
        if first["motor_status"] != second["motor_status"]:
            raise TransportError("Entstörung: Motorstatus änderte sich während der Direktlesung")

        blockers = []
        service_raw = int(second["service_code_raw"])
        motor_status = int(second["motor_status"])
        if service_raw == 0:
            blockers.append("Keine aktive Störung")
        if motor_status not in SERVICE_RESET_SAFE_MOTOR_STATUSES:
            blockers.append(
                f"Motorstatus {motor_status} ist nicht sicher AUS (zulässig: 15 oder 16)"
            )
        if not self.service_reset_temperature_policy_approved:
            blockers.append(
                "Entstör-Temperatur- und Wiederanlaufpolitik ist für diese Installation "
                "nicht ausdrücklich fachlich freigegeben"
            )

        temperatures = []
        for key, label, minimum, maximum in SERVICE_RESET_TEMPERATURE_LIMITS:
            left = first["temperatures"][key]
            right = second["temperatures"][key]
            delta = abs(float(right["value_c"]) - float(left["value_c"]))
            if delta > SERVICE_RESET_TEMPERATURE_COHERENCE_C:
                raise TransportError(
                    f"Entstörung: {label} änderte sich um {delta:g} °C während der Direktlesung"
                )
            samples = [float(left["value_c"]), float(right["value_c"])]
            value = max(samples)
            valid = bool(left["valid"] and right["valid"])
            safe = valid and all(minimum <= sample <= maximum for sample in samples)
            if not valid:
                blockers.append(f"{label} ist nicht plausibel lesbar")
            elif not safe:
                blockers.append(
                    f"{label} mit Messwerten {samples[0]:g}/{samples[1]:g} °C "
                    "außerhalb der Entstörgrenze "
                    f"{minimum:g}–{maximum:g} °C"
                )
            temperatures.append({
                "key": key,
                "label": label,
                "value_c": value,
                "samples_c": samples,
                "minimum_c": minimum,
                "maximum_c": maximum,
                "safe": safe,
            })

        service_code = service_raw + 100 if service_raw else 0
        return {
            "schema": SERVICE_RESET_SCHEMA,
            "eligible": not blockers,
            "blockers": blockers,
            "service_code": service_code,
            "service_code_raw": service_raw,
            "service_text": second["service_text"],
            "fault_count": int(second["fault_count"]),
            "fault_runtime_seconds": int(second["fault_runtime_seconds"]),
            "fault_event": {
                "count": int(second["fault_count"]),
                "runtime_seconds": int(second["fault_runtime_seconds"]),
            },
            "motor_status": motor_status,
            "motor_status_text": second["motor_status_text"],
            "safe_motor_statuses": sorted(SERVICE_RESET_SAFE_MOTOR_STATUSES),
            "temperatures": temperatures,
            "validated_samples": 2,
            "read_source": "direkte Block-22/24-Doppellesung über Serialworker",
            "read_at": _now(),
            "read_duration_seconds": round(read_duration, 3),
            "volatile_ram_only": True,
            "auth_level": SERVICE_RESET_AUTH_LEVEL,
            "temperature_policy_approved": bool(
                self.service_reset_temperature_policy_approved
            ),
        }

    def _clear_service_reset_token(self, username: str) -> None:
        with self._service_reset_token_lock:
            self._service_reset_token = None

    def _require_service_reset_worker(self) -> None:
        if not str(getattr(self.service, "serial_socket", "") or "").strip():
            raise TransportError(
                "Entstörung ist ausschließlich über den exklusiven Serialworker verfügbar"
            )

    @contextmanager
    def _service_reset_worker_session(self):
        """Open one worker session only if web serial is still enabled.

        The second state check deliberately happens after acquiring the web
        serial lease.  A reset that was queued when the operator disconnected
        web serial must not overtake that disconnect and write afterwards.
        """
        with self.serial_lock:
            with self.state_lock:
                if not self.serial_enabled:
                    raise TransportError("serielle Verbindung ist getrennt")
            with self.service.session() as session:
                yield session

    def _issue_service_reset_token(self, username: str, state: dict) -> str:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._service_reset_token_lock:
            # One controller has one outstanding acknowledgement capability.
            # Bind it to the complete fault-event identity, not merely to the
            # visible SC number. A newer preview invalidates every older tab.
            self._service_reset_token = {
                "token": token,
                "username": str(username),
                "service_code_raw": int(state["service_code_raw"]),
                "fault_count": int(state["fault_count"]),
                "fault_runtime_seconds": int(state["fault_runtime_seconds"]),
                "expires_at": now + SERVICE_RESET_TOKEN_TTL_SECONDS,
            }
        return token

    def _consume_service_reset_token(
        self, username: str, token: object, expected_raw: int
    ) -> dict:
        supplied = str(token or "")
        with self._service_reset_token_lock:
            issued = self._service_reset_token
            expired = bool(
                issued is not None
                and time.monotonic() > float(issued["expires_at"])
            )
            identity_matches = bool(
                issued is not None
                and supplied
                and str(issued["username"]) == str(username)
                and secrets.compare_digest(supplied, str(issued["token"]))
            )
            valid = bool(
                identity_matches
                and not expired
                and int(issued["service_code_raw"]) == int(expected_raw)
            )
            # Consume a matching capability even if its bound code was sent
            # incorrectly.  A stale token must not delete a newer preview.
            if expired or identity_matches:
                self._service_reset_token = None
        if not valid:
            raise APIRequestConflictError(
                "Entstöranforderung fehlt, ist abgelaufen oder wurde bereits verbraucht; "
                "Zustand neu lesen und niemals automatisch wiederholen"
            )
        return dict(issued)

    @staticmethod
    def _require_service_reset_token_fresh(token_preimage: dict, stage: str) -> None:
        if time.monotonic() > float(token_preimage["expires_at"]):
            raise APIRequestConflictError(
                f"Entstörbestätigung lief {stage} ab; Zustand neu lesen und erneut bestätigen"
            )

    @staticmethod
    def _require_service_reset_prewrite_fresh(checked_at: float) -> float:
        age = max(0.0, time.monotonic() - float(checked_at))
        if age > SERVICE_RESET_MAX_PREWRITE_AGE_SECONDS:
            raise APIRequestConflictError(
                "Letzte Entstör-Sicherheitsprüfung ist vor dem Write nicht mehr frisch; "
                "kein Telegramm gesendet"
            )
        return age

    def _service_reset_retry_guard_remaining(self, service_code_raw: int) -> float:
        now = time.monotonic()
        with self._service_reset_token_lock:
            attempt = self._service_reset_last_attempt
        if attempt is None or int(attempt["service_code_raw"]) != int(service_code_raw):
            return 0.0
        return max(
            0.0,
            SERVICE_RESET_RETRY_GUARD_SECONDS - (now - float(attempt["attempted_at"])),
        )

    def _record_service_reset_attempt(self, service_code_raw: int) -> None:
        with self._service_reset_token_lock:
            self._service_reset_last_attempt = {
                "service_code_raw": int(service_code_raw),
                "attempted_at": time.monotonic(),
            }

    def service_reset_preview(self, username: str) -> dict:
        """Return a fresh, non-authenticating eligibility preview."""
        with self._service_reset_flow_lock:
            return self._service_reset_preview_locked(username)

    def _service_reset_preview_locked(self, username: str) -> dict:
        self._clear_service_reset_token(username)
        self._require_service_reset_worker()
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        with self._service_reset_worker_session() as session:
            state = self._service_reset_state(session)
        pending_intent = self.store.pending_service_reset_write_intent()
        if pending_intent is not None:
            state = {
                **state,
                "eligible": False,
                "blockers": [
                    *state["blockers"],
                    "Nicht aufgelöster früherer Entstör-Schreibversuch; nicht wiederholen, "
                    "Audit und Reglerzustand manuell prüfen",
                ],
            }
        retry_guard = self._service_reset_retry_guard_remaining(
            state["service_code_raw"]
        )
        if retry_guard > 0.0 and state["service_code_raw"] != 0:
            state = {
                **state,
                "eligible": False,
                "blockers": [
                    *state["blockers"],
                    "Vorheriger Entstörversuch wird nicht automatisch wiederholt; "
                    f"erneute manuelle Prüfung frühestens in {retry_guard:.1f} s",
                ],
            }
        return {
            **state,
            "request_token": (
                self._issue_service_reset_token(username, state)
                if state["eligible"] else None
            ),
            "request_token_ttl_seconds": SERVICE_RESET_TOKEN_TTL_SECONDS,
            "retry_guard_remaining_seconds": round(retry_guard, 3),
            "unresolved_write_intent": pending_intent,
        }

    def _read_service_code_once(self, session) -> tuple[int, str]:
        result, fields = self.service.decoded_block(session, 22)
        if not result.ok:
            raise TransportError("Entstörung: Servicecode-Readback aus Block 22 fehlgeschlagen")
        field = self._required_decoded_field(fields, "Hka_Bd.bStoerung", 22)
        if isinstance(field.raw, bool) or not isinstance(field.raw, int):
            raise TransportError("Entstörung: Servicecode-Readback ist ungültig")
        return int(field.raw), str(field.value)

    def _service_reset_readback(self, session) -> dict:
        raw_codes = []
        errors = []
        consecutive_zero = 0
        last_text = ""
        for attempt in range(1, SERVICE_RESET_READBACK_ATTEMPTS + 1):
            if attempt > 1:
                self._service_reset_wait(SERVICE_RESET_READBACK_DELAY_SECONDS)
            try:
                raw, last_text = self._read_service_code_once(session)
                raw_codes.append(raw)
                consecutive_zero = consecutive_zero + 1 if raw == 0 else 0
                if consecutive_zero >= SERVICE_RESET_STABLE_ZERO_SAMPLES:
                    return {
                        "ok": True,
                        "attempts": attempt,
                        "raw_codes": raw_codes,
                        "errors": errors,
                        "service_code_raw": 0,
                        "service_code": 0,
                        "service_text": last_text,
                    }
            except Exception as exc:
                consecutive_zero = 0
                errors.append(f"{exc.__class__.__name__}: {exc}")
        last_raw = raw_codes[-1] if raw_codes else None
        return {
            "ok": False,
            "attempts": SERVICE_RESET_READBACK_ATTEMPTS,
            "raw_codes": raw_codes,
            "errors": errors,
            "service_code_raw": last_raw,
            "service_code": None if last_raw is None else (last_raw + 100 if last_raw else 0),
            "service_text": last_text,
        }

    def reset_service_code(
        self, username: str, expected_service_code: object, confirm: object,
        request_token: object,
    ) -> dict:
        """Acknowledge exactly one current fault through volatile RAM 0x1c9e."""
        with self._service_reset_flow_lock:
            return self._reset_service_code_locked(
                username, expected_service_code, confirm, request_token
            )

    def _reset_service_code_locked(
        self, username: str, expected_service_code: object, confirm: object,
        request_token: object,
    ) -> dict:
        if confirm is not True:
            raise ValueError("Entstörung muss ausdrücklich bestätigt werden")
        if isinstance(expected_service_code, bool) or not isinstance(expected_service_code, int):
            raise ValueError("erwarteter Servicecode muss eine ganze SC-Nummer sein")
        if not 101 <= expected_service_code <= 355:
            raise ValueError("erwarteter Servicecode muss zwischen SC 101 und SC 355 liegen")
        expected_raw = expected_service_code - 100
        self._require_service_reset_worker()
        token_preimage = self._consume_service_reset_token(
            username, request_token, expected_raw
        )
        pending_intent = self.store.pending_service_reset_write_intent()
        if pending_intent is not None:
            raise APIRequestConflictError(
                "Nicht aufgelöster früherer Entstör-Schreibversuch; nicht wiederholen, "
                "Audit und Reglerzustand manuell prüfen"
            )
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")

        recorded_at = _now()
        audit = {
            "schema": SERVICE_RESET_SCHEMA,
            "operation": "manual-servicecode-reset",
            "critical": True,
            "cpu": 0,
            "address": f"0x{SERVICE_RESET_ADDRESS:06x}",
            "volatile_ram_only": True,
            "nvm_write": False,
            "request_hex": SERVICE_RESET_REQUEST.hex(),
            "expected_response_hex": SERVICE_RESET_RESPONSE.hex(),
            "expected_service_code": expected_service_code,
            "expected_service_code_raw": expected_raw,
            "service_code_before": None,
            "service_code_raw_before": None,
            "fault_count_before": None,
            "fault_runtime_seconds_before": None,
            "service_code_after": None,
            "service_code_raw_after": None,
            "motor_status_before": None,
            "preconditions": None,
            "prewrite_preconditions": None,
            "prewrite_age_seconds": None,
            "prewrite_max_age_seconds": SERVICE_RESET_MAX_PREWRITE_AGE_SECONDS,
            "token_valid_at_write": None,
            "conflict": False,
            "auth_level_requested": SERVICE_RESET_AUTH_LEVEL,
            "auth_level_granted": None,
            "auth_result_uncertain": False,
            "write_attempted": False,
            "write_count": 0,
            "write_intent_id": None,
            "write_intent_persisted": False,
            "write_intent_resolved": False,
            "write_intent_resolution": None,
            "restart_guard_pending": False,
            "audit_persisted": False,
            "written": False,
            "ack_positive": None,
            "response_echo_ok": False,
            "readback_ok": None,
            "readback_scope": "service-code-zero-two-consecutive",
            "readback_attempts": 0,
            "readback_codes_raw": [],
            "service_code_cleared": False,
            "write_confirmed": False,
            "clear_confirmed_at": None,
            "clear_marker_persisted": False,
            "deauthenticated": None,
            "cleanup_uncertain": False,
            "post_cleanup_readback_ok": None,
            "post_cleanup_readback_attempts": 0,
            "post_cleanup_readback_codes_raw": [],
            "post_cleanup_readback_errors": [],
            "write_uncertain": False,
            "uncertain": False,
            "changed": False,
            "changed_keys": ["CPU0.RAM.0x001c9e"],
            "unchanged": False,
            "error": None,
        }

        try:
            with self._service_reset_worker_session() as session:
                self._require_service_reset_token_fresh(
                    token_preimage, "während der Wartezeit auf den Serialworker"
                )
                before = self._service_reset_state(session)
                audit.update({
                    "service_code_before": before["service_code"],
                    "service_code_raw_before": before["service_code_raw"],
                    "fault_count_before": before["fault_count"],
                    "fault_runtime_seconds_before": before["fault_runtime_seconds"],
                    "motor_status_before": before["motor_status"],
                    "preconditions": before,
                })
                retry_guard = self._service_reset_retry_guard_remaining(
                    before["service_code_raw"]
                )
                if retry_guard > 0.0 and before["service_code_raw"] != 0:
                    audit["conflict"] = True
                    raise APIRequestConflictError(
                        "Vorheriger Entstörversuch wird nicht automatisch wiederholt; "
                        f"erneute manuelle Prüfung frühestens in {retry_guard:.1f} s"
                    )
                token_event_changed = bool(
                    int(before["fault_count"]) != int(token_preimage["fault_count"])
                    or int(before["fault_runtime_seconds"])
                    != int(token_preimage["fault_runtime_seconds"])
                )
                if before["service_code_raw"] != 0 and token_event_changed:
                    audit["conflict"] = True
                    raise APIRequestConflictError(
                        "Störereignis änderte sich seit der bestätigten Vorschau; "
                        "Zustand neu lesen und erneut bestätigen"
                    )
                if before["service_code_raw"] == 0:
                    audit.update({
                        "unchanged": True,
                        "readback_ok": True,
                        "service_code_after": 0,
                        "service_code_raw_after": 0,
                        "service_code_cleared": True,
                    })
                    result = {**audit, "ok": True}
                else:
                    if before["service_code_raw"] != expected_raw:
                        audit["conflict"] = True
                        raise APIRequestConflictError(
                            f"Aktive Störung änderte sich von SC {expected_service_code} "
                            f"auf SC {before['service_code']}; Vorschau neu laden"
                        )
                    if not before["eligible"]:
                        audit["conflict"] = True
                        raise APIRequestConflictError(
                            "Entstörung blockiert: " + "; ".join(before["blockers"])
                        )

                    operation_error = None
                    cleanup_error = None
                    granted_password = ""
                    auth_obtained = False
                    auth_attempted = False
                    try:
                        auth_inputs = self.service.authentication_inputs(session)
                        granted_password = calculate_pw4(
                            auth_inputs.serial_number, auth_inputs.operating_hours
                        )
                        if not re.fullmatch(r"[0-9]{4}", granted_password):
                            raise ValueError("serverseitiger Auth-Schlüssel ist ungültig")
                    except Exception:
                        operation_error = TransportError(
                            "Entstörung: Auth-Schlüssel konnte nicht frisch abgeleitet werden"
                        )

                    # Persist the restart-safe at-most-once guard before any
                    # authentication telegram can leave.  The final safety
                    # double-read happens only after this potentially blocking
                    # database commit, so a changed controller state cannot be
                    # followed by a stale write.
                    if operation_error is None:
                        try:
                            intent_id = secrets.token_urlsafe(24)
                            intent_at = _now()
                            self.store.begin_service_reset_write_intent(
                                intent_id,
                                intent_at,
                                username,
                                expected_raw,
                                int(before["fault_count"]),
                                int(before["fault_runtime_seconds"]),
                                {
                                    "schema": SERVICE_RESET_SCHEMA,
                                    "operation": "manual-servicecode-reset-intent",
                                    "request_hex": SERVICE_RESET_REQUEST.hex(),
                                    "expected_service_code": expected_service_code,
                                    "fault_event": before["fault_event"],
                                },
                            )
                            audit["write_intent_id"] = intent_id
                            audit["write_intent_persisted"] = True
                            audit["restart_guard_pending"] = True
                            self._require_service_reset_token_fresh(
                                token_preimage, "vor der Authentifizierung"
                            )
                        except APIRequestConflictError as exc:
                            audit["conflict"] = True
                            operation_error = exc
                        except Exception:
                            operation_error = RuntimeError(
                                "Persistenter Entstör-Schreibschutz konnte nicht angelegt werden"
                            )

                    if operation_error is None:
                        auth_attempted = True
                        try:
                            auth = self.service.authenticate(
                                session, SERVICE_RESET_AUTH_LEVEL, granted_password
                            )
                            audit["auth_level_granted"] = getattr(auth, "granted_level", None)
                            auth_obtained = bool(
                                getattr(auth, "ok", False)
                                and audit["auth_level_granted"] == SERVICE_RESET_AUTH_LEVEL
                            )
                            if not auth_obtained:
                                operation_error = PermissionError(
                                    "Auth-Level 3 wurde nicht exakt gewährt"
                                )
                        except Exception:
                            audit["auth_result_uncertain"] = True
                            operation_error = TransportError(
                                "Entstörung: Authentifizierung konnte nicht bestätigt werden"
                            )

                    if auth_obtained:
                        try:
                            # Authentication itself performs controller reads.
                            # Revalidate the complete safety state afterwards,
                            # immediately before the sole possible write.
                            prewrite = self._service_reset_state(session)
                            prewrite_checked_at = time.monotonic()
                            audit["prewrite_preconditions"] = prewrite
                            if prewrite["service_code_raw"] == 0:
                                audit.update({
                                    "unchanged": True,
                                    "readback_ok": True,
                                    "service_code_after": 0,
                                    "service_code_raw_after": 0,
                                    "service_code_cleared": True,
                                })
                            elif prewrite["service_code_raw"] != expected_raw:
                                audit["conflict"] = True
                                operation_error = APIRequestConflictError(
                                    f"Aktive Störung änderte sich unmittelbar vor dem Write "
                                    f"von SC {expected_service_code} auf SC {prewrite['service_code']}"
                                )
                            elif (
                                int(prewrite["fault_count"])
                                != int(token_preimage["fault_count"])
                                or int(prewrite["fault_runtime_seconds"])
                                != int(token_preimage["fault_runtime_seconds"])
                            ):
                                audit["conflict"] = True
                                operation_error = APIRequestConflictError(
                                    "Störereignis änderte sich unmittelbar vor dem Write; "
                                    "Zustand neu lesen und erneut bestätigen"
                                )
                            elif not prewrite["eligible"]:
                                audit["conflict"] = True
                                operation_error = APIRequestConflictError(
                                    "Entstörung unmittelbar vor dem Write blockiert: "
                                    + "; ".join(prewrite["blockers"])
                                )
                            if operation_error is None and not audit["unchanged"]:
                                self._require_service_reset_token_fresh(
                                    token_preimage, "unmittelbar vor dem Write"
                                )
                                audit["token_valid_at_write"] = True
                                audit["prewrite_age_seconds"] = (
                                    self._require_service_reset_prewrite_fresh(
                                        prewrite_checked_at
                                    )
                                )
                        except APIRequestConflictError as exc:
                            audit["conflict"] = True
                            operation_error = exc
                        except Exception as exc:
                            operation_error = exc

                    if auth_obtained and operation_error is None and not audit["unchanged"]:
                        try:
                            audit["write_attempted"] = True
                            audit["write_count"] = 1
                            self._record_service_reset_attempt(expected_raw)
                            response = session.request(
                                SERVICE_RESET_REQUEST,
                                session.next_packet(),
                                timeout=0.9,
                                cpu=0,
                            )
                            audit["ack_positive"] = bool(
                                getattr(getattr(response, "ack", None), "positive", False)
                            )
                            validate_service_reset_write_response(response)
                            audit["response_echo_ok"] = True
                            audit["written"] = True
                        except Exception as exc:
                            operation_error = exc

                    if audit["write_attempted"]:
                        try:
                            readback = self._service_reset_readback(session)
                            audit.update({
                                "readback_ok": bool(readback["ok"]),
                                "readback_attempts": int(readback["attempts"]),
                                "readback_codes_raw": list(readback["raw_codes"]),
                                "readback_errors": list(readback["errors"]),
                                "service_code_after": readback["service_code"],
                                "service_code_raw_after": readback["service_code_raw"],
                                "service_code_cleared": bool(readback["ok"]),
                            })
                            if operation_error is None and not readback["ok"]:
                                operation_error = TransportError(
                                    "Entstörung: Servicecode wurde nicht stabil als 0 rückgelesen"
                                )
                        except Exception as exc:
                            audit["readback_ok"] = False
                            audit["readback_errors"] = [exc.__class__.__name__]
                            if operation_error is None:
                                operation_error = TransportError(
                                    "Entstörung: Rückleseprüfung brach unerwartet ab"
                                )

                    # Once an auth telegram may have left the worker, always
                    # issue and validate Auth 0.  The PW4 was derived before
                    # that attempt, so even a lost/invalid auth response has
                    # a deterministic cleanup path.
                    cleanup_required = auth_attempted
                    if cleanup_required:
                        try:
                            self.service.deauthenticate(session, granted_password)
                            audit["deauthenticated"] = True
                        except Exception:
                            audit["deauthenticated"] = False
                            cleanup_error = TransportError(
                                "Auth-0-Cleanup konnte nicht bestätigt werden"
                            )

                    # If Auth 0 is uncertain after a stable clear, gather one
                    # additional read-only controller observation.  This never
                    # changes the already confirmed single-write outcome.
                    if cleanup_error is not None and audit["service_code_cleared"]:
                        try:
                            post_cleanup = self._service_reset_readback(session)
                            audit.update({
                                "post_cleanup_readback_ok": bool(post_cleanup["ok"]),
                                "post_cleanup_readback_attempts": int(
                                    post_cleanup["attempts"]
                                ),
                                "post_cleanup_readback_codes_raw": list(
                                    post_cleanup["raw_codes"]
                                ),
                                "post_cleanup_readback_errors": list(
                                    post_cleanup["errors"]
                                ),
                            })
                        except Exception as exc:
                            audit["post_cleanup_readback_ok"] = False
                            audit["post_cleanup_readback_errors"] = [
                                exc.__class__.__name__
                            ]

                    audit["write_confirmed"] = bool(
                        audit["written"] and audit["readback_ok"] is True
                    )
                    audit["changed"] = bool(
                        audit["write_attempted"] and audit["service_code_cleared"]
                    )
                    audit["write_uncertain"] = bool(
                        audit["write_attempted"] and not audit["write_confirmed"]
                    )
                    audit["cleanup_uncertain"] = bool(
                        auth_attempted and audit["deauthenticated"] is not True
                    )

                    if audit["write_confirmed"]:
                        clear_confirmed_at = _now()
                        self.store.mark_service_reset_clear_confirmed(
                            str(audit["write_intent_id"]), clear_confirmed_at
                        )
                        audit["clear_confirmed_at"] = clear_confirmed_at
                        audit["clear_marker_persisted"] = True

                    if audit["write_intent_id"]:
                        audit["write_intent_resolved"] = bool(
                            audit["write_confirmed"] or not audit["write_attempted"]
                        )
                        if audit["write_confirmed"]:
                            audit["write_intent_resolution"] = "write-confirmed"
                        elif audit["unchanged"]:
                            audit["write_intent_resolution"] = (
                                "already-cleared-before-write"
                            )
                        elif audit["write_attempted"]:
                            audit["write_intent_resolution"] = "write-uncertain"
                        else:
                            audit["write_intent_resolution"] = "aborted-before-write"
                        audit["restart_guard_pending"] = not audit[
                            "write_intent_resolved"
                        ]

                    error_messages = []
                    if operation_error is not None:
                        error_messages.append(str(operation_error))
                    if cleanup_error is not None:
                        if audit["write_confirmed"]:
                            error_messages.append(
                                "Störung ist bestätigt gelöscht; " + str(cleanup_error)
                            )
                        else:
                            error_messages.append(str(cleanup_error))
                        if audit["post_cleanup_readback_ok"] is False:
                            error_messages.append(
                                "read-only Nachprüfung des Reglerzustands blieb unklar"
                            )
                    audit["error"] = "; ".join(dict.fromkeys(error_messages)) or None
                    audit["uncertain"] = bool(
                        audit["write_uncertain"]
                        or audit["cleanup_uncertain"]
                        or audit["restart_guard_pending"]
                    )
                    result = {
                        **audit,
                        "ok": bool(
                            (audit["write_confirmed"] or audit["unchanged"])
                            and audit["deauthenticated"] is True
                            and not audit["error"]
                        ),
                    }

                    if audit["service_code_cleared"]:
                        with self.state_lock:
                            current = self.live_values.get((22, "Hka_Bd.bStoerung"))
                            if current is not None:
                                self.live_values[(22, "Hka_Bd.bStoerung")] = {
                                    **current,
                                    "raw": 0,
                                    "value": "Kein aktiver Servicecode",
                                    "recorded_at": _now(),
                                }
        except (APIRequestConflictError, ValueError, KeyError, PermissionError, TransportError) as exc:
            audit["error"] = str(exc)
            if audit["write_intent_id"]:
                audit.update({
                    "write_intent_resolved": False,
                    "write_intent_resolution": "unexpected-error",
                    "restart_guard_pending": True,
                    "audit_persisted": True,
                    "uncertain": True,
                })
                try:
                    self.store.record_service_reset_outcome(
                        audit["write_intent_id"], _now(), username, 0, audit,
                        resolve=False,
                        resolution="unexpected-error",
                        details={
                            "schema": SERVICE_RESET_SCHEMA,
                            "write_attempted": bool(audit["write_attempted"]),
                            "error": audit["error"],
                        },
                    )
                except Exception:
                    audit["audit_persisted"] = False
            else:
                audit["audit_persisted"] = True
                self.store.audit(recorded_at, username, 0, audit)
            raise
        except Exception as exc:
            audit["error"] = str(exc) or exc.__class__.__name__
            if audit["write_intent_id"]:
                audit.update({
                    "write_intent_resolved": False,
                    "write_intent_resolution": "unexpected-error",
                    "restart_guard_pending": True,
                    "audit_persisted": True,
                    "uncertain": True,
                })
                try:
                    self.store.record_service_reset_outcome(
                        audit["write_intent_id"], _now(), username, 0, audit,
                        resolve=False,
                        resolution="unexpected-error",
                        details={
                            "schema": SERVICE_RESET_SCHEMA,
                            "write_attempted": bool(audit["write_attempted"]),
                            "error": audit["error"],
                        },
                    )
                except Exception:
                    audit["audit_persisted"] = False
            else:
                audit["audit_persisted"] = True
                self.store.audit(recorded_at, username, 0, audit)
            raise

        result["audit_persisted"] = True
        if result["write_intent_id"]:
            intent_details = {
                "schema": SERVICE_RESET_SCHEMA,
                "operation": "manual-servicecode-reset-intent-outcome",
                "resolution": result["write_intent_resolution"],
                "write_attempted": bool(result["write_attempted"]),
                "write_confirmed": bool(result["write_confirmed"]),
                "service_code_cleared": bool(result["service_code_cleared"]),
                "readback_codes_raw": list(result["readback_codes_raw"]),
                "deauthenticated": result["deauthenticated"],
                "cleanup_uncertain": bool(result["cleanup_uncertain"]),
                "post_cleanup_readback_ok": result["post_cleanup_readback_ok"],
                "post_cleanup_readback_codes_raw": list(
                    result["post_cleanup_readback_codes_raw"]
                ),
                "clear_confirmed": bool(result["write_confirmed"]),
                "clear_confirmed_at": result.get("clear_confirmed_at"),
                "error": result["error"],
            }
            try:
                self.store.record_service_reset_outcome(
                    result["write_intent_id"],
                    _now(),
                    username,
                    0,
                    result,
                    resolve=bool(result["write_intent_resolved"]),
                    resolution=str(result["write_intent_resolution"]),
                    details=intent_details,
                )
            except Exception:
                persistence_message = (
                    "Persistenter Entstör-Schreibschutz und Pflichtaudit konnten "
                    "nicht atomar abgeschlossen werden"
                )
                prior_error = str(result["error"] or "").strip()
                result.update({
                    "write_intent_resolved": False,
                    "restart_guard_pending": True,
                    "audit_persisted": False,
                    "uncertain": True,
                    "ok": False,
                    "error": (
                        f"{prior_error}; {persistence_message}"
                        if prior_error else persistence_message
                    ),
                })
                return result
        else:
            self.store.audit(recorded_at, username, 0, result)
        return result

    @staticmethod
    def _dachs_control_action(action: object) -> tuple[str, bool]:
        value = str(action or "").strip().lower()
        if value not in {"on", "off"}:
            raise ValueError("Dachs Ein/Aus: Aktion muss 'on' oder 'off' sein")
        return value, value == "on"

    def _dachs_control_read_byte(self, session, request: bytes, label: str) -> int:
        response = session.request(
            request,
            session.next_packet(),
            timeout=0.9,
            cpu=0,
        )
        return dachs_control_read_byte(response, request, label)

    def _dachs_control_sample(self, session) -> dict:
        result22, fields22 = self.service.decoded_block(session, 22)
        if not result22.ok:
            raise TransportError("Dachs Ein/Aus: Block 22 konnte nicht frisch gelesen werden")
        result24, fields24 = self.service.decoded_block(session, 24)
        if not result24.ok:
            raise TransportError("Dachs Ein/Aus: Block 24 konnte nicht frisch gelesen werden")

        service = self._required_decoded_field(
            fields22, "Hka_Bd.bStoerung", 22, "Dachs Ein/Aus"
        )
        release = self._required_decoded_field(
            fields22, DACHS_CONTROL_RELEASE_FIELD, 22, "Dachs Ein/Aus"
        )
        motor = self._required_decoded_field(
            fields24, MOTOR_STATUS_KEY, 24, "Dachs Ein/Aus"
        )
        for label, field, maximum in (
            ("Servicecode", service, 0xFF),
            ("Motorstatus", motor, 0xFF),
            ("Lauffreigabe", release, 0xFFFF),
        ):
            if (
                isinstance(field.raw, bool)
                or not isinstance(field.raw, int)
                or not 0 <= field.raw <= maximum
            ):
                raise TransportError(f"Dachs Ein/Aus: {label} besitzt keinen gültigen Rohwert")

        release_byte = self._dachs_control_read_byte(
            session, DACHS_CONTROL_READ_REQUEST, "Ein/Aus"
        )
        block_release_byte = (int(release.raw) >> 8) & 0xFF
        if release_byte != block_release_byte:
            raise TransportError(
                "Dachs Ein/Aus: RAM 0x0ea1 und Block 22 liefern verschiedene Zustände"
            )
        manual_mode = self._dachs_control_read_byte(
            session, DACHS_CONTROL_MANUAL_MODE_REQUEST, "Handbetrieb"
        )

        temperatures = {}
        for key, label, minimum, maximum in SERVICE_RESET_TEMPERATURE_LIMITS:
            field = next((item for item in fields24 if item.key == key), None)
            value = None
            valid = False
            raw = None
            if field is not None:
                raw = _json_value(field.raw)
                try:
                    candidate = float(field.value)
                except (TypeError, ValueError):
                    candidate = math.nan
                if math.isfinite(candidate):
                    value = candidate
                    valid = web_measurement_valid(24, key, field.value, field.raw)
            temperatures[key] = {
                "key": key,
                "label": label,
                "value_c": value,
                "raw": raw,
                "minimum_c": minimum,
                "maximum_c": maximum,
                "valid": bool(valid),
            }
        return {
            "service_code_raw": int(service.raw),
            "service_text": str(service.value),
            "motor_status": int(motor.raw),
            "motor_status_text": str(motor.value),
            "release_word": int(release.raw),
            "release_byte": release_byte,
            "enabled": bool(release_byte & DACHS_CONTROL_MASK),
            "manual_mode": manual_mode,
            "temperatures": temperatures,
        }

    def _dachs_control_state(self, session, action: object, recorded_at: str) -> dict:
        """Read the command preimage twice and derive a fail-closed preview."""
        action_name, target_enabled = self._dachs_control_action(action)
        first = self._dachs_control_sample(session)
        second = self._dachs_control_sample(session)
        for key, label in (
            ("service_code_raw", "Servicecode"),
            ("motor_status", "Motorstatus"),
            ("release_word", "Lauffreigabe"),
            ("release_byte", "RAM 0x0ea1"),
            ("manual_mode", "Handbetrieb"),
        ):
            if first[key] != second[key]:
                raise TransportError(
                    f"Dachs Ein/Aus: {label} änderte sich während der Direktlesung"
                )

        enabled = bool(second["enabled"])
        transition_required = enabled != target_enabled
        blockers = []
        if transition_required and int(second["manual_mode"]) != 0:
            blockers.append(
                f"Hand-/Aktortest ist aktiv (RAM 0x1b5a={int(second['manual_mode'])})"
            )
        if transition_required and target_enabled:
            if int(second["service_code_raw"]) != 0:
                blockers.append(
                    f"Aktive Störung SC {int(second['service_code_raw']) + 100} zuerst getrennt entstören"
                )
            if int(second["motor_status"]) not in DACHS_CONTROL_SAFE_START_STATUSES:
                blockers.append(
                    f"Motorstatus {int(second['motor_status'])} ist nicht sicher AUS "
                    "(zulässig: 15 oder 16)"
                )

        temperatures = []
        for key, label, minimum, maximum in SERVICE_RESET_TEMPERATURE_LIMITS:
            left = first["temperatures"][key]
            right = second["temperatures"][key]
            samples = [left["value_c"], right["value_c"]]
            finite = all(value is not None and math.isfinite(float(value)) for value in samples)
            coherent = bool(
                finite
                and abs(float(samples[1]) - float(samples[0]))
                <= SERVICE_RESET_TEMPERATURE_COHERENCE_C
            )
            valid = bool(left["valid"] and right["valid"] and coherent)
            safe = bool(
                valid
                and all(minimum <= float(value) <= maximum for value in samples)
            )
            if transition_required and target_enabled:
                if not finite:
                    blockers.append(f"{label} ist nicht numerisch lesbar")
                elif not coherent:
                    blockers.append(f"{label} änderte sich während der Direktlesung")
                elif not valid:
                    blockers.append(f"{label} ist nicht plausibel lesbar")
                elif not safe:
                    blockers.append(
                        f"{label} mit Messwerten {float(samples[0]):g}/{float(samples[1]):g} °C "
                        f"außerhalb der Freigabegrenze {minimum:g}–{maximum:g} °C"
                    )
            temperatures.append({
                "key": key,
                "label": label,
                "value_c": right["value_c"],
                "samples_c": samples,
                "minimum_c": minimum,
                "maximum_c": maximum,
                "safe": safe,
            })

        service_raw = int(second["service_code_raw"])
        return {
            "schema": DACHS_CONTROL_SCHEMA,
            "action": action_name,
            "target_enabled": target_enabled,
            "eligible": not blockers,
            "blockers": blockers,
            "transition_required": transition_required,
            "unchanged": not transition_required,
            "enabled": enabled,
            "release_byte": int(second["release_byte"]),
            "release_byte_hex": f"0x{int(second['release_byte']):02x}",
            "release_word": int(second["release_word"]),
            "service_code": service_raw + 100 if service_raw else 0,
            "service_code_raw": service_raw,
            "service_text": second["service_text"],
            "motor_status": int(second["motor_status"]),
            "motor_status_text": second["motor_status_text"],
            "manual_mode": int(second["manual_mode"]),
            "temperatures": temperatures,
            "validated_samples": 2,
            "read_source": "Block-22/24- und RAM-Doppellesung über Serialworker",
            "read_at": str(recorded_at),
            "volatile_ram_only": True,
            "auth_level": DACHS_CONTROL_AUTH_LEVEL,
            "semantics": (
                "Automatikfreigabe, kein Zwangsstart"
                if target_enabled
                else "kontrollierte Stoppanforderung, kein Not-Aus"
            ),
        }

    def _clear_dachs_control_token(self) -> None:
        with self._dachs_control_token_lock:
            self._dachs_control_token = None

    def _issue_dachs_control_token(
        self, username: str, action: str, release_byte: int
    ) -> str:
        token = secrets.token_urlsafe(32)
        with self._dachs_control_token_lock:
            self._dachs_control_token = {
                "token": token,
                "username": str(username),
                "action": str(action),
                "release_byte": int(release_byte),
                "expires_at": time.monotonic() + DACHS_CONTROL_TOKEN_TTL_SECONDS,
            }
        return token

    def _consume_dachs_control_token(
        self, username: str, action: str, token: object
    ) -> dict:
        supplied = str(token or "")
        with self._dachs_control_token_lock:
            issued = self._dachs_control_token
            expired = bool(
                issued is not None
                and time.monotonic() > float(issued["expires_at"])
            )
            identity_matches = bool(
                issued is not None
                and supplied
                and str(issued["username"]) == str(username)
                and secrets.compare_digest(supplied, str(issued["token"]))
            )
            valid = bool(
                identity_matches
                and not expired
                and str(issued["action"]) == str(action)
            )
            if expired or identity_matches:
                self._dachs_control_token = None
        if not valid:
            raise APIRequestConflictError(
                "Dachs-Ein/Aus-Anforderung fehlt, ist abgelaufen oder wurde bereits verbraucht; "
                "Zustand neu lesen und niemals automatisch wiederholen"
            )
        return dict(issued)

    def _dachs_control_retry_guard_remaining(
        self, action: str, release_byte: int
    ) -> float:
        with self._dachs_control_token_lock:
            attempt = self._dachs_control_last_attempt
        if (
            attempt is None
            or str(attempt["action"]) != str(action)
            or int(attempt["release_byte"]) != int(release_byte)
        ):
            return 0.0
        return max(
            0.0,
            DACHS_CONTROL_RETRY_GUARD_SECONDS
            - (time.monotonic() - float(attempt["attempted_at"])),
        )

    def _record_dachs_control_attempt(self, action: str, release_byte: int) -> None:
        with self._dachs_control_token_lock:
            self._dachs_control_last_attempt = {
                "action": str(action),
                "release_byte": int(release_byte),
                "attempted_at": time.monotonic(),
            }

    def dachs_control_preview(self, username: str, action: object) -> dict:
        """Return a fresh, read-only on/off preview with one-shot capability."""
        with self._dachs_control_flow_lock:
            action_name, _target = self._dachs_control_action(action)
            self._clear_dachs_control_token()
            if not str(getattr(self.service, "serial_socket", "") or "").strip():
                raise TransportError(
                    "Dachs Ein/Aus ist ausschließlich über den exklusiven Serialworker verfügbar"
                )
            with self.state_lock:
                if not self.serial_enabled:
                    raise TransportError("serielle Verbindung ist getrennt")
            with self._service_reset_worker_session() as session:
                state = self._dachs_control_state(session, action_name, _now())
            retry_guard = self._dachs_control_retry_guard_remaining(
                action_name, state["release_byte"]
            )
            if retry_guard > 0.0 and state["transition_required"]:
                state = {
                    **state,
                    "eligible": False,
                    "blockers": [
                        *state["blockers"],
                        "Vorheriger Ein/Aus-Versuch wird nicht automatisch wiederholt; "
                        f"erneute manuelle Prüfung frühestens in {retry_guard:.1f} s",
                    ],
                }
            return {
                **state,
                "request_token": (
                    self._issue_dachs_control_token(
                        username, action_name, state["release_byte"]
                    )
                    if state["eligible"]
                    else None
                ),
                "request_token_ttl_seconds": DACHS_CONTROL_TOKEN_TTL_SECONDS,
                "retry_guard_remaining_seconds": round(retry_guard, 3),
            }

    def _dachs_control_readback(self, session, expected_byte: int) -> dict:
        samples = []
        errors = []
        for _attempt in range(2):
            try:
                samples.append(
                    self._dachs_control_read_byte(
                        session, DACHS_CONTROL_READ_REQUEST, "Ein/Aus-Rücklesung"
                    )
                )
            except Exception as exc:
                errors.append(f"{exc.__class__.__name__}: {exc}")
        return {
            "ok": len(samples) == 2 and all(value == expected_byte for value in samples),
            "samples": samples,
            "errors": errors,
            "expected_byte": int(expected_byte),
        }

    def set_dachs_control(
        self, username: str, action: object, confirm: object, request_token: object
    ) -> dict:
        """Apply one fixed read-modify-write to fTasteOnOff with readback."""
        with self._dachs_control_flow_lock:
            return self._set_dachs_control_locked(
                username, action, confirm, request_token
            )

    def _set_dachs_control_locked(
        self, username: str, action: object, confirm: object, request_token: object
    ) -> dict:
        action_name, target_enabled = self._dachs_control_action(action)
        if confirm is not True:
            raise ValueError("Dachs Ein/Aus muss ausdrücklich bestätigt werden")
        if not str(getattr(self.service, "serial_socket", "") or "").strip():
            raise TransportError(
                "Dachs Ein/Aus ist ausschließlich über den exklusiven Serialworker verfügbar"
            )
        issued = self._consume_dachs_control_token(
            username, action_name, request_token
        )
        recorded_at = _now()
        audit = {
            "schema": DACHS_CONTROL_SCHEMA,
            "operation": "manual-dachs-enable" if target_enabled else "manual-dachs-disable",
            "critical": True,
            "cpu": 0,
            "address": f"0x{DACHS_CONTROL_ADDRESS:06x}",
            "mask": f"0x{DACHS_CONTROL_MASK:02x}",
            "action": action_name,
            "target_enabled": target_enabled,
            "volatile_ram_only": True,
            "nvm_write": False,
            "before_byte": None,
            "desired_byte": None,
            "after_byte": None,
            "preconditions": None,
            "prewrite_preconditions": None,
            "auth_level_requested": DACHS_CONTROL_AUTH_LEVEL,
            "auth_level_granted": None,
            "auth_result_uncertain": False,
            "write_attempted": False,
            "write_count": 0,
            "written": False,
            "ack_positive": None,
            "response_echo_ok": False,
            "readback_ok": None,
            "readback_scope": "exact-byte-two-consecutive",
            "readback_bytes": [],
            "target_reached": False,
            "write_confirmed": False,
            "deauthenticated": None,
            "cleanup_uncertain": False,
            "write_uncertain": False,
            "uncertain": False,
            "changed": False,
            "changed_keys": ["CPU0.RAM.0x000ea1.bit6"],
            "unchanged": False,
            "conflict": False,
            "error": None,
        }

        try:
            with self._service_reset_worker_session() as session:
                before = self._dachs_control_state(session, action_name, recorded_at)
                audit.update({
                    "before_byte": before["release_byte"],
                    "preconditions": before,
                })
                if before["release_byte"] != int(issued["release_byte"]):
                    audit["conflict"] = True
                    raise APIRequestConflictError(
                        "Dachs-Ein/Aus-Ausgangswert änderte sich seit der Vorschau; neu lesen"
                    )
                retry_guard = self._dachs_control_retry_guard_remaining(
                    action_name, before["release_byte"]
                )
                if retry_guard > 0.0 and before["transition_required"]:
                    audit["conflict"] = True
                    raise APIRequestConflictError(
                        "Vorheriger Dachs-Ein/Aus-Versuch wird nicht automatisch wiederholt; "
                        f"erneute manuelle Prüfung frühestens in {retry_guard:.1f} s"
                    )
                if before["unchanged"]:
                    audit.update({
                        "unchanged": True,
                        "readback_ok": True,
                        "target_reached": True,
                        "after_byte": before["release_byte"],
                    })
                    result = {**audit, "ok": True}
                else:
                    if not before["eligible"]:
                        audit["conflict"] = True
                        raise APIRequestConflictError(
                            "Dachs Ein/Aus blockiert: " + "; ".join(before["blockers"])
                        )

                    operation_error = None
                    cleanup_error = None
                    granted_password = ""
                    auth_attempted = False
                    auth_obtained = False
                    try:
                        auth_inputs = self.service.authentication_inputs(session)
                        granted_password = calculate_pw4(
                            auth_inputs.serial_number, auth_inputs.operating_hours
                        )
                        if not re.fullmatch(r"[0-9]{4}", granted_password):
                            raise ValueError("serverseitiger Auth-Schlüssel ist ungültig")
                    except Exception:
                        operation_error = TransportError(
                            "Dachs Ein/Aus: Auth-Schlüssel konnte nicht frisch abgeleitet werden"
                        )
                    if operation_error is None:
                        auth_attempted = True
                        try:
                            auth = self.service.authenticate(
                                session, DACHS_CONTROL_AUTH_LEVEL, granted_password
                            )
                            audit["auth_level_granted"] = getattr(auth, "granted_level", None)
                            auth_obtained = bool(
                                getattr(auth, "ok", False)
                                and audit["auth_level_granted"] == DACHS_CONTROL_AUTH_LEVEL
                            )
                            if not auth_obtained:
                                operation_error = PermissionError(
                                    "Auth-Level 3 wurde nicht exakt gewährt"
                                )
                        except Exception:
                            audit["auth_result_uncertain"] = True
                            operation_error = TransportError(
                                "Dachs Ein/Aus: Authentifizierung konnte nicht bestätigt werden"
                            )

                    if auth_obtained:
                        try:
                            prewrite = self._dachs_control_state(
                                session, action_name, _now()
                            )
                            audit["prewrite_preconditions"] = prewrite
                            if prewrite["unchanged"]:
                                audit.update({
                                    "unchanged": True,
                                    "readback_ok": True,
                                    "target_reached": True,
                                    "after_byte": prewrite["release_byte"],
                                })
                            elif prewrite["release_byte"] != before["release_byte"]:
                                audit["conflict"] = True
                                operation_error = APIRequestConflictError(
                                    "Dachs-Ein/Aus-Ausgangswert änderte sich unmittelbar vor dem Write"
                                )
                            elif not prewrite["eligible"]:
                                audit["conflict"] = True
                                operation_error = APIRequestConflictError(
                                    "Dachs Ein/Aus unmittelbar vor dem Write blockiert: "
                                    + "; ".join(prewrite["blockers"])
                                )
                        except Exception as exc:
                            operation_error = exc

                    if auth_obtained and operation_error is None and not audit["unchanged"]:
                        desired_byte = (
                            before["release_byte"] | DACHS_CONTROL_MASK
                            if target_enabled
                            else before["release_byte"] & ~DACHS_CONTROL_MASK
                        )
                        desired_byte &= 0xFF
                        audit["desired_byte"] = desired_byte
                        request = DACHS_CONTROL_WRITE_PREFIX + bytes([desired_byte])
                        audit["request_hex"] = request.hex()
                        audit["expected_response_hex"] = DACHS_CONTROL_WRITE_RESPONSE.hex()
                        try:
                            audit["write_attempted"] = True
                            audit["write_count"] = 1
                            self._record_dachs_control_attempt(
                                action_name, before["release_byte"]
                            )
                            response = session.request(
                                request,
                                session.next_packet(),
                                timeout=0.9,
                                cpu=0,
                            )
                            audit["ack_positive"] = bool(
                                getattr(
                                    getattr(response, "ack", None), "positive", False
                                )
                            )
                            validate_dachs_control_write_response(response)
                            audit["response_echo_ok"] = True
                            audit["written"] = True
                        except Exception as exc:
                            operation_error = exc

                    if audit["write_attempted"]:
                        try:
                            readback = self._dachs_control_readback(
                                session, int(audit["desired_byte"])
                            )
                            audit.update({
                                "readback_ok": bool(readback["ok"]),
                                "readback_bytes": list(readback["samples"]),
                                "readback_errors": list(readback["errors"]),
                                "after_byte": (
                                    readback["samples"][-1]
                                    if readback["samples"]
                                    else None
                                ),
                                "target_reached": bool(readback["ok"]),
                            })
                            if operation_error is None and not readback["ok"]:
                                operation_error = TransportError(
                                    "Dachs Ein/Aus: Zielbyte wurde nicht zweimal exakt rückgelesen"
                                )
                        except Exception:
                            audit["readback_ok"] = False
                            audit["readback_errors"] = ["unerwarteter Rücklesefehler"]
                            if operation_error is None:
                                operation_error = TransportError(
                                    "Dachs Ein/Aus: Rückleseprüfung brach unerwartet ab"
                                )

                    if auth_attempted:
                        try:
                            self.service.deauthenticate(session, granted_password)
                            audit["deauthenticated"] = True
                        except Exception:
                            audit["deauthenticated"] = False
                            cleanup_error = TransportError(
                                "Auth-0-Cleanup konnte nicht bestätigt werden"
                            )

                    audit["write_confirmed"] = bool(
                        audit["written"] and audit["readback_ok"] is True
                    )
                    audit["changed"] = bool(
                        audit["target_reached"]
                        and audit["before_byte"] != audit["after_byte"]
                    )
                    audit["write_uncertain"] = bool(
                        audit["write_attempted"] and not audit["write_confirmed"]
                    )
                    audit["cleanup_uncertain"] = bool(
                        auth_attempted and audit["deauthenticated"] is not True
                    )
                    audit["uncertain"] = bool(
                        audit["write_uncertain"] or audit["cleanup_uncertain"]
                    )
                    if operation_error is not None:
                        audit["error"] = str(operation_error)
                    if cleanup_error is not None:
                        audit["error"] = (
                            f"{audit['error']}; {cleanup_error}"
                            if audit["error"]
                            else str(cleanup_error)
                        )
                    result = {
                        **audit,
                        "ok": bool(
                            (audit["write_confirmed"] or audit["unchanged"])
                            and audit["deauthenticated"] is True
                            and not audit["error"]
                        ),
                    }
        except (APIRequestConflictError, ValueError, KeyError, PermissionError, TransportError) as exc:
            audit["error"] = str(exc)
            self.store.audit(recorded_at, username, 0, audit)
            raise
        except Exception as exc:
            audit["error"] = str(exc) or exc.__class__.__name__
            self.store.audit(recorded_at, username, 0, audit)
            raise

        self.store.audit(recorded_at, username, 0, result)
        return result

    def _require_sc123_limit_worker(self) -> None:
        if not str(getattr(self.service, "serial_socket", "") or "").strip():
            raise TransportError(
                "SC123-Grenze ist ausschließlich über den exklusiven Serialworker verfügbar"
            )

    @contextmanager
    def _sc123_limit_worker_session(self):
        """Open one worker session after the web serial lease is acquired."""
        with self.serial_lock:
            with self.state_lock:
                if not self.serial_enabled:
                    raise TransportError("serielle Verbindung ist getrennt")
            with self.service.session() as session:
                yield session

    def _read_sc123_block(self, session, block: int, label: str) -> bytes:
        result = self.service.read_block(session, block)
        if not result.ok:
            raise TransportError(
                f"SC123-Grenze: {label} von Block {block} fehlgeschlagen"
            )
        payload = bytes(result.payload)
        if len(payload) != SC123_LIMIT_PAYLOAD_LENGTH:
            raise TransportError(
                f"SC123-Grenze: {label} von Block {block} lieferte "
                f"{len(payload)} statt {SC123_LIMIT_PAYLOAD_LENGTH} Byte"
            )
        return payload

    def _sc123_limit_state(self, session) -> tuple[dict, bytes, bytes]:
        capability_first = self._read_sc123_block(
            session, SC123_LIMIT_CAPABILITY_BLOCK, "erste Fähigkeitslesung"
        )
        capability_second = self._read_sc123_block(
            session, SC123_LIMIT_CAPABILITY_BLOCK, "zweite Fähigkeitslesung"
        )
        storage_first = self._read_sc123_block(
            session, SC123_LIMIT_STORAGE_BLOCK, "erste Speicherlesung"
        )
        storage_second = self._read_sc123_block(
            session, SC123_LIMIT_STORAGE_BLOCK, "zweite Speicherlesung"
        )
        state = sc123_limit_capability(
            capability_first, capability_second, storage_first, storage_second
        )
        pending_commands = sc123_limit_pending_commands(capability_second)
        state.update({
            "pending_commands": pending_commands,
            "write_safe": bool(state["available"] and not pending_commands),
            "capability_block": SC123_LIMIT_CAPABILITY_BLOCK,
            "block": SC123_LIMIT_STORAGE_BLOCK,
            "record_offset": SC123_LIMIT_RECORD_OFFSET,
            "record_length": SC123_LIMIT_RECORD_LENGTH,
            "offset": SC123_LIMIT_THRESHOLD_OFFSET,
            "persistent": True,
            "auth_level": SC123_LIMIT_AUTH_LEVEL,
            "manufacturer_evidence_c": [180, 200],
            "unvalidated_above_c": 200,
            "warning": (
                "201–240 °C sind technisch einstellbar, aber nicht durch eine "
                "historische Herstellerfreigabe oder thermische Liveprüfung belegt."
            ),
        })
        return state, capability_second, storage_second

    @staticmethod
    def _sc123_storage_preimage(payload: bytes) -> str:
        if len(payload) != SC123_LIMIT_PAYLOAD_LENGTH:
            raise ValueError("SC123-Grenze: ungültige Block-60-Vorlage")
        return bytes(payload).hex()

    def _poll_sc123_nvm_record(self, session, expected_record: bytes) -> dict:
        expected_record = bytes(expected_record)
        if len(expected_record) != SC123_LIMIT_RECORD_LENGTH:
            raise ValueError("SC123-Grenze: ungültiger erwarteter NVM-Datensatz")
        last_record = None
        last_error = None
        for attempt in range(1, SC123_LIMIT_NVM_POLL_ATTEMPTS + 1):
            try:
                response = session.request(
                    SC123_LIMIT_NVM_READ_REQUEST,
                    session.next_packet(),
                    timeout=self.service.timeout,
                    cpu=0,
                )
                last_record = sc123_limit_nvm_record(response)
                last_error = None
                if last_record == expected_record:
                    return {
                        "ok": True,
                        "attempts": attempt,
                        "record_hex": last_record.hex(),
                        "error": None,
                    }
            except Exception:
                # NVM reads are safely retryable. Keep audit text free of raw
                # transport payloads and credentials.
                last_error = "NVM-Lesung nicht exakt validiert"
            if (
                attempt < SC123_LIMIT_NVM_POLL_ATTEMPTS
                and SC123_LIMIT_NVM_POLL_DELAY_SECONDS > 0
            ):
                time.sleep(SC123_LIMIT_NVM_POLL_DELAY_SECONDS)
        return {
            "ok": False,
            "attempts": SC123_LIMIT_NVM_POLL_ATTEMPTS,
            "record_hex": last_record.hex() if last_record is not None else None,
            "error": last_error or "persistenter NVM-Datensatz stimmt nicht überein",
        }

    def _clear_sc123_limit_token(self) -> None:
        with self._sc123_limit_token_lock:
            self._sc123_limit_token = None

    def _issue_sc123_limit_token(
        self, username: str, target_c: int, storage_payload: bytes
    ) -> str:
        token = secrets.token_urlsafe(32)
        with self._sc123_limit_token_lock:
            self._sc123_limit_token = {
                "token": token,
                "username": str(username),
                "target_c": int(target_c),
                "storage_preimage": self._sc123_storage_preimage(storage_payload),
                "expires_at": time.monotonic() + SC123_LIMIT_TOKEN_TTL_SECONDS,
            }
        return token

    def _consume_sc123_limit_token(
        self, username: str, target_c: int, request_token: object
    ) -> dict:
        if not isinstance(request_token, str) or not request_token:
            raise ValueError("SC123-Grenze: gültige Vorschau-Bestätigung fehlt")
        with self._sc123_limit_token_lock:
            issued = self._sc123_limit_token
            self._sc123_limit_token = None
        if issued is None:
            raise APIRequestConflictError(
                "SC123-Grenze: Vorschau fehlt oder wurde bereits verwendet"
            )
        if time.monotonic() > float(issued["expires_at"]):
            raise APIRequestConflictError("SC123-Grenze: Vorschau ist abgelaufen")
        if not secrets.compare_digest(str(issued["token"]), request_token):
            raise APIRequestConflictError("SC123-Grenze: Vorschau-Token stimmt nicht")
        if issued["username"] != str(username) or issued["target_c"] != int(target_c):
            raise APIRequestConflictError(
                "SC123-Grenze: Vorschau gehört nicht zu dieser Änderung"
            )
        return issued

    def _sc123_limit_retry_guard_remaining(self) -> float:
        with self._sc123_limit_token_lock:
            attempt = self._sc123_limit_last_attempt
        if attempt is None:
            return 0.0
        return max(
            0.0,
            SC123_LIMIT_RETRY_GUARD_SECONDS
            - (time.monotonic() - float(attempt["attempted_at"])),
        )

    def _record_sc123_limit_attempt(self, target_c: int) -> None:
        with self._sc123_limit_token_lock:
            self._sc123_limit_last_attempt = {
                "target_c": int(target_c),
                "attempted_at": time.monotonic(),
            }

    def sc123_temperature_limit_status(self) -> dict:
        """Read the dynamic capability twice; stock firmware fails closed."""
        with self._sc123_limit_flow_lock:
            self._require_sc123_limit_worker()
            with self._sc123_limit_worker_session() as session:
                state, _capability_payload, _storage_payload = (
                    self._sc123_limit_state(session)
                )
            return {
                **state,
                "retry_guard_remaining_seconds": round(
                    self._sc123_limit_retry_guard_remaining(), 3
                ),
            }

    def sc123_temperature_limit_preview(
        self, username: str, value: object
    ) -> dict:
        """Issue a one-shot token only after positive firmware detection."""
        target_c = parse_sc123_limit_c(value)
        with self._sc123_limit_flow_lock:
            self._clear_sc123_limit_token()
            self._require_sc123_limit_worker()
            with self._sc123_limit_worker_session() as session:
                state, _capability_payload, storage_payload = (
                    self._sc123_limit_state(session)
                )
            blockers = list(state["blockers"])
            if state["pending_commands"]:
                blockers.append(
                    "aktive Block-50-Befehlsbits: "
                    + ", ".join(state["pending_commands"])
                )
            guard = self._sc123_limit_retry_guard_remaining()
            transition_required = not (
                state["configured"] and state["threshold_c"] == target_c
            )
            if guard > 0.0 and transition_required:
                blockers.append(
                    "vorheriger SC123-Schreibversuch wird nicht automatisch wiederholt; "
                    f"erneute Vorschau in {guard:.1f} s"
                )
            eligible = bool(state["available"] and not blockers)
            return {
                **state,
                "target_c": target_c,
                "transition_required": transition_required,
                "eligible": eligible,
                "blockers": blockers,
                "request_token": (
                    self._issue_sc123_limit_token(
                        username, target_c, storage_payload
                    )
                    if eligible else None
                ),
                "request_token_ttl_seconds": SC123_LIMIT_TOKEN_TTL_SECONDS,
                "retry_guard_remaining_seconds": round(guard, 3),
            }

    def set_sc123_temperature_limit(
        self,
        username: str,
        value: object,
        confirm: object,
        write_enabled: object,
        request_token: object,
    ) -> dict:
        """Apply one Auth-3 Block-60 write with RAM and NVM verification."""
        target_c = parse_sc123_limit_c(value)
        if confirm is not True:
            raise ValueError("SC123-Grenze muss ausdrücklich bestätigt werden")
        if write_enabled is not True:
            raise ValueError("Hardware-Schreiben muss ausdrücklich aktiviert werden")
        with self._sc123_limit_flow_lock:
            return self._set_sc123_temperature_limit_locked(
                username, target_c, request_token
            )

    def _set_sc123_temperature_limit_locked(
        self, username: str, target_c: int, request_token: object
    ) -> dict:
        issued = self._consume_sc123_limit_token(
            username, target_c, request_token
        )
        self._require_sc123_limit_worker()
        recorded_at = _now()
        audit = {
            "schema": SC123_LIMIT_SCHEMA,
            "operation": "sc123-temperature-limit-write",
            "critical": True,
            "cpu": 0,
            "block": SC123_LIMIT_STORAGE_BLOCK,
            "capability_block": SC123_LIMIT_CAPABILITY_BLOCK,
            "record_offset": SC123_LIMIT_RECORD_OFFSET,
            "record_length": SC123_LIMIT_RECORD_LENGTH,
            "offset": SC123_LIMIT_THRESHOLD_OFFSET,
            "persistent": True,
            "nvm_write": True,
            "nvm_read_request_hex": SC123_LIMIT_NVM_READ_REQUEST.hex(),
            "target_c": target_c,
            "before_c": None,
            "stored_before_record": None,
            "auth_level_requested": SC123_LIMIT_AUTH_LEVEL,
            "auth_level_granted": None,
            "auth_attempted": False,
            "auth_result_uncertain": False,
            "write_attempted": False,
            "write_count": 0,
            "written": False,
            "ack_positive": None,
            "readback_attempts": 0,
            "ram_readback_attempts": 0,
            "nvm_readback_attempts": 0,
            "ram_readback_ok": None,
            "nvm_readback_ok": None,
            "readback_ok": None,
            "readback_scope": "full-block-60-plus-service-04-nvm-record",
            "deauthenticated": None,
            "write_confirmed": False,
            "write_uncertain": False,
            "cleanup_uncertain": False,
            "uncertain": False,
            "unchanged": False,
            "changed": False,
            "changed_keys": ["Waermef_Ew.Reserve[0..5]/SC123-Temperaturgrenze"],
            "error": None,
        }
        try:
            with self._sc123_limit_worker_session() as session:
                state, _fresh_capability, fresh_storage = (
                    self._sc123_limit_state(session)
                )
                audit["capability"] = state
                audit["before_c"] = state["threshold_c"]
                audit["stored_before_record"] = state["record_hex"]
                if not state["available"]:
                    raise APIRequestConflictError(
                        "SC123-Grenze: einstellbare Firmware oder stabiler Speicher nicht sicher erkannt"
                    )
                if state["pending_commands"]:
                    raise APIRequestConflictError(
                        "SC123-Grenze: aktive Block-50-Befehlsbits: "
                        + ", ".join(state["pending_commands"])
                    )
                if (
                    self._sc123_storage_preimage(fresh_storage)
                    != issued["storage_preimage"]
                ):
                    raise APIRequestConflictError(
                        "SC123-Block-60-Speicher änderte sich seit der Vorschau; neu prüfen"
                    )
                if state["configured"] and state["threshold_c"] == target_c:
                    expected_record = fresh_storage[
                        SC123_LIMIT_RECORD_OFFSET:
                        SC123_LIMIT_RECORD_OFFSET + SC123_LIMIT_RECORD_LENGTH
                    ]
                    nvm_readback = self._poll_sc123_nvm_record(
                        session, expected_record
                    )
                    audit["nvm_readback"] = nvm_readback
                    audit["nvm_readback_attempts"] = int(
                        nvm_readback["attempts"]
                    )
                    audit["nvm_readback_ok"] = bool(nvm_readback["ok"])
                    if not nvm_readback["ok"]:
                        audit["ram_readback_ok"] = True
                        audit["readback_ok"] = False
                        raise TransportError(
                            "SC123-Grenze ist im RAM unverändert, aber im "
                            "persistenten NVM nicht bestätigt"
                        )
                    audit.update({
                        "unchanged": True,
                        "ram_readback_ok": True,
                        "nvm_readback_ok": True,
                        "readback_ok": True,
                        "write_confirmed": False,
                    })
                else:
                    guard = self._sc123_limit_retry_guard_remaining()
                    if guard > 0.0:
                        raise APIRequestConflictError(
                            "SC123-Schreibversuch wird nicht automatisch wiederholt; "
                            f"erneut in {guard:.1f} s"
                        )
                    granted_password = ""
                    auth_attempted = False
                    operation_error = None
                    cleanup_error = None
                    try:
                        try:
                            inputs = self.service.authentication_inputs(session)
                            granted_password = calculate_pw4(
                                inputs.serial_number, inputs.operating_hours
                            )
                            if not re.fullmatch(r"[0-9]{4}", granted_password):
                                raise ValueError
                        except Exception:
                            raise TransportError(
                                "SC123-Grenze: Auth-Schlüssel konnte nicht frisch abgeleitet werden"
                            ) from None
                        auth_attempted = True
                        audit["auth_attempted"] = True
                        try:
                            auth = self.service.authenticate(
                                session, SC123_LIMIT_AUTH_LEVEL, granted_password
                            )
                        except Exception:
                            audit["auth_result_uncertain"] = True
                            raise TransportError(
                                "SC123-Grenze: Authentifizierung konnte nicht bestätigt werden"
                            ) from None
                        audit["auth_level_granted"] = getattr(
                            auth, "granted_level", None
                        )
                        if not (
                            getattr(auth, "ok", False)
                            and audit["auth_level_granted"] == SC123_LIMIT_AUTH_LEVEL
                        ):
                            raise PermissionError("Auth-Level 3 wurde nicht exakt gewährt")

                        prewrite_state, _prewrite_capability, prewrite_storage = (
                            self._sc123_limit_state(session)
                        )
                        audit["prewrite_capability"] = prewrite_state
                        if not prewrite_state["available"]:
                            raise APIRequestConflictError(
                                "SC123-Firmwarefähigkeit oder Block-60-Stabilität unmittelbar vor dem Write verloren"
                            )
                        if prewrite_state["pending_commands"]:
                            raise APIRequestConflictError(
                                "SC123-Grenze: unmittelbar vor dem Write aktive Block-50-Befehlsbits"
                            )
                        if (
                            self._sc123_storage_preimage(prewrite_storage)
                            != issued["storage_preimage"]
                            or prewrite_storage != fresh_storage
                        ):
                            raise APIRequestConflictError(
                                "SC123-Block-60-Speicher änderte sich unmittelbar vor dem Write"
                            )
                        wire_payload = sc123_limit_payload(
                            prewrite_storage, target_c
                        )
                        expected_record = wire_payload[
                            SC123_LIMIT_RECORD_OFFSET:
                            SC123_LIMIT_RECORD_OFFSET + SC123_LIMIT_RECORD_LENGTH
                        ]
                        audit["before_hex"] = prewrite_storage.hex()
                        audit["after_hex"] = wire_payload.hex()
                        audit["expected_record_hex"] = expected_record.hex()
                        self._record_sc123_limit_attempt(target_c)
                        audit["write_attempted"] = True
                        audit["write_count"] = 1
                        response = session.write_block(
                            SC123_LIMIT_STORAGE_BLOCK,
                            wire_payload,
                            packet=None,
                            timeout=self.service.timeout,
                        )
                        audit["ack_positive"] = bool(
                            getattr(getattr(response, "ack", None), "positive", False)
                        )
                        if not audit["ack_positive"]:
                            raise TransportError(
                                "SC123-Grenze: Write erhielt kein positives ACK"
                            )
                        audit["written"] = True

                        audit["readback_attempts"] = 1
                        audit["ram_readback_attempts"] = 1
                        readback_payload = self._read_sc123_block(
                            session, SC123_LIMIT_STORAGE_BLOCK, "RAM-Rücklesung"
                        )
                        readback = sc123_limit_readback(
                            wire_payload, readback_payload
                        )
                        audit["readback"] = readback
                        audit["ram_readback_ok"] = bool(readback["ok"])
                        if not readback["ok"]:
                            raise TransportError(
                                "SC123-Grenze wurde in Block 60 nicht bytegenau rückgelesen"
                            )

                        nvm_readback = self._poll_sc123_nvm_record(
                            session, expected_record
                        )
                        audit["nvm_readback"] = nvm_readback
                        audit["nvm_readback_attempts"] = int(
                            nvm_readback["attempts"]
                        )
                        audit["nvm_readback_ok"] = bool(nvm_readback["ok"])
                        audit["readback_ok"] = bool(
                            audit["ram_readback_ok"]
                            and audit["nvm_readback_ok"]
                        )
                        if not nvm_readback["ok"]:
                            raise TransportError(
                                "SC123-Grenze wurde im persistenten NVM nicht bestätigt"
                            )
                    except Exception as exc:
                        operation_error = exc
                    finally:
                        if auth_attempted:
                            try:
                                self.service.deauthenticate(
                                    session, granted_password
                                )
                                audit["deauthenticated"] = True
                            except Exception:
                                audit["deauthenticated"] = False
                                cleanup_error = TransportError(
                                    "Auth-0-Cleanup konnte nicht bestätigt werden"
                                )
                    audit["write_confirmed"] = bool(
                        audit["written"]
                        and audit["ram_readback_ok"] is True
                        and audit["nvm_readback_ok"] is True
                    )
                    audit["changed"] = audit["write_confirmed"]
                    audit["write_uncertain"] = bool(
                        audit["write_attempted"] and not audit["write_confirmed"]
                    )
                    audit["cleanup_uncertain"] = bool(
                        auth_attempted and audit["deauthenticated"] is not True
                    )
                    audit["uncertain"] = bool(
                        audit["write_uncertain"] or audit["cleanup_uncertain"]
                    )
                    if operation_error is not None:
                        raise operation_error
                    if cleanup_error is not None:
                        raise cleanup_error
            cleanup_ok = bool(
                audit["auth_attempted"] is not True
                or audit["deauthenticated"] is True
            )
            result = {
                **audit,
                "ok": bool(
                    (audit["write_confirmed"] or audit["unchanged"])
                    and cleanup_ok
                    and not audit["error"]
                ),
            }
        except Exception as exc:
            audit["error"] = str(exc) or exc.__class__.__name__
            self.store.audit(recorded_at, username, SC123_LIMIT_STORAGE_BLOCK, audit)
            raise
        self.store.audit(recorded_at, username, SC123_LIMIT_STORAGE_BLOCK, result)
        return result

    def runtime_24h_write_preview(self) -> dict:
        """Return a fresh coherent counter value without authenticating."""
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        recorded_at = _now()
        with self.serial_lock, self.service.session() as session:
            current = self._read_runtime_24h(session, recorded_at)
        return {
            **current,
            "max_settable_seconds": RUNTIME_24H_MAX_SETTABLE_SECONDS,
            "prewrite_tolerance_seconds": RUNTIME_24H_PREWRITE_TOLERANCE_SECONDS,
            "readback_tolerance_seconds": RUNTIME_24H_READBACK_TOLERANCE_SECONDS,
        }

    def _runtime_24h_guarded_write(self, session, target: int, audit: dict) -> dict:
        """Auth 3 -> exact RAM write -> bounded readback -> Auth 0 for 0x0f0e.

        Shared by the manual dialog and the automatic reset. The caller holds
        ``serial_lock`` and the session and has validated the preimage.
        """
        target_bytes = int(target).to_bytes(RUNTIME_24H_COUNTER_LENGTH, "little")
        # Auth level and credential derivation are fixed server-side.
        # Passing no override makes the authentication service read
        # its current Block-20/22 inputs and calculate PW4 freshly.
        auth = self.service.authenticate(
            session, RUNTIME_24H_WRITE_AUTH_LEVEL, None
        )
        audit["auth_level_granted"] = getattr(auth, "granted_level", None)
        granted_password = str(getattr(auth, "pw4", "") or "")
        if not getattr(auth, "ok", False) or audit["auth_level_granted"] != 3:
            try:
                self.service.deauthenticate(session, granted_password)
                audit["deauthenticated"] = True
            except Exception as exc:
                audit["deauthenticated"] = False
                raise TransportError(
                    f"Auth-Level 3 wurde nicht exakt gewährt; Deauth fehlgeschlagen: {exc}"
                ) from exc
            raise PermissionError("Auth-Level 3 wurde nicht exakt gewährt")
        operation_error = None
        deauth_error = None
        readback = None
        try:
            audit["write_attempted"] = True
            response = session.request(
                RUNTIME_24H_WRITE_PREFIX + target_bytes,
                session.next_packet(),
                timeout=0.9,
                cpu=0,
            )
            audit["ack_positive"] = bool(
                getattr(getattr(response, "ack", None), "positive", False)
            )
            validate_runtime_24h_write_response(response)
            audit["written"] = True
            readback_response = session.request(
                RUNTIME_24H_REQUEST,
                session.next_packet(),
                timeout=0.9,
                cpu=0,
            )
            audit["readback_attempts"] = 1
            readback = runtime_24h_status(readback_response, _now())
            readback_counter = int(readback["counter_seconds"])
            audit["readback_advanced_seconds"] = readback_counter - target
            # The immediate readback may cross one active-second
            # tick after an otherwise exact volatile-RAM write.
            audit["readback_ok"] = target <= readback_counter <= (
                target + RUNTIME_24H_READBACK_TOLERANCE_SECONDS
            )
            if not audit["readback_ok"]:
                raise TransportError(
                    f"24-h-Zähler: Readback {readback['counter_seconds']} statt {target}"
                )
        except Exception as exc:
            operation_error = exc
        finally:
            try:
                self.service.deauthenticate(session, granted_password)
                audit["deauthenticated"] = True
            except Exception as exc:
                audit["deauthenticated"] = False
                deauth_error = exc
        if operation_error is not None:
            if deauth_error is not None:
                raise TransportError(
                    f"{operation_error}; zusätzlich Deauth fehlgeschlagen: {deauth_error}"
                ) from operation_error
            raise operation_error
        if deauth_error is not None:
            raise TransportError(f"MSR2-Deauth fehlgeschlagen: {deauth_error}")
        return readback

    def set_runtime_24h_counter(
        self,
        username: str,
        value: object,
        expected_counter: object,
        write_enabled: object,
    ) -> dict:
        """CAS-write only the volatile CPU-0 24-hour counter and read it back."""
        target = runtime_24h_write_seconds(value)
        if isinstance(expected_counter, bool) or not isinstance(expected_counter, int):
            raise ValueError("erwarteter 24-h-Zähler muss eine ganze Sekundenzahl sein")
        if not 0 <= expected_counter <= 0xFFFFFFFF:
            raise ValueError("erwarteter 24-h-Zähler liegt außerhalb des 4-Byte-Bereichs")
        if write_enabled is not True:
            raise ValueError("Hardware-Schreiben muss ausdrücklich aktiviert werden")
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")

        recorded_at = _now()
        target_bytes = target.to_bytes(RUNTIME_24H_COUNTER_LENGTH, "little")
        audit = {
            "operation": "runtime-24h-counter-write",
            "key": "CPU0.RAM.0x000f0e",
            "cpu": 0,
            "address": "0x000f0e",
            "volatile_ram_only": True,
            "nvm_write": False,
            "flag_write": False,
            "auth_level_requested": RUNTIME_24H_WRITE_AUTH_LEVEL,
            "auth_level_granted": None,
            "expected_counter": expected_counter,
            "prewrite_counter": None,
            "prewrite_tolerance_seconds": RUNTIME_24H_PREWRITE_TOLERANCE_SECONDS,
            "readback_tolerance_seconds": RUNTIME_24H_READBACK_TOLERANCE_SECONDS,
            "before_hex": "",
            "after_hex": target_bytes.hex(),
            "changed_keys": ["CPU0.RAM.0x000f0e"],
            "dry_run": False,
            "changed": False,
            "write_attempted": False,
            "written": False,
            "ack_positive": None,
            "readback_ok": None,
            "readback_scope": "exact-service-0x02",
            "readback_attempts": 0,
            "deauthenticated": None,
            "error": None,
        }
        try:
            with self.serial_lock, self.service.session() as session:
                before = self._read_runtime_24h(session, recorded_at)
                current = int(before["counter_seconds"])
                audit["prewrite_counter"] = current
                audit["before_hex"] = current.to_bytes(4, "little").hex()
                audit["changed"] = current != target
                audit["prewrite_validated_samples"] = int(before["validated_samples"])
                # The dashboard cache is refreshed in the ten-second slow
                # cycle. The dialog and guarded POST use fresh direct reads.
                # Accept only bounded monotonic preimage progress. This bound
                # never modifies the absolute target supplied by the admin.
                if not expected_counter <= current <= (
                    expected_counter + RUNTIME_24H_PREWRITE_TOLERANCE_SECONDS
                ):
                    raise APIRequestConflictError(
                        f"24-h-Zähler hat sich von {expected_counter} auf {current} geändert; "
                        "Istwert neu lesen und Hardware-Schreiben erneut freigeben"
                    )
                if current == target:
                    audit["readback_ok"] = True
                    result = {**audit, "counter_seconds": current, "unchanged": True}
                else:
                    readback = self._runtime_24h_guarded_write(session, target, audit)
                    result = {**audit, **readback, "unchanged": False}
                    with self.state_lock:
                        self.runtime_24h = {**readback, "error": None}
        except Exception as exc:
            audit["error"] = str(exc)
            self.store.audit(recorded_at, username, 0, audit)
            raise
        self.store.audit(recorded_at, username, 0, result)
        return result

    # ------------------------------------------------------------------
    # Controller automation: clock sync (A), 0x0f0e auto-reset (B) and the
    # real runtime since start (C). Rules live in controller_automation.py.
    # ------------------------------------------------------------------
    def _timedatectl_ntp_status(self) -> tuple[bool, str]:
        """ODM time is only trusted when systemd reports NTPSynchronized=yes."""
        try:
            completed = subprocess.run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"timedatectl nicht verfügbar: {exc}"
        value = (completed.stdout or "").strip()
        return value == "yes", f"NTPSynchronized={value or 'leer'}"

    def _load_automation_json(self, path: Path) -> dict | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _load_clock_sync_settings(self) -> dict:
        payload = self._load_automation_json(self.clock_sync_settings_path)
        if payload is None:
            return {**CLOCK_SYNC_DEFAULTS, "updated_by": None, "updated_at": None}
        try:
            settings = normalize_clock_sync_settings(payload)
        except ValueError:
            # A damaged file never enables a background write.
            return {**CLOCK_SYNC_DEFAULTS, "updated_by": None, "updated_at": None}
        return {
            **settings,
            "updated_by": payload.get("updated_by"),
            "updated_at": payload.get("updated_at"),
        }

    def _load_runtime_auto_settings(self) -> dict:
        payload = self._load_automation_json(self.runtime_auto_settings_path)
        defaults = {
            **RUNTIME_AUTO_DEFAULTS,
            "min_runtime_minutes_at_save": None,
            "updated_by": None,
            "updated_at": None,
            "disabled_reason": None,
        }
        if payload is None:
            return defaults
        minutes = payload.get("min_runtime_minutes_at_save")
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            minutes = None
        try:
            rule = normalize_runtime_auto_rule(payload, minutes)
        except ValueError:
            return defaults
        return {
            **rule,
            "min_runtime_minutes_at_save": minutes,
            "updated_by": payload.get("updated_by"),
            "updated_at": payload.get("updated_at"),
            "disabled_reason": payload.get("disabled_reason"),
        }

    def _cached_block50(self, max_age: float = 60.0) -> bytes | None:
        with self.state_lock:
            cached = self._display_slow_payloads.get(CONTROLLER_CLOCK_BLOCK)
        if not cached or cached[0] is None:
            return None
        payload, read_mono = cached
        if time.monotonic() - read_mono > max_age or len(payload) != CLOCK_SYNC_PAYLOAD_LENGTH:
            return None
        return bytes(payload)

    def _cached_min_runtime_minutes(self) -> int | None:
        payload = self._cached_block50()
        if payload is None:
            return None
        return int(payload[RUNTIME_AUTO_MIN_RUNTIME_OFFSET])

    def _read_block_raws(self, session, block: int) -> tuple[bytes, dict]:
        result, fields = self.service.decoded_block(session, block)
        if not result.ok:
            raise TransportError(f"Block {block}: Status {result.status!r}")
        raws = {}
        for field in fields:
            raw = field.raw
            if isinstance(raw, bool):
                continue
            try:
                raws[field.key] = int(raw)
            except (TypeError, ValueError):
                continue
        return bytes(result.payload), raws

    # --- A) Regleruhr-Sync -------------------------------------------------
    def controller_clock_sync_status(self) -> dict:
        now = self._utcnow()
        with self.state_lock:
            settings = dict(self.clock_sync_config)
            state = json.loads(json.dumps(self.clock_sync_state))
        return {
            "schema": "open-dachs-controller-clock-sync/v1",
            "settings": settings,
            "last_run": state.get("last_run"),
            "last_auto_date": state.get("last_auto_date"),
            "next_run": next_clock_sync(settings, now, state.get("last_auto_date")),
            "path": "Block 50 Offset 36 (Hka_Ew.ulSystemTime) + Kommando-Bit 5 in Offset 0",
            "auth_level": CLOCK_SYNC_AUTH_LEVEL,
            "readback_tolerance_seconds": CLOCK_SYNC_READBACK_TOLERANCE_SECONDS,
        }

    def set_controller_clock_sync_settings(self, username: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Einstellungen für den Uhrabgleich fehlen")
        forbidden = sorted({"auth_level", "pass4", "pw4"}.intersection(payload))
        if forbidden:
            raise ValueError("clientseitige Auth-Felder sind nicht zulässig: " + ", ".join(forbidden))
        settings = normalize_clock_sync_settings(payload)
        with self.state_lock:
            was_enabled = bool(self.clock_sync_config.get("enabled"))
        if settings["enabled"] and not was_enabled and payload.get("acknowledge") is not True:
            raise ValueError(
                "Aktivieren erfordert die Bestätigung, dass ODM die Regleruhr täglich "
                "ohne Bediener mit Auth 3 stellen darf"
            )
        stored = {**settings, "updated_by": str(username), "updated_at": _now()}
        write_json_atomic(self.clock_sync_settings_path, {"version": 1, **stored})
        with self.state_lock:
            self.clock_sync_config = stored
        return self.controller_clock_sync_status()

    def _save_clock_sync_state(self) -> None:
        with self.state_lock:
            state = json.loads(json.dumps(self.clock_sync_state))
        write_json_atomic(self.clock_sync_state_path, {"version": 1, **state})

    def _clock_sync_tick(self) -> dict | None:
        now = self._utcnow()
        with self.state_lock:
            settings = dict(self.clock_sync_config)
            last_auto_date = self.clock_sync_state.get("last_auto_date")
        due, today = clock_sync_due(settings, now, last_auto_date)
        if not due:
            return None
        # Persist the daily marker before any serial access: a crash or
        # restart during the run can never produce a second run that day.
        with self.state_lock:
            self.clock_sync_state["last_auto_date"] = today
        self._save_clock_sync_state()
        return self.sync_controller_clock(CONTROLLER_AUTOMATION_USER, "auto")

    def sync_controller_clock(self, username: str, trigger: str) -> dict:
        if trigger not in {"auto", "manual"}:
            raise ValueError("unbekannter Auslöser")
        with self.automation_lock:
            record = self._sync_controller_clock_locked(str(username), trigger)
        with self.state_lock:
            self.clock_sync_state["last_run"] = record
        self._save_clock_sync_state()
        return record

    def _sync_controller_clock_locked(self, username: str, trigger: str) -> dict:
        started = self._utcnow()
        with self.state_lock:
            settings = dict(self.clock_sync_config)
            serial_enabled = self.serial_enabled
        record = {
            "at": started.isoformat(),
            "trigger": trigger,
            "username": username,
            "result": None,
            "reason": None,
            "ntp": None,
            "offset_before_seconds": None,
            "offset_after_seconds": None,
            "controller_time_before": None,
            "controller_time_after": None,
            "write_attempted": False,
            "uncertain": False,
            "audit_written": False,
        }

        def finish(result: str, reason: str | None) -> dict:
            record["result"] = result
            record["reason"] = reason
            return record

        if trigger == "auto" and not settings.get("enabled"):
            return finish("übersprungen", "Automatik ausgeschaltet")
        if not serial_enabled:
            return finish("übersprungen", "serielle Verbindung ist getrennt")
        ntp_ok, ntp_detail = self._ntp_status()
        record["ntp"] = ntp_detail
        if not ntp_ok:
            return finish("übersprungen", f"ODM-Zeit nicht NTP-synchron ({ntp_detail})")
        threshold = int(settings["threshold_seconds"])
        max_offset = int(settings["max_offset_seconds"])
        audit: dict | None = None
        recorded_at = _now()
        try:
            with self.serial_lock, self.service.session() as session:
                first = self.service.read_block(session, CLOCK_SYNC_BLOCK)
                read_at = self._utcnow()
                if not first.ok or len(bytes(first.payload)) != CLOCK_SYNC_PAYLOAD_LENGTH:
                    return finish("fehlgeschlagen", "Block 50 nicht lesbar")
                first_payload = bytes(first.payload)
                raw_before = decode_controller_time(first_payload)
                controller_before = controller_naive(raw_before)
                odm_before = odm_local_naive(read_at)
                offset = int(round((controller_before - odm_before).total_seconds()))
                record["offset_before_seconds"] = offset
                record["controller_time_before"] = controller_before.isoformat()
                if first_payload[CLOCK_SYNC_COMMAND_OFFSET] & CLOCK_SYNC_COMMAND_BIT:
                    return finish("fehlgeschlagen", "Kommando-Bit 5 in Block 50 ist bereits gesetzt")
                if first_payload[CLOCK_SYNC_GUARD_OFFSET] == CLOCK_SYNC_GUARD_REFUSED_VALUE:
                    return finish(
                        "übersprungen",
                        "Firmware-Sperrbedingung möglich (0x202f = 4); Uhrübernahme nicht gesichert",
                    )
                if abs(offset) < threshold:
                    return finish("übersprungen", f"Versatz {offset:+d} s unter Schwelle {threshold} s")
                if abs(offset) > max_offset:
                    return finish(
                        "fehlgeschlagen",
                        f"Versatz {offset:+d} s über Maximum {max_offset} s; bitte manuell prüfen",
                    )
                _payload22, raws22 = self._read_block_raws(session, 22)
                starts = raws22.get("Hka_Bd.ulAnzahlStarts")
                window_reason = clock_sync_window_reason(
                    controller_before, odm_before, read_at, starts
                )
                if window_reason:
                    return finish("übersprungen", window_reason)
                audit = {
                    "operation": "controller-clock-sync",
                    "trigger": trigger,
                    "key": "Hka_Ew.ulSystemTime",
                    "cpu": 0,
                    "block": CLOCK_SYNC_BLOCK,
                    "path": "block50-offset36+command-bit5",
                    "firmware_evidence": "f47293 BTST 5,0x1acc -> 0x1af0->0x163e -> f464e1 (RAM+RTC) -> BCLR 5",
                    "auth_level_requested": CLOCK_SYNC_AUTH_LEVEL,
                    "auth_level_granted": None,
                    "ntp": ntp_detail,
                    "threshold_seconds": threshold,
                    "starts": starts,
                    "night_shutdown_second": night_shutdown_second(starts),
                    "controller_time_before": controller_before.isoformat(),
                    "odm_time_before": odm_before.isoformat(),
                    "offset_before_seconds": offset,
                    "before_hex": "",
                    "after_hex": "",
                    "target_raw": None,
                    "changed_keys": ["Hka_Ew.ulSystemTime"],
                    "dry_run": False,
                    "write_attempted": False,
                    "written": False,
                    "ack_positive": None,
                    "readback_ok": None,
                    "readback_tolerance_seconds": CLOCK_SYNC_READBACK_TOLERANCE_SECONDS,
                    "controller_time_after": None,
                    "offset_after_seconds": None,
                    "deauthenticated": None,
                    "error": None,
                }
                auth = self.service.authenticate(session, CLOCK_SYNC_AUTH_LEVEL, None)
                audit["auth_level_granted"] = getattr(auth, "granted_level", None)
                granted_password = str(getattr(auth, "pw4", "") or "")
                if not getattr(auth, "ok", False) or audit["auth_level_granted"] != CLOCK_SYNC_AUTH_LEVEL:
                    try:
                        self.service.deauthenticate(session, granted_password)
                        audit["deauthenticated"] = True
                    except Exception as exc:
                        audit["deauthenticated"] = False
                        raise TransportError(
                            f"Auth-Level 3 wurde nicht exakt gewährt; Deauth fehlgeschlagen: {exc}"
                        ) from exc
                    raise PermissionError("Auth-Level 3 wurde nicht exakt gewährt")
                operation_error = None
                deauth_error = None
                try:
                    second = self.service.read_block(session, CLOCK_SYNC_BLOCK)
                    second_payload = bytes(second.payload) if second.ok else b""
                    if len(second_payload) != CLOCK_SYNC_PAYLOAD_LENGTH:
                        raise TransportError("Block 50 vor dem Write nicht lesbar")
                    if second_payload[CLOCK_SYNC_COMMAND_OFFSET] & CLOCK_SYNC_COMMAND_BIT:
                        raise TransportError("Kommando-Bit 5 vor dem Write gesetzt")
                    if not stable_block50_equal(first_payload, second_payload):
                        raise APIRequestConflictError("Block 50 hat sich seit der Prüfung geändert")
                    write_at = self._utcnow()
                    target_raw = controller_raw(odm_local_naive(write_at))
                    after = clock_sync_payload(second_payload, target_raw)
                    audit["target_raw"] = target_raw
                    audit["before_hex"] = second_payload.hex(" ").upper()
                    audit["after_hex"] = after.hex(" ").upper()
                    audit["write_attempted"] = True
                    record["write_attempted"] = True
                    response = session.write_block(
                        CLOCK_SYNC_BLOCK, after, packet=None, timeout=self.service.timeout
                    )
                    audit["ack_positive"] = bool(
                        getattr(getattr(response, "ack", None), "positive", False)
                    )
                    if not audit["ack_positive"]:
                        raise TransportError("Uhr-Write erhielt kein positives ACK")
                    audit["written"] = True
                    readback = self.service.read_block(session, CLOCK_SYNC_BLOCK)
                    readback_at = self._utcnow()
                    readback_payload = bytes(readback.payload) if readback.ok else b""
                    if len(readback_payload) != CLOCK_SYNC_PAYLOAD_LENGTH:
                        raise TransportError("Readback Block 50 nicht lesbar")
                    controller_after = controller_naive(decode_controller_time(readback_payload))
                    offset_after = int(round(
                        (controller_after - odm_local_naive(readback_at)).total_seconds()
                    ))
                    audit["controller_time_after"] = controller_after.isoformat()
                    audit["offset_after_seconds"] = offset_after
                    record["controller_time_after"] = controller_after.isoformat()
                    record["offset_after_seconds"] = offset_after
                    if readback_payload[CLOCK_SYNC_COMMAND_OFFSET] & CLOCK_SYNC_COMMAND_BIT:
                        raise TransportError(
                            "Kommando-Bit 5 nach dem Write noch gesetzt; Firmware hat die Uhr nicht übernommen"
                        )
                    if not stable_block50_equal(second_payload, readback_payload):
                        raise TransportError("Block 50 nach dem Write in weiteren Bytes verändert")
                    audit["readback_ok"] = abs(offset_after) <= CLOCK_SYNC_READBACK_TOLERANCE_SECONDS
                    if not audit["readback_ok"]:
                        raise TransportError(
                            f"Readback: Versatz {offset_after:+d} s außerhalb ±"
                            f"{CLOCK_SYNC_READBACK_TOLERANCE_SECONDS} s"
                        )
                except Exception as exc:
                    operation_error = exc
                finally:
                    try:
                        self.service.deauthenticate(session, granted_password)
                        audit["deauthenticated"] = True
                    except Exception as exc:
                        audit["deauthenticated"] = False
                        deauth_error = exc
                if operation_error is not None:
                    if deauth_error is not None:
                        raise TransportError(
                            f"{operation_error}; zusätzlich Deauth fehlgeschlagen: {deauth_error}"
                        ) from operation_error
                    raise operation_error
                if deauth_error is not None:
                    raise TransportError(f"MSR2-Deauth fehlgeschlagen: {deauth_error}")
        except Exception as exc:
            if audit is None:
                return finish("fehlgeschlagen", str(exc))
            audit["error"] = str(exc)
            self.store.audit(recorded_at, username, CLOCK_SYNC_BLOCK, audit)
            record["audit_written"] = True
            confirmed = bool(audit.get("readback_ok"))
            record["uncertain"] = bool(audit["write_attempted"]) and not confirmed
            if record["uncertain"] and trigger == "auto":
                self._disable_clock_sync("Uhr-Write unsicher; Automatik bis zur Prüfung ausgeschaltet")
            return finish("fehlgeschlagen", str(exc))
        self.store.audit(recorded_at, username, CLOCK_SYNC_BLOCK, audit)
        record["audit_written"] = True
        return finish(
            "synchronisiert",
            f"Versatz {record['offset_before_seconds']:+d} s → {record['offset_after_seconds']:+d} s",
        )

    def _disable_clock_sync(self, reason: str) -> None:
        with self.state_lock:
            stored = {**self.clock_sync_config, "enabled": False,
                      "updated_by": CONTROLLER_AUTOMATION_USER, "updated_at": _now(),
                      "disabled_reason": reason}
            self.clock_sync_config = stored
        write_json_atomic(self.clock_sync_settings_path, {"version": 1, **stored})

    def _controller_clock_sync_summary(self) -> dict:
        with self.state_lock:
            settings = self.clock_sync_config
            last = self.clock_sync_state.get("last_run") or {}
            return {
                "enabled": bool(settings.get("enabled")),
                "time": settings.get("time"),
                "disabled_reason": settings.get("disabled_reason"),
                "last_result": last.get("result"),
                "last_at": last.get("at"),
                "last_reason": last.get("reason"),
            }

    # --- B) Automatische Rücksetzung 0x0f0e ---------------------------------
    def _runtime_auto_reset_summary(self) -> dict:
        with self.state_lock:
            settings = self.runtime_auto_config
            live = dict(self.runtime_auto_live)
            last = self.runtime_auto_state.get("last_result") or {}
            return {
                "enabled": bool(settings.get("enabled")),
                "threshold_seconds": settings.get("threshold_seconds"),
                "target_seconds": settings.get("target_seconds"),
                "max_resets_per_run": settings.get("max_resets_per_run"),
                "disabled_reason": settings.get("disabled_reason"),
                "phase": live.get("phase"),
                "reason": live.get("reason"),
                "last_result": last.get("result"),
                "last_at": last.get("at"),
            }

    def runtime_auto_reset_status(self) -> dict:
        with self.state_lock:
            settings = dict(self.runtime_auto_config)
            state = json.loads(json.dumps(self.runtime_auto_state))
            status = dict(self.runtime_auto_live)
        minutes = self._cached_min_runtime_minutes()
        run_key = None
        with suppress(Exception):
            run_key = self.real_runtime().get("run_key")
        resets = int(state.get("resets_in_run") or 0) if state.get("run_key") == run_key and run_key else 0
        return {
            "schema": "open-dachs-runtime-auto-reset/v1",
            "settings": settings,
            "phase": status.get("phase", "disabled" if not settings["enabled"] else "watching"),
            "reason": status.get("reason"),
            "checked_at": status.get("checked_at"),
            "last_write_at": state.get("last_write_at"),
            "last_result": state.get("last_result"),
            "resets_in_run": resets,
            "run_key": run_key,
            "limits": {
                "min_target_seconds": runtime_auto_min_target(minutes),
                "min_runtime_minutes": minutes,
                "universal_target_seconds": RUNTIME_AUTO_UNIVERSAL_TARGET_SECONDS,
                "min_gap_seconds": RUNTIME_AUTO_MIN_GAP_SECONDS,
                "max_threshold_seconds": RUNTIME_AUTO_MAX_THRESHOLD_SECONDS,
                "max_resets_per_run": RUNTIME_AUTO_MAX_RESETS,
                "rate_limit_seconds": RUNTIME_AUTO_RATE_LIMIT_SECONDS,
            },
            "defaults": dict(RUNTIME_AUTO_DEFAULTS),
        }

    def set_runtime_auto_reset_settings(self, username: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Regel für die automatische Rücksetzung fehlt")
        forbidden = sorted({"auth_level", "pass4", "pw4", "address"}.intersection(payload))
        if forbidden:
            raise ValueError("unzulässige Felder: " + ", ".join(forbidden))
        minutes = self._cached_min_runtime_minutes()
        rule = normalize_runtime_auto_rule(payload, minutes)
        with self.state_lock:
            was_enabled = bool(self.runtime_auto_config.get("enabled"))
        if rule["enabled"] and not was_enabled and payload.get("acknowledge") is not True:
            raise ValueError(
                "Aktivieren erfordert die Bestätigung, dass ODM den Zähler 0x0f0e ohne "
                "Bediener mit Auth 3 schreiben darf"
            )
        stored = {
            **rule,
            "min_runtime_minutes_at_save": minutes,
            "updated_by": str(username),
            "updated_at": _now(),
            "disabled_reason": None,
        }
        write_json_atomic(self.runtime_auto_settings_path, {"version": 1, **stored})
        with self.state_lock:
            self.runtime_auto_config = stored
            self.runtime_auto_live = {
                "phase": "watching" if rule["enabled"] else "disabled",
                "reason": None,
                "checked_at": _now(),
            }
            history = list(self.runtime_auto_state.get("changes") or [])[-19:]
            history.append({
                "at": stored["updated_at"], "by": stored["updated_by"],
                "enabled": rule["enabled"], "rule": {k: rule[k] for k in (
                    "threshold_seconds", "target_seconds", "max_resets_per_run")},
            })
            self.runtime_auto_state["changes"] = history
        self._save_runtime_auto_state()
        return self.runtime_auto_reset_status()

    def _save_runtime_auto_state(self) -> None:
        with self.state_lock:
            state = json.loads(json.dumps(self.runtime_auto_state))
        write_json_atomic(self.runtime_auto_state_path, {"version": 1, **state})

    def _disable_runtime_auto(self, reason: str) -> None:
        with self.state_lock:
            stored = {**self.runtime_auto_config, "enabled": False,
                      "updated_by": CONTROLLER_AUTOMATION_USER, "updated_at": _now(),
                      "disabled_reason": reason}
            self.runtime_auto_config = stored
        write_json_atomic(self.runtime_auto_settings_path, {"version": 1, **stored})

    def _set_runtime_auto_live(self, phase: str, reason: str | None) -> dict:
        live = {"phase": phase, "reason": reason, "checked_at": _now()}
        with self.state_lock:
            self.runtime_auto_live = live
        return live

    def _runtime_auto_reset_tick(self) -> dict:
        with self.automation_lock:
            return self._runtime_auto_reset_locked()

    def _runtime_auto_reset_locked(self) -> dict:
        now = self._utcnow()
        with self.state_lock:
            settings = dict(self.runtime_auto_config)
            state = json.loads(json.dumps(self.runtime_auto_state))
            runtime = dict(self.runtime_24h)
            serial_enabled = self.serial_enabled
        counter = None
        if runtime.get("available") is True and runtime.get("counter_seconds") is not None:
            recorded = DachsStore._parse_recorded_at(runtime.get("recorded_at"))
            if recorded is not None and 0 <= (now - recorded).total_seconds() <= RUNTIME_AUTO_COUNTER_MAX_AGE_SECONDS:
                counter = int(runtime["counter_seconds"])
        real = self.real_runtime()
        run_key = real.get("run_key") if real.get("running") else None
        phase, reason = runtime_auto_precheck(settings, state, counter, run_key, now)
        if phase != "due":
            return self._set_runtime_auto_live(phase, reason)
        if time.monotonic() < self._runtime_auto_backoff_until:
            with self.state_lock:
                previous = self.runtime_auto_live.get("reason")
            return self._set_runtime_auto_live("blocked", previous or "Wartezeit nach abgelehnter Prüfung")
        if not serial_enabled:
            return self._set_runtime_auto_live("blocked", "serielle Verbindung ist getrennt")
        target = int(settings["target_seconds"])
        rule = {key: settings[key] for key in ("threshold_seconds", "target_seconds", "max_resets_per_run")}
        recorded_at = _now()
        used = int(state.get("resets_in_run") or 0) if state.get("run_key") == run_key and run_key else 0
        audit = {
            "operation": "runtime-auto-reset",
            "key": "CPU0.RAM.0x000f0e",
            "cpu": 0,
            "address": "0x000f0e",
            "volatile_ram_only": True,
            "nvm_write": False,
            "flag_write": False,
            "rule": rule,
            "run_key": run_key,
            "reset_index_in_run": used + 1,
            "eligibility": None,
            "auth_level_requested": RUNTIME_24H_WRITE_AUTH_LEVEL,
            "auth_level_granted": None,
            "prewrite_counter": None,
            "readback_tolerance_seconds": RUNTIME_24H_READBACK_TOLERANCE_SECONDS,
            "before_hex": "",
            "after_hex": target.to_bytes(4, "little").hex(),
            "changed_keys": ["CPU0.RAM.0x000f0e"],
            "dry_run": False,
            "changed": True,
            "write_attempted": False,
            "written": False,
            "ack_positive": None,
            "readback_ok": None,
            "readback_scope": "exact-service-0x02",
            "readback_attempts": 0,
            "deauthenticated": None,
            "error": None,
        }
        attempted = False
        try:
            with self.serial_lock, self.service.session() as session:
                _p24, raws24 = self._read_block_raws(session, MOTOR_STATUS_BLOCK)
                _p22, raws22 = self._read_block_raws(session, 22)
                manual = self._read_ram_fields(session, (RUNTIME_AUTO_MANUAL_MODE_FIELD,), recorded_at)[
                    RUNTIME_AUTO_MANUAL_MODE_FIELD["key"]
                ]
                block50 = self.service.read_block(session, CLOCK_SYNC_BLOCK)
                block50_payload = bytes(block50.payload) if block50.ok else b""
                minutes = (
                    int(block50_payload[RUNTIME_AUTO_MIN_RUNTIME_OFFSET])
                    if len(block50_payload) == CLOCK_SYNC_PAYLOAD_LENGTH
                    else None
                )
                current = self._read_runtime_24h(session, recorded_at)
                counter_now = int(current["counter_seconds"])
                # Re-read the motor state immediately before authentication.
                _p24b, raws24b = self._read_block_raws(session, MOTOR_STATUS_BLOCK)
                statuses = [raws24.get(MOTOR_STATUS_KEY), raws24b.get(MOTOR_STATUS_KEY)]
                motor_status = None if None in statuses else min(statuses)
                eligibility = {
                    "motor_status": statuses,
                    "service_code": raws22.get("Hka_Bd.bStoerung"),
                    "manual_mode": manual.get("value") if manual.get("available") else None,
                    "min_runtime_minutes": minutes,
                    "counter_seconds": counter_now,
                    "validated_samples": int(current.get("validated_samples") or 0),
                }
                audit["eligibility"] = eligibility
                audit["prewrite_counter"] = counter_now
                audit["before_hex"] = counter_now.to_bytes(4, "little").hex()
                block_reason = runtime_auto_eligibility(
                    settings,
                    motor_status=motor_status,
                    service_code=eligibility["service_code"],
                    manual_mode=eligibility["manual_mode"],
                    min_runtime_minutes=minutes,
                    counter_seconds=counter_now,
                )
                if block_reason:
                    self._runtime_auto_backoff_until = time.monotonic() + RUNTIME_AUTO_BACKOFF_SECONDS
                    return self._set_runtime_auto_live("blocked", block_reason)
                # Persist the attempt before authenticating: rate limit and
                # per-run count hold even if the process dies mid-write.
                attempted = True
                with self.state_lock:
                    self.runtime_auto_state.update({
                        "last_write_at": now.isoformat(),
                        "run_key": run_key,
                        "resets_in_run": used + 1,
                        "last_result": {"at": now.isoformat(), "result": "läuft", "rule": rule},
                    })
                self._save_runtime_auto_state()
                self._set_runtime_auto_live("executing", None)
                readback = self._runtime_24h_guarded_write(session, target, audit)
                with self.state_lock:
                    self.runtime_24h = {**readback, "error": None}
        except Exception as exc:
            if not attempted:
                self._runtime_auto_backoff_until = time.monotonic() + RUNTIME_AUTO_BACKOFF_SECONDS
                return self._set_runtime_auto_live("blocked", f"Prüfung fehlgeschlagen: {exc}")
            audit["error"] = str(exc)
            self.store.audit(recorded_at, CONTROLLER_AUTOMATION_USER, 0, audit)
            uncertain = bool(audit["write_attempted"]) and not audit.get("readback_ok")
            cleanup_failed = audit.get("deauthenticated") is False
            with self.state_lock:
                self.runtime_auto_state["last_result"] = {
                    "at": now.isoformat(), "result": "fehlgeschlagen", "rule": rule,
                    "error": str(exc), "uncertain": uncertain, "cleanup_failed": cleanup_failed,
                }
            self._save_runtime_auto_state()
            if uncertain or cleanup_failed:
                self._disable_runtime_auto(
                    "Write/Deauth unsicher; Automatik bis zur Prüfung ausgeschaltet"
                )
                return self._set_runtime_auto_live("blocked", "unsicher: " + str(exc))
            return self._set_runtime_auto_live("blocked", str(exc))
        self.store.audit(recorded_at, CONTROLLER_AUTOMATION_USER, 0, {**audit, **readback})
        with self.state_lock:
            self.runtime_auto_state["last_result"] = {
                "at": now.isoformat(), "result": "gesetzt", "rule": rule,
                "before": audit["prewrite_counter"], "after": readback.get("counter_seconds"),
                "reset_index_in_run": used + 1,
            }
        self._save_runtime_auto_state()
        return self._set_runtime_auto_live("confirmed", None)

    # --- C) Echte Laufzeit seit Start ---------------------------------------
    def real_runtime(self) -> dict:
        now = self._utcnow()
        now_mono = time.monotonic()
        with self.state_lock:
            status_row = self.live_values.get((MOTOR_STATUS_BLOCK, MOTOR_STATUS_KEY))
            seconds_row = self.live_values.get((22, "Hka_Bd.ulBetriebssekunden"))
            counter_row = self.live_values.get((24, "Hka_Mw1.ulMotorlaufsekunden"))
            runtime = dict(self.runtime_24h)
            cached = self._real_runtime_history_cache

        def fresh_raw(row) -> int | None:
            if not row:
                return None
            recorded = DachsStore._parse_recorded_at(row.get("recorded_at"))
            if recorded is None or not 0 <= (now - recorded).total_seconds() <= REAL_RUNTIME_LIVE_MAX_AGE_SECONDS:
                return None
            raw = row.get("raw")
            if isinstance(raw, bool):
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        motor_status = fresh_raw(status_row)
        counter = fresh_raw(counter_row)
        if counter is None and runtime.get("available") is True and runtime.get("counter_seconds") is not None:
            counter = int(runtime["counter_seconds"])
        if cached is None or now_mono - cached[0] > REAL_RUNTIME_CACHE_SECONDS or cached[2] != motor_status:
            try:
                history = self.store.real_runtime_history()
            except Exception as exc:
                return {**real_runtime_status(
                    now_utc=now, motor_status=None, history={},
                    operating_seconds_now=None, counter_0f0e=counter,
                ), "error": f"Historie: {exc}"}
            with self.state_lock:
                self._real_runtime_history_cache = (now_mono, history, motor_status)
        else:
            history = cached[1]
        return real_runtime_status(
            now_utc=now,
            motor_status=motor_status,
            history=history,
            operating_seconds_now=fresh_raw(seconds_row),
            counter_0f0e=counter,
        )

    def _automation_loop(self) -> None:
        while not self.stop_event.wait(AUTOMATION_TICK_SECONDS):
            try:
                self._clock_sync_tick()
            except Exception as exc:
                with self.state_lock:
                    self.clock_sync_state["tick_error"] = str(exc)
            try:
                self._runtime_auto_reset_tick()
            except Exception as exc:
                self._set_runtime_auto_live("blocked", f"interner Fehler: {exc}")

    def write_block(self, username: str, block: int, changes: list[dict], auth_level: int, pass4: str, write_enabled: bool) -> dict:
        if not isinstance(write_enabled, bool):
            raise ValueError("write_enabled muss true oder false sein")
        if write_enabled:
            auth_level = _validated_auth_level(auth_level, "Auth-Level für Hardware-Schreiben")
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        block = validate_block(block, writable=True)
        if not changes:
            raise ValueError("keine Änderungen übergeben")
        allowlist = WriteAllowlist()
        with self.serial_lock, self.service.session() as session:
            auth = None
            current = self.service.read_block(session, block)
            if not current.ok:
                raise RuntimeError(f"Block {block} konnte nicht gelesen werden")
            before = bytes(current.payload)
            after = bytearray(before)
            changed_keys = []
            for change in changes:
                key = str(change.get("key", ""))
                presentation_groups = self.pack.presentation_groups(block)
                if key in presentation_groups:
                    self.pack.encode_value(after, key, str(change.get("value", "")), block=block)
                    changed_keys.extend(presentation_groups[key]["components"])
                    continue
                if key not in self.pack.field_map(block):
                    raise KeyError(f"Feld {key!r} ist in Block {block} nicht vorhanden")
                self.pack.encode_value(after, key, str(change.get("value", "")), block=block)
                changed_keys.append(key)
            validate_generic_write_payload(0, block, before, bytes(after))
            self._validate_power_target_changes(session, block, changes, bytes(after))
            if write_enabled and auth_level >= 0 and bytes(after) != before:
                auth = self.service.authenticate(session, auth_level, pass4 or None)
                if not auth.ok:
                    raise PermissionError(f"Auth-Level {auth_level} wurde nicht gewährt")
            audit = self.service.write_payload(session, block, before, bytes(after), changed_keys, allowlist, dry_run=not write_enabled)
        payload = audit.as_dict()
        payload["auth_level_requested"] = auth_level
        payload["auth_level_granted"] = getattr(auth, "granted_level", None)
        self.store.audit(_now(), username, block, payload)
        return payload

    def write_network_protection(
        self,
        username: str,
        cpu: int,
        changes: list[dict],
        auth_level: int,
        pass4: str,
        write_enabled: bool,
        *,
        block: int = NETWORK_PROTECTION_BLOCK,
    ) -> dict:
        """Reject writes unless a reviewed network block is explicitly writable."""
        if not isinstance(write_enabled, bool):
            raise ValueError("write_enabled muss true oder false sein")
        if write_enabled:
            auth_level = _validated_auth_level(auth_level, "Auth-Level für Hardware-Schreiben")
        cpu = validate_network_cpu(cpu)
        block = validate_network_block(block)
        if not network_protection_schema(cpu, block)["writable"]:
            raise PermissionError(
                f"Netzschutz CPU {cpu}, Block {block} ist nur lesbar"
            )
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        if not changes:
            raise ValueError("keine Änderungen übergeben")
        allowlist = WriteAllowlist()
        with self.serial_lock, self.service.session() as session:
            auth = None
            current = self.service.read_block(session, block, cpu=cpu)
            if not current.ok:
                raise RuntimeError(
                    f"Netzschutz CPU {cpu}, Block {block} konnte nicht gelesen werden"
                )
            before = bytes(current.payload)
            # Decode first so unexpected/short controller layouts can never be
            # edited merely because the transport returned a status byte.
            decode_network_protection(cpu, before, block=block)
            after = bytearray(before)
            changed_keys: list[str] = []
            profile_key = f"NetzKonfig{cpu}.ubSchutzart"
            ordered_changes = sorted(
                changes,
                key=lambda change: 0 if str(change.get("key", "")) == profile_key else 1,
            )
            seen_keys: set[str] = set()
            for change in ordered_changes:
                key = str(change.get("key", ""))
                if key in seen_keys:
                    raise ValueError(f"Netzschutzfeld {key!r} wurde mehrfach übergeben")
                encode_network_protection_value(
                    after,
                    cpu,
                    key,
                    change.get("value", ""),
                    block=block,
                )
                changed_keys.append(key)
                seen_keys.add(key)
            if write_enabled and auth_level >= 0 and bytes(after) != before:
                auth = self.service.authenticate(session, auth_level, pass4 or None)
                if not auth.ok:
                    raise PermissionError(f"Auth-Level {auth_level} wurde nicht gewährt")
            audit = self.service.write_payload(
                session,
                block,
                before,
                bytes(after),
                changed_keys,
                allowlist,
                dry_run=not write_enabled,
                cpu=cpu,
            )
        payload = audit.as_dict()
        payload["critical"] = True
        payload["auth_level_requested"] = auth_level
        payload["auth_level_granted"] = getattr(auth, "granted_level", None)
        self.store.audit(_now(), username, block, payload)
        return payload

    def write_power_target(self, username: str, value: object) -> dict:
        """Write the dashboard target with a fresh server-calculated PW4."""
        return self.write_block(
            username,
            POWER_TARGET_BLOCK,
            [{"key": POWER_TARGET_KEY, "value": value}],
            POWER_TARGET_AUTH_LEVEL,
            "",
            True,
        )

    def set_monitor(self, enabled: bool) -> None:
        with self.state_lock:
            self.monitor_enabled = bool(enabled) and self.serial_enabled
            self.monitor_state["enabled"] = self.monitor_enabled

    def set_serial_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self.state_lock:
            self.serial_enabled = enabled
            self.monitor_enabled = enabled
            self.monitor_state.update({
                "serial_enabled": enabled,
                "connection_state": "verbunden" if enabled else "getrennt",
                "enabled": enabled,
                "running": False if not enabled else self.monitor_state.get("running", False),
            })
        self._save_serial_enabled(enabled)
        if not enabled:
            # Wait for the current web lease to finish. The central worker and
            # other clients remain available; this switch pauses only web
            # polling and web-originated serial actions.
            with self.serial_lock:
                pass


class DachsRequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"OpenDachsManager/{__version__}"

    @property
    def app(self) -> DachsWebApp:
        return self.server.dachs_app  # type: ignore[attr-defined]

    @property
    def base_path(self) -> str:
        return self.server.base_path  # type: ignore[attr-defined]

    @property
    def cookie_path(self) -> str:
        return f"{self.base_path}/" if self.base_path else "/"

    @property
    def local_access(self) -> bool:
        return bool(getattr(self.server, "local_access", False))

    @property
    def local_origin(self) -> str:
        port = int(self.server.server_address[1])  # type: ignore[attr-defined]
        return _local_access_origin(port)

    def _route_path(self, path: str) -> str | None:
        if not self.base_path:
            return path
        prefix = self.base_path + "/"
        if not path.startswith(prefix):
            return None
        return path[len(self.base_path):] or "/"

    def log_message(self, fmt, *args):
        return

    def _token(self) -> str | None:
        for item in (self.headers.get("Cookie", "").split(";")):
            name, _, value = item.strip().partition("=")
            if name == "open_dachs_session":
                return value
        return None

    def _user(self) -> dict | None:
        if self.local_access:
            return {
                "username": LOCAL_ACCESS_PRINCIPAL,
                "role": getattr(self.server, "local_role", "guest"),
                "auth_source": LOCAL_ACCESS_PRINCIPAL,
                "local_access": True,
            }
        return self.app.session_user(self._token())

    def _validate_local_request(self, *, mutating: bool = False) -> bool:
        if not self.local_access:
            return True
        if self.client_address[0] != LOCAL_ACCESS_BIND_HOST:
            self._error(403, "local access requires a loopback peer")
            return False
        expected_authority = self.local_origin.removeprefix("http://")
        if self.headers.get_all("Host", []) != [expected_authority]:
            self._error(421, "local access requires the canonical 127.0.0.1 host")
            return False
        forwarded = [
            name
            for name in self.headers.keys()
            if name.lower().startswith("x-forwarded-")
            or name.lower() in {item.lower() for item in LOCAL_ACCESS_FORWARDED_HEADERS}
        ]
        if forwarded:
            self._error(403, "forwarded requests are not accepted on local access")
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1:
            self._error(403, "multiple origins are not accepted on local access")
            return False
        origin = origins[0] if origins else None
        if origin is not None and origin != self.local_origin:
            self._error(403, "foreign origin is not accepted on local access")
            return False
        fetch_site = self.headers.get("Sec-Fetch-Site", "").strip().lower()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            # The settings UI exposes the effective loopback URL as a link.
            # Chromium labels a click from the primary port (or the public
            # proxy origin) as same-site/cross-site even though it is a safe
            # top-level navigation. Allow only that visible, user-activated
            # GET; scripts, frames, subresources and all mutations stay denied.
            user_navigation = (
                not mutating
                and self.command == "GET"
                and origin is None
                and self.headers.get("Sec-Fetch-Mode", "").strip().lower()
                == "navigate"
                and self.headers.get("Sec-Fetch-Dest", "").strip().lower()
                == "document"
                and self.headers.get("Sec-Fetch-User", "").strip() == "?1"
            )
            if not user_navigation:
                self._error(403, "cross-site requests are not accepted on local access")
                return False
        if mutating:
            if origin != self.local_origin:
                self._error(403, "local changes require the exact local origin")
                return False
            if self.headers.get_all(LOCAL_ACCESS_MUTATION_HEADER, []) != ["1"]:
                self._error(403, "local changes require the request guard")
                return False
        return True

    def _api_principal(self) -> dict | None:
        scheme, _, secret = self.headers.get("Authorization", "").strip().partition(" ")
        if scheme.lower() != "bearer" or not secret:
            return None
        return self.app.store.authenticate_api_token(secret.strip())

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8",
              cookies: list[str] | None = None, headers: dict[str, str] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )
        if cookies:
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200, cookies: list[str] | None = None):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), cookies=cookies)

    def _error(self, status: int, message: str):
        self._json({"ok": False, "error": message}, status)

    def _session_cookie(self, token: str, max_age: int) -> str:
        return (
            f"open_dachs_session={token}; Path={self.cookie_path}; Max-Age={max_age}; "
            "HttpOnly; SameSite=Strict"
        )

    def _require(self, admin: bool = False) -> dict | None:
        user = self._user()
        if user is None:
            self._error(401, "login required")
            return None
        if admin and user.get("role") != "admin":
            self._error(403, "admin role required")
            return None
        return user

    def _require_api(self, scope: str) -> dict | None:
        principal = self._api_principal()
        if principal is None:
            self._error(401, "gültiger Bearer-Token erforderlich")
            return None
        if scope not in set(principal.get("scopes", [])):
            self._error(403, f"API-Recht {scope!r} erforderlich")
            return None
        return principal

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_JSON_BODY_BYTES:
            raise ValueError("request too large")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON object expected")
        return payload

    def do_GET(self):
        if not self._validate_local_request():
            return
        parsed = urlparse(self.path)
        if self.base_path and parsed.path == self.base_path:
            return self._send(
                308,
                b"",
                "text/plain; charset=utf-8",
                headers={"Location": self.base_path + "/"},
            )
        path = self._route_path(parsed.path)
        if path is None:
            return self._error(404, "not found")
        try:
            if path == "/healthz":
                return self._json({"ok": True, "version": __version__, "base_path": self.base_path})
            if path.startswith("/api/v1/"):
                if path == "/api/v1/live":
                    principal = self._require_api("read")
                    if principal is None:
                        return
                    return self._json({"schema": "open-dachs-api-live/v1", **self.app.live()})
                if path == "/api/v1/catalog":
                    principal = self._require_api("read")
                    if principal is None:
                        return
                    return self._json(self.app.api_catalog())
                if path == "/api/v1/actions":
                    principal = self._require_api("read")
                    if principal is None:
                        return
                    settings = self.app.api_settings()
                    return self._json({
                        "write_enabled": settings["write_enabled"],
                        "items": [{
                            "id": "set-value",
                            "description": "Ein gemapptes Feld über Read → Validate → Auth → Write → Readback ändern",
                            "required_scope": "write",
                            "enabled": bool(settings["write_enabled"]),
                        }],
                    })
                block_match = re.fullmatch(r"/api/v1/blocks/(\d+)", path)
                if block_match:
                    principal = self._require_api("read")
                    if principal is None:
                        return
                    return self._json(self.app.read_block(int(block_match.group(1))))
                network_match = re.fullmatch(
                    r"/api/v1/network-protection/([12])(?:/(16|20|21))?",
                    path,
                )
                if network_match:
                    principal = self._require_api("read")
                    if principal is None:
                        return
                    return self._json(self.app.read_network_protection(
                        int(network_match.group(1)),
                        int(network_match.group(2) or NETWORK_PROTECTION_BLOCK),
                    ))
                if path == "/api/v1/history":
                    principal = self._require_api("history")
                    if principal is None:
                        return
                    query = parse_qs(parsed.query)
                    block = validate_block(int(query.get("block", ["24"])[0]))
                    key = str(query.get("key", [""])[0]).strip()
                    if not key:
                        raise ValueError("key ist erforderlich")
                    start, end, duration = _history_bounds(query)
                    limit = min(HISTORY_MAX_POINTS, max(1, int(query.get("limit", [str(HISTORY_MAX_POINTS)])[0])))
                    points = self.app.store.adaptive_history_between(block, key, start, end, limit)
                    return self._json({
                        "schema": "open-dachs-api-history/v1",
                        "block": block,
                        "key": key,
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                        "hours": duration / 3600.0,
                        "points": points,
                    })
                return self._error(404, "unknown API v1 path")
            if path.startswith("/api/"):
                user = self._require()
                if user is None:
                    return
                if path == "/api/session":
                    return self._json({"authenticated": True, "user": user})
                if path == "/api/schema":
                    return self._json(self.app.schema())
                if path == "/api/live":
                    return self._json(self.app.live())
                if path == "/api/monitor":
                    return self._json(self.app.live()["monitor"])
                if path == "/api/maintenance/status":
                    return self._json(self.app.live()["maintenance"])
                if path == "/api/settings/maintenance":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.maintenance_settings())
                if path == "/api/settings/dashboard":
                    return self._json(self.app.dashboard_settings())
                if path == "/api/settings/soot-filter":
                    return self._json(self.app.soot_filter_settings())
                if path == "/api/settings/api":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.api_settings())
                if path == "/api/settings/local-access":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.local_access_settings())
                if path == "/api/settings/email-notifications":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.email_notification_settings())
                if path == "/api/update/status":
                    status = self.app.update_manager.public_status()
                    if user.get("role") != "admin" or self.local_access:
                        release = status.get("available")
                        status = {
                            "schema": status["schema"],
                            "current_version": status["current_version"],
                            "checked_at": status.get("checked_at"),
                            "update_available": bool(release),
                            "target_version": release.get("version") if release else None,
                            "error": status.get("error"),
                        }
                    return self._json(status)
                if path == "/api/notifications/outbox":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["50"])[0])
                    return self._json(self.app.email_notification_outbox(limit))
                if path == "/api/users":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json({"items": self.app.users(), "roles": ["guest", "admin"]})
                if path == "/api/tokens":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json({"items": self.app.store.api_tokens(), "scopes": sorted(API_TOKEN_SCOPES)})
                if path == "/api/history/adaptive/status":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.store.adaptive_status())
                if path == "/api/service-codes":
                    query = parse_qs(parsed.query)
                    search = query.get("q", [""])[0]
                    limit = int(query.get("limit", ["250"])[0])
                    return self._json(self.app.pack.service_catalog(search, limit))
                if path == "/api/backup/archive":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.backup_archives())
                archive_download = re.fullmatch(
                    r"/api/backup/archive/(\d+)/download", path
                )
                if archive_download:
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    body, filename = self.app.backup_archive_download(
                        int(archive_download.group(1))
                    )
                    return self._send(
                        200,
                        body,
                        "application/json; charset=utf-8",
                        headers={
                            "Content-Disposition": f'attachment; filename="{filename}"',
                        },
                    )
                if path == "/api/maintenance/reports":
                    return self._json(self.app.maintenance_reports())
                export_match = re.fullmatch(r"/api/maintenance/reports/(\d+)/export/(html|pdf|json)", path)
                if export_match:
                    body, content_type, filename = self.app.maintenance_export(
                        int(export_match.group(1)), export_match.group(2)
                    )
                    return self._send(200, body, content_type, headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                    })
                report_match = re.fullmatch(r"/api/maintenance/reports/(\d+)", path)
                if report_match:
                    return self._json(self.app.maintenance_report(int(report_match.group(1))))
                if path == "/api/auth-preview":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.auth_preview())
                if path == "/api/service-reset/preview":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.service_reset_preview(user["username"]))
                if path == "/api/dachs-control/preview":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    query = parse_qs(parsed.query)
                    action = query.get("action", [""])[0]
                    return self._json(
                        self.app.dachs_control_preview(user["username"], action)
                    )
                if path == "/api/runtime-24h/write-preview":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.runtime_24h_write_preview())
                if path == "/api/sc123-temperature-limit":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.sc123_temperature_limit_status())
                if path == "/api/controller-clock/sync":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.controller_clock_sync_status())
                if path == "/api/runtime-auto-reset":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json(self.app.runtime_auto_reset_status())
                if path == "/api/history-batch":
                    query = parse_qs(parsed.query)
                    try:
                        requested = json.loads(query.get("series", ["[]"])[0])
                    except (TypeError, ValueError) as exc:
                        raise ValueError("ungültige Diagrammserien") from exc
                    if not isinstance(requested, list) or not requested or len(requested) > 16:
                        raise ValueError("Diagrammserien müssen zwischen 1 und 16 Einträge enthalten")
                    requests: list[tuple[str, int, str]] = []
                    seen_ids: set[str] = set()
                    derived_ids: list[str] = []
                    for item in requested:
                        if not isinstance(item, dict):
                            raise ValueError("ungültige Diagrammserie")
                        series_id = str(item.get("id", "")).strip()
                        key = str(item.get("key", "")).strip()
                        if not series_id or not key or series_id in seen_ids:
                            raise ValueError("Diagrammserien benötigen eindeutige IDs und Keys")
                        try:
                            block = validate_block(int(item.get("block")))
                        except (TypeError, ValueError) as exc:
                            raise ValueError("ungültiger Diagrammblock") from exc
                        seen_ids.add(series_id)
                        if (block, key) == (HISTORY_REAL_RUNTIME_SERIES["block"], HISTORY_REAL_RUNTIME_SERIES["key"]):
                            derived_ids.append(series_id)
                            continue
                        if key not in self.app.pack.field_map(block):
                            raise KeyError(f"unknown field: {key}")
                        requests.append((series_id, block, key))
                    limit = min(HISTORY_MAX_POINTS, max(1, int(query.get("limit", [str(HISTORY_MAX_POINTS)])[0])))
                    start, end, duration = _history_bounds(query)
                    points = self.app.store.measurements_batch(requests, start, end, limit)
                    for series_id in derived_ids:
                        points[series_id] = self.app.store.real_runtime_series(start, end, limit)
                    return self._json({
                        "series": points,
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                        "hours": duration / 3600.0,
                    })
                if path == "/api/motor-status-history":
                    query = parse_qs(parsed.query)
                    start, end, duration = _history_bounds(query)
                    return self._json({
                        "segments": self.app.store.motor_status_segments(start, end),
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                        "hours": duration / 3600.0,
                        "backfill_complete": self.app.store.history_migration(
                            MOTOR_STATUS_MIGRATION
                        ) is not None,
                    })
                if path == "/api/display-status/events":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", [str(DISPLAY_STATUS_EVENT_LIMIT)])[0])
                    return self._json({
                        "schema": "open-dachs-display-status-events/v1",
                        "items": self.app.store.display_status_events(limit),
                    })
                if path == "/api/diagnosis/250h":
                    query = parse_qs(parsed.query)
                    refresh = query.get("refresh", ["0"])[0] == "1"
                    return self._json(
                        self.app.diagnosis_250h_cache.get(refresh=refresh)
                    )
                if path == "/api/audit":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json({"items": self.app.store.audits()})
                network_match = re.fullmatch(
                    r"/api/network-protection/([12])(?:/(16|20|21))?",
                    path,
                )
                if network_match:
                    return self._json(self.app.read_network_protection(
                        int(network_match.group(1)),
                        int(network_match.group(2) or NETWORK_PROTECTION_BLOCK),
                    ))
                if path == "/api/history":
                    query = parse_qs(parsed.query)
                    block = validate_block(int(query.get("block", ["24"])[0]))
                    key = query.get("key", [""])[0]
                    limit = min(HISTORY_MAX_POINTS, max(1, int(query.get("limit", [str(HISTORY_MAX_POINTS)])[0])))
                    if not key:
                        return self._error(400, "key required")
                    start, end, duration = _history_bounds(query)
                    points = self.app.store.measurements_between(block, key, start, end, limit)
                    return self._json({"block": block, "key": key, "from": start.isoformat(), "to": end.isoformat(), "hours": duration / 3600.0, "points": points})
                if path.startswith("/api/block/"):
                    block = int(path.rsplit("/", 1)[1])
                    return self._json(self.app.read_block(block))
                return self._error(404, "unknown API path")
            return self._static(path)
        except (ValueError, KeyError) as exc:
            self._error(400, str(exc))
        except PermissionError as exc:
            self._error(403, str(exc))
        except TransportError as exc:
            self._error(503, str(exc))
        except Exception as exc:
            self._error(500, str(exc))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = self._route_path(parsed.path)
        bearer_api = bool(path and path.startswith("/api/v1/"))
        if not self._validate_local_request(mutating=not bearer_api):
            return
        if path is None:
            return self._error(404, "not found")
        try:
            payload = self._body()
            if path == "/api/v1/actions/set-value":
                principal = self._require_api("write")
                if principal is None:
                    return
                result = self.app.execute_api_set_value(principal, payload)
                return self._json(result, status=200 if result.get("ok") else 502)
            if path == "/api/backup/create":
                if self._require() is None:
                    return
                return self._json(self.app.create_backup(payload.get("blocks")))
            if path == "/api/backup/inspect":
                if self._require() is None:
                    return
                return self._json(self.app.inspect_backup(payload.get("image")))
            if path == "/api/firmware/inspect":
                if self._require(admin=True) is None:
                    return
                encoded = payload.get("content_b64")
                if not isinstance(encoded, str) or not encoded:
                    raise ValueError("Firmwaredatei fehlt")
                if len(encoded) > ((MUC_MAX_FILE_BYTES + 2) // 3) * 4:
                    raise ValueError("Firmwaredatei ist zu groß")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("Firmwaredatei ist nicht gültig base64-kodiert") from exc
                return self._json(inspect_muc(raw, payload.get("filename")))
            if path == "/api/backup/restore":
                user = self._require(admin=True)
                if user is None:
                    return
                return self._json(self.app.restore_backup(
                    user["username"],
                    payload.get("image"),
                    payload.get("image_sha256"),
                    payload.get("blocks"),
                    payload.get("auth_level", -1),
                    str(payload.get("pass4", "")),
                    payload.get("write_enabled", False),
                    str(payload.get("confirmation", "")),
                ))
            if path == "/api/login":
                if self.local_access:
                    return self._error(403, "local access does not use password login")
                result = self.app.login(str(payload.get("username", "")), str(payload.get("password", "")))
                if result is None:
                    return self._error(401, "Benutzername oder Passwort falsch")
                token, role = result
                cookie = self._session_cookie(token, 43200)
                return self._json({"ok": True, "role": role}, cookies=[cookie])
            if path == "/api/logout":
                if self.local_access:
                    return self._json({"ok": True, "local_access": True})
                self.app.logout(self._token())
                return self._json({"ok": True}, cookies=[self._session_cookie("", 0)])
            if path == "/api/password":
                if self.local_access:
                    return self._error(403, "local access has no password")
                user = self._require()
                if user is None:
                    return
                self.app.change_password(user["username"], str(payload.get("current_password", "")), str(payload.get("new_password", "")))
                self.app.logout(self._token())
                return self._json({"ok": True, "message": "Passwort geändert; bitte neu anmelden."}, cookies=[self._session_cookie("", 0)])
            if path == "/api/users/gast/password":
                user = self._require(admin=True)
                if user is None:
                    return
                self.app.change_guest_password(
                    user["username"],
                    str(payload.get("current_password", "")),
                    str(payload.get("new_password", "")),
                )
                return self._json({"ok": True, "message": "Gastpasswort geändert."})
            if path == "/api/settings/maintenance":
                if self._require(admin=True) is None:
                    return
                test_mode = payload.get("test_mode")
                if not isinstance(test_mode, bool):
                    raise ValueError("test_mode muss true oder false sein")
                return self._json(self.app.set_maintenance_test_mode(test_mode))
            if path == "/api/settings/dashboard":
                if self._require(admin=True) is None:
                    return
                return self._json(self.app.set_dashboard_settings(payload.get("cards")))
            if path == "/api/settings/soot-filter":
                if self._require(admin=True) is None:
                    return
                return self._json(self.app.set_soot_filter_settings(payload))
            if path == "/api/settings/api":
                if self._require(admin=True) is None:
                    return
                return self._json(self.app.set_api_settings(payload))
            if path == "/api/settings/local-access":
                if self._require(admin=True) is None:
                    return
                return self._json(self.app.set_local_access_settings(payload))
            if path == "/api/settings/email-notifications":
                user = self._require(admin=True)
                if user is None:
                    return
                if self.local_access:
                    return self._error(403, "SMTP-Secrets dürfen nicht über lokalen Override geändert werden")
                if self.headers.get("X-Open-Dachs-Request") != "1":
                    return self._error(403, "missing Open Dachs request guard")
                return self._json(
                    self.app.set_email_notification_settings(payload, user["username"])
                )
            if path == "/api/update/check":
                if self._require(admin=True) is None:
                    return
                if self.headers.get("X-Open-Dachs-Request") != "1":
                    return self._error(403, "missing Open Dachs request guard")
                if self.local_access:
                    return self._error(403, "Updatedetails erfordern einen normalen Admin-Login")
                if payload:
                    raise ValueError("Updateprüfung erwartet ein leeres JSON-Objekt")
                return self._json(self.app.update_manager.check(force=True))
            if path == "/api/update/settings":
                user = self._require(admin=True)
                if user is None:
                    return
                if self.headers.get("X-Open-Dachs-Request") != "1":
                    return self._error(403, "missing Open Dachs request guard")
                required = {"automatic", "timezone", "window_start", "window_end", "password", "acknowledge"}
                if set(payload) != required or type(payload.get("acknowledge")) is not bool:
                    raise ValueError("Updateeinstellung erwartet ein striktes Reauth-/Bestätigungspayload")
                enabling = payload.get("automatic") is True
                password_ok = bool(
                    enabling and payload.get("acknowledge") is True
                    and self.app.verify_admin_password(
                        user["username"], str(payload.get("password") or "")
                    )
                )
                settings = {
                    key: payload[key]
                    for key in ("automatic", "timezone", "window_start", "window_end")
                }
                result = self.app.update_manager.save_settings(
                    settings, local_access=self.local_access, password_ok=password_ok
                )
                self.app.store.update_audit(
                    _now(), user["username"], "settings", None, None,
                    {"automatic": bool(settings["automatic"]),
                     "reauthenticated": password_ok, "local_access": self.local_access},
                )
                return self._json(result)
            if path == "/api/update/preview":
                user = self._require(admin=True)
                if user is None:
                    return
                if self.headers.get("X-Open-Dachs-Request") != "1":
                    return self._error(403, "missing Open Dachs request guard")
                if set(payload) != {"password"}:
                    raise ValueError("Updatevorschau erwartet exakt password")
                password_ok = self.app.verify_admin_password(
                    user["username"], str(payload.get("password") or "")
                )
                session_nonce, auth_epoch = self.app.session_update_identity(self._token())
                return self._json(
                    self.app.update_manager.preview_update(
                        user["username"], password_ok,
                        local_access=self.local_access,
                        session_nonce=session_nonce, auth_epoch=auth_epoch,
                    )
                )
            if path == "/api/update/apply":
                user = self._require(admin=True)
                if user is None:
                    return
                if self.headers.get("X-Open-Dachs-Request") != "1":
                    return self._error(403, "missing Open Dachs request guard")
                session_nonce, auth_epoch = self.app.session_update_identity(self._token())
                operation = self.app.update_manager.apply(
                    user["username"], payload, local_access=self.local_access,
                    session_nonce=session_nonce, auth_epoch=auth_epoch,
                )
                self.app.store.update_audit(
                    _now(), user["username"], "apply-request",
                    operation["request_id"], operation["version"], {
                        "automatic": False, "controller_written": False,
                        "auth_used": False, "serial_session_opened": False,
                    },
                )
                return self._json(operation, status=202)
            if path == "/api/notifications/test":
                user = self._require(admin=True)
                if user is None:
                    return
                if self.local_access:
                    return self._error(403, "Testmail ist über lokalen Override gesperrt")
                if self.headers.get("X-Open-Dachs-Request") != "1":
                    return self._error(403, "missing Open Dachs request guard")
                if set(payload) != {"request_id"}:
                    raise ValueError("Testmail-Anfrage erlaubt nur request_id")
                return self._json(
                    self.app.test_email_notification(
                        user["username"], str(payload.get("request_id") or "")
                    )
                )
            if path == "/api/users":
                if self._require(admin=True) is None:
                    return
                return self._json(self.app.create_user(
                    payload.get("username"), payload.get("password"), payload.get("role")
                ), status=201)
            user_match = re.fullmatch(r"/api/users/([A-Za-z0-9._-]{3,32})", path)
            if user_match:
                user = self._require(admin=True)
                if user is None:
                    return
                return self._json(self.app.update_user(user["username"], user_match.group(1), payload))
            if path == "/api/tokens":
                user = self._require(admin=True)
                if user is None:
                    return
                return self._json(self.app.store.create_api_token(
                    user["username"], payload.get("name"), payload.get("scopes")
                ), status=201)
            token_match = re.fullmatch(r"/api/tokens/(\d+)", path)
            if token_match:
                if self._require(admin=True) is None:
                    return
                return self._json(self.app.store.update_api_token(
                    int(token_match.group(1)),
                    scopes=payload.get("scopes"),
                    enabled=payload.get("enabled"),
                ))
            if path == "/api/monitor":
                if self._require(admin=True) is None:
                    return
                self.app.set_monitor(bool(payload.get("enabled", True)))
                return self._json(self.app.live()["monitor"])
            if path == "/api/serial":
                if self._require(admin=True) is None:
                    return
                self.app.set_serial_enabled(bool(payload.get("enabled", True)))
                return self._json(self.app.live()["monitor"])
            if path == "/api/overview/power-target":
                user = self._require(admin=True)
                if user is None:
                    return
                result = self.app.write_power_target(
                    user["username"], payload.get("value", ""),
                )
                return self._json(result, status=502 if result.get("error") else 200)
            if path == "/api/service-reset":
                user = self._require(admin=True)
                if user is None:
                    return
                if not isinstance(payload, dict):
                    raise ValueError("Entstörung: JSON-Objekt erforderlich")
                unexpected = sorted(
                    set(payload).difference({
                        "confirm", "expected_service_code", "request_token",
                    })
                )
                if unexpected:
                    raise ValueError(
                        "Entstörung: unzulässige Felder: " + ", ".join(unexpected)
                    )
                result = self.app.reset_service_code(
                    user["username"],
                    payload.get("expected_service_code"),
                    payload.get("confirm"),
                    payload.get("request_token"),
                )
                status = 200 if result.get("ok") else 409 if result.get("conflict") else 502
                return self._json(result, status=status)
            if path == "/api/dachs-control":
                user = self._require(admin=True)
                if user is None:
                    return
                if not isinstance(payload, dict):
                    raise ValueError("Dachs Ein/Aus: JSON-Objekt erforderlich")
                unexpected = sorted(
                    set(payload).difference({"action", "confirm", "request_token"})
                )
                if unexpected:
                    raise ValueError(
                        "Dachs Ein/Aus: unzulässige Felder: " + ", ".join(unexpected)
                    )
                result = self.app.set_dachs_control(
                    user["username"],
                    payload.get("action"),
                    payload.get("confirm"),
                    payload.get("request_token"),
                )
                status = (
                    200
                    if result.get("ok")
                    else 409
                    if result.get("conflict")
                    else 502
                )
                return self._json(result, status=status)
            if path == "/api/sc123-temperature-limit/preview":
                user = self._require(admin=True)
                if user is None:
                    return
                if not isinstance(payload, dict):
                    raise ValueError("SC123-Grenze: JSON-Objekt erforderlich")
                unexpected = sorted(set(payload).difference({"value"}))
                if unexpected:
                    raise ValueError(
                        "SC123-Grenze: unzulässige Felder: "
                        + ", ".join(unexpected)
                    )
                return self._json(
                    self.app.sc123_temperature_limit_preview(
                        user["username"], payload.get("value")
                    )
                )
            if path == "/api/sc123-temperature-limit":
                user = self._require(admin=True)
                if user is None:
                    return
                if not isinstance(payload, dict):
                    raise ValueError("SC123-Grenze: JSON-Objekt erforderlich")
                allowed = {"value", "confirm", "write_enabled", "request_token"}
                unexpected = sorted(set(payload).difference(allowed))
                if unexpected:
                    raise ValueError(
                        "SC123-Grenze: unzulässige Felder: "
                        + ", ".join(unexpected)
                    )
                result = self.app.set_sc123_temperature_limit(
                    user["username"],
                    payload.get("value"),
                    payload.get("confirm"),
                    payload.get("write_enabled"),
                    payload.get("request_token"),
                )
                return self._json(result, status=200 if result.get("ok") else 502)
            if path == "/api/controller-clock/sync/settings":
                user = self._require(admin=True)
                if user is None:
                    return
                return self._json(
                    self.app.set_controller_clock_sync_settings(user["username"], payload)
                )
            if path == "/api/controller-clock/sync":
                user = self._require(admin=True)
                if user is None:
                    return
                forbidden = sorted({"auth_level", "pass4", "pw4", "target", "value"}.intersection(payload))
                if forbidden:
                    raise ValueError("unzulässige Felder: " + ", ".join(forbidden))
                if payload.get("confirm") is not True:
                    raise ValueError("Uhrabgleich muss ausdrücklich bestätigt werden")
                result = self.app.sync_controller_clock(user["username"], "manual")
                return self._json(result, status=502 if result.get("result") == "fehlgeschlagen" else 200)
            if path == "/api/runtime-auto-reset":
                user = self._require(admin=True)
                if user is None:
                    return
                return self._json(
                    self.app.set_runtime_auto_reset_settings(user["username"], payload)
                )
            if path == "/api/runtime-24h/counter":
                user = self._require(admin=True)
                if user is None:
                    return
                forbidden = sorted(
                    {"auth_level", "pass4", "confirmation"}.intersection(payload)
                )
                if forbidden:
                    raise ValueError(
                        "24-h-Zähler: clientseitige Auth-/Bestätigungsfelder sind nicht zulässig: "
                        + ", ".join(forbidden)
                    )
                return self._json(self.app.set_runtime_24h_counter(
                    user["username"],
                    payload.get("value", ""),
                    payload.get("expected_counter"),
                    payload.get("write_enabled", False),
                ))
            if path == "/api/maintenance/reports":
                user = self._require(admin=True)
                if user is None:
                    return
                return self._json(self.app.create_maintenance_report(user["username"]))
            intermediate_match = re.fullmatch(
                r"/api/maintenance/reports/(\d+)/complete-local-intermediate", path
            )
            if intermediate_match:
                user = self._require(admin=True)
                if user is None:
                    return
                return self._json(self.app.complete_local_intermediate_maintenance(
                    user["username"], int(intermediate_match.group(1)),
                    dict(payload.get("protocol") or {}), str(payload.get("reason", "")),
                ))
            completion_match = re.fullmatch(r"/api/maintenance/reports/(\d+)/complete", path)
            if completion_match:
                user = self._require(admin=True)
                if user is None:
                    return
                result = self.app.complete_maintenance(
                    user["username"], int(completion_match.group(1)), dict(payload.get("protocol") or {}),
                    payload.get("auth_level", -1), str(payload.get("pass4", "")),
                    str(payload.get("confirmation", "")),
                    demo=self.app.maintenance_settings()["test_mode"],
                )
                return self._json(result)
            report_match = re.fullmatch(r"/api/maintenance/reports/(\d+)", path)
            if report_match:
                if self._require(admin=True) is None:
                    return
                return self._json(self.app.save_maintenance_report(
                    int(report_match.group(1)), dict(payload.get("protocol") or {})
                ))
            if path.startswith("/api/block/"):
                user = self._require(admin=True)
                if user is None:
                    return
                block = int(path.rsplit("/", 1)[1])
                result = self.app.write_block(
                    user["username"], block, list(payload.get("changes", [])),
                    payload.get("auth_level", -1), str(payload.get("pass4", "")), payload.get("write_enabled", False),
                )
                return self._json(result, status=502 if result.get("error") else 200)
            network_match = re.fullmatch(
                r"/api/network-protection/([12])(?:/(16|20|21))?",
                path,
            )
            if network_match:
                user = self._require(admin=True)
                if user is None:
                    return
                result = self.app.write_network_protection(
                    user["username"], int(network_match.group(1)),
                    list(payload.get("changes", [])),
                    payload.get("auth_level", -1), str(payload.get("pass4", "")),
                    payload.get("write_enabled", False),
                    block=int(network_match.group(2) or NETWORK_PROTECTION_BLOCK),
                )
                return self._json(result, status=502 if result.get("error") else 200)
            return self._error(404, "unknown API path")
        except (APIRequestConflictError, LocalAccessConflictError) as exc:
            self._error(409, str(exc))
        except (ValueError, KeyError) as exc:
            self._error(400, str(exc))
        except PermissionError as exc:
            self._error(403, str(exc))
        except TransportError as exc:
            self._error(503, str(exc))
        except Exception as exc:
            self._error(500, str(exc))

    def do_DELETE(self):
        if not self._validate_local_request(mutating=True):
            return
        path = self._route_path(urlparse(self.path).path)
        if path is None:
            return self._error(404, "not found")
        try:
            user_match = re.fullmatch(r"/api/users/([A-Za-z0-9._-]{3,32})", path)
            if user_match:
                user = self._require(admin=True)
                if user is None:
                    return
                deleted = self.app.delete_user(user["username"], user_match.group(1))
                return self._json({"ok": True, "deleted": deleted})
            token_match = re.fullmatch(r"/api/tokens/(\d+)", path)
            if token_match:
                if self._require(admin=True) is None:
                    return
                deleted = self.app.store.delete_api_token(int(token_match.group(1)))
                return self._json({"ok": True, "deleted": deleted})
            report_match = re.fullmatch(r"/api/maintenance/reports/(\d+)", path)
            if report_match:
                if self._require(admin=True) is None:
                    return
                deleted = self.app.delete_maintenance_report(int(report_match.group(1)))
                return self._json({"ok": True, "deleted": deleted})
            return self._error(404, "unknown API path")
        except APIRequestConflictError as exc:
            self._error(409, str(exc))
        except (ValueError, KeyError) as exc:
            self._error(400, str(exc))
        except PermissionError as exc:
            self._error(403, str(exc))
        except Exception as exc:
            self._error(500, str(exc))

    def _static(self, path: str):
        relative = "index.html" if path in ("", "/") else path.removeprefix("/static/") if path.startswith("/static/") else ""
        if not relative or ".." in Path(relative).parts:
            return self._error(404, "not found")
        target = (WEB_DIR / relative).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            return self._error(404, "not found")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        if target.name == "index.html":
            body = body.replace(b"__OPEN_DACHS_BASE_PATH__", self.base_path.encode("ascii"))
            body = body.replace(b"__OPEN_DACHS_VERSION__", __version__.encode("ascii"))
        self._send(200, body, content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else ""))


class DachsHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        app: DachsWebApp,
        base_path: str = "",
        *,
        local_access: bool = False,
        local_role: str = "admin",
    ):
        if local_role not in LOCAL_ACCESS_ROLES:
            raise ValueError("local_role must be guest or admin")
        if local_access and str(address[0]) != LOCAL_ACCESS_BIND_HOST:
            raise ValueError("local access must bind exactly to 127.0.0.1")
        self.dachs_app = app
        self.base_path = normalize_base_path(base_path)
        self.local_access = bool(local_access)
        self.local_role = local_role
        super().__init__(address, DachsRequestHandler)


class LocalAccessManager:
    """Own one optional cookie-less HTTP listener bound only to loopback."""

    def __init__(
        self,
        app: DachsWebApp,
        *,
        base_path: str,
        primary_port: int,
    ):
        self.app = app
        self.base_path = normalize_base_path(base_path)
        self.primary_port = int(primary_port)
        self.lock = threading.RLock()
        self.server: DachsHTTPServer | None = None
        self.thread: threading.Thread | None = None
        app.set_local_access_manager(self)

    def _url(self, port: int) -> str:
        suffix = f"{self.base_path}/" if self.base_path else "/"
        return f"{_local_access_origin(port)}{suffix}"

    @staticmethod
    def _stop_pair(
        server: DachsHTTPServer | None,
        thread: threading.Thread | None,
    ) -> None:
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _new_pair(self, settings: dict) -> tuple[DachsHTTPServer, threading.Thread]:
        port = int(settings["port"])
        try:
            server = DachsHTTPServer(
                (LOCAL_ACCESS_BIND_HOST, port),
                self.app,
                base_path=self.base_path,
                local_access=True,
                local_role=str(settings["role"]),
            )
        except OSError as exc:
            detail = exc.strerror or str(exc)
            raise LocalAccessConflictError(
                f"Lokaler Port {port} kann auf 127.0.0.1 nicht geöffnet werden: {detail}"
            ) from exc
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"dachs-local-access-{port}",
            daemon=True,
        )
        return server, thread

    def _apply(self, settings: dict, *, persist: bool) -> dict:
        old_server = old_thread = None
        with self.lock:
            if int(settings["port"]) == self.primary_port:
                raise LocalAccessConflictError(
                    "Lokaler Port muss sich vom normalen Web-Port unterscheiden"
                )
            current_server = self.server
            current_thread = self.thread
            same_port = bool(
                settings["enabled"]
                and current_server is not None
                and int(current_server.server_address[1]) == int(settings["port"])
            )
            if same_port:
                if persist:
                    self.app._commit_local_access_settings(settings)
                current_server.local_role = str(settings["role"])
                self.app._set_local_access_runtime(
                    active=True,
                    last_error=None,
                    restart_required=False,
                    url=self._url(int(settings["port"])),
                )
                return self.app.local_access_settings()

            new_server = new_thread = None
            if settings["enabled"]:
                new_server, new_thread = self._new_pair(settings)
                try:
                    new_thread.start()
                except Exception:
                    new_server.server_close()
                    raise
            try:
                if persist:
                    self.app._commit_local_access_settings(settings)
            except Exception:
                self._stop_pair(new_server, new_thread)
                raise

            old_server, old_thread = current_server, current_thread
            self.server, self.thread = new_server, new_thread
            self.app._set_local_access_runtime(
                active=bool(new_server),
                last_error=None,
                restart_required=False,
                url=self._url(int(settings["port"])) if new_server else None,
            )

        self._stop_pair(old_server, old_thread)
        return self.app.local_access_settings()

    def start(self) -> dict:
        settings = self.app._normalize_local_access_settings(
            self.app.local_access_config
        )
        if not settings["enabled"]:
            self.app._set_local_access_runtime(
                active=False,
                last_error=None,
                restart_required=False,
                url=None,
            )
            return self.app.local_access_settings()
        try:
            result = self._apply(settings, persist=False)
        except Exception as exc:
            self.app._set_local_access_runtime(
                active=False,
                last_error=str(exc),
                restart_required=False,
                url=self._url(int(settings["port"])),
            )
            print(f"Open Dachs Manager: lokaler Zugang inaktiv: {exc}", flush=True)
            return self.app.local_access_settings()
        print(f"Open Dachs Manager lokaler Zugang: {result['url']}", flush=True)
        return result

    def reconfigure(self, settings: dict) -> dict:
        return self._apply(settings, persist=True)

    def stop(self) -> None:
        with self.lock:
            server, thread = self.server, self.thread
            self.server = None
            self.thread = None
        self._stop_pair(server, thread)
        self.app._set_local_access_runtime(
            active=False,
            restart_required=False,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-dachs-web",
        description="Open Dachs Manager Weboberfläche",
    )
    parser.add_argument("--host", default=os.environ.get("OPEN_DACHS_WEB_HOST", "0.0.0.0"))
    parser.add_argument("--web-port", "--port", dest="web_port", type=int, default=int(os.environ.get("OPEN_DACHS_WEB_PORT", "8084")))
    parser.add_argument("--serial-port", default=os.environ.get("OPEN_DACHS_SERIAL_PORT", "/dev/ttyUSB0"))
    parser.add_argument(
        "--serial-socket",
        default=os.environ.get("OPEN_DACHS_SERIAL_SOCKET", DEFAULT_SERIAL_WORKER_SOCKET),
    )
    parser.add_argument("--baud", type=int, default=int(os.environ.get("OPEN_DACHS_BAUD", "19200")))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("OPEN_DACHS_TIMEOUT", "0.9")))
    parser.add_argument("--pack-rev", default=os.environ.get("OPEN_DACHS_PACK_REV", "50"))
    parser.add_argument("--data-dir", default=os.environ.get("OPEN_DACHS_WEB_DATA_DIR", "/var/lib/open-dachs-manager"))
    parser.add_argument(
        "--base-path",
        default=os.environ.get("OPEN_DACHS_BASE_PATH", ""),
        help="external URL prefix, for example /dachs",
    )
    parser.add_argument("--interval", type=float, default=float(os.environ.get("OPEN_DACHS_WEB_INTERVAL", "0.75")))
    parser.add_argument(
        "--history-interval",
        type=float,
        default=float(os.environ.get("OPEN_DACHS_HISTORY_INTERVAL", str(DEFAULT_HISTORY_SAMPLE_INTERVAL))),
        help="minimum seconds between compact 22/24 history snapshots; 0 stores every cycle",
    )
    parser.add_argument(
        "--maintenance-live-writes",
        action="store_true",
        default=os.environ.get("OPEN_DACHS_MAINTENANCE_LIVE_WRITES", "0").strip().lower() in {"1", "true", "yes", "on"},
        help="enable the real block-100/block-104 maintenance completion; default is safe demo mode",
    )
    parser.add_argument(
        "--service-reset-temperature-policy-approved",
        action="store_true",
        default=os.environ.get(
            "OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED", "0"
        ).strip().lower() in {"1", "true", "yes", "on"},
        help=(
            "enable manual service-code reset only after the installation-specific "
            "temperature/restart policy has been reviewed; default is fail-closed"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = DachsWebApp(
        port=args.serial_port,
        baud=args.baud,
        timeout=args.timeout,
        pack_rev=args.pack_rev,
        data_dir=args.data_dir,
        interval=args.interval,
        history_interval=args.history_interval,
        serial_socket=args.serial_socket,
        maintenance_live_writes=args.maintenance_live_writes,
        service_reset_temperature_policy_approved=(
            args.service_reset_temperature_policy_approved
        ),
    )
    base_path = normalize_base_path(args.base_path)
    server = DachsHTTPServer((args.host, args.web_port), app, base_path=base_path)
    local_access = LocalAccessManager(
        app,
        base_path=base_path,
        primary_port=args.web_port,
    )
    app.start()
    local_access.start()
    print(f"Open Dachs Manager: http://{args.host}:{args.web_port}{base_path or '/'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        local_access.stop()
        app.stop()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
