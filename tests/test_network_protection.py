import unittest

from open_dachs_manager.mapping import PackRepository, WriteAllowlist
from open_dachs_manager.network_protection import (
    CONFIG_FIELDS,
    FIELDS,
    LIVE_FIELDS,
    NETWORK_PROTECTION_BLOCKS,
    NETWORK_PROTECTION_PAYLOAD_LENGTH,
    NETWORK_PROTECTION_PAYLOAD_LENGTHS,
    decode_network_protection,
    encode_network_protection_value,
    network_protection_schema,
    network_protection_schemas,
    validate_network_block,
    validate_network_cpu,
)
from open_dachs_manager.service import DachsService
from open_dachs_manager.transport import BlockResult, Frame, Response


def synthetic_block20_payload():
    """Return an artificial layout-4 config image, never captured from hardware."""
    payload = bytearray(59)
    payload[0] = 1  # Benutzer profile
    words = {
        1: 12345,
        3: 23456,
        5: 120,
        7: 121,
        9: 13579,
        11: 24680,
        13: 130,
        15: 131,
        17: 40123,
        19: 50987,
        21: 140,
        23: 141,
        25: 41234,
        27: 52345,
        29: 150,
        31: 151,
        33: 48765,
        48: -321,
        51: -1234,
        57: 24673,
    }
    for offset, value in words.items():
        payload[offset:offset + 2] = value.to_bytes(
            2, "little", signed=value < 0
        )
    payload[35] = 17
    payload[36:48] = bytes((1, 0) * 6)
    payload[50] = 37
    payload[53:57] = bytes((1, 0, 1, 0))
    return bytes(payload)


def synthetic_block21_payload():
    """Return artificial words covering unsigned, signed and scaled decoding."""
    words = (
        12345, 23456, 34567,
        111, 222, 333,
        45678, 50123, 54321,
        -1234, 2345, -3456,
        4567, -5678, 6789,
        -321, 654, -987,
        1234, 2345, 3456, 4567,
        1234, 2345, 3456,
        4321, 5432, 6543,
    )
    return b"".join(
        value.to_bytes(2, "little", signed=value < 0)
        for value in words
    )


