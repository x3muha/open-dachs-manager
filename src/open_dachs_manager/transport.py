"""Checked MSR2 serial transport for Open Dachs Manager."""

from __future__ import annotations

from dataclasses import dataclass
import time

try:
    import serial
except ImportError:  # pragma: no cover - exercised on missing dependency
    serial = None


class ProtocolError(ValueError):
    """The received telegram is structurally or cryptographically invalid."""


class TransportError(RuntimeError):
    """Serial transport could not be opened or used."""


def crc16_msr(data: bytes) -> int:
    poly = 0x1021
    value = 0
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ poly) & 0xFFFF if value & 0x8000 else (value << 1) & 0xFFFF
    return value


@dataclass(frozen=True)
class Frame:
    kind: str
    packet: int
    raw: bytes
    payload: bytes = b""
    positive: bool | None = None

    @property
    def status(self) -> int | None:
        return self.payload[0] if self.payload else None


@dataclass(frozen=True)
class Response:
    tx: bytes
    ack: Frame | None
    data: Frame | None
    elapsed_ms: float
    crc_errors: int = 0
    protocol_errors: int = 0


@dataclass(frozen=True)
class BlockResult:
    block: int
    packet: int
    response: Response
    status: int | None
    payload: bytes
    cpu: int = 0

    @property
    def ok(self) -> bool:
        return self.response.data is not None and self.status is not None


def validate_block(block: int, *, writable: bool = False) -> int:
    try:
        value = int(block)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid block: {block!r}") from exc
    if not 0 <= value <= 255:
        raise ValueError(f"block must be in range 0..255, got {value}")
    # The MSR2 write service addresses block + 1 in one byte.  Reject 255
    # instead of silently wrapping it to zero.
    if writable and value >= 255:
        raise ValueError("block 255 cannot be written through the one-byte write service")
    return value


def validate_cpu(cpu: int) -> int:
    try:
        value = int(cpu)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid CPU: {cpu!r}") from exc
    if not 0 <= value <= 15:
        raise ValueError(f"CPU must be in range 0..15, got {value}")
    return value


def destination_for_cpu(cpu: int, module: int = 1) -> int:
    cpu = validate_cpu(cpu)
    module = int(module)
    if not 0 <= module <= 15:
        raise ValueError(f"module must be in range 0..15, got {module}")
    return (module << 4) | cpu


def encode_data(payload: bytes, packet: int, src: int = 0x00, dst: int = 0x10) -> bytes:
    if not 0 <= packet <= 15:
        raise ValueError(f"packet must be in range 0..15, got {packet}")
    if len(payload) > 0xFFF:
        raise ValueError("MSR2 payload is limited to 4095 bytes")
    header = bytes([0x02, src, dst, (packet << 4) | ((len(payload) >> 8) & 0x0F), len(payload) & 0xFF])
    body = header + bytes(payload)
    crc = crc16_msr(body)
    return body + crc.to_bytes(2, "big")


def encode_ack(frame: Frame, positive: bool = True) -> bytes:
    if frame.kind != "data":
        raise ValueError("ACK can only be generated for a data frame")
    head = bytes([0x06 if positive else 0x15, frame.raw[3], frame.raw[4]])
    return head + crc16_msr(head).to_bytes(2, "big")


def parse_frame(raw: bytes) -> Frame:
    if not raw:
        raise ProtocolError("empty frame")
    if raw[0] in (0x06, 0x15):
        if len(raw) != 5:
            raise ProtocolError(f"invalid ACK length: {len(raw)}")
        expected = crc16_msr(raw[:3])
        actual = int.from_bytes(raw[3:5], "big")
        if actual != expected:
            raise ProtocolError(f"ACK CRC mismatch: expected {expected:04X}, got {actual:04X}")
        return Frame("ack", (raw[1] >> 4) & 0x0F, raw, positive=raw[0] == 0x06)

    if raw[0] != 0x02 or len(raw) < 7:
        raise ProtocolError("invalid data frame header")
    length = ((raw[3] & 0x0F) << 8) | raw[4]
    expected_length = 5 + length + 2
    if len(raw) != expected_length:
        raise ProtocolError(f"invalid data frame length: expected {expected_length}, got {len(raw)}")
    expected = crc16_msr(raw[:-2])
    actual = int.from_bytes(raw[-2:], "big")
    if actual != expected:
        raise ProtocolError(f"data CRC mismatch: expected {expected:04X}, got {actual:04X}")
    return Frame("data", (raw[3] >> 4) & 0x0F, raw, payload=raw[5:-2])


def _pop_complete_frame(buffer: bytearray) -> bytes | None:
    while buffer and buffer[0] not in (0x02, 0x06, 0x15):
        del buffer[0]
    if not buffer:
        return None
    if buffer[0] in (0x06, 0x15):
        if len(buffer) < 5:
            return None
        return bytes(buffer[:5])
    if len(buffer) < 5:
        return None
    length = ((buffer[3] & 0x0F) << 8) | buffer[4]
    if length > 0xFFF:
        del buffer[0]
        return None
    total = 5 + length + 2
    if len(buffer) < total:
        return None
    return bytes(buffer[:total])


