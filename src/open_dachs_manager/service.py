"""Shared read, backup and guarded write services for CLI and TUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .auth import AuthInputs, AuthResult, authenticate, read_auth_inputs
from .mapping import PackRepository, WriteAllowlist
from .serial_worker import SerialWorkerSession
from .transport import BlockResult, SerialSession


@dataclass(frozen=True)
class WriteAudit:
    key: str
    block: int
    before_hex: str
    after_hex: str
    dry_run: bool
    written: bool
    readback_ok: bool | None
    timestamp_utc: str
    error: str | None = None
    changed_keys: tuple[str, ...] = ()
    ack_positive: bool | None = None
    cpu: int = 0

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "block": self.block,
            "cpu": self.cpu,
            "before_hex": self.before_hex,
            "after_hex": self.after_hex,
            "dry_run": self.dry_run,
            "written": self.written,
            "readback_ok": self.readback_ok,
            "timestamp_utc": self.timestamp_utc,
            "error": self.error,
            "changed_keys": list(self.changed_keys or ((self.key,) if self.key else ())),
            "changed": self.before_hex != self.after_hex,
            "ack_positive": self.ack_positive,
        }


class DachsService:
    def __init__(self, port: str, baud: int, timeout: float, pack: PackRepository,
                 serial_socket: str | Path | None = None, queue_timeout: float = 120.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.pack = pack
        self.serial_socket = str(serial_socket or "")
        self.queue_timeout = float(queue_timeout)

    def session(self) -> SerialSession | SerialWorkerSession:
        if self.serial_socket:
            return SerialWorkerSession(self.serial_socket, self.queue_timeout)
        return SerialSession(self.port, self.baud)

    def read_block(self, session: SerialSession, block: int, cpu: int = 0) -> BlockResult:
        if cpu:
            return session.read_block(block, packet=None, timeout=self.timeout, cpu=cpu)
        return session.read_block(block, packet=None, timeout=self.timeout)

    def decoded_block(self, session: SerialSession, block: int, cpu: int = 0) -> tuple[BlockResult, list]:
        result = self.read_block(session, block, cpu=cpu)
        return result, self.pack.display_fields(block, result.payload) if result.ok else []

    def authenticate(self, session: SerialSession, level: int, pass4: str | None = None) -> AuthResult:
        return authenticate(session, self.pack, level, pass4, self.timeout)

    def authentication_inputs(self, session: SerialSession) -> AuthInputs:
        return read_auth_inputs(session, self.pack, self.timeout)

    def write_payload(
        self,
        session: SerialSession,
        block: int,
        before: bytes,
        after: bytes,
        changed_keys: list[str],
        allowlist: WriteAllowlist,
        dry_run: bool,
        cpu: int = 0,
    ) -> WriteAudit:
        now = datetime.now(timezone.utc).isoformat()
        if len(before) != len(after):
            return WriteAudit(
                changed_keys[0] if changed_keys else "",
                block,
                before.hex(" ").upper(),
                after.hex(" ").upper(),
                dry_run,
                False,
                None,
                now,
                "payload length changed",
                tuple(changed_keys),
                cpu=cpu,
            )
        denied = [key for key in changed_keys if not allowlist.allows(key)]
        if denied:
            return WriteAudit(
                changed_keys[0] if changed_keys else "",
                block,
                before.hex(" ").upper(),
                after.hex(" ").upper(),
                dry_run,
                False,
                None,
                now,
                "not allowlisted: " + ", ".join(denied),
                tuple(changed_keys),
                cpu=cpu,
            )
        if dry_run:
            return WriteAudit(
                changed_keys[0] if changed_keys else "",
                block,
                before.hex(" ").upper(),
                after.hex(" ").upper(),
                True,
                False,
                None,
                now,
                None,
                tuple(changed_keys),
                cpu=cpu,
            )
        try:
            current = self.read_block(session, block, cpu=cpu)
            if not current.ok or current.payload != before:
                raise RuntimeError("block changed since it was loaded; reload before writing")
            if cpu:
                response = session.write_block(
                    block, after, packet=None, timeout=self.timeout, cpu=cpu
                )
            else:
                response = session.write_block(block, after, packet=None, timeout=self.timeout)
            if response.ack is None or not response.ack.positive:
                raise RuntimeError("write did not receive a positive ACK")
            readback = self.read_block(session, block, cpu=cpu)
            ok = readback.ok and readback.payload == after
            if not ok:
                raise RuntimeError("readback mismatch")
            return WriteAudit(
                changed_keys[0] if changed_keys else "",
                block,
                before.hex(" ").upper(),
                after.hex(" ").upper(),
                False,
                True,
                True,
                now,
                None,
                tuple(changed_keys),
                True,
                cpu,
            )
        except Exception as exc:
            return WriteAudit(
                changed_keys[0] if changed_keys else "",
                block,
                before.hex(" ").upper(),
                after.hex(" ").upper(),
                False,
                False,
                False,
                now,
                str(exc),
                tuple(changed_keys),
                cpu=cpu,
            )

    def backup(self, session: SerialSession, blocks: list[int], decode: bool = True) -> dict:
        records = []
        for block in blocks:
            try:
                result = self.read_block(session, block)
            except Exception as exc:
                records.append({"block": block, "ok": False, "error": str(exc)})
                continue
            record = {
                "block": block,
                "ok": result.ok,
                "status": result.status,
                "payload_hex": result.payload.hex().upper(),
                "payload_len": len(result.payload),
                "rtt_ms": round(result.response.elapsed_ms, 1),
                "crc_errors": result.response.crc_errors,
                "protocol_errors": result.response.protocol_errors,
            }
            if decode and result.ok:
                record["values"] = [
                    {"key": item.key, "label": item.label, "raw": item.raw, "value": item.value, "unit": item.unit}
                    for item in self.pack.display_fields(block, result.payload)
                ]
            records.append(record)
        return {
            "schema": "dachs-msr2-backup/v3",
            "schema_version": 3,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "port": self.port,
            "baud": self.baud,
            "requested_blocks": len(blocks),
            "successful_blocks": sum(bool(item.get("ok")) for item in records),
            "failed_blocks": sum(not bool(item.get("ok")) for item in records),
            "blocks": records,
        }


def write_json_atomic(path: str | Path, data: dict) -> None:
    import os
    import tempfile

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with open(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
