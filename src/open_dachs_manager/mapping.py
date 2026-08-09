"""Pack, field metadata and standalone decoding for Open Dachs Manager."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from . import decoder


@dataclass(frozen=True)
class DecodedField:
    key: str
    label: str
    raw: object
    value: object
    unit: str
    metadata: dict


def is_reserved_key(key: str) -> bool:
    normalized = str(key).lower()
    return "reserve" in normalized or ".bres" in normalized


def package_data_root() -> Path:
    return Path(__file__).resolve().parent / "data"


def default_allowlist_path() -> Path:
    return package_data_root() / "write_allowlist.json"


VERSION_BASES = frozenset({
    "Hka_Bd_Stat.bSoftwareVersionUeberw",
    "Hka_Bd_Stat.bSoftwareVersionMessen",
    "Hka_Bd_Stat.bSoftwareVersionRegler",
})

HYDRAULIK_BASE = "Hka_Ew.HydraulikNr"
HYDRAULIK_COMPONENTS = (
    "Hka_Ew.HydraulikNr.bSpeicherArt",
    "Hka_Ew.HydraulikNr.bWW_Art",
    "Hka_Ew.HydraulikNr.b2_Waermeerzeuger",
    "Hka_Ew.HydraulikNr.bMehrmodul",
)

SPECIAL_LABELS_DE = {
    HYDRAULIK_BASE: "Hydraulische Einbindung",
}


# Most choices and ranges live in bundled ``ui_metadata.json``.  These
# additions describe fields which need a human note or a conditional value
# that cannot be represented as one static range.
BLOCK50_UI_OVERRIDES = {
    "Hka_Ew.UFlag_Ew1.bGrundeinstellung": {
        "help": "Kombiniertes Grundeinstellungs-/Flag-Byte; hierfür ist keine einzelne belastbare Auswahlliste dokumentiert.",
    },
    "Hka_Ew.uchProgrammwahl": {
        "help": "Gespeichert wird das ASCII-Zeichen A, B, E oder S. Die fachliche Bedeutung der vier Programme ist noch nicht belastbar dokumentiert.",
    },
    "Hka_Ew.bMindestlaufzeit": {
        "min": 30,
        "max": 180,
        "step": 1,
        "help": "Dokumentierter Bereich: 30 bis 180 Minuten.",
    },
    "Hka_Ew.bMaxlaufzeit": {
        "help": "Schaltet die maximale Dachslaufzeit ohne Pufferspeicher ein oder aus.",
    },
    "Hka_Ew.bMaxRuecklaufTempHKA": {
        "min": 50,
        "max": 73,
        "step": 1,
        "help": "Dokumentierter Bereich: 50 bis 73 °C.",
    },
    "Hka_Ew.bLaenderkonfiguration": {
        "help": "Für dieses Feld ist keine eindeutige Auswahlliste dokumentiert; deshalb bleibt es bewusst ein Rohwert.",
    },
    "Hka_Ew.usSollGenerator": {
        "step": 0.1,
        "help": "Generatornennleistung in kW; der zulässige Bereich hängt vom Kraftstoff- und Anlagentyp ab.",
    },
    "Hka_Ew.usAufstellhoehe": {
        "min": 0,
        "max": 3500,
        "step": 100,
        "help": "Dokumentierter Bereich: 0 bis 3500 m, Schrittweite 100 m.",
    },
    HYDRAULIK_BASE: {
        "help": "Vierstelliger Hydraulikcode X.X.X.X; jede Stelle kann weiterhin direkt als Rohwert 0 bis 8 eingegeben werden.",
    },
    "Hka_Ew.sbFuehlerAbgasMotorKor": {
        "min": -40,
        "max": 10,
        "step": 10,
        "help": "Dokumentierter Bereich: -40 bis +10 °C, Schrittweite 10 °C.",
    },
    "Hka_Ew.bFuehlerAbgasMotorTyp": {
        "help": "Bekannte Fühlervarianten; ein abweichender Rohwert bleibt möglich.",
    },
}


def _plain_option_label(value: object) -> str:
    """Turn the pack's small HTML labels into safe plain UI text."""
    text = re.sub(r"(?i)<br\s*/?>", " / ", str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.split()).strip()


def _version_display_part(value: object) -> str:
    """Render a byte as ASCII when it contains a printable version character."""
    number = int(value)
    return chr(number) if 48 <= number < 128 else str(number)


def _format_version(base: str, values: list[object]) -> str:
    """Render the three Dachs software-version fields consistently."""
    numbers = [int(value) for value in values]
    if base == "Hka_Bd_Stat.bSoftwareVersionRegler" and len(numbers) == 5:
        prefix = "".join(_version_display_part(value) for value in numbers[:3])
        return f"R {prefix}.{numbers[3]:03d}.{numbers[4]:03d}"
    if base == "Hka_Bd_Stat.bSoftwareVersionUeberw" and len(numbers) == 4:
        prefix = "".join(_version_display_part(value) for value in numbers[:2])
        return f"U {prefix}.{numbers[2]:03d}.{numbers[3]:03d}"
    if base == "Hka_Bd_Stat.bSoftwareVersionMessen" and len(numbers) == 4:
        return f"M {_version_display_part(numbers[1])}.{numbers[2]:03d}.{numbers[3]:03d}"
    return ".".join(str(value) for value in numbers)


