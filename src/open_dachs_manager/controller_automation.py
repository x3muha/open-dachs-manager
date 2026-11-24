"""Pure rules for the controller-clock sync, the 0x0f0e auto-reset and the
real runtime since start.

Nothing in this module touches the serial line. The web application owns the
serial session, authentication, write, readback, deauthentication and audit;
this module only validates settings, decides eligibility and computes
time windows so that every decision is unit-testable without hardware.

Evidence:
- Clock path: reports/uhr-sprung-nachtabschaltung-firmwareanalyse-2026-10-26.md
  §1.2 (block-50 write handler f47213: 70 bytes to RAM 0x1acc, then
  ``f47293 BTST 5,0x1acc`` -> ``MOV.L 0x1af0 -> 0x163e`` -> ``JSR f464e1``
  (RAM + RTC) -> ``BCLR 5,0x1acc``; refused when ``0x0d4c != 0`` and
  ``0x202f == 4``; 0x202f is mirrored into block-50 offset 54 on every read).
- Night shutdown: exact second ``tod == 10800 + 60*(starts mod 100)``, only
  while 0x0f0e >= 21600; 24-h threshold 86370 at 0x0f0e
  (reports/nachtabschaltung-steuerung-firmwareanalyse-2026-10-24.md).
- Safe 0x0f0e target ``>= max(1800, 60*bMindestlaufzeit)``; 360-s statistic
  period (same report).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ODM_TIMEZONE = ZoneInfo("Europe/Berlin")
CONTROLLER_EPOCH = datetime(2000, 1, 1)

# ---------------------------------------------------------------------------
# A) Controller clock sync over block 50
# ---------------------------------------------------------------------------
CLOCK_SYNC_BLOCK = 50
CLOCK_SYNC_PAYLOAD_LENGTH = 70
CLOCK_SYNC_TIME_OFFSET = 36          # Hka_Ew.ulSystemTime = RAM 0x1af0
CLOCK_SYNC_COMMAND_OFFSET = 0        # Hka_Ew.UFlag_Ew1.bGrundeinstellung = RAM 0x1acc
CLOCK_SYNC_COMMAND_BIT = 0x20        # bit 5: "take over ulSystemTime" (f47293)
CLOCK_SYNC_GUARD_OFFSET = 54         # Hka_Ew.Res[0] mirrors RAM 0x202f (f4721b)
CLOCK_SYNC_GUARD_REFUSED_VALUE = 4   # f472a1: 0x202f == 4 (with 0x0d4c != 0) refuses
# Offsets whose value legitimately changes between two reads of block 50.
# Offset 31 is Reserve_2: stock firmware leaves it static, while the optional
# adjustable-SC123 firmware increments it as a dynamic capability heartbeat.
CLOCK_SYNC_SC123_HEARTBEAT_OFFSET = 31
CLOCK_SYNC_VOLATILE_OFFSETS = (
    frozenset(range(36, 40))
    | {CLOCK_SYNC_GUARD_OFFSET, CLOCK_SYNC_SC123_HEARTBEAT_OFFSET}
)
CLOCK_SYNC_AUTH_LEVEL = 3
CLOCK_SYNC_READBACK_TOLERANCE_SECONDS = 3
CLOCK_SYNC_EXCLUSION_MARGIN_SECONDS = 15 * 60
CLOCK_SYNC_RUN_WINDOW_MINUTES = 60
CLOCK_SYNC_EARLIEST_MINUTE = 6 * 60      # 06:00 ODM time
CLOCK_SYNC_LATEST_MINUTE = 22 * 60       # 22:00 ODM time
CLOCK_SYNC_DEFAULTS = {
    "enabled": False,
    "time": "12:00",
    "threshold_seconds": 30,
    "max_offset_seconds": 3600,
}
CLOCK_SYNC_THRESHOLD_RANGE = (10, 3600)
CLOCK_SYNC_MAX_OFFSET_RANGE = (60, 43200)
NIGHT_SHUTDOWN_BASE_SECONDS = 3 * 3600
SECONDS_PER_DAY = 86400


def _strict_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} muss eine ganze Zahl sein")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*[0-9]+\s*", value):
        return int(value)
    raise ValueError(f"{label} muss eine ganze Zahl sein")


def parse_clock_time(value: object) -> int:
    """Return minutes after midnight for HH:MM inside the allowed day window."""
    if not isinstance(value, str):
        raise ValueError("Uhrzeit für den Uhrabgleich muss HH:MM sein")
    match = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})", value.strip())
    if match is None:
        raise ValueError("Uhrzeit für den Uhrabgleich muss HH:MM sein")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        raise ValueError("Uhrzeit für den Uhrabgleich ist ungültig")
    total = hours * 60 + minutes
    if not CLOCK_SYNC_EARLIEST_MINUTE <= total <= CLOCK_SYNC_LATEST_MINUTE:
        raise ValueError(
            "Uhrabgleich nur zwischen 06:00 und 22:00 ODM-Zeit "
            "(fern von Mitternacht, Nachtabschaltung und Sommerzeitumstellung)"
        )
    return total


def normalize_clock_sync_settings(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Einstellungen für den Uhrabgleich fehlen")
    enabled = payload.get("enabled", CLOCK_SYNC_DEFAULTS["enabled"])
    if not isinstance(enabled, bool):
        raise ValueError("enabled muss true oder false sein")
    time_text = payload.get("time", CLOCK_SYNC_DEFAULTS["time"])
    minutes = parse_clock_time(time_text)
    threshold = _strict_int(
        payload.get("threshold_seconds", CLOCK_SYNC_DEFAULTS["threshold_seconds"]),
        "Schwelle",
    )
    low, high = CLOCK_SYNC_THRESHOLD_RANGE
    if not low <= threshold <= high:
        raise ValueError(f"Schwelle muss zwischen {low} und {high} s liegen")
    max_offset = _strict_int(
        payload.get("max_offset_seconds", CLOCK_SYNC_DEFAULTS["max_offset_seconds"]),
        "Maximaler Versatz",
    )
    low, high = CLOCK_SYNC_MAX_OFFSET_RANGE
    if not low <= max_offset <= high:
        raise ValueError(f"Maximaler Versatz muss zwischen {low} und {high} s liegen")
    if max_offset <= threshold:
        raise ValueError("Maximaler Versatz muss größer als die Schwelle sein")
    return {
        "enabled": enabled,
        "time": f"{minutes // 60:02d}:{minutes % 60:02d}",
        "threshold_seconds": threshold,
        "max_offset_seconds": max_offset,
    }


def controller_naive(raw: int) -> datetime:
    return CONTROLLER_EPOCH + timedelta(seconds=int(raw))


def controller_raw(local_naive: datetime) -> int:
    return int(round((local_naive - CONTROLLER_EPOCH).total_seconds()))


def odm_local_naive(now_utc: datetime) -> datetime:
    return now_utc.astimezone(ODM_TIMEZONE).replace(tzinfo=None)


def decode_controller_time(payload: bytes) -> int:
    if len(payload) != CLOCK_SYNC_PAYLOAD_LENGTH:
        raise ValueError(f"Block 50 hat {len(payload)} statt {CLOCK_SYNC_PAYLOAD_LENGTH} Byte")
    raw = int.from_bytes(
        payload[CLOCK_SYNC_TIME_OFFSET:CLOCK_SYNC_TIME_OFFSET + 4], "little", signed=False
    )
    if raw in (0, 0xFFFFFFFF):
        raise ValueError(f"ungültige Reglerzeit (Rohwert {raw})")
    return raw


def clock_sync_payload(before: bytes, target_raw: int) -> bytes:
    """Read-modify-write: only offset 36..39 and command bit 5 of byte 0 change."""
    if len(before) != CLOCK_SYNC_PAYLOAD_LENGTH:
        raise ValueError("Block 50 hat eine unerwartete Länge")
    if not 0 < int(target_raw) < 0xFFFFFFFF:
        raise ValueError("Zielzeit außerhalb des 4-Byte-Bereichs")
    after = bytearray(before)
    after[CLOCK_SYNC_COMMAND_OFFSET] |= CLOCK_SYNC_COMMAND_BIT
    after[CLOCK_SYNC_TIME_OFFSET:CLOCK_SYNC_TIME_OFFSET + 4] = int(target_raw).to_bytes(4, "little")
    return bytes(after)


def stable_block50_equal(left: bytes, right: bytes) -> bool:
    """Compare two block-50 images except the known volatile offsets."""
    if len(left) != len(right):
        return False
    return all(
        left[index] == right[index]
        for index in range(len(left))
        if index not in CLOCK_SYNC_VOLATILE_OFFSETS
    )


def night_shutdown_second(starts: int) -> int:
    return NIGHT_SHUTDOWN_BASE_SECONDS + 60 * (int(starts) % 100)


def _crosses_time_of_day(start: datetime, end: datetime, tod_seconds: int) -> bool:
    """True when a local wall time with that time-of-day lies in [start, end]."""
    day = start.date() - timedelta(days=1)
    while day <= end.date() + timedelta(days=1):
        moment = datetime(day.year, day.month, day.day) + timedelta(seconds=tod_seconds)
        if start <= moment <= end:
            return True
        day += timedelta(days=1)
    return False


def _dst_change_between(start_utc: datetime, end_utc: datetime) -> bool:
    step = timedelta(minutes=5)
    reference = start_utc.astimezone(ODM_TIMEZONE).utcoffset()
    moment = start_utc
    while moment <= end_utc:
        if moment.astimezone(ODM_TIMEZONE).utcoffset() != reference:
            return True
        moment += step
    return end_utc.astimezone(ODM_TIMEZONE).utcoffset() != reference


def clock_sync_window_reason(
    controller_before: datetime,
    controller_after: datetime,
    now_utc: datetime,
    starts: int | None,
    margin_seconds: int = CLOCK_SYNC_EXCLUSION_MARGIN_SECONDS,
) -> str | None:
    """Return why the clock must not be set now, or None when it is safe.

    The controller timeline is examined from the earlier to the later of the
    old and the new controller time, widened by the margin on both sides.
    It must not contain midnight, the night-shutdown second for the current
    or the next start count, or (in ODM time) a DST transition.
    """
    if starts is None:
        return "Anzahl Starts unbekannt; Nachtabschalt-Sekunde nicht bestimmbar"
    margin = timedelta(seconds=int(margin_seconds))
    low = min(controller_before, controller_after) - margin
    high = max(controller_before, controller_after) + margin
    if _crosses_time_of_day(low, high, 0):
        return "Zielintervall liegt an Mitternacht (±15 min)"
    for count in (int(starts), int(starts) + 1):
        second = night_shutdown_second(count)
        if _crosses_time_of_day(low, high, second):
            minutes = second // 60
            return (
                "Zielintervall berührt die Nachtabschalt-Sekunde "
                f"{minutes // 60:02d}:{minutes % 60:02d}:00 (±15 min)"
            )
    shift = abs((controller_after - controller_before).total_seconds())
    if _dst_change_between(
        now_utc - timedelta(seconds=shift) - margin,
        now_utc + timedelta(seconds=shift) + margin,
    ):
        return "Sommer-/Winterzeitumstellung im Zielintervall (±15 min)"
    return None


def clock_sync_due(
    settings: dict, now_utc: datetime, last_auto_date: str | None
) -> tuple[bool, str | None]:
    """Decide whether the daily automatic run is due; returns (due, today)."""
    if not settings.get("enabled"):
        return False, None
    local = now_utc.astimezone(ODM_TIMEZONE)
    today = local.date().isoformat()
    if last_auto_date == today:
        return False, today
    minutes = parse_clock_time(settings["time"])
    scheduled = local.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
    if scheduled <= local < scheduled + timedelta(minutes=CLOCK_SYNC_RUN_WINDOW_MINUTES):
        return True, today
    return False, today


def next_clock_sync(settings: dict, now_utc: datetime, last_auto_date: str | None) -> str | None:
    if not settings.get("enabled"):
        return None
    local = now_utc.astimezone(ODM_TIMEZONE)
    minutes = parse_clock_time(settings["time"])
    day: date = local.date()
    scheduled = datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=ODM_TIMEZONE)
    window_end = scheduled + timedelta(minutes=CLOCK_SYNC_RUN_WINDOW_MINUTES)
    if last_auto_date == day.isoformat() or local >= window_end:
        day = day + timedelta(days=1)
        scheduled = datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=ODM_TIMEZONE)
    return scheduled.isoformat(timespec="minutes")


# ---------------------------------------------------------------------------
# B) Automatic reset of 0x0f0e ("Laufzeit seit Start")
# ---------------------------------------------------------------------------
RUNTIME_SHUTDOWN_SECONDS = 86370
RUNTIME_AUTO_MIN_TARGET_SECONDS = 1800
RUNTIME_AUTO_STATISTIC_PERIOD_SECONDS = 360
RUNTIME_AUTO_MIN_GAP_SECONDS = 600
RUNTIME_AUTO_THRESHOLD_MARGIN_SECONDS = 900
RUNTIME_AUTO_MAX_THRESHOLD_SECONDS = RUNTIME_SHUTDOWN_SECONDS - RUNTIME_AUTO_THRESHOLD_MARGIN_SECONDS
RUNTIME_AUTO_UNIVERSAL_TARGET_SECONDS = 10800
RUNTIME_AUTO_MAX_RESETS = 999
RUNTIME_AUTO_RATE_LIMIT_SECONDS = 600
RUNTIME_AUTO_RUNNING_STATUS = 30
RUNTIME_AUTO_DEFAULTS = {
    "enabled": False,
    "threshold_seconds": 20000,
    "target_seconds": 10800,
    "max_resets_per_run": 0,
}


def runtime_auto_min_target(min_runtime_minutes: int | None) -> int:
    """Lowest permitted target; unknown bMindestlaufzeit means the universal 3 h."""
    if min_runtime_minutes is None:
        return RUNTIME_AUTO_UNIVERSAL_TARGET_SECONDS
    return max(
        RUNTIME_AUTO_MIN_TARGET_SECONDS,
        RUNTIME_AUTO_STATISTIC_PERIOD_SECONDS,
        60 * int(min_runtime_minutes),
    )


def normalize_runtime_auto_rule(payload: object, min_runtime_minutes: int | None) -> dict:
    """Validate X/Y/Z against the hard limits; raises ValueError."""
    if not isinstance(payload, dict):
        raise ValueError("Regel für die automatische Rücksetzung fehlt")
    enabled = payload.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled muss true oder false sein")
    threshold = _strict_int(payload.get("threshold_seconds"), "Schwelle X")
    target = _strict_int(payload.get("target_seconds"), "Zielwert Y")
    resets = _strict_int(payload.get("max_resets_per_run", 0), "Anzahl Z")
    minimum = runtime_auto_min_target(min_runtime_minutes)
    if target < minimum:
        source = (
            "Mindestlaufzeit unbekannt → universell 10800 s"
            if min_runtime_minutes is None
            else f"max(1800, 60·{int(min_runtime_minutes)} min Mindestlaufzeit)"
        )
        raise ValueError(f"Zielwert Y muss mindestens {minimum} s betragen ({source})")
    if threshold <= target + RUNTIME_AUTO_MIN_GAP_SECONDS:
        raise ValueError(
            f"Schwelle X muss größer als Y + {RUNTIME_AUTO_MIN_GAP_SECONDS} s "
            f"= {target + RUNTIME_AUTO_MIN_GAP_SECONDS} s sein"
        )
    if threshold > RUNTIME_AUTO_MAX_THRESHOLD_SECONDS:
        raise ValueError(
            f"Schwelle X darf höchstens {RUNTIME_AUTO_MAX_THRESHOLD_SECONDS} s "
            "(86370 − 900) betragen"
        )
    if not 0 <= resets <= RUNTIME_AUTO_MAX_RESETS:
        raise ValueError("Anzahl Z muss zwischen 0 (unbegrenzt) und 999 liegen")
    return {
        "enabled": enabled,
        "threshold_seconds": threshold,
        "target_seconds": target,
        "max_resets_per_run": resets,
    }


def runtime_auto_precheck(
    settings: dict,
    state: dict,
    counter_seconds: int | None,
    run_key: str | None,
    now_utc: datetime,
) -> tuple[str, str | None]:
    """Cheap cached-value decision before any serial access.

    Returns (phase, reason). Only phase ``due`` may lead to the fresh
    same-lock check and a write.
    """
    if not settings.get("enabled"):
        return "disabled", None
    if counter_seconds is None:
        return "blocked", "Zähler 0x0f0e nicht verfügbar"
    if counter_seconds < int(settings["threshold_seconds"]):
        return "watching", None
    last = state.get("last_write_at")
    if last:
        try:
            last_at = datetime.fromisoformat(str(last))
        except ValueError:
            return "blocked", "Zeitpunkt des letzten Writes unlesbar"
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        elapsed = (now_utc - last_at).total_seconds()
        if elapsed < 0:
            return "blocked", "ODM-Uhr liegt vor dem letzten Write (Uhr-Rücksprung)"
        if elapsed < RUNTIME_AUTO_RATE_LIMIT_SECONDS:
            return "blocked", (
                f"Rate-Limit: nächster Write frühestens in "
                f"{int(RUNTIME_AUTO_RATE_LIMIT_SECONDS - elapsed)} s"
            )
    limit = int(settings.get("max_resets_per_run") or 0)
    if limit:
        if not run_key:
            return "blocked", "aktueller Motorlauf nicht eindeutig bestimmbar (Z-Zählung)"
        used = int(state.get("resets_in_run") or 0) if state.get("run_key") == run_key else 0
        if used >= limit:
            return "limit", f"{used} von {limit} Rücksetzungen in diesem Lauf verbraucht; läuft weiter"
    return "due", None


def runtime_auto_eligibility(
    settings: dict,
    *,
    motor_status: int | None,
    service_code: int | None,
    manual_mode: int | None,
    min_runtime_minutes: int | None,
    counter_seconds: int | None,
) -> str | None:
    """Fresh same-session eligibility; returns the block reason or None."""
    if motor_status is None:
        return "Motorstatus nicht frisch gelesen"
    if motor_status < RUNTIME_AUTO_RUNNING_STATUS:
        return f"Motor läuft nicht (Status {motor_status})"
    if service_code is None:
        return "Servicecode nicht frisch gelesen"
    if service_code != 0:
        return f"Servicecode {service_code} aktiv"
    if manual_mode is None:
        return "Handbetrieb-Flag 0x1b5a nicht lesbar"
    if manual_mode != 0:
        return "Handbetrieb aktiv (0x1b5a)"
    if min_runtime_minutes is None:
        return "Mindestlaufzeit (Block 50) nicht lesbar"
    minimum = runtime_auto_min_target(min_runtime_minutes)
    if int(settings["target_seconds"]) < minimum:
        return f"Zielwert {settings['target_seconds']} s < Mindestwert {minimum} s (Mindestlaufzeit)"
    if counter_seconds is None:
        return "Zähler 0x0f0e nicht kohärent gelesen"
    if counter_seconds < int(settings["threshold_seconds"]):
        return f"Zähler {counter_seconds} s unter Schwelle"
    if counter_seconds >= RUNTIME_SHUTDOWN_SECONDS:
        return "24-h-Schwelle bereits erreicht"
    return None


# ---------------------------------------------------------------------------
# C) Real runtime since start
# ---------------------------------------------------------------------------
REAL_RUNTIME_RUNNING_STATUS = 30
REAL_RUNTIME_RESET_TOLERANCE_SECONDS = 120
REAL_RUNTIME_SAMPLE_WINDOW_SECONDS = 300


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def real_runtime_status(
    *,
    now_utc: datetime,
    motor_status: int | None,
    history: dict,
    operating_seconds_now: int | None,
    counter_0f0e: int | None,
) -> dict:
    """Combine the live state with a store-side reconstruction.

    ``history`` comes from ``DachsStore.real_runtime_history``: last
    standstill point before the current run, first running point after it,
    whether a recording gap lies after the standstill, operating seconds at
    the standstill/start samples and, for a stopped motor, the stop time.
    """
    result = {
        "schema": "open-dachs-real-runtime/v1",
        "available": False,
        "running": False,
        "state": "unbekannt",
        "since": None,
        "since_exact": False,
        "seconds": None,
        "source": None,
        "run_key": None,
        "counter_0f0e": counter_0f0e,
        "counter_deviation_seconds": None,
        "counter_reset_detected": False,
        "error": None,
    }
    if motor_status is None:
        return {**result, "error": "Motorstatus nicht aktuell"}
    if motor_status < REAL_RUNTIME_RUNNING_STATUS:
        stopped = history.get("stopped_since")
        state = "startet" if 20 <= motor_status < REAL_RUNTIME_RUNNING_STATUS else "steht"
        return {
            **result,
            "available": True,
            "state": state,
            "since": _iso(stopped),
            "since_exact": stopped is not None and not history.get("stop_gap"),
            "source": "historie" if stopped is not None else None,
        }
    standstill = history.get("standstill_at")
    start = history.get("start_at")
    run_key = "after:" + _iso(standstill) if standstill is not None else (
        "first:" + _iso(start) if start is not None else None
    )
    result.update({"running": True, "state": "läuft", "run_key": run_key})
    if start is None:
        return {**result, "error": "kein Laufbeginn in der Historie"}
    if not history.get("gap"):
        seconds = max(0, int((now_utc - start).total_seconds()))
        result.update({
            "available": True,
            "since": _iso(start),
            "since_exact": standstill is not None,
            "seconds": seconds,
            "source": "historie" if standstill is not None else "historie (Beginn abgeschnitten)",
        })
    else:
        base = history.get("operating_seconds_at_standstill")
        if base is None or operating_seconds_now is None or operating_seconds_now < base:
            return {**result, "error": "Laufbeginn in Aufzeichnungslücke, Betriebssekunden fehlen"}
        seconds = int(operating_seconds_now - base)
        result.update({
            "available": True,
            "since": _iso(now_utc - timedelta(seconds=seconds)),
            "since_exact": False,
            "seconds": seconds,
            "source": "betriebssekunden (Aufzeichnungslücke)",
        })
    if counter_0f0e is not None and result["seconds"] is not None:
        deviation = int(result["seconds"]) - int(counter_0f0e)
        result["counter_deviation_seconds"] = deviation
        result["counter_reset_detected"] = deviation > REAL_RUNTIME_RESET_TOLERANCE_SECONDS
    return result
