"""Fail-closed rules for the adjustable SC123 temperature limit.

The firmware capability is proved dynamically by a +1 heartbeat in Block 50
(offset 31). The persistent setting is a versioned six-byte record in the
otherwise reserved tail of Block 60: 53 43 31 01 value complement at
offsets 40..45. Invalid records mean the firmware's safe 180 °C fallback;
they never make an unversioned reserve byte authoritative.

Nothing in this module owns or opens the serial adapter.
"""
from __future__ import annotations

import re

SC123_LIMIT_SCHEMA = "open-dachs-sc123-temperature-limit/v2"
SC123_LIMIT_CAPABILITY_BLOCK = 50
SC123_LIMIT_STORAGE_BLOCK = 60
SC123_LIMIT_PAYLOAD_LENGTH = 70
SC123_LIMIT_HEARTBEAT_OFFSET = 31
SC123_LIMIT_RECORD_OFFSET = 40
SC123_LIMIT_RECORD_LENGTH = 6
SC123_LIMIT_MAGIC = bytes.fromhex("53 43 31 01")
SC123_LIMIT_THRESHOLD_OFFSET = 44
SC123_LIMIT_COMPLEMENT_OFFSET = 45
SC123_LIMIT_MIN_C = 180
SC123_LIMIT_MAX_C = 240
SC123_LIMIT_FALLBACK_C = 180
SC123_LIMIT_AUTH_LEVEL = 3
SC123_LIMIT_TOKEN_TTL_SECONDS = 60.0
SC123_LIMIT_RETRY_GUARD_SECONDS = 30.0
SC123_LIMIT_NVM_ADDRESS = 0x01B4
SC123_LIMIT_NVM_POLL_ATTEMPTS = 5
SC123_LIMIT_NVM_POLL_DELAY_SECONDS = 0.1
SC123_LIMIT_NVM_READ_REQUEST = bytes(
    [
        0x04,
        (SC123_LIMIT_NVM_ADDRESS >> 8) & 0xFF,
        SC123_LIMIT_NVM_ADDRESS & 0xFF,
        SC123_LIMIT_RECORD_LENGTH,
    ]
)
SC123_LIMIT_NVM_READ_PREFIX = bytes([0x84]) + SC123_LIMIT_NVM_READ_REQUEST[1:]

# Block-50 values that legitimately move across fresh reads. Offset 31 moves
# only on the adjustable firmware and is also the positive capability proof.
SC123_LIMIT_TIME_OFFSETS = frozenset(range(36, 40))
SC123_LIMIT_GUARD_OFFSET = 54
SC123_LIMIT_CAPABILITY_VOLATILE_OFFSETS = (
    SC123_LIMIT_TIME_OFFSETS
    | {SC123_LIMIT_HEARTBEAT_OFFSET, SC123_LIMIT_GUARD_OFFSET}
)

# The Block-50 write handler interprets these bits as one-shot commands. A
# separate Block-60 write must not race with an already pending controller
# command either.
SC123_LIMIT_COMMAND_MASKS = {
    0: 0x21,   # 0x1acc bits 0 and 5
    18: 0x30,  # 0x1ade bits 4 and 5
}


def parse_sc123_limit_c(value: object) -> int:
    """Return a strict whole-degree threshold inside 180..240 C."""
    if isinstance(value, bool):
        raise ValueError("SC123-Grenze muss eine ganze Temperatur sein")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"\s*[0-9]+\s*", value):
        result = int(value)
    else:
        raise ValueError("SC123-Grenze muss eine ganze Temperatur sein")
    if not SC123_LIMIT_MIN_C <= result <= SC123_LIMIT_MAX_C:
        raise ValueError(
            f"SC123-Grenze muss zwischen {SC123_LIMIT_MIN_C} und "
            f"{SC123_LIMIT_MAX_C} °C liegen"
        )
    return result


def _payload(payload: bytes | bytearray, label: str, block: int) -> bytes:
    result = bytes(payload)
    if len(result) != SC123_LIMIT_PAYLOAD_LENGTH:
        raise ValueError(
            f"{label}: Block {block} hat {len(result)} statt "
            f"{SC123_LIMIT_PAYLOAD_LENGTH} Byte"
        )
    return result