class NetworkProtectionTests(unittest.TestCase):
    BLOCK20_SYNTHETIC = synthetic_block20_payload()
    BLOCK21_SYNTHETIC = synthetic_block21_payload()

    def test_legacy_layout_has_reviewed_18_byte_fields_for_both_cpus(self):
        self.assertEqual(len(FIELDS), NETWORK_PROTECTION_PAYLOAD_LENGTH)
        self.assertEqual([field.offset for field in FIELDS], list(range(18)))
        self.assertEqual(network_protection_schema(1)["fields"][0]["key"], "UC1.SA1.ubLaendercode")
        self.assertEqual(network_protection_schema(2)["fields"][0]["key"], "UC2.SA1.ubLaendercode")

    def test_all_six_confirmed_cpu_block_targets_have_distinct_schema(self):
        schemas = network_protection_schemas()

        self.assertEqual(
            [(item["cpu"], item["block"]) for item in schemas],
            [(cpu, block) for cpu in (1, 2) for block in NETWORK_PROTECTION_BLOCKS],
        )
        self.assertEqual(
            [item["target_key"] for item in schemas],
            ["1:16", "1:20", "1:21", "2:16", "2:20", "2:21"],
        )
        for item in schemas:
            self.assertEqual(
                item["payload_length"],
                NETWORK_PROTECTION_PAYLOAD_LENGTHS[item["block"]],
            )
            self.assertEqual(item["writable"], item["block"] in (16, 20))
            self.assertTrue(item["backup_eligible"])
            self.assertEqual(item["restore_eligible"], item["block"] == 16)

    def test_layout4_config_offsets_scaling_and_profile_are_source_aligned(self):
        self.assertEqual(len(CONFIG_FIELDS), 39)
        self.assertEqual(max(field.offset + field.size for field in CONFIG_FIELDS), 59)
        fields = {
            item["key"]: item
            for item in decode_network_protection(1, self.BLOCK20_SYNTHETIC, block=20)
        }

        self.assertEqual(fields["NetzKonfig1.ubSchutzart"]["value"], "Benutzer")
        self.assertEqual(fields["NetzKonfig1.usSpannung1Unten"]["value"], 123.45)
        self.assertEqual(fields["NetzKonfig1.usSpannung1Oben"]["value"], 234.56)
        self.assertEqual(fields["NetzKonfig1.usAbschaltzeitU1Oben"]["value"], 1.21)
        self.assertEqual(fields["NetzKonfig1.usFrequenz1Unten"]["value"], 40.123)
        self.assertEqual(fields["NetzKonfig1.usFrequenzObenRd"]["value"], 48.765)
        self.assertEqual(fields["NetzKonfig1.sImpedanzsprung"]["value"], -3.21)
        self.assertEqual(fields["NetzKonfig1.sImpedanzsprungLom"]["value"], -1234)
        self.assertEqual(fields["NetzKonfig1.usSpannung10min"]["value"], 246.7)
        self.assertTrue(all(item["write"] is True for item in fields.values()))

    def test_layout4_config_all_39_fields_encode_back_byte_exactly(self):
        edited = bytearray(self.BLOCK20_SYNTHETIC)
        fields = decode_network_protection(1, self.BLOCK20_SYNTHETIC, block=20)

        for field in fields:
            encode_network_protection_value(
                edited,
                1,
                field["key"],
                field["edit_value"],
                block=20,
            )

        self.assertEqual(bytes(edited), self.BLOCK20_SYNTHETIC)

    def test_layout4_live_values_decode_little_endian_and_signed_shorts(self):
        self.assertEqual(len(LIVE_FIELDS), 28)
        self.assertEqual(max(field.offset + field.size for field in LIVE_FIELDS), 56)
        fields = {
            item["key"]: item
            for item in decode_network_protection(1, self.BLOCK21_SYNTHETIC, block=21)
        }

        self.assertEqual(fields["Netzwerte1.usMeanVoltageL1"]["value"], 123.45)
        self.assertEqual(fields["Netzwerte1.usMeanCurrentL3"]["value"], 3.33)
        self.assertEqual(fields["Netzwerte1.usMeanFrequencyL2"]["value"], 50.123)
        self.assertEqual(fields["Netzwerte1.sMeanPowerL1"]["raw"], -321)
        self.assertEqual(fields["Netzwerte1.sMeanPowerL1"]["value"], -321)
        self.assertEqual(fields["Netzwerte1.usWinkelU1U2"]["value"], 123.4)
        self.assertEqual(fields["Netzwerte1.usKalibrierwertU1"]["value"], 1.001234)
        self.assertEqual(fields["Netzwerte1.usWinkelU1I1"]["value"], 1.004321)
        self.assertTrue(all(item["write"] is False for item in fields.values()))

    def test_display_conversions_and_choices_are_applied(self):
        payload = bytes([12, 4, 35, 24, 5, 15, 10, 10, 1, 25, 8, 30, 20, 15, 20, 15, 25, 23])
        fields = {field["key"]: field for field in decode_network_protection(2, payload)}
        self.assertEqual(fields["UC2.SA1.ubLaendercode"]["value"], "DE")
        self.assertEqual(fields["UC2.SA1.ubFesteSchutzart"]["value"], "VDE 4105 (Legacy-Profil)")
        self.assertEqual(fields["UC2.SA1.usSpannungUntenFix"]["value"], 195)
        self.assertEqual(fields["UC2.SA1.usSpannungObenFix"]["value"], 254)
        self.assertEqual(fields["UC2.SA1.usFrequenzUntenFix"]["value"], 48.5)
        self.assertEqual(fields["UC2.SA1.usFrequenzObenFix"]["value"], 51.0)
        self.assertEqual(fields["UC2.SA1.usImpedanzsprung"]["value"], 0.25)
        self.assertEqual(fields["UC2.SA2.ubMittelwertU10min"]["value"], 253)

    def test_every_display_value_encodes_back_to_the_same_byte(self):
        baseline = bytes(range(18))
        fields = decode_network_protection(1, baseline)
        edited = bytearray(baseline)
        for field in fields:
            encode_network_protection_value(edited, 1, field["key"], field["edit_value"])
        self.assertEqual(bytes(edited), baseline)

    def test_unknown_cpu_short_payload_and_out_of_range_value_are_rejected(self):
        for cpu in (0, True, 1.0, "1", None):
            with self.subTest(cpu=cpu):
                with self.assertRaises(ValueError):
                    validate_network_cpu(cpu)
        with self.assertRaises(ValueError):
            decode_network_protection(1, bytes(17))
        payload = bytearray(18)
        with self.assertRaises(ValueError):
            encode_network_protection_value(payload, 1, "UC1.SA1.usSpannungObenFix", "999")

    def test_unknown_blocks_wrong_lengths_and_live_encoding_are_rejected(self):
        for block in (19, True, 16.0, "16", None):
            with self.subTest(block=block):
                with self.assertRaises(ValueError):
                    validate_network_block(block)
        for block, length in ((20, 59), (21, 56)):
            for candidate in (bytes(length - 1), bytes(length + 1)):
                with self.subTest(block=block, length=len(candidate)):
                    with self.assertRaises(ValueError):
                        decode_network_protection(2, candidate, block=block)
        schema = network_protection_schema(2, 21)
        with self.assertRaises(PermissionError):
            encode_network_protection_value(
                bytearray(56),
                2,
                schema["fields"][0]["key"],
                0,
                block=21,
            )

    def test_layout4_encoder_rejects_rounding_overflow_and_ambiguous_times(self):
        payload = bytearray(self.BLOCK20_SYNTHETIC)

        with self.assertRaisesRegex(ValueError, "höchstens 2 Nachkommastellen"):
            encode_network_protection_value(
                payload, 1, "NetzKonfig1.usSpannung1Unten", "184,001", block=20
            )
        with self.assertRaisesRegex(ValueError, "0 bis 65535"):
            encode_network_protection_value(
                payload, 1, "NetzKonfig1.usSpannung1Unten", "655,36", block=20
            )
        with self.assertRaisesRegex(ValueError, "unbekannter Auswahlwert"):
            encode_network_protection_value(
                payload, 1, "NetzKonfig1.fSpannung1Oben", 2, block=20
            )
        encode_network_protection_value(
            payload, 1, "NetzKonfig1.fSpannung1Oben", "raw:2", block=20
        )
        self.assertEqual(payload[36], 2)

        payload[0] = 4
        with self.assertRaisesRegex(ValueError, "fest 0,1 s"):
            encode_network_protection_value(
                payload,
                1,
                "NetzKonfig1.usAbschaltzeitU1Oben",
                "0,1",
                block=20,
            )
        encode_network_protection_value(
            payload,
            1,
            "NetzKonfig1.usAbschaltzeitU1Oben",
            "raw:99",
            block=20,
        )
        self.assertEqual(payload[5:7], b"\x63\x00")

    def test_layout4_lossy_display_values_require_explicit_raw_for_changes(self):
        payload = bytearray(self.BLOCK20_SYNTHETIC)

        # Unchanged rounded displays preserve the exact bytes already read.
        encode_network_protection_value(
            payload,
            1,
            "NetzKonfig1.ubAbschaltzeitFrRd",
            "2",
            block=20,
        )
        encode_network_protection_value(
            payload,
            1,
            "NetzKonfig1.usSpannung10min",
            "246,7",
            block=20,
        )
        self.assertEqual(payload[35], 17)
        self.assertEqual(payload[57:59], (24673).to_bytes(2, "little"))

        with self.assertRaisesRegex(ValueError, "raw:<Rohwert>"):
            encode_network_protection_value(
                payload,
                1,
                "NetzKonfig1.ubAbschaltzeitFrRd",
                "3",
                block=20,
            )
        with self.assertRaisesRegex(ValueError, "raw:<Rohwert>"):
            encode_network_protection_value(
                payload,
                1,
                "NetzKonfig1.usSpannung10min",
                "246,6",
                block=20,
            )

        encode_network_protection_value(
            payload,
            1,
            "NetzKonfig1.ubAbschaltzeitFrRd",
            "raw:18",
            block=20,
        )
        encode_network_protection_value(
            payload,
            1,
            "NetzKonfig1.usSpannung10min",
            "raw:24665",
            block=20,
        )
        self.assertEqual(payload[35], 18)
        self.assertEqual(payload[57:59], (24665).to_bytes(2, "little"))

    def test_layout4_profile_dependent_times_roundtrip_for_every_profile(self):
        time_fields = [
            field for field in CONFIG_FIELDS
            if field.transform.startswith("trip_")
        ]
        for profile in range(10):
            for field in time_fields:
                with self.subTest(profile=profile, field=field.name):
                    payload = bytearray(59)
                    payload[0] = profile
                    payload[field.offset:field.offset + 2] = (123).to_bytes(2, "little")
                    decoded = next(
                        item for item in decode_network_protection(1, bytes(payload), block=20)
                        if item["key"] == field.key(1)
                    )
                    if profile in (2, 4) and field.transform in {
                        "trip_u1", "trip_f1_low", "trip_f1_high",
                    }:
                        value = "raw:123"
                    else:
                        value = decoded["edit_value"]
                    encode_network_protection_value(
                        payload, 1, field.key(1), value, block=20
                    )
                    self.assertEqual(payload[field.offset:field.offset + 2], b"{\x00")

    def test_checked_write_keeps_cpu_target_for_read_write_readback(self):
        before = bytes(18)
        after = bytes([0, 4]) + bytes(16)

        class Session:
            def __init__(self):
                self.calls = []
                self.reads = [before, after]

            def read_block(self, block, packet=None, timeout=0.9, cpu=0):
                self.calls.append(("read", cpu, block))
                payload = self.reads.pop(0)
                data = Frame("data", 1, b"", payload=b"\x00" + payload)
                return BlockResult(
                    block, 1, Response(b"", None, data, 1.0), 0, payload, cpu
                )

            def write_block(self, block, payload, packet=None, timeout=0.9, cpu=0):
                self.calls.append(("write", cpu, block, bytes(payload)))
                return Response(b"", Frame("ack", 2, b"", positive=True), None, 1.0)

        service = DachsService("/dev/null", 19200, 0.1, PackRepository())
        session = Session()
        audit = service.write_payload(
            session, 16, before, after, ["UC2.SA1.ubFesteSchutzart"],
            WriteAllowlist(), dry_run=False, cpu=2,
        )
        self.assertTrue(audit.written)
        self.assertTrue(audit.readback_ok)
        self.assertEqual(audit.cpu, 2)
        self.assertEqual(
            [(call[0], call[1], call[2]) for call in session.calls],
            [("read", 2, 16), ("write", 2, 16), ("read", 2, 16)],
        )

    def test_network_cpu_write_never_accepts_changed_fields_only_readback(self):
        key = "NetzKonfig1.usSpannung1Unten"
        before = bytes(59)
        after = bytearray(before)
        after[1:3] = (18400).to_bytes(2, "little")
        after = bytes(after)
        partial = bytearray(after)
        partial[40] = 1
        partial = bytes(partial)

        class Pack:
            @staticmethod
            def field_map(_block):
                return {key: {"offset": 1, "size": 2}}

        class Session:
            def __init__(self):
                self.reads = [before, partial, partial]

            def read_block(self, block, packet=None, timeout=0.9, cpu=0):
                payload = self.reads.pop(0)
                data = Frame("data", 1, b"", payload=b"\x00" + payload)
                return BlockResult(
                    block, 1, Response(b"", None, data, 1.0), 0, payload, cpu
                )

            @staticmethod
            def write_block(block, payload, packet=None, timeout=0.9, cpu=0):
                return Response(
                    b"", Frame("ack", 2, b"", positive=True), None, 1.0
                )

        service = DachsService(
            "/dev/null", 19200, 0.1, Pack(),
            readback_attempts=2, readback_delay=0,
        )
        audit = service.write_payload(
            Session(), 20, before, after, [key],
            WriteAllowlist(), dry_run=False, cpu=1,
        )

        self.assertFalse(audit.written)
        self.assertFalse(audit.readback_ok)
        self.assertEqual(audit.readback_attempts, 2)
        self.assertIn("readback mismatch", audit.error)


if __name__ == "__main__":
    unittest.main()
