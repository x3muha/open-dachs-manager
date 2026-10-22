"""Offline 250-operating-hour diagnosis derived from ODM history snapshots."""

from __future__ import annotations

import copy
import json
import math
import os
import stat
import tempfile
import threading
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from datetime import time as datetime_time
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

INTERVAL_SECONDS = 250 * 3600
MAX_TRUSTED_GAP_SECONDS = 30 * 60
MIN_CURRENT_EVALUATION_HOURS = 10.0
SUFFICIENT_COVERAGE = 0.90
DIAGNOSIS_CACHE_SCHEMA = "open-dachs-manager-250h-cache/v1"
DIAGNOSIS_RESULT_SCHEMA = "open-dachs-manager-250h-diagnosis/v1"
DIAGNOSIS_CACHE_MAX_BYTES = 4 * 1024 * 1024
DIAGNOSIS_STALE_AFTER = timedelta(hours=26)
DIAGNOSIS_TIMEZONE = ZoneInfo("Europe/Berlin")
MOTOR_RUNNING_STATUSES = frozenset(range(30, 36))
MOTOR_STOPPED_STATUSES = frozenset({15, 16})
MOTOR_STOP_CONFIRM_SECONDS = 5.0

METRICS = {
    "motor_exhaust": {
        "title": "Motorabgas vor Rußfilter",
        "unit": "°C",
        "normal": "HR-Beispiel: 420–490 °C",
        "trend_limit": 20.0,
    },
    "dachs_exhaust": {
        "title": "Dachs-Abgas nach Rußfilter",
        "unit": "°C",
        "normal": "Wartungshinweis: <170 °C",
        "maximum": 170.0,
        "trend_limit": 15.0,
    },
    "coolant": {
        "title": "Kühlwasser Motor",
        "unit": "°C",
        "normal": "Herstellerbereich: 72–85 °C",
        "minimum": 72.0,
        "maximum": 85.0,
        "trend_limit": 10.0,
    },
    "inlet": {
        "title": "Dachs-Eintritt",
        "unit": "°C",
        "normal": "Hinweiszone 60–65 °C (Dokumentwiderspruch)",
        "hint_minimum": 60.0,
    },
    "controller": {
        "title": "Reglertemperatur MSR2",
        "unit": "°C",
        "normal": "Herstellerhinweis: <40 °C",
        "maximum": 40.0,
    },
    "power": {
        "title": "Generatorleistung",
        "unit": "kW",
        "normal": "Dynamischer Sollwert ±0,15 kW",
    },
}


