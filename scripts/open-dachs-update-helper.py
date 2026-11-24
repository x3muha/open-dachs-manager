#!/usr/bin/python3
"""Root-only, argument-free, crash-recoverable ODM release switch helper.

The helper is installed outside release wheels.  It independently validates all
signed inputs and GitHub release identity; candidate code is never imported into
this process.
"""
from __future__ import annotations

import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

PRODUCT_DATA = Path("/var/lib/open-dachs-manager")
UPDATER_ROOT = Path("/var/lib/open-dachs-manager-updater")
RUNTIME_ROOT = Path("/run/open-dachs-manager-updater")
REQUEST = PRODUCT_DATA / "update-request.json"
STAGING = PRODUCT_DATA / "update-staging"
STATE = UPDATER_ROOT / "state.json"
JOB_STATUS = PRODUCT_DATA / "update-job-status.json"
EVIDENCE = UPDATER_ROOT / "evidence"
BACKUPS = UPDATER_ROOT / "backups"
CUSTODY = RUNTIME_ROOT / "custody"
LOCK = RUNTIME_ROOT / "update.lock"
RELEASES = Path("/opt/open-dachs-manager/releases")
CURRENT = Path("/opt/open-dachs-manager/current")
PUBLIC_KEY = Path("/usr/local/share/open-dachs-manager/update_release_public_key.pem")
DATABASE = PRODUCT_DATA / "dachs-web.db"
SERIAL_SOCKET = Path("/run/open-dachs-manager/serial.sock")
CONFIG = Path("/etc/open-dachs-manager/open-dachs-manager.env")
SERVICES = ("open-dachs-manager-serial.service", "open-dachs-manager-web.service")
MANIFEST = "open_dachs_update_manifest.json"
SIGNATURE = "open_dachs_update_manifest.sig"
REQUEST_SCHEMA = "open-dachs-update-request/v1"
MANIFEST_SCHEMA = "open-dachs-release/v1"
EVIDENCE_SCHEMA = "open-dachs-update-evidence/v1"
REPO = "x3muha/open-dachs-manager"
GITHUB_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases"
TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA = re.compile(r"^[a-f0-9]{64}$")
COMMIT = re.compile(r"^[a-f0-9]{40}$")
REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
DEPENDENCIES = hashlib.sha256(b"pyserial>=3.5\nreportlab>=4.2\n").hexdigest()
DB_SCHEMA = 1
HELPER_VERSION = (1, 0, 0)
AUTO_MIN_AGE = dt.timedelta(hours=48)
MAX_WHEEL = 128 * 1024 * 1024
RESERVE_BYTES = 1024 * 1024 * 1024
MUTABLE_FILES = (
    "users.json", "maintenance_settings.json", "dashboard_settings.json",
    "soot_filter_settings.json", "api_settings.json", "local_access_settings.json",
    "email_notification_settings.json", "serial_state.json", "controller_clock_sync.json",
    "controller_clock_sync_state.json", "runtime_auto_reset.json", "runtime_auto_reset_state.json",
    "update-settings.json", "update-state.json", "diagnosis_250h_cache.json",
    "dashboard-cleanup-v1.done", "phase6_verify.json", "servicecodes_de.properties",
)
MUTABLE_DIRS = ("secrets", "backup-archive", "restore-preimages", "phase6-verify")


class Failure(RuntimeError):
    def __init__(self, error_class: str):
        self.error_class = error_class
        super().__init__(error_class)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def parse_time(value, error_class="invalid-timestamp"):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise Failure(error_class) from exc
    if parsed.tzinfo is None:
        raise Failure(error_class)
    return parsed.astimezone(dt.timezone.utc)


def parse_version(value, error_class="invalid-version"):
    match = VERSION.fullmatch(str(value))
    if not match:
        raise Failure(error_class)
    return tuple(int(item) for item in match.groups())


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value, mode=0o600):
    ensure_root_dir(path.parent)
    name = "." + path.name + "." + os.urandom(8).hex()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.parent / name, flags, mode)
    try:
        os.write(fd, canonical(value) + b"\n")
        os.fsync(fd)
        os.fchmod(fd, mode)
        os.fchown(fd, 0, 0)
    finally:
        os.close(fd)
    try:
        os.replace(path.parent / name, path)
        fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path.parent / name)


def publish(value):
    public = {key: value.get(key) for key in (
        "schema", "request_id", "version", "automatic", "started_at", "finished_at",
        "status", "phase", "error_class", "rollback_error_class",
    ) if key in value}
    # JOB_STATUS is the only helper-owned file under the service-user data tree.
    temporary = JOB_STATUS.with_name("." + JOB_STATUS.name + "." + os.urandom(8).hex())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, canonical(public) + b"\n")
        os.fsync(fd)
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)
    os.replace(temporary, JOB_STATUS)
    fsync_dir(JOB_STATUS.parent)


