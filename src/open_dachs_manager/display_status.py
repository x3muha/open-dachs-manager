"""MSR2 MMI start page: status lines 1 and 2, recomputed from controller data.

The main-controller firmware (R 000.004.043) builds both lines on every
screen refresh from raw RAM bytes; nothing in the block protocol carries the
resulting text.  This module reproduces the decision chain exactly as
documented in ``reports/display-statuszeilen-firmwareanalyse-2026-10-24.md``:

* line 1: index byte ``0x00291c``, routine ``0xf5edf3``, table ``0xf3c2b0``
* line 2: index byte ``0x00291d``, routine ``0xf5eebf``, table ``0xf3c390``;
  an active service code (``0x0e91 != 0``) switches to ``0xf5f0a8`` and the
  service-code table ``0xf3c5ac``

Every later assignment overwrites an earlier one, so the statements below
keep the firmware's execution order; the comments cite the instruction
address they reproduce.  Inputs come from the already polled block payloads
(a block payload is a contiguous RAM copy) plus a few one-byte CPU-0 RAM
reads.  A missing or stale input makes the affected line unavailable; an old
value is never reused.
"""

from __future__ import annotations

import json
from pathlib import Path

DISPLAY_STATUS_SCHEMA = "open-dachs-display-status/v1"
DISPLAY_STATUS_TEXTS_PATH = Path(__file__).resolve().parent / "data" / "display_status_texts_de.json"

# Block payload = RAM[base .. base+69] (block service switch 0xf47d2d).
DISPLAY_STATUS_BLOCK_BASES = {22: 0x0E7A, 24: 0x0ED4, 26: 0x0F1A, 50: 0x1ACC, 104: 0x1B12}
DISPLAY_STATUS_BLOCK_INPUTS = {
    22: (0x0E91, 0x0E9E, 0x0E9F, 0x0EA0, 0x0EA1, 0x0EA4),
    24: (0x0ED4, 0x0EFA, 0x0EFF, 0x0F0A),
    26: (0x0F33,),
    50: (0x1ADA, 0x1ADC, 0x1ADD, 0x1AE2, 0x1AF4),
    104: (0x1B12,),
}
DISPLAY_STATUS_FAST_BLOCKS = (22, 24, 26)
DISPLAY_STATUS_SLOW_BLOCKS = (50, 104)
# Bytes only reachable through CPU-0 RAM service 0x02 (0x0f96 lies in block
# 76, which ODM does not poll).  Fixed allowlist, one byte each.
DISPLAY_STATUS_RAM_ADDRESSES = (0x0DB3, 0x0F96, 0x1BCC, 0x1C9F, 0x1B5C, 0x1ED4)
DISPLAY_STATUS_ALWAYS_RAM = (0x0DB3, 0x0F96, 0x1BCC)
DISPLAY_STATUS_INPUT_ADDRESSES = tuple(sorted(
    {address for addresses in DISPLAY_STATUS_BLOCK_INPUTS.values() for address in addresses}
    | set(DISPLAY_STATUS_RAM_ADDRESSES)
))
SERVICE_CODE_ALTERNATIVES = {0x16: 0xF0, 0x18: 0xF1}


def _load_texts() -> dict:
    data = json.loads(DISPLAY_STATUS_TEXTS_PATH.read_text(encoding="utf-8"))
    if len(data["line1"]) != 14 or len(data["line2"]) != 34 or len(data["service_codes"]) != 256:
        raise ValueError("Display-Statustexte: unerwartete Tabellengröße")
    return data


DISPLAY_STATUS_TEXTS = _load_texts()
LINE1_TEXTS = tuple(DISPLAY_STATUS_TEXTS["line1"])
LINE2_TEXTS = tuple(DISPLAY_STATUS_TEXTS["line2"])
SERVICE_CODE_TEXTS = tuple(DISPLAY_STATUS_TEXTS["service_codes"])


def address_key(address: int) -> str:
    return f"0x{int(address):04x}"


class MissingDisplayInput(KeyError):
    """A byte the firmware branch needs is missing or stale."""

    def __init__(self, address: int):
        super().__init__(address)
        self.address = int(address)


class _Ram:
    """Read-only byte view that raises for any absent input."""

    def __init__(self, values: dict):
        self._values = values

    def __getitem__(self, address: int) -> int:
        value = self._values.get(address)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
            raise MissingDisplayInput(address)
        return value

    def bit(self, address: int, number: int) -> bool:
        return bool(self[address] >> number & 1)


