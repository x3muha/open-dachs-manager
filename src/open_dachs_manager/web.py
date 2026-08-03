"""Dependency-light web interface for Open Dachs Manager.

The web layer deliberately sits above :mod:`open_dachs_manager.service`: reads,
authentication, guarded writes and readback keep the same semantics as the
CLI/TUI.  Production access goes through the shared FIFO serial worker, so
separate processes never open or fight over the optical adapter.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import http.server
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlparse

from .auth import calculate_pw4
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
    fuel_type_from_raw,
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
from .network_protection import (
    NETWORK_PROTECTION_BLOCK,
    NETWORK_PROTECTION_CPUS,
    decode_network_protection,
    encode_network_protection_value,
    network_protection_schema,
    validate_network_cpu,
)
from .serial_worker import DEFAULT_SERIAL_WORKER_SOCKET
from .service import DachsService
from .transport import TransportError, validate_block


WEB_DIR = Path(__file__).with_name("web")
DEFAULT_MONITOR_BLOCKS = (20, 22, 24, 26, 50, 104)
DEFAULT_FAST_MONITOR_BLOCKS = (20, 22, 24)
DEFAULT_SLOW_MONITOR_BLOCKS = (26, 50, 104)
DEFAULT_SLOW_MONITOR_INTERVAL = 10.0
POWER_TARGET_BLOCK = 50
POWER_TARGET_KEY = "Hka_Ew.usSollGenerator"
RUN_HISTORY_BLOCKS = (28, 30, 31, 32)
SERVICE_HISTORY_BLOCKS = (80, 82)
MOTOR_SNAPSHOT_BLOCKS = (84, 88, 92)
MC_STATUS_BLOCKS = (26, 86, 90, 94)
HISTORY_RETENTION_DAYS = 30
HISTORY_MAX_POINTS = 2000
HISTORY_CLEANUP_MARKER = "dashboard-cleanup-v1.done"
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
    ("betriebsstunden", "Laufzeit seit Start", 24, "Hka_Mw1.ulMotorlaufsekunden", "h", "#7c3aed"),
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
    hours = min(HISTORY_RETENTION_DAYS * 24, max(1, int(query.get("hours", ["24"])[0])))
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    return start, end, hours * 3600.0


def web_field_visible(block: int, key: str, metadata: dict | None = None) -> bool:
    """Return whether a correctly mapped field may be shown in the UI."""
    return True


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
    for username, role, password in (("admin", "admin", admin_password), ("gast", "guest", guest_password)):
        salt, digest = _hash_password(password)
        users.append({"username": username, "role": role, "salt": salt, "digest": digest})
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": 1, "users": users}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
                CREATE TABLE IF NOT EXISTS write_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    username TEXT NOT NULL,
                    block INTEGER NOT NULL,
                    audit_json TEXT NOT NULL
                );
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
                    completion_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_maintenance_reports_created
                    ON maintenance_reports(created_at DESC);
                """
            )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
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
        with self.lock:
            db = self.connect()
            try:
                return self._measurements_between_db(db, block, key, start, end, limit)
            finally:
                db.close()

    @staticmethod
    def _measurements_between_db(
        db: sqlite3.Connection,
        block: int,
        key: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[dict]:
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        limit = min(HISTORY_MAX_POINTS, max(1, int(limit)))
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

    def measurements_batch(
        self,
        requests: list[tuple[str, int, str]],
        start: datetime,
        end: datetime,
        limit: int,
    ) -> dict[str, list[dict]]:
        """Load chart series with two bounded WAL read connections."""
        if not requests:
            return {}

        def load(item: tuple[str, int, str]) -> tuple[str, list[dict]]:
            series_id, block, key = item
            db = self.connect()
            try:
                return series_id, self._measurements_between_db(db, block, key, start, end, limit)
            finally:
                db.close()

        # WAL permits concurrent readers while the monitor commits samples.
        # Two workers are faster on the Pi's SD card than eight random readers
        # and keep this endpoint bounded when more than one browser is open.
        with ThreadPoolExecutor(max_workers=min(2, len(requests))) as pool:
            return dict(pool.map(load, requests))

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

    def audit(self, recorded_at: str, username: str, block: int, audit: dict) -> None:
        with self.lock, self.database() as db:
            db.execute(
                "INSERT INTO write_audit(recorded_at, username, block, audit_json) VALUES(?,?,?,?)",
                (recorded_at, username, block, json.dumps(audit, ensure_ascii=False)),
            )

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
        if full:
            item.update({"report": report, "protocol": protocol, "completion": completion})
        return item

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

    def purge(self, days: int = 90) -> None:
        cutoff = datetime.fromtimestamp(time.time() - max(1, days) * 86400, timezone.utc).isoformat()
        with self.lock, self.database() as db:
            db.execute("DELETE FROM measurements WHERE recorded_at < ?", (cutoff,))

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
                 maintenance_live_writes: bool = False):
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
        self.interval = max(0.3, float(interval))
        # Maintenance completion starts in an intentionally non-writing demo
        # mode.  Enabling the later live workflow is a server-side operator
        # decision; a browser request cannot bypass this guard.
        self.maintenance_live_writes_enabled = bool(maintenance_live_writes)
        self.slow_monitor_interval = DEFAULT_SLOW_MONITOR_INTERVAL
        self._last_slow_poll = 0.0
        # Do not run the first full-table retention scan during startup. The
        # monitor loop performs the same maintenance hourly after the web
        # listener is already responsive.
        self._last_retention_purge = time.monotonic()
        self.serial_state_path = self.data_dir / "serial_state.json"
        self.dashboard_cleanup_marker = self.data_dir / HISTORY_CLEANUP_MARKER
        self.serial_enabled = self._load_serial_enabled()
        self.monitor_enabled = self.serial_enabled
        self.stop_event = threading.Event()
        self.serial_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.sessions: dict[str, tuple[str, str, float]] = {}
        self.live_values: dict[tuple[int, str], dict] = {}
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
        self.monitor_thread = threading.Thread(target=self._monitor_loop, name="dachs-web-monitor", daemon=True)
        self.maintenance_thread = threading.Thread(
            target=self._startup_maintenance,
            name="dachs-web-maintenance",
            daemon=True,
        )

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

    def start(self) -> None:
        if self.initial_credentials:
            print("Open Dachs Manager Web-Erstzugang (nur einmal anzeigen):", flush=True)
            print(f"  admin / {self.initial_credentials['admin']}", flush=True)
            print(f"  gast  / {self.initial_credentials['gast']}", flush=True)
        self.monitor_thread.start()
        # A large SQLite history can make either cleanup query take minutes on
        # the Pi's SD card.  Keep the HTTP listener responsive and run the
        # maintenance work in the background after startup.
        self.maintenance_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.monitor_thread.join(timeout=2)

    def _load_users(self) -> list[dict]:
        try:
            return list(json.loads(self.users_path.read_text(encoding="utf-8")).get("users", []))
        except Exception:
            return []

    def _save_users(self, users: list[dict]) -> None:
        temporary = self.users_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "users": users}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.users_path)

    def _invalidate_user_sessions(self, username: str) -> None:
        with self.state_lock:
            expired = [token for token, item in self.sessions.items() if item[0] == username]
            for token in expired:
                self.sessions.pop(token, None)

    def _startup_maintenance(self) -> None:
        """Clean known bad chart samples once, off the HTTP start path."""
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

    def login(self, username: str, password: str) -> tuple[str, str] | None:
        for user in self._load_users():
            if user.get("username") == username and _check_password(password, user.get("salt", ""), user.get("digest", "")):
                token = secrets.token_urlsafe(32)
                with self.state_lock:
                    self.sessions[token] = (username, str(user.get("role", "guest")), time.time() + 12 * 3600)
                return token, str(user.get("role", "guest"))
        return None

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

    def logout(self, token: str | None) -> None:
        if token:
            with self.state_lock:
                self.sessions.pop(token, None)

    def change_password(self, username: str, current_password: str, new_password: str) -> None:
        if len(new_password) < 12:
            raise ValueError("neues Passwort muss mindestens 12 Zeichen haben")
        users = self._load_users()
        found = False
        for user in users:
            if user.get("username") != username:
                continue
            if user.get("role") != "admin":
                raise PermissionError("nur der Admin kann Web-Passwörter ändern")
            if not _check_password(current_password, user.get("salt", ""), user.get("digest", "")):
                raise PermissionError("bisheriges Passwort ist falsch")
            salt, digest = _hash_password(new_password)
            user["salt"] = salt
            user["digest"] = digest
            found = True
            break
        if not found:
            raise KeyError("Benutzer nicht gefunden")
        self._save_users(users)
        self._invalidate_user_sessions(username)

    def change_guest_password(self, admin_username: str, current_password: str, new_password: str) -> None:
        if len(new_password) < 12:
            raise ValueError("neues Gastpasswort muss mindestens 12 Zeichen haben")
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
        self._save_users(users)
        self._invalidate_user_sessions("gast")

    def _monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            with self.state_lock:
                serial_enabled = self.serial_enabled
                enabled = self.monitor_enabled and serial_enabled
                self.monitor_state["enabled"] = enabled
                self.monitor_state["serial_enabled"] = serial_enabled
                self.monitor_state["connection_state"] = "verbunden" if serial_enabled else "getrennt"
                self.monitor_state["running"] = enabled
            if not enabled:
                self.stop_event.wait(0.5)
                continue
            recorded_at = _now()
            rows: list[dict] = []
            ok_blocks = failed_blocks = 0
            error_text = None
            try:
                with self.serial_lock:
                    with self.state_lock:
                        if not self.serial_enabled:
                            continue
                    blocks = list(DEFAULT_FAST_MONITOR_BLOCKS)
                    now_mono = time.monotonic()
                    if now_mono - self._last_slow_poll >= self.slow_monitor_interval:
                        blocks.extend(DEFAULT_SLOW_MONITOR_BLOCKS)
                        self._last_slow_poll = now_mono
                    with self.service.session() as session:
                        for block in blocks:
                            try:
                                result, fields = self.service.decoded_block(session, block)
                                if not result.ok:
                                    failed_blocks += 1
                                    error_text = f"Block {block}: Status {result.status!r}"
                                    continue
                                ok_blocks += 1
                                for field in fields:
                                    if not web_monitor_field_visible(block, field.key):
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
                                        "rtt_ms": round(result.response.elapsed_ms, 1),
                                        "recorded_at": recorded_at,
                                    }
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
                                            "rtt_ms": round(result.response.elapsed_ms, 1),
                                            "recorded_at": recorded_at,
                                        }
                                        rows.append(outlet_row)
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
                                            "rtt_ms": round(result.response.elapsed_ms, 1),
                                            "recorded_at": recorded_at,
                                        }
                                        rows.append(flag_row)
                                        self.live_values[(104, key)] = flag_row
                                if block in DEFAULT_SLOW_MONITOR_BLOCKS:
                                    with self.state_lock:
                                        self.monitor_state["last_slow_cycle"] = recorded_at
                            except Exception as exc:
                                failed_blocks += 1
                                error_text = f"Block {block}: {exc}"
                self.store.record(recorded_at, rows)
                if time.monotonic() - self._last_retention_purge >= 3600:
                    self.store.purge(HISTORY_RETENTION_DAYS)
                    self._last_retention_purge = time.monotonic()
            except Exception as exc:
                error_text = str(exc)
                failed_blocks = len(DEFAULT_FAST_MONITOR_BLOCKS)
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
            self.stop_event.wait(self.interval)

    def live(self) -> dict:
        with self.state_lock:
            state = dict(self.monitor_state)
            values = list(self.live_values.values())
        values.sort(key=lambda item: (int(item["block"]), str(item["key"])))
        cache_values = {
            str(item["key"]): item.get("value")
            for item in values
            if int(item["block"]) == 104
        }
        return {"monitor": state, "values": values, "maintenance": maintenance_status(cache_values)}

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
                    "size": meta.get("size", 1),
                    "offset": meta.get("offset", 0),
                    "write": True,
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
        return {
            "version": "dachs-msr2-web/v1",
            "pack_rev": self.pack.pack_rev,
            "monitor_blocks": list(DEFAULT_MONITOR_BLOCKS),
            "fast_monitor_blocks": list(DEFAULT_FAST_MONITOR_BLOCKS),
            "slow_monitor_blocks": list(DEFAULT_SLOW_MONITOR_BLOCKS),
            "slow_interval_seconds": self.slow_monitor_interval,
            "history_retention_days": HISTORY_RETENTION_DAYS,
            "blocks": blocks,
            "network_protection": [network_protection_schema(cpu) for cpu in NETWORK_PROTECTION_CPUS],
            "series": series,
            "roles": {"guest": "lesen", "admin": "lesen und schreiben"},
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
                    "write": True,
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

    def read_network_protection(self, cpu: int) -> dict:
        """Read block 16 from network-monitor CPU 1 or 2."""
        cpu = validate_network_cpu(cpu)
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        with self.serial_lock, self.service.session() as session:
            result = self.service.read_block(session, NETWORK_PROTECTION_BLOCK, cpu=cpu)
        if not result.ok:
            raise RuntimeError(
                f"Netzschutz CPU {cpu}, Block {NETWORK_PROTECTION_BLOCK} konnte nicht gelesen werden"
            )
        fields = decode_network_protection(cpu, result.payload)
        return {
            "cpu": cpu,
            "block": NETWORK_PROTECTION_BLOCK,
            "name": f"Netzschutz · Überwachungs-CPU {cpu}",
            "ok": True,
            "status": result.status,
            "payload_hex": result.payload.hex(" ").upper(),
            "payload_len": len(result.payload),
            "rtt_ms": round(result.response.elapsed_ms, 1),
            "critical": True,
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
        return {"items": self.store.maintenance_reports(), "status": self.live()["maintenance"]}

    def maintenance_report(self, report_id: int) -> dict:
        item = self.store.maintenance_report(report_id)
        live_writes_enabled = bool(getattr(self, "maintenance_live_writes_enabled", False))
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
        """Read every serially addressable block as one local snapshot."""
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        started_at = _now()
        target_blocks = tuple(self.pack.addressable_blocks())
        snapshots: dict[str, dict] = {}
        payloads: dict[int, bytes] = {}
        failed_blocks: list[dict] = []
        with self.serial_lock, self.service.session() as session:
            for block in target_blocks:
                try:
                    result, fields = self.service.decoded_block(session, block)
                except Exception as exc:
                    error = str(exc) or exc.__class__.__name__
                    failed_blocks.append({"block": block, "error": error})
                    snapshots[str(block)] = {
                        "block": block,
                        "name": self.pack.block_name(block),
                        "ok": False,
                        "status": None,
                        "error": error,
                        "payload_hex": "",
                        "rtt_ms": None,
                        "fields": [],
                    }
                    continue
                if not result.ok:
                    error = "keine gültige serielle Antwort"
                    failed_blocks.append({"block": block, "status": result.status, "error": error})
                    snapshots[str(block)] = {
                        "block": block,
                        "name": self.pack.block_name(block),
                        "ok": False,
                        "status": result.status,
                        "error": error,
                        "payload_hex": "",
                        "rtt_ms": round(result.response.elapsed_ms, 1),
                        "fields": [],
                    }
                    continue
                payloads[block] = bytes(result.payload)
                snapshots[str(block)] = {
                    "block": block,
                    "name": self.pack.block_name(block),
                    "ok": True,
                    "status": result.status,
                    "captured_at": _now(),
                    "payload_hex": result.payload.hex(" ").upper(),
                    "rtt_ms": round(result.response.elapsed_ms, 1),
                    "fields": [self._report_field(field) for field in fields if web_field_visible(block, field.key, field.metadata)],
                }
        completed_at = _now()
        missing_required = sorted(MAINTENANCE_REQUIRED_BLOCKS - payloads.keys())
        if missing_required:
            blocks = ", ".join(str(block) for block in missing_required)
            raise RuntimeError(f"Wartungsstart nicht möglich; Pflichtblöcke fehlen: {blocks}")
        block24 = {item["key"]: item for item in snapshots["24"]["fields"]}
        fuel_type = fuel_type_from_raw((block24.get("Hka_Mw1.bKraftstofftyp") or {}).get("raw"))
        if fuel_type == "unknown":
            fuel_type = "gas"
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
            "version": 2,
            "generated_at": completed_at,
            "generated_by": username,
            "pack_rev": self.pack.pack_rev,
            "fuel_type": fuel_type,
            "snapshot": {
                "started_at": started_at,
                "completed_at": completed_at,
                "attempted_blocks": list(target_blocks),
                "captured_blocks": sorted(payloads),
                "failed_blocks": failed_blocks,
                "complete": not failed_blocks,
            },
            "maintenance_status": maintenance_status(cache_values),
            "blocks": snapshots,
        }
        protocol = new_protocol(fuel_type, snapshots["100"]["fields"])
        created = self.store.create_maintenance_report(username, fuel_type, report, protocol)
        return self.maintenance_report(created["id"])

    def save_maintenance_report(self, report_id: int, protocol: dict) -> dict:
        current = self.store.maintenance_report(report_id)
        if current["status"] != "draft":
            raise ValueError("abgeschlossene Wartungsberichte sind unveränderlich")
        clean = validate_protocol(protocol)
        self.store.update_maintenance_protocol(report_id, clean)
        return self.maintenance_report(report_id)

    def complete_maintenance(self, username: str, report_id: int, protocol: dict,
                             auth_level: int, pass4: str, confirmation: str,
                             *, demo: bool = False) -> dict:
        """Write all mapped maintenance values, verify them, then confirm."""
        if demo:
            if str(confirmation).strip() != MAINTENANCE_DEMO_CONFIRMATION:
                raise ValueError(f"zur Demo-Bestätigung exakt {MAINTENANCE_DEMO_CONFIRMATION!r} eingeben")
            clean = validate_protocol(protocol, complete=True)
            self.store.update_maintenance_protocol(report_id, clean)
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
            self.store.complete_maintenance_report(report_id, completion)
            return self.maintenance_report(report_id)
        if str(confirmation).strip() != MAINTENANCE_CONFIRMATION:
            raise ValueError(f"zur Bestätigung exakt {MAINTENANCE_CONFIRMATION!r} eingeben")
        if auth_level < 0:
            raise ValueError("Wartungsabschluss benötigt ein Auth-Level")
        clean = validate_protocol(protocol, complete=True)
        self.store.update_maintenance_protocol(report_id, clean)
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        allowlist = WriteAllowlist()
        audits: list[dict] = []
        with self.serial_lock, self.service.session() as session:
            auth = self.service.authenticate(session, auth_level, pass4 or None)
            if not auth.ok:
                raise PermissionError(f"Auth-Level {auth_level} wurde nicht gewährt")

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
            self.store.audit(_now(), username, 100, audit100)
            audits.append(audit100)
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
            self.store.audit(_now(), username, 104, audit104)
            audits.append(audit104)
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
            "auth_level_granted": auth.granted_level,
            "audits": audits,
        }
        self.store.complete_maintenance_report(report_id, completion)
        return self.maintenance_report(report_id)

    def maintenance_export(self, report_id: int, export_type: str) -> tuple[bytes, str, str]:
        item = self.store.maintenance_report(report_id)
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

    def write_block(self, username: str, block: int, changes: list[dict], auth_level: int, pass4: str, write_enabled: bool) -> dict:
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        block = validate_block(block, writable=True)
        if not changes:
            raise ValueError("keine Änderungen übergeben")
        if write_enabled and auth_level < 0:
            raise ValueError("Hardware-Schreiben benötigt ein Auth-Level")
        allowlist = WriteAllowlist()
        with self.serial_lock, self.service.session() as session:
            auth = None
            if write_enabled and auth_level >= 0:
                auth = self.service.authenticate(session, auth_level, pass4 or None)
                if write_enabled and not auth.ok:
                    raise PermissionError(f"Auth-Level {auth_level} wurde nicht gewährt")
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
    ) -> dict:
        """Apply CPU1/2 block-16 changes through the standard explicit gate."""
        cpu = validate_network_cpu(cpu)
        with self.state_lock:
            if not self.serial_enabled:
                raise TransportError("serielle Verbindung ist getrennt")
        if not changes:
            raise ValueError("keine Änderungen übergeben")
        if write_enabled and auth_level < 0:
            raise ValueError("Hardware-Schreiben benötigt ein Auth-Level")
        allowlist = WriteAllowlist()
        with self.serial_lock, self.service.session() as session:
            auth = None
            if write_enabled and auth_level >= 0:
                auth = self.service.authenticate(session, auth_level, pass4 or None)
                if not auth.ok:
                    raise PermissionError(f"Auth-Level {auth_level} wurde nicht gewährt")
            current = self.service.read_block(session, NETWORK_PROTECTION_BLOCK, cpu=cpu)
            if not current.ok:
                raise RuntimeError(f"Netzschutz CPU {cpu}, Block 16 konnte nicht gelesen werden")
            before = bytes(current.payload)
            # Decode first so unexpected/short controller layouts can never be
            # edited merely because the transport returned a status byte.
            decode_network_protection(cpu, before)
            after = bytearray(before)
            changed_keys: list[str] = []
            for change in changes:
                key = str(change.get("key", ""))
                encode_network_protection_value(after, cpu, key, change.get("value", ""))
                changed_keys.append(key)
            audit = self.service.write_payload(
                session,
                NETWORK_PROTECTION_BLOCK,
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
        self.store.audit(_now(), username, NETWORK_PROTECTION_BLOCK, payload)
        return payload

    def write_power_target(self, username: str, value: object, auth_level: int, pass4: str) -> dict:
        """Always execute the dashboard generator target as a checked live write."""
        return self.write_block(
            username,
            POWER_TARGET_BLOCK,
            [{"key": POWER_TARGET_KEY, "value": value}],
            auth_level,
            pass4,
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
    server_version = "OpenDachsManager/0.9"

    @property
    def app(self) -> DachsWebApp:
        return self.server.dachs_app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        return

    def _token(self) -> str | None:
        for item in (self.headers.get("Cookie", "").split(";")):
            name, _, value = item.strip().partition("=")
            if name == "open_dachs_session":
                return value
        return None

    def _user(self) -> dict | None:
        return self.app.session_user(self._token())

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8",
              cookies: list[str] | None = None, headers: dict[str, str] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
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

    def _require(self, admin: bool = False) -> dict | None:
        user = self._user()
        if user is None:
            self._error(401, "login required")
            return None
        if admin and user.get("role") != "admin":
            self._error(403, "admin role required")
            return None
        return user

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1_000_000:
            raise ValueError("request too large")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON object expected")
        return payload

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
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
                        if key not in self.app.pack.field_map(block):
                            raise KeyError(f"unknown field: {key}")
                        seen_ids.add(series_id)
                        requests.append((series_id, block, key))
                    limit = min(HISTORY_MAX_POINTS, max(1, int(query.get("limit", [str(HISTORY_MAX_POINTS)])[0])))
                    start, end, duration = _history_bounds(query)
                    points = self.app.store.measurements_batch(requests, start, end, limit)
                    return self._json({
                        "series": points,
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                        "hours": duration / 3600.0,
                    })
                if path == "/api/audit":
                    if user.get("role") != "admin":
                        return self._error(403, "admin role required")
                    return self._json({"items": self.app.store.audits()})
                network_match = re.fullmatch(r"/api/network-protection/([12])", path)
                if network_match:
                    return self._json(self.app.read_network_protection(int(network_match.group(1))))
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
        path = parsed.path
        try:
            payload = self._body()
            if path == "/api/login":
                result = self.app.login(str(payload.get("username", "")), str(payload.get("password", "")))
                if result is None:
                    return self._error(401, "Benutzername oder Passwort falsch")
                token, role = result
                cookie = f"open_dachs_session={token}; Path=/; Max-Age=43200; HttpOnly; SameSite=Strict"
                return self._json({"ok": True, "role": role}, cookies=[cookie])
            if path == "/api/logout":
                self.app.logout(self._token())
                return self._json({"ok": True}, cookies=["open_dachs_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"])
            if path == "/api/password":
                user = self._require(admin=True)
                if user is None:
                    return
                self.app.change_password(user["username"], str(payload.get("current_password", "")), str(payload.get("new_password", "")))
                self.app.logout(self._token())
                return self._json({"ok": True, "message": "Passwort geändert; bitte neu anmelden."}, cookies=["open_dachs_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"])
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
                    int(payload.get("auth_level", -1)), str(payload.get("pass4", "")),
                )
                return self._json(result)
            if path == "/api/maintenance/reports":
                user = self._require(admin=True)
                if user is None:
                    return
                return self._json(self.app.create_maintenance_report(user["username"]))
            completion_match = re.fullmatch(r"/api/maintenance/reports/(\d+)/complete", path)
            if completion_match:
                user = self._require(admin=True)
                if user is None:
                    return
                result = self.app.complete_maintenance(
                    user["username"], int(completion_match.group(1)), dict(payload.get("protocol") or {}),
                    int(payload.get("auth_level", -1)), str(payload.get("pass4", "")),
                    str(payload.get("confirmation", "")),
                    demo=not self.app.maintenance_live_writes_enabled,
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
                    int(payload.get("auth_level", -1)), str(payload.get("pass4", "")), bool(payload.get("write_enabled", False)),
                )
                return self._json(result)
            network_match = re.fullmatch(r"/api/network-protection/([12])", path)
            if network_match:
                user = self._require(admin=True)
                if user is None:
                    return
                result = self.app.write_network_protection(
                    user["username"], int(network_match.group(1)),
                    list(payload.get("changes", [])),
                    int(payload.get("auth_level", -1)), str(payload.get("pass4", "")),
                    bool(payload.get("write_enabled", False)),
                )
                return self._json(result)
            return self._error(404, "unknown API path")
        except (ValueError, KeyError) as exc:
            self._error(400, str(exc))
        except PermissionError as exc:
            self._error(403, str(exc))
        except TransportError as exc:
            self._error(503, str(exc))
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
        self._send(200, target.read_bytes(), content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else ""))


class DachsHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: DachsWebApp):
        self.dachs_app = app
        super().__init__(address, DachsRequestHandler)


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
    parser.add_argument("--interval", type=float, default=float(os.environ.get("OPEN_DACHS_WEB_INTERVAL", "0.75")))
    parser.add_argument(
        "--maintenance-live-writes",
        action="store_true",
        default=os.environ.get("OPEN_DACHS_MAINTENANCE_LIVE_WRITES", "0").strip().lower() in {"1", "true", "yes", "on"},
        help="enable the real block-100/block-104 maintenance completion; default is safe demo mode",
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
        serial_socket=args.serial_socket,
        maintenance_live_writes=args.maintenance_live_writes,
    )
    server = DachsHTTPServer((args.host, args.web_port), app)
    app.start()
    print(f"Open Dachs Manager: http://{args.host}:{args.web_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
