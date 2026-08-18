import unittest
from collections import deque

from open_dachs_manager.transport import (
    ProtocolError,
    SerialSession,
    destination_for_cpu,
    encode_ack,
    encode_data,
    parse_frame,
    validate_block,
    validate_cpu,
)


class TransportTests(unittest.TestCase):
    def test_data_frame_roundtrip(self):
        frame = encode_data(b"abc", 3)
        parsed = parse_frame(frame)
        self.assertEqual(parsed.kind, "data")
        self.assertEqual(parsed.packet, 3)
        self.assertEqual(parsed.payload, b"abc")
        self.assertEqual(parsed.source_address, 0x00)
        self.assertEqual(parsed.destination_address, 0x10)

    def test_bad_crc_is_rejected(self):
        frame = bytearray(encode_data(b"abc", 3))
        frame[-1] ^= 0x01
        with self.assertRaises(ProtocolError):
            parse_frame(bytes(frame))

    def test_block_range_is_not_truncated(self):
        with self.assertRaises(ValueError):
            validate_block(256)
        with self.assertRaises(ValueError):
            validate_block(255, writable=True)

    def test_cpu_destination_uses_separate_low_nibble_namespace(self):
        self.assertEqual(destination_for_cpu(0), 0x10)
        self.assertEqual(destination_for_cpu(1), 0x11)
        self.assertEqual(destination_for_cpu(2), 0x12)
        self.assertEqual(encode_data(b"\x10", 1, dst=destination_for_cpu(2))[2], 0x12)
        with self.assertRaises(ValueError):
            validate_cpu(16)

    def test_request_ignores_data_from_wrong_cpu_or_destination(self):
        class AddressedFakeSerial:
            def __init__(self):
                self.is_open = True
                self.writes = []
                self.read_queue = deque()

            def write(self, data):
                self.writes.append(bytes(data))
                if not data or data[0] != 0x02:
                    return len(data)
                request = parse_frame(bytes(data))
                self.read_queue.append(encode_ack(request))
                if request.payload:
                    # Both foreign frames are valid and must be ACKed, but
                    # neither may satisfy a CPU-2 request.
                    self.read_queue.append(encode_data(b"\x00\x11", 3, src=0x11, dst=0x00))
                    self.read_queue.append(encode_data(b"\x00\x22", 4, src=0x12, dst=0x01))
                    # The response packet deliberately differs from the
                    # request: the controller has an independent TX sequence.
                    self.read_queue.append(encode_data(b"\x00\xAA", 5, src=0x12, dst=0x00))
                return len(data)

            def read(self, _size):
                return self.read_queue.popleft() if self.read_queue else b""

        fake = AddressedFakeSerial()
        session = SerialSession(port="fake", read_timeout=0.001)
        session._serial = fake

        result = session.read_block(16, packet=0, timeout=0.05, cpu=2)

        self.assertTrue(result.ok)
        self.assertEqual(result.cpu, 2)
        self.assertEqual(result.payload, b"\xAA")
        self.assertEqual(result.response.data.packet, 5)
        self.assertEqual(result.response.protocol_errors, 2)
        response_acks = [parse_frame(raw) for raw in fake.writes if raw and raw[0] == 0x06]
        self.assertEqual([frame.packet for frame in response_acks], [3, 4, 5])

    def test_layout4_config_write_uses_service_21_and_exact_59_byte_payload(self):
        class AckingSerial:
            def __init__(self):
                self.is_open = True
                self.writes = []
                self.read_queue = deque()

            def write(self, data):
                self.writes.append(bytes(data))
                if data and data[0] == 0x02:
                    self.read_queue.append(encode_ack(parse_frame(bytes(data))))
                return len(data)

            def read(self, _size):
                return self.read_queue.popleft() if self.read_queue else b""

        fake = AckingSerial()
        session = SerialSession(port="fake", read_timeout=0.001)
        session._serial = fake
        config = bytes(range(59))

        response = session.write_block(20, config, packet=0, timeout=0.05, cpu=1)

        self.assertTrue(response.ack.positive)
        frames = [parse_frame(raw) for raw in fake.writes if raw[0] == 0x02]
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].payload, b"")
        self.assertEqual(frames[1].payload, b"\x15" + config)
        self.assertEqual(frames[1].destination_address, 0x11)


if __name__ == "__main__":
    unittest.main()
