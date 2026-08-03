"""MSR2 block-16 layout for the two network-monitor CPUs.

Read-only captures from both addressed CPUs confirm an 18-byte payload.  The
mapping stays separate from the regulator pack so CPU namespace and increased
risk remain visible in code and in the red web presentation.
"""

from __future__ import annotations

from dataclasses import dataclass


NETWORK_PROTECTION_BLOCK = 16
NETWORK_PROTECTION_CPUS = (1, 2)
NETWORK_PROTECTION_PAYLOAD_LENGTH = 18


COUNTRY_CHOICES = (
    (1, "AT"), (2, "AU"), (5, "BE"), (7, "CA"), (9, "CH"),
    (10, "HR"), (11, "CZ"), (12, "DE"), (13, "DK"), (15, "ES"),
    (18, "EE"), (19, "FR"), (22, "GR"), (23, "HU"), (25, "IT"),
    (28, "IE"), (29, "IR"), (31, "LT"), (33, "LU"), (35, "NO"),
    (37, "NL"), (38, "PT"), (39, "PL"), (43, "RU"), (44, "SE"),
    (48, "SK"), (50, "GB"), (51, "US"), (57, "Sonstige"),
)

PROTECTION_CHOICES = (
    (0, "Unbekannt"),
    (1, "Keine"),
    (2, "ENS"),
    (3, "LOM"),
    (4, "VDE 4105 (Legacy-Profil)"),
)

BOOLEAN_CHOICES = ((0, "Nicht aktiv"), (1, "Aktiv"))


@dataclass(frozen=True)
class NetworkProtectionField:
    section: str
    name: str
    label: str
    offset: int
    unit: str = ""
    transform: str = "raw"
    choices: tuple[tuple[int, str], ...] = ()
    help: str = ""

    def key(self, cpu: int) -> str:
        return f"UC{cpu}.{self.section}.{self.name}"


FIELDS = (
    NetworkProtectionField("SA1", "ubLaendercode", "Ländercode", 0, choices=COUNTRY_CHOICES),
    NetworkProtectionField("SA1", "ubFesteSchutzart", "Feste Schutzart", 1, choices=PROTECTION_CHOICES,
                           help="Legacy-Profilbezeichnung des Überwachungscontrollers; kein Nachweis aktueller Normkonformität."),
    NetworkProtectionField("SA1", "usSpannungUntenFix", "Unterspannungsgrenze fest", 2, "V", "voltage_low"),
    NetworkProtectionField("SA1", "usSpannungObenFix", "Überspannungsgrenze fest", 3, "V", "voltage_high"),
    NetworkProtectionField("SA1", "ubAbschaltzeitUFix", "Abschaltzeit Spannung fest", 4, "s", "trip_rev1"),
    NetworkProtectionField("SA1", "usFrequenzUntenFix", "Unterfrequenzgrenze fest", 5, "Hz", "frequency_low"),
    NetworkProtectionField("SA1", "usFrequenzObenFix", "Überfrequenzgrenze fest", 6, "Hz", "frequency_high"),
    # The position in the repeated voltage/frequency sequence identifies
    # offset 7 as the fixed frequency trip time.
    NetworkProtectionField("SA1", "ubAbschaltzeitFFix", "Abschaltzeit Frequenz fest", 7, "s", "trip_rev1"),
    NetworkProtectionField("SA1", "fImpedanzAktiv", "Impedanzschutz", 8, choices=BOOLEAN_CHOICES),
    NetworkProtectionField("SA1", "usImpedanzsprung", "Impedanzsprung", 9, "Ohm", "hundredth"),
    NetworkProtectionField("SA1", "ubAbschaltzeitImp", "Abschaltzeit Impedanz", 10, "s", "tenth"),
    NetworkProtectionField("SA2", "usSpannungUntenVariabel", "Unterspannungsgrenze variabel", 11, "V", "voltage_low"),
    NetworkProtectionField("SA2", "usSpannungObenVariabel", "Überspannungsgrenze variabel", 12, "V", "voltage_high"),
    NetworkProtectionField("SA2", "ubAbschaltzeitUVariabel", "Abschaltzeit Spannung variabel", 13, "s", "trip_rev1"),
    NetworkProtectionField("SA2", "usFrequenzUntenVariabel", "Unterfrequenzgrenze variabel", 14, "Hz", "frequency_low"),
    NetworkProtectionField("SA2", "usFrequenzObenVariabel", "Überfrequenzgrenze variabel", 15, "Hz", "frequency_high"),
    NetworkProtectionField("SA2", "ubAbschaltzeitFVariabel", "Abschaltzeit Frequenz variabel", 16, "s", "trip_rev1"),
    NetworkProtectionField("SA2", "ubMittelwertU10min", "Mittelwert Spannung (10 min)", 17, "V", "voltage_high"),
)


