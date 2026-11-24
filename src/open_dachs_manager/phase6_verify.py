"""Temporary read-only Phase-6 RAM verification logger (variant A).

Specification: ``reports/firmware-deep/phase6-live-leseplan.md`` (§1 cells,
§2 procedure).  The logger runs inside the existing web-monitor lease, only
issues service-0x02 RAM reads through :func:`verify_read_request` and never
authenticates or writes.  It is off by default and only becomes active through
``<data_dir>/phase6_verify.json`` with an end date; a hard 14-day limit from
the first activation of a run is enforced regardless of that end date.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from .mapping import _format_version


PHASE6_SCHEMA = "dachs-phase6-verify/v1"
PHASE6_CONFIG_NAME = "phase6_verify.json"
PHASE6_LOG_DIRNAME = "phase6-verify"
PHASE6_STATE_NAME = "state.json"
PHASE6_MAX_RUN_SECONDS = 14 * 86400
PHASE6_LOG_MAX_BYTES_PER_FILE = 50 * 1024 * 1024
PHASE6_LOG_MAX_BYTES_TOTAL = 14 * PHASE6_LOG_MAX_BYTES_PER_FILE
# Reads stop this far below a cap, so that no sent read remains unlogged.
PHASE6_LOG_MARGIN_BYTES = 256 * 1024
PHASE6_CONFIG_RELOAD_SECONDS = 10.0
# Firmware binding (plan §0 "Firmwarebindung", RUNBOOK 2026-08-27): CPU-0 B20
# version fields of the analysed images.  The Überwachung field is the version
# of both network CPUs as reported by the main controller.
PHASE6_EXPECTED_VERSIONS = {
    "regler": "R 000.004.043",
    "ueberw": "U 00.003.050",
}
PHASE6_CPU0_FIRMWARE_SHA256 = "07dbe0d12833a731fbe5af47613b93236a54397335d0df0f66bcb832ba1a30bc"
PHASE6_VERSION_MAX_AGE_SECONDS = 60.0

TAKT_SECONDS = {"S": 10.0, "M": 60.0, "Z": 600.0, "E": 86400.0}
TAKT_PRIORITY = {"P0": 0, "E": 1, "Z": 2, "M": 3, "S": 4}
F_WINDOW_SECONDS = 180.0
F_WINDOWS_PER_HOUR = 6
NIGHT_BEFORE_SECONDS = 120
NIGHT_AFTER_SECONDS = 600
NIGHT_GATE_SECONDS = 21600
NIGHT_BASE_TOD = 10800
# Bus budget (plan §2.3): at most three Phase-6 frames per 0.75-s cycle
# (~180 ms at ~60 ms RTT, below the 0.2-s cycle extension limit).  Each network
# CPU gets a token bucket of two frames with two frames per minute refill and
# at most two frames per cycle; only one network CPU is served per cycle.
MAX_FRAMES_PER_CYCLE = 3
NET_CPU_BURST = 2
NET_CPU_FRAMES_PER_MINUTE = 2.0
# Abort thresholds (plan §2.5).
READ_FAILURE_LIMIT = 3
READ_FAILURE_WINDOW_SECONDS = 600.0
LINK_ERROR_RATIO = 0.01
LINK_ERROR_MIN_FRAMES = 100
SLOW_FAST_CYCLE_SECONDS = 1.5
SLOW_FAST_CYCLE_LIMIT = 3
# §2.5/5 pause mode (deviation from the plan's immediate abort, analysed in
# reports/odm-webzyklus-analyse-2026-10-26.md): only real fast cycles count –
# slow-poll cycles (~1.8 s by design) and the hourly retention cycle are
# skipped.  Three slow fast cycles pause all Phase-6 reads; 20 healthy fast
# cycles in a row (~15 s, always spanning one 10-s slow poll) resume them.
# A pause that lasts 10 min, or the 7th pause within one hour, aborts: then
# the web monitor is degraded persistently, not by a transient local stall.
SLOW_PAUSE_RESUME_CYCLES = 20
SLOW_PAUSE_ABORT_SECONDS = 600.0
SLOW_PAUSES_PER_HOUR_LIMIT = 6
UNAVAILABLE_SLOW_CYCLE_LIMIT = 3

STATE_GROUPS = {
    **{code: "S0" for code in (0, 15, 16)},
    **{code: "S1" for code in (20, 21, 22, 23, 24, 30, 31, 32)},
    **{code: "S2" for code in (33, 34, 35)},
    **{code: "S3" for code in (11, 12, 13)},
}

BLOCK_BASES = {22: 0x0E7A, 24: 0x0ED4, 26: 0x0F1A, 50: 0x1ACC}


class Phase6GuardError(ValueError):
    """A request outside the fixed read-only contract (programming error)."""


class Phase6ReadError(RuntimeError):
    """A read failed validation (no ACK, wrong echo or wrong length)."""


@dataclass(frozen=True)
class VerifyField:
    id: str
    paket: str
    cpu: int
    addr: int
    length: int
    takt: str
    fast: bool = False
    double: bool = False
    group: str = ""
    states: tuple[str, ...] = ()
    s_states: tuple[str, ...] = ()
    first_hour_s: bool = False
    window_only: bool = False
    condition: str = ""
    monotonic: str = ""
    note: str = ""


def _f(id, cpu, addr, length, takt, **kw) -> VerifyField:
    return VerifyField(id=id, paket=id.split(".")[0], cpu=cpu, addr=addr, length=length, takt=takt, **kw)


# Fixed allowlist: every service-0x02 request of plan §1 (47 requests:
# CPU 0 25, CPU 1 10, CPU 2 12, including P0).  Cells that the plan sources
# from the polled blocks 22/24/26/50 are listed in PHASE6_BLOCK_CELLS and are
# logged from the cycle context instead of being read twice.
PHASE6_VERIFY_FIELDS: tuple[VerifyField, ...] = (
    # P0 – base verification (one-off, S0 and S2; compared with the same cycle)
    _f("P0.1", 0, 0x0E7A, 64, "P0", double=True, note="Block-22-Basis"),
    _f("P0.2", 0, 0x0ED4, 64, "P0", double=True, note="Block-24-Basis"),
    _f("P0.3", 0, 0x0F1A, 64, "P0", double=True, note="Block-26-Basis"),
    _f("P0.4", 0, 0x0E66, 20, "P0", states=("S0",), note="Block-20-Basis (Hypothese)"),
    _f("P0.5", 1, 0x0772, 2, "P0", note="= CPU-1-B20 Offset 11/12"),
    _f("P0.5", 2, 0x0772, 2, "P0", note="= CPU-2-B20 Offset 11/12"),
    # P1 – release mask, cycle lock, demand
    _f("P1.4", 0, 0x0D78, 10, "S", fast=True, note="Taktsperre 0x0d78..0x0d81"),
    _f("P1.6", 0, 0x1B5A, 1, "Z", note="Handbetrieb/Aktortest; Ausschluss P2/P3"),
    # P2 – actuator, power control, start counters (P1.5 = 0x2232 inside P2.1)
    _f("P2.1", 0, 0x222A, 19, "S", fast=True, note="0x222a..0x223c inkl. P1.5 0x2232"),
    _f("P2.7", 0, 0x0DD2, 1, "S", note="Startwiederholzähler"),
    _f("P2.8", 0, 0x1CA3, 2, "S", window_only=True, note="Start-/Anlasserergebnis je UC"),
    # P4 – counters and time base
    _f("P4.4", 0, 0x2798, 4, "M", double=True, monotonic="tick_ms", note="1-ms-Tick"),
    _f("P4.5", 0, 0x163E, 6, "M", monotonic="seconds", note="Reglerzeit + Tageszeitindex"),
    _f("P4.6", 0, 0x0E00, 4, "Z", monotonic="non_increasing", note="Stack-Hochwassermarke"),
    # P5 – status, acknowledgement, communication
    _f("P5.2", 0, 0x0D6E, 5, "M", note="Warnzeit + Quittungskontingent"),
    _f("P5.3", 0, 0x1C9C, 2, "M", note="Anforderung ohne Netzfreigabe"),
    _f("P5.4", 0, 0x1D15, 1, "Z", note="24-h-Karenz SC177"),
    _f("P5.5", 0, 0x202E, 2, "Z", note="Modemzustand"),
    _f("P5.6", 0, 0x28FC, 1, "Z", note="BSB-Stationsstatus"),
    _f("P5.7", 0, 0x27A8, 1, "Z", note="Tick-Trimm"),
    _f("P5.8", 0, 0x1030, 2, "Z", note="Kommando-5-Wort"),
    _f("P5.9", 0, 0x2780, 2, "Z", note="Fern-Stromanforderung"),
    _f("P5.10", 0, 0x1C79, 2, "Z", first_hour_s=True, note="Empfangsteilezähler (1 h S, dann Z)"),
    _f("P5.11", 0, 0x2312, 6, "S", states=("S0", "S2"), note="5. Teiltelegramm Messplatine"),
    _f("P5.12", 0, 0x224C, 1, "E", note="Netzschutz-Norm"),
    _f("P5.12", 0, 0x1BC8, 2, "E", note="UC-Protokollstand"),
    _f("P5.13", 0, 0x0D39, 1, "Z", condition="zweiter_we", note="2. WE Mindestlaufzeit (bedingt)"),
    # P6 – network CPU 1 (destination 0x11)
    _f("P4.7", 1, 0x0556, 4, "Z", double=True, monotonic="seconds", note="CPU-1-Laufzeitsekunden"),
    _f("P6.1", 1, 0x0E1A, 26, "S", group="snap1", states=("S0", "S2"), note="Messwert-Schnappschuss"),
    _f("P6.2", 1, 0x0F14, 2, "S", group="snap1", note="Pulswert"),
    _f("P6.3", 1, 0x0E40, 6, "E", note="Kreuzvergleich-Toleranzen"),
    _f("P6.4", 1, 0x0E46, 52, "M", group="akku1", states=("S0", "S2"), note="IIR-Akkus eigen"),
    _f("P6.4", 1, 0x0E7A, 52, "M", group="akku1", states=("S0", "S2"), note="IIR-Akkus Partner"),
    _f("P6.5", 1, 0x0F76, 14, "Z", s_states=("S1", "S3"), note="Schaltablauf/Ausgänge"),
    _f("P6.6", 1, 0x09F4, 6, "E", note="Impedanz intern"),
    _f("P6.7", 1, 0x0B82, 30, "M", states=("S2",), note="Mittelwerte/Winkel"),
    # P7 – network CPU 2 (destination 0x12)
    _f("P7.1", 2, 0x0E14, 26, "S", group="snap2", states=("S0", "S2"), note="Partnervergleich-Mittel"),
    _f("P7.2", 2, 0x0F0E, 2, "S", group="snap2", note="Pulsrate"),
    _f("P7.3", 2, 0x0E3A, 6, "E", note="Akku-Toleranzen"),
    _f("P7.4", 2, 0x0E40, 48, "M", group="akku2", states=("S0", "S2"), note="Akkus 0x0e40..0x0e6f"),
    _f("P7.4", 2, 0x0E70, 56, "M", group="akku2", states=("S0", "S2"), note="Akkus 0x0e70..0x0ea7"),
    _f("P7.5", 2, 0x0F53, 4, "Z", s_states=("S1", "S3"), note="Sammelfreigaben"),
    _f("P7.6", 2, 0x0F70, 5, "Z", s_states=("S1", "S3"), note="Schaltsequenz/Portbild"),
    _f("P7.7", 2, 0x0F9C, 6, "Z", note="Spannungsschutz-Statusmasken"),
    _f("P7.8", 2, 0x09D4, 24, "Z", states=("S2",), note="Impedanz-/LoM-Akkus"),
    _f("P7.8", 2, 0x09F6, 6, "E", note="Impedanz-/LoM-Grenzen"),
    _f("P7.9", 2, 0x0A34, 3, "S", states=("S0", "S1"), note="Digitaleingänge 2-bit"),
)

# Plan cells taken from the polled blocks (no extra frame): (id, address,
# width, block).  Logged in every context line of the cycle.
PHASE6_BLOCK_CELLS: tuple[tuple[str, int, int, int], ...] = (
    ("P1.1", 0x0EA0, 2, 22), ("P1.2", 0x0E9E, 2, 22),
    ("P1.3", 0x0EA4, 1, 22), ("P1.3", 0x0EA8, 1, 22),
    ("P2.2", 0x0ED8, 2, 24), ("P2.2", 0x0ED4, 1, 24),
    ("P2.3", 0x0F04, 2, 24), ("P2.3", 0x0F06, 2, 24),
    ("P2.5", 0x0F35, 1, 26), ("P2.5", 0x0F3D, 1, 26),
    ("P3.1", 0x0EFE, 2, 24), ("P3.2", 0x0EFF, 1, 24), ("P3.2", 0x0F00, 1, 24), ("P3.2", 0x0F02, 1, 24),
    ("P3.3", 0x0F33, 1, 26), ("P3.4", 0x0F3C, 1, 26), ("P3.4", 0x0F3E, 1, 26),
    ("P3.5", 0x0F4E, 8, 26),
    ("P4.1", 0x0E7A, 4, 22), ("P4.1", 0x0E7E, 4, 22), ("P4.1", 0x0E82, 4, 22),
    ("P4.2", 0x0E86, 4, 22), ("P4.2", 0x0E8A, 4, 22),
    ("P4.3", 0x0F0E, 4, 24), ("P5.1", 0x0ED4, 1, 24),
    ("P1.5", 0x1AFB, 1, 50), ("ctx.sc", 0x0E91, 1, 22), ("ctx.warnung", 0x0E90, 1, 22),
)

_ALLOWED = frozenset((f.cpu, f.addr, f.length) for f in PHASE6_VERIFY_FIELDS)


def allowlist_sha256() -> str:
    data = json.dumps([asdict(f) for f in PHASE6_VERIFY_FIELDS], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def check_verify_payload(cpu: object, payload: bytes) -> None:
    """Reject everything except an allowlisted four-byte service-0x02 read."""
    if isinstance(cpu, bool) or not isinstance(cpu, int) or cpu not in (0, 1, 2):
        raise Phase6GuardError(f"Phase 6: CPU {cpu!r} nicht erlaubt")
    payload = bytes(payload)
    if len(payload) != 4:
        raise Phase6GuardError(f"Phase 6: Anfrage muss 4 Byte lang sein, nicht {len(payload)}")
    if payload[0] != 0x02:
        raise Phase6GuardError(f"Phase 6: Dienst 0x{payload[0]:02x} gesperrt, nur 0x02")
    addr = payload[1] << 8 | payload[2]
    length = payload[3]
    if not 1 <= length <= 64:
        raise Phase6GuardError(f"Phase 6: Länge {length} außerhalb 1..64")
    if addr < 0x0400:
        raise Phase6GuardError(f"Phase 6: Adresse 0x{addr:04x} im SFR-Bereich gesperrt")
    if addr + length - 1 > 0xFFFF:
        raise Phase6GuardError("Phase 6: Adressüberlauf")
    if (cpu, addr, length) not in _ALLOWED:
        raise Phase6GuardError(f"Phase 6: CPU {cpu} 0x{addr:04x}/{length} nicht in der Allowlist")


def verify_read_request(cpu: object, addr: object, length: object) -> bytes:
    """The only Phase-6 payload builder; raises before anything is sent."""
    for name, value in (("addr", addr), ("length", length)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise Phase6GuardError(f"Phase 6: {name} muss eine Ganzzahl sein")
    if not 1 <= length <= 64:
        raise Phase6GuardError(f"Phase 6: Länge {length} außerhalb 1..64")
    if not 0x0400 <= addr <= 0xFFFF:
        raise Phase6GuardError(f"Phase 6: Adresse {addr:#x} gesperrt")
    if addr + length - 1 > 0xFFFF:
        raise Phase6GuardError("Phase 6: Adressüberlauf")
    payload = bytes([0x02, (addr >> 8) & 0xFF, addr & 0xFF, length])
    check_verify_payload(cpu, payload)
    return payload


def guarded_request(session, cpu: int, payload: bytes, timeout: float = 0.9):
    """Send through ``session.request`` only after a second guard check."""
    check_verify_payload(cpu, payload)
    return session.request(bytes(payload), session.next_packet(), timeout=timeout, cpu=cpu)


def validate_verify_response(field_cpu: int, payload: bytes, response) -> bytes:
    """Return the data bytes of an exact ``82 hi lo len data`` reply (fail-closed)."""
    ack = getattr(response, "ack", None)
    if ack is None or getattr(ack, "positive", None) is not True:
        raise Phase6ReadError("kein positives ACK")
    data = getattr(response, "data", None)
    body = bytes(getattr(data, "payload", b"")) if data is not None else b""
    expected = bytes([0x82]) + bytes(payload[1:4])
    length = payload[3]
    if not body.startswith(expected):
        raise Phase6ReadError(f"falsches Echo {body[:4].hex() or 'leer'}")
    if len(body) != 4 + length:
        raise Phase6ReadError(f"falsche Länge {len(body) - 4} statt {length}")
    return body[4:]


def state_group(status: object) -> str | None:
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return STATE_GROUPS.get(status, "S?")


def _cell(payloads: dict, addr: int, width: int, block: int) -> int | None:
    payload = payloads.get(block)
    base = BLOCK_BASES[block]
    offset = addr - base
    if payload is None or offset < 0 or offset + width > len(payload):
        return None
    return int.from_bytes(payload[offset:offset + width], "little")


def night_window_delta(reglerzeit: int, starts: int) -> int:
    """Seconds relative to today's single-module night shutdown time N."""
    target = NIGHT_BASE_TOD + 60 * (int(starts) % 100)
    delta = int(reglerzeit) % 86400 - target
    if delta >= 43200:
        delta -= 86400
    elif delta < -43200:
        delta += 86400
    return delta