def line1_index(values: dict) -> int:
    """Status line 1 index (0x00291c) in firmware execution order."""
    ram = values if isinstance(values, _Ram) else _Ram(values)
    idx = 0                                                   # f5edf3
    if not ram.bit(0x0EA1, 6):                                # f5edf8 BTST 6 ; JNE f5ee02
        idx = 1                                               # f5ee85 Dachs ausgeschaltet
    elif ram[0x0E91] != 0:                                    # f5ee02 service code active
        idx = 9 if ram[0x1C9F] == 1 else 10                   # f5ee6f..f5ee7e
    else:
        status = ram[0x0ED4]
        if status == 0x14:                                    # f5ee0f STZ
            idx = 2
        if status == 0x15:                                    # f5ee1b STZ
            idx = 3
        if status >= 0x16:                                    # f5ee21 JLTU / f5ee29
            idx = 4
        if not ram.bit(0x0EA0, 0):                            # f5ee2e STZ
            idx = 6
        release = ram[0x0EA0] | ram[0x0EA1] << 8              # f5ee39 MOV.W
        if release not in (0xFFFE, 0xFFFF):                   # f5ee3e..f5ee44 STNZ
            idx = 8
        if not ram.bit(0x0EA1, 1):                            # f5ee4a STZ
            idx = 7
        if not ram.bit(0x0EA0, 1):                            # f5ee55 STZ
            idx = 5
        if not ram.bit(0x0EA1, 2):                            # f5ee60 JNE / f5ee67
            idx = 13
    if not ram.bit(0x0EA1, 7):                                # f5ee8a STZ IB erforderlich
        idx = 11
    if ram[0x1BCC] == 1:                                      # f5ee95 STZ Platine getauscht
        idx = 12
    return idx


def line2_result(values: dict) -> tuple[str, int]:
    """Return ``("status", index)`` or ``("servicecode", table_index)``."""
    ram = values if isinstance(values, _Ram) else _Ram(values)
    if ram[0x0E91] != 0:                                      # f5eec4 -> f5f0a8
        code = ram[0x0E91]
        if code in SERVICE_CODE_ALTERNATIVES and ram.bit(0x1B5C, 0):   # f5f0ae
            code = SERVICE_CODE_ALTERNATIVES[code]            # f5f0b9 / f5f0bf
        return "servicecode", code
    idx = 0                                                   # f5eebf
    multi_module = ram[0x1ADD]
    if multi_module != 0 and (ram[0x1AF4] & 0x0F) != 1:      # f5eece..f5eede
        idx = 9                                               # f5ef54 Dachs Modulregler
    else:
        storage, type_code = ram[0x1ADA], ram[0x0F0A]         # f5eee0 / f5eee5
        se = storage in (1, 2)
        if type_code & 0x80:                                  # f5eeea gas
            g50 = type_code == 0x90
            idx = (6 if g50 else 5) if se else (2 if g50 else 1)
        elif type_code & 0x08:                                # f5ef1f oil
            rs = type_code == 0x0A
            idx = (8 if rs else 7) if se else (4 if rs else 3)
    if ram[0x1B12] & 0x0C == 0x04:                            # f5ef5a..f5ef63
        idx = 31
    demand = ram[0x0E9E] | ram[0x0E9F] << 8
    if demand != 0:                                           # f5ef69
        if demand & 0x0001:                                   # f5ef73
            idx = 22
        if demand & 0x000C:                                   # f5ef7e
            idx = 13
        if demand & 0x0002:                                   # f5ef8b
            idx = 10
        if demand & 0x0040:                                   # f5ef96
            power_flags = ram[0x0EA4]
            if power_flags & 0x02:                            # f5ef9d
                idx = 14
            if power_flags & 0x24:                            # f5efa8
                idx = 15
            if power_flags & 0x18:                            # f5efb5
                idx = 16
        if multi_module == 1 and (ram[0x1AF4] & 0x0F) != 1:   # f5efc2..f5efd2 exact == 1
            if demand & 0x0080:                               # f5efd4
                idx = 17
            if ram[0x1ED4] == 1:                              # f5efdf exact == 1
                idx = 18
        if demand & 0x0100:                                   # f5efea (0x0e9f bit 0)
            idx = 32
    if ram[0x0ED4] >= 0x20 and ram.bit(0x0EFA, 0):            # f5eff5..f5f002
        idx = 19
    if not ram.bit(0x0EFF, 3) and ram[0x1ADC] != 0:           # f5f008..f5f014
        idx = 21 if ram[0x1AE2] & 0xE0 == 0x20 else 20        # f5f016..f5f01f STZX
    if ram[0x0DB3] != 0:                                      # f5f026
        idx = 23
    if ram.bit(0x0F96, 1):                                    # f5f031
        idx = 33
    if ram.bit(0x0E9E, 5):                                    # f5f03c
        idx = 24
    if not ram.bit(0x0EA0, 6):                                # f5f047
        idx = 25
    if not ram.bit(0x0EA0, 5):                                # f5f052
        idx = 26
    if not ram.bit(0x0EA0, 2):                                # f5f05d
        idx = 27
    if not ram.bit(0x0EA1, 5):                                # f5f068
        idx = 28
    if not ram.bit(0x0EA1, 4):                                # f5f073
        idx = 29
    if not ram.bit(0x0F33, 4):                                # f5f07e
        idx = 30
    return "status", idx


