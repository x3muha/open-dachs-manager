"""MSR2 privilege authentication and PW4 calculation."""

from __future__ import annotations

from dataclasses import dataclass

from .mapping import PackRepository
from .transport import SerialSession


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