def compare_base(block_payload: bytes, first: bytes, second: bytes) -> tuple[bool, dict]:
    """P0 comparison of a 64-byte 0x02 read pair with the same-cycle block.

    Bytes equal to either read pass; bytes that differ between both reads are
    volatile (live measurements) and pass; counter LSBs may differ by one.
    The base passes when at most four unexplained bytes remain and at least
    90 % of the compared bytes are explained (equal, volatile or counter LSB).
    Volatile bytes count as explained since 1.8.9: with the engine running,
    block 26 carries too many live measurements for a plain equality quota,
    which aborted every run at the S2 base check (2026-10-27).
    """
    count = min(len(block_payload), len(first), len(second))
    equal = volatile = unexplained = 0
    mismatches = []
    for index in range(count):
        b, x, y = block_payload[index], first[index], second[index]
        if b in (x, y):
            equal += 1
            continue
        if x != y or abs(b - x) <= 1:
            volatile += 1
            continue
        unexplained += 1
        mismatches.append(index)
    ok = count > 0 and unexplained <= 4 and (count - unexplained) >= 0.9 * count
    return ok, {"compared": count, "equal": equal, "volatile": volatile, "unexplained": mismatches}


def _parse_utc(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_config(path: Path) -> tuple[dict | None, str | None]:
    """Return ``(config, error)``; a missing file means disabled (default)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"Konfiguration nicht lesbar: {exc}"
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "Konfiguration ist kein gültiges JSON"
    if not isinstance(data, dict):
        return None, "Konfiguration muss ein JSON-Objekt sein"
    if data.get("enabled") is not True:
        return {"enabled": False}, None
    end_at = _parse_utc(data.get("end_at"))
    if end_at is None:
        return None, "end_at fehlt oder ist kein ISO-Zeitpunkt mit Zeitzone"
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", run_id):
        return None, "run_id fehlt oder ist ungültig ([A-Za-z0-9._-], max. 64)"
    approval = data.get("approval", "")
    if not isinstance(approval, str) or len(approval) > 500:
        return None, "approval muss Text bis 500 Zeichen sein"
    return {"enabled": True, "end_at": end_at, "run_id": run_id, "approval": approval}, None


class Phase6Verifier:
    """State machine, scheduler and JSONL logger for the Phase-6 reads."""

    def __init__(self, data_dir: str | Path, *, version: str = "", serial_socket: str = "",
                 write_audit_count=None, clock=None):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / PHASE6_CONFIG_NAME
        self.log_dir = self.data_dir / PHASE6_LOG_DIRNAME
        self.state_path = self.log_dir / PHASE6_STATE_NAME
        self.version = version
        self.serial_socket = serial_socket
        self.write_audit_count = write_audit_count or (lambda: None)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self.config: dict | None = None
        self.config_error: str | None = None
        self._config_checked = float("-inf")
        self.persisted = self._load_state()
        self.state = "aus"
        self.reason: str | None = None
        self.counters = {
            "frames": 0, "reads_ok": 0, "reads_failed": 0, "crc_errors": 0,
            "protocol_errors": 0, "f_windows": 0, "night_windows": 0,
            "net_frames_cpu1": 0, "net_frames_cpu2": 0, "log_lines": 0,
            "slow_pauses": 0,
        }
        self._reset_run_memory()
        self._boot_restart_checked = False

    # ---------------------------------------------------------------- state
    def _reset_run_memory(self) -> None:
        self.next_due: dict[tuple, float] = {}
        self.run_started_mono: float | None = None
        self.prev_group: str | None = None
        self.prev_freigabe: int | None = None
        self.f_window_until = float("-inf")
        self.f_window_label = "-"
        self.f_window_opens: deque = deque()
        self.night_until = float("-inf")
        self.night_key: tuple | None = None
        self.hand_mode: bool | None = None
        self.hand_checked = float("-inf")
        self.reglerzeit: tuple[int, float] | None = None
        self.versions: dict | None = None
        self.versions_mono = float("-inf")
        self.p0_pending: list[str] = []
        self.p0_done_groups: set[str] = set()
        self.p0_4_done = False
        self.failures: deque = deque()
        self.slow_cycles = 0
        self.slow_recent: deque = deque(maxlen=SLOW_FAST_CYCLE_LIMIT)
        self.slow_pause_since: float | None = None
        self.slow_pause_healthy = 0
        self.slow_pause_max = 0.0
        self.slow_pause_opens: deque = deque()
        self._slow_pause_state = "läuft"
        self.display_unavailable = 0
        self.ram_unavailable = 0
        self.tokens = {1: float(NET_CPU_BURST), 2: float(NET_CPU_BURST)}
        self.tokens_mono: float | None = None
        self.net_turn = 1
        self.last_values: dict[tuple, tuple[int, float]] = {}
        self.baseline_warnings: int | None = None
        self.baseline_warning_code: int | None = None
        self.socket_identity = None
        self.seq = 0
        self.cycle_no = 0
        self.cycle_frames = 0
        self._p0_group: str | None = None
        self._log_total: int | None = None

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.persisted, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def status(self) -> dict:
        with self.lock:
            end = self.effective_end()
            return {
                "schema": PHASE6_SCHEMA,
                "enabled": bool(self.config and self.config.get("enabled")),
                "state": self.state,
                "reason": self.reason,
                "run_id": (self.config or {}).get("run_id"),
                "started_at": self.persisted.get("started_at"),
                "end_at": _iso(end),
                "f_window": self.f_window_label if self.state == "läuft" else "-",
                "hand_mode": self.hand_mode,
                "counters": dict(self.counters),
                "slow_pause": None if self.slow_pause_since is None else {
                    "healthy_cycles": self.slow_pause_healthy,
                    "resume_after": SLOW_PAUSE_RESUME_CYCLES,
                    "max_elapsed_s": round(self.slow_pause_max, 3),
                },
            }

    def effective_end(self) -> datetime | None:
        if not self.config or not self.config.get("enabled"):
            return None
        end = self.config["end_at"]
        started = _parse_utc(self.persisted.get("started_at"))
        if started is not None and self.persisted.get("run_id") == self.config["run_id"]:
            end = min(end, started + timedelta(seconds=PHASE6_MAX_RUN_SECONDS))
        else:
            end = min(end, self.clock() + timedelta(seconds=PHASE6_MAX_RUN_SECONDS))
        return end

    def _set(self, state: str, reason: str | None = None) -> None:
        with self.lock:
            self.state = state
            self.reason = reason

    def _terminal(self, state: str, reason: str) -> None:
        """Stop the logger for this run and keep the reason (plan §2.5)."""
        self._set(state, reason)
        self.persisted.update({"state": state, "reason": reason, "ended_at": _iso(self.clock())})
        self._save_state()
        self._log({"type": "ende", "state": state, "reason": reason,
                   "write_audit_max_id": self._safe_audit(), "counters": dict(self.counters)})

    def _safe_audit(self):
        try:
            return self.write_audit_count()
        except Exception as exc:  # pragma: no cover - defensive
            return f"Fehler: {exc}"

    def _refresh_config(self, now_mono: float) -> None:
        if now_mono - self._config_checked < PHASE6_CONFIG_RELOAD_SECONDS:
            return
        self._config_checked = now_mono
        config, error = load_config(self.config_path)
        with self.lock:
            self.config, self.config_error = config, error

    # ----------------------------------------------------------- lifecycle
    def _update_lifecycle(self, now_mono: float) -> bool:
        """Return True when the logger may work in this cycle."""
        self._refresh_config(now_mono)
        if self.config_error:
            self._set("konfigfehler", self.config_error)
            return False
        if not self.config or not self.config.get("enabled"):
            if self.state in ("läuft", "basisprüfung", "vorbedingungen", "blockiert", "pausiert"):
                self._terminal("beendet", "per Konfiguration deaktiviert")
            elif self.state not in ("abgebrochen", "angehalten", "beendet"):
                self._set("aus", None)
            return False
        run_id = self.config["run_id"]
        if self.persisted.get("run_id") != run_id:
            self.persisted = {"run_id": run_id, "started_at": _iso(self.clock()),
                              "state": "vorbedingungen", "pid": os.getpid()}
            self._save_state()
            self._reset_run_memory()
            self.run_started_mono = now_mono
            self._boot_restart_checked = True
            self._set("vorbedingungen", None)
            self._log_manifest("start")
        elif not self._boot_restart_checked:
            self._boot_restart_checked = True
            previous = self.persisted.get("state")
            if previous in ("abgebrochen", "angehalten", "beendet"):
                self._set(previous, self.persisted.get("reason"))
                return False
            if self.persisted.get("pid") != os.getpid():
                self._terminal("abgebrochen", "Web-Neustart während des Laufs (§2.5/4)")
                return False
        if self.state in ("abgebrochen", "angehalten", "beendet"):
            return False
        if self.clock() >= self.effective_end():
            self._terminal("beendet", "Enddatum bzw. 14-Tage-Obergrenze erreicht (§2.5/8)")
            return False
        if self.state == "aus" or self.state == "konfigfehler":
            self._set("vorbedingungen", None)
        if self.run_started_mono is None:
            self.run_started_mono = now_mono
        return True

    # -------------------------------------------------------------- logging
    def _log_path(self) -> Path:
        return self.log_dir / f"{self.clock().strftime('%Y%m%d')}.jsonl"

    def _log(self, record: dict) -> bool:
        path = self._log_path()
        line = json.dumps({"schema": PHASE6_SCHEMA, "run_id": (self.config or {}).get("run_id"),
                           "ts_utc": _iso(self.clock()), **record},
                          separators=(",", ":"), ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        if self._log_total is None:
            self._log_total = sum(p.stat().st_size for p in self.log_dir.glob("*.jsonl") if p.is_file())
        if size + len(data) > PHASE6_LOG_MAX_BYTES_PER_FILE \
                or self._log_total + len(data) > PHASE6_LOG_MAX_BYTES_TOTAL:
            return False
        with path.open("ab") as handle:
            handle.write(data)
        self._log_total += len(data)
        self.counters["log_lines"] += 1
        return True

    def _log_manifest(self, kind: str) -> None:
        self._log({
            "type": "manifest", "event": kind, "product_version": self.version,
            "cpu0_firmware_sha256": PHASE6_CPU0_FIRMWARE_SHA256,
            "expected_versions": PHASE6_EXPECTED_VERSIONS,
            "allowlist_sha256": allowlist_sha256(),
            "allowlist_requests": len(PHASE6_VERIFY_FIELDS),
            "started_at": self.persisted.get("started_at"),
            "end_at": _iso(self.effective_end()),
            "approval": (self.config or {}).get("approval", ""),
            "pid": os.getpid(), "write_audit_max_id": self._safe_audit(),
            "serial_socket": self.serial_socket,
            "block_bases": {str(block): f"0x{base:04x}" for block, base in BLOCK_BASES.items()},
            "block_cells": [
                {"paket": cell_id, "addr": f"0x{addr:04x}", "len": width, "block": block,
                 "offset": addr - BLOCK_BASES[block]}
                for cell_id, addr, width, block in PHASE6_BLOCK_CELLS
            ],
        })

    # ------------------------------------------------------------- context
    def _context(self, payloads: dict, now_mono: float) -> dict:
        reglerzeit = None
        if self.reglerzeit is not None:
            reglerzeit = self.reglerzeit[0] + int(now_mono - self.reglerzeit[1])
        window = []
        if now_mono < self.f_window_until:
            window.append(self.f_window_label)
        if now_mono < self.night_until:
            window.append("N")
        freigabe = _cell(payloads, 0x0EA0, 2, 22)
        typcode = _cell(payloads, 0x0F0A, 1, 24)
        leistung = _cell(payloads, 0x0ED8, 2, 24)
        if leistung is not None and leistung >= 0x8000:
            leistung -= 0x10000
        return {
            "motorstatus": _cell(payloads, 0x0ED4, 1, 24),
            "freigabe": None if freigabe is None else f"0x{freigabe:04x}",
            "sc": _cell(payloads, 0x0E91, 1, 22),
            "drehzahl": _cell(payloads, 0x0ED6, 2, 24),
            "leistung_w": leistung,
            "typcode": None if typcode is None else f"0x{typcode:02x}",
            "reglerzeit": reglerzeit,
            "fenster": "|".join(window) or "-",
            "hand": self.hand_mode,
        }

    def _log_context(self, payloads: dict, now_mono: float) -> None:
        """Raw B22/B24/B26 bytes 0..63 of this cycle (cell map in the manifest)."""
        blocks = {str(b): payloads[b][:64].hex() for b in (22, 24, 26) if b in payloads}
        if 50 in payloads:
            blocks["50"] = payloads[50][:64].hex()
        if not self._log({"type": "kontext", "cycle": self.cycle_no, "ts_mono": round(now_mono, 3),
                          "blocks": blocks}):
            raise _LogFull()

    def _log_budget(self) -> str | None:
        """Return why no further read may start because the log is (nearly) full."""
        try:
            size = self._log_path().stat().st_size
        except FileNotFoundError:
            size = 0
        if self._log_total is not None \
                and self._log_total >= PHASE6_LOG_MAX_BYTES_TOTAL - PHASE6_LOG_MARGIN_BYTES:
            return "total"
        if size >= PHASE6_LOG_MAX_BYTES_PER_FILE - PHASE6_LOG_MARGIN_BYTES:
            return "day"
        return None

    # ------------------------------------------------------------- reading
    def _read(self, session, field: VerifyField, payloads: dict, now_mono: float,
              coherent=None) -> bytes | None:
        payload = verify_read_request(field.cpu, field.addr, field.length)
        self.counters["frames"] += 1
        self.cycle_frames += 1
        if field.cpu:
            self.counters[f"net_frames_cpu{field.cpu}"] += 1
        record = {"type": "read", "seq": None, "ts_mono": round(now_mono, 3), "paket": field.id,
                  "cpu": field.cpu, "addr": f"0x{field.addr:04x}", "len": field.length,
                  "raw_hex": None, "ack": False, "rtt_ms": None, "crc_errors": 0,
                  "protocol_errors": 0, "coherent": False, "cycle": self.cycle_no,
                  "ctx": self._context(payloads, now_mono), "error": None}
        data = None
        try:
            response = guarded_request(session, field.cpu, payload)
            record["rtt_ms"] = round(float(getattr(response, "elapsed_ms", 0.0)), 1)
            record["crc_errors"] = int(getattr(response, "crc_errors", 0) or 0)
            record["protocol_errors"] = int(getattr(response, "protocol_errors", 0) or 0)
            self.counters["crc_errors"] += record["crc_errors"]
            self.counters["protocol_errors"] += record["protocol_errors"]
            data = validate_verify_response(field.cpu, payload, response)
            record["ack"] = True
            record["raw_hex"] = data.hex()
            record["coherent"] = self._coherent(field, data, now_mono) if coherent is None else coherent
            if field.cpu == 0 and field.addr == 0x163E:
                self.reglerzeit = (int.from_bytes(data[:4], "little"), now_mono)
            self.counters["reads_ok"] += 1
        except Phase6GuardError:
            raise
        except Exception as exc:
            record["error"] = str(exc) or type(exc).__name__
            self.counters["reads_failed"] += 1
            self.failures.append(now_mono)
            data = None
        self.seq += 1
        record["seq"] = self.seq
        if not self._log(record):
            raise _LogFull()
        return data

    def _coherent(self, field: VerifyField, data: bytes, now_mono: float) -> bool:
        if not field.monotonic:
            return True
        width = 4 if field.length >= 4 else field.length
        value = int.from_bytes(data[:width], "little")
        key = (field.cpu, field.addr)
        previous = self.last_values.get(key)
        self.last_values[key] = (value, now_mono)
        if previous is None:
            return True
        last, last_mono = previous
        elapsed = max(0.0, now_mono - last_mono)
        delta = value - last
        if field.monotonic == "non_increasing":
            return delta <= 0
        rate = 1000.0 if field.monotonic == "tick_ms" else 1.0
        expected = rate * elapsed
        return 0 <= delta <= expected * 1.05 + rate * 2 + 2

    # ------------------------------------------------------------- windows
    def _update_windows(self, payloads: dict, now_mono: float) -> bool:
        """Update event/night windows; return True when an F window opened."""
        opened = False
        status = _cell(payloads, 0x0ED4, 1, 24)
        freigabe = _cell(payloads, 0x0EA0, 2, 22)
        group = state_group(status)
        trigger = None
        if group is not None and self.prev_group is not None and group != self.prev_group:
            trigger = "F:start" if group == "S1" else "F:stopp" if group in ("S3", "S0") else f"F:{group}"
        if freigabe is not None and self.prev_freigabe == 0xFFFF and freigabe != 0xFFFF:
            trigger = trigger or "F:freigabe"
        if group is not None:
            self.prev_group = group
        if freigabe is not None:
            self.prev_freigabe = freigabe
        while self.f_window_opens and now_mono - self.f_window_opens[0] >= 3600.0:
            self.f_window_opens.popleft()
        if trigger and now_mono >= self.f_window_until:
            if len(self.f_window_opens) < F_WINDOWS_PER_HOUR:
                self.f_window_opens.append(now_mono)
                self.f_window_until = now_mono + F_WINDOW_SECONDS
                self.f_window_label = trigger
                self.counters["f_windows"] += 1
                opened = True
                self._log({"type": "fenster", "fenster": trigger, "ts_mono": round(now_mono, 3),
                           "status": status, "freigabe": freigabe})
            else:
                self._log({"type": "fenster", "fenster": trigger, "unterdrückt": "max. 6 je Stunde",
                           "ts_mono": round(now_mono, 3)})
        # Night shutdown window N−2 … N+10 min (single module only).
        starts = _cell(payloads, 0x0E7E, 4, 22)
        runtime = _cell(payloads, 0x0F0E, 4, 24)
        multimodule = _cell(payloads, 0x1ADD, 1, 50)
        if self.reglerzeit is not None and starts is not None and runtime is not None \
                and now_mono - self.reglerzeit[1] <= 900 and multimodule in (None, 0):
            reglerzeit = self.reglerzeit[0] + int(now_mono - self.reglerzeit[1])
            delta = night_window_delta(reglerzeit, starts)
            key = (reglerzeit // 86400, starts % 100)
            if -NIGHT_BEFORE_SECONDS <= delta <= NIGHT_AFTER_SECONDS and key != self.night_key \
                    and runtime + max(0, -delta) >= NIGHT_GATE_SECONDS:
                self.night_key = key
                self.night_until = now_mono + (NIGHT_AFTER_SECONDS - delta)
                self.counters["night_windows"] += 1
                self._log({"type": "fenster", "fenster": "N", "delta_s": delta,
                           "ts_mono": round(now_mono, 3)})
        return opened

    def in_f_window(self, now_mono: float) -> bool:
        return now_mono < self.f_window_until or now_mono < self.night_until

    # --------------------------------------------------------- scheduling
    def _interval(self, field: VerifyField, group: str | None, now_mono: float) -> float:
        # S and M reads of the network CPUs alternate between CPU 1 and 2
        # (plan §1 P6.1/P6.4, §2.3), i.e. 20 s / 120 s per CPU.
        factor = 2.0 if field.cpu else 1.0
        if field.s_states and group in field.s_states:
            return factor * TAKT_SECONDS["S"]
        if field.first_hour_s and self.run_started_mono is not None \
                and now_mono - self.run_started_mono < 3600:
            return TAKT_SECONDS["S"]
        if field.takt in ("S", "M"):
            return factor * TAKT_SECONDS[field.takt]
        return TAKT_SECONDS[field.takt]

    def _eligible(self, field: VerifyField, group: str | None, payloads: dict, now_mono: float) -> bool:
        if field.paket in ("P2", "P3") and self.hand_mode is not False:
            return False
        if field.states and group not in field.states:
            return False
        if field.window_only and not self.in_f_window(now_mono):
            return False
        if field.condition == "zweiter_we":
            # 0x0fea (2. WE installiert) liegt in keinem gepollten Block und
            # steht nicht in der Allowlist: ohne Beleg kein Read (fail-closed).
            return False
        return True

    def _refill(self, now_mono: float) -> None:
        if self.tokens_mono is None:
            self.tokens_mono = now_mono
            return
        elapsed = max(0.0, now_mono - self.tokens_mono)
        self.tokens_mono = now_mono
        for cpu in self.tokens:
            self.tokens[cpu] = min(float(NET_CPU_BURST),
                                   self.tokens[cpu] + elapsed * NET_CPU_FRAMES_PER_MINUTE / 60.0)

    def _plan_cycle(self, payloads: dict, now_mono: float) -> list[list[VerifyField]]:
        """Return read units (lists read back to back) for this cycle."""
        group = state_group(_cell(payloads, 0x0ED4, 1, 24))
        units: list[list[VerifyField]] = []
        budget = MAX_FRAMES_PER_CYCLE - self.cycle_frames
        if self.in_f_window(now_mono):
            for field in PHASE6_VERIFY_FIELDS:
                if field.fast and self._eligible(field, group, payloads, now_mono) and budget > 0:
                    units.append([field])
                    budget -= 1
                    self.next_due[(field.cpu, field.addr)] = now_mono + self._interval(field, group, now_mono)
        candidates = []
        seen_groups = set()
        for field in PHASE6_VERIFY_FIELDS:
            if field.takt == "P0" or (field.fast and any(u[0] is field for u in units)):
                continue
            if field.group:
                if (field.cpu, field.group) in seen_groups:
                    continue
                seen_groups.add((field.cpu, field.group))
                members = [f for f in PHASE6_VERIFY_FIELDS if f.cpu == field.cpu and f.group == field.group]
            else:
                members = [field]
            if not all(self._eligible(f, group, payloads, now_mono) for f in members):
                continue
            due = self.next_due.get((field.cpu, field.addr), float("-inf"))
            if due > now_mono:
                continue
            candidates.append((TAKT_PRIORITY[field.takt], due, members))
        candidates.sort(key=lambda item: (item[0], item[1]))

        def frames_of(members):
            return len(members) * (2 if members[0].double else 1)

        # Only one network CPU per cycle, alternating, and only within its
        # token bucket (≤ 2 frames in a row, ≤ 2 frames/min on average).
        # The most urgent due unit of a CPU reserves its tokens: lower
        # priority single frames must not starve two-frame units.
        heads = {}
        for _p, _d, members in candidates:
            heads.setdefault(members[0].cpu, members)
        net_cpu = next((
            cpu for cpu in (self.net_turn, 3 - self.net_turn)
            if cpu in heads and frames_of(heads[cpu]) <= self.tokens[cpu] + 1e-9
        ), None)
        net_blocked = False
        for _prio, _due, members in candidates:
            cpu = members[0].cpu
            frames = frames_of(members)
            if cpu and (cpu != net_cpu or net_blocked):
                continue
            if frames > budget:
                continue
            if cpu:
                if frames > self.tokens[cpu] + 1e-9:
                    net_blocked = True
                    continue
                self.tokens[cpu] -= frames
            budget -= frames
            units.append(members)
            for member in members:
                self.next_due[(member.cpu, member.addr)] = now_mono + self._interval(member, group, now_mono)
            if budget <= 0:
                break
        if net_cpu is not None:
            self.net_turn = 3 - net_cpu
        return units

    # ---------------------------------------------------------------- P0
    def _p0_field(self, field_id: str, cpu: int = 0) -> VerifyField:
        return next(f for f in PHASE6_VERIFY_FIELDS if f.id == field_id and f.cpu == cpu)

    def _run_p0_step(self, session, service, payloads: dict, now_mono: float) -> None:
        step = self.p0_pending[0]
        if step in ("P0.1", "P0.2", "P0.3"):
            field = self._p0_field(step)
            block = {"P0.1": 22, "P0.2": 24, "P0.3": 26}[step]
            if block not in payloads:
                return
            first = self._read(session, field, payloads, now_mono, coherent=True)
            second = self._read(session, field, payloads, now_mono, coherent=True) if first is not None else None
            if first is None or second is None:
                self._abort(f"{step}: Basislesung fehlgeschlagen (§2.5/2)")
                return
            ok, detail = compare_base(payloads[block][:64], first, second)
            self._log({"type": "p0", "paket": step, "ok": ok, "detail": detail,
                       "gruppe": state_group(_cell(payloads, 0x0ED4, 1, 24))})
            if not ok:
                self._abort(f"{step}: Block-{block}-Basisvergleich fehlgeschlagen (§2.5/2)")
                return
        elif step in ("P0.5/1", "P0.5/2"):
            cpu = int(step[-1])
            if self.tokens[cpu] < 2 - 1e-9:
                return
            self.tokens[cpu] -= 2
            self.counters["frames"] += 1
            self.cycle_frames += 1
            self.counters[f"net_frames_cpu{cpu}"] += 1
            try:
                block20 = service.read_block(session, 20, cpu=cpu)
                reference = bytes(block20.payload)[11:13] if block20.ok else b""
            except Exception as exc:
                reference = b""
                self._log({"type": "p0", "paket": "P0.5", "cpu": cpu, "error": f"B20: {exc}"})
            value = self._read(session, self._p0_field("P0.5", cpu), payloads, now_mono, coherent=True)
            ok = value is not None and len(reference) == 2 and value == reference
            self._log({"type": "p0", "paket": "P0.5", "cpu": cpu, "ok": ok,
                       "b20_11_12": reference.hex(), "ram_0772": None if value is None else value.hex()})
            if not ok:
                self._abort(f"P0.5 CPU {cpu}: 0x0772 ≠ B20 Offset 11/12 (§2.5/2)")
                return
        self.p0_pending.pop(0)

    def _run_p0_4(self, session, payloads: dict, now_mono: float, block20: bytes | None) -> None:
        field = self._p0_field("P0.4")
        value = self._read(session, field, payloads, now_mono, coherent=True)
        self.p0_4_done = True
        hit = value is not None and block20 is not None and block20[:20] == value
        self._log({"type": "p0", "paket": "P0.4", "treffer": hit,
                   "deutung": "Block-20-Basis 0x0e66 bestätigt" if hit
                   else "kein Treffer: B20 nicht RAM-linear oder Basis abweichend (kein Abbruch)"})

    # -------------------------------------------------------------- abort
    def _abort(self, reason: str) -> None:
        self._terminal("abgebrochen", reason)

    def _check_versions(self, payloads: dict, now_mono: float) -> str | None:
        """Update firmware binding from CPU-0 B20; return a mismatch text."""
        block20 = payloads.get(20)
        if block20 is not None and len(block20) >= 39:
            versions = {
                "regler": _format_version("Hka_Bd_Stat.bSoftwareVersionRegler", list(block20[34:39])),
                "ueberw": _format_version("Hka_Bd_Stat.bSoftwareVersionUeberw", list(block20[26:30])),
                "messen": _format_version("Hka_Bd_Stat.bSoftwareVersionMessen", list(block20[30:34])),
            }
            self.versions = versions
            self.versions_mono = now_mono
        if self.versions is None:
            return None
        for key, expected in PHASE6_EXPECTED_VERSIONS.items():
            if self.versions.get(key) != expected:
                return f"Firmwarebindung: {key} {self.versions.get(key)} ≠ {expected}"
        return None

    def _socket_identity(self):
        if not self.serial_socket:
            return None
        try:
            info = os.stat(self.serial_socket)
        except OSError:
            return None
        return (info.st_dev, info.st_ino, info.st_ctime_ns)

    def _check_abort(self, payloads: dict, now_mono: float, cycle: dict) -> bool:
        while self.failures and now_mono - self.failures[0] > READ_FAILURE_WINDOW_SECONDS:
            self.failures.popleft()
        if len(self.failures) > READ_FAILURE_LIMIT:
            self._abort(f"mehr als {READ_FAILURE_LIMIT} fehlerhafte Reads in 10 min (§2.5/1)")
            return True
        frames = self.counters["frames"]
        link = self.counters["crc_errors"] + self.counters["protocol_errors"]
        if frames >= LINK_ERROR_MIN_FRAMES and link > LINK_ERROR_RATIO * frames:
            self._abort("CRC-/Protokollfehler über 1 % der Frames (§2.5/1)")
            return True
        mismatch = self._check_versions(payloads, now_mono)
        if mismatch:
            self._abort(f"{mismatch} (§2.5/2)")
            return True
        sc = _cell(payloads, 0x0E91, 1, 22)
        warning = _cell(payloads, 0x0E90, 1, 22)
        warnings = _cell(payloads, 0x0E92, 2, 22)
        if sc not in (None, 0):
            self._terminal("angehalten", f"Servicecode 0x0e91 = {sc} – Pause bis Klärung durch Bober (§2.5/3)")
            return True
        if warning is not None:
            if self.baseline_warning_code is None:
                self.baseline_warning_code = warning
            elif warning != 0 and warning != self.baseline_warning_code:
                self._terminal("angehalten", f"neue Warnung (Code {warning}) – "
                                             "Pause bis Klärung durch Bober (§2.5/3)")
                return True
        if warnings is not None:
            if self.baseline_warnings is None:
                self.baseline_warnings = warnings
            elif warnings > self.baseline_warnings:
                self._terminal("angehalten", f"neue Warnung (Zähler {warnings}, Code {warning}) – "
                                             "Pause bis Klärung durch Bober (§2.5/3)")
                return True
        identity = self._socket_identity()
        if identity is not None:
            if self.socket_identity is None:
                self.socket_identity = identity
            elif identity != self.socket_identity:
                self._abort("Serialworker neu gestartet (Socket ersetzt, §2.5/4)")
                return True
        if self._check_cycle_time(cycle, now_mono):
            return True
        if cycle.get("slow"):
            self.display_unavailable = 0 if cycle.get("display_available") else self.display_unavailable + 1
            ram_ok = cycle.get("ram_telemetry_available")
            self.ram_unavailable = 0 if ram_ok is not False else self.ram_unavailable + 1
            if self.display_unavailable >= UNAVAILABLE_SLOW_CYCLE_LIMIT:
                self._abort("display_status wiederholt nicht verfügbar (§2.5/5)")
                return True
            if self.ram_unavailable >= UNAVAILABLE_SLOW_CYCLE_LIMIT:
                self._abort("ram_telemetry wiederholt nicht verfügbar (§2.5/5)")
                return True
        return False

    def _check_cycle_time(self, cycle: dict, now_mono: float) -> bool:
        """§2.5/5 with pause mode; return True when the run was aborted.

        The monitor reports the previous cycle.  Slow-poll and hourly
        retention cycles are long by design and neither count nor reset.
        """
        if self.slow_pause_since is not None \
                and now_mono - self.slow_pause_since >= SLOW_PAUSE_ABORT_SECONDS:
            self._abort("Web-Fast-Zyklus > 1,5 s anhaltend: Pause länger als 10 min (§2.5/5)")
            return True
        elapsed = cycle.get("previous_elapsed")
        if elapsed is None or cycle.get("previous_slow") or cycle.get("previous_housekeeping"):
            return False
        elapsed = float(elapsed)
        serial = cycle.get("previous_serial_elapsed")
        sample = {"elapsed_s": round(elapsed, 3),
                  "serial_s": None if serial is None else round(float(serial), 3)}
        if self.slow_pause_since is not None:
            self.slow_pause_max = max(self.slow_pause_max, elapsed)
            if elapsed > SLOW_FAST_CYCLE_SECONDS:
                self.slow_pause_healthy = 0
                return False
            self.slow_pause_healthy += 1
            if self.slow_pause_healthy >= SLOW_PAUSE_RESUME_CYCLES:
                self._log({"type": "fortsetzung", "grund": "§2.5/5",
                           "pause_s": round(now_mono - self.slow_pause_since, 3),
                           "max_elapsed_s": round(self.slow_pause_max, 3),
                           "gesunde_zyklen": self.slow_pause_healthy})
                self.slow_pause_since = None
                self.slow_cycles = 0
                self.slow_recent.clear()
                self._set(self._slow_pause_state, None)
            return False
        if elapsed > SLOW_FAST_CYCLE_SECONDS:
            self.slow_cycles += 1
            self.slow_recent.append(sample)
        else:
            self.slow_cycles = 0
            self.slow_recent.clear()
        if self.slow_cycles < SLOW_FAST_CYCLE_LIMIT:
            return False
        while self.slow_pause_opens and now_mono - self.slow_pause_opens[0] >= 3600.0:
            self.slow_pause_opens.popleft()
        if len(self.slow_pause_opens) >= SLOW_PAUSES_PER_HOUR_LIMIT:
            self._abort(f"Web-Fast-Zyklus > 1,5 s wiederkehrend: {SLOW_PAUSES_PER_HOUR_LIMIT + 1}. "
                        "Pause innerhalb 1 h (§2.5/5)")
            return True
        self.slow_pause_opens.append(now_mono)
        self.counters["slow_pauses"] += 1
        self.slow_pause_since = now_mono
        self.slow_pause_healthy = 0
        self.slow_pause_max = max(item["elapsed_s"] for item in self.slow_recent)
        self._slow_pause_state = self.state if self.state in ("läuft", "basisprüfung") else "läuft"
        self._log({"type": "pause", "grund": "Web-Fast-Zyklus > 1,5 s in 3 Folgezyklen (§2.5/5)",
                   "zyklen": list(self.slow_recent),
                   "fortsetzung_nach": SLOW_PAUSE_RESUME_CYCLES,
                   "abbruch_nach_s": SLOW_PAUSE_ABORT_SECONDS})
        self.slow_cycles = 0
        return False

    # -------------------------------------------------------------- cycle
    def _check_hand(self, session, payloads: dict, now_mono: float, force: bool = False) -> None:
        if not force and now_mono - self.hand_checked < TAKT_SECONDS["Z"]:
            return
        field = self._p0_field("P1.6")
        value = self._read(session, field, payloads, now_mono)
        self.hand_checked = now_mono
        self.next_due[(field.cpu, field.addr)] = now_mono + TAKT_SECONDS["Z"]
        previous = self.hand_mode
        self.hand_mode = None if value is None else bool(value[0])
        if previous is not self.hand_mode:
            self._log({"type": "hand", "hand_mode": self.hand_mode,
                       "wirkung": "P2/P3 pausiert (§2.5/7)" if self.hand_mode is not False else "P2/P3 aktiv"})

    def _preconditions(self, session, payloads: dict, now_mono: float) -> bool:
        mismatch = self._check_versions(payloads, now_mono)
        if self.versions is None or now_mono - self.versions_mono > PHASE6_VERSION_MAX_AGE_SECONDS:
            self._set("vorbedingungen", "warte auf CPU-0-Block 20 (Firmwarebindung)")
            return False
        if mismatch:
            self._set("blockiert", mismatch)
            return False
        sc = _cell(payloads, 0x0E91, 1, 22)
        if sc is None:
            self._set("vorbedingungen", "warte auf Block 22 (Servicecode)")
            return False
        if sc != 0:
            self._set("blockiert", f"Servicecode 0x0e91 = {sc}")
            return False
        self._check_hand(session, payloads, now_mono, force=self.hand_mode is not False
                         and now_mono - self.hand_checked >= TAKT_SECONDS["M"])
        if self.hand_mode is not False:
            self._set("blockiert", "Handbetrieb 0x1b5a ≠ 0 oder nicht lesbar")
            return False
        group = state_group(_cell(payloads, 0x0ED4, 1, 24))
        if group not in ("S0", "S2"):
            self._set("vorbedingungen", f"P0 wartet auf Stillstand oder stationären Lauf (aktuell {group})")
            return False
        self.p0_pending = ["P0.1", "P0.2", "P0.3", "P0.5/1", "P0.5/2"]
        self.p0_done_groups = set()
        self._p0_group = group
        self._set("basisprüfung", None)
        self._log({"type": "vorbedingungen", "ok": True, "versions": self.versions, "sc": sc,
                   "hand": self.hand_mode, "gruppe": group,
                   "extern_zu_prüfen": ["systemctl is-active", "NRestarts", "fuser /dev/ttyUSB0",
                                        "healthz", "write_audit"]})
        return False

    def cycle(self, session, service, payloads: dict, now_mono: float, cycle: dict | None = None) -> None:
        """Run the Phase-6 part of one monitor cycle inside the existing lease."""
        cycle = dict(cycle or {})
        if not self._update_lifecycle(now_mono):
            return
        self.cycle_no += 1
        self.cycle_frames = 0
        self._refill(now_mono)
        budget = self._log_budget()
        if budget == "total":
            self._abort("Log-Gesamtobergrenze erreicht")
            return
        if budget == "day":
            if self.state in ("läuft", "basisprüfung"):
                self._set("pausiert", "Tageslog hat 50 MB erreicht – Reads ruhen bis zum nächsten UTC-Tag")
            return
        try:
            if self.state in ("vorbedingungen", "blockiert"):
                self._preconditions(session, payloads, now_mono)
                return
            if self._check_abort(payloads, now_mono, cycle):
                return
            if self.slow_pause_since is not None:
                # No Phase-6 frame while the web monitor is slow (§2.5/5).
                self._set("pausiert", "Web-Fast-Zyklus > 1,5 s – Reads ruhen bis "
                          f"{SLOW_PAUSE_RESUME_CYCLES} gesunde Zyklen (§2.5/5)")
                return
            if self.versions is None or now_mono - self.versions_mono > PHASE6_VERSION_MAX_AGE_SECONDS:
                self._set("pausiert", "Firmwarebindung nicht aktuell geprüft (B20 fehlt)")
                return
            group = state_group(_cell(payloads, 0x0ED4, 1, 24))
            opened = self._update_windows(payloads, now_mono)
            if self.state == "basisprüfung":
                if group != self._p0_group:
                    if group in ("S0", "S2"):
                        self._p0_group = group
                    else:
                        return
                self._log_context(payloads, now_mono)
                self._run_p0_step(session, service, payloads, now_mono)
                if self.state == "basisprüfung" and not self.p0_pending:
                    self.p0_done_groups.add(self._p0_group)
                    self._set("läuft", None)
                    self.persisted["state"] = "läuft"
                    self._save_state()
                return
            if self.state == "pausiert":
                self._set("läuft", None)
            # P0 once more in the other base state (S0 and S2, plan §1 P0).
            if group in ("S0", "S2") and group not in self.p0_done_groups and not self.p0_pending:
                self.p0_pending = ["P0.1", "P0.2", "P0.3"]
                self._p0_group = group
            if self.p0_pending and group == self._p0_group:
                self._log_context(payloads, now_mono)
                self._run_p0_step(session, service, payloads, now_mono)
                if not self.p0_pending and self.state == "läuft":
                    self.p0_done_groups.add(group)
                return
            if opened:
                self._check_hand(session, payloads, now_mono, force=True)
            if group == "S0" and not self.p0_4_done and 20 in payloads:
                self._log_context(payloads, now_mono)
                self._run_p0_4(session, payloads, now_mono, payloads.get(20))
                return
            units = self._plan_cycle(payloads, now_mono)
            # Full context every cycle inside F windows, otherwise every 10 s;
            # each read line carries the compact ctx of its own cycle.
            if self.in_f_window(now_mono) or cycle.get("slow"):
                self._log_context(payloads, now_mono)
            for unit in units:
                for field in unit:
                    if field.id == "P1.6":
                        self._check_hand(session, payloads, now_mono, force=True)
                        continue
                    first = self._read(session, field, payloads, now_mono)
                    if field.double and first is not None:
                        self._read(session, field, payloads, now_mono)
        except Phase6GuardError as exc:
            self._abort(f"Guard hat Anfrage verweigert: {exc} (§2.5/6)")
        except _LogFull:
            self._abort("Log-Obergrenze erreicht, Read konnte nicht protokolliert werden")


class _LogFull(Exception):
    pass
