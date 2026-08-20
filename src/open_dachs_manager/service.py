"""Shared read, backup and guarded write services for CLI and TUI."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import time

from . import __version__
from .auth import (
    AuthInputs,
    AuthResult,
    auth_inputs_from_payloads,
    authenticate,
    read_auth_inputs,
)
from .mapping import PackRepository, WriteAllowlist
from .network_protection import (
    NETWORK_PROTECTION_BACKUP_BLOCKS,
    NETWORK_PROTECTION_BLOCK,
    NETWORK_PROTECTION_CPUS,
    NETWORK_PROTECTION_PAYLOAD_LENGTH,
    NETWORK_PROTECTION_RESTORE_BLOCKS,
    decode_network_protection,
    network_protection_name,
    network_protection_payload_length,
)
from .serial_worker import SerialWorkerSession
from .transport import BlockResult, SerialSession, validate_block


BACKUP_SCHEMA = "dachs-msr2-backup/v3"
BACKUP_SCHEMA_VERSION = 3
BACKUP_PRODUCT_NAME = "Open Dachs Manager"
BACKUP_PACK_NAME = "MSR2 Dachs Runtime Pack"
MAX_RESTORE_PAYLOAD_LENGTH = 4094
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_PREWRITE_VOLATILE_FIELDS = {
    (0, 50): (70, (("Hka_Ew.ulSystemTime", 36, 4),)),
}

# One reviewed capture scope for browser backups and maintenance archives.
# Network blocks 20/21 are preserved and verified, but remain outside the
# narrower restore scope: B20 has no physical restore acceptance test and B21
# is live telemetry without a write service.
BACKUP_CPU0_BLOCKS = (
    18, 20, 22, 24, 26, 28, 30, 31, 32, 34, 36, 38, 46, 50, 52, 54, 56,
    60, 62, 66, 70, 76, 80, 82, 84, 86, 88, 90, 92, 94, 100, 102, 104,
    110, 112, 114,
)
BACKUP_LEGACY_TARGETS = tuple((0, block) for block in BACKUP_CPU0_BLOCKS) + (
    (1, NETWORK_PROTECTION_BLOCK),
    (2, NETWORK_PROTECTION_BLOCK),
)
BACKUP_RESTORE_TARGETS = tuple((0, block) for block in BACKUP_CPU0_BLOCKS) + tuple(
    (cpu, block)
    for cpu in NETWORK_PROTECTION_CPUS
    for block in NETWORK_PROTECTION_RESTORE_BLOCKS
)
BACKUP_TARGETS = tuple((0, block) for block in BACKUP_CPU0_BLOCKS) + tuple(
    (cpu, block)
    for cpu in NETWORK_PROTECTION_CPUS
    for block in NETWORK_PROTECTION_BACKUP_BLOCKS
)
BACKUP_PAYLOAD_LENGTHS = {
    target: (
        network_protection_payload_length(target[1])
        if target[0]
        else {31: 14, 36: 30, 38: 2, 46: 10}.get(target[1], 70)
    )
    for target in BACKUP_TARGETS
}
if len(BACKUP_TARGETS) != 42 or len(set(BACKUP_TARGETS)) != 42:  # pragma: no cover
    raise RuntimeError("the reviewed backup capture set must contain exactly 42 targets")
if len(BACKUP_RESTORE_TARGETS) != 38:  # pragma: no cover
    raise RuntimeError("the reviewed backup restore set must contain exactly 38 targets")
if BACKUP_RESTORE_TARGETS != BACKUP_LEGACY_TARGETS:  # pragma: no cover
    raise RuntimeError("network restore eligibility drifted from the legacy 38-target contract")


def _canonical_json(data: dict) -> bytes:
    """Encode a backup deterministically for its image-level digest."""
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest_text(value: object) -> object:
    return " ".join(value.split()).strip() if isinstance(value, str) else value


def _image_digest_data(image: dict) -> dict:
    """Select and normalize only data that determines restore semantics.

    Decoded display values and transport timings deliberately stay outside the
    digest.  In particular, a browser JSON roundtrip may legally turn ``0.0``
    into ``0`` without changing the raw payload that would be restored.
    """
    raw_pack = image.get("pack")
    pack = None
    if isinstance(raw_pack, dict):
        pack = {
            "name": _digest_text(raw_pack.get("name")),
            "schema": _digest_text(raw_pack.get("schema")),
            "revision": _digest_text(raw_pack.get("revision")),
        }
    elif raw_pack is not None:
        pack = raw_pack

    raw_controller = image.get("controller")
    controller = None
    if isinstance(raw_controller, dict):
        controller = {"available": raw_controller.get("available")}
        if raw_controller.get("available") is True:
            controller.update({
                "serial_number": _digest_text(raw_controller.get("serial_number")),
                "operating_hours": raw_controller.get("operating_hours"),
            })
        elif raw_controller.get("error") is not None:
            controller["error"] = _digest_text(raw_controller.get("error"))
    elif raw_controller is not None:
        controller = raw_controller

    raw_records = image.get("blocks")
    target_format = bool(
        "requested_targets" in image
        or isinstance(raw_records, list)
        and any(isinstance(record, dict) and "cpu" in record for record in raw_records)
    )
    records = []
    derived_requested_blocks = []
    derived_requested_targets = []
    if isinstance(raw_records, list):
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                records.append(raw_record)
                derived_requested_blocks.append(None)
                derived_requested_targets.append(None)
                continue
            block = raw_record.get("block")
            cpu = raw_record.get("cpu") if target_format else None
            derived_requested_blocks.append(block)
            derived_requested_targets.append({"cpu": cpu, "block": block})
            payload_digest = raw_record.get("payload_sha256")
            if isinstance(payload_digest, str):
                payload_digest = payload_digest.lower()
            record = {
                "block": block,
                "ok": raw_record.get("ok"),
                "status": raw_record.get("status"),
                "payload_len": raw_record.get("payload_len"),
                "payload_sha256": payload_digest,
                "error": _digest_text(raw_record.get("error")),
            }
            if target_format:
                record["cpu"] = cpu
            records.append(record)
    else:
        records = raw_records

    semantic = {
        "schema": image.get("schema"),
        "schema_version": image.get("schema_version"),
        "pack": pack,
        "controller": controller,
        "records": records,
    }
    if "maintenance_archive" in image:
        raw_context = image.get("maintenance_archive")
        semantic["maintenance_archive"] = (
            {
                "version": raw_context.get("version"),
                "source": _digest_text(raw_context.get("source")),
                "created_by": _digest_text(raw_context.get("created_by")),
            }
            if isinstance(raw_context, dict)
            else raw_context
        )
    if target_format:
        # Target-aware images were introduced after the original CPU-0-only
        # digest contract.  Their capture time is therefore safe to bind,
        # while the legacy branch below must remain byte-for-byte compatible
        # with already-created 1.2.0 images.
        semantic["created_utc"] = image.get("created_utc")
        semantic["requested_targets"] = image.get(
            "requested_targets", derived_requested_targets
        )
    else:
        # Preserve the exact 1.2.0 digest contract for already-created CPU-0
        # images.  New target-aware images use the branch above, where CPU is
        # part of every record and of the requested target list.
        semantic["requested_blocks"] = image.get(
            "requested_block_ids", derived_requested_blocks
        )
    return semantic


def _image_sha256(image: dict) -> str:
    return hashlib.sha256(_canonical_json(_image_digest_data(image))).hexdigest()


def _short_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    clean = " ".join(value.split()).strip()
    if not clean or len(clean) > maximum:
        raise ValueError(f"{field} must contain 1..{maximum} characters")
    return clean


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
    readback_scope: str | None = None
    readback_attempts: int = 0
    write_attempted: bool = False
    prewrite_scope: str | None = None
    rebased_volatile_keys: tuple[str, ...] = ()

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
            "readback_scope": self.readback_scope,
            "readback_attempts": self.readback_attempts,
            "write_attempted": self.write_attempted,
            "prewrite_scope": self.prewrite_scope,
            "rebased_volatile_keys": list(self.rebased_volatile_keys),
        }


class DachsService:
    def __init__(self, port: str, baud: int, timeout: float, pack: PackRepository,
                 serial_socket: str | Path | None = None, queue_timeout: float = 120.0,
                 readback_attempts: int = 4, readback_delay: float = 0.2):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.pack = pack
        self.serial_socket = str(serial_socket or "")
        self.queue_timeout = float(queue_timeout)
        self.readback_attempts = max(1, int(readback_attempts))
        self.readback_delay = max(0.0, float(readback_delay))

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

    def _normalize_backup_target(self, value: object) -> tuple[int, int]:
        """Return a strictly validated ``(cpu, block)`` backup target.

        Integer values retain the original v3 meaning (regulator CPU 0).
        Target objects additionally expose the reviewed block 16, 20 and 21
        capture addresses on the two network-monitor CPUs.
        """
        if isinstance(value, bool):
            raise ValueError("backup target must be an integer or an object")
        if isinstance(value, int):
            cpu = 0
            raw_block = value
        elif isinstance(value, dict):
            raw_cpu = value.get("cpu")
            raw_block = value.get("block")
            if isinstance(raw_cpu, bool) or not isinstance(raw_cpu, int):
                raise ValueError("backup target CPU must be an integer")
            if isinstance(raw_block, bool) or not isinstance(raw_block, int):
                raise ValueError("backup target block must be an integer")
            cpu = raw_cpu
        else:
            raise ValueError("backup target must be an integer or an object")

        block = validate_block(raw_block, writable=True)
        if cpu == 0:
            if block not in self.pack.addressable_blocks():
                raise ValueError(f"block {block} is not mapped and writable on CPU 0")
            return cpu, block
        if cpu not in NETWORK_PROTECTION_CPUS:
            raise ValueError(f"backup target CPU must be 0, 1 or 2, got {cpu}")
        if block not in NETWORK_PROTECTION_BACKUP_BLOCKS:
            raise ValueError(
                f"CPU {cpu} exposes only reviewed network blocks "
                f"{', '.join(str(item) for item in NETWORK_PROTECTION_BACKUP_BLOCKS)}"
            )
        return cpu, block

    def _backup_target_name(self, cpu: int, block: int) -> str:
        if cpu:
            return network_protection_name(cpu, block)
        return self.pack.block_name(block)

    def _field_masks(
        self,
        block: int,
        keys: list[str] | tuple[str, ...],
        payload_length: int,
    ) -> list[int] | None:
        """Return one bit mask per payload byte for mapped fields."""
        if payload_length < 0 or not keys:
            return None
        fields = self.pack.field_map(block)
        masks = [0] * payload_length
        for key in keys:
            metadata = fields.get(key)
            if metadata is None:
                return None
            offset = int(metadata["offset"])
            size = int(metadata["size"])
            if offset < 0 or size < 1 or offset + size > payload_length:
                return None
            if metadata.get("packed"):
                bit_offset = int(metadata["bit_offset"])
                bit_length = int(metadata["bit_length"])
                if size != 1 or bit_offset < 0 or bit_length < 1 or bit_offset + bit_length > 8:
                    return None
                masks[offset] |= ((1 << bit_length) - 1) << bit_offset
            else:
                for index in range(offset, offset + size):
                    masks[index] = 0xFF
        return masks

    def _changed_fields_match(
        self,
        block: int,
        expected: bytes,
        actual: bytes,
        changed_keys: list[str],
    ) -> bool:
        """Verify only the mapped bits/bytes that the write was meant to change.

        Some controller blocks contain live counters beside settings.  Requiring
        the complete block to remain byte-identical after a write therefore
        creates false failures even when the requested field was persisted.
        """
        if len(expected) != len(actual):
            return False
        masks = self._field_masks(block, changed_keys, len(expected))
        if masks is None:
            return False
        return all(
            ((expected[index] ^ actual[index]) & mask) == 0
            for index, mask in enumerate(masks)
        )

    def _rebase_known_volatile_fields(
        self,
        block: int,
        cpu: int,
        before: bytes,
        after: bytes,
        current: bytes,
        changed_keys: list[str],
    ) -> tuple[bytes, bytes, tuple[str, ...]] | None:
        """Rebase declared changes only when every concurrent change is known volatile."""
        if len(before) != len(after) or len(before) != len(current):
            return None
        volatile_spec = _PREWRITE_VOLATILE_FIELDS.get((int(cpu), int(block)))
        if volatile_spec is None:
            return None
        expected_length, field_specs = volatile_spec
        if len(before) != expected_length:
            return None
        fields = self.pack.field_map(block)
        for key, expected_offset, expected_size in field_specs:
            metadata = fields.get(key)
            if (
                metadata is None
                or bool(metadata.get("packed"))
                or int(metadata.get("offset", -1)) != expected_offset
                or int(metadata.get("size", -1)) != expected_size
            ):
                return None
        changed_key_set = set(changed_keys)
        volatile_keys = tuple(
            key for key, _offset, _size in field_specs if key not in changed_key_set
        )
        volatile_masks = self._field_masks(block, volatile_keys, len(before))
        changed_masks = self._field_masks(block, changed_keys, len(before))
        if volatile_masks is None or changed_masks is None:
            return None

        # Target bits are never accepted as volatility, even if a future
        # packed layout happens to share their storage byte.
        volatile_masks = [
            volatile_mask & (~changed_mask & 0xFF)
            for volatile_mask, changed_mask in zip(volatile_masks, changed_masks)
        ]
        if any(
            ((before[index] ^ current[index]) & (~volatile_masks[index] & 0xFF)) != 0
            for index in range(len(before))
        ):
            return None
        # Never carry an undeclared encoder-side byte change into the wire
        # payload merely because a volatile controller field also moved.
        if any(
            ((before[index] ^ after[index]) & (~changed_masks[index] & 0xFF)) != 0
            for index in range(len(before))
        ):
            return None

        rebased = bytearray(current)
        for index, mask in enumerate(changed_masks):
            rebased[index] = (rebased[index] & (~mask & 0xFF)) | (after[index] & mask)
        return current, bytes(rebased), volatile_keys

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
        if before == after:
            return WriteAudit(
                changed_keys[0] if changed_keys else "",
                block,
                before.hex(" ").upper(),
                after.hex(" ").upper(),
                dry_run,
                False,
                True,
                now,
                None,
                tuple(changed_keys),
                None,
                cpu,
                "block",
                0,
                False,
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
        ack_positive: bool | None = None
        readback_attempts = 0
        write_attempted = False
        prewrite_scope: str | None = None
        rebased_volatile_keys: tuple[str, ...] = ()
        try:
            current = self.read_block(session, block, cpu=cpu)
            if not current.ok:
                raise RuntimeError("block changed since it was loaded; reload before writing")
            if current.payload == before:
                prewrite_scope = "block"
            else:
                rebased = self._rebase_known_volatile_fields(
                    block,
                    cpu,
                    before,
                    after,
                    bytes(current.payload),
                    changed_keys,
                )
                if rebased is None:
                    raise RuntimeError("block changed since it was loaded; reload before writing")
                before, after, rebased_volatile_keys = rebased
                prewrite_scope = "stable-fields"
            write_attempted = True
            if cpu:
                response = session.write_block(
                    block, after, packet=None, timeout=self.timeout, cpu=cpu
                )
            else:
                response = session.write_block(block, after, packet=None, timeout=self.timeout)
            ack_positive = bool(response.ack is not None and response.ack.positive)
            if not ack_positive:
                raise RuntimeError("write did not receive a positive ACK")
            readback_scope = None
            for attempt in range(1, self.readback_attempts + 1):
                readback_attempts = attempt
                readback = self.read_block(session, block, cpu=cpu)
                if readback.ok and readback.payload == after:
                    readback_scope = "block"
                    break
                if cpu == 0 and readback.ok and self._changed_fields_match(
                    block, after, readback.payload, changed_keys
                ):
                    readback_scope = "changed-fields"
                    break
                if attempt < self.readback_attempts and self.readback_delay:
                    time.sleep(self.readback_delay)
            if readback_scope is None:
                raise RuntimeError(
                    f"readback mismatch after {readback_attempts} attempts"
                )
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
                readback_scope,
                readback_attempts,
                True,
                prewrite_scope,
                rebased_volatile_keys,
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
                ack_positive,
                cpu=cpu,
                readback_attempts=readback_attempts,
                write_attempted=write_attempted,
                prewrite_scope=prewrite_scope,
                rebased_volatile_keys=rebased_volatile_keys,
            )

    def backup(
        self,
        session: SerialSession,
        blocks: list[int | dict],
        decode: bool = True,
        include_identity: bool = False,
    ) -> dict:
        legacy_cpu0_format = all(
            isinstance(item, int) and not isinstance(item, bool) for item in blocks
        )
        targets = [self._normalize_backup_target(item) for item in blocks]
        if len(set(targets)) != len(targets):
            raise ValueError("backup contains duplicate CPU/block targets")
        records = []
        for cpu, block in targets:
            block_name = self._backup_target_name(cpu, block)
            try:
                result = self.read_block(session, block, cpu=cpu)
            except Exception as exc:
                record = {
                    "block": block,
                    "block_name": block_name,
                    "ok": False,
                    "error": str(exc),
                }
                if not legacy_cpu0_format:
                    record.update({
                        "cpu": cpu,
                        "target_key": f"{cpu}:{block}",
                    })
                    if cpu:
                        record["critical"] = True
                records.append(record)
                continue
            payload = bytes(result.payload)
            record = {
                "block": block,
                "block_name": block_name,
                "ok": result.ok,
                "status": result.status,
                "payload_hex": payload.hex().upper(),
                "payload_len": len(payload),
                "payload_sha256": _payload_sha256(payload),
                "rtt_ms": round(result.response.elapsed_ms, 1),
                "crc_errors": result.response.crc_errors,
                "protocol_errors": result.response.protocol_errors,
            }
            if not legacy_cpu0_format:
                record.update({
                    "cpu": cpu,
                    "target_key": f"{cpu}:{block}",
                })
                if cpu:
                    record["critical"] = True
            network_values = None
            if record["ok"] and cpu:
                try:
                    expected_length = network_protection_payload_length(block)
                    if len(payload) != expected_length:
                        raise ValueError(
                            f"erwartet {expected_length} Byte, "
                            f"empfangen {len(payload)} Byte"
                        )
                    network_values = decode_network_protection(cpu, payload, block)
                except ValueError as exc:
                    # Keep the raw capture visible in a partial image, but do
                    # not label an invalid safety-controller payload as
                    # restorable and do not abort the remaining targets.
                    record["ok"] = False
                    record["error"] = (
                        f"Netzschutz CPU {cpu}, Block {block} ist ungültig: {exc}"
                    )
            if decode and record["ok"]:
                if cpu:
                    record["values"] = network_values
                else:
                    record["values"] = [
                        {"key": item.key, "label": item.label, "raw": item.raw, "value": item.value, "unit": item.unit}
                        for item in self.pack.display_fields(block, payload)
                    ]
            records.append(record)
        image = {
            "schema": BACKUP_SCHEMA,
            "schema_version": BACKUP_SCHEMA_VERSION,
            "product": {
                "name": BACKUP_PRODUCT_NAME,
                "version": __version__,
            },
            "pack": {
                "name": BACKUP_PACK_NAME,
                "schema": str(self.pack.data.get("schema") or ""),
                "revision": self.pack.pack_rev,
            },
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "port": self.port,
            "baud": self.baud,
            "requested_blocks": len(targets),
            "successful_blocks": sum(bool(item.get("ok")) for item in records),
            "failed_blocks": sum(not bool(item.get("ok")) for item in records),
            "blocks": records,
        }
        if legacy_cpu0_format:
            image["requested_block_ids"] = [block for _cpu, block in targets]
        else:
            image["requested_targets"] = [
                {"cpu": cpu, "block": block} for cpu, block in targets
            ]
            if all(cpu == 0 for cpu, _block in targets):
                image["requested_block_ids"] = [block for _cpu, block in targets]
        if include_identity:
            try:
                identity = self.authentication_inputs(session)
                image["controller"] = {
                    "available": True,
                    "serial_number": identity.serial_number,
                    "operating_hours": identity.operating_hours,
                }
            except Exception as exc:
                image["controller"] = {
                    "available": False,
                    "error": str(exc),
                }
        image["image_sha256"] = _image_sha256(image)
        return image

    def maintenance_backup(
        self,
        session: SerialSession,
        *,
        decode: bool = True,
        created_by: str = "system",
    ) -> tuple[dict, dict[tuple[int, int], dict]]:
        """Capture the reviewed 42-target image exactly once in one session.

        The returned capture is the sole source for the maintenance report.
        Controller identity is decoded from the already captured CPU-0 blocks
        20 and 22; this method never authenticates, writes, or performs hidden
        identity reads.
        """
        capture: dict[tuple[int, int], dict] = {}
        records: list[dict] = []
        for cpu, block in BACKUP_TARGETS:
            target = (cpu, block)
            block_name = self._backup_target_name(cpu, block)
            try:
                result = self.read_block(session, block, cpu=cpu)
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                record = {
                    "cpu": cpu,
                    "block": block,
                    "target_key": f"{cpu}:{block}",
                    "block_name": block_name,
                    "ok": False,
                    "error": error,
                }
                if cpu:
                    record["critical"] = True
                capture[target] = {
                    "cpu": cpu,
                    "block": block,
                    "target_key": f"{cpu}:{block}",
                    "name": block_name,
                    "ok": False,
                    "status": None,
                    "payload": b"",
                    "fields": [],
                    "error": error,
                    "rtt_ms": None,
                }
                records.append(record)
                continue

            payload = bytes(result.payload)
            response = getattr(result, "response", None)
            ok = bool(result.ok)
            error = None if ok else "keine gültige serielle Antwort"
            decoded_values: list | None = None
            if ok:
                try:
                    expected_length = BACKUP_PAYLOAD_LENGTHS[target]
                    if len(payload) != expected_length:
                        raise ValueError(
                            f"erwartet {expected_length} Byte, empfangen {len(payload)} Byte"
                        )
                    if cpu:
                        decoded_values = decode_network_protection(cpu, payload, block)
                    elif decode:
                        decoded_values = self.pack.display_fields(block, payload)
                except Exception as exc:
                    ok = False
                    error = str(exc) or exc.__class__.__name__

            record = {
                "cpu": cpu,
                "block": block,
                "target_key": f"{cpu}:{block}",
                "block_name": block_name,
                "ok": ok,
                "status": result.status,
                "payload_hex": payload.hex().upper(),
                "payload_len": len(payload),
                "payload_sha256": _payload_sha256(payload),
                "rtt_ms": round(float(getattr(response, "elapsed_ms", 0.0)), 1),
                "crc_errors": int(getattr(response, "crc_errors", 0)),
                "protocol_errors": int(getattr(response, "protocol_errors", 0)),
            }
            if cpu:
                record["critical"] = True
            if error:
                record["error"] = error
            if decode and ok:
                if cpu:
                    record["values"] = decoded_values
                else:
                    record["values"] = [
                        {
                            "key": item.key,
                            "label": item.label,
                            "raw": item.raw,
                            "value": item.value,
                            "unit": item.unit,
                        }
                        for item in (decoded_values or [])
                    ]
            capture[target] = {
                "cpu": cpu,
                "block": block,
                "target_key": f"{cpu}:{block}",
                "name": block_name,
                "ok": ok,
                "status": result.status,
                "payload": payload,
                "fields": decoded_values or [],
                "error": error,
                "rtt_ms": record["rtt_ms"],
            }
            records.append(record)

        controller: dict
        try:
            identity = auth_inputs_from_payloads(
                self.pack,
                capture[(0, 20)]["payload"],
                capture[(0, 22)]["payload"],
            )
            if not capture[(0, 20)]["ok"] or not capture[(0, 22)]["ok"]:
                raise RuntimeError("identity blocks are not valid capture records")
            controller = {
                "available": True,
                "serial_number": identity.serial_number,
                "operating_hours": identity.operating_hours,
            }
        except Exception as exc:
            controller = {
                "available": False,
                "error": str(exc) or exc.__class__.__name__,
            }

        image = {
            "schema": BACKUP_SCHEMA,
            "schema_version": BACKUP_SCHEMA_VERSION,
            "product": {"name": BACKUP_PRODUCT_NAME, "version": __version__},
            "pack": {
                "name": BACKUP_PACK_NAME,
                "schema": str(self.pack.data.get("schema") or ""),
                "revision": self.pack.pack_rev,
            },
            "controller": controller,
            "maintenance_archive": {
                "version": 2,
                "source": "maintenance",
                "created_by": _short_text(created_by, "maintenance_archive.created_by", 128),
            },
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "port": self.port,
            "baud": self.baud,
            "requested_blocks": len(BACKUP_TARGETS),
            "successful_blocks": sum(bool(record["ok"]) for record in records),
            "failed_blocks": sum(not bool(record["ok"]) for record in records),
            "requested_targets": [
                {"cpu": cpu, "block": block} for cpu, block in BACKUP_TARGETS
            ],
            "blocks": records,
        }
        image["image_sha256"] = _image_sha256(image)
        return image, capture

    def inspect_backup(self, image: dict | str | bytes) -> dict:
        """Validate an image and return only restore-relevant metadata.

        Images produced before per-payload and image-level digests were added
        remain usable.  New images bind normalized restore metadata and every
        raw payload digest without depending on decoded display-value syntax.
        """
        if isinstance(image, bytes):
            try:
                image = image.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("backup image is not valid UTF-8") from exc
        if isinstance(image, str):
            try:
                image = json.loads(image)
            except json.JSONDecodeError as exc:
                raise ValueError("backup image is not valid JSON") from exc
        if not isinstance(image, dict):
            raise ValueError("backup image must be a JSON object")

        digest_present = "image_sha256" in image
        supplied_image_digest = image.get("image_sha256")
        if digest_present:
            if not isinstance(supplied_image_digest, str) or not _SHA256_PATTERN.fullmatch(
                supplied_image_digest
            ):
                raise ValueError("image_sha256 must be a lowercase SHA-256 digest")
            try:
                computed_image_digest = _image_sha256(image)
            except (TypeError, ValueError) as exc:
                raise ValueError("backup image contains non-canonical JSON values") from exc
            if not hmac.compare_digest(supplied_image_digest, computed_image_digest):
                raise ValueError("backup image SHA-256 mismatch")

        if image.get("schema") != BACKUP_SCHEMA:
            raise ValueError(f"unsupported backup schema: {image.get('schema')!r}")
        schema_version = image.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != BACKUP_SCHEMA_VERSION
        ):
            raise ValueError(f"unsupported backup schema version: {schema_version!r}")

        created_utc = image.get("created_utc")
        if not isinstance(created_utc, str):
            raise ValueError("created_utc must be an ISO-8601 timestamp")
        try:
            created = datetime.fromisoformat(created_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_utc must be an ISO-8601 timestamp") from exc
        if created.tzinfo is None:
            raise ValueError("created_utc must include a timezone")

        raw_records = image.get("blocks")
        if not isinstance(raw_records, list) or not raw_records:
            raise ValueError("backup blocks must be a non-empty list")

        writable_blocks = set()
        for candidate in self.pack.addressable_blocks():
            try:
                writable_blocks.add(validate_block(candidate, writable=True))
            except ValueError:
                continue
        capture_targets = {(0, block) for block in writable_blocks}
        capture_targets.update(
            (cpu, block)
            for cpu in NETWORK_PROTECTION_CPUS
            for block in NETWORK_PROTECTION_BACKUP_BLOCKS
        )
        # Every mapped CPU-0 block keeps the generic raw-backup/restore
        # contract.  The fixed 38-target maintenance contract is narrower,
        # but must not silently make other addressable pack blocks
        # non-restorable.  On the two network CPUs only the physically
        # accepted block 16 remains eligible for restore.
        restore_targets = {(0, block) for block in writable_blocks}
        restore_targets.update(
            target for target in BACKUP_RESTORE_TARGETS if target[0] != 0
        )
        if len(raw_records) > len(capture_targets):
            raise ValueError("backup contains more records than reviewed capture targets")

        target_format = bool(
            "requested_targets" in image
            or any(isinstance(record, dict) and "cpu" in record for record in raw_records)
        )

        records = []
        seen_targets: set[tuple[int, int]] = set()
        successful_blocks = 0
        for index, raw_record in enumerate(raw_records):
            prefix = f"blocks[{index}]"
            if not isinstance(raw_record, dict):
                raise ValueError(f"{prefix} must be an object")
            if target_format:
                raw_cpu = raw_record.get("cpu")
                if isinstance(raw_cpu, bool) or not isinstance(raw_cpu, int):
                    raise ValueError(f"{prefix}.cpu must be an integer")
                cpu = raw_cpu
            else:
                cpu = 0
            raw_block = raw_record.get("block")
            if isinstance(raw_block, bool) or not isinstance(raw_block, int):
                raise ValueError(f"{prefix}.block must be an integer")
            try:
                block = validate_block(raw_block, writable=True)
            except ValueError as exc:
                raise ValueError(f"{prefix}.block is not writable: {exc}") from exc
            target = (cpu, block)
            if target not in capture_targets:
                if cpu == 0:
                    raise ValueError(
                        f"{prefix}.block {block} is not mapped and writable on CPU 0"
                    )
                if cpu not in NETWORK_PROTECTION_CPUS:
                    raise ValueError(f"{prefix}.cpu must be 0, 1 or 2")
                raise ValueError(
                    f"{prefix}: CPU {cpu} exposes only reviewed network blocks "
                    f"{', '.join(str(item) for item in NETWORK_PROTECTION_BACKUP_BLOCKS)}"
                )
            if target in seen_targets:
                raise ValueError(f"duplicate backup target: CPU {cpu}, block {block}")
            seen_targets.add(target)

            ok = raw_record.get("ok")
            if not isinstance(ok, bool):
                raise ValueError(f"{prefix}.ok must be a boolean")
            status = raw_record.get("status")
            if (
                isinstance(status, bool)
                or status is not None
                and (not isinstance(status, int) or not 0 <= status <= 255)
            ):
                raise ValueError(f"{prefix}.status must be null or an integer in 0..255")
            if ok and status is None:
                raise ValueError(f"{prefix}.status is required for a successful record")
            sanitized = {
                "cpu": cpu,
                "block": block,
                "target_key": f"{cpu}:{block}",
                "block_name": self._backup_target_name(cpu, block),
                "ok": ok,
                "status": status,
                "restorable": False,
            }
            if cpu:
                sanitized["critical"] = True

            payload_fields = {
                "payload_hex",
                "payload_len",
                "payload_sha256",
            }
            payload_present = any(field in raw_record for field in payload_fields)
            if ok or payload_present:
                payload_hex = raw_record.get("payload_hex")
                if not isinstance(payload_hex, str):
                    raise ValueError(f"{prefix}.payload_hex must be a hexadecimal string")
                try:
                    payload = bytes.fromhex(payload_hex)
                except ValueError as exc:
                    raise ValueError(f"{prefix}.payload_hex is invalid") from exc
                if ok and not payload:
                    raise ValueError(f"{prefix}.payload_hex must not be empty")
                if len(payload) > MAX_RESTORE_PAYLOAD_LENGTH:
                    raise ValueError(
                        f"{prefix}.payload exceeds the {MAX_RESTORE_PAYLOAD_LENGTH}-byte restore limit"
                    )
                # A failed wire read can legitimately have no payload at all.
                # It remains visible but non-restorable and does not need the
                # successful-record payload contract below.
                if not ok and not payload:
                    error = raw_record.get("error")
                    if error is not None:
                        sanitized["error"] = _short_text(
                            error, f"{prefix}.error", 512
                        )
                    records.append(sanitized)
                    continue

                payload_len = raw_record.get("payload_len")
                if isinstance(payload_len, bool) or not isinstance(payload_len, int):
                    raise ValueError(f"{prefix}.payload_len must be an integer")
                if payload_len != len(payload):
                    raise ValueError(
                        f"{prefix}.payload_len does not match payload_hex"
                    )

                supplied_payload_digest = raw_record.get("payload_sha256")
                if supplied_payload_digest is None and digest_present:
                    raise ValueError(f"{prefix}.payload_sha256 is required")
                if supplied_payload_digest is not None:
                    if not isinstance(supplied_payload_digest, str) or not _SHA256_PATTERN.fullmatch(
                        supplied_payload_digest
                    ):
                        raise ValueError(
                            f"{prefix}.payload_sha256 must be a lowercase SHA-256 digest"
                        )
                    computed_payload_digest = _payload_sha256(payload)
                    if not hmac.compare_digest(
                        supplied_payload_digest, computed_payload_digest
                    ):
                        raise ValueError(f"{prefix}.payload SHA-256 mismatch")
                else:
                    computed_payload_digest = _payload_sha256(payload)

                if ok and cpu:
                    expected_length = network_protection_payload_length(block)
                    if len(payload) != expected_length:
                        raise ValueError(
                            f"{prefix}.payload must contain exactly "
                            f"{expected_length} bytes for network block {block}"
                        )
                    try:
                        decode_network_protection(cpu, payload, block)
                    except ValueError as exc:
                        raise ValueError(
                            f"{prefix}.payload is not a valid network-protection block: {exc}"
                        ) from exc

                sanitized.update({
                    "payload_hex": payload.hex().upper(),
                    "payload_len": len(payload),
                    "payload_sha256": computed_payload_digest,
                    "payload_digest_present": supplied_payload_digest is not None,
                })

            if ok:
                successful_blocks += 1
                sanitized["restorable"] = target in restore_targets
            else:
                error = raw_record.get("error")
                if error is not None:
                    sanitized["error"] = _short_text(error, f"{prefix}.error", 512)
            records.append(sanitized)

        failed_blocks = len(records) - successful_blocks
        requested_targets = [
            {"cpu": record["cpu"], "block": record["block"]} for record in records
        ]
        if target_format:
            declared_targets = image.get("requested_targets")
            if not isinstance(declared_targets, list):
                raise ValueError("requested_targets must be a list")
            normalized_declared_targets = []
            for index, declared_target in enumerate(declared_targets):
                if not isinstance(declared_target, dict):
                    raise ValueError(f"requested_targets[{index}] must be an object")
                raw_cpu = declared_target.get("cpu")
                raw_block = declared_target.get("block")
                if isinstance(raw_cpu, bool) or not isinstance(raw_cpu, int):
                    raise ValueError(f"requested_targets[{index}].cpu must be an integer")
                if isinstance(raw_block, bool) or not isinstance(raw_block, int):
                    raise ValueError(f"requested_targets[{index}].block must be an integer")
                normalized_declared_targets.append({"cpu": raw_cpu, "block": raw_block})
            if normalized_declared_targets != requested_targets:
                raise ValueError("requested_targets do not match backup records")

        requested_block_ids = [record["block"] for record in records]
        if "requested_block_ids" in image:
            declared_ids = image["requested_block_ids"]
            if (
                any(record["cpu"] != 0 for record in records)
                or not isinstance(declared_ids, list)
                or any(
                    isinstance(block, bool) or not isinstance(block, int)
                    for block in declared_ids
                )
                or declared_ids != requested_block_ids
            ):
                raise ValueError("requested_block_ids do not match CPU-0 backup records")
        declared_counts = {
            "requested_blocks": len(records),
            "successful_blocks": successful_blocks,
            "failed_blocks": failed_blocks,
        }
        for key, expected in declared_counts.items():
            if key not in image:
                continue
            declared = image[key]
            if isinstance(declared, bool) or not isinstance(declared, int) or declared != expected:
                raise ValueError(f"{key} does not match backup records")

        product = None
        raw_product = image.get("product")
        if raw_product is not None:
            if not isinstance(raw_product, dict):
                raise ValueError("product must be an object")
            product = {
                "name": _short_text(raw_product.get("name"), "product.name", 128),
                "version": _short_text(raw_product.get("version"), "product.version", 64),
            }

        pack = None
        raw_pack = image.get("pack")
        if raw_pack is not None:
            if not isinstance(raw_pack, dict):
                raise ValueError("pack must be an object")
            pack = {
                "name": _short_text(raw_pack.get("name"), "pack.name", 128),
                "schema": _short_text(raw_pack.get("schema"), "pack.schema", 128),
                "revision": _short_text(raw_pack.get("revision"), "pack.revision", 64),
            }

        controller = None
        raw_controller = image.get("controller")
        if raw_controller is not None:
            if not isinstance(raw_controller, dict) or not isinstance(
                raw_controller.get("available"), bool
            ):
                raise ValueError("controller.available must be a boolean")
            if raw_controller["available"]:
                hours = raw_controller.get("operating_hours")
                if isinstance(hours, bool) or not isinstance(hours, int) or hours < 0:
                    raise ValueError("controller.operating_hours must be a non-negative integer")
                controller = {
                    "available": True,
                    "serial_number": _short_text(
                        raw_controller.get("serial_number"),
                        "controller.serial_number",
                        128,
                    ),
                    "operating_hours": hours,
                }
            else:
                controller = {"available": False}
                if raw_controller.get("error") is not None:
                    controller["error"] = _short_text(
                        raw_controller["error"], "controller.error", 512
                    )

        current_pack_schema = str(self.pack.data.get("schema") or "")
        pack_compatible = bool(
            pack
            and pack["schema"] == current_pack_schema
            and pack["revision"] == self.pack.pack_rev
        )
        result = {
            "schema": BACKUP_SCHEMA,
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_utc": created.isoformat(),
            "product": product,
            "pack": pack,
            "pack_compatible": pack_compatible if pack is not None else None,
            "controller": controller,
            "image_sha256": supplied_image_digest if digest_present else None,
            "digest_present": digest_present,
            "digest_verified": True if digest_present else None,
            "requested_blocks": len(records),
            "requested_targets": requested_targets,
            "successful_blocks": successful_blocks,
            "failed_blocks": failed_blocks,
            "restorable_blocks": sum(bool(record["restorable"]) for record in records),
            "restorable_targets": [
                {"cpu": record["cpu"], "block": record["block"]}
                for record in records if record["restorable"]
            ],
            "records": records,
        }
        if all(record["cpu"] == 0 for record in records):
            result["requested_block_ids"] = requested_block_ids
            result["restorable_block_ids"] = [
                record["block"] for record in records if record["restorable"]
            ]
        return result

    def restore_payload(
        self,
        session: SerialSession,
        block: int,
        before: bytes,
        target: bytes,
        dry_run: bool,
        cpu: int = 0,
    ) -> WriteAudit:
        """Restore one complete CPU/block target with exact full-block readback."""
        now = datetime.now(timezone.utc).isoformat()
        before = bytes(before)
        target = bytes(target)
        valid_block_type = isinstance(block, int) and not isinstance(block, bool)
        valid_cpu_type = isinstance(cpu, int) and not isinstance(cpu, bool)
        block_id = block if valid_block_type else -1
        cpu_id = cpu if valid_cpu_type else -1
        synthetic_key = (
            f"backup.restore.block[{block_id}].full_payload"
            if cpu_id == 0
            else f"backup.restore.cpu[{cpu_id}].block[{block_id}].full_payload"
        )

        def audit(
            *,
            written: bool = False,
            readback_ok: bool | None = None,
            error: str | None = None,
            ack_positive: bool | None = None,
            readback_scope: str | None = None,
            readback_attempts: int = 0,
            write_attempted: bool = False,
        ) -> WriteAudit:
            return WriteAudit(
                key=synthetic_key,
                block=block_id,
                before_hex=before.hex(" ").upper(),
                after_hex=target.hex(" ").upper(),
                dry_run=bool(dry_run),
                written=written,
                readback_ok=readback_ok,
                timestamp_utc=now,
                error=error,
                changed_keys=(synthetic_key,),
                ack_positive=ack_positive,
                cpu=cpu_id,
                readback_scope=readback_scope,
                readback_attempts=readback_attempts,
                write_attempted=write_attempted,
            )

        if not valid_block_type:
            return audit(error="block must be an integer")
        if not valid_cpu_type:
            return audit(error="CPU must be an integer")
        try:
            writable_block = validate_block(block_id, writable=True)
        except ValueError as exc:
            return audit(error=str(exc))
        if cpu_id == 0:
            if writable_block not in self.pack.addressable_blocks():
                return audit(error=f"block {writable_block} is not mapped and writable")
        elif cpu_id not in NETWORK_PROTECTION_CPUS:
            return audit(error=f"CPU must be 0, 1 or 2, got {cpu_id}")
        elif writable_block != NETWORK_PROTECTION_BLOCK:
            return audit(
                error=f"CPU {cpu_id} exposes only network-protection block "
                f"{NETWORK_PROTECTION_BLOCK}"
            )
        if len(before) != len(target):
            return audit(error="payload length changed")
        if not before:
            return audit(error="payload must not be empty")
        if len(target) > MAX_RESTORE_PAYLOAD_LENGTH:
            return audit(
                error=f"payload exceeds the {MAX_RESTORE_PAYLOAD_LENGTH}-byte restore limit"
            )
        if cpu_id:
            if (
                len(before) != NETWORK_PROTECTION_PAYLOAD_LENGTH
                or len(target) != NETWORK_PROTECTION_PAYLOAD_LENGTH
            ):
                return audit(
                    error="network-protection payload must contain exactly "
                    f"{NETWORK_PROTECTION_PAYLOAD_LENGTH} bytes"
                )
            try:
                decode_network_protection(cpu_id, before)
                decode_network_protection(cpu_id, target)
            except ValueError as exc:
                return audit(error=f"invalid network-protection payload: {exc}")
        if before == target:
            return audit(readback_ok=True, readback_scope="block")
        if dry_run:
            return audit()

        write_attempted = False
        ack_positive: bool | None = None
        readback_attempts = 0
        try:
            current = self.read_block(session, writable_block, cpu=cpu_id)
            if not current.ok or current.payload != before:
                raise RuntimeError(
                    "block changed since it was loaded; reload before restoring"
                )
            write_attempted = True
            if cpu_id:
                response = session.write_block(
                    writable_block,
                    target,
                    packet=None,
                    timeout=self.timeout,
                    cpu=cpu_id,
                )
            else:
                response = session.write_block(
                    writable_block,
                    target,
                    packet=None,
                    timeout=self.timeout,
                )
            ack_positive = bool(response.ack is not None and response.ack.positive)
            if not ack_positive:
                raise RuntimeError("write did not receive a positive ACK")
            for attempt in range(1, self.readback_attempts + 1):
                readback_attempts = attempt
                readback = self.read_block(session, writable_block, cpu=cpu_id)
                if readback.ok and readback.payload == target:
                    return audit(
                        written=True,
                        readback_ok=True,
                        ack_positive=True,
                        readback_scope="block",
                        readback_attempts=readback_attempts,
                        write_attempted=True,
                    )
                if attempt < self.readback_attempts and self.readback_delay:
                    time.sleep(self.readback_delay)
            raise RuntimeError(
                f"full-block readback mismatch after {readback_attempts} attempts"
            )
        except Exception as exc:
            return audit(
                readback_ok=False,
                error=str(exc),
                ack_positive=ack_positive,
                readback_attempts=readback_attempts,
                write_attempted=write_attempted,
            )


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
        directory_fd = os.open(
            destination.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            # Persist the directory entry as well as the file contents.  Any
            # failure propagates so a live restore cannot proceed on the false
            # assumption that its preimage is durable.
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_exclusive(path: str | Path, data: dict) -> bytes:
    """Durably create one JSON file without replacing any existing inode.

    A hard-link inside an already opened, non-symlink directory provides the
    atomic no-replace publication step available on all supported Linux/Python
    combinations.  Both file data and the final directory entry are fsynced.
    The returned bytes are exactly those stored and can therefore be used for
    the archive-level ``file_sha256``.
    """
    import os
    import secrets
    import stat

    destination = Path(path)
    if destination.name in {"", ".", ".."} or destination.parent == destination:
        raise ValueError("invalid archive destination")
    payload = (
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(destination.parent, directory_flags)
    temporary_name = f".{destination.name}.{secrets.token_hex(12)}.tmp"
    published = False
    try:
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise OSError("archive parent is not a directory")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(file_fd, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            published = True
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)
        if published:
            os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return payload