def ensure_root_dir(path, mode=0o700):
    path = Path(path)
    if not path.is_absolute():
        raise Failure("unsafe-directory")
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, mode)
            os.chown(current, 0, 0)
            fsync_dir(current.parent)
            info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise Failure("unsafe-directory")
        if current in {Path("/var"), Path("/var/lib"), Path("/run"), Path("/opt")}:
            continue
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise Failure("unsafe-directory-owner-mode")
    os.chmod(path, mode)
    os.chown(path, 0, 0)
    return path


def verify_parent_chain(path, *, service_leaf=False):
    path = Path(path)
    current = Path("/")
    service_uid = pwd.getpwnam("open-dachs").pw_uid if service_leaf else -1
    for index, part in enumerate(path.parts[1:]):
        current /= part
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise Failure("symlink-parent")
        if index < 1:
            continue
        is_service = service_leaf and current in {PRODUCT_DATA, STAGING, path}
        allowed = {0, service_uid} if is_service else {0}
        if info.st_uid not in allowed:
            raise Failure("unsafe-parent-owner")
        if not is_service and info.st_mode & 0o022:
            raise Failure("writable-parent")


def safe_regular(path, *, root_owned=False, max_size=None):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink < 1:
        os.close(fd); raise Failure("unsafe-file")
    if info.st_mode & 0o022:
        os.close(fd); raise Failure("writable-file")
    if root_owned and info.st_uid != 0:
        os.close(fd); raise Failure("file-not-root-owned")
    if max_size is not None and info.st_size > max_size:
        os.close(fd); raise Failure("file-too-large")
    return fd


def read_fd(fd, limit):
    chunks, total = [], 0
    while True:
        part = os.read(fd, min(1024 * 1024, limit + 1 - total))
        if not part:
            break
        chunks.append(part); total += len(part)
        if total > limit:
            raise Failure("file-too-large")
    return b"".join(chunks)


def root_copy(path, destination, limit):
    fd = safe_regular(path, max_size=limit)
    try:
        raw = read_fd(fd, limit)
    finally:
        os.close(fd)
    outfd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(outfd, raw); os.fsync(outfd); os.fchown(outfd, 0, 0); os.fchmod(outfd, 0o600)
    finally:
        os.close(outfd)
    fsync_dir(Path(destination).parent)
    return raw


