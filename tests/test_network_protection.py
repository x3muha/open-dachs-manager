import unittest

from open_dachs_manager.mapping import PackRepository, WriteAllowlist
from open_dachs_manager.network_protection import (
    FIELDS,
    NETWORK_PROTECTION_PAYLOAD_LENGTH,
    decode_network_protection,
    encode_network_protection_value,
    network_protection_schema,
    validate_network_cpu,
)
from open_dachs_manager.service import DachsService
from open_dachs_manager.transport import BlockResult, Frame, Response


class NetworkProtectionTests(unittest.TestCase):
    def test_legacy_layout_has_reviewed_18_byte_fields_for_both_cpus(self):
        self.assertEqual(len(FIELDS), NETWORK_PROTECTION_PAYLOAD_LENGTH)
        self.assertEqual([field.offset for field in FIELDS], list(range(18)))
        self.assertEqual(network_protection_schema(1)["fields"][0]["key"], "UC1.SA1.ubLaendercode")
        self.assertEqual(network_protection_schema(2)["fields"][0]["key"], "UC2.SA1.ubLaendercode")

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
        with self.assertRaises(ValueError):
            validate_network_cpu(0)
        with self.assertRaises(ValueError):
            decode_network_protection(1, bytes(17))
        payload = bytearray(18)
        with self.assertRaises(ValueError):
            encode_network_protection_value(payload, 1, "UC1.SA1.usSpannungObenFix", "999")

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


if __name__ == "__main__":
    unittest.main()