def validate_network_cpu(cpu: int) -> int:
    value = int(cpu)
    if value not in NETWORK_PROTECTION_CPUS:
        raise ValueError(f"Netzschutz ist nur für CPU 1 oder CPU 2 verfügbar, erhalten: {value}")
    return value


def _number(value: object) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ungültiger Zahlenwert: {value!r}") from exc


def _decoded(raw: int, transform: str) -> int | float:
    if transform == "voltage_low":
        return 230 - raw
    if transform == "voltage_high":
        return 230 + raw
    if transform == "frequency_low":
        return round((500 - raw) / 10, 1)
    if transform == "frequency_high":
        return round((500 + raw) / 10, 1)
    if transform == "trip_rev1":
        return round((raw * 9 + 5) / 100, 2)
    if transform == "hundredth":
        return round(raw / 100, 2)
    if transform == "tenth":
        return round(raw / 10, 1)
    return raw


def _encoded(value: object, transform: str) -> int:
    number = _number(value)
    if transform == "voltage_low":
        raw = round(230 - number)
    elif transform == "voltage_high":
        raw = round(number - 230)
    elif transform == "frequency_low":
        raw = round(500 - number * 10)
    elif transform == "frequency_high":
        raw = round(number * 10 - 500)
    elif transform == "trip_rev1":
        raw = round((number * 100 - 5) / 9)
    elif transform == "hundredth":
        raw = round(number * 100)
    elif transform == "tenth":
        raw = round(number * 10)
    else:
        raw = round(number)
    if not 0 <= raw <= 255:
        raise ValueError(f"Wert {value!r} ergibt Rohwert {raw}; erlaubt sind 0 bis 255")
    return raw


def _field_by_key(cpu: int, key: str) -> NetworkProtectionField:
    cpu = validate_network_cpu(cpu)
    for field in FIELDS:
        if field.key(cpu) == key:
            return field
    raise KeyError(f"unbekanntes Netzschutzfeld: {key}")


def network_protection_schema(cpu: int) -> dict:
    cpu = validate_network_cpu(cpu)
    return {
        "cpu": cpu,
        "block": NETWORK_PROTECTION_BLOCK,
        "name": f"Netzschutz · Überwachungs-CPU {cpu}",
        "critical": True,
        "fields": [
            {
                "key": field.key(cpu),
                "label": field.label,
                "type": "byte",
                "size": 1,
                "offset": field.offset,
                "unit": field.unit,
                "write": True,
                "critical": True,
                "choices": [{"value": value, "label": label} for value, label in field.choices],
                "help": field.help,
            }
            for field in FIELDS
        ],
    }


def decode_network_protection(cpu: int, payload: bytes) -> list[dict]:
    cpu = validate_network_cpu(cpu)
    if len(payload) < NETWORK_PROTECTION_PAYLOAD_LENGTH:
        raise ValueError(
            f"CPU {cpu} Block 16 ist zu kurz: {len(payload)} statt mindestens {NETWORK_PROTECTION_PAYLOAD_LENGTH} Byte"
        )
    output = []
    for field in FIELDS:
        raw = int(payload[field.offset])
        edit_value = raw if field.choices else _decoded(raw, field.transform)
        choice_label = next((label for value, label in field.choices if value == raw), None)
        output.append({
            "key": field.key(cpu),
            "label": field.label,
            "raw": raw,
            "value": choice_label if choice_label is not None else edit_value,
            "edit_value": edit_value,
            "unit": field.unit,
            "type": "byte",
            "size": 1,
            "offset": field.offset,
            "write": True,
            "reserved": False,
            "critical": True,
            "choices": [{"value": value, "label": label} for value, label in field.choices],
            "min": None,
            "max": None,
            "step": 0.01 if field.transform in {"trip_rev1", "hundredth"} else (0.1 if field.unit in {"Hz", "s"} else 1),
            "help": field.help,
        })
    return output


def encode_network_protection_value(payload: bytearray, cpu: int, key: str, value: object) -> None:
    field = _field_by_key(cpu, key)
    if field.offset >= len(payload):
        raise ValueError(f"Netzschutzpayload ist zu kurz für {key}")
    raw = _encoded(value, "raw" if field.choices else field.transform)
    payload[field.offset] = raw