def load_json_regular(path, limit=16384, *, root_owned=False):
    fd = safe_regular(path, root_owned=root_owned, max_size=limit)
    try:
        return json.loads(read_fd(fd, limit).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Failure("invalid-json") from exc
    finally:
        os.close(fd)


def load_request():
    value = load_json_regular(REQUEST)
    required = {"schema", "request_id", "requested_at", "requested_by", "automatic",
                "version", "release_id", "commit", "published_at", "manifest_sha256", "wheel_sha256"}
    if not isinstance(value, dict) or set(value) != required or value["schema"] != REQUEST_SCHEMA:
        raise Failure("invalid-request")
    if not REQUEST_ID.fullmatch(str(value["request_id"])) or not VERSION.fullmatch(str(value["version"])):
        raise Failure("invalid-request-identity")
    if type(value["automatic"]) is not bool or not SHA.fullmatch(str(value["manifest_sha256"])) or not SHA.fullmatch(str(value["wheel_sha256"])):
        raise Failure("invalid-request-fields")
    if isinstance(value["release_id"], bool) or not isinstance(value["release_id"], int) or value["release_id"] <= 0:
        raise Failure("invalid-request-release")
    if not COMMIT.fullmatch(str(value["commit"])):
        raise Failure("invalid-request-commit")
    parse_time(value["requested_at"], "invalid-request-time")
    parse_time(value["published_at"], "invalid-request-published-at")
    if not isinstance(value["requested_by"], str) or not 1 <= len(value["requested_by"]) <= 64:
        raise Failure("invalid-request-user")
    return value


def validate_manifest(raw, request, current_version):
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise Failure("invalid-manifest") from exc
    required = {"schema", "repo", "version", "tag", "release_id", "commit", "wheel", "wheel_size",
                "wheel_sha256", "dependency_fingerprint", "db_schema_from", "db_schema_to",
                "rollback_compatible", "min_helper_version", "auto_eligible", "published_at"}
    if not isinstance(value, dict) or set(value) != required or canonical(value) != raw:
        raise Failure("manifest-contract")
    match = TAG.fullmatch(str(value["tag"])); version = str(value["version"])
    if value["schema"] != MANIFEST_SCHEMA or value["repo"] != REPO or not match or version != ".".join(match.groups()):
        raise Failure("manifest-identity")
    if version != request["version"] or parse_version(version) <= parse_version(current_version, "invalid-current-version"):
        raise Failure("candidate-not-newer")
    if value["release_id"] != request["release_id"] or value["commit"] != request["commit"]:
        raise Failure("release-binding")
    if not COMMIT.fullmatch(str(value["commit"])) or isinstance(value["release_id"], bool) or not isinstance(value["release_id"], int) or value["release_id"] <= 0:
        raise Failure("invalid-release-fields")
    if value["wheel"] != f"open_dachs_manager-{version}-py3-none-any.whl" or value["wheel_sha256"] != request["wheel_sha256"]:
        raise Failure("wheel-identity")
    if not SHA.fullmatch(str(value["wheel_sha256"])) or not SHA.fullmatch(str(value["dependency_fingerprint"])):
        raise Failure("invalid-hash")
    if type(value["rollback_compatible"]) is not bool or type(value["auto_eligible"]) is not bool:
        raise Failure("invalid-manifest-boolean")
    if value["dependency_fingerprint"] != DEPENDENCIES or value["db_schema_from"] != DB_SCHEMA or value["db_schema_to"] != DB_SCHEMA or value["rollback_compatible"] is not True:
        raise Failure("release-not-rollback-compatible")
    if any(type(value[field]) is not int or value[field] < 1 for field in ("db_schema_from", "db_schema_to")):
        raise Failure("invalid-schema-version")
    minimum = parse_version(value["min_helper_version"], "invalid-min-helper")
    if minimum > HELPER_VERSION:
        raise Failure("helper-too-old")
    if type(value["wheel_size"]) is not int or not 0 < value["wheel_size"] <= MAX_WHEEL:
        raise Failure("wheel-size")
    published = parse_time(value["published_at"], "invalid-published-at")
    if published != parse_time(request["published_at"], "invalid-request-published-at"):
        raise Failure("published-at-binding")
    if hashlib.sha256(raw).hexdigest() != request["manifest_sha256"]:
        raise Failure("manifest-hash")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure("github-redirect")


def fetch_github_release(release_id):
    url = f"{GITHUB_RELEASE_API}/{int(release_id)}"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "open-dachs-manager-root-helper/1",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            raw = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 1024 * 1024 or response.geturl() != url:
                raise Failure("github-release-response")
            return json.loads(raw.decode("utf-8"))
    except Failure:
        raise
    except Exception as exc:
        raise Failure("github-release-unavailable") from exc


def validate_github_release(value, manifest, request, now=None):
    if not isinstance(value, dict) or value.get("id") != manifest["release_id"]:
        raise Failure("github-release-id")
    if value.get("draft") is not False or value.get("prerelease") is not False or value.get("tag_name") != manifest["tag"]:
        raise Failure("github-release-state")
    if value.get("target_commitish") != manifest["commit"]:
        raise Failure("github-release-commit")
    actual = parse_time(value.get("published_at"), "github-published-at")
    if actual != parse_time(manifest["published_at"]):
        raise Failure("github-published-at-binding")
    if request["automatic"]:
        if manifest["auto_eligible"] is not True:
            raise Failure("automatic-not-eligible")
        now = now or dt.datetime.now(dt.timezone.utc)
        if now - actual < AUTO_MIN_AGE:
            raise Failure("automatic-release-too-young")


def verify_signature(manifest, signature):
    keyfd = safe_regular(PUBLIC_KEY, root_owned=True, max_size=4096); os.close(keyfd)
    result = subprocess.run(["/usr/bin/openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(PUBLIC_KEY),
                             "-sigfile", str(signature), "-rawin", "-in", str(manifest)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode:
        raise Failure("signature-invalid")


def verify_wheel(path, manifest):
    digest = hashlib.sha256(); size = 0
    fd = safe_regular(path, max_size=MAX_WHEEL)
    try:
        while True:
            part = os.read(fd, 1024 * 1024)
            if not part: break
            digest.update(part); size += len(part)
    finally:
        os.close(fd)
    if size != manifest["wheel_size"] or digest.hexdigest() != manifest["wheel_sha256"]:
        raise Failure("wheel-hash")
    dist = f"open_dachs_manager-{manifest['version']}.dist-info"; record = f"{dist}/RECORD"; metadata = f"{dist}/METADATA"
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or record not in names or metadata not in names:
                raise Failure("wheel-metadata")
            if any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or "\\" in name for name in names):
                raise Failure("wheel-path")
            meta = archive.read(metadata).decode("utf-8")
            if "Name: open-dachs-manager\n" not in meta or f"Version: {manifest['version']}\n" not in meta:
                raise Failure("wheel-version")
            seen = set()
            for row in archive.read(record).decode("utf-8").splitlines():
                fields = row.split(",")
                if len(fields) != 3 or fields[0] in seen: raise Failure("wheel-record")
                seen.add(fields[0])
                if fields[0] == record:
                    if fields[1] or fields[2]: raise Failure("wheel-record-self")
                    continue
                if fields[0] not in names or not fields[1].startswith("sha256=") or not fields[2].isdigit(): raise Failure("wheel-record-row")
                raw = archive.read(fields[0]); encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
                if encoded != fields[1][7:] or len(raw) != int(fields[2]): raise Failure("wheel-record-hash")
            if seen != set(names): raise Failure("wheel-unrecorded")
    except (zipfile.BadZipFile, UnicodeError, KeyError) as exc:
        raise Failure("wheel-invalid") from exc


def run_checked(command, timeout=300, error_class="command-failed"):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(error_class) from exc
    if result.returncode:
        raise Failure(error_class)


def validate_release_tree(root):
    root = Path(root)
    release_base = RELEASES.resolve()
    if root.parent.resolve() != release_base or not root.is_dir():
        raise Failure("release-outside-root")
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in ["."] + dirs + files:
            path = Path(current) if name == "." else Path(current) / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve(strict=True); target_info = os.stat(target, follow_symlinks=False)
                if not (target == root or root in target.parents or target == Path("/usr") or Path("/usr") in target.parents):
                    raise Failure("release-symlink-escape")
                if target_info.st_uid != 0 or target_info.st_mode & 0o022:
                    raise Failure("release-symlink-escape")
            elif info.st_uid != 0 or info.st_mode & 0o022:
                raise Failure("release-writable-child")
            elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise Failure("release-special-file")
    return root


def current_release():
    info = os.lstat(CURRENT)
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
        raise Failure("current-not-root-symlink")
    target = Path(os.readlink(CURRENT))
    if not target.is_absolute(): target = CURRENT.parent / target
    return validate_release_tree(target.resolve(strict=True))


def installed_version(release):
    candidates = list(Path(release).glob("venv/lib/python*/site-packages/open_dachs_manager-*.dist-info/METADATA"))
    if len(candidates) != 1:
        raise Failure("installed-version-metadata")
    fd = safe_regular(candidates[0], root_owned=True, max_size=1024 * 1024)
    try: text = read_fd(fd, 1024 * 1024).decode("utf-8")
    finally: os.close(fd)
    matches = re.findall(r"^Version: (\S+)$", text, re.MULTILINE)
    if len(matches) != 1 or not VERSION.fullmatch(matches[0]):
        raise Failure("installed-version-metadata")
    return matches[0]


def tree_size(path, limit=16 * 1024 * 1024 * 1024):
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            info = os.lstat(Path(root) / name)
            if stat.S_ISREG(info.st_mode): total += info.st_size
            if total > limit: raise Failure("tree-size-limit")
    return total


def disk_gates(manifest, previous):
    release_need = tree_size(previous) + manifest["wheel_size"] * 4 + RESERVE_BYTES
    database_size = os.stat(DATABASE, follow_symlinks=False).st_size
    mutable_size = sum((os.stat(PRODUCT_DATA / name, follow_symlinks=False).st_size
                        for name in MUTABLE_FILES if (PRODUCT_DATA / name).is_file()), 0)
    mutable_size += sum(
        tree_size(PRODUCT_DATA / name, limit=512 * 1024 * 1024)
        for name in MUTABLE_DIRS if (PRODUCT_DATA / name).is_dir()
    )
    backup_need = database_size + mutable_size + max(256 * 1024 * 1024, database_size // 10) + RESERVE_BYTES
    if shutil.disk_usage(RELEASES).free < release_need:
        raise Failure("insufficient-release-filesystem")
    if shutil.disk_usage(BACKUPS).free < backup_need:
        raise Failure("insufficient-backup-filesystem")
    return {"release_required": release_need, "backup_required": backup_need}


def sqlite_backup(request_id):
    ensure_root_dir(BACKUPS)
    target = BACKUPS / f"{request_id}.db"
    if target.exists():
        fd = safe_regular(target, root_owned=True); os.close(fd)
    else:
        reserve = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(reserve)
        source = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=30)
        destination = sqlite3.connect(target)
        try: source.backup(destination); destination.commit()
        finally: source.close(); destination.close()
        os.chown(target, 0, 0); os.chmod(target, 0o600); fsync_dir(BACKUPS)
    check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok": raise Failure("backup-quick-check")
    finally: check.close()
    return target


def validate_existing_snapshot_tree(root):
    ensure_root_dir(root)
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            info = os.lstat(Path(current) / name)
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise Failure("unsafe-existing-mutable-snapshot")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise Failure("unsafe-existing-mutable-snapshot")


def snapshot_mutable(request_id):
    root = BACKUPS / f"{request_id}-files"
    if root.exists():
        # A crash may leave an apparently root-owned but incomplete tree.
        # Never infer completeness from directory existence; validate that no
        # hostile/special entries appeared, then rebuild the snapshot fully.
        validate_existing_snapshot_tree(root)
        shutil.rmtree(root); fsync_dir(root.parent)
    os.mkdir(root, 0o700); os.chown(root, 0, 0); fsync_dir(root.parent)
    service_uid = pwd.getpwnam("open-dachs").pw_uid
    total = 0; maximum = 512 * 1024 * 1024
    for name in MUTABLE_FILES:
        source = PRODUCT_DATA / name
        if not source.exists(): continue
        fd = safe_regular(source, max_size=64 * 1024 * 1024)
        try: raw = read_fd(fd, 64 * 1024 * 1024)
        finally: os.close(fd)
        total += len(raw)
        if total > maximum: raise Failure("mutable-snapshot-too-large")
        root_copy(source, root / name, 64 * 1024 * 1024)
    for name in MUTABLE_DIRS:
        source = PRODUCT_DATA / name
        if not source.exists(): continue
        verify_parent_chain(source, service_leaf=True)
        destination = root / name; os.mkdir(destination, 0o700); os.chown(destination, 0, 0)
        for current, dirs, files in os.walk(source, followlinks=False):
            for dirname in dirs:
                directory_info = os.lstat(Path(current) / dirname)
                if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid not in {0, service_uid}:
                    raise Failure("unsafe-mutable-directory")
            relative = Path(current).relative_to(source); target_dir = destination / relative
            if relative != Path("."):
                os.mkdir(target_dir, 0o700); os.chown(target_dir, 0, 0)
            for filename in files:
                item = Path(current) / filename
                info = os.lstat(item)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, service_uid}:
                    raise Failure("unsafe-mutable-file")
                total += info.st_size
                if total > maximum: raise Failure("mutable-snapshot-too-large")
                root_copy(item, target_dir / filename, min(64 * 1024 * 1024, maximum))
    write_json(root / "SNAPSHOT.json", {"schema": "open-dachs-mutable-snapshot/v1", "request_id": request_id,
               "db_schema": DB_SCHEMA, "database_restore_on_same_schema_rollback": False,
               "captured": sorted([p.name for p in root.iterdir()])})
    return root


def configured_runtime():
    port, base, serial_port = "8084", "", "/dev/ttyUSB0"
    if not CONFIG.exists():
        return port, base, serial_port
    for line in CONFIG.read_text("utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key == "OPEN_DACHS_WEB_PORT" and value.isdigit() and 1 <= int(value) <= 65535:
            port = value
        if sep and key == "OPEN_DACHS_BASE_PATH" and re.fullmatch(r"(?:/[A-Za-z0-9._~/-]+)?", value):
            base = value.rstrip("/")
        if sep and key == "OPEN_DACHS_SERIAL_PORT":
            if not re.fullmatch(r"/dev/(?:ttyUSB[0-9]+|serial/by-id/[A-Za-z0-9._:+-]+)", value):
                raise Failure("serial-config-invalid")
            serial_port = value
    return port, base, serial_port


def verify_serial_owner(serial_port):
    if not os.path.exists(serial_port):
        raise Failure("serial-device-missing")
    real_port = os.path.realpath(serial_port)
    try:
        device_info = os.stat(real_port, follow_symlinks=False)
    except OSError as exc:
        raise Failure("serial-device-missing") from exc
    if not re.fullmatch(r"/dev/ttyUSB[0-9]+", real_port) or not stat.S_ISCHR(device_info.st_mode):
        raise Failure("serial-device-invalid")
    main_pid = subprocess.run(
        ["/usr/bin/systemctl", "show", "--property=MainPID", "--value", SERVICES[0]],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=5, text=True,
    )
    owners = subprocess.run(
        ["/usr/bin/fuser", real_port],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=5, text=True,
    )
    expected = main_pid.stdout.strip()
    actual = {item for item in owners.stdout.split() if item.isdigit()}
    if main_pid.returncode or owners.returncode or not expected.isdigit() or actual != {expected}:
        raise Failure("serial-owner-mismatch")


def health(version, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for unit in SERVICES:
                run_checked(["/usr/bin/systemctl", "is-active", "--quiet", unit], timeout=5, error_class="unit-inactive")
            info = os.stat(SERIAL_SOCKET, follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode): raise Failure("serial-socket-missing")
            port, base, serial_port = configured_runtime()
            verify_serial_owner(serial_port)
            result = subprocess.run(["/usr/bin/curl", "--silent", "--show-error", "--fail", "--max-time", "2",
                                     "--max-redirs", "0", "--proto", "=http", f"http://127.0.0.1:{port}{base}/healthz"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4)
            if result.returncode: raise Failure("web-health-unavailable")
            value = json.loads(result.stdout.decode("utf-8"))
            if value.get("ok") is not True or value.get("version") != version: raise Failure("web-version-mismatch")
            # The consistent pre-switch backup already passed a full quick_check.
            # Keep the outage gate bounded: prove the activated database opens,
            # has a schema and can read its catalog. A second full scan of the
            # multi-gigabyte live DB would turn a short restart into minutes.
            database = subprocess.run(["/usr/bin/python3", "-I", "-c",
                "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True,timeout=5); c.execute('PRAGMA query_only=ON'); s=c.execute('PRAGMA schema_version').fetchone(); n=c.execute('SELECT COUNT(*) FROM sqlite_master').fetchone(); c.close(); raise SystemExit(0 if s and n and n[0]>0 else 1)", str(DATABASE)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if database.returncode: raise Failure("database-open-check")
            return
        except (Failure, OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
            time.sleep(1)
    raise Failure("health-gate-failed")


def switch_link(target):
    temporary = CURRENT.with_name(".current.update")
    with contextlib.suppress(FileNotFoundError):
        info = os.lstat(temporary)
        if stat.S_ISLNK(info.st_mode): os.unlink(temporary)
        else: raise Failure("unsafe-temporary-link")
    os.symlink(str(target), temporary)
    os.replace(temporary, CURRENT)
    fsync_dir(CURRENT.parent)


def fsync_tree(root):
    """Durably flush a fully validated candidate before the current-link switch."""
    directories = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current); directories.append(current_path)
        for name in files:
            path = current_path / name; info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise Failure("candidate-special-file")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in reversed(directories):
        fsync_dir(directory)


def identity(evidence):
    return {key: evidence.get(key) for key in ("request_id", "version", "automatic", "manifest_sha256", "wheel_sha256")}


def transition(evidence, evidence_path, phase, **changes):
    evidence.update(changes); evidence["phase"] = phase
    write_json(STATE, evidence); write_json(evidence_path, evidence); publish(evidence)


def stop_services():
    run_checked(["/usr/bin/systemctl", "stop", SERVICES[1], SERVICES[0]], timeout=60, error_class="service-stop-failed")


def start_services(restart=False):
    verb = "restart" if restart else "start"
    run_checked(["/usr/bin/systemctl", verb, SERVICES[0], SERVICES[1]], timeout=60,
                error_class="service-restart-failed" if restart else "service-start-failed")


def rollback(evidence, evidence_path, previous, original):
    try:
        switch_link(previous); start_services(restart=True); health(installed_version(previous))
    except Exception as rollback_exc:
        error_class = rollback_exc.error_class if isinstance(rollback_exc, Failure) else "rollback-unknown"
        transition(evidence, evidence_path, "recovery-required", status="recovery-required",
                   original_error_class=original.error_class, rollback_error_class=error_class,
                   finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        return False
    transition(evidence, evidence_path, "complete", status="rolled-back",
               original_error_class=original.error_class,
               finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
    return True


def prune(previous, current):
    keep = {previous.resolve(), current.resolve()}
    entries = []
    for path in RELEASES.iterdir():
        info = os.lstat(path)
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and path.resolve() not in keep:
            entries.append(path)
    for old in sorted(entries, key=lambda p: os.stat(p, follow_symlinks=False).st_mtime, reverse=True)[2:]:
        validate_release_tree(old); shutil.rmtree(old)
    protected_ids = set()
    evidence_files = sorted(EVIDENCE.glob("*.json"), key=lambda p: os.stat(p, follow_symlinks=False).st_mtime, reverse=True)
    for index, path in enumerate(evidence_files):
        with contextlib.suppress(Exception):
            item = load_json_regular(path, root_owned=True)
            if index == 0 or item.get("status") in {"recovery-required", "rollback-failed"}:
                protected_ids.add(str(item.get("request_id") or ""))
    db_backups = []
    for path in BACKUPS.glob("*.db"):
        fd = safe_regular(path, root_owned=True); info = os.fstat(fd); os.close(fd); db_backups.append((info.st_mtime, path))
    removable = [item for item in sorted(db_backups, reverse=True) if item[1].stem not in protected_ids]
    for _, old in removable[4:]: old.unlink()
    fsync_dir(BACKUPS)


def recover_if_needed(prior, evidence, evidence_path, previous, candidate, manifest):
    if not prior or prior.get("status") != "running": return False
    if identity(prior) != identity(evidence) or prior.get("previous") != previous.name or prior.get("candidate") != candidate.name:
        transition(evidence, evidence_path, "recovery-required", status="recovery-required",
                   error_class="evidence-binding-mismatch", finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        return True
    current = current_release()
    if current == candidate:
        try:
            start_services(); health(manifest["version"])
            transition(evidence, evidence_path, "complete", status="succeeded", recovered=True,
                       current=candidate.name, finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        except Failure as exc:
            rollback(evidence, evidence_path, previous, exc)
        return True
    if current == previous and prior.get("phase") in {"stopping", "stopped", "switching"}:
        try:
            start_services(); health(installed_version(previous))
        except Failure as exc:
            transition(evidence, evidence_path, "recovery-required", status="recovery-required",
                       error_class="resume-previous-failed", rollback_error_class=exc.error_class,
                       finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
            return True
        return False
    if current != previous:
        transition(evidence, evidence_path, "recovery-required", status="recovery-required",
                   error_class="uncertain-current-release", finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        return True
    return False


def main():
    if len(sys.argv) != 1: raise Failure("arguments-forbidden")
    if os.geteuid() != 0: raise Failure("root-required")
    ensure_root_dir(UPDATER_ROOT); ensure_root_dir(EVIDENCE); ensure_root_dir(BACKUPS)
    ensure_root_dir(RUNTIME_ROOT); ensure_root_dir(CUSTODY)
    lockfd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    lockinfo = os.fstat(lockfd)
    if not stat.S_ISREG(lockinfo.st_mode) or lockinfo.st_uid != 0 or lockinfo.st_mode & 0o077:
        raise Failure("unsafe-lock")
    fcntl.flock(lockfd, fcntl.LOCK_EX)
    verify_parent_chain(STAGING, service_leaf=True)
    request = load_request(); request_id = request["request_id"]
    evidence_path = EVIDENCE / f"{request_id}.json"
    prior = load_json_regular(evidence_path, root_owned=True) if evidence_path.exists() else None
    base_identity = {"request_id": request_id, "version": request["version"], "automatic": request["automatic"],
                     "manifest_sha256": request["manifest_sha256"], "wheel_sha256": request["wheel_sha256"]}
    if prior and prior.get("status") in {"succeeded", "failed", "rolled-back"}:
        if identity(prior) != base_identity: raise Failure("terminal-evidence-binding")
        REQUEST.unlink(missing_ok=True); fsync_dir(REQUEST.parent); return 0
    if prior and prior.get("status") in {"recovery-required", "rollback-failed"}:
        if identity(prior) != base_identity: raise Failure("terminal-evidence-binding")
        publish(prior)
        return 1
    if prior and prior.get("status") == "running":
        if identity(prior) != base_identity:
            raise Failure("evidence-binding-mismatch")
        # Preserve previous/candidate and the exact crash phase until offline
        # reconciliation has completed. Overwriting it with "verify" would make
        # a second power loss during recovery unrecoverable.
        evidence = dict(prior)
    else:
        evidence = {"schema": EVIDENCE_SCHEMA, **base_identity,
                    "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "status": "running", "phase": "verify"}
        transition(evidence, evidence_path, "verify")
    stage = STAGING / request_id; verify_parent_chain(stage, service_leaf=True)
    stage_info = os.lstat(stage); service_uid = pwd.getpwnam("open-dachs").pw_uid
    if not stat.S_ISDIR(stage_info.st_mode) or stage_info.st_uid not in {service_uid, 0} or stage_info.st_mode & 0o077:
        raise Failure("unsafe-stage")
    staged_request = stage / "request.json"
    if os.stat(REQUEST, follow_symlinks=False).st_ino != os.stat(staged_request, follow_symlinks=False).st_ino:
        raise Failure("request-inode-mismatch")
    custody = CUSTODY / request_id
    if custody.exists():
        ensure_root_dir(custody)
    else:
        os.mkdir(custody, 0o700); os.chown(custody, 0, 0); fsync_dir(custody.parent)
    active_release = current_release(); previous = active_release
    # If power failed after the atomic link switch, CURRENT already names the
    # candidate. Recover against the root-owned evidence's previous release,
    # otherwise anti-downgrade validation would reject the same candidate and
    # make reconciliation unreachable.
    if prior and prior.get("status") == "running" and prior.get("phase") in {"switching", "switched", "starting", "verifying"}:
        if identity(prior) != base_identity:
            raise Failure("evidence-binding-mismatch")
        previous_name = str(prior.get("previous") or "")
        candidate_name = str(prior.get("candidate") or "")
        release_name = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-[a-f0-9]{16}$")
        if not release_name.fullmatch(previous_name) or not release_name.fullmatch(candidate_name):
            raise Failure("evidence-release-name")
        if active_release.name == candidate_name:
            previous = validate_release_tree(RELEASES / previous_name)
        elif active_release.name != previous_name:
            raise Failure("uncertain-current-release")
    current_version = installed_version(previous)
    manifest_path = custody / MANIFEST; signature_path = custody / SIGNATURE
    if manifest_path.exists(): raw = Path(manifest_path).read_bytes()
    else: raw = root_copy(stage / MANIFEST, manifest_path, 64 * 1024)
    if not signature_path.exists(): root_copy(stage / SIGNATURE, signature_path, 4096)
    manifest = validate_manifest(raw, request, current_version)
    wheel = custody / manifest["wheel"]
    if not wheel.exists(): root_copy(stage / manifest["wheel"], wheel, MAX_WHEEL)
    verify_signature(manifest_path, signature_path); verify_wheel(wheel, manifest)
    release = RELEASES / f"{manifest['version']}-{manifest['wheel_sha256'][:16]}"
    evidence.update({"previous": previous.name, "candidate": release.name})
    # Recovery after a crash at/after the service stop or link switch must be
    # entirely offline. The first execution had already passed the remote gate
    # before it could reach those phases; signed root custody and evidence are
    # sufficient to restore/start the recorded release deterministically.
    if recover_if_needed(prior, evidence, evidence_path, previous, release, manifest):
        return 0 if evidence.get("status") == "succeeded" else 1
    transition(evidence, evidence_path, "verified")
    validate_github_release(fetch_github_release(manifest["release_id"]), manifest, request)
    transition(evidence, evidence_path, "remote-verified")
    disk_gates(manifest, previous)
    transition(evidence, evidence_path, "backup")
    sqlite_backup(request_id); snapshot_mutable(request_id)
    transition(evidence, evidence_path, "backup-complete")
    # A crash may leave a root-owned but incomplete candidate tree. Never trust
    # presence alone: while the old release is still active, rebuild the exact
    # signed candidate from scratch and then smoke-test it.
    if release.exists():
        validate_release_tree(release); shutil.rmtree(release); fsync_dir(RELEASES)
    shutil.copytree(previous, release, symlinks=True)
    venv = release / "venv"
    run_checked([str(venv / "bin/python"), "-m", "pip", "install", "--no-index", "--no-deps", "--force-reinstall", str(wheel)], timeout=600, error_class="candidate-install-failed")
    run_checked([str(venv / "bin/python"), "-I", "-c", "import open_dachs_manager,open_dachs_manager.web; assert open_dachs_manager.__version__ == " + repr(manifest["version"])], timeout=30, error_class="candidate-import-failed")
    for root, dirs, files in os.walk(release, followlinks=False):
        os.chown(root, 0, 0, follow_symlinks=False); os.chmod(root, 0o755, follow_symlinks=False)
        for name in files:
            path = Path(root) / name; info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode): continue
            os.chown(path, 0, 0, follow_symlinks=False)
            os.chmod(path, 0o755 if info.st_mode & 0o111 else 0o644, follow_symlinks=False)
    validate_release_tree(release); fsync_tree(release); fsync_dir(RELEASES)
    transition(evidence, evidence_path, "candidate-ready")
    switched = False; stop_attempted = False
    try:
        transition(evidence, evidence_path, "stopping")
        stop_attempted = True
        stop_services()
        transition(evidence, evidence_path, "stopped")
        transition(evidence, evidence_path, "switching")
        switch_link(release); switched = True
        transition(evidence, evidence_path, "switched")
        transition(evidence, evidence_path, "starting")
        start_services()
        transition(evidence, evidence_path, "verifying")
        health(manifest["version"])
        transition(evidence, evidence_path, "complete", status="succeeded", current=release.name,
                   finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
    except Exception as raw_exc:
        exc = raw_exc if isinstance(raw_exc, Failure) else Failure("unexpected-switch-failure")
        if switched:
            rollback(evidence, evidence_path, previous, exc)
        elif stop_attempted:
            try:
                start_services(); health(current_version)
                transition(evidence, evidence_path, "complete", status="failed", error_class=exc.error_class,
                           finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
            except Failure as recovery:
                transition(evidence, evidence_path, "recovery-required", status="recovery-required",
                           original_error_class=exc.error_class, rollback_error_class=recovery.error_class,
                           finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        else:
            transition(evidence, evidence_path, "complete", status="failed", error_class=exc.error_class,
                       finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        if evidence["status"] != "recovery-required":
            REQUEST.unlink(missing_ok=True); fsync_dir(REQUEST.parent)
        return 1
    REQUEST.unlink(missing_ok=True); fsync_dir(REQUEST.parent); prune(previous, release); return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as raw_exc:
        exc = raw_exc if isinstance(raw_exc, Failure) else Failure("unexpected-helper-failure")
        failed = {"schema": EVIDENCE_SCHEMA, "status": "failed", "phase": "startup",
                  "error_class": exc.error_class, "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        preserve_request = False
        request = None
        with contextlib.suppress(Exception):
            request = load_request(); failed.update({"request_id": request["request_id"], "version": request["version"], "automatic": request["automatic"]})
            existing_path = EVIDENCE / (str(request["request_id"]) + ".json")
            if existing_path.exists():
                existing = load_json_regular(existing_path, root_owned=True)
                if existing.get("status") == "recovery-required" or (
                    existing.get("status") == "running"
                    and existing.get("phase") in {"switching", "switched", "starting", "verifying"}
                ):
                    preserve_request = True
                    existing.update({"status": "recovery-required", "phase": "recovery-required",
                                     "error_class": exc.error_class,
                                     "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()})
                    failed = existing
        with contextlib.suppress(Exception):
            ensure_root_dir(UPDATER_ROOT); ensure_root_dir(EVIDENCE)
            write_json(STATE, failed); publish(failed)
            if failed.get("request_id"):
                write_json(EVIDENCE / (str(failed["request_id"]) + ".json"), failed)
        if not preserve_request:
            with contextlib.suppress(Exception):
                REQUEST.unlink(missing_ok=True); fsync_dir(REQUEST.parent)
        print("open-dachs-update-helper: failed", file=sys.stderr)
        raise SystemExit(1)