def next_local_midnight(now: datetime) -> datetime:
    """Return the next Europe/Berlin midnight, including DST transitions."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local = now.astimezone(DIAGNOSIS_TIMEZONE)
    tomorrow = local.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime_time.min, DIAGNOSIS_TIMEZONE)


class MotorStopDetector:
    """Confirm one full stop after a real run without retriggering on chatter."""

    def __init__(self, confirm_seconds: float = MOTOR_STOP_CONFIRM_SECONDS):
        self.confirm_seconds = max(0.0, float(confirm_seconds))
        self.armed = False
        self.candidate_since: datetime | None = None

    @staticmethod
    def _observed_time(value: object) -> datetime | None:
        parsed = _time(value)
        return parsed

    def observe(
        self,
        recorded_at: object,
        *,
        status: object,
        rpm: object,
    ) -> bool:
        """Return true once status 15/16 and zero rpm remain confirmed.

        A prior positive rpm or running status 30–35 arms the detector. Status
        14 is neither a completed stop nor a failed-start inference; it merely
        cancels an in-progress stop confirmation until 15/16 is observed.
        """
        at = self._observed_time(recorded_at)
        status_number = _number(status)
        rpm_number = _number(rpm)
        if at is None:
            return False
        status_code = int(status_number) if status_number is not None else None
        running = (
            (rpm_number is not None and rpm_number > 0)
            or status_code in MOTOR_RUNNING_STATUSES
        )
        if running:
            self.armed = True
            self.candidate_since = None
            return False
        stopped = (
            status_code in MOTOR_STOPPED_STATUSES
            and rpm_number is not None
            and rpm_number == 0
        )
        if not self.armed or not stopped:
            self.candidate_since = None
            return False
        if self.candidate_since is None or at < self.candidate_since:
            self.candidate_since = at
            return self.confirm_seconds == 0
        if (at - self.candidate_since).total_seconds() < self.confirm_seconds:
            return False
        self.armed = False
        self.candidate_since = None
        return True


class Diagnosis250hCache:
    """Run the expensive SQLite diagnosis in one coalescing background thread."""

    def __init__(
        self,
        path: str | Path,
        calculate: Callable[[], dict],
        *,
        clock: Callable[[], datetime] | None = None,
        stop_confirm_seconds: float = MOTOR_STOP_CONFIRM_SECONDS,
    ):
        self.path = Path(path)
        self.calculate = calculate
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.running = False
        self.pending_reasons: set[str] = set()
        self.last_error: str | None = None
        self.result: dict | None = None
        self.trigger_reason: str | None = None
        self.generated_at: datetime | None = None
        self.next_midnight = next_local_midnight(self.clock())
        self.stop_detector = MotorStopDetector(stop_confirm_seconds)
        self._load()

    @staticmethod
    def _aware_utc(value: object) -> datetime | None:
        return _time(value)

    def _load(self) -> None:
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > DIAGNOSIS_CACHE_MAX_BYTES:
                return
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            result = loaded["result"]
            generated_at = self._aware_utc(loaded["generated_at"])
            if (
                loaded.get("schema") != DIAGNOSIS_CACHE_SCHEMA
                or not isinstance(result, dict)
                or result.get("schema") != DIAGNOSIS_RESULT_SCHEMA
                or generated_at is None
                or not isinstance(loaded.get("trigger_reason"), str)
            ):
                return
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        self.result = result
        self.generated_at = generated_at
        self.trigger_reason = loaded["trigger_reason"]

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > DIAGNOSIS_CACHE_MAX_BYTES:
            raise ValueError("250-h-Diagnosecache überschreitet die zulässige Größe")
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            with suppress(OSError):
                directory_fd = os.open(
                    self.path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def start(self) -> None:
        with self.lock:
            if self.thread is not None:
                return
            self.thread = threading.Thread(
                target=self._worker,
                name="dachs-250h-diagnosis",
                daemon=True,
            )
            self.thread.start()
            self.pending_reasons.add("service_start")
            self.wake.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def request_refresh(self, reason: str) -> bool:
        clean_reason = str(reason).strip() or "unspecified"
        self.start()
        with self.lock:
            already_pending = clean_reason in self.pending_reasons
            self.pending_reasons.add(clean_reason)
            self.wake.set()
            return not already_pending

    def observe_motor(self, recorded_at: object, *, status: object, rpm: object) -> bool:
        if not self.stop_detector.observe(recorded_at, status=status, rpm=rpm):
            return False
        self.request_refresh("motor_stop")
        return True

    def _schedule_midnight_if_due(self, now: datetime) -> bool:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        with self.lock:
            if now < self.next_midnight:
                return False
            self.pending_reasons.add("midnight")
            self.next_midnight = next_local_midnight(now)
            self.wake.set()
            return True

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            now = self.clock()
            self._schedule_midnight_if_due(now)
            with self.lock:
                reasons = sorted(self.pending_reasons)
                self.pending_reasons.clear()
                if reasons:
                    self.running = True
                timeout = max(0.1, min(60.0, (self.next_midnight - now).total_seconds()))
            if not reasons:
                self.wake.wait(timeout)
                self.wake.clear()
                continue
            reason = "+".join(reasons)
            try:
                result = self.calculate()
                if not isinstance(result, dict) or result.get("schema") != DIAGNOSIS_RESULT_SCHEMA:
                    raise ValueError("250-h-Diagnose lieferte ein ungültiges Ergebnis")
                generated_at = self.clock()
                if generated_at.tzinfo is None:
                    generated_at = generated_at.replace(tzinfo=UTC)
                generated_at = generated_at.astimezone(UTC)
                finished = copy.deepcopy(result)
                finished["generated_at"] = generated_at.isoformat()
                cache_payload = {
                    "schema": DIAGNOSIS_CACHE_SCHEMA,
                    "generated_at": generated_at.isoformat(),
                    "trigger_reason": reason,
                    "result": finished,
                }
                self._write(cache_payload)
                with self.lock:
                    self.result = finished
                    self.generated_at = generated_at
                    self.trigger_reason = reason
                    self.last_error = None
            except Exception as exc:  # noqa: BLE001 - keep the daemon alive on any calculation/persistence fault
                with self.lock:
                    self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                with self.lock:
                    self.running = False

    def get(self, *, refresh: bool = False) -> dict:
        self.start()
        if refresh:
            self.request_refresh("manual")
        with self.lock:
            result = copy.deepcopy(self.result)
            generated_at = self.generated_at
            running = self.running or bool(self.pending_reasons)
            last_error = self.last_error
            trigger_reason = self.trigger_reason
            next_midnight = self.next_midnight
        if result is None:
            result = {
                "schema": DIAGNOSIS_RESULT_SCHEMA,
                "available": False,
                "reason": "Die erste Hintergrundberechnung läuft.",
                "source": "Lokale ODM-SQLite-Historie; keine MSR2-Diagnoseblöcke",
                "intervals": [],
                "metrics": METRICS,
                "notices": [],
                "hint_count": 0,
                "action_count": 0,
                "condenser_available": False,
                "failed_start_available": False,
            }
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        stale = bool(
            last_error
            or generated_at is None
            or now.astimezone(UTC) - generated_at > DIAGNOSIS_STALE_AFTER
        )
        result.update({
            "cache_available": generated_at is not None,
            "pending": generated_at is None and running,
            "running": running,
            "stale": stale,
            "last_error": last_error,
            "trigger_reason": trigger_reason,
            "generated_at": generated_at.isoformat() if generated_at else None,
            "next_midnight": next_midnight.isoformat(),
        })
        return result


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _new_interval(index: int, latest_seconds: int) -> dict:
    start_seconds = index * INTERVAL_SECONDS
    end_seconds = start_seconds + INTERVAL_SECONDS
    current = index == latest_seconds // INTERVAL_SECONDS
    expected_seconds = max(0, latest_seconds - start_seconds) if current else INTERVAL_SECONDS
    return {
        "index": index,
        "from_bh": start_seconds / 3600,
        "to_bh": end_seconds / 3600,
        "current": current,
        "expected_seconds": expected_seconds,
        "covered_seconds": 0.0,
        "starts": 0.0,
        "metric_sums": defaultdict(float),
        "metric_weights": defaultdict(float),
        "power_in_band_seconds": 0.0,
        "power_evaluated_seconds": 0.0,
        "target_segments": defaultdict(lambda: {
            "seconds": 0.0,
            "actual_sum": 0.0,
            "actual_weight": 0.0,
            "in_band_seconds": 0.0,
        }),
        "gap_count": 0,
        "protection_shutdowns": 0,
        "status_events": 0,
        "status_gap": False,
    }


def _split_delta(start_seconds: float, delta: float) -> Iterable[tuple[int, float]]:
    position = start_seconds
    remaining = max(0.0, delta)
    while remaining > 0:
        index = int(position // INTERVAL_SECONDS)
        boundary = (index + 1) * INTERVAL_SECONDS
        part = min(remaining, max(0.0, boundary - position))
        if part <= 0:
            break
        yield index, part
        position += part
        remaining -= part


def build_250h_diagnosis(
    samples: list[dict],
    *,
    status_events: list[dict] | None = None,
    history_complete: bool = False,
) -> dict:
    """Aggregate timestamped ODM samples into operating-hour intervals.

    Counter deltas spanning more than 30 calendar minutes are deliberately not
    credited as covered data. They still advance the observed operating-hours
    range, so completed but incompletely observed intervals remain visible.
    """
    clean = []
    for source in samples:
        at = _time(source.get("recorded_at"))
        operating = _number(source.get("operating_seconds"))
        starts = _number(source.get("starts"))
        if at is None or operating is None or starts is None:
            continue
        if not (0 <= operating <= 0xFFFFFFFF and 0 <= starts <= 0xFFFFFFFF):
            continue
        item = dict(source)
        item.update({"at": at, "operating_seconds": int(operating), "starts": int(starts)})
        clean.append(item)
    clean.sort(key=lambda item: item["at"])
    if len(clean) < 2:
        return {
            "schema": "open-dachs-manager-250h-diagnosis/v1",
            "available": False,
            "reason": "Zu wenige kohärente Historienpunkte",
            "source": "Lokale ODM-SQLite-Historie; keine MSR2-Diagnoseblöcke",
            "intervals": [],
            "metrics": METRICS,
            "notices": [{"category": "data", "metric": "data_quality", "title": "Unzureichende Daten", "text": "Für eine 250-Bh-Auswertung werden mindestens zwei kohärente Historienpunkte benötigt."}],
            "hint_count": 0,
            "action_count": 0,
            "condenser_available": False,
            "failed_start_available": False,
            "failed_start_basis": "Nicht verfügbar: Die gespeicherten ODM-Reihen enthalten derzeit keinen belegten Fehlstartzähler; Motorstatus 14 ist 200 U/min nicht erreicht und wird nicht als Fehlstart gezählt.",
        }

    latest_seconds = clean[-1]["operating_seconds"]
    first_index = clean[0]["operating_seconds"] // INTERVAL_SECONDS
    last_index = latest_seconds // INTERVAL_SECONDS
    intervals = {
        index: _new_interval(index, latest_seconds)
        for index in range(first_index, last_index + 1)
    }

    for previous, current in pairwise(clean):
        operating_delta = current["operating_seconds"] - previous["operating_seconds"]
        starts_delta = current["starts"] - previous["starts"]
        elapsed = (current["at"] - previous["at"]).total_seconds()
        if operating_delta < 0 or starts_delta < 0 or elapsed <= 0:
            for interval in intervals.values():
                interval["gap_count"] += 1
            continue
        trusted = (
            elapsed <= MAX_TRUSTED_GAP_SECONDS
            and operating_delta <= elapsed + 120
        )
        if not trusted:
            start_index = max(first_index, previous["operating_seconds"] // INTERVAL_SECONDS)
            end_index = min(last_index, current["operating_seconds"] // INTERVAL_SECONDS)
            for index in range(start_index, end_index + 1):
                intervals[index]["gap_count"] += 1
            continue
        if operating_delta <= 0:
            continue
        pair_values = {}
        for key in METRICS:
            first = _number(previous.get(key))
            second = _number(current.get(key))
            pair_values[key] = (first + second) / 2 if first is not None and second is not None else None
        target_first = _number(previous.get("power_target"))
        target_second = _number(current.get("power_target"))
        target = target_second if target_second is not None else target_first
        for index, part in _split_delta(previous["operating_seconds"], operating_delta):
            interval = intervals.get(index)
            if interval is None:
                continue
            fraction = part / operating_delta
            interval["covered_seconds"] += part
            interval["starts"] += starts_delta * fraction
            for key, value in pair_values.items():
                if value is None:
                    continue
                interval["metric_sums"][key] += value * part
                interval["metric_weights"][key] += part
            actual = pair_values["power"]
            if actual is not None and target is not None:
                segment_key = round(target, 2)
                segment = interval["target_segments"][segment_key]
                segment["seconds"] += part
                segment["actual_sum"] += actual * part
                segment["actual_weight"] += part
                interval["power_evaluated_seconds"] += part
                if abs(actual - target) <= 0.15:
                    interval["power_in_band_seconds"] += part
                    segment["in_band_seconds"] += part

    sample_times = [item["at"] for item in clean]
    previous_status: int | None = None
    for event in sorted(status_events or [], key=lambda item: str(item.get("recorded_at", ""))):
        at = _time(event.get("recorded_at"))
        status = _number(event.get("status"))
        if at is None:
            continue
        if status is None:
            previous_status = None
            position = bisect_right(sample_times, at) - 1
            if position >= 0:
                index = clean[position]["operating_seconds"] // INTERVAL_SECONDS
                if index in intervals:
                    intervals[index]["status_gap"] = True
            continue
        status_code = int(status)
        if status_code == previous_status:
            continue
        previous_status = status_code
        position = bisect_right(sample_times, at) - 1
        if position < 0:
            continue
        index = clean[position]["operating_seconds"] // INTERVAL_SECONDS
        interval = intervals.get(index)
        if interval is None:
            continue
        interval["status_events"] += 1
        if status_code == 10:
            interval["protection_shutdowns"] += 1

    output = []
    for index in sorted(intervals):
        item = intervals[index]
        expected = max(1.0, float(item["expected_seconds"]))
        coverage = min(1.0, item["covered_seconds"] / expected)
        metrics = {}
        for key in METRICS:
            weight = item["metric_weights"].get(key, 0.0)
            metrics[key] = item["metric_sums"].get(key, 0.0) / weight if weight else None
        starts = item["starts"]
        hours = item["covered_seconds"] / 3600
        ratio = hours / starts if starts > 0 else None
        failed_factor = None
        target_segments = []
        for target, segment in sorted(item["target_segments"].items()):
            target_segments.append({
                "target_kw": target,
                "operating_hours": segment["seconds"] / 3600,
                "actual_kw": segment["actual_sum"] / segment["actual_weight"] if segment["actual_weight"] else None,
                "in_band_percent": segment["in_band_seconds"] * 100 / segment["seconds"] if segment["seconds"] else None,
            })
        output.append({
            "index": index,
            "from_bh": item["from_bh"],
            "to_bh": item["to_bh"],
            "current": item["current"],
            "complete": not item["current"],
            "covered_hours": hours,
            "expected_hours": item["expected_seconds"] / 3600,
            "coverage_percent": coverage * 100,
            "sufficient": coverage >= SUFFICIENT_COVERAGE and item["gap_count"] == 0,
            "gap_count": item["gap_count"],
            "starts": starts,
            "bh_per_start": ratio,
            "failed_starts": None,
            "failed_start_percent": failed_factor,
            "failed_start_reliable": False,
            "failed_start_reason": "Die gespeicherten ODM-Reihen enthalten keinen belegten Fehlstartzähler; Motorstatus 14 wird nicht als Fehlstart gezählt.",
            "protection_shutdowns": item["protection_shutdowns"],
            "metrics": metrics,
            "power_in_band_percent": (
                item["power_in_band_seconds"] * 100 / item["power_evaluated_seconds"]
                if item["power_evaluated_seconds"] else None
            ),
            "target_segments": target_segments,
        })

    trends = {}
    completed = [item for item in output if item["complete"] and item["sufficient"]]
    for key in METRICS:
        values = [item for item in completed if item["metrics"].get(key) is not None]
        recent = values[-5:]
        consecutive = len(recent) == 5 and all(
            current["index"] == previous["index"] + 1
            for previous, current in pairwise(recent)
        )
        if consecutive:
            span = (recent[-1]["index"] - recent[0]["index"]) * 250.0
            trends[key] = (
                (recent[-1]["metrics"][key] - recent[0]["metrics"][key])
                * 1000
                / span
            )
        else:
            trends[key] = None

    latest = output[-1]
    notices = []
    evaluable = latest["coverage_percent"] >= SUFFICIENT_COVERAGE * 100 and latest["covered_hours"] >= MIN_CURRENT_EVALUATION_HOURS
    if not evaluable:
        notices.append({
            "category": "data",
            "metric": "data_quality",
            "title": "Laufender Bereich noch nicht belastbar",
            "text": f"{latest['covered_hours']:.1f} von {latest['expected_hours']:.1f} Bh beobachtet; Abdeckung {latest['coverage_percent']:.0f} %.",
        })
    if evaluable:
        if latest["bh_per_start"] is not None and latest["bh_per_start"] <= 1:
            notices.append({"category": "hint", "metric": "bh_per_start", "title": "Bh/Start ≤1", "text": "Herstellerhinweis: Hydraulik und relevante MSR2-Einstellungen prüfen; kein automatisches Defekturteil."})
        checks = (
            ("dachs_exhaust", lambda value: value >= 170, "Dachs-Abgas ≥170 °C", "Abgasweg, Rußfilter, Wärmetauscher, Fühler und Betriebsbedingungen prüfen."),
            ("coolant", lambda value: value < 72 or value > 85, "Kühlwasser außerhalb 72–85 °C", "Durchfluss, Wassermenge, Thermostat und Heizwasserqualität kontextuell prüfen."),
            ("inlet", lambda value: value >= 60, "Dachs-Eintritt in Hinweiszone", "60–65 °C ist im Infoblatt widersprüchlich; Hydraulik/Systemtemperatur prüfen, nicht als Defekt werten."),
            ("controller", lambda value: value >= 40, "Reglertemperatur ≥40 °C", "Heizraum, Wärmeeintrag, Kabeldurchführung und Außenluftansaugung prüfen."),
        )
        for key, predicate, title, text in checks:
            value = latest["metrics"].get(key)
            if value is not None and predicate(value):
                notices.append({"category": "hint", "metric": key, "title": title, "text": text})
        in_band = latest.get("power_in_band_percent")
        if in_band is not None and in_band < 90:
            notices.append({"category": "hint", "metric": "power", "title": "Generatorleistung außerhalb Soll ±150 W", "text": "Sollwertsegmente, Aufstellhöhe, Leistungsregelung und bei HR/RS den Abgasgegendruck gemeinsam prüfen."})
    for key, limit in (("motor_exhaust", 20), ("dachs_exhaust", 15), ("coolant", 10)):
        trend = trends.get(key)
        if trend is not None and trend > limit:
            notices.append({"category": "hint", "metric": key, "title": f"{METRICS[key]['title']}: Trend +{trend:.1f} K/1000 Bh", "text": f"Hersteller-Trendhinweis oberhalb +{limit} K/1000 Bh; Ursachen nur im Anlagenkontext prüfen."})
    warning_code = next((int(value) for value in reversed([_number(item.get("warning_code")) for item in clean]) if value is not None), 0)
    service_code = next((int(value) for value in reversed([_number(item.get("service_code")) for item in clean]) if value is not None), 0)
    if warning_code:
        notices.append({"category": "warning", "metric": "warning_code", "title": f"Aktive Warnung {warning_code}", "text": "Gespeicherter letzter Warncode; getrennt von den 250-Bh-Herstellerhinweisen."})
    if service_code or latest["protection_shutdowns"]:
        detail = f"Servicecode {service_code}" if service_code else f"{latest['protection_shutdowns']} Störabschaltung(en)"
        notices.append({"category": "protection", "metric": "protection_shutdown", "title": detail, "text": "Schutz-/Störungskontext aus der gespeicherten Historie; keine Ableitung aus einer Trendgrenze."})

    action_count = sum(1 for item in notices if item["category"] in {"hint", "warning", "protection"})
    return {
        "schema": "open-dachs-manager-250h-diagnosis/v1",
        "available": True,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "Lokale ODM-SQLite-Historie; keine MSR2-Diagnoseblöcke und keine zusätzlichen Blockreads",
        "sampling": {"interval_hours": 250, "trusted_gap_minutes": MAX_TRUSTED_GAP_SECONDS // 60, "sufficient_coverage_percent": int(SUFFICIENT_COVERAGE * 100)},
        "history_from": clean[0]["at"].isoformat(),
        "history_to": clean[-1]["at"].isoformat(),
        "operating_hours_from": clean[0]["operating_seconds"] / 3600,
        "operating_hours_to": latest_seconds / 3600,
        "intervals": output,
        "trends_k_per_1000_bh": trends,
        "metrics": METRICS,
        "notices": notices,
        "hint_count": action_count,
        "action_count": action_count,
        "condenser_available": False,
        "failed_start_available": False,
        "failed_start_basis": "Nicht verfügbar: Die gespeicherten ODM-Reihen enthalten derzeit keinen belegten Fehlstartzähler; Motorstatus 14 ist 200 U/min nicht erreicht und wird nicht als Fehlstart gezählt.",
    }