def _version_display_char_to_raw(character: str, previous: int) -> int:
    """Invert one version character without changing its encoding family."""
    if len(character) != 1:
        raise ValueError("Versionskennzeichen muss genau ein Zeichen lang sein")
    if 48 <= int(previous) < 128:
        return ord(character)
    if character.isdigit():
        return int(character)
    return ord(character)


def _parse_version(base: str, value: str, previous: list[int]) -> list[int]:
    """Accept the displayed notation plus the raw dotted-byte notation."""
    text = str(value).strip()
    style = None
    if base == "Hka_Bd_Stat.bSoftwareVersionRegler":
        style = re.fullmatch(r"(?i)R\s*(.{3})\.(\d{1,3})\.(\d{1,3})", text)
        if style:
            prefix, main, sub = style.groups()
            return [
                *[_version_display_char_to_raw(char, previous[index]) for index, char in enumerate(prefix)],
                int(main),
                int(sub),
            ]
    elif base == "Hka_Bd_Stat.bSoftwareVersionUeberw":
        style = re.fullmatch(r"(?i)U\s*(.{2})\.(\d{1,3})\.(\d{1,3})", text)
        if style:
            prefix, main, sub = style.groups()
            return [
                *[_version_display_char_to_raw(char, previous[index]) for index, char in enumerate(prefix)],
                int(main),
                int(sub),
            ]
    elif base == "Hka_Bd_Stat.bSoftwareVersionMessen":
        style = re.fullmatch(r"(?i)M\s*(.)\.(\d{1,3})\.(\d{1,3})", text)
        if style:
            prefix, main, sub = style.groups()
            return [
                previous[0],
                _version_display_char_to_raw(prefix, previous[1]),
                int(main),
                int(sub),
            ]

    parts = [part for part in re.split(r"[.,;:/\s]+", text) if part]
    if len(parts) != len(previous):
        raise ValueError(
            f"Version {base} benötigt die angezeigte Schreibweise oder {len(previous)} Rohbytes, erhalten: {value!r}"
        )
    return [int(part, 0) for part in parts]


def _parse_hydraulik(value: str) -> list[int]:
    """Parse the four-part Dachs hydraulic code.

    Every position accepts 0..8 and treats 9 as the unselected placeholder.
    Keep that rule explicit instead of silently accepting arbitrary bytes.
    """
    parts = [part.strip() for part in str(value).strip().split(".")]
    if len(parts) != 4 or any(not part.isdigit() for part in parts):
        raise ValueError("Hydraulische Einbindung benötigt vier Ziffern im Format X.X.X.X")
    numbers = [int(part) for part in parts]
    if any(number < 0 or number > 8 for number in numbers):
        raise ValueError("Hydraulikcode: jede Stelle muss zwischen 0 und 8 liegen (9 ist 'nicht gewählt')")
    return numbers


# Human-facing names for all serially addressable blocks in the runtime pack.
# The pack remains authoritative for layouts and technical keys; these names
# keep the UI from falling back to bare block numbers.
BLOCK_NAMES_DE: dict[int, str] = {
    18: "Meldungsliste",
    20: "Anlagen- und Softwareinformationen",
    22: "Betriebsdaten und Energie",
    24: "Motor, Temperaturen und Leistung",
    26: "Generator und Stromnetz",
    28: "Dachs-Laufhistorie – Ring und Tage 1–5",
    30: "Dachs-Laufhistorie – Tage 6–7 und Abschaltungen",
    31: "Dachs-Laufhistorie – aktueller Tag",
    32: "Dachs-Laufhistorie – Summenwerte",
    34: "Mehrmodul-Betriebsdaten",
    36: "Mehrmodul-Grenzwerte",
    38: "Motorbelastung (Legacy, MSR2-Version 2/4)",
    46: "Funk-, Raum- und Bediengeräte",
    50: "Dachs-Einstellungen",
    52: "Schaltzeiten Dachs und Wärmeführung",
    54: "Schaltzeiten Stromführung",
    56: "Schaltzeiten Heizkreise",
    60: "Wärmeführung und Heizkreise",
    62: "Brenner-Betriebsdaten",
    66: "Stromführung und Lastmanagement",
    70: "Heizkreis-Daten und Einstellungen",
    76: "Warmwasser",
    80: "Servicecode-Historie (1L–8L)",
    82: "Servicecode- und Warnhistorie (Service 9L–13L)",
    84: "Servicecode-Messwerte Motor (1L)",
    86: "Servicecode-Messwerte Stromnetz (1L)",
    88: "Servicecode-Messwerte Motor (2L)",
    90: "Servicecode-Messwerte Stromnetz (2L)",
    92: "Servicecode-Messwerte Motor (3L)",
    94: "Servicecode-Messwerte Stromnetz (3L)",
    100: "Wartungsdaten",
    102: "Öl-Nachfüllhistorie",
    104: "Wartungs- und Service-Cache",
    110: "Name und Adresse",
    112: "Ort, E-Mail und Länderdaten",
    114: "Service-Telefonnummern",
}