def stable_sc123_block50_equal(left: bytes, right: bytes) -> bool:
    """Compare Block 50 while ignoring only proven read-time volatility."""
    if len(left) != len(right) or len(left) != SC123_LIMIT_PAYLOAD_LENGTH:
        return False
    return all(
        left[index] == right[index]
        for index in range(SC123_LIMIT_PAYLOAD_LENGTH)
        if index not in SC123_LIMIT_CAPABILITY_VOLATILE_OFFSETS
    )


def stable_sc123_block60_equal(left: bytes, right: bytes) -> bool:
    """Block 60 is persistent configuration and must compare byte-exactly."""
    return (
        len(left) == len(right) == SC123_LIMIT_PAYLOAD_LENGTH
        and bytes(left) == bytes(right)
    )


def sc123_limit_record(payload: bytes | bytearray) -> dict:
    """Decode the versioned Block-60 record and its safe fallback semantics."""
    payload = _payload(payload, "SC123-Speicher", SC123_LIMIT_STORAGE_BLOCK)
    record = payload[
        SC123_LIMIT_RECORD_OFFSET:
        SC123_LIMIT_RECORD_OFFSET + SC123_LIMIT_RECORD_LENGTH
    ]
    value = record[4]
    complement = record[5]
    magic_ok = record[:4] == SC123_LIMIT_MAGIC
    range_ok = SC123_LIMIT_MIN_C <= value <= SC123_LIMIT_MAX_C
    complement_ok = (value ^ complement) == 0xFF
    valid = magic_ok and range_ok and complement_ok
    return {
        "record_hex": record.hex(),
        "magic_ok": magic_ok,
        "range_ok": range_ok,
        "complement_ok": complement_ok,
        "configured": valid,
        "fallback_active": not valid,
        "threshold_c": value if valid else SC123_LIMIT_FALLBACK_C,
        "stored_threshold_byte": value,
        "stored_complement_byte": complement,
    }


def sc123_limit_capability(
    block50_first: bytes,
    block50_second: bytes,
    block60_first: bytes,
    block60_second: bytes,
) -> dict:
    """Validate two reads of both blocks and return a fail-closed state."""
    block50_first = _payload(
        block50_first, "erste SC123-Fähigkeitslesung", SC123_LIMIT_CAPABILITY_BLOCK
    )
    block50_second = _payload(
        block50_second, "zweite SC123-Fähigkeitslesung", SC123_LIMIT_CAPABILITY_BLOCK
    )
    block60_first = _payload(
        block60_first, "erste SC123-Speicherlesung", SC123_LIMIT_STORAGE_BLOCK
    )
    block60_second = _payload(
        block60_second, "zweite SC123-Speicherlesung", SC123_LIMIT_STORAGE_BLOCK
    )
    heartbeat_first = block50_first[SC123_LIMIT_HEARTBEAT_OFFSET]
    heartbeat_second = block50_second[SC123_LIMIT_HEARTBEAT_OFFSET]
    heartbeat_ok = heartbeat_second == ((heartbeat_first + 1) & 0xFF)
    capability_stable = stable_sc123_block50_equal(block50_first, block50_second)
    storage_stable = stable_sc123_block60_equal(block60_first, block60_second)
    firmware_capability = heartbeat_ok and capability_stable
    available = firmware_capability and storage_stable
    record = sc123_limit_record(block60_second)
    blockers = []
    if not heartbeat_ok:
        blockers.append(
            "kein dynamischer Firmware-Heartbeat; einstellbarer SC123-Patch nicht sicher erkannt"
        )
    if not capability_stable:
        blockers.append("Block 50 änderte sich außerhalb der erlaubten Laufzeitfelder")
    if not storage_stable:
        blockers.append("Block 60 war über zwei frische Lesungen nicht bytegenau stabil")
    return {
        "schema": SC123_LIMIT_SCHEMA,
        "available": available,
        "firmware_capability": firmware_capability,
        **record,
        "record_valid": record["configured"],
        "fallback_c": SC123_LIMIT_FALLBACK_C,
        "minimum_c": SC123_LIMIT_MIN_C,
        "maximum_c": SC123_LIMIT_MAX_C,
        "comparison": "Abgastemperatur > Grenzwert, länger als 11 s",
        "heartbeat_first": heartbeat_first,
        "heartbeat_second": heartbeat_second,
        "heartbeat_ok": heartbeat_ok,
        "stable_reads": capability_stable,
        "capability_stable_reads": capability_stable,
        "storage_stable_reads": storage_stable,
        "blockers": blockers,
    }


