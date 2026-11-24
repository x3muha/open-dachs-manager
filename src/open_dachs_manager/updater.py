"""Signed stable-release discovery and unprivileged update staging.

The web process never installs software. It validates one fixed GitHub repo,
stages immutable inputs, then atomically drops one request for the root helper.
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

REPOSITORY = "x3muha/open-dachs-manager"
GITHUB_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MANIFEST_NAME = "open_dachs_update_manifest.json"
SIGNATURE_NAME = "open_dachs_update_manifest.sig"
MANIFEST_SCHEMA = "open-dachs-release/v1"
SETTINGS_SCHEMA = "open-dachs-update-settings/v1"
STATE_SCHEMA = "open-dachs-update-state/v1"
REQUEST_SCHEMA = "open-dachs-update-request/v1"
HELPER_VERSION = "1.0.0"
UPDATE_DB_SCHEMA = 1
DEPENDENCY_FINGERPRINT = hashlib.sha256(b"pyserial>=3.5\nreportlab>=4.2\n").hexdigest()
TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
ASSET_HOSTS = frozenset({"github.com", "api.github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
MAX_API_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 4096
MAX_WHEEL_BYTES = 128 * 1024 * 1024
NETWORK_TIMEOUT = 10.0
PREVIEW_SECONDS = 300
CHECK_INTERVAL_SECONDS = 6 * 3600
SCHEDULER_INTERVAL_SECONDS = 60
BACKOFF_MAX_SECONDS = 24 * 3600
AUTO_RELEASE_AGE_SECONDS = 48 * 3600


class UpdateError(ValueError):
    pass


def parse_version(value: object) -> tuple[int, int, int]:
    match = TAG_RE.fullmatch(str(value))
    if not match:
        raise UpdateError("release tag must be strict vX.Y.Z")
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def version_text(value: tuple[int, int, int]) -> str:
    return ".".join(str(item) for item in value)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _parse_timestamp(value: object) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpdateError("invalid published_at") from exc
    if parsed.tzinfo is None:
        raise UpdateError("published_at needs timezone")
    return parsed.astimezone(dt.timezone.utc)


def validate_manifest(raw: bytes, *, expected_tag: str | None = None) -> dict:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("manifest too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("invalid manifest JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise UpdateError("manifest is not canonical JSON")
    required = {"schema", "repo", "version", "tag", "release_id", "commit", "wheel", "wheel_size", "wheel_sha256", "dependency_fingerprint", "db_schema_from", "db_schema_to", "rollback_compatible", "min_helper_version", "auto_eligible", "published_at"}
    if set(value) != required:
        raise UpdateError("manifest fields mismatch")
    tag = str(value["tag"])
    parsed = parse_version(tag)
    if value["schema"] != MANIFEST_SCHEMA or value["repo"] != REPOSITORY:
        raise UpdateError("manifest identity mismatch")
    if value["version"] != version_text(parsed) or (expected_tag and tag != expected_tag):
        raise UpdateError("manifest version/tag mismatch")
    expected_wheel = f"open_dachs_manager-{value['version']}-py3-none-any.whl"
    if value["wheel"] != expected_wheel:
        raise UpdateError("wheel filename mismatch")
    if isinstance(value["wheel_size"], bool) or not isinstance(value["wheel_size"], int) or not 0 < value["wheel_size"] <= MAX_WHEEL_BYTES:
        raise UpdateError("invalid wheel size")
    if not SHA256_RE.fullmatch(str(value["wheel_sha256"])) or not SHA256_RE.fullmatch(str(value["dependency_fingerprint"])):
        raise UpdateError("invalid SHA-256")
    if not COMMIT_RE.fullmatch(str(value["commit"])):
        raise UpdateError("invalid release commit")
    if isinstance(value["release_id"], bool) or not isinstance(value["release_id"], int) or value["release_id"] <= 0:
        raise UpdateError("invalid release id")
    for field in ("db_schema_from", "db_schema_to"):
        if isinstance(value[field], bool) or not isinstance(value[field], int) or value[field] < 1:
            raise UpdateError(f"invalid {field}")
    if type(value["rollback_compatible"]) is not bool or type(value["auto_eligible"]) is not bool:
        raise UpdateError("manifest booleans invalid")
    parse_version("v" + str(value["min_helper_version"]))
    _parse_timestamp(value["published_at"])
    return value


def release_compatible(manifest: dict) -> tuple[bool, str | None]:
    if manifest["dependency_fingerprint"] != DEPENDENCY_FINGERPRINT:
        return False, "Abhängigkeiten geändert – manuelle Aktualisierung erforderlich"
    if manifest["db_schema_from"] != UPDATE_DB_SCHEMA or manifest["db_schema_to"] != UPDATE_DB_SCHEMA:
        return False, "Datenbankschema geändert – manuelle Aktualisierung erforderlich"
    if not manifest["rollback_compatible"]:
        return False, "Release ist nicht selbst-rollback-kompatibel"
    if parse_version("v" + manifest["min_helper_version"]) > parse_version("v" + HELPER_VERSION):
        return False, "Installierter Update-Helfer ist zu alt"
    return True, None


def _safe_url(url: str, *, asset_id: int | None = None) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise UpdateError("unsafe update URL")
    if parsed.hostname not in ASSET_HOSTS or parsed.port not in (None, 443):
        raise UpdateError("update URL host is not allowed")
    if asset_id is not None and parsed.hostname == "api.github.com":
        exact = f"/repos/{REPOSITORY}/releases/assets/{asset_id}"
        if parsed.path != exact or parsed.query:
            raise UpdateError("asset API URL mismatch")
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_https(url: str, *, limit: int, etag: str | None = None, accept: str = "application/vnd.github+json") -> tuple[int, bytes, str | None, str]:
    _safe_url(url)
    headers = {"Accept": accept, "User-Agent": "open-dachs-manager-updater/1", "X-GitHub-Api-Version": "2022-11-28"}
    if etag:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_SafeRedirect)
    try:
        with opener.open(request, timeout=NETWORK_TIMEOUT) as response:
            final_url = _safe_url(response.geturl())
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > limit:
                raise UpdateError("download exceeds size limit")
            body = response.read(limit + 1)
            if len(body) > limit:
                raise UpdateError("download exceeds size limit")
            return response.status, body, response.headers.get("ETag"), final_url
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, b"", exc.headers.get("ETag") or etag, url
        raise UpdateError(f"GitHub HTTP {exc.code}") from exc
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise UpdateError("GitHub request failed") from exc


def verify_signature(manifest_path: Path, signature_path: Path, public_key: Path) -> None:
    try:
        result = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_key), "-sigfile", str(signature_path), "-rawin", "-in", str(manifest_path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("signature verifier unavailable") from exc
    if result.returncode != 0:
        raise UpdateError("release signature invalid")


def verify_wheel(path: Path, manifest: dict) -> None:
    digest = hashlib.sha256(); size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk); digest.update(chunk)
    if size != manifest["wheel_size"] or digest.hexdigest() != manifest["wheel_sha256"]:
        raise UpdateError("wheel size/hash mismatch")
    if path.name != manifest["wheel"]:
        raise UpdateError("wheel path mismatch")
    dist_info = f"open_dachs_manager-{manifest['version']}.dist-info"
    record_name, metadata_name = f"{dist_info}/RECORD", f"{dist_info}/METADATA"
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or record_name not in names or metadata_name not in names:
                raise UpdateError("wheel RECORD/METADATA missing")
            for name in names:
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts or "\\" in name:
                    raise UpdateError("unsafe wheel path")
            metadata = archive.read(metadata_name).decode("utf-8", "strict")
            if "Name: open-dachs-manager\n" not in metadata or f"Version: {manifest['version']}\n" not in metadata:
                raise UpdateError("wheel name/version mismatch")
            rows = archive.read(record_name).decode("utf-8", "strict").splitlines(); seen = set()
            for row in rows:
                fields = row.split(",")
                if len(fields) != 3 or fields[0] in seen:
                    raise UpdateError("invalid wheel RECORD")
                seen.add(fields[0])
                if fields[0] == record_name:
                    if fields[1] or fields[2]: raise UpdateError("invalid RECORD self entry")
                    continue
                if fields[0] not in names or not fields[1].startswith("sha256=") or not fields[2].isdigit():
                    raise UpdateError("incomplete wheel RECORD")
                raw = archive.read(fields[0])
                actual = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
                if not hmac.compare_digest(actual, fields[1][7:]) or len(raw) != int(fields[2]):
                    raise UpdateError("wheel RECORD mismatch")
            if seen != set(names):
                raise UpdateError("wheel contains unrecorded files")
    except (zipfile.BadZipFile, UnicodeError, KeyError) as exc:
        raise UpdateError("invalid wheel") from exc


def atomic_json(path: Path, value: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json(value) + b"\n"); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, mode); os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError): os.unlink(temporary)


@dataclasses.dataclass
class UpdatePaths:
    data_dir: Path
    staging_dir: Path = Path("/var/lib/open-dachs-manager/update-staging")
    request_path: Path = Path("/var/lib/open-dachs-manager/update-request.json")
    public_key: Path = Path(__file__).with_name("data") / "update_release_public_key.pem"
    @property
    def settings(self): return self.data_dir / "update-settings.json"
    @property
    def state(self): return self.data_dir / "update-state.json"
    @property
    def job_status(self): return self.data_dir / "update-job-status.json"


class UpdateManager:
    def __init__(self, data_dir: str | Path, current_version: str, *, paths: UpdatePaths | None = None, fetcher: Callable = fetch_https, now: Callable[[], dt.datetime] | None = None):
        self.current_version = current_version
        self.paths = paths or UpdatePaths(Path(data_dir))
        self.fetcher = fetcher
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.lock = threading.RLock(); self.stop_event = threading.Event(); self.preview: dict | None = None
        self.settings_corrupt = False; self.settings = self._load_settings(); self.state = self._load_state()
        self.thread = threading.Thread(target=self._loop, name="dachs-update-check", daemon=True)

    @staticmethod
    def default_settings() -> dict:
        return {"schema": SETTINGS_SCHEMA, "automatic": False, "timezone": "Europe/Berlin", "window_start": "02:00", "window_end": "04:00"}

    def _load_settings(self) -> dict:
        if not self.paths.settings.exists(): return self.default_settings()
        try:
            value = json.loads(self.paths.settings.read_text("utf-8")); expected = self.default_settings()
            if set(value) != set(expected) or value["schema"] != SETTINGS_SCHEMA or type(value["automatic"]) is not bool or value["timezone"] != "Europe/Berlin" or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value["window_start"]) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value["window_end"]): raise ValueError
            return value
        except Exception:
            self.settings_corrupt = True; return self.default_settings()

    def _load_state(self) -> dict:
        base = {"schema": STATE_SCHEMA, "etag": None, "checked_at": None, "next_check_at": None, "failures": 0, "available": None, "error": None, "operation": None, "attempted_versions": []}
        if not self.paths.state.exists(): return base
        try:
            value = json.loads(self.paths.state.read_text("utf-8"))
            if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA: raise ValueError
            return {**base, **{key: value.get(key) for key in base}}
        except Exception:
            base["error"] = "Update-Status beschädigt; sicher deaktiviert"; return base

    def _save_state(self): atomic_json(self.paths.state, self.state)
    def start(self):
        if not self.thread.is_alive(): self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive(): self.thread.join(timeout=2)
    def _loop(self):
        self.stop_event.wait(3)
        while not self.stop_event.is_set():
            try:
                self._reconcile_job_status()
                due = _parse_timestamp(self.state["next_check_at"]) if self.state.get("next_check_at") else None
                if due is None or self.now() >= due:
                    self.check()
                self._automatic_tick()
            except Exception: pass
            if self.stop_event.wait(SCHEDULER_INTERVAL_SECONDS): break

    def _reconcile_job_status(self) -> dict | None:
        """Consume terminal helper state independently of any HTTP/UI request."""
        operation = self.state.get("operation")
        try:
            fd = os.open(self.paths.job_status, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if not 0 <= info.st_size <= 16384 or not stat.S_ISREG(info.st_mode):
                    raise UpdateError("invalid job status")
                raw = os.read(fd, 16385)
            finally:
                os.close(fd)
            job = json.loads(raw.decode("utf-8"))
            if not isinstance(job, dict) or not isinstance(operation, dict) or job.get("request_id") != operation.get("request_id"):
                return operation
            self.state["operation"] = job
            terminal_failure = job.get("status") in {"failed", "rolled-back", "rollback-failed", "recovery-required"}
            if job.get("automatic") is True and terminal_failure and self.settings["automatic"]:
                self.settings["automatic"] = False
                atomic_json(self.paths.settings, self.settings)
                self.state["error"] = "Automatik nach fehlgeschlagenem Helferlauf deaktiviert"
            self._save_state()
            return job
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError, UnicodeError, UpdateError):
            return operation

    def public_status(self) -> dict:
        with self.lock:
            operation = self._reconcile_job_status()
            return {"schema": STATE_SCHEMA, "current_version": self.current_version, "checked_at": self.state.get("checked_at"), "next_check_at": self.state.get("next_check_at"), "available": self.state.get("available"), "error": self.state.get("error"), "automatic": bool(self.settings["automatic"]), "settings": dict(self.settings), "settings_corrupt": self.settings_corrupt, "operation": operation}

    @staticmethod
    def _asset_map(release: dict) -> dict[str, dict]:
        assets = release.get("assets")
        if not isinstance(assets, list): raise UpdateError("release assets missing")
        result = {}
        for asset in assets:
            if not isinstance(asset, dict): continue
            name = asset.get("name")
            if name in result: raise UpdateError("duplicate release asset")
            if isinstance(name, str): result[name] = asset
        return result

    def _download_asset(self, asset: dict, target: Path, limit: int) -> bytes:
        asset_id = asset.get("id")
        if isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id <= 0: raise UpdateError("invalid asset id")
        url = f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset_id}"; _safe_url(url, asset_id=asset_id)
        status, body, _, _ = self.fetcher(url, limit=limit, accept="application/octet-stream")
        if status != 200: raise UpdateError("asset download failed")
        target.write_bytes(body); target.chmod(0o600); return body

    def check(self, *, force: bool = False) -> dict:
        with self.lock:
            try:
                status, body, new_etag, _ = self.fetcher(GITHUB_API, limit=MAX_API_BYTES, etag=None if force else self.state.get("etag")); now = self.now()
                if status == 304:
                    self.state.update({"checked_at": now.isoformat(), "next_check_at": (now + dt.timedelta(seconds=CHECK_INTERVAL_SECONDS)).isoformat(), "failures": 0, "error": None}); self._save_state(); return self.public_status()
                release = json.loads(body.decode("utf-8")); tag = release.get("tag_name"); target_version = parse_version(tag)
                if release.get("draft") is not False or release.get("prerelease") is not False: raise UpdateError("latest release is not stable")
                github_published_at = _parse_timestamp(release.get("published_at")).isoformat()
                release_id = release.get("id")
                if isinstance(release_id, bool) or not isinstance(release_id, int): raise UpdateError("invalid release id")
                if target_version <= parse_version("v" + self.current_version): available = None
                else:
                    assets = self._asset_map(release); wheel_name = f"open_dachs_manager-{version_text(target_version)}-py3-none-any.whl"
                    for required in (MANIFEST_NAME, SIGNATURE_NAME, wheel_name):
                        if required not in assets: raise UpdateError(f"release asset missing: {required}")
                    with tempfile.TemporaryDirectory(prefix="odm-update-check-") as temp:
                        directory = Path(temp); manifest_raw = self._download_asset(assets[MANIFEST_NAME], directory / MANIFEST_NAME, MAX_MANIFEST_BYTES); self._download_asset(assets[SIGNATURE_NAME], directory / SIGNATURE_NAME, MAX_SIGNATURE_BYTES)
                        manifest = validate_manifest(manifest_raw, expected_tag=tag)
                        if manifest["release_id"] != release_id: raise UpdateError("release id mismatch")
                        if _parse_timestamp(manifest["published_at"]) != _parse_timestamp(github_published_at): raise UpdateError("published_at does not match GitHub release")
                        if str(release.get("target_commitish") or "") not in {manifest["commit"], tag}:
                            raise UpdateError("release target/manifest commit mismatch")
                        verify_signature(directory / MANIFEST_NAME, directory / SIGNATURE_NAME, self.paths.public_key)
                    compatible, reason = release_compatible(manifest)
                    available = {"version": manifest["version"], "tag": tag, "published_at": github_published_at, "release_id": release_id, "commit": manifest["commit"], "release_notes": str(release.get("body") or "")[:20000], "compatible": compatible, "manual_required_reason": reason, "auto_eligible": bool(manifest["auto_eligible"]), "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(), "wheel_sha256": manifest["wheel_sha256"], "assets": {name: {"id": assets[name]["id"], "size": assets[name].get("size")} for name in (MANIFEST_NAME, SIGNATURE_NAME, wheel_name)}, "manifest": manifest}
                self.state.update({"etag": new_etag, "checked_at": now.isoformat(), "next_check_at": (now + dt.timedelta(seconds=CHECK_INTERVAL_SECONDS)).isoformat(), "failures": 0, "available": available, "error": None})
            except Exception as exc:
                failures = int(self.state.get("failures") or 0) + 1; now = self.now(); delay = min(BACKOFF_MAX_SECONDS, 300 * 2 ** min(8, failures - 1))
                self.state.update({"checked_at": now.isoformat(), "next_check_at": (now + dt.timedelta(seconds=delay)).isoformat(), "failures": failures, "error": str(exc)[:240]})
            self._save_state(); return self.public_status()

    @staticmethod
    def _assert_real_admin(local_access: bool):
        if local_access: raise PermissionError("Updates sind über den lokalen Override gesperrt")

    def save_settings(self, payload: dict, *, local_access: bool, password_ok: bool = False) -> dict:
        if set(payload) != {"automatic", "timezone", "window_start", "window_end"} or type(payload["automatic"]) is not bool: raise UpdateError("strict update settings payload required")
        if local_access and payload["automatic"]:
            raise PermissionError("Automatische Updates können über den lokalen Override nicht aktiviert werden")
        value = {"schema": SETTINGS_SCHEMA, **payload}
        if value["timezone"] != "Europe/Berlin" or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value["window_start"])) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value["window_end"])): raise UpdateError("invalid maintenance window")
        if value["window_start"] >= value["window_end"]: raise UpdateError("maintenance window requires start before end")
        if value["automatic"] and not password_ok: raise PermissionError("Admin-Passwort und ausdrückliche Bestätigung erforderlich")
        if value["automatic"] and self.settings_corrupt: raise UpdateError("beschädigte Einstellungen: Automatik bleibt aus")
        atomic_json(self.paths.settings, value); self.settings = value; self.settings_corrupt = False; return self.public_status()

    def preview_update(self, username: str, password_ok: bool, *, local_access: bool, session_nonce: str, auth_epoch: int) -> dict:
        self._assert_real_admin(local_access)
        if not password_ok: raise PermissionError("Admin-Passwort ist falsch")
        with self.lock:
            available = self.state.get("available")
            if not available or not available.get("compatible"): raise UpdateError("kein kompatibles neueres Release verfügbar")
            token = secrets.token_urlsafe(32); expires = time.time() + PREVIEW_SECONDS
            binding = {"username": username, "session_nonce": session_nonce, "auth_epoch": int(auth_epoch), "current_version": self.current_version, "version": available["version"], "manifest_sha256": available["manifest_sha256"], "wheel_sha256": available["wheel_sha256"]}
            self.preview = {**binding, "token_hash": hashlib.sha256(token.encode()).hexdigest(), "expires": expires}
            return {"preview_token": token, "expires_at": dt.datetime.fromtimestamp(expires, dt.timezone.utc).isoformat(), "confirmation": f"OPEN DACHS {available['version']} AKTUALISIEREN", "release": {key: available[key] for key in ("version", "tag", "published_at", "release_notes", "wheel_sha256")}}

    def apply(self, username: str, payload: dict, *, local_access: bool, session_nonce: str, auth_epoch: int) -> dict:
        self._assert_real_admin(local_access)
        if set(payload) != {"preview_token", "confirmation", "request_id"}: raise UpdateError("strict update apply payload required")
        request_id = str(payload["request_id"])
        if not re.fullmatch(r"[a-f0-9]{32}", request_id): raise UpdateError("invalid request id")
        with self.lock:
            preview, self.preview = self.preview, None; available = self.state.get("available")
            token_hash = hashlib.sha256(str(payload["preview_token"]).encode()).hexdigest()
            if not preview or time.time() > preview["expires"] or not hmac.compare_digest(preview["token_hash"], token_hash): raise UpdateError("preview token invalid or expired")
            if preview["username"] != username or not available or preview["version"] != available["version"] or preview["manifest_sha256"] != available["manifest_sha256"] or preview["wheel_sha256"] != available["wheel_sha256"]: raise UpdateError("preview binding mismatch")
            if preview["session_nonce"] != session_nonce or preview["auth_epoch"] != int(auth_epoch): raise UpdateError("preview session binding mismatch")
            if payload["confirmation"] != f"OPEN DACHS {available['version']} AKTUALISIEREN": raise UpdateError("confirmation mismatch")
            return self._stage(request_id, username, available, automatic=False)

    def _automatic_tick(self) -> None:
        with self.lock:
            available = self.state.get("available")
            if not self.settings["automatic"] or not available or not available.get("compatible") or not available.get("auto_eligible"): return
            if available["version"] in set(self.state.get("attempted_versions") or []): return
            if self.now() - _parse_timestamp(available["published_at"]) < dt.timedelta(seconds=AUTO_RELEASE_AGE_SECONDS): return
            from zoneinfo import ZoneInfo
            local = self.now().astimezone(ZoneInfo("Europe/Berlin")); current = local.strftime("%H:%M")
            if not self.settings["window_start"] <= current < self.settings["window_end"]: return
            self.state.setdefault("attempted_versions", []).append(available["version"]); self._save_state()
            try: self._stage(secrets.token_hex(16), "automatic-updater", available, automatic=True)
            except Exception as exc:
                self.settings["automatic"] = False; atomic_json(self.paths.settings, self.settings)
                self.state["error"] = f"Automatik nach Fehler deaktiviert: {str(exc)[:160]}"; self._save_state()

    def _stage(self, request_id: str, username: str, available: dict, automatic: bool) -> dict:
        if self.paths.request_path.exists(): raise UpdateError("another update request is pending")
        self.paths.staging_dir.mkdir(parents=True, exist_ok=True); self.paths.staging_dir.chmod(0o700)
        target = self.paths.staging_dir / request_id; target.mkdir(mode=0o700)
        assets, manifest = available["assets"], available["manifest"]
        try:
            manifest_raw = self._download_asset(assets[MANIFEST_NAME], target / MANIFEST_NAME, MAX_MANIFEST_BYTES); self._download_asset(assets[SIGNATURE_NAME], target / SIGNATURE_NAME, MAX_SIGNATURE_BYTES); self._download_asset(assets[manifest["wheel"]], target / manifest["wheel"], MAX_WHEEL_BYTES)
            checked = validate_manifest(manifest_raw, expected_tag=available["tag"]); verify_signature(target / MANIFEST_NAME, target / SIGNATURE_NAME, self.paths.public_key); verify_wheel(target / checked["wheel"], checked)
            if hashlib.sha256(manifest_raw).hexdigest() != available["manifest_sha256"]: raise UpdateError("manifest changed after preview")
            request = {"schema": REQUEST_SCHEMA, "request_id": request_id, "requested_at": self.now().isoformat(), "requested_by": username, "automatic": automatic, "version": checked["version"], "release_id": available["release_id"], "commit": available["commit"], "published_at": available["published_at"], "manifest_sha256": available["manifest_sha256"], "wheel_sha256": checked["wheel_sha256"]}
            atomic_json(target / "request.json", request); os.link(target / "request.json", self.paths.request_path)
            operation = {"request_id": request_id, "version": checked["version"], "status": "requested", "requested_at": request["requested_at"], "automatic": automatic}
            self.state["operation"] = operation; self._save_state(); return operation
        except Exception:
            shutil.rmtree(target, ignore_errors=True); raise