class PackRepository:
    """Load the self-contained, versioned MSR2 runtime pack."""

    def __init__(self, pack_file: str | Path | None = None, pack_rev: str = "50",
                 service_codes_file: str | Path | None = None):
        data_root = package_data_root()
        self.pack_file = Path(pack_file) if pack_file else data_root / "msr2_pack_master_version.json"
        self.pack_rev = str(pack_rev)
        self.data = json.loads(self.pack_file.read_text(encoding="utf-8"))
        physical_offsets_path = data_root / "physical_offsets.json"
        physical_offsets_data = (
            json.loads(physical_offsets_path.read_text(encoding="utf-8"))
            if physical_offsets_path.exists()
            else {}
        )
        self.physical_offsets: dict[int, dict[str, int]] = {}
        self.packed_fields: dict[int, dict[str, dict]] = {}
        if str(physical_offsets_data.get("pack_rev")) == self.pack_rev:
            for block_text, fields in (physical_offsets_data.get("blocks") or {}).items():
                if str(block_text).isdigit() and isinstance(fields, dict):
                    self.physical_offsets[int(block_text)] = {
                        str(key): int(offset)
                        for key, offset in fields.items()
                    }
            for block_text, fields in (physical_offsets_data.get("packed_fields") or {}).items():
                if str(block_text).isdigit() and isinstance(fields, dict):
                    self.packed_fields[int(block_text)] = {
                        str(key): dict(metadata)
                        for key, metadata in fields.items()
                        if isinstance(metadata, dict)
                    }
        formats_path = data_root / "formats.json"
        self.formats = json.loads(formats_path.read_text(encoding="utf-8")).get("formats", {}) if formats_path.exists() else {}
        ui_metadata_path = data_root / "ui_metadata.json"
        ui_metadata_data = json.loads(ui_metadata_path.read_text(encoding="utf-8")) if ui_metadata_path.exists() else {}
        self.labels = self._load_properties(data_root / "labels_master.properties")
        self.melde_labels = self._load_properties(data_root / "meldehist_types_de.properties")
        self.fault_catalog_file = data_root / "fault_catalog_de.json"
        self.fault_catalog_schema, self.service_labels = self._load_fault_catalog(
            self.fault_catalog_file
        )
        configured_service_codes = str(
            os.environ.get("OPEN_DACHS_SERVICE_CODES_FILE", "")
            if service_codes_file is None
            else service_codes_file
        ).strip()
        self.service_codes_file = Path(configured_service_codes).expanduser().resolve() if configured_service_codes else None
        if self.service_codes_file is not None:
            self.service_labels.update(self._load_properties(self.service_codes_file))
        self.service_details_available = any(
            re.fullmatch(r"(?:sc\.\d+\.uc|uc\.[^.]+|mc\.[^.]+)", key)
            for key in self.service_labels
        )
        self.metadata_labels: dict[tuple[int, str], str] = {}
        self.ui_metadata: dict[tuple[int, str], dict] = {}
        for block in self.blocks():
            for field in self.layout(block):
                if field.get("kind") != "data":
                    continue
                key = str(field.get("key") or "")
                if key and field.get("label_de"):
                    self.metadata_labels[(block, key)] = str(field["label_de"])
                if not key:
                    continue
                interpretations = list(field.get("interpretation") or [])
                choices = []
                if interpretations and all("operator" not in item for item in interpretations):
                    try:
                        choices = []
                        for item in interpretations:
                            raw_text = str(item["value"])
                            raw_value: object = raw_text if str(field.get("type", "")).lower() == "string" else int(raw_text, 0)
                            choices.append({
                                "value": raw_value,
                                "label": _plain_option_label(item.get("text")) or raw_text,
                            })
                    except (KeyError, TypeError, ValueError):
                        choices = []
                if choices and len({item["value"] for item in choices}) == len(choices):
                    self.ui_metadata[(block, key)] = {"choices": choices}
        for block, fields in self.packed_fields.items():
            for key, metadata in fields.items():
                if key.startswith("Wartung_Ew1.Dicht_Wart.") and int(metadata.get("bit_length", 0)) == 2:
                    self.ui_metadata[(block, key)] = {"choices": [
                        {"value": 1, "label": "Ja / in Ordnung"},
                        {"value": 0, "label": "Nein / nicht in Ordnung"},
                        {"value": 2, "label": "Korrigiert"},
                    ]}
                elif str(metadata.get("type", "")).lower() == "boolean":
                    self.ui_metadata[(block, key)] = {"choices": [
                        {"value": 1, "label": "Ja"},
                        {"value": 0, "label": "Nein"},
                    ]}
        for block_text, fields in (ui_metadata_data.get("blocks") or {}).items():
            if not str(block_text).isdigit() or not isinstance(fields, dict):
                continue
            block = int(block_text)
            for key, metadata in fields.items():
                if isinstance(metadata, dict):
                    self.ui_metadata.setdefault((block, str(key)), {}).update(metadata)
        for key, override in BLOCK50_UI_OVERRIDES.items():
            self.ui_metadata.setdefault((50, key), {}).update(override)

    @staticmethod
    def _load_properties(path: Path) -> dict[str, str]:
        return decoder.load_properties(path)

    @staticmethod
    def _load_fault_catalog(path: Path) -> tuple[str, dict[str, str]]:
        """Load the compact Open-Dachs code-to-title catalogue."""
        data = json.loads(path.read_text(encoding="utf-8"))
        schema = str(data.get("schema") or "")
        if schema != "open-dachs-manager/fault-catalog/v1":
            raise ValueError(f"unsupported fault catalogue schema: {schema or 'missing'}")
        codes = data.get("codes")
        if not isinstance(codes, dict):
            raise ValueError("fault catalogue codes must be an object")
        result: dict[str, str] = {}
        for code_text, title in codes.items():
            code = int(str(code_text), 10)
            clean_title = " ".join(str(title).split()).strip()
            if code < 1 or code > 65535 or not clean_title:
                raise ValueError(f"invalid fault catalogue entry: {code_text!r}")
            result[f"sc.{code}"] = clean_title
        return schema, result

    def service_catalog(self, query: str = "", limit: int = 250) -> dict:
        """Return bundled fault titles plus optional local diagnostic details."""
        requested = str(query or "").strip().casefold()
        maximum = max(1, min(500, int(limit)))
        entries = []
        for key in self.service_labels:
            match = re.fullmatch(r"sc\.(\d+)", key)
            if not match:
                continue
            code = int(match.group(1))
            text, causes, measures = decoder._service_code_details(code, self.service_labels)
            searchable = " ".join([
                str(code),
                text,
                *(str(item.get("text", "")) for item in causes),
                *(str(item.get("text", "")) for item in measures),
            ]).casefold()
            if requested and requested not in searchable:
                continue
            entries.append({
                "code": code,
                "text": text or "Keine Beschreibung hinterlegt",
                "causes": causes,
                "measures": measures,
            })
        entries.sort(key=lambda item: int(item["code"]))
        total = sum(1 for key in self.service_labels if re.fullmatch(r"sc\.\d+", key))
        return {
            "available": bool(total),
            "count": total,
            "schema": self.fault_catalog_schema,
            "details_available": self.service_details_available,
            "query": str(query or "").strip(),
            "items": entries[:maximum],
            "truncated": len(entries) > maximum,
        }

    def blocks(self) -> list[int]:
        source = self.data.get("layouts") or self.data.get("blocks") or {}
        return sorted(int(k) for k in source if str(k).lstrip("-").isdigit())

    def addressable_blocks(self) -> list[int]:
        """Return blocks addressable by the one-byte MSR2 serial service."""
        return [block for block in self.blocks() if 0 <= block <= 255]

    def keys(self, block: int | None = None) -> list[str]:
        """Return sorted mapped technical keys, optionally for one block."""
        blocks = [int(block)] if block is not None else self.blocks()
        return sorted({key for item in blocks for key in self.field_map(item)})

    def label(self, key: str, block: int | None = None) -> str:
        """Return a human label, preferring block-local pack metadata."""
        if key in SPECIAL_LABELS_DE:
            return SPECIAL_LABELS_DE[key]
        fallback = decoder.label_for_key(key, {})
        custom = decoder.label_for_key(key, self.labels)
        if custom != fallback:
            return custom
        if block is not None:
            block_id = int(block)
            base = decoder.base_key(key)
            for candidate in (key, base):
                metadata = self.metadata_labels.get((block_id, candidate))
                if metadata:
                    labels = {key: metadata, base: metadata, candidate: metadata}
                    return decoder.label_for_key(key, labels)
        return custom

    def field_ui_metadata(self, block: int, key: str) -> dict:
        """Return bundled input choices and numeric ranges."""
        block_id = int(block)
        key_text = str(key)
        return dict(
            self.ui_metadata.get((block_id, key_text))
            or self.ui_metadata.get((block_id, decoder.base_key(key_text)))
            or {}
        )

    def resolve_key(self, query: str, block: int | None = None) -> tuple[int, str, dict]:
        """Resolve a technical key or a unique human label.

        Exact technical keys win.  Label/substring matches are deliberately
        required to be unique so a convenient query can never silently target
        an unrelated block.
        """
        needle = str(query).strip()
        if not needle:
            raise ValueError("field key/label must not be empty")
        block_ids = [int(block)] if block is not None else self.blocks()
        exact_key = []
        exact_label = []
        partial = []
        lowered = needle.casefold()
        for block_id in block_ids:
            for base, group in self.presentation_groups(block_id).items():
                components = group["components"]
                first = self.field_map(block_id)[components[0]]
                grouped_meta = dict(first)
                grouped_meta.update({
                    "type": group["type"],
                    "size": len(components),
                    "components": components,
                    "write": True,
                })
                grouped_item = (block_id, base, grouped_meta)
                grouped_label = self.label(base, block_id)
                if base == needle or base.casefold() == lowered:
                    exact_key.append(grouped_item)
                elif grouped_label.casefold() == lowered:
                    exact_label.append(grouped_item)
            for key, metadata in self.field_map(block_id).items():
                label = self.label(key, block_id)
                item = (block_id, key, metadata)
                if key == needle or key.casefold() == lowered:
                    exact_key.append(item)
                elif label.casefold() == lowered:
                    exact_label.append(item)
                elif lowered in key.casefold() or lowered in label.casefold():
                    partial.append(item)

        matches = exact_key or exact_label or partial
        if not matches:
            raise KeyError(f"field key/label not found: {query!r}")
        if len(matches) > 1:
            locations = ", ".join(f"{item[1]} (block {item[0]})" for item in matches[:6])
            suffix = " ..." if len(matches) > 6 else ""
            raise ValueError(f"field query is ambiguous; specify --block or use an exact key: {locations}{suffix}")
        return matches[0]

    def layout(self, block: int) -> list[dict]:
        block = int(block)
        layouts = self.data.get("layouts") or {}
        if str(block) in layouts:
            return self._layout_with_physical_offsets(block, layouts[str(block)] or [])
        item = (self.data.get("blocks") or {}).get(str(block), {})
        out = list(item.get("base", []) or [])
        for variant in item.get("variants", []) or []:
            choices = variant.get("choices", []) or []
            selected = None
            for choice in choices:
                if self.pack_rev in [str(v) for v in choice.get("versions", []) or []]:
                    selected = choice.get("entry")
                    break
            if selected is None and len(choices) == 1:
                selected = choices[0].get("entry")
            if isinstance(selected, dict):
                out.append(selected)
        return self._layout_with_physical_offsets(block, out)

    def _layout_with_physical_offsets(self, block: int, entries: list[dict]) -> list[dict]:
        """Apply reviewed byte offsets without mutating the bundled pack."""
        offsets = self.physical_offsets.get(int(block), {})
        result = []
        for entry in entries:
            item = dict(entry)
            key = str(item.get("key") or "")
            if key in offsets:
                item["offset"] = offsets[key]
                # The offset pack contains final wire offsets.  Mark them so
                # the HKA_MW1 compatibility pad is not applied a second time.
                item["_physical_offset"] = True
            result.append(item)
        return result

    def block_name(self, block: int) -> str:
        block_id = int(block)
        if block_id in BLOCK_NAMES_DE:
            return BLOCK_NAMES_DE[block_id]
        item = (self.data.get("blocks") or {}).get(str(block_id), {})
        return str(item.get("block_name_de") or item.get("name_de") or f"Block {block}")

    def field_map(self, block: int) -> dict[str, dict]:
        result = {}
        cursor = 0
        pad_shift = 0
        diag_pad_applied = False
        for field in self.layout(block):
            if field.get("kind") == "space":
                cursor += int(field.get("length", 0) or 0)
                continue
            if field.get("kind") != "data":
                continue
            raw_offset = field.get("offset")
            base_offset = cursor if raw_offset is None else int(raw_offset)
            typ = str(field.get("type") or "Byte").lower()
            unsigned = bool(field.get("unsigned"))
            repeat = int(field.get("repeat", 1) or 1)
            length = int(field.get("length", 0) or 0)
            count = repeat if repeat > 1 else (length if length > 1 else 1)
            size = 1 if typ == "byte" else 2 if typ == "short" else 4 if typ == "long" else (length if typ == "string" else 1)
            if typ == "string":
                count = 1
            stride = int(field.get("stride", size) or size)
            key = str(field.get("key") or "")
            if decoder.needs_pre_motor_pad(key) and not diag_pad_applied:
                pad_shift += 1
                diag_pad_applied = True
            offset = base_offset if field.get("_physical_offset") else base_offset + pad_shift
            for index in range(count):
                item_key = f"{key}[{index}]" if count > 1 else key
                result[item_key] = {
                    "offset": offset + index * stride,
                    "type": typ,
                    "unsigned": unsigned,
                    "size": size,
                    "stride": stride,
                    "base_key": key,
                    "write": bool(field.get("write") or field.get("schreiben")),
                }
            cursor = max(cursor, offset + (count - 1) * stride + size)
        for key, metadata in self.packed_fields.get(int(block), {}).items():
            item = dict(metadata)
            item.update({
                "offset": int(item["offset"]),
                "bit_offset": int(item["bit_offset"]),
                "bit_length": int(item["bit_length"]),
                "size": 1,
                "base_key": key,
                "packed": True,
                "write": True,
            })
            result[key] = item
        return result

    def version_components(self, block: int) -> dict[str, list[str]]:
        """Return version byte arrays as ordered writable component keys."""
        groups: dict[str, list[tuple[int, str]]] = {}
        for key in self.field_map(block):
            match = re.match(r"^(.*)\[(\d+)\]$", key)
            if not match or match.group(1) not in VERSION_BASES:
                continue
            groups.setdefault(match.group(1), []).append((int(match.group(2)), key))
        return {
            base: [key for _index, key in sorted(items)]
            for base, items in groups.items()
        }

    def presentation_groups(self, block: int) -> dict[str, dict]:
        """Return physical fields that should be edited as one logical value."""
        groups = {
            base: {"type": "version", "components": components}
            for base, components in self.version_components(block).items()
        }
        fields = self.field_map(block)
        if all(component in fields for component in HYDRAULIK_COMPONENTS):
            groups[HYDRAULIK_BASE] = {
                "type": "hydraulik-code",
                "components": list(HYDRAULIK_COMPONENTS),
            }
        return groups

    def display_fields(self, block: int, payload: bytes) -> list[DecodedField]:
        """Decode fields while presenting known software versions as strings.

        The individual byte keys remain in the physical map so every byte is
        still writable.  Only the presentation is grouped into one dotted
        version value; :meth:`encode_value` expands it back to those bytes.
        """
        decoded = self.decode(block, payload)
        groups = self.presentation_groups(block)
        by_key = {field.key: field for field in decoded}
        component_to_base = {
            component: base
            for base, group in groups.items()
            for component in group["components"]
        }
        emitted: set[str] = set()
        result: list[DecodedField] = []
        for field in decoded:
            base = component_to_base.get(field.key)
            if base:
                if base in emitted:
                    continue
                group = groups[base]
                components = group["components"]
                values = [by_key[key].raw for key in components if key in by_key]
                if len(values) != len(components):
                    continue
                metadata = dict(field.metadata)
                metadata.update({
                    "type": group["type"],
                    "size": len(components),
                    "components": components,
                    "write": True,
                })
                metadata.update(self.field_ui_metadata(block, base))
                value = _format_version(base, values) if group["type"] == "version" else ".".join(str(int(item)) for item in values)
                result.append(DecodedField(
                    base,
                    self.label(base, block),
                    values,
                    value,
                    "",
                    metadata,
                ))
                emitted.add(base)
                continue
            result.append(field)
        return result

    def decode(self, block: int, payload: bytes) -> list[DecodedField]:
        layout = self.layout(block)
        values = decoder.decode_fields(payload, layout)
        fields = self.field_map(block)
        for key, metadata in fields.items():
            if not metadata.get("packed"):
                continue
            offset = int(metadata["offset"])
            bit_offset = int(metadata["bit_offset"])
            bit_length = int(metadata["bit_length"])
            if offset < len(payload):
                values[key] = (payload[offset] >> bit_offset) & ((1 << bit_length) - 1)
        result = []
        for key, raw in values.items():
            value, unit = decoder.apply_format(key, raw, self.formats, self.service_labels)
            metadata = dict(fields.get(key, {}))
            metadata.update(self.field_ui_metadata(block, key))
            choices = list(metadata.get("choices") or [])
            selected_choice = next(
                (choice for choice in choices if str(choice.get("value")) == str(raw)),
                None,
            )
            if selected_choice:
                value = f"{raw} ({selected_choice['label']})"
            if key == "CMDANRUF.bMeldecodeTypReturn":
                type_label = self.melde_labels.get(
                    f"MeldeHIST.bMeldecodeTyp.option.{int(raw)}",
                    "Unbekannter Meldetyp",
                )
                value = f"{raw} ({type_label})"
            elif re.search(r"\.bStatusFlags$", key):
                entstoerart = "ja" if int(raw) & 0x01 else "nein"
                auto_reset = "ja" if int(raw) & 0x02 else "nein"
                value = f"{raw} (Entstörart={entstoerart}, Auto-Rücksetzen={auto_reset})"
            elif re.search(r"\.bWarntypModul$", key):
                warn_type = (int(raw) >> 4) & 0x0F
                module = int(raw) & 0x0F
                type_label = self.melde_labels.get(
                    f"MeldeHIST.bMeldecodeTyp.option.{warn_type}",
                    "Unbekannter Warntyp",
                )
                module_label = "kein Modul" if module == 0 else "Dachs" if module == 1 else f"Modul {module}"
                value = f"{raw} (Typ={warn_type} {type_label}, Modul={module} {module_label})"
            label = self.label(key, block)
            result.append(DecodedField(key, label, raw, value, unit, metadata))
        return result

    def meldehist(self, payload: bytes) -> dict:
        """Decode block 18's packed message-history entries for presentation."""
        return decoder.decode_block18_history(payload, self.melde_labels, self.service_labels)

    def oil_refill_history(self, payload: bytes) -> dict:
        """Decode block 102's three interleaved oil-refill records."""
        return decoder.decode_block102_history(payload)

    def run_history(self, payloads: dict[int, bytes]) -> dict:
        """Combine blocks 28, 30, 31 and 32 into the seven-day chart."""
        return decoder.decode_run_history(payloads)

    def mc_status(self, payload: bytes) -> dict:
        """Expand the two monitoring-controller bitsets in an HKA_UC block."""
        return decoder.decode_mc_status(payload)

    def service_history(self, payloads: dict[int, bytes]) -> dict:
        """Combine blocks 80 and 82 into service and warning history."""
        return decoder.decode_service_history(payloads, self.melde_labels, self.service_labels)

    def motor_snapshot(self, block: int, payload: bytes, service_history: dict | None = None) -> dict:
        """Present one of the three service-code motor snapshots.

        Group HKA_MW1 fields by operating data, temperatures, control values
        and actors while keeping all physical fields available below it.
        """
        slots = {84: 1, 88: 2, 92: 3}
        if block not in slots:
            raise ValueError(f"Block {block} ist kein Motor-Messwertspeicher")
        slot = slots[block]
        prefix = f"Hka_BZbeiSC_Mw1_{slot}L"
        decoded = {field.key: field for field in self.decode(block, payload)}

        def item(suffix: str, label: str, unit: str | None = None) -> dict | None:
            field = decoded.get(prefix + suffix)
            if field is None:
                return None
            return {
                "key": field.key,
                "label": label,
                "value": field.value,
                "raw": field.raw,
                "unit": field.unit if unit is None else unit,
                "offset": field.metadata.get("offset"),
            }

        def section(title: str, items: list[dict | None]) -> dict:
            return {"title": title, "items": [entry for entry in items if entry is not None]}

        motor_temperature = decoded.get(prefix + ".Temp.sbMotor")
        dachs_outlet = None
        if motor_temperature is not None and isinstance(motor_temperature.value, (int, float)):
            value = float(motor_temperature.value) + 3.0
            dachs_outlet = {
                "key": prefix + ".Temp.DachsAustritt",
                "label": "Dachs-Austritt (aus Kühlwasser Motor + 3 K)",
                "value": int(value) if value.is_integer() else value,
                "raw": None,
                "unit": "°C",
                "offset": motor_temperature.metadata.get("offset"),
                "derived": True,
            }

        sections = [
            section("Betrieb und Motor", [
                item(".bMotorStatus", "Motorstatus"),
                item(".usDrehzahl", "Motordrehzahl"),
                item(".sWirkleistung", "Elektrische Generatorleistung"),
                item(".ulMotorlaufsekunden", "Motorlaufzeit"),
                item(".usLuftdruck", "Luftdruck"),
                item(".bKraftstofftyp", "Kraftstofftyp / Status 2"),
            ]),
            section("Temperaturen im Dachs", [
                item(".Temp.sbGen", "Dachs-Eintritt", "°C"),
                item(".Temp.sbMotor", "Kühlwasser Motor", "°C"),
                dachs_outlet,
                item(".Temp.sAbgasMotor", "Motorabgastemperatur", "°C"),
                item(".Temp.sAbgasHKA", "Dachs-Abgastemperatur nach Rußfilter", "°C"),
                item(".Temp.sKapsel", "Kapsel-/Kondenser-Abgastemperatur", "°C"),
                item(".Temp.sbRegler", "Reglerfühler MSR2", "°C"),
            ]),
            section("Heizkreis, Speicher und Raum", [
                item(".Temp.sbVorlauf", "Vorlauffühler VF (externer Heizkreis)", "°C"),
                item(".Temp.sbRuecklauf", "Rücklauffühler RF (externer Heizkreis)", "°C"),
                item(".Solltemp.sbVorlauf", "Vorlauf-Solltemperatur", "°C"),
                item(".Solltemp.sbRuecklauf", "Rücklauf-Solltemperatur", "°C"),
                item(".Temp.sbAussen", "Außentemperatur", "°C"),
                item(".Temp.sbFuehler1", "Fühler 1", "°C"),
                item(".Temp.sbFuehler2", "Fühler 2", "°C"),
                item(".Temp.sbZS_Vorlauf1", "Zusatzmodul Vorlauf 1", "°C"),
                item(".Temp.sbZS_Vorlauf2", "Zusatzmodul Vorlauf 2", "°C"),
                item(".Temp.sbZS_Warmwasser", "Warmwasser-Isttemperatur", "°C"),
                item(".Temp.sbZS_Fuehler3", "Zusatzmodul Fühler 3", "°C"),
                item(".Temp.sbZS_Fuehler4", "Zusatzmodul Fühler 4", "°C"),
                item(".Temp.sbRaumKreis1", "Raumtemperatur Heizkreis 1", "°C"),
                item(".Temp.sbRaumKreis2", "Raumtemperatur Heizkreis 2", "°C"),
            ]),
            section("Regelwerte und Bivalenz", [
                item(".Regel_Temp.sZS_Vorlauf1", "Regeltemperatur Heizkreis 1", "°C"),
                item(".Regel_Temp.sZS_Vorlauf2", "Regeltemperatur Heizkreis 2", "°C"),
                item(".Regel_Temp.sZS_Warmwasser", "Regeltemperatur Warmwasser", "°C"),
                item(".Bivschalt.bZeitBisUmschaltung", "Zeit bis zur Bivalenz-Umschaltung"),
                item(".Bivschalt.bBivUmschaltTemperatur", "Temperatur bei Bivalenz-Umschaltung", "°C"),
                item(".Bivschalt.bZeitAlterWaermNichtAngesteuert", "Zeit: zweiter Wärmeerzeuger nicht angesteuert"),
            ]),
            section("Aktoren und Hardware", [
                item(".Aktor.bWwPumpe", "Warmwasser-Ladepumpe"),
                item(".Aktor.sStellmotorGas", "Ansteuerung Stellmotor Gas"),
                item(".Aktor.sStellmotorOel", "Ansteuerung Stellmotor Öl"),
                item(".Temp.sbFreigabeModul", "Modulfreigabe"),
                item(".bCodierstecker", "Codierstecker"),
                item(".bZusatzplatinen", "Zusatzplatinen"),
            ]),
        ]

        service_context = None
        if service_history:
            ring = service_history.get("snapshot_ring")
            if ring is not None:
                physical_order = [
                    (7 - rank - ((3 - int(ring)) % 3)) % 3
                    for rank in range(3)
                ]
                try:
                    recency = physical_order.index(slot - 1) + 1
                except ValueError:
                    recency = None
                if recency is not None:
                    service = next(
                        (entry for entry in service_history.get("services", []) if entry.get("recency") == recency),
                        None,
                    )
                    if service:
                        service_context = {
                            "recency": recency,
                            "slot": service.get("slot"),
                            "code": service.get("code"),
                            "text": service.get("text"),
                            "timestamp_text": service.get("timestamp_text"),
                        }

        return {
            "available": True,
            "slot": slot,
            "paired_mc_block": block + 2,
            "service_context": service_context,
            "sections": sections,
        }

    def encode_value(
        self,
        payload: bytearray,
        key: str,
        value: str,
        raw_mode: bool = False,
        block: int | None = None,
    ) -> None:
        """Encode one mapped value without changing the payload length.

        ``block`` is important because the MSR2 pack intentionally reuses
        field names in several blocks/modules.  Without it, a write could
        resolve to the first matching block rather than the block the caller
        just read.
        """
        groups = self.presentation_groups(int(block)) if block is not None else {}
        if block is not None and key in groups:
            group = groups[key]
            components = group["components"]
            fields = self.field_map(int(block))
            previous = [int(payload[int(fields[component]["offset"])]) for component in components]
            numbers = (
                _parse_version(key, str(value), previous)
                if group["type"] == "version"
                else _parse_hydraulik(str(value))
            )
            for component, number in zip(components, numbers):
                if not 0 <= number <= 255:
                    raise ValueError(f"Byte außerhalb 0..255: {number}")
                self.encode_value(payload, component, str(number), raw_mode=True, block=block)
            return
        fields = self.field_map_from_key(key, block)
        meta = fields[1]
        typ = meta["type"]
        if typ == "string":
            raw = str(value)
        elif isinstance(value, str) and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}(?: \d{2}:\d{2}:\d{2})?(?: \(!\))?", value.strip()):
            text = value.strip().removesuffix(" (!)")
            pattern = "%d.%m.%Y %H:%M:%S" if len(text) > 10 else "%d.%m.%Y"
            dt = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
            raw = int((dt - datetime(2000, 1, 1, tzinfo=timezone.utc)).total_seconds())
        else:
            number = float(value)
            if raw_mode:
                raw = int(round(number))
            else:
                format_key = decoder.format_key(str(meta.get("base_key") or key), self.formats)
                fmt = self.formats.get(format_key, {}) if format_key else {}
                raw = int(round((number - float(fmt.get("adder", 0) or 0)) * float(fmt.get("divisor", 1) or 1)))
        offset = int(meta["offset"])
        size = int(meta["size"])
        if offset < 0 or offset + size > len(payload):
            raise ValueError(
                f"mapped field {key} exceeds payload length ({offset}+{size}>{len(payload)})"
            )
        if typ == "string":
            data = str(raw).encode("latin-1", errors="ignore")[:size]
            payload[offset:offset + size] = data + b"\x00" * max(0, size - len(data))
        elif meta.get("packed"):
            bit_offset = int(meta["bit_offset"])
            bit_length = int(meta["bit_length"])
            raw_value = int(raw)
            maximum = (1 << bit_length) - 1
            if raw_value < 0 or raw_value > maximum:
                raise ValueError(f"value {raw_value} does not fit {bit_length}-bit field at {key}")
            if bit_offset < 0 or bit_offset + bit_length > 8:
                raise ValueError(f"invalid packed field boundary at {key}")
            mask = maximum << bit_offset
            payload[offset] = (payload[offset] & (~mask & 0xFF)) | (raw_value << bit_offset)
        else:
            signed = not bool(meta.get("unsigned"))
            try:
                payload[offset:offset + size] = int(raw).to_bytes(size, "little", signed=signed)
            except OverflowError as exc:
                raise ValueError(f"value {raw} does not fit {meta['type']} at {key}") from exc

    def field_map_from_key(self, key: str, block: int | None = None) -> tuple[str, dict]:
        if block is not None:
            fields = self.field_map(int(block))
            if key in fields:
                return str(int(block)), fields[key]
            raise KeyError(f"{key} not found in block {block}")
        matches = []
        for block in self.blocks():
            fields = self.field_map(block)
            if key in fields:
                matches.append((block, fields[key]))
        if len(matches) == 1:
            return str(matches[0][0]), matches[0][1]
        if len(matches) > 1:
            blocks = ", ".join(str(item[0]) for item in matches)
            raise ValueError(f"{key} occurs in multiple blocks ({blocks}); specify --block")
        raise KeyError(key)


class WriteAllowlist:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_allowlist_path()
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"keys": []}
        if isinstance(data, dict):
            keys = data.get("keys", [])
            mode = str(data.get("mode", "")).strip().lower()
        else:
            keys = data
            mode = ""
        self.keys = {str(key) for key in keys}
        self.allow_all = mode in {"all", "allow_all", "all_fields"} or "*" in self.keys

    def allows(self, key: str) -> bool:
        return self.allow_all or key in self.keys