def sc123_limit_pending_commands(block50_payload: bytes) -> list[str]:
    block50_payload = _payload(
        block50_payload, "SC123-Vorprüfung", SC123_LIMIT_CAPABILITY_BLOCK
    )
    pending = []
    for offset, mask in SC123_LIMIT_COMMAND_MASKS.items():
        active = block50_payload[offset] & mask
        if active:
            pending.append(f"Block 50 Offset {offset}: 0x{active:02x}")
    return pending


def sc123_limit_record_bytes(threshold_c: object) -> bytes:
    threshold = parse_sc123_limit_c(threshold_c)
    return SC123_LIMIT_MAGIC + bytes([threshold, threshold ^ 0xFF])


def sc123_limit_payload(block60_before: bytes, threshold_c: object) -> bytes:
    """Replace only the dedicated six-byte record in a fresh Block-60 image."""
    before = _payload(
        block60_before, "SC123-Schreibbasis", SC123_LIMIT_STORAGE_BLOCK
    )
    record = sc123_limit_record_bytes(threshold_c)
    after = bytearray(before)
    after[
        SC123_LIMIT_RECORD_OFFSET:
        SC123_LIMIT_RECORD_OFFSET + SC123_LIMIT_RECORD_LENGTH
    ] = record
    changed = {
        index for index, pair in enumerate(zip(before, after)) if pair[0] != pair[1]
    }
    allowed = set(
        range(
            SC123_LIMIT_RECORD_OFFSET,
            SC123_LIMIT_RECORD_OFFSET + SC123_LIMIT_RECORD_LENGTH,
        )
    )
    if not changed <= allowed:
        raise AssertionError("SC123-Payload änderte unerwartete Bytes")
    return bytes(after)


def sc123_limit_readback(expected: bytes, actual: bytes) -> dict:
    """Validate the one allowed full Block-60 RAM readback byte-exactly."""
    expected = _payload(expected, "SC123-Schreibpayload", SC123_LIMIT_STORAGE_BLOCK)
    actual = _payload(actual, "SC123-Rücklesung", SC123_LIMIT_STORAGE_BLOCK)
    expected_record = expected[
        SC123_LIMIT_RECORD_OFFSET:
        SC123_LIMIT_RECORD_OFFSET + SC123_LIMIT_RECORD_LENGTH
    ]
    actual_record = actual[
        SC123_LIMIT_RECORD_OFFSET:
        SC123_LIMIT_RECORD_OFFSET + SC123_LIMIT_RECORD_LENGTH
    ]
    exact = expected == actual
    return {
        "ok": exact,
        "record_ok": expected_record == actual_record,
        "stable_fields_ok": exact,
        "record_hex": actual_record.hex(),
        "threshold_c": actual[SC123_LIMIT_THRESHOLD_OFFSET],
    }


def sc123_limit_nvm_record(response) -> bytes:
    """Return an exact service-0x04 NVM record response or fail closed."""
    ack = getattr(response, "ack", None)
    if ack is None or getattr(ack, "positive", None) is not True:
        raise ValueError("SC123-NVM-Lesung erhielt kein positives ACK")
    data = getattr(response, "data", None)
    body = bytes(getattr(data, "payload", b"")) if data is not None else b""
    expected_length = len(SC123_LIMIT_NVM_READ_PREFIX) + SC123_LIMIT_RECORD_LENGTH
    if len(body) != expected_length:
        raise ValueError(
            f"SC123-NVM-Lesung lieferte {len(body)} statt {expected_length} Byte"
        )
    if not body.startswith(SC123_LIMIT_NVM_READ_PREFIX):
        raise ValueError("SC123-NVM-Lesung lieferte kein exaktes Service-0x04-Echo")
    return body[len(SC123_LIMIT_NVM_READ_PREFIX):]
