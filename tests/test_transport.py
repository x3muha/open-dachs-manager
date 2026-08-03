import unittest

from open_dachs_manager.transport import (
    ProtocolError,
    destination_for_cpu,
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


if __name__ == "__main__":
    unittest.main()
