import struct
import sys
import unittest
from unittest.mock import patch

from open_dachs_manager.firmware import inspect_muc


def fixture_unpacked():
    chunks = []
    for entry_id in (4, 5, 6, 7, 0, 1, 2, 3):
        chunk = bytearray(0x140)
        chunk[:8] = b"\x01\x00\x01\x00" + struct.pack("<I", entry_id)
        source = f"\\release\\module-{entry_id}.hex".encode("ascii") + b"\x00"
        chunk[0x106:0x106 + len(source)] = source
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def container(unpacked_size, packed=b"abc"):
    return b"CUM\x1a" + struct.pack("<III", 16, unpacked_size, len(packed)) + packed


def python_command(source):
    return (sys.executable, "-c", source)


class FirmwareInspectionTests(unittest.TestCase):
    def test_inspects_eight_modules_without_serial_or_storage(self):
        unpacked = fixture_unpacked()
        command = python_command(f"import sys; sys.stdout.buffer.write({unpacked!r})")
        with patch("open_dachs_manager.firmware.MUC_DECOMPRESS_COMMAND", command):
            result = inspect_muc(
                container(len(unpacked)),
                "Software_MSR2_DE_024.muc",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["entry_count"], 8)
        self.assertEqual(result["entries"][0]["module"], "Regler")
        self.assertEqual(result["entries"][0]["architecture"], "M32C")
        self.assertFalse(result["flash_available"])
        self.assertFalse(result["serial_accessed"])
        self.assertFalse(result["stored"])

    def test_rejects_truncated_container_before_decompression(self):
        raw = b"CUM\x1a" + struct.pack("<III", 16, 1, 4) + b"abc"
        with patch("open_dachs_manager.firmware.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "gepackte Länge"):
                inspect_muc(raw, "firmware.muc")
        popen.assert_not_called()

    def test_stops_decompressor_as_soon_as_output_exceeds_declared_size(self):
        command = python_command(
            "import sys, time; sys.stdout.buffer.write(b'x' * 65536); "
            "sys.stdout.buffer.flush(); time.sleep(2)"
        )
        with patch("open_dachs_manager.firmware.MUC_DECOMPRESS_COMMAND", command):
            with self.assertRaisesRegex(ValueError, "überschreiten"):
                inspect_muc(container(32), "firmware.muc")

    def test_times_out_and_cleans_up_decompressor(self):
        command = python_command("import time; time.sleep(2)")
        with (
            patch("open_dachs_manager.firmware.MUC_DECOMPRESS_COMMAND", command),
            patch("open_dachs_manager.firmware.MUC_DECOMPRESS_TIMEOUT_SECONDS", 0.05),
        ):
            with self.assertRaisesRegex(ValueError, "Zeitlimit"):
                inspect_muc(container(32), "firmware.muc")

    def test_reports_decompressor_failure_with_bounded_stderr(self):
        command = python_command(
            "import sys; sys.stderr.write('kaputter Datenstrom'); sys.exit(7)"
        )
        with patch("open_dachs_manager.firmware.MUC_DECOMPRESS_COMMAND", command):
            with self.assertRaisesRegex(ValueError, "kaputter Datenstrom"):
                inspect_muc(container(32), "firmware.muc")

    def test_rejects_wrong_extension(self):
        with self.assertRaisesRegex(ValueError, "Endung"):
            inspect_muc(b"", "firmware.zip")
