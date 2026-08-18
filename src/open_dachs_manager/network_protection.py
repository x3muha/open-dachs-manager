"""Reviewed MSR2 layouts for the two network-monitor CPUs.

The regulator exposes three confirmed blocks in each addressed network CPU:

* block 16: the legacy 18-byte controller layout already used by the product;
* block 20: the newer 59-byte protection-configuration layout;
* block 21: the newer 56-byte live network-measurement layout.

Blocks 16 and 20 have a reviewed write service.  In layout 4 the original
DataMap derives service 21 (``block + 1``) as the full-payload write command
for block 20.  Block 21 is the corresponding live-measurement service and has
no write command; it therefore remains strictly read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from math import floor, isfinite
import re


# Backward-compatible names for the already published block-16 interface.
NETWORK_PROTECTION_BLOCK = 16
NETWORK_PROTECTION_PAYLOAD_LENGTH = 18

NETWORK_PROTECTION_CPUS = (1, 2)
NETWORK_PROTECTION_BLOCKS = (16, 20, 21)
NETWORK_PROTECTION_PAYLOAD_LENGTHS = {16: 18, 20: 59, 21: 56}
NETWORK_PROTECTION_WRITABLE_BLOCKS = (16, 20)
NETWORK_PROTECTION_BACKUP_BLOCKS = (16,)


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

LAYOUT4_PROTECTION_CHOICES = (
    (0, "Unbekannt"),
    (1, "Benutzer"),
    (2, "VDE 0126"),
    (3, "G83"),
    (4, "VDE 4105"),
    (5, "CEI 0-21"),
    (6, "DK 5940"),
    (7, "RD 1699"),
    (8, "G83/2"),
    (9, "Fehlt"),
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
    size: int = 1
    signed: bool = False
    write: bool = True

    def key(self, cpu: int) -> str:
        if self.section in {"NetzKonfig", "Netzwerte"}:
            return f"{self.section}{cpu}.{self.name}"
        return f"UC{cpu}.{self.section}.{self.name}"

    @property
    def field_type(self) -> str:
        return "short" if self.size == 2 else "byte"


# Legacy layout 1-3, block 16.  Keep ``FIELDS`` as the public compatibility
# alias used by existing callers and tests.
FIELDS = (
    NetworkProtectionField("SA1", "ubLaendercode", "Ländercode", 0, choices=COUNTRY_CHOICES),
    NetworkProtectionField(
        "SA1", "ubFesteSchutzart", "Feste Schutzart", 1,
        choices=PROTECTION_CHOICES,
        help=(
            "Legacy-Profilbezeichnung des Überwachungscontrollers; "
            "kein Nachweis aktueller Normkonformität."
        ),
    ),
    NetworkProtectionField("SA1", "usSpannungUntenFix", "Unterspannungsgrenze fest", 2, "V", "voltage_low"),
    NetworkProtectionField("SA1", "usSpannungObenFix", "Überspannungsgrenze fest", 3, "V", "voltage_high"),
    NetworkProtectionField("SA1", "ubAbschaltzeitUFix", "Abschaltzeit Spannung fest", 4, "s", "trip_rev1"),
    NetworkProtectionField("SA1", "usFrequenzUntenFix", "Unterfrequenzgrenze fest", 5, "Hz", "frequency_low"),
    NetworkProtectionField("SA1", "usFrequenzObenFix", "Überfrequenzgrenze fest", 6, "Hz", "frequency_high"),
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


_CONFIG_FIELDS = (
    NetworkProtectionField(
        "NetzKonfig", "ubSchutzart", "Schutzprofil", 0,
        choices=LAYOUT4_PROTECTION_CHOICES,
        help=(
            "Profilbezeichnung aus der historischen Controllerdefinition; "
            "sie ist kein Nachweis aktueller Normkonformität."
        ),
        write=False,
    ),
    NetworkProtectionField("NetzKonfig", "usSpannung1Unten", "Unterspannungsgrenze Stufe 1", 1, "V", "hundredth", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usSpannung1Oben", "Überspannungsgrenze Stufe 1", 3, "V", "hundredth", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usAbschaltzeitU1Oben", "Abschaltzeit Überspannung Stufe 1", 5, "s", "trip_u1", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usAbschaltzeitU1Unten", "Abschaltzeit Unterspannung Stufe 1", 7, "s", "trip_u1", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usSpannung2Unten", "Unterspannungsgrenze Stufe 2", 9, "V", "hundredth", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usSpannung2Oben", "Überspannungsgrenze Stufe 2", 11, "V", "hundredth", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usAbschaltzeitU2Oben", "Abschaltzeit Überspannung Stufe 2", 13, "s", "trip_u2", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usAbschaltzeitU2Unten", "Abschaltzeit Unterspannung Stufe 2", 15, "s", "trip_u2", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usFrequenz1Unten", "Unterfrequenzgrenze Stufe 1", 17, "Hz", "thousandth", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usFrequenz1Oben", "Überfrequenzgrenze Stufe 1", 19, "Hz", "thousandth", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usAbschaltzeitFrequenz1Oben", "Abschaltzeit Überfrequenz Stufe 1", 21, "s", "trip_f1_high", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usAbschaltzeitFrequenz1Unten", "Abschaltzeit Unterfrequenz Stufe 1", 23, "s", "trip_f1_low", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usFrequenz2Unten", "Unterfrequenzgrenze Stufe 2", 25, "Hz", "thousandth", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usFrequenz2Oben", "Überfrequenzgrenze Stufe 2", 27, "Hz", "thousandth", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usAbschaltzeitFrequenz2Oben", "Abschaltzeit Überfrequenz Stufe 2", 29, "s", "trip_f2_high", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usAbschaltzeitFrequenz2Unten", "Abschaltzeit Unterfrequenz Stufe 2", 31, "s", "trip_f2_low", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "usFrequenzObenRd", "Frequenzgrenze Leistungsreduktion", 33, "Hz", "thousandth", size=2, write=False),
    NetworkProtectionField("NetzKonfig", "ubAbschaltzeitFrRd", "Abschaltzeit Leistungsreduktion", 35, "s", "rd_trip", write=False),
    NetworkProtectionField("NetzKonfig", "fSpannung1Oben", "Überspannung Stufe 1 aktiv", 36, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fSpannung1Unten", "Unterspannung Stufe 1 aktiv", 37, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fSpannung2Oben", "Überspannung Stufe 2 aktiv", 38, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fSpannung2Unten", "Unterspannung Stufe 2 aktiv", 39, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fU10min", "10-Minuten-Spannungsgrenze aktiv", 40, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fFrequenz1Oben", "Überfrequenz Stufe 1 aktiv", 41, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fFrequenz1Unten", "Unterfrequenz Stufe 1 aktiv", 42, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fFrequenz2Oben", "Überfrequenz Stufe 2 aktiv", 43, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fFrequenz2Unten", "Unterfrequenz Stufe 2 aktiv", 44, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fFrequenzRd", "Frequenzabhängige Leistungsreduktion aktiv", 45, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fImpedanz", "Impedanzsprungschutz aktiv", 46, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fImpedanzLom", "Loss-of-Mains-Schutz aktiv", 47, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "sImpedanzsprung", "Grenzwert Impedanzsprung", 48, "Ohm", "hundredth", size=2, signed=True, write=False),
    NetworkProtectionField("NetzKonfig", "ubAbschaltzeitImpedanz", "Abschaltzeit Impedanzsprung", 50, "s", "tenth", write=False),
    NetworkProtectionField(
        "NetzKonfig", "sImpedanzsprungLom", "LOM-Impedanzsprung (Rohwert)", 51,
        help=(
            "Die historische Oberfläche unterdrückt die physikalische Anzeige "
            "wegen einer dort dokumentierten Fehlbeschriftung; deshalb bleibt "
            "dieser Wert bewusst ohne erfundene Einheit oder Skalierung."
        ),
        size=2, signed=True, write=False,
    ),
    NetworkProtectionField("NetzKonfig", "fTelescatto", "Fernabschaltung aktiv (Telescatto)", 53, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fLocale", "Lokaler Befehl aktiv", 54, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fComunicazione", "Kommunikationssignal aktiv", 55, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "fComunicazioneHw", "Hardware-Kommunikation aktiv", 56, choices=BOOLEAN_CHOICES, write=False),
    NetworkProtectionField("NetzKonfig", "usSpannung10min", "Spannungsgrenze 10-Minuten-Mittelwert", 57, "V", "voltage_10min", size=2, write=False),
)

# The historic GUI tagged these fields as non-editable, but the underlying
# layout-4 DataMap assigns block 20 the full-payload write service 21.  Keep the
# source-oriented declarations above intact and make the reviewed protocol
# capability explicit in one place.
CONFIG_FIELDS = tuple(replace(field, write=True) for field in _CONFIG_FIELDS)


LIVE_FIELDS = (
    NetworkProtectionField("Netzwerte", "usMeanVoltageL1", "Spannung L1", 0, "V", "hundredth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usMeanVoltageL2", "Spannung L2", 2, "V", "hundredth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usMeanVoltageL3", "Spannung L3", 4, "V", "hundredth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usMeanCurrentL1", "Generatorstrom L1", 6, "A", "hundredth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usMeanCurrentL2", "Generatorstrom L2", 8, "A", "hundredth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usMeanCurrentL3", "Generatorstrom L3", 10, "A", "hundredth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usMeanFrequencyL1", "Netzfrequenz L1", 12, "Hz", "thousandth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usMeanFrequencyL2", "Netzfrequenz L2", 14, "Hz", "thousandth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usMeanFrequencyL3", "Netzfrequenz L3", 16, "Hz", "thousandth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "sImpedanz60sL1", "Impedanz 60 s L1", 18, "Ohm", "thousandth", size=2, signed=True, write=False),
    NetworkProtectionField("Netzwerte", "sImpedanz60sL2", "Impedanz 60 s L2", 20, "Ohm", "thousandth", size=2, signed=True, write=False),
    NetworkProtectionField("Netzwerte", "sImpedanz60sL3", "Impedanz 60 s L3", 22, "Ohm", "thousandth", size=2, signed=True, write=False),
    NetworkProtectionField("Netzwerte", "sImpedanzLomL1", "LOM-Impedanz L1", 24, "Ohm", "thousandth", size=2, signed=True, write=False),
    NetworkProtectionField("Netzwerte", "sImpedanzLomL2", "LOM-Impedanz L2", 26, "Ohm", "thousandth", size=2, signed=True, write=False),
    NetworkProtectionField("Netzwerte", "sImpedanzLomL3", "LOM-Impedanz L3", 28, "Ohm", "thousandth", size=2, signed=True, write=False),
    NetworkProtectionField("Netzwerte", "sMeanPowerL1", "Wirkleistung L1", 30, "W", size=2, signed=True, write=False),
    NetworkProtectionField("Netzwerte", "sMeanPowerL2", "Wirkleistung L2", 32, "W", size=2, signed=True, write=False),
    NetworkProtectionField("Netzwerte", "sMeanPowerL3", "Wirkleistung L3", 34, "W", size=2, signed=True, write=False),
    NetworkProtectionField("Netzwerte", "usWinkelU1U2", "Winkel U1 – U2", 36, "°", "tenth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usWinkelU1U3", "Winkel U1 – U3", 38, "°", "tenth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usWinkelI1I2", "Winkel I1 – I2", 40, "°", "tenth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usWinkelI1I3", "Winkel I1 – I3", 42, "°", "tenth", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usKalibrierwertU1", "Kalibrierfaktor L1", 44, transform="factor_plus_one", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usKalibrierwertU2", "Kalibrierfaktor L2", 46, transform="factor_plus_one", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usKalibrierwertU3", "Kalibrierfaktor L3", 48, transform="factor_plus_one", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usWinkelU1I1", "Cosinus Phi L1", 50, transform="factor_plus_one", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usWinkelU2I2", "Cosinus Phi L2", 52, transform="factor_plus_one", size=2, write=False),
    NetworkProtectionField("Netzwerte", "usWinkelU3I3", "Cosinus Phi L3", 54, transform="factor_plus_one", size=2, write=False),
)


FIELDS_BY_BLOCK = {16: FIELDS, 20: CONFIG_FIELDS, 21: LIVE_FIELDS}


def validate_network_cpu(cpu: int) -> int:
    if type(cpu) is not int:
        raise ValueError(f"Netzschutz ist nur für CPU 1 oder CPU 2 verfügbar, erhalten: {cpu!r}")
    if cpu not in NETWORK_PROTECTION_CPUS:
        raise ValueError(f"Netzschutz ist nur für CPU 1 oder CPU 2 verfügbar, erhalten: {cpu}")
    return cpu


def validate_network_block(block: int) -> int:
    if type(block) is not int:
        raise ValueError(f"Unbekannter Netzschutzblock: {block!r}")
    if block not in NETWORK_PROTECTION_BLOCKS:
        raise ValueError(
            f"Unbekannter Netzschutzblock {block}; bestätigt sind 16, 20 und 21"
        )
    return block


def network_protection_payload_length(block: int) -> int:
    return NETWORK_PROTECTION_PAYLOAD_LENGTHS[validate_network_block(block)]


def network_protection_name(cpu: int, block: int = NETWORK_PROTECTION_BLOCK) -> str:
    cpu = validate_network_cpu(cpu)
    block = validate_network_block(block)
    if block == 16:
        return f"Netzschutz · Überwachungs-CPU {cpu}"
    if block == 20:
        return f"Netzschutz-Parameter · Überwachungs-CPU {cpu}"
    return f"Live-Netzwerte · Überwachungs-CPU {cpu}"


def _number(value: object) -> float:
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ungültiger Zahlenwert: {value!r}") from exc
    if not isfinite(number):
        raise ValueError(f"ungültiger Zahlenwert: {value!r}")
    return number


def _exact_scaled_integer(
    value: object,
    scale: int,
    label: str,
) -> int:
    """Encode a layout-4 decimal without rounding or silent truncation."""
    try:
        number = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"ungültiger Zahlenwert: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"ungültiger Zahlenwert: {value!r}")
    scaled = number * scale
    integral = scaled.to_integral_value()
    if scaled != integral:
        decimals = len(str(scale)) - 1
        raise ValueError(
            f"{label} ist nur mit höchstens {decimals} Nachkommastellen darstellbar"
        )
    return int(integral)


def _profile_trip_adjustment(profile: int, transform: str) -> int | None:
    """Return the layout-4 raw correction, or ``None`` for a fixed display.

    VDE 0126 and VDE 4105 force the first-stage U/F display to 0.1 s
    independently of the stored word.  Such a value has no unique physical
    inverse; callers can still use the explicit ``raw:<word>`` syntax.
    """
    if transform == "trip_u1":
        if profile in {2, 4}:
            return None
        return 2 if profile == 5 else (1 if profile in {1, 3, 6, 7, 8, 9} else 0)
    if transform == "trip_u2":
        if profile in {2, 4}:
            return 8
        if profile == 5:
            return 2
        return 1 if profile in {1, 3, 6, 7, 8, 9} else 0
    if transform == "trip_f1_low":
        if profile in {2, 4}:
            return None
        return 4 if profile == 5 else (1 if profile in {1, 3, 6, 7, 8, 9} else 0)
    if transform == "trip_f1_high":
        if profile in {2, 4}:
            return None
        if profile in {5, 6}:
            return 3
        if profile == 8:
            return 2
        return 1 if profile in {1, 3, 7, 9} else 0
    if transform == "trip_f2_low":
        if profile in {2, 4}:
            return 8
        if profile == 5:
            return 4
        if profile == 6:
            return 3
        return 1 if profile in {1, 3, 7, 8, 9} else 0
    if transform == "trip_f2_high":
        if profile in {2, 4}:
            return 8
        if profile in {5, 6}:
            return 3
        if profile == 8:
            return 2
        return 1 if profile in {1, 3, 7, 9} else 0
    raise ValueError(f"unbekannte profilabhängige Skalierung: {transform}")


def _profile_trip(raw: int, profile: int, transform: str) -> float:
    adjustment = _profile_trip_adjustment(profile, transform)
    if adjustment is None:
        return 0.1
    return round((raw + adjustment) / 100, 2)


def _decoded(raw: int, transform: str, profile: int = 0) -> int | float:
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
    if transform == "thousandth":
        return round(raw / 1000, 3)
    if transform == "tenth":
        return round(raw / 10, 1)
    if transform.startswith("trip_") and transform != "trip_rev1":
        return _profile_trip(raw, profile, transform)
    if transform == "rd_trip":
        # Historical source: Math.round((((value + 2) * 9) + 5) / 100).
        return floor(((((raw + 2) * 9) + 5) / 100) + 0.5)
    if transform == "voltage_10min":
        return floor((raw / 10) + 0.5) / 10
    if transform == "factor_plus_one":
        return round((raw / 1_000_000) + 1, 6)
    return raw


def _encoded(
    value: object,
    field: NetworkProtectionField,
    *,
    profile: int = 0,
    current_raw: int | None = None,
) -> int:
    raw_override = re.fullmatch(r"\s*raw\s*:\s*(-?\d+)\s*", str(value), re.IGNORECASE)
    if raw_override:
        raw = int(raw_override.group(1))
    else:
        transform = "raw" if field.choices else field.transform
        number = _number(value)
        if field.choices:
            raw = (
                _exact_scaled_integer(value, 1, field.label)
                if field.section == "NetzKonfig"
                else round(number)
            )
            known = {choice_value for choice_value, _label in field.choices}
            if field.section == "NetzKonfig" and raw not in known:
                raise ValueError(
                    f"unbekannter Auswahlwert {raw} für {field.label}; "
                    "für einen ausdrücklich gewünschten Rohwert raw:<Rohwert> verwenden"
                )
        elif transform == "voltage_low":
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
            raw = (
                _exact_scaled_integer(value, 100, field.label)
                if field.section == "NetzKonfig"
                else round(number * 100)
            )
        elif transform == "thousandth":
            raw = (
                _exact_scaled_integer(value, 1000, field.label)
                if field.section == "NetzKonfig"
                else round(number * 1000)
            )
        elif transform == "tenth":
            raw = (
                _exact_scaled_integer(value, 10, field.label)
                if field.section == "NetzKonfig"
                else round(number * 10)
            )
        elif transform.startswith("trip_"):
            adjustment = _profile_trip_adjustment(profile, transform)
            if adjustment is None:
                raise ValueError(
                    "dieses Schutzprofil zeigt für das Feld fest 0,1 s an; "
                    "für eine eindeutige Änderung raw:<Rohwert> verwenden"
                )
            raw = _exact_scaled_integer(value, 100, field.label) - adjustment
        elif transform == "rd_trip":
            if (
                current_raw is not None
                and number == _decoded(current_raw, transform, profile)
            ):
                raw = current_raw
            else:
                raise ValueError(
                    "diese ganzzahlige Anzeige besitzt keine eindeutige "
                    "Rückabbildung; raw:<Rohwert> verwenden"
                )
        elif transform == "voltage_10min":
            if (
                current_raw is not None
                and number == _decoded(current_raw, transform, profile)
            ):
                raw = current_raw
            else:
                raise ValueError(
                    "die gerundete 10-Minuten-Anzeige besitzt keine eindeutige "
                    "Rückabbildung; raw:<Rohwert> verwenden"
                )
        else:
            if field.section == "NetzKonfig":
                raw = _exact_scaled_integer(value, 1, field.label)
            else:
                raw = round(number)

    bits = field.size * 8
    minimum = -(1 << (bits - 1)) if field.signed else 0
    maximum = (1 << (bits - 1)) - 1 if field.signed else (1 << bits) - 1
    if not minimum <= raw <= maximum:
        raise ValueError(
            f"Wert {value!r} ergibt Rohwert {raw}; erlaubt sind {minimum} bis {maximum}"
        )
    return raw


def _field_by_key(cpu: int, key: str, block: int) -> NetworkProtectionField:
    cpu = validate_network_cpu(cpu)
    block = validate_network_block(block)
    for field in FIELDS_BY_BLOCK[block]:
        if field.key(cpu) == key:
            return field
    raise KeyError(f"unbekanntes Netzschutzfeld in Block {block}: {key}")


def _step(field: NetworkProtectionField) -> float | int:
    if field.transform == "rd_trip":
        return 1
    if field.transform == "voltage_10min":
        return 0.1
    if field.transform in {"hundredth", "trip_rev1"} or field.transform.startswith("trip_"):
        return 0.01
    if field.transform == "thousandth":
        return 0.001
    if field.transform == "factor_plus_one":
        return 0.000001
    if field.transform == "tenth" or field.unit in {"Hz", "s"}:
        return 0.1
    return 1


def network_protection_schema(cpu: int, block: int = NETWORK_PROTECTION_BLOCK) -> dict:
    cpu = validate_network_cpu(cpu)
    block = validate_network_block(block)
    fields = FIELDS_BY_BLOCK[block]
    writable = block in NETWORK_PROTECTION_WRITABLE_BLOCKS
    read_only_reason = ""
    if block == 21:
        read_only_reason = (
            "Block 21 enthält laufende Messwerte und besitzt in der geprüften "
            "Original-DataMap keinen Schreibdienst."
        )
    return {
        "cpu": cpu,
        "block": block,
        "target_key": f"{cpu}:{block}",
        "name": network_protection_name(cpu, block),
        "tab_label": (
            "Netzschutz (Legacy)" if block == 16
            else "Schutzparameter" if block == 20
            else "Live-Netzwerte"
        ),
        "critical": True,
        "layout": 1 if block == 16 else 4,
        "payload_length": network_protection_payload_length(block),
        "writable": writable,
        "backup_eligible": block in NETWORK_PROTECTION_BACKUP_BLOCKS,
        "live_values": block == 21,
        "read_only_reason": read_only_reason,
        "fields": [
            {
                "key": field.key(cpu),
                "label": field.label,
                "type": field.field_type,
                "size": field.size,
                "offset": field.offset,
                "unit": field.unit,
                "write": field.write,
                "critical": True,
                "choices": [
                    {"value": value, "label": label}
                    for value, label in field.choices
                ],
                "help": field.help,
            }
            for field in fields
        ],
    }


def network_protection_schemas() -> list[dict]:
    return [
        network_protection_schema(cpu, block)
        for cpu in NETWORK_PROTECTION_CPUS
        for block in NETWORK_PROTECTION_BLOCKS
    ]


def decode_network_protection(
    cpu: int,
    payload: bytes,
    block: int = NETWORK_PROTECTION_BLOCK,
) -> list[dict]:
    cpu = validate_network_cpu(cpu)
    block = validate_network_block(block)
    expected = network_protection_payload_length(block)
    if len(payload) != expected:
        raise ValueError(
            f"CPU {cpu} Block {block} hat {len(payload)} statt exakt {expected} Byte"
        )
    profile = int(payload[0]) if block == 20 else 0
    output = []
    for field in FIELDS_BY_BLOCK[block]:
        raw = int.from_bytes(
            payload[field.offset:field.offset + field.size],
            "little",
            signed=field.signed,
        )
        edit_value = raw if field.choices else _decoded(raw, field.transform, profile)
        choice_label = next(
            (label for value, label in field.choices if value == raw),
            None,
        )
        output.append({
            "key": field.key(cpu),
            "label": field.label,
            "raw": raw,
            "value": choice_label if choice_label is not None else edit_value,
            "edit_value": edit_value,
            "unit": field.unit,
            "type": field.field_type,
            "size": field.size,
            "offset": field.offset,
            "write": field.write,
            "reserved": False,
            "critical": True,
            "choices": [
                {"value": value, "label": label}
                for value, label in field.choices
            ],
            "min": None,
            "max": None,
            "step": _step(field),
            "help": field.help,
        })
    return output


def encode_network_protection_value(
    payload: bytearray,
    cpu: int,
    key: str,
    value: object,
    block: int = NETWORK_PROTECTION_BLOCK,
) -> None:
    block = validate_network_block(block)
    if block not in NETWORK_PROTECTION_WRITABLE_BLOCKS:
        raise PermissionError(
            f"Netzschutz CPU {validate_network_cpu(cpu)}, Block {block} ist nur lesbar"
        )
    field = _field_by_key(cpu, key, block)
    if not field.write:
        raise PermissionError(f"Netzschutzfeld {key} ist nur lesbar")
    expected = network_protection_payload_length(block)
    if len(payload) != expected:
        raise ValueError(
            f"CPU {cpu} Block {block} hat {len(payload)} statt exakt {expected} Byte"
        )
    current_raw = int.from_bytes(
        payload[field.offset:field.offset + field.size],
        "little",
        signed=field.signed,
    )
    profile = int(payload[0]) if block == 20 else 0
    raw = _encoded(
        value,
        field,
        profile=profile,
        current_raw=current_raw,
    )
    payload[field.offset:field.offset + field.size] = raw.to_bytes(
        field.size, "little", signed=field.signed
    )
