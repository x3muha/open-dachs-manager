"""Fail-closed, offline inspection of Senertec MSR2 ``.muc`` containers.

This module deliberately contains no serial or flashing primitive.  It only
validates the container, decompresses it with the system ``uncompress`` tool
and inventories the eight embedded module images.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PureWindowsPath
import selectors
import struct
import subprocess
import tempfile
import time


MUC_MAGIC = b"CUM\x1a"
MUC_HEADER_SIZE = 16
MUC_MAX_FILE_BYTES = 1024 * 1024
MUC_MAX_UNPACKED_BYTES = 4 * 1024 * 1024
MUC_DECOMPRESS_TIMEOUT_SECONDS = 10.0
MUC_DECOMPRESS_COMMAND = ("uncompress", "-c")
ENTRY_PREFIX = b"\x01\x00\x01\x00"
EXPECTED_ENTRY_SEQUENCE = (4, 5, 6, 7, 0, 1, 2, 3)
MODULES = ("Regler", "Überwachung 1", "Überwachung 2", "Messwerterfassung")


def _stop_process(process: subprocess.Popen) -> None:
    """Reap a decompressor, escalating to kill if it does not terminate."""
    if process.poll() is not None:
        process.wait()
        return
    process.terminate()
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_decompressor(packed: bytes, expected_size: int) -> bytes:
    """Stream decompressor output with a hard byte limit and deadline."""
    with tempfile.TemporaryFile() as compressed, tempfile.TemporaryFile() as errors:
        compressed.write(packed)
        compressed.seek(0)
        try:
            process = subprocess.Popen(
                MUC_DECOMPRESS_COMMAND,
                stdin=compressed,
                stdout=subprocess.PIPE,
                stderr=errors,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Systemwerkzeug 'uncompress' ist nicht installiert") from exc

        assert process.stdout is not None
        deadline = time.monotonic() + MUC_DECOMPRESS_TIMEOUT_SECONDS
        output = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ValueError("MUC-Dekompression hat das Zeitlimit überschritten")
                chunk = os.read(
                    process.stdout.fileno(),
                    min(64 * 1024, expected_size + 1 - len(output)),
                )
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > expected_size:
                    raise ValueError(
                        "entpackte MUC-Daten überschreiten die im Header angegebene Länge"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("MUC-Dekompression hat das Zeitlimit überschritten")
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise ValueError(
                    "MUC-Dekompression hat das Zeitlimit überschritten"
                ) from exc
            if returncode != 0:
                errors.seek(0)
                detail = errors.read(4096).decode("utf-8", "replace").strip()
                raise ValueError(
                    "MUC-Datenstrom wurde abgelehnt: "
                    f"{detail or 'Dekompression fehlgeschlagen'}"
                )
            return bytes(output)
        finally:
            selector.close()
            process.stdout.close()
            _stop_process(process)


def _decompress_muc(raw: bytes) -> tuple[bytes, dict]:
    if not isinstance(raw, bytes):
        raise ValueError("Firmwareinhalt muss binär vorliegen")
    if len(raw) > MUC_MAX_FILE_BYTES:
        raise ValueError(f"MUC-Datei ist größer als {MUC_MAX_FILE_BYTES} Byte")
    if len(raw) < MUC_HEADER_SIZE or raw[:4] != MUC_MAGIC:
        raise ValueError("ungültiger CUM/MUC-Header")
    header_size, unpacked_size, packed_size = struct.unpack_from("<III", raw, 4)
    if header_size != MUC_HEADER_SIZE:
        raise ValueError(f"nicht unterstützte MUC-Headergröße {header_size}")
    if packed_size != len(raw) - header_size:
        raise ValueError(
            f"gepackte Länge stimmt nicht: Header {packed_size}, Datei {len(raw) - header_size}"
        )
    if not 0 < unpacked_size <= MUC_MAX_UNPACKED_BYTES:
        raise ValueError("entpackte MUC-Länge liegt außerhalb des sicheren Bereichs")
    unpacked = _run_decompressor(raw[header_size:], unpacked_size)
    if len(unpacked) != unpacked_size:
        raise ValueError(
            f"entpackte Länge stimmt nicht: Header {unpacked_size}, tatsächlich {len(unpacked)}"
        )
    return unpacked, {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "packed_bytes": packed_size,
        "unpacked_bytes": unpacked_size,
    }


def _entries(unpacked: bytes) -> list[dict]:
    candidates: list[tuple[int, int, str]] = []
    for entry_id in range(8):
        signature = ENTRY_PREFIX + struct.pack("<I", entry_id)
        cursor = 0
        while True:
            offset = unpacked.find(signature, cursor)
            if offset < 0:
                break
            path_start = offset + 0x106
            path_end = unpacked.find(b"\x00", path_start, path_start + 0x200)
            if path_end > path_start:
                source = unpacked[path_start:path_end].decode("cp1252", "replace")
                if source.startswith("\\") and source.lower().endswith(".hex"):
                    candidates.append((offset, entry_id, source))
            cursor = offset + 1
    candidates.sort()
    sequence = tuple(item[1] for item in candidates)
    if sequence != EXPECTED_ENTRY_SEQUENCE:
        raise ValueError(f"unerwartete Firmwaremodulfolge {list(sequence)}")
    result = []
    for index, (start, entry_id, source) in enumerate(candidates):
        end = candidates[index + 1][0] if index + 1 < len(candidates) else len(unpacked)
        result.append({
            "id": entry_id,
            "architecture": "M16C" if entry_id < 4 else "M32C",
            "module": MODULES[entry_id % 4],
            "offset": start,
            "bytes": end - start,
            "source": PureWindowsPath(source).name,
        })
    return result


def inspect_muc(raw: bytes, filename: object = "firmware.muc") -> dict:
    """Validate and inventory a MUC without storing it or touching serial IO."""
    safe_name = Path(str(filename or "firmware.muc").replace("\\", "/")).name
    if not safe_name.lower().endswith(".muc"):
        raise ValueError("nur Dateien mit Endung .muc werden angenommen")
    unpacked, metadata = _decompress_muc(raw)
    entries = _entries(unpacked)
    return {
        "ok": True,
        "filename": safe_name,
        **metadata,
        "entry_count": len(entries),
        "entries": entries,
        "flash_available": False,
        "flash_blocked_reason": (
            "Der native Bootloader-Ablauf ist noch nicht vollständig aus dem "
            "Golden Trace verifiziert. Die Datei wurde nur offline geprüft."
        ),
        "serial_accessed": False,
        "stored": False,
    }
