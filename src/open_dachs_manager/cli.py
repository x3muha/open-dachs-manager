"""Open Dachs Manager command-line interface."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from . import __version__
from .auth import validate_auth_level
from .mapping import PackRepository, WriteAllowlist, default_allowlist_path, is_reserved_key
from .serial_worker import DEFAULT_SERIAL_WORKER_SOCKET, SerialWorkerSession
from .service import DachsService, write_json_atomic
from .transport import TransportError, serial, validate_block


def _blocks(value: str) -> list[int]:
    if not str(value).strip():
        raise ValueError("block list must not be empty")
    out = []
    seen = set()
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"invalid empty block in list {value!r}")
        try:
            block = int(part, 0)
        except ValueError as exc:
            raise ValueError(f"invalid block {part!r}; use decimal or 0xNN") from exc
        if block not in seen:
            out.append(block)
            seen.add(block)
    return out


def _checked_blocks(value: str) -> list[int]:
    return [validate_block(block) for block in _blocks(value)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-dachs",
        description="Open Dachs Manager for serial MSR2 communication",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=19200)
    parser.add_argument("--timeout", type=float, default=0.9, help="serial response timeout in seconds")
    parser.add_argument(
        "--serial-socket",
        default=os.environ.get("OPEN_DACHS_SERIAL_SOCKET", DEFAULT_SERIAL_WORKER_SOCKET),
        help="Unix socket of the shared Open Dachs serial worker",
    )
    parser.add_argument(
        "--direct-serial",
        action="store_true",
        help="maintenance fallback: bypass the worker and open --port directly",
    )
    parser.add_argument("--pack-file", default="", help="versioned pack JSON")
    parser.add_argument("--pack-rev", default="50")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check the local runtime, worker and mapping")
    doctor.add_argument("--json", action="store_true")

    blocks = commands.add_parser("list-blocks", help="list mapped MSR2 blocks")
    blocks.add_argument("--addressable-only", action="store_true", help="only blocks addressable by the serial service")
    blocks.add_argument("--json", action="store_true")

    keys = commands.add_parser("list-keys", help="list mapped technical field keys")
    keys.add_argument("--block", type=int)
    keys.add_argument("--search", default="", help="filter by key or human label")
    keys.add_argument("--show-reserved", action="store_true")
    keys.add_argument("--json", action="store_true")

    watch = commands.add_parser("watch", help="monitor the MSR2 link")
    watch.add_argument("--count", type=int, default=10)
    watch.add_argument("--interval", type=float, default=0.5)

    read = commands.add_parser("read", help="read raw or decoded values")
    read_commands = read.add_subparsers(dest="read_command", required=True)
    block = read_commands.add_parser("block", help="read one block")
    block.add_argument("--block", required=True, type=int)
    block.add_argument("--json", action="store_true")
    decoded = read_commands.add_parser("decoded", help="read and decode blocks")
    decoded.add_argument("--blocks", default="20,22,24,26")
    decoded.add_argument("--json", action="store_true")
    decoded.add_argument("--show-reserved", action="store_true")

    auth = commands.add_parser("auth", help="calculate PW4 and request an MSR2 auth level")
    auth.add_argument("--level", type=int, required=True, help="supported MSR2 level 1..5")
    auth.add_argument("--pass4", default="")
    auth.add_argument("--show-secret", action="store_true", help="print PW4; avoid in shared logs")

    backup = commands.add_parser("backup", help="create a JSON backup")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    create = backup_commands.add_parser("create")
    create.add_argument("--blocks", default="20,22,24,26")
    create.add_argument("--all-blocks", action="store_true")
    create.add_argument("--output", default="open_dachs_backup.json")
    create.add_argument("--no-decode", action="store_true")

    write = commands.add_parser("write", help="plan or apply a mapped field change")
    write_commands = write.add_subparsers(dest="write_command", required=True)

    def add_write_arguments(command):
        command.add_argument("--key", required=True, help="technical mapped field key or unique human label")
        command.add_argument("--value", required=True, help="display value, or raw value with --raw")
        command.add_argument("--block", type=int, help="block containing the key (optional when key is unique)")
        command.add_argument("--raw", action="store_true", help="interpret numeric input as the stored raw value")
        command.add_argument(
            "--auth-level", type=int, default=-1, help="supported MSR2 level 1..5"
        )
        command.add_argument("--pass4", default="")
        command.add_argument("--write-enabled", action="store_true", help="permit the live write after auth and readback checks")
        command.add_argument("--allowlist", default=str(default_allowlist_path()))
        command.add_argument("--json", action="store_true")

    write_set = write_commands.add_parser("set", help="read a block, prepare one field change and optionally write it")
    add_write_arguments(write_set)
    write_plan = write_commands.add_parser("plan", help="read a block and print a dry-run write audit")
    add_write_arguments(write_plan)
    write_apply = write_commands.add_parser("apply", help="apply a field change with explicit write enablement")
    add_write_arguments(write_apply)

    tui = commands.add_parser("tui", help="interactive read/edit UI")
    tui.add_argument("--block", type=int, default=20)
    tui.add_argument("--all-blocks", action="store_true")
    tui.add_argument(
        "--auth-level", type=int, default=-1, help="supported MSR2 level 1..5"
    )
    tui.add_argument("--pass4", default="")
    tui.add_argument("--write-enabled", action="store_true", help="permit hardware writes; still requires auth and readback")
    tui.add_argument("--dry-run", action="store_true", help="stage edits without writing; default unless --write-enabled is used")
    tui.add_argument("--allowlist", default=str(default_allowlist_path()))
    tui.add_argument("--show-reserved", action="store_true")

    return parser


def _service(args) -> DachsService:
    if args.timeout <= 0:
        raise ValueError("--timeout must be greater than zero")
    if args.baud <= 0:
        raise ValueError("--baud must be greater than zero")
    pack = PackRepository(args.pack_file or None, args.pack_rev)
    serial_socket = None if args.direct_serial else args.serial_socket
    return DachsService(args.port, args.baud, args.timeout, pack, serial_socket=serial_socket)


def _print_block(result, as_json: bool = False) -> None:
    record = {
        "block": result.block,
        "packet": result.packet,
        "ok": result.ok,
        "status": result.status,
        "payload_hex": result.payload.hex().upper(),
        "payload_len": len(result.payload),
        "rtt_ms": round(result.response.elapsed_ms, 1),
        "crc_errors": result.response.crc_errors,
        "protocol_errors": result.response.protocol_errors,
    }
    if as_json:
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return
    print(f"block={result.block} ok={result.ok} status={result.status!r} payload={len(result.payload)}B rtt={result.response.elapsed_ms:.1f}ms crc_errors={result.response.crc_errors} protocol_errors={result.response.protocol_errors}")
    print(f"payload={result.payload.hex(' ').upper()}")


def _run(args) -> int:
    if args.command == "doctor":
        pack = PackRepository(args.pack_file or None, args.pack_rev)
        blocks = pack.blocks()
        addressable = pack.addressable_blocks()
        serial_socket = "" if args.direct_serial else str(args.serial_socket or "")
        worker_status = None
        worker_error = None
        if serial_socket:
            try:
                with SerialWorkerSession(serial_socket, queue_timeout=5.0) as worker:
                    worker_status = worker.ping()
            except Exception as exc:
                worker_error = str(exc)
        diagnostics = {
            "version": __version__,
            "package_root": str(Path(__file__).resolve().parent),
            "pack": str(pack.pack_file),
            "pack_exists": pack.pack_file.exists(),
            "pack_rev": pack.pack_rev,
            "mapped_blocks": len(blocks),
            "addressable_blocks": len(addressable),
            "non_addressable_blocks": len(blocks) - len(addressable),
            "mapped_keys": len(pack.keys()),
            "pyserial": serial is not None,
            "serial": args.port,
            "serial_exists": Path(args.port).exists(),
            "transport": "direct" if args.direct_serial else "serial-worker",
            "serial_socket": serial_socket,
            "serial_worker": worker_status,
            "serial_worker_error": worker_error,
            "baud": args.baud,
        }
        if args.json:
            print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        else:
            print(f"open-dachs-manager={diagnostics['version']}")
            print(f"package={diagnostics['package_root']}")
            print(f"pack={diagnostics['pack']} rev={diagnostics['pack_rev']} exists={diagnostics['pack_exists']}")
            print(f"blocks={diagnostics['mapped_blocks']} addressable={diagnostics['addressable_blocks']} non-addressable={diagnostics['non_addressable_blocks']}")
            print(f"keys={diagnostics['mapped_keys']} pyserial={diagnostics['pyserial']}")
            print(f"transport={diagnostics['transport']} serial={args.port} exists={diagnostics['serial_exists']} baud={args.baud}")
            if serial_socket:
                state = "ok" if worker_status else f"ERROR {worker_error}"
                print(f"serial-worker={serial_socket} {state}")
        transport_ready = diagnostics["serial_exists"] if args.direct_serial else bool(worker_status)
        return 0 if diagnostics["pack_exists"] and diagnostics["pyserial"] and transport_ready else 2

    if args.command == "list-blocks":
        pack = PackRepository(args.pack_file or None, args.pack_rev)
        block_ids = pack.addressable_blocks() if args.addressable_only else pack.blocks()
        records = [
            {"block": block, "addressable": 0 <= block <= 255, "name": pack.block_name(block), "fields": len(pack.field_map(block))}
            for block in block_ids
        ]
        if args.json:
            print(json.dumps(records, ensure_ascii=False, indent=2))
        else:
            for item in records:
                mark = "ok" if item["addressable"] else "offline-only"
                print(f"{item['block']:>4}  {mark:<12} {item['fields']:>3} fields  {item['name']}")
        return 0

    if args.command == "list-keys":
        pack = PackRepository(args.pack_file or None, args.pack_rev)
        block_ids = [validate_block(args.block)] if args.block is not None else pack.addressable_blocks()
        records = []
        for block_id in block_ids:
            for key, metadata in pack.field_map(block_id).items():
                if args.show_reserved or not is_reserved_key(key):
                    records.append({"block": block_id, "key": key, "label": pack.label(key), **metadata})
        if args.search:
            needle = args.search.casefold()
            records = [item for item in records if needle in item["key"].casefold() or needle in item["label"].casefold()]
        records.sort(key=lambda item: (item["key"], item["block"]))
        if args.json:
            print(json.dumps(records, ensure_ascii=False, indent=2))
        else:
            for item in records:
                print(f"{item['block']:>3}  {item['key']}  @{item['offset']} {item['type']}  {item['label']}")
        return 0

    service = _service(args)

    if args.command == "watch":
        if args.count < 1:
            raise ValueError("--count must be at least 1")
        if args.interval < 0:
            raise ValueError("--interval must not be negative")
        failures = 0
        print("time\tpacket\tack\trtt_ms\tcrc_errors\tprotocol_errors")
        for packet in range(max(0, args.count)):
            with service.session() as session:
                response = session.request(b"", packet & 0x0F, service.timeout)
            failures += response.ack is None or not response.ack.positive
            print(f"{time.strftime('%H:%M:%S')}\t{packet & 0x0F:X}\t{response.ack.positive if response.ack else None}\t{response.elapsed_ms:.1f}\t{response.crc_errors}\t{response.protocol_errors}")
            time.sleep(max(0.0, args.interval))
        return 0 if failures == 0 else 3

    if args.command == "read":
        if args.read_command == "block":
            validate_block(args.block)
            with service.session() as session:
                result = service.read_block(session, args.block)
                _print_block(result, args.json)
            return 0 if result.ok else 3
        blocks = _checked_blocks(args.blocks)
        records = []
        failures = 0
        with service.session() as session:
            for block_id in blocks:
                result, fields = service.decoded_block(session, block_id)
                failures += not result.ok
                history = service.pack.meldehist(result.payload) if block_id == 18 and result.ok else None
                record = {
                    "block": block_id,
                    "ok": result.ok,
                    "status": result.status,
                    "payload_hex": result.payload.hex().upper(),
                    "payload_len": len(result.payload),
                    "rtt_ms": round(result.response.elapsed_ms, 1),
                    "crc_errors": result.response.crc_errors,
                    "protocol_errors": result.response.protocol_errors,
                    "values": [
                        {"key": f.key, "label": f.label, "raw": f.raw, "value": f.value, "unit": f.unit}
                        for f in fields
                        if args.show_reserved or not is_reserved_key(f.key)
                    ],
                }
                if history is not None:
                    record["history"] = history
                    record["values"] = []
                records.append(record)
                if not args.json:
                    print(f"\n[{block_id}] {service.pack.block_name(block_id)} status={result.status!r} payload={len(result.payload)}B")
                    if history is not None:
                        current = history["current_ring"]
                        print(f"  Aktueller Ring: {current if current is not None else '-'}")
                        for entry in history["entries"]:
                            mark = " <= aktuell" if entry["active"] else ""
                            timestamp = entry["timestamp_text"] or "-"
                            print(
                                f"  [{entry['index']:02d}]{mark} {timestamp} | "
                                f"Meldung: {entry['message']} | ID={entry['message_id'] if entry['message_id'] is not None else '-'} | "
                                f"Typ={entry['type']} ({entry['type_label']}) | "
                                f"Modul={entry['module']} ({entry['module_label']})"
                            )
                    else:
                        for field in record["values"]:
                            unit = f" {field['unit']}" if field["unit"] else ""
                            print(f"  {field['label']} [{field['key']}] = {field['value']}{unit}")
        if args.json:
            print(json.dumps(records, ensure_ascii=False, indent=2))
        return 0 if not failures else 3

    if args.command == "auth":
        level = validate_auth_level(args.level, "--level")
        with service.session() as session:
            result = service.authenticate(session, level, args.pass4 or None)
        print(json.dumps(result.as_dict(args.show_secret), ensure_ascii=False, indent=2))
        return 0 if result.ok else 3

    if args.command == "backup":
        all_mapped = service.pack.blocks()
        blocks = service.pack.addressable_blocks() if args.all_blocks else _checked_blocks(args.blocks)
        with service.session() as session:
            result = service.backup(session, blocks, decode=not args.no_decode)
        if args.all_blocks:
            result["skipped_non_addressable_blocks"] = [block for block in all_mapped if block not in blocks]
        write_json_atomic(args.output, result)
        print(f"backup={args.output} blocks={result['successful_blocks']}/{result['requested_blocks']} failed={result['failed_blocks']}")
        return 0 if result["failed_blocks"] == 0 else 3

    if args.command == "write":
        if args.write_command == "apply" and not args.write_enabled:
            raise ValueError("write apply requires --write-enabled")
        if args.write_enabled:
            args.auth_level = validate_auth_level(
                args.auth_level, "--auth-level für Live-Schreiben"
            )
        block_id, key, metadata = service.pack.resolve_key(args.key, args.block)
        dry_run = args.write_command == "plan" or not args.write_enabled
        validate_block(block_id, writable=not dry_run)
        with service.session() as session:
            auth_result = None
            if args.write_enabled:
                auth_result = service.authenticate(session, args.auth_level, args.pass4 or None)
                if not auth_result.ok:
                    raise RuntimeError(f"requested auth level was not granted: {auth_result.granted_level}")
            before_result = service.read_block(session, block_id)
            if not before_result.ok:
                raise RuntimeError(f"block {block_id} read failed (status={before_result.status!r})")
            before = bytes(before_result.payload)
            after = bytearray(before)
            service.pack.encode_value(after, key, args.value, raw_mode=args.raw, block=block_id)
            if before == bytes(after):
                audit = {
                    "schema": "dachs-msr2-write-audit/v3",
                    "key": key,
                    "block": block_id,
                    "changed": False,
                    "changed_keys": [],
                    "dry_run": dry_run,
                    "written": False,
                    "readback_ok": True,
                    "error": None,
                    "message": "value already matches requested value",
                    "before_hex": before.hex(" ").upper(),
                    "after_hex": bytes(after).hex(" ").upper(),
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "ack_positive": None,
                }
            else:
                audit_obj = service.write_payload(
                    session,
                    block_id,
                    before,
                    bytes(after),
                    [key],
                    WriteAllowlist(args.allowlist),
                    dry_run=dry_run,
                )
                audit = {"schema": "dachs-msr2-write-audit/v3", **audit_obj.as_dict()}
            audit["auth"] = auth_result.as_dict(False) if auth_result is not None else None
        if args.json:
            print(json.dumps(audit, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(audit, ensure_ascii=False, indent=2))
        return 0 if audit.get("error") is None and audit.get("readback_ok", True) is not False else 3

    if args.command == "tui":
        from .tui import run_tui
        return run_tui(service, args)

    return 2


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return _run(args)
    except (ValueError, RuntimeError, TransportError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130
