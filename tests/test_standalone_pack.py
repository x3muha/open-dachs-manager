import json
import unittest
from collections import defaultdict
from pathlib import Path

from open_dachs_manager.mapping import BLOCK_NAMES_DE, PackRepository, is_reserved_key


class StandalonePackTests(unittest.TestCase):
    def test_runtime_data_is_self_contained_and_has_no_xml_provenance(self):
        pack = PackRepository()
        data_root = Path(pack.pack_file).parent
        formats_path = data_root / "formats.json"
        ui_metadata_path = data_root / "ui_metadata.json"
        physical_offsets_path = data_root / "physical_offsets.json"

        self.assertTrue(formats_path.is_file())
        self.assertTrue(ui_metadata_path.is_file())
        self.assertTrue(physical_offsets_path.is_file())
        self.assertFalse((data_root / "msr2_formats_v2.json").exists())
        self.assertFalse(any(path.suffix.lower() in {".xml", ".jar"} for path in data_root.rglob("*")))

        formats_text = formats_path.read_text(encoding="utf-8").lower()
        pack_text = Path(pack.pack_file).read_text(encoding="utf-8").lower()
        ui_metadata_text = ui_metadata_path.read_text(encoding="utf-8").lower()
        physical_offsets_text = physical_offsets_path.read_text(encoding="utf-8").lower()
        self.assertNotIn(".xml", formats_text)
        self.assertNotIn('"source"', formats_text)
        self.assertNotIn("source_mapping", pack_text)
        self.assertNotIn("library/xml", pack_text)
        self.assertNotIn("label_source", pack_text)
        self.assertNotIn(".xml", ui_metadata_text)
        self.assertNotIn('"source"', ui_metadata_text)
        self.assertNotIn(".xml", physical_offsets_text)
        self.assertNotIn('"source"', physical_offsets_text)

        json.loads(formats_text)
        json.loads(pack_text)
        json.loads(ui_metadata_text)
        json.loads(physical_offsets_text)

    def test_reviewed_physical_offsets_cover_settings_blocks(self):
        pack = PackRepository()
        data = json.loads((Path(pack.pack_file).parent / "physical_offsets.json").read_text(encoding="utf-8"))
        self.assertEqual(data["summary"], {
            "blocks": 8,
            "fields": 253,
            "packed_fields": 28,
            "corrected_offsets": 203,
        })
        expected = {
            (24, "Hka_Mw1.bMotorStatus"): 0,
            (24, "Hka_Mw1.usDrehzahl"): 2,
            (24, "Hka_Mw1.Bivschalt.bZeitBisUmschaltung"): 39,
            (24, "Hka_Mw1.Aktor.bWwPumpe"): 46,
            (24, "Hka_Mw1.bKraftstofftyp"): 54,
            (24, "Hka_Mw1.ulMotorlaufsekunden"): 58,
            (24, "Hka_Mw1.bRes[7]"): 69,
            (50, "Hka_Ew.Res[0]"): 54,
            (50, "Hka_Ew.ulSystemTime"): 36,
            (50, "Hka_Ew.bStartverzoegerung"): 47,
            (50, "Hka_Ew.bFuehlerAbgasMotorTyp"): 48,
            (60, "Waermef_Ew.bEinschalttemp"): 2,
            (60, "Waermef_Ew.bSolltempRuecklaufHoch"): 3,
            (66, "Stromf_Ew.LastExtern.bDauer"): 11,
            (70, "Hk_Ew.Heizkreis1.bVorlaufanstieg"): 12,
            (76, "Ww_Ew.bWwSollTemp"): 32,
            (100, "Wartung_Ew1.bAbgasgegendnachRFTausch"): 35,
            (100, "Wartung_Ew1.Vorher.bAvSpiel"): 22,
            (100, "Wartung_Ew1.Vorher.bEvSpiel"): 24,
            (100, "Wartung_Ew1.Dicht_Wart.bGeraeusch"): 32,
            (100, "Wartung_Ew1.Flags_Allg1.fDichtheitskontrolle"): 33,
            (100, "Wartung_Ew1.Flags_Allg2.fEintragWartungsplan"): 34,
            (100, "Wartung_Ew1.Flags_Gas.fZuendkerze"): 36,
            (100, "Wartung_Ew1.Flags_Oel.fKraftstofffilter"): 37,
            (104, "Wartung_Cache.fBestaetigt"): 0,
            (104, "Wartung_Cache.usIntervall"): 6,
        }
        for (block, key), offset in expected.items():
            self.assertEqual(pack.field_map(block)[key]["offset"], offset, f"{block}: {key}")

        for block in data["blocks"]:
            for key, field in pack.field_map(int(block)).items():
                self.assertLessEqual(field["offset"] + field["size"], 70, f"{block}: {key}")

    def test_ui_metadata_has_reviewed_systematic_coverage(self):
        pack = PackRepository()
        data = json.loads((Path(pack.pack_file).parent / "ui_metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(data["summary"], {
            "blocks": 12,
            "fields": 64,
            "choice_fields": 18,
            "range_fields": 46,
        })

        self.assertEqual(pack.field_ui_metadata(70, "Hk_Ew.fHkObjekt")["choices"][1], {"value": 1, "label": "getrennt"})
        self.assertEqual(pack.field_ui_metadata(112, "Adresse2.bLand")["choices"][8], {"value": 12, "label": "DE"})
        self.assertEqual(pack.field_ui_metadata(76, "Ww_Ew.bWwSollTemp")["max"], 65)
        self.assertEqual(pack.field_ui_metadata(100, "Wartung_Ew1.Vorher.bAvSpiel")["step"], 0.01)

        # Unimplemented fields do not get nominal limits presented as
        # reliable editor rules.
        self.assertEqual(pack.field_ui_metadata(66, "Stromf_Ew.HT_NT.bBezugsPreisHT"), {})

        # Relational display rules are not exact enum values and therefore
        # must never become misleading dropdowns.
        self.assertNotIn("choices", pack.field_ui_metadata(24, "Hka_Mw1.Aktor.bWwPumpe"))

    def test_all_pack_blocks_have_reviewed_human_names(self):
        pack = PackRepository()
        self.assertEqual(len(pack.blocks()), 36)
        self.assertEqual(set(pack.blocks()), set(BLOCK_NAMES_DE))
        self.assertTrue(all(0 <= block <= 255 for block in pack.blocks()))
        self.assertTrue(all(not pack.block_name(block).startswith("Block ") for block in pack.blocks()))
        self.assertIn("Legacy", pack.block_name(38))

    def test_non_reserved_labels_are_unique_inside_each_block(self):
        pack = PackRepository()
        for block in pack.blocks():
            labels = defaultdict(list)
            for key in pack.field_map(block):
                if not is_reserved_key(key):
                    labels[pack.label(key, block)].append(key)
            duplicates = {label: keys for label, keys in labels.items() if len(keys) > 1}
            self.assertEqual(duplicates, {}, f"duplicate labels in block {block}: {duplicates}")

    def test_maintenance_abbreviations_are_expanded(self):
        pack = PackRepository()
        self.assertEqual(
            pack.label("Wartung_Ew1.bAbgasgegendnachRFTausch", 100),
            "Abgasgegendruck nach Rußfiltertausch",
        )
        self.assertEqual(pack.label("Wartung_Ew1.Vorher.bAvSpiel", 100), "Auslassventil-Spiel (vor Wartung)")
        self.assertEqual(pack.label("Wartung_Ew1.Vorher.bEvSpiel", 100), "Einlassventil-Spiel (vor Wartung)")


if __name__ == "__main__":
    unittest.main()