class SerialSession:
    """Exclusive serial owner with checked frame parsing and packet handling."""

    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = 19200, read_timeout: float = 0.02):
        if read_timeout <= 0:
            raise ValueError("serial read timeout must be greater than zero")
        self.port = port
        self.baud = baud
        self.read_timeout = read_timeout
        self._serial = None
        self._buffer = bytearray()
        self._next_packet = 0

    def __enter__(self) -> "SerialSession":
        if serial is None:
            raise TransportError("pyserial is not installed")
        kwargs = dict(port=self.port, baudrate=self.baud, bytesize=8, parity="N", stopbits=1, timeout=self.read_timeout)
        try:
            self._serial = serial.Serial(exclusive=True, **kwargs)
        except TypeError:  # pragma: no cover - old pyserial fallback
            try:
                self._serial = serial.Serial(**kwargs)
            except Exception as exc:
                raise TransportError(f"cannot open serial port {self.port}: {exc}") from exc
        except Exception as exc:
            raise TransportError(f"cannot open serial port {self.port}: {exc}") from exc
        self._buffer.clear()
        self._next_packet = 0
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._serial is not None:
            self._serial.close()
        self._serial = None
        self._buffer.clear()

    def next_packet(self) -> int:
        """Reserve the next packet number for a service exchange."""
        packet = self._next_packet
        self._next_packet = (packet + 1) & 0x0F
        return packet

    @property
    def is_open(self) -> bool:
        return self._serial is not None and bool(self._serial.is_open)

    def request(self, payload: bytes, packet: int, timeout: float = 0.9, cpu: int = 0) -> Response:
        if not self.is_open:
            raise TransportError("serial session is not open")
        if timeout <= 0:
            raise ValueError("request timeout must be greater than zero")
        cpu = validate_cpu(cpu)
        tx = encode_data(payload, packet, dst=destination_for_cpu(cpu))
        started = time.monotonic()
        try:
            self._serial.write(tx)
        except Exception as exc:
            raise TransportError(f"serial write failed on {self.port}: {exc}") from exc
        deadline = started + timeout
        ack = None
        data = None
        crc_errors = 0
        protocol_errors = 0
        while time.monotonic() < deadline:
            try:
                chunk = self._serial.read(512)
            except Exception as exc:
                raise TransportError(f"serial read failed on {self.port}: {exc}") from exc
            if chunk:
                self._buffer.extend(chunk)
            while True:
                raw = _pop_complete_frame(self._buffer)
                if raw is None:
                    break
                del self._buffer[:len(raw)]
                try:
                    frame = parse_frame(raw)
                except ProtocolError as exc:
                    if "CRC mismatch" in str(exc):
                        crc_errors += 1
                    else:
                        protocol_errors += 1
                    continue
                if frame.kind == "ack" and frame.packet == packet:
                    ack = frame
                elif frame.kind == "data":
                    data = frame
                    try:
                        self._serial.write(encode_ack(frame))
                    except Exception:
                        pass
            # A sync service has no response data; do not burn the complete
            # timeout after its positive ACK already arrived.
            if data is not None or (ack is not None and not payload):
                break
        return Response(tx, ack, data, (time.monotonic() - started) * 1000.0, crc_errors, protocol_errors)

    def sync(self, packet: int = 0, timeout: float = 0.9, cpu: int = 0) -> Response:
        return self.request(b"", packet, timeout, cpu=cpu)

    def read_block(self, block: int, packet: int | None = None, timeout: float = 0.9,
                   cpu: int = 0) -> BlockResult:
        block = validate_block(block)
        cpu = validate_cpu(cpu)
        if packet is None:
            packet = self.next_packet()
            request_packet = self.next_packet()
        else:
            request_packet = (packet + 1) & 0x0F
        self.sync(packet, timeout, cpu=cpu)
        response = self.request(bytes([block]), request_packet, timeout, cpu=cpu)
        data = response.data.payload if response.data else b""
        return BlockResult(
            block, request_packet, response, data[0] if data else None,
            data[1:] if len(data) > 1 else b"", cpu,
        )

    def write_block(self, block: int, payload: bytes, packet: int | None = None,
                    timeout: float = 0.9, cpu: int = 0) -> Response:
        block = validate_block(block, writable=True)
        cpu = validate_cpu(cpu)
        if packet is None:
            packet = self.next_packet()
            request_packet = self.next_packet()
        else:
            request_packet = (packet + 1) & 0x0F
        self.sync(packet, timeout, cpu=cpu)
        # This is the existing MSR2 write service semantics.  The safety
        # policy and readback decision live above this transport primitive.
        return self.request(bytes([block + 1]) + bytes(payload), request_packet, timeout, cpu=cpu)
