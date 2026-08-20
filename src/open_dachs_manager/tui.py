"""Compact, keyboard-driven Open Dachs Manager TUI.

The UI is deliberately a frontend.  It never constructs a serial frame by
itself; reads and guarded writes go through DachsService.
"""

from __future__ import annotations

import curses
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .auth import validate_auth_level
from .mapping import WriteAllowlist, is_reserved_key
from .transport import validate_block


def _safe(value) -> str:
    return str(value if value is not None else "").replace("\x00", " ")


def _edit_value(field):
    if str(field.metadata.get("type", "")).lower() == "version":
        return field.value
    if re.search(r"(?i)(\.bstoerung$|\.bwarnung$|\.bstoercode$|\.bwarncode$|bmeldecodetypereturn$|\.bstatusflags$|\.bwarntypmodul$)", field.key):
        return field.raw
    return field.value


def _draw(stdscr, service, state):
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    mode = "WRITE" if state["write_enabled"] else "DRY-RUN"
    title = f" OPEN DACHS  |  Block {state['block']} {service.pack.block_name(state['block'])}  |  {mode} "
    stdscr.addnstr(0, 0, title, max(1, width - 1), curses.A_BOLD)
    auth = state.get("auth")
    auth_text = "AUTH n/a"
    if auth is not None:
        auth_text = f"AUTH requested={auth.requested_level} granted={auth.granted_level}"
    stdscr.addnstr(1, 0, f" {auth_text} | changed={len(state['changed'])} | q quit | r reload | ←/→ block | Enter edit | F2 save ", max(1, width - 1))
    if state.get("message"):
        stdscr.addnstr(2, 0, " " + _safe(state["message"]), max(1, width - 1), curses.A_REVERSE)

    max_rows = max(1, height - 5)
    if state["block"] == 18:
        history = state.get("history") or {}
        entries = history.get("entries", [])
        current = history.get("current_ring")
        stdscr.addnstr(3, 0, f" Meldungsliste · aktueller Ring: {current if current is not None else '-'} · Rohfelder editierbar", max(1, width - 1), curses.A_BOLD)
        history_limit = min(len(entries), max(1, min(10, (max_rows - 2) // 2)))
        for row_no, entry in enumerate(entries[:history_limit], start=4):
            mark = "*" if entry.get("active") else " "
            line = (
                f"{mark} [{entry['index']:02d}] {_safe(entry.get('timestamp_text') or '-'):<19} "
                f"{_safe(entry.get('message'))[:34]:<34} ID={entry.get('message_id') if entry.get('message_id') is not None else '-':<5} "
                f"Typ={entry.get('type')} {_safe(entry.get('type_label'))[:20]} Modul={entry.get('module')} {_safe(entry.get('module_label'))}"
            )
            stdscr.addnstr(row_no, 0, line, max(1, width - 1), curses.A_NORMAL)
        raw_heading = 4 + history_limit
        if raw_heading < height - 1:
            stdscr.addnstr(raw_heading, 0, " Rohfelder (Cursor + Enter bearbeiten, F2 = Dry-Run/Write)", max(1, width - 1), curses.A_DIM)
        rows = state["fields"]
        field_start = raw_heading + 1
        field_capacity = max(1, height - 1 - field_start)
        start = max(0, min(state["cursor"] - field_capacity // 2, max(0, len(rows) - field_capacity)))
        for row_no, field in enumerate(rows[start:start + field_capacity], start=field_start):
            idx = start + row_no - field_start
            attr = curses.A_REVERSE if idx == state["cursor"] else curses.A_NORMAL
            label = _safe(field.label)[:28]
            key = _safe(field.key)[:42]
            value = _safe(_edit_value(field))
            changed = "*" if field.key in state["changed"] else " "
            line = f"{changed} {label:<28} {key:<42} = {value}"
            stdscr.addnstr(row_no, 0, line, max(1, width - 1), attr)
    else:
        rows = state["fields"]
        start = max(0, min(state["cursor"] - max_rows // 2, max(0, len(rows) - max_rows)))
        for row_no, field in enumerate(rows[start:start + max_rows], start=4):
            idx = start + row_no - 4
            attr = curses.A_REVERSE if idx == state["cursor"] else curses.A_NORMAL
            label = _safe(field.label)[:28]
            key = _safe(field.key)[:42]
            value = _safe(field.value)
            unit = _safe(field.unit)
            changed = "*" if field.key in state["changed"] else " "
            line = f"{changed} {label:<28} {key:<42} = {value} {unit}"
            stdscr.addnstr(row_no, 0, line, max(1, width - 1), attr)

    write_scope = "all" if state["allowlist"].allow_all else str(len(state["allowlist"].keys))
    footer = f" raw={len(state['payload'])}B | write-scope={write_scope} | {state['block_index'] + 1}/{len(state['block_ids'])}"
    stdscr.addnstr(height - 1, 0, footer, max(1, width - 1), curses.A_DIM)
    stdscr.refresh()


def _load_block(service, state, block: int) -> None:
    with service.session() as session:
        result = service.read_block(session, block)
    if not result.ok:
        raise RuntimeError(f"block {block} read failed (status={result.status!r})")
    state["block"] = block
    state["payload"] = bytearray(result.payload)
    state["baseline"] = bytes(result.payload)
    state["history"] = service.pack.meldehist(result.payload) if block == 18 else None
    state["fields"] = [field for field in service.pack.display_fields(block, result.payload) if state["show_reserved"] or not is_reserved_key(field.key)]
    state["field_keys"] = {field.key for field in state["fields"]}
    state["changed"].clear()
    state["cursor"] = 0


def _edit(stdscr, service, state):
    field = state["fields"][state["cursor"]]
    height, width = stdscr.getmaxyx()
    prompt = f" {field.key} [{_edit_value(field)}] > "
    stdscr.move(height - 2, 0)
    stdscr.clrtoeol()
    stdscr.addnstr(height - 2, 0, prompt, max(1, width - 1))
    curses.echo()
    try:
        raw = stdscr.getstr(height - 2, min(len(prompt), max(0, width - 2)), max(1, width - len(prompt) - 2)).decode("utf-8", errors="replace")
    finally:
        curses.noecho()
    if not raw:
        state["message"] = "edit cancelled"
        return
    try:
        service.pack.encode_value(state["payload"], field.key, raw, block=state["block"])
        state["changed"].add(field.key)
        # Refresh the rendered field from the edited payload.
        state["fields"] = [field for field in service.pack.display_fields(state["block"], bytes(state["payload"])) if state["show_reserved"] or not is_reserved_key(field.key)]
        state["message"] = f"staged {field.key} (not written yet)"
    except Exception as exc:
        state["message"] = f"edit error: {exc}"


def _save(service, state):
    if not state["changed"]:
        state["message"] = "nothing changed"
        return
    with service.session() as session:
        if state["write_enabled"]:
            state["auth"] = service.authenticate(
                session, state["auth_level"], state["pass4"] or None
            )
            if not state["auth"].ok:
                raise RuntimeError(
                    f"requested auth level was not granted: {state['auth'].granted_level}"
                )
        audit = service.write_payload(
            session,
            state["block"],
            state["baseline"],
            bytes(state["payload"]),
            sorted(state["changed"]),
            state["allowlist"],
            dry_run=not state["write_enabled"],
        )
    if audit.written:
        state["baseline"] = bytes(state["payload"])
        state["changed"].clear()
    state["message"] = json.dumps(audit.as_dict(), ensure_ascii=False)


def _run(stdscr, service, state):
    curses.curs_set(0)
    stdscr.keypad(True)
    while True:
        _draw(stdscr, service, state)
        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), curses.KEY_F10, 27):
            return 0
        if key in (curses.KEY_UP, ord("k")):
            state["cursor"] = max(0, state["cursor"] - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            state["cursor"] = min(max(0, len(state["fields"]) - 1), state["cursor"] + 1)
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT) and len(state["block_ids"]) > 1:
            delta = -1 if key == curses.KEY_LEFT else 1
            state["block_index"] = (state["block_index"] + delta) % len(state["block_ids"])
            try:
                _load_block(service, state, state["block_ids"][state["block_index"]])
                state["message"] = "block loaded"
            except Exception as exc:
                state["message"] = str(exc)
        elif key in (ord("r"), ord("R")):
            try:
                _load_block(service, state, state["block"])
                state["message"] = "reloaded"
            except Exception as exc:
                state["message"] = str(exc)
        elif key in (curses.KEY_ENTER, 10, 13):
            _edit(stdscr, service, state)
        elif key in (curses.KEY_F2, 266, ord("s"), ord("S")):
            try:
                _save(service, state)
            except Exception as exc:
                state["message"] = f"save error: {exc}"


def run_tui(service, args) -> int:
    validate_block(args.block)
    if args.write_enabled:
        args.auth_level = validate_auth_level(
            args.auth_level, "--auth-level für Live-Schreiben"
        )
    elif args.auth_level != -1:
        args.auth_level = validate_auth_level(args.auth_level, "--auth-level")
    allowlist = WriteAllowlist(args.allowlist)
    block_ids = [args.block]
    if args.all_blocks:
        block_ids = sorted(set([args.block, 20, 22] + [b for b in service.pack.blocks() if 0 <= b <= 254]))
    state = {
        "block": args.block,
        "block_ids": block_ids,
        "block_index": block_ids.index(args.block),
        "payload": bytearray(),
        "baseline": b"",
        "fields": [],
        "field_keys": set(),
        "changed": set(),
        "cursor": 0,
        "allowlist": allowlist,
        "write_enabled": bool(args.write_enabled and not getattr(args, "dry_run", False)),
        "show_reserved": bool(args.show_reserved),
        "message": "starting",
        "auth": None,
        "auth_level": int(args.auth_level),
        "pass4": str(args.pass4 or ""),
    }
    if args.auth_level >= 0:
        with service.session() as session:
            state["auth"] = service.authenticate(session, args.auth_level, args.pass4 or None)
            if args.write_enabled and not state["auth"].ok:
                raise RuntimeError(f"requested auth level was not granted: {state['auth'].granted_level}")
    _load_block(service, state, args.block)
    return curses.wrapper(_run, service, state)
