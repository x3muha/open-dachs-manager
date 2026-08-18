"""MSR2 privilege authentication and PW4 calculation."""

from __future__ import annotations

from dataclasses import dataclass

from .mapping import PackRepository
from .transport import SerialSession


AUTH_INPUT_BLOCK_LENGTHS = {20: 70, 22: 70}


def calculate_pw4(serial_number: str, operating_hours: int) -> str:
    """Calculate the four-digit daily MSR2 password (PW4).

    The verified controller calculation uses the last three serial-number
    characters, the constant 2749 and half the operating hours within the
    latest 10,000-hour window.  The 16-bit result contributes its last four
    decimal digits; the requested auth level is transmitted separately.
    """
    serial_text = str(serial_number).strip()
    serial_tail = int(serial_text[-3:]) if serial_text[-3:].isdigit() else 0
    dez_code = (serial_tail + 2749 + ((int(operating_hours) % 10000) // 2)) & 0xFFFF
    return f"{dez_code % 10000:04d}"


@dataclass(frozen=True)
class AuthInputs:
    """Values read from the controller that determine the current PW4."""

    serial_number: str
    operating_hours: int


def auth_inputs_from_payloads(
    pack: PackRepository,
    block20_payload: bytes,
    block22_payload: bytes,
) -> AuthInputs:
    """Derive PW4 inputs from an existing, complete controller capture.

    Maintenance backups already contain CPU-0 blocks 20 and 22.  Keeping this
    decoding separate from :func:`read_auth_inputs` prevents an otherwise
    easy-to-miss second read of both blocks while still applying the same
    fail-closed identity checks.
    """
    payload20 = bytes(block20_payload)
    payload22 = bytes(block22_payload)
    expected20 = AUTH_INPUT_BLOCK_LENGTHS[20]
    expected22 = AUTH_INPUT_BLOCK_LENGTHS[22]
    if len(payload20) != expected20:
        raise RuntimeError(
            f"block 20 payload has {len(payload20)} bytes, expected {expected20}"
        )
    if len(payload22) != expected22:
        raise RuntimeError(
            f"block 22 payload has {len(payload22)} bytes, expected {expected22}"
        )
    try:
        values20 = {item.key: item.raw for item in pack.decode(20, payload20)}
        values22 = {item.key: item.raw for item in pack.decode(22, payload22)}
    except Exception as exc:
        raise RuntimeError("cannot decode authentication inputs from blocks 20 and 22") from exc
    serial_number = str(values20.get("Hka_Bd_Stat.uchSeriennummer", "")).strip()
    raw_operating_seconds = values22.get("Hka_Bd.ulBetriebssekunden")
    if not serial_number:
        raise RuntimeError("block 20 did not contain a Dachs serial number")
    if raw_operating_seconds is None:
        raise RuntimeError("block 22 did not contain total operating seconds")
    try:
        operating_seconds = int(raw_operating_seconds)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("block 22 contained invalid total operating seconds") from exc
    if operating_seconds < 0:
        raise RuntimeError("block 22 contained negative total operating seconds")
    return AuthInputs(serial_number, operating_seconds // 3600)


def read_auth_inputs(
    session: SerialSession,
    pack: PackRepository,
    timeout: float = 0.9,
) -> AuthInputs:
    """Read the serial number and total operating hours without authenticating."""
    block20 = session.read_block(20, packet=None, timeout=timeout)
    block22 = session.read_block(22, packet=None, timeout=timeout)
    if not block20.ok or not block22.ok:
        raise RuntimeError("cannot read authentication inputs from blocks 20 and 22")
    # Keep interactive authentication compatible with controllers/tests that
    # expose only the decoded prefix.  The maintenance archive uses the strict
    # full-payload helper above because it must prove a complete 38-target
    # capture before it is allowed to become a recovery image.
    values20 = {item.key: item.raw for item in pack.decode(20, block20.payload)}
    values22 = {item.key: item.raw for item in pack.decode(22, block22.payload)}
    serial_number = str(values20.get("Hka_Bd_Stat.uchSeriennummer", "")).strip()
    operating_hours = int(values22.get("Hka_Bd.ulBetriebssekunden", 0) or 0) // 3600
    if not serial_number:
        raise RuntimeError("block 20 did not contain a Dachs serial number")
    return AuthInputs(serial_number, operating_hours)


@dataclass(frozen=True)
class AuthResult:
    serial_number: str
    operating_hours: int
    requested_level: int
    granted_level: int | None
    pw4: str
    response_hex: str | None

    @property
    def ok(self) -> bool:
        return self.granted_level is not None and self.granted_level >= self.requested_level

    def as_dict(self, reveal_secret: bool = False) -> dict:
        out = {
            "serial_number": self.serial_number,
            "operating_hours": self.operating_hours,
            "requested_level": self.requested_level,
            "granted_level": self.granted_level,
            "ok": self.ok,
            "response_hex": self.response_hex,
        }
        if reveal_secret:
            out["pw4"] = self.pw4
        return out


def authenticate(
    session: SerialSession,
    pack: PackRepository,
    level: int,
    pass4_override: str | None = None,
    timeout: float = 0.9,
) -> AuthResult:
    if not 0 <= int(level) <= 255:
        raise ValueError("auth level must be in range 0..255")
    inputs = read_auth_inputs(session, pack, timeout)
    serial_number = inputs.serial_number
    operating_hours = inputs.operating_hours
    pw4 = str(pass4_override or calculate_pw4(serial_number, operating_hours)).strip()
    if len(pw4) != 4 or not pw4.isdigit():
        raise ValueError("PW4 must contain exactly four digits")
    auth_payload = bytes([0x7E]) + pw4.encode("ascii") + bytes([int(level)])
    packet = session.next_packet() if hasattr(session, "next_packet") else 1
    response = session.request(auth_payload, packet=packet, timeout=timeout)
    granted = None
    if response.data and len(response.data.payload) >= 2 and response.data.payload[0] == 0xFE:
        granted = int(response.data.payload[1])
    return AuthResult(serial_number, operating_hours, int(level), granted, pw4, response.data.raw.hex(" ").upper() if response.data else None)