def inputs_from_payloads(payloads: dict) -> dict[int, int]:
    """Pick the documented input bytes out of raw block payloads."""
    values: dict[int, int] = {}
    for block, addresses in DISPLAY_STATUS_BLOCK_INPUTS.items():
        payload = payloads.get(block)
        if payload is None:
            continue
        payload = bytes(payload)
        base = DISPLAY_STATUS_BLOCK_BASES[block]
        for address in addresses:
            offset = address - base
            if 0 <= offset < len(payload):
                values[address] = payload[offset]
    return values


def ram_reads_needed(values: dict) -> tuple[int, ...]:
    """RAM-only addresses the current block inputs make the firmware consult.

    ``0x0db3``, ``0x0f96`` and ``0x1bcc`` are always read.  The conditional
    bytes are only requested on the branch that evaluates them; without the
    deciding block inputs no conditional read is issued.
    """
    ram = _Ram(values)
    needed = list(DISPLAY_STATUS_ALWAYS_RAM)
    try:
        code = ram[0x0E91]
    except MissingDisplayInput:
        return tuple(needed)
    if code != 0:
        try:
            if ram.bit(0x0EA1, 6):                            # line 1 branch B only when switched on
                needed.append(0x1C9F)
        except MissingDisplayInput:
            pass
        if code in SERVICE_CODE_ALTERNATIVES:
            needed.append(0x1B5C)
        return tuple(needed)
    try:
        demand = ram[0x0E9E] | ram[0x0E9F] << 8
        if demand != 0 and ram[0x1ADD] == 1 and (ram[0x1AF4] & 0x0F) != 1:
            needed.append(0x1ED4)
    except MissingDisplayInput:
        pass
    return tuple(needed)


def display_status_unavailable(error=None, recorded_at=None, inputs=None) -> dict:
    return {
        "schema": DISPLAY_STATUS_SCHEMA,
        "available": False,
        "line1": None,
        "line2": None,
        "line1_available": False,
        "line2_available": False,
        "line1_index": None,
        "line2_index": None,
        "line2_kind": None,
        "service_code": None,
        "service_code_index": None,
        "inputs": {address_key(address): None for address in DISPLAY_STATUS_INPUT_ADDRESSES}
        if inputs is None else inputs,
        "missing_inputs": [],
        "source": "berechnet aus Reglerdaten",
        "firmware": "R 000.004.043",
        "recorded_at": recorded_at,
        "error": None if error is None else str(error),
    }


def compute_display_status(values: dict, recorded_at=None, input_errors: dict | None = None) -> dict:
    """Both lines plus raw inputs; each line fails closed on its own."""
    input_errors = dict(input_errors or {})
    inputs = {
        address_key(address): (
            values.get(address)
            if isinstance(values.get(address), int) and not isinstance(values.get(address), bool)
            else None
        )
        for address in DISPLAY_STATUS_INPUT_ADDRESSES
    }
    result = display_status_unavailable(recorded_at=recorded_at, inputs=inputs)
    errors: list[str] = []
    missing: list[str] = []

    def describe(address: int) -> str:
        key = address_key(address)
        if key not in missing:
            missing.append(key)
        reason = input_errors.get(address) or "nicht verfügbar"
        return f"Eingang {key}: {reason}"

    try:
        index = line1_index(values)
        result.update(line1=LINE1_TEXTS[index], line1_index=index, line1_available=True)
    except MissingDisplayInput as exc:
        errors.append("Zeile 1: " + describe(exc.address))
    try:
        kind, index = line2_result(values)
        if kind == "servicecode":
            result.update(
                line2=SERVICE_CODE_TEXTS[index],
                line2_kind="servicecode",
                service_code=100 + int(values[0x0E91]),
                service_code_index=index,
                line2_available=True,
            )
        else:
            result.update(
                line2=LINE2_TEXTS[index], line2_kind="status", line2_index=index, line2_available=True
            )
    except MissingDisplayInput as exc:
        errors.append("Zeile 2: " + describe(exc.address))
    result["available"] = bool(result["line1_available"] and result["line2_available"])
    result["missing_inputs"] = missing
    result["error"] = "; ".join(errors) or None
    return result
