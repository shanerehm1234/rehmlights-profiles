#!/usr/bin/env python3
"""
GDTF Cooker - Convert GDTF fixture profiles to Rehmlights (VIBE/VECTR) format.

Parses .gdtf files (ZIP archives containing description.xml), extracts DMX
channel maps, wheel data, physical ranges, and strobe values. Outputs either
per-fixture JSON files for the online rehmlights-profiles catalog, or a legacy
C factory_profiles.h for in-firmware bundling.

Usage:
    # JSON output (catalog contributions — most common)
    python tools/gdtf_cooker.py cook-json <file.gdtf> [--output-dir sources/]

    # Inspect a GDTF file
    python tools/gdtf_cooker.py inspect <file.gdtf>

    # Legacy C header output (for bundling profiles into firmware)
    python tools/gdtf_cooker.py cook <file.gdtf> [--output-dir .]

    # Download from GDTF Share (needs GDTF_USER + GDTF_PASSWORD env vars)
    python tools/gdtf_cooker.py download --list
    python tools/gdtf_cooker.py download --search "YPS"

    # Validate a parse against a known-good CSV
    python tools/gdtf_cooker.py validate <file.gdtf> --expected-csv profiles.csv
"""

import argparse
import csv
import json
import os
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

# =============================================================================
# Constants
# =============================================================================

MAX_WHEEL_POSITIONS = 30
MAX_NAME_LENGTH = 22

# GDTF attribute → VECTR field mapping
# Value is tuple of (coarse_field, fine_field) or (field_only,)
ATTRIBUTE_MAP = {
    # Tier 1: Movement
    'Pan':              ('panChannel', 'panFineChannel'),
    'Tilt':             ('tiltChannel', 'tiltFineChannel'),

    # Tier 1: Intensity
    'Dimmer':           ('dimmerChannel', 'dimmerFineChannel'),

    # Tier 1: Speed
    'PositionMSpeed':   ('speedChannel',),

    # Tier 1: Shutter (channel only - strobe range parsed separately)
    'Shutter1':         ('shutterChannel',),

    # Tier 1: Color wheel
    'Color1':           ('colorChannel',),

    # Tier 1: Gobo wheel
    'Gobo1':            ('goboChannel',),

    # Tier 1: Prism
    'Prism1':           ('prismChannel',),
    'Prism1PosRotate':  ('prismRotationChannel',),
    'Prism1Rot':        ('prismRotationChannel',),
    'Prism2':           ('prism2Ch',),
    'Prism2PosRotate':  ('prism2RotationCh',),
    'Prism2Rot':        ('prism2RotationCh',),

    # Tier 1: Optics
    'Focus1':           ('focusChannel',),
    'Frost1':           ('frostCh',),

    # Tier 1: Lamp control
    'LampControl':      ('lampControlChannel',),

    # Tier 1: RGB additive mixing
    'ColorAdd_R':       ('redChannel',),
    'ColorAdd_G':       ('greenChannel',),
    'ColorAdd_B':       ('blueChannel',),
    'ColorAdd_W':       ('whiteChannel',),
    'ColorAdd_WW':      ('whiteChannel',),
    'ColorAdd_CW':      ('whiteChannel',),
    'ColorAdd_A':       ('amberChannel',),
    'ColorAdd_RY':      ('amberChannel',),
    'ColorAdd_UV':      ('uvChannel',),

    # Tier 1: Reset
    'FixtureGlobalReset': ('resetChannel',),

    # Tier 2: Zoom / Iris
    'Zoom':             ('zoomChannel',),
    'Iris':             ('irisChannel',),

    # Tier 2: Second gobo wheel
    'Gobo2':            ('gobo2Channel',),
    'Gobo1PosRotate':   ('goboRotationChannel',),
    'Gobo1Rot':         ('goboRotationChannel',),
    'Gobo2PosRotate':   ('gobo2RotationCh',),
    'Gobo2Rot':         ('gobo2RotationCh',),

    # Tier 2: Second color wheel
    'Color2':           ('color2Channel',),

    # Tier 2: Animation wheel
    'AnimationWheel1':  ('animationChannel',),

    # Tier 2: CMY subtractive mixing
    'Cyan':             ('cyanCh',),
    'Magenta':          ('magentaCh',),
    'Yellow':           ('yellowCh',),

    # Tier 2: Color temperature
    'CTO':              ('ctoCh',),
    'CTB':              ('ctbCh',),

    # Tier 3: Effects
    'Effects1':         ('effectsCh',),
}

# Prefix rules for matching GDTF attribute variants to canonical names.
# e.g. "Shutter1Strobe" starts with "Shutter1" → maps to Shutter1 for channel assignment
# Order matters: longer prefixes checked first
ATTRIBUTE_PREFIXES = [
    ('Gobo1PosRotate', 'Gobo1PosRotate'),
    ('Gobo2PosRotate', 'Gobo2PosRotate'),
    ('Gobo1Rot',       'Gobo1Rot'),
    ('Gobo2Rot',       'Gobo2Rot'),
    ('Gobo1SelectSpin', 'Gobo1'),
    ('Gobo1SelectShake', 'Gobo1'),
    ('Gobo1WheelSpin', None),  # Skip wheel-level effects
    ('Gobo1WheelShake', None),
    ('Gobo2SelectSpin', 'Gobo2'),
    ('Gobo2SelectShake', 'Gobo2'),
    ('Gobo2WheelSpin', None),
    ('Gobo2WheelShake', None),
    ('Color1WheelSpin', None),
    ('Color1WheelShake', None),
    ('Color2WheelSpin', None),
    ('Color2WheelShake', None),
    ('Shutter1Strobe',  'Shutter1'),
    ('Shutter1StrobeRandom', 'Shutter1'),
    ('Shutter1PulseClose', 'Shutter1'),
    ('Shutter1PulseOpen', 'Shutter1'),
    ('Prism1PosRotate', 'Prism1PosRotate'),
    ('Prism1SelectSpin', 'Prism1'),
    ('Prism1Shake',     'Prism1'),
    ('Prism2PosRotate', 'Prism2PosRotate'),
    ('Prism2SelectSpin', 'Prism2'),
    ('AnimationWheel1Rot', 'AnimationWheel1'),
    ('AnimationWheel1Audio', None),
    ('Effects1Rate',    None),
    ('Effects1Fade',    None),
    ('Effects1Adjust',  None),
    ('Effects1Pos',     'Effects1'),
    ('Effects1PosRotate', 'Effects1'),
    ('IrisStrobe',      None),  # Log but don't store
    ('IrisPulseClose',  None),
    ('IrisPulseOpen',   None),
    ('IrisRandomPulse', None),
    ('Control1',        None),  # Handled specially for reset detection
]

# Attributes that have wheel slot data (Color/Gobo wheels)
WHEEL_ATTRIBUTES = {
    'Color1': ('colorValues', 'colorNames', 'colorCount'),
    'Color2': ('color2Values', 'color2Names', 'color2Count'),
    'Gobo1':  ('goboValues', 'goboNames', 'goboCount'),
    'Gobo2':  ('gobo2Values', 'gobo2Names', 'gobo2Count'),
}


# =============================================================================
# Data Model
# =============================================================================

@dataclass
class VectrProfile:
    """Intermediate representation of a fixture profile."""
    # Identity
    name: str = ""
    manufacturer: str = ""
    mode_name: str = ""
    fixture_type: str = "FIXTURE_TYPE_LED_WHEEL"

    # Common channels (1-based DMX offset, 0 = unused)
    panChannel: int = 0
    panFineChannel: int = 0
    tiltChannel: int = 0
    tiltFineChannel: int = 0
    speedChannel: int = 0
    dimmerChannel: int = 0
    dimmerFineChannel: int = 0
    resetChannel: int = 0

    # HID lamp control
    lampControlChannel: int = 0
    lampOnValue: int = 0
    lampOffValue: int = 0

    # Wheel/HID shared channels
    colorChannel: int = 0
    goboChannel: int = 0
    prismChannel: int = 0
    shutterChannel: int = 0
    focusChannel: int = 0
    prismRotationChannel: int = 0
    prism2Ch: int = 0
    prism2RotationCh: int = 0
    frostCh: int = 0

    # Wheel data (Color1 / Gobo1)
    colorCount: int = 0
    colorValues: list = field(default_factory=list)
    colorNames: list = field(default_factory=list)
    goboCount: int = 0
    goboValues: list = field(default_factory=list)
    goboNames: list = field(default_factory=list)

    # RGB additive channels
    redChannel: int = 0
    greenChannel: int = 0
    blueChannel: int = 0
    whiteChannel: int = 0
    amberChannel: int = 0
    uvChannel: int = 0

    # --- Tier 2: New fields for v1.2 ---
    zoomChannel: int = 0
    irisChannel: int = 0
    gobo2Channel: int = 0
    goboRotationChannel: int = 0
    gobo2RotationCh: int = 0
    color2Channel: int = 0
    animationChannel: int = 0
    cyanCh: int = 0
    magentaCh: int = 0
    yellowCh: int = 0
    ctoCh: int = 0
    ctbCh: int = 0
    effectsCh: int = 0

    # Second wheel data (Color2 / Gobo2)
    gobo2Count: int = 0
    gobo2Values: list = field(default_factory=list)
    gobo2Names: list = field(default_factory=list)
    color2Count: int = 0
    color2Values: list = field(default_factory=list)
    color2Names: list = field(default_factory=list)

    # --- Tier 3: Metadata (not in C struct yet, for future use) ---
    pan_degrees: float = 0.0
    tilt_degrees: float = 0.0
    shutter_open_value: int = 0
    shutter_closed_value: int = 0
    strobe_start_value: int = 0
    strobe_end_value: int = 0
    dimmer_default_value: int = 0

    # Unrecognized channels (for inspect logging)
    unrecognized: list = field(default_factory=list)


# =============================================================================
# Parsing Utilities
# =============================================================================

def parse_offset(offset_str: str) -> tuple:
    """Parse GDTF Offset attribute like '1,2' into (coarse, fine).
    Returns (coarse, 0) for 8-bit single channels.
    Returns (0, 0) for virtual/missing channels."""
    if not offset_str or offset_str in ('None', ''):
        return (0, 0)
    parts = [int(x.strip()) for x in offset_str.split(',')]
    coarse = parts[0]
    fine = parts[1] if len(parts) > 1 else 0
    return (coarse, fine)


def parse_dmx_from(dmx_from_str: str) -> int:
    """Parse GDTF DMXFrom like '64/1' into integer DMX value.
    Format is 'value/resolution' where resolution 1 = 8-bit."""
    if not dmx_from_str or dmx_from_str in ('None', ''):
        return 0
    parts = dmx_from_str.split('/')
    value = int(parts[0])
    # Resolution 1 = 8-bit (0-255), Resolution 2 = 16-bit
    # We always want the 8-bit (coarse) value
    if len(parts) > 1:
        resolution = int(parts[1])
        if resolution == 2:
            value = value >> 8  # Take high byte for 16-bit values
    return value


def _truncate_to_bytes(s: str, max_bytes: int) -> str:
    """Truncate a string so its UTF-8 encoding fits in max_bytes (for C char arrays).
    Strips non-ASCII characters that cause multi-byte issues."""
    # Replace common unicode with ASCII equivalents
    s = s.replace('®', '').replace('™', '').replace('©', '').replace('°', 'deg')
    s = s.replace('\u2013', '-').replace('\u2014', '-').replace('\u2019', "'")
    # Encode and truncate at byte boundary
    encoded = s.encode('utf-8')
    if len(encoded) <= max_bytes:
        return s
    # Truncate bytes and decode safely
    truncated = encoded[:max_bytes]
    return truncated.decode('utf-8', errors='ignore')


def match_attribute(attr_name: str) -> Optional[str]:
    """Match a GDTF attribute name to our canonical attribute map key.

    Handles exact matches, prefix matching for variants, and common aliases.
    Returns None if the attribute should be skipped/ignored.
    Returns the canonical key for ATTRIBUTE_MAP lookup.
    """
    # Direct match first
    if attr_name in ATTRIBUTE_MAP:
        return attr_name

    # Check prefix rules (longer prefixes checked first by list order)
    for prefix, canonical in ATTRIBUTE_PREFIXES:
        if attr_name.startswith(prefix) or attr_name == prefix:
            return canonical  # None means skip

    # Common aliases
    aliases = {
        'Shutter': 'Shutter1',
        'Color': 'Color1',
        'Gobo': 'Gobo1',
        'Prism': 'Prism1',
        'Focus': 'Focus1',
        'Frost': 'Frost1',
        'Speed': 'PositionMSpeed',
        'StrobeFrequency': 'Shutter1',
        'ColorTemperature': 'CTO',
        'Reset': 'FixtureGlobalReset',
    }
    if attr_name in aliases:
        return aliases[attr_name]

    return None  # Unknown attribute


# =============================================================================
# GDTF Parser
# =============================================================================

class GdtfParser:
    """Parses .gdtf files into VectrProfile objects."""

    # Normalize inconsistent manufacturer names from GDTF Share
    MANUFACTURER_ALIASES = {
        'Chauvet': 'Chauvet DJ',      # bare "Chauvet" → Chauvet DJ
        'shehds': 'SHEHDS',           # case normalization
        'China': 'Generic',            # generic Chinese fixtures
    }

    def _normalize_manufacturer(self, mfg: str) -> str:
        """Normalize manufacturer name for consistency."""
        stripped = mfg.strip()
        return self.MANUFACTURER_ALIASES.get(stripped, stripped)

    def parse(self, filepath: str) -> list:
        """Parse a .gdtf file and return a list of VectrProfile (one per DMX mode)."""
        tree = self._extract_xml(filepath)
        root = tree.getroot()

        # GDTF spec: <GDTF> root contains <FixtureType> with name/manufacturer
        ft = root.find('FixtureType')
        if ft is None:
            ft = root  # Fallback for non-standard files

        fixture_name = ft.get('LongName', '') or ft.get('Name', '') or 'Unknown'
        fixture_name = fixture_name.strip()
        manufacturer = self._normalize_manufacturer(ft.get('Manufacturer', 'Unknown'))

        profiles = []
        for dmx_mode in root.findall('.//DMXMode'):
            profile = self._parse_dmx_mode(dmx_mode, fixture_name, manufacturer)
            profiles.append(profile)

        return profiles

    def _extract_xml(self, filepath: str) -> ET.ElementTree:
        """Extract and parse description.xml from a .gdtf ZIP archive."""
        with zipfile.ZipFile(filepath, 'r') as z:
            with z.open('description.xml') as f:
                return ET.parse(f)

    def _parse_dmx_mode(self, dmx_mode_el, fixture_name: str, manufacturer: str) -> VectrProfile:
        """Parse a single DMXMode element into a VectrProfile."""
        profile = VectrProfile()
        mode_name = dmx_mode_el.get('Name', 'Default')
        profile.manufacturer = manufacturer
        profile.mode_name = mode_name

        # Store fixture name and mode name separately
        profile.name = _truncate_to_bytes(fixture_name, MAX_NAME_LENGTH - 1)
        profile.mode_name = _truncate_to_bytes(mode_name, MAX_NAME_LENGTH - 1)

        # Parse each DMX channel
        for dmx_ch in dmx_mode_el.findall('.//DMXChannel'):
            self._parse_dmx_channel(dmx_ch, profile)

        # Auto-detect fixture type
        profile.fixture_type = self._detect_fixture_type(profile)

        return profile

    def _parse_dmx_channel(self, dmx_ch_el, profile: VectrProfile):
        """Parse a single DMXChannel element and assign to profile fields."""
        offset_str = dmx_ch_el.get('Offset', 'None')
        coarse, fine = parse_offset(offset_str)

        if coarse == 0:
            return  # Virtual channel, skip

        for logical_ch in dmx_ch_el.findall('LogicalChannel'):
            attr = logical_ch.get('Attribute', '')
            if not attr:
                continue

            # Capture default DMX value if present on logical channel
            default_val_str = logical_ch.get('Default', '0/1')
            default_val = parse_dmx_from(default_val_str)

            # Check for reset in Control channels
            if attr in ('Control1', 'Control') or attr.startswith('Control'):
                if self._has_reset_function(logical_ch):
                    profile.resetChannel = coarse
                    continue

            # Match attribute to canonical name
            canonical = match_attribute(attr)
            if canonical is None:
                # Explicitly skipped (e.g., wheel spin effects)
                continue

            if canonical not in ATTRIBUTE_MAP:
                profile.unrecognized.append((attr, coarse))
                continue

            # Assign channel number(s)
            fields = ATTRIBUTE_MAP[canonical]
            setattr(profile, fields[0], coarse)
            if len(fields) > 1 and fine > 0:
                setattr(profile, fields[1], fine)

            # Special handling for Dimmer default / 100%
            if canonical == 'Dimmer':
                # Try to find a "100%" or "Full" channel set
                dimmer_full = 255
                found_full = False
                for ch_func in logical_ch.findall('ChannelFunction'):
                    for cs in ch_func.findall('ChannelSet'):
                        cs_name = cs.get('Name', '').lower()
                        cs_dmx = parse_dmx_from(cs.get('DMXFrom', '0/1'))
                        if '100%' in cs_name or 'full' in cs_name or 'open' in cs_name:
                            dimmer_full = cs_dmx
                            found_full = True
                            # Don't break yet, if we find a 255 value, that's even better
                            if cs_dmx == 255: break
                    if found_full and dimmer_full == 255: break
                
                profile.dimmer_default_value = dimmer_full if found_full else (default_val if default_val > 0 else 255)

            # Parse sub-functions for wheel data, strobe, lamp, physical ranges
            self._parse_channel_details(logical_ch, profile, canonical)

    def _parse_channel_details(self, logical_ch_el, profile: VectrProfile, canonical: str):
        """Parse ChannelFunction/ChannelSet details for special attributes."""
        # Wheel slot extraction
        if canonical in WHEEL_ATTRIBUTES:
            self._parse_wheel_slots(logical_ch_el, profile, canonical)

        # Physical range (pan/tilt degrees)
        if canonical in ('Pan', 'Tilt'):
            self._parse_physical_range(logical_ch_el, profile, canonical)

        # Shutter/strobe ranges
        if canonical == 'Shutter1':
            self._parse_shutter_functions(logical_ch_el, profile)

        # Lamp control on/off values
        if canonical == 'LampControl':
            self._parse_lamp_control(logical_ch_el, profile)

    def _parse_wheel_slots(self, logical_ch_el, profile: VectrProfile, canonical: str):
        """Extract wheel slot names and DMX values from ChannelFunction/ChannelSet."""
        val_field, name_field, count_field = WHEEL_ATTRIBUTES[canonical]
        names = []
        values = []

        for ch_func in logical_ch_el.findall('ChannelFunction'):
            func_attr = ch_func.get('Attribute', '')

            # Only parse the static selection function (Color1, Gobo1, etc.)
            # Skip spin, shake, random, audio variants
            skip_suffixes = ('WheelSpin', 'WheelShake', 'WheelRandom', 'WheelAudio',
                            'SelectSpin', 'SelectShake', 'Random')
            if any(func_attr.endswith(s) for s in skip_suffixes):
                continue

            for ch_set in ch_func.findall('ChannelSet'):
                name = ch_set.get('Name', '')
                dmx_from = parse_dmx_from(ch_set.get('DMXFrom', '0/1'))
                if name:
                    names.append(_truncate_to_bytes(name, MAX_NAME_LENGTH - 1))
                    values.append(dmx_from)

        # Cap at MAX_WHEEL_POSITIONS
        names = names[:MAX_WHEEL_POSITIONS]
        values = values[:MAX_WHEEL_POSITIONS]

        setattr(profile, val_field, values)
        setattr(profile, name_field, names)
        setattr(profile, count_field, len(values))

    def _parse_physical_range(self, logical_ch_el, profile: VectrProfile, attr: str):
        """Extract PhysicalFrom/PhysicalTo for pan/tilt degree ranges."""
        for ch_func in logical_ch_el.findall('ChannelFunction'):
            phys_from = ch_func.get('PhysicalFrom')
            phys_to = ch_func.get('PhysicalTo')
            if phys_from is not None and phys_to is not None:
                try:
                    total_degrees = abs(float(phys_to) - float(phys_from))
                    if attr == 'Pan':
                        profile.pan_degrees = total_degrees
                    elif attr == 'Tilt':
                        profile.tilt_degrees = total_degrees
                except (ValueError, TypeError):
                    pass
                break  # Use the first (primary) function

    def _parse_shutter_functions(self, logical_ch_el, profile: VectrProfile):
        """Parse Shutter1 ChannelFunctions to find Open and Strobe DMX ranges."""
        functions = list(logical_ch_el.findall('ChannelFunction'))

        for i, ch_func in enumerate(functions):
            attr = ch_func.get('Attribute', '')
            func_name = ch_func.get('Name', '').lower()
            dmx_from = parse_dmx_from(ch_func.get('DMXFrom', '0/1'))
            phys_from = ch_func.get('PhysicalFrom')
            
            # Convert PhysicalFrom to float if it exists
            phys_val = -1.0
            try:
                if phys_from is not None: phys_val = float(phys_from)
            except (ValueError, TypeError): pass

            if attr in ('Shutter1', 'Shutter'):
                # Check physical value (1.0 = Open, 0.0 = Closed)
                if phys_val >= 1.0:
                    profile.shutter_open_value = dmx_from
                elif phys_val == 0.0 and 'Strobe' not in attr:
                    profile.shutter_closed_value = dmx_from

                # Check function name itself (some GDTF files put Open here)
                if any(kw in func_name for kw in ('open', 'steady', 'static', 'no strobe', 'on')):
                    if profile.shutter_open_value == 0 or dmx_from > 0:
                        profile.shutter_open_value = dmx_from
                elif any(kw in func_name for kw in ('close', 'blackout', 'off')):
                    profile.shutter_closed_value = dmx_from

                # Check ChannelSets for open/closed indicators
                for cs in ch_func.findall('ChannelSet'):
                    cs_name = cs.get('Name', '').lower()
                    cs_dmx = parse_dmx_from(cs.get('DMXFrom', '0/1'))
                    # Search for keywords that mean "Steady ON"
                    if any(kw in cs_name for kw in ('open', 'steady', 'static', 'no strobe', 'on')):
                        if profile.shutter_open_value == 0 or cs_dmx > 0:
                            profile.shutter_open_value = cs_dmx
                    elif any(kw in cs_name for kw in ('close', 'blackout', 'off')):
                        profile.shutter_closed_value = cs_dmx

            elif 'Strobe' in attr:
                # Only use the FIRST strobe range as our primary strobe value
                if profile.strobe_start_value == 0:
                    profile.strobe_start_value = dmx_from
                
                # Update end value
                if i + 1 < len(functions):
                    next_from = parse_dmx_from(functions[i + 1].get('DMXFrom', '0/1'))
                    if next_from > 0:
                        profile.strobe_end_value = next_from - 1
                    else:
                        profile.strobe_end_value = 255
                else:
                    profile.strobe_end_value = 255

    def _parse_lamp_control(self, logical_ch_el, profile: VectrProfile):
        """Extract lamp on/off DMX values from LampControl channel."""
        for ch_func in logical_ch_el.findall('ChannelFunction'):
            for cs in ch_func.findall('ChannelSet'):
                name = cs.get('Name', '').lower()
                dmx = parse_dmx_from(cs.get('DMXFrom', '0/1'))
                if 'on' in name and 'off' not in name:
                    profile.lampOnValue = dmx
                elif 'off' in name:
                    profile.lampOffValue = dmx

    def _has_reset_function(self, logical_ch_el) -> bool:
        """Check if a Control/Maintenance channel has a reset function."""
        for ch_func in logical_ch_el.findall('ChannelFunction'):
            func_attr = ch_func.get('Attribute', '').lower()
            if 'reset' in func_attr:
                return True
            for cs in ch_func.findall('ChannelSet'):
                name = cs.get('Name', '').lower()
                if 'reset' in name:
                    return True
        return False

    def _detect_fixture_type(self, profile: VectrProfile) -> str:
        """Determine VECTR fixture type from parsed channels."""
        has_lamp = profile.lampControlChannel > 0
        has_rgb = any([profile.redChannel, profile.greenChannel, profile.blueChannel])
        has_cmy = any([profile.cyanCh, profile.magentaCh, profile.yellowCh])
        has_color_wheel = profile.colorChannel > 0

        if has_lamp:
            return 'FIXTURE_TYPE_HID'
        elif has_rgb and not has_color_wheel:
            return 'FIXTURE_TYPE_RGBX'
        elif has_cmy and not has_color_wheel:
            return 'FIXTURE_TYPE_RGBX'  # CMY treated as RGBX variant
        elif has_color_wheel:
            return 'FIXTURE_TYPE_LED_WHEEL'
        else:
            # Fallback: if we have pan/tilt, assume LED_WHEEL
            return 'FIXTURE_TYPE_LED_WHEEL'


# =============================================================================
# C Header Writer
# =============================================================================

class CHeaderWriter:
    """Generates factory_profiles.h in the format the firmware expects."""

    def write(self, profiles: list, output_path: str):
        """Write profiles to a C header file."""
        lines = []
        lines.append("// Auto-generated by gdtf_cooker.py. DO NOT EDIT.")
        lines.append("")
        lines.append("#pragma once")
        lines.append("")
        lines.append('#include "FixtureProfiles.h"')
        lines.append("")
        lines.append("const FixtureProfile factoryProfiles[] = {")

        for i, p in enumerate(profiles):
            comma = "," if i < len(profiles) - 1 else ""
            lines.extend(self._format_profile(p, comma))

        lines.append("};")
        lines.append("")
        lines.append(f"const int factoryProfileCount = {len(profiles)};")
        lines.append("")
        lines.append("const char* factoryProfileNames[] = {")
        for i, p in enumerate(profiles):
            comma = "," if i < len(profiles) - 1 else ""
            name = p.name.replace('"', '\\"')
            lines.append(f'    "{name}"{comma}')
        lines.append("};")
        lines.append("")

        # Build sorted unique manufacturer list
        mfg_set = {}
        for p in profiles:
            mfg_key = p.manufacturer.strip().lower()
            if mfg_key and mfg_key not in mfg_set:
                mfg_set[mfg_key] = _truncate_to_bytes(p.manufacturer.strip(), MAX_NAME_LENGTH - 1)
        mfg_sorted = sorted(mfg_set.values(), key=lambda s: s.lower())

        lines.append(f"const int factoryMfgCount = {len(mfg_sorted)};")
        lines.append("")
        lines.append("const char* factoryMfgNames[] = {")
        for i, mfg in enumerate(mfg_sorted):
            comma = "," if i < len(mfg_sorted) - 1 else ""
            lines.append(f'    "{mfg.replace(chr(34), "")}"' + comma)
        lines.append("};")
        lines.append("")

        with open(output_path, 'w') as f:
            f.write('\n'.join(lines) + '\n')

        print(f"Generated {output_path} with {len(profiles)} profiles")

    def _format_profile(self, p: VectrProfile, comma: str) -> list:
        """Format a single profile as C struct initialization."""
        lines = []
        lines.append("    {")

        # Name (must fit in char[MAX_NAME_LENGTH] including null terminator)
        name = _truncate_to_bytes(p.name, MAX_NAME_LENGTH - 1).replace('"', '\\"')
        lines.append(f'      "{name}",  // name')

        # Manufacturer
        mfg = _truncate_to_bytes(p.manufacturer, MAX_NAME_LENGTH - 1).replace('"', '\\"')
        lines.append(f'      "{mfg}",  // manufacturer')

        # Mode name
        mode = _truncate_to_bytes(p.mode_name, MAX_NAME_LENGTH - 1).replace('"', '\\"')
        lines.append(f'      "{mode}",  // modeName')

        # Fixture type
        lines.append(f"      {p.fixture_type},")

        # Common channels
        lines.append(f"      {p.panChannel}, {p.panFineChannel},  // pan channels")
        lines.append(f"      {p.tiltChannel}, {p.tiltFineChannel},  // tilt channels")
        lines.append(f"      {p.speedChannel}, {p.dimmerChannel}, {p.dimmerFineChannel},  // speed, dimmer")
        lines.append(f"      {p.resetChannel},  // reset channel")

        # Attributes union
        lines.append("      {")
        if p.fixture_type == 'FIXTURE_TYPE_HID':
            lines.extend(self._format_hid_attributes(p))
        elif p.fixture_type == 'FIXTURE_TYPE_LED_WHEEL':
            lines.extend(self._format_wheel_attributes(p))
        elif p.fixture_type == 'FIXTURE_TYPE_RGBX':
            lines.extend(self._format_rgb_attributes(p))
        lines.append("      }")

        lines.append(f"    }}{comma}")
        return lines

    def _format_hid_attributes(self, p: VectrProfile) -> list:
        """Format HidAttributes struct fields."""
        lines = []
        lines.append("        .hid = {")
        lines.append(f"          {p.lampControlChannel}, {p.lampOnValue}, {p.lampOffValue},")
        lines.append(f"          {p.colorChannel}, {p.goboChannel}, {p.prismChannel}, {p.shutterChannel},")
        lines.append(f"          {p.focusChannel}, {p.prismRotationChannel},")
        lines.append(f"          {p.prism2Ch}, {p.prism2RotationCh},")
        lines.append(f"          {p.frostCh},")
        # v1.2 new fields
        lines.append(f"          {p.zoomChannel}, {p.irisChannel},")
        lines.append(f"          {p.gobo2Channel}, {p.gobo2RotationCh}, {p.goboRotationChannel},")
        lines.append(f"          {p.color2Channel}, {p.animationChannel},")
        lines.append(f"          {p.cyanCh}, {p.magentaCh}, {p.yellowCh},")
        lines.append(f"          {p.ctoCh}, {p.effectsCh},")
        lines.append(f"          {p.colorCount}, {p.goboCount},")
        lines.append(f"          {self._c_int_array(p.colorValues)},")
        lines.append(f"          {self._c_string_array(p.colorNames)},")
        lines.append(f"          {self._c_int_array(p.goboValues)},")
        lines.append(f"          {self._c_string_array(p.goboNames)},")
        lines.append(f"          {p.gobo2Count},")
        lines.append(f"          {self._c_int_array(p.gobo2Values)},")
        lines.append(f"          {self._c_string_array(p.gobo2Names)},")
        lines.append(f"          {p.color2Count},")
        lines.append(f"          {self._c_int_array(p.color2Values)},")
        lines.append(f"          {self._c_string_array(p.color2Names)},")
        lines.append(f"          {p.shutter_open_value},")
        lines.append(f"          {p.strobe_start_value},")
        lines.append(f"          {p.dimmer_default_value}")
        lines.append("        }")
        return lines

    def _format_wheel_attributes(self, p: VectrProfile) -> list:
        """Format WheelAttributes struct fields."""
        lines = []
        lines.append("        .wheel = {")
        lines.append(f"          {p.colorChannel}, {p.goboChannel}, {p.prismChannel}, {p.shutterChannel},")
        lines.append(f"          {p.focusChannel}, {p.prismRotationChannel},")
        lines.append(f"          {p.prism2Ch}, {p.prism2RotationCh},")
        lines.append(f"          {p.frostCh},")
        # v1.2 new fields
        lines.append(f"          {p.zoomChannel}, {p.irisChannel},")
        lines.append(f"          {p.gobo2Channel}, {p.gobo2RotationCh}, {p.goboRotationChannel},")
        lines.append(f"          {p.color2Channel}, {p.animationChannel},")
        lines.append(f"          {p.cyanCh}, {p.magentaCh}, {p.yellowCh},")
        lines.append(f"          {p.ctoCh}, {p.effectsCh},")
        lines.append(f"          {p.colorCount}, {p.goboCount},")
        lines.append(f"          {self._c_int_array(p.colorValues)},")
        lines.append(f"          {self._c_string_array(p.colorNames)},")
        lines.append(f"          {self._c_int_array(p.goboValues)},")
        lines.append(f"          {self._c_string_array(p.goboNames)},")
        lines.append(f"          {p.gobo2Count},")
        lines.append(f"          {self._c_int_array(p.gobo2Values)},")
        lines.append(f"          {self._c_string_array(p.gobo2Names)},")
        lines.append(f"          {p.color2Count},")
        lines.append(f"          {self._c_int_array(p.color2Values)},")
        lines.append(f"          {self._c_string_array(p.color2Names)},")
        lines.append(f"          {p.shutter_open_value},")
        lines.append(f"          {p.strobe_start_value},")
        lines.append(f"          {p.dimmer_default_value}")
        lines.append("        }")
        return lines

    def _format_rgb_attributes(self, p: VectrProfile) -> list:
        """Format RgbAttributes struct fields."""
        lines = []
        lines.append("        .rgb = {")
        lines.append(f"          {p.redChannel}, {p.greenChannel}, {p.blueChannel},")
        lines.append(f"          {p.whiteChannel}, {p.amberChannel}, {p.uvChannel},")
        lines.append(f"          {p.shutterChannel},")
        # v1.2 CMY channels (stored in RgbAttributes for CMY fixtures)
        lines.append(f"          {p.cyanCh}, {p.magentaCh}, {p.yellowCh},")
        lines.append(f"          {p.ctoCh},")
        lines.append(f"          {p.shutter_open_value},")
        lines.append(f"          {p.strobe_start_value},")
        lines.append(f"          {p.dimmer_default_value}")
        lines.append("        }")
        return lines

    def _c_int_array(self, values: list) -> str:
        """Format as C int array, padded to MAX_WHEEL_POSITIONS."""
        padded = (values + [0] * MAX_WHEEL_POSITIONS)[:MAX_WHEEL_POSITIONS]
        return "{ " + ", ".join(str(v) for v in padded) + " }"

    def _c_string_array(self, names: list) -> str:
        """Format as C string array, padded to MAX_WHEEL_POSITIONS."""
        padded = []
        for i in range(MAX_WHEEL_POSITIONS):
            if i < len(names) and names[i]:
                s = names[i].replace('"', '\\"')
                s = _truncate_to_bytes(s, MAX_NAME_LENGTH - 1)
                padded.append(f'"{s}"')
            else:
                padded.append('""')
        return "{ " + ", ".join(padded) + " }"


# =============================================================================
# JSON Writer (rehmlights-profiles catalog output)
# =============================================================================

import re

class JsonWriter:
    """Emits per-fixture JSON in the cooked vibe_profile_t shape that
    components/vibe_repo on the device consumes directly. The Python
    dataclass mirrors the legacy FixtureProfile, so this writer essentially
    re-implements vibe_profile_from_legacy() in Python — channel fields
    become an array of {role, offset, default}, wheel arrays go flat.

    File layout: sources/<manufacturer-slug>/<model-mode-slug>.json
    """

    SCHEMA_VERSION = "vibe-profile/1"

    # Legacy dataclass field -> cooked role enum string. Order shapes the
    # channels[] array order on disk; keep it close to physical channel order
    # for readability (movement, intensity, color, beam, mechanical).
    FIELD_TO_ROLE = [
        ("panChannel",          "PAN"),
        ("panFineChannel",      "PAN_FINE"),
        ("tiltChannel",         "TILT"),
        ("tiltFineChannel",     "TILT_FINE"),
        ("speedChannel",        "PAN_TILT_SPEED"),
        ("dimmerChannel",       "DIMMER"),
        ("dimmerFineChannel",   "DIMMER_FINE"),
        ("shutterChannel",      "SHUTTER"),
        # Subtractive + additive color mix
        ("redChannel",          "RED"),
        ("greenChannel",        "GREEN"),
        ("blueChannel",         "BLUE"),
        ("whiteChannel",        "WHITE"),
        ("amberChannel",        "AMBER"),
        ("uvChannel",           "UV"),
        ("cyanCh",              "CYAN"),
        ("magentaCh",           "MAGENTA"),
        ("yellowCh",            "YELLOW"),
        ("ctoCh",               "CTO"),
        ("ctbCh",               "CTB"),
        # Wheels
        ("colorChannel",        "COLOR_WHEEL"),
        ("color2Channel",       "COLOR_WHEEL_2"),
        ("goboChannel",         "GOBO_WHEEL"),
        ("gobo2Channel",        "GOBO_WHEEL_2"),
        ("goboRotationChannel", "GOBO_ROT"),
        ("gobo2RotationCh",     "GOBO_ROT_2"),
        # Beam
        ("focusChannel",        "FOCUS"),
        ("frostCh",             "FROST"),
        ("zoomChannel",         "ZOOM"),
        ("irisChannel",         "IRIS"),
        ("prismChannel",        "PRISM"),
        ("prismRotationChannel","PRISM_ROT"),
        ("prism2Ch",            "PRISM_2"),
        ("prism2RotationCh",    "PRISM_ROT_2"),
        ("animationChannel",    "ANIMATION"),
        ("effectsCh",           "EFFECTS"),
        # Control
        ("lampControlChannel",  "LAMP"),
        ("resetChannel",        "RESET"),
    ]

    @staticmethod
    def slug(s):
        """Filesystem-safe lowercase slug. e.g. 'YPS 150W' -> 'yps-150w'."""
        s = s.strip().lower()
        s = re.sub(r"[^\w\s-]", "", s)        # strip punctuation
        s = re.sub(r"[\s_]+", "-", s).strip("-")
        return s or "unknown"

    def profile_id(self, p):
        """Stable, repo-wide-unique id: <mfg>-<name>-<mode>."""
        return f"{self.slug(p.manufacturer)}/{self.slug(p.name)}-{self.slug(p.mode_name)}"

    def write(self, profiles, output_dir):
        """Write each profile to sources/<mfg>/<model-mode>.json.

        Returns list of (id, relative_path, written_dict) for index building.
        """
        written = []
        for p in profiles:
            pid = self.profile_id(p)
            # split id into "<mfg>/<rest>" for directory layout
            mfg_slug, rest_slug = pid.split("/", 1)
            out_dir = os.path.join(output_dir, mfg_slug)
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"{rest_slug}.json")

            obj = self._profile_to_dict(p, pid)
            with open(out_path, "w") as f:
                json.dump(obj, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(f"Wrote {out_path}")
            written.append((pid, f"sources/{mfg_slug}/{rest_slug}.json", obj))
        return written

    def _profile_to_dict(self, p, pid):
        """Convert a VectrProfile dataclass into the cooked catalog JSON.

        Channels: array of {role, offset, default} entries, one per non-zero
        legacy channel field. Wheels: top-level arrays of {value, name} slots.
        Values: shutter / strobe / dimmer / lamp constants.
        """
        # Build channels[] — only fields with a non-zero offset land here.
        channels = []
        for field, role in self.FIELD_TO_ROLE:
            off = getattr(p, field, 0) or 0
            if not off:
                continue
            entry = {"role": role, "offset": off}
            # Per-role default values where it matters. Engine treats
            # absent default as 0.
            if role in ("PAN", "TILT"):
                entry["default"] = 127            # centre on power-up
            elif role == "DIMMER":
                # Use the dimmer_default_value if the GDTF gave us one;
                # most LED fixtures want a non-zero static dimmer or you
                # see nothing on first power-up. Falls back to 0 (off).
                entry["default"] = p.dimmer_default_value or 0
            elif role == "SHUTTER":
                entry["default"] = p.shutter_open_value or 0
            channels.append(entry)

        # Compute footprint — highest DMX offset referenced by any channel.
        footprint = max((c["offset"] for c in channels), default=0)

        out = {
            "schema":       self.SCHEMA_VERSION,
            "id":           pid,
            "name":         p.name,
            "manufacturer": p.manufacturer,
            "mode":         p.mode_name,
            "fixture_type": p.fixture_type,
            "footprint":    footprint,
            "channels":     channels,
        }

        # Wheel slots as top-level arrays (matches vibe_repo_fetch_profile()).
        if p.colorCount:
            out["color_wheel"]   = self._slots(p.colorNames, p.colorValues, p.colorCount)
        if p.color2Count:
            out["color_wheel_2"] = self._slots(p.color2Names, p.color2Values, p.color2Count)
        if p.goboCount:
            out["gobo_wheel"]    = self._slots(p.goboNames, p.goboValues, p.goboCount)
        if p.gobo2Count:
            out["gobo_wheel_2"]  = self._slots(p.gobo2Names, p.gobo2Values, p.gobo2Count)

        # Shared values map. Only emit what the GDTF actually provided —
        # device fills in sane defaults for missing keys.
        vals = {}
        if p.shutter_open_value:    vals["shutter_open"]  = p.shutter_open_value
        if p.shutter_closed_value:  vals["shutter_close"] = p.shutter_closed_value
        if p.strobe_start_value:    vals["strobe_slow"]   = p.strobe_start_value
        if p.strobe_end_value:      vals["strobe_fast"]   = p.strobe_end_value
        if p.lampOnValue:           vals["lamp_on"]       = p.lampOnValue
        if p.lampOffValue:          vals["lamp_off"]      = p.lampOffValue
        # dimmer_max defaults to 255 on device; only emit if the GDTF disagreed.
        if p.dimmer_default_value and p.dimmer_default_value != 255:
            vals["dimmer_max"] = p.dimmer_default_value
        if vals:
            out["values"] = vals

        # Physical ranges — informational only (not consumed by engine).
        meta = {}
        if p.pan_degrees:   meta["pan_degrees"]  = round(p.pan_degrees, 1)
        if p.tilt_degrees:  meta["tilt_degrees"] = round(p.tilt_degrees, 1)
        if meta:
            out["meta"] = meta

        return out

    @staticmethod
    def _slots(names, values, count):
        """Zip name+value into a list of slot dicts. Skips empty-name slots
        which the device-side parser would reject anyway."""
        slots = []
        for i in range(count):
            name = names[i] if i < len(names) else ""
            if not name:
                continue
            slots.append({"value": values[i] if i < len(values) else 0,
                          "name":  name})
        return slots


# =============================================================================
# CSV Profile Loader (for --append-csv and validation)
# =============================================================================

# Manufacturer lookup for CSV profiles (no manufacturer column in CSV)
CSV_MANUFACTURER_MAP = {
    'Dominar Beam IP': 'Dominar',
    'Dominar Lempa': 'Dominar',
    'Dominar X': 'Dominar',
    'PhoenixRay': 'Mattos Designs',
    'Phoenix Ray': 'Mattos Designs',
    'YPS NOVA': 'Your Pixel Store',
    'YPS Starlight': 'Your Pixel Store',
    'Magicolour': 'Magical Light Shows',
    'Mini Mover': 'Your Pixel Store',
    'Nano Beam': 'Your Pixel Store',
    '350W 17R': 'Generic',
    'Beam 230': 'Generic',
    'Beam 280': 'Generic',
}


def _guess_csv_manufacturer(name: str) -> str:
    """Guess manufacturer from CSV profile name using prefix matching."""
    for prefix, mfg in CSV_MANUFACTURER_MAP.items():
        if name.startswith(prefix):
            return mfg
    return 'Generic'


def load_csv_profiles(csv_path: str) -> list:
    """Load profiles from the existing profiles.csv format."""
    profiles = []

    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get('Name') or not row.get('Type'):
                continue

            p = VectrProfile()
            p.name = row['Name'][:MAX_NAME_LENGTH - 1]
            p.manufacturer = _guess_csv_manufacturer(p.name)

            # Map CSV type to enum
            csv_type = row.get('Type', '')
            if csv_type == 'HID':
                p.fixture_type = 'FIXTURE_TYPE_HID'
            elif csv_type == 'LED Color Wheel':
                p.fixture_type = 'FIXTURE_TYPE_LED_WHEEL'
            elif csv_type == 'RGBx':
                p.fixture_type = 'FIXTURE_TYPE_RGBX'

            # Common channels
            p.panChannel = _safe_int(row.get('PanCh'))
            p.panFineChannel = _safe_int(row.get('PanFineCh'))
            p.tiltChannel = _safe_int(row.get('TiltCh'))
            p.tiltFineChannel = _safe_int(row.get('TiltFineCh'))
            p.speedChannel = _safe_int(row.get('SpeedCh'))
            p.dimmerChannel = _safe_int(row.get('DimmerCh'))
            p.dimmerFineChannel = _safe_int(row.get('DimmerFineCh'))
            p.resetChannel = _safe_int(row.get('ResetCh'))

            # HID lamp
            p.lampControlChannel = _safe_int(row.get('LampControlCh'))
            p.lampOnValue = _safe_int(row.get('LampOnValue'))
            p.lampOffValue = _safe_int(row.get('LampOffValue'))

            # Shared channels
            p.shutterChannel = _safe_int(row.get('ShutterCh'))
            p.colorChannel = _safe_int(row.get('ColorWheelCh'))
            p.goboChannel = _safe_int(row.get('GoboWheelCh'))
            p.prismChannel = _safe_int(row.get('PrismCh'))
            p.prismRotationChannel = _safe_int(row.get('PrismRotationCh'))
            p.prism2Ch = _safe_int(row.get('Prism2Ch'))
            p.prism2RotationCh = _safe_int(row.get('Prism2RotationCh'))
            p.focusChannel = _safe_int(row.get('FocusCh'))
            p.frostCh = _safe_int(row.get('FrostCh'))

            # RGB
            p.redChannel = _safe_int(row.get('RedCh'))
            p.greenChannel = _safe_int(row.get('GreenCh'))
            p.blueChannel = _safe_int(row.get('BlueCh'))
            p.whiteChannel = _safe_int(row.get('WhiteCh'))
            p.amberChannel = _safe_int(row.get('AmberCh'))
            p.uvChannel = _safe_int(row.get('UVCh'))

            # Wheel data
            p.colorValues = _parse_semicolon_ints(row.get('ColorValues', ''))
            p.colorNames = _parse_semicolon_strs(row.get('ColorNames', ''))
            p.colorCount = len(p.colorValues)
            p.goboValues = _parse_semicolon_ints(row.get('GoboValues', ''))
            p.goboNames = _parse_semicolon_strs(row.get('GoboNames', ''))
            p.goboCount = len(p.goboValues)

            # Advanced defaults for CSV profiles
            p.shutter_open_value = 255
            p.shutter_closed_value = 0
            p.strobe_start_value = 100
            p.dimmer_default_value = 255

            profiles.append(p)

    return profiles


def _safe_int(val) -> int:
    if not val or not str(val).strip():
        return 0
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return 0


def _parse_semicolon_ints(s: str) -> list:
    if not s or not s.strip():
        return []
    return [int(x.strip()) for x in s.split(';') if x.strip()]


def _parse_semicolon_strs(s: str) -> list:
    if not s or not s.strip():
        return []
    return [x.strip()[:MAX_NAME_LENGTH - 1] for x in s.split(';') if x.strip()]


# =============================================================================
# GDTF Share API Client
# =============================================================================

class GdtfShareClient:
    """Client for the GDTF Share public API."""

    BASE_URL = 'https://gdtf-share.com/apis/public/'
    CACHE_DIR = os.path.join(os.path.dirname(__file__), 'gdtf_cache')

    def __init__(self):
        self.session = None
        self.authenticated = False

    def _ensure_requests(self):
        """Import requests lazily so the script works without it for local files."""
        try:
            import requests
            self.session = requests.Session()
        except ImportError:
            print("Error: 'requests' package required for GDTF Share API.")
            print("Install with: pip install requests")
            sys.exit(1)

    def login(self, username: str, password: str) -> bool:
        """Authenticate with GDTF Share."""
        self._ensure_requests()
        resp = self.session.post(
            self.BASE_URL + 'login.php',
            json={'user': username, 'password': password}
        )
        data = resp.json()
        self.authenticated = data.get('result', False)
        if not self.authenticated:
            print(f"Login failed: {data.get('error', 'Unknown error')}")
        return self.authenticated

    def get_fixture_list(self) -> list:
        """Get list of all fixtures on GDTF Share."""
        if not self.authenticated:
            print("Error: Not authenticated. Call login() first.")
            return []
        resp = self.session.get(self.BASE_URL + 'getList.php')
        data = resp.json()
        if data.get('result'):
            return data.get('list', [])
        return []

    def download_fixture(self, rid: int, force: bool = False) -> str:
        """Download a .gdtf file by revision ID. Returns local cache path."""
        os.makedirs(self.CACHE_DIR, exist_ok=True)
        local_path = os.path.join(self.CACHE_DIR, f'{rid}.gdtf')

        if os.path.exists(local_path) and not force:
            print(f"Using cached: {local_path}")
            return local_path

        if not self.authenticated:
            print("Error: Not authenticated. Call login() first.")
            return ''

        resp = self.session.get(
            self.BASE_URL + 'downloadFile.php',
            params={'rid': rid}
        )
        resp.raise_for_status()

        with open(local_path, 'wb') as f:
            f.write(resp.content)

        print(f"Downloaded: {local_path} ({len(resp.content)} bytes)")
        return local_path

    @staticmethod
    def get_credentials() -> tuple:
        """Get GDTF Share credentials from environment or .env file."""
        username = os.environ.get('GDTF_USER', '')
        password = os.environ.get('GDTF_PASSWORD', '')

        if not username:
            env_path = os.path.join(os.path.dirname(__file__), '.env')
            if os.path.exists(env_path):
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('GDTF_USER='):
                            username = line.split('=', 1)[1].strip()
                        elif line.startswith('GDTF_PASSWORD='):
                            password = line.split('=', 1)[1].strip()

        return username, password


# =============================================================================
# Inspect Command
# =============================================================================

def do_inspect(args):
    """Human-readable dump of a .gdtf file's parsed profile data."""
    parser = GdtfParser()
    profiles = parser.parse(args.file)

    if not profiles:
        print("No DMX modes found in file.")
        return

    # Print fixture info from first profile
    p0 = profiles[0]
    print(f"\n=== {p0.name} ===")
    print(f"Manufacturer: {p0.manufacturer}")
    print(f"Modes: {len(profiles)}")

    for p in profiles:
        print(f"\n--- Mode: \"{p.mode_name}\" ---")
        print(f"Type: {p.fixture_type} (auto-detected)")
        print()

        # Channel table
        print("Channels:")
        _print_ch("Pan",          p.panChannel, p.panFineChannel,
                  f"[{p.pan_degrees:.0f} degrees]" if p.pan_degrees else "")
        _print_ch("Tilt",         p.tiltChannel, p.tiltFineChannel,
                  f"[{p.tilt_degrees:.0f} degrees]" if p.tilt_degrees else "")
        _print_ch("Speed",        p.speedChannel)
        _print_ch("Dimmer",       p.dimmerChannel, p.dimmerFineChannel)
        _print_ch("Shutter",      p.shutterChannel, extra=_shutter_info(p))
        _print_ch("Color",        p.colorChannel, extra=f"[{p.colorCount} slots]" if p.colorCount else "")
        _print_ch("Gobo",         p.goboChannel, extra=f"[{p.goboCount} slots]" if p.goboCount else "")
        _print_ch("Prism",        p.prismChannel)
        _print_ch("Prism Rot",    p.prismRotationChannel)
        _print_ch("Prism 2",      p.prism2Ch)
        _print_ch("Prism 2 Rot",  p.prism2RotationCh)
        _print_ch("Focus",        p.focusChannel)
        _print_ch("Frost",        p.frostCh)
        _print_ch("Zoom",         p.zoomChannel)
        _print_ch("Iris",         p.irisChannel)
        _print_ch("Gobo 2",       p.gobo2Channel, extra=f"[{p.gobo2Count} slots]" if p.gobo2Count else "")
        _print_ch("Gobo Rot",     p.goboRotationChannel)
        _print_ch("Gobo 2 Rot",   p.gobo2RotationCh)
        _print_ch("Color 2",      p.color2Channel, extra=f"[{p.color2Count} slots]" if p.color2Count else "")
        _print_ch("Animation",    p.animationChannel)
        _print_ch("Cyan",         p.cyanCh)
        _print_ch("Magenta",      p.magentaCh)
        _print_ch("Yellow",       p.yellowCh)
        _print_ch("CTO",          p.ctoCh)
        _print_ch("CTB",          p.ctbCh)
        _print_ch("Effects",      p.effectsCh)
        _print_ch("Reset",        p.resetChannel)

        # Lamp control
        if p.lampControlChannel:
            _print_ch("Lamp",     p.lampControlChannel,
                      extra=f"[on={p.lampOnValue}, off={p.lampOffValue}]")

        # RGB channels
        if any([p.redChannel, p.greenChannel, p.blueChannel]):
            print()
            _print_ch("Red",     p.redChannel)
            _print_ch("Green",   p.greenChannel)
            _print_ch("Blue",    p.blueChannel)
            _print_ch("White",   p.whiteChannel)
            _print_ch("Amber",   p.amberChannel)
            _print_ch("UV",      p.uvChannel)

        # Wheel data
        if p.colorCount:
            print(f"\nColor Wheel ({p.colorCount} slots):")
            for i in range(p.colorCount):
                name = p.colorNames[i] if i < len(p.colorNames) else ""
                val = p.colorValues[i] if i < len(p.colorValues) else 0
                print(f"  {i}: {val:3d} = {name}")

        if p.goboCount:
            print(f"\nGobo Wheel ({p.goboCount} slots):")
            for i in range(p.goboCount):
                name = p.goboNames[i] if i < len(p.goboNames) else ""
                val = p.goboValues[i] if i < len(p.goboValues) else 0
                print(f"  {i}: {val:3d} = {name}")

        if p.color2Count:
            print(f"\nColor Wheel 2 ({p.color2Count} slots):")
            for i in range(p.color2Count):
                name = p.color2Names[i] if i < len(p.color2Names) else ""
                val = p.color2Values[i] if i < len(p.color2Values) else 0
                print(f"  {i}: {val:3d} = {name}")

        if p.gobo2Count:
            print(f"\nGobo Wheel 2 ({p.gobo2Count} slots):")
            for i in range(p.gobo2Count):
                name = p.gobo2Names[i] if i < len(p.gobo2Names) else ""
                val = p.gobo2Values[i] if i < len(p.gobo2Values) else 0
                print(f"  {i}: {val:3d} = {name}")

        # Unrecognized attributes
        if p.unrecognized:
            print(f"\nUnrecognized attributes:")
            for attr, ch in p.unrecognized:
                print(f"  {attr} → ch {ch}")


def _print_ch(label: str, coarse: int, fine: int = 0, extra: str = ""):
    """Print a channel line if the channel is assigned."""
    if coarse == 0:
        return
    fine_str = f" (fine: {fine})" if fine else ""
    extra_str = f"   {extra}" if extra else ""
    print(f"  {label:14s} ch {coarse:3d}{fine_str}{extra_str}")


def _shutter_info(p: VectrProfile) -> str:
    """Format shutter open/strobe info string."""
    parts = []
    if p.shutter_open_value:
        parts.append(f"open: {p.shutter_open_value}")
    if p.strobe_start_value:
        parts.append(f"strobe: {p.strobe_start_value}-{p.strobe_end_value}")
    return f"[{', '.join(parts)}]" if parts else ""


# =============================================================================
# Cook Command
# =============================================================================

def _get_max_channel(profile):
    """Return the highest non-zero channel number in a profile (proxy for channel count)."""
    max_ch = 0
    for field_name in vars(profile):
        if field_name.endswith('Channel') or field_name.endswith('Ch'):
            val = getattr(profile, field_name, 0)
            if isinstance(val, int) and val > max_ch:
                max_ch = val
    return max_ch


def _filter_modes(profiles_by_source, max_modes):
    """Filter modes per fixture. Keeps the smallest mode, and if max_modes>=2, also the largest.

    profiles_by_source: list of (source_path, [profiles]) tuples
    Returns flat list of selected profiles.
    """
    selected = []
    for source, profiles in profiles_by_source:
        if max_modes <= 0 or len(profiles) <= max_modes:
            selected.extend(profiles)
            continue

        # Sort by channel count
        scored = [(p, _get_max_channel(p)) for p in profiles]
        scored.sort(key=lambda x: x[1])

        # Always pick smallest (most basic mode)
        picks = [scored[0][0]]
        if max_modes >= 2 and len(scored) >= 2:
            # Pick largest (most extended mode) if different from smallest
            if scored[-1][1] != scored[0][1]:
                picks.append(scored[-1][0])
            else:
                # All modes same channel count — just pick first two
                picks.append(scored[1][0])

        kept_names = [p.name for p in picks]
        skipped = len(profiles) - len(picks)
        if skipped > 0:
            print(f"  Filtered {source}: kept {len(picks)}/{len(profiles)} modes ({', '.join(kept_names)})")
        selected.extend(picks)

    return selected


def do_cook(args):
    """Cook .gdtf files into VECTR factory_profiles.h."""
    parser = GdtfParser()
    profiles_by_source = []

    # Parse GDTF files
    for filepath in args.files:
        if not os.path.exists(filepath):
            print(f"Warning: File not found: {filepath}")
            continue
        try:
            profiles = parser.parse(filepath)
            profiles_by_source.append((os.path.basename(filepath), profiles))
            print(f"Parsed {filepath}: {len(profiles)} mode(s)")
        except Exception as e:
            print(f"Error parsing {filepath}: {e}")

    # Apply mode filter
    if args.max_modes > 0:
        total_before = sum(len(p) for _, p in profiles_by_source)
        all_profiles = _filter_modes(profiles_by_source, args.max_modes)
        print(f"\nMode filter: {total_before} → {len(all_profiles)} profiles (max {args.max_modes} per fixture)")
    else:
        all_profiles = [p for _, profiles in profiles_by_source for p in profiles]

    # Optionally merge CSV profiles
    if args.append_csv:
        csv_path = args.append_csv
        if os.path.exists(csv_path):
            csv_profiles = load_csv_profiles(csv_path)
            all_profiles.extend(csv_profiles)
            print(f"Loaded {csv_path}: {len(csv_profiles)} profiles")
        else:
            print(f"Warning: CSV not found: {csv_path}")

    if not all_profiles:
        print("No profiles to cook.")
        return

    # Sort by name
    all_profiles.sort(key=lambda p: p.name.lower())

    # Write header
    output_path = os.path.join(args.output_dir, 'include', 'factory_profiles.h')
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    writer = CHeaderWriter()
    writer.write(all_profiles, output_path)


# =============================================================================
# Cook-JSON Command (rehmlights-profiles catalog)
# =============================================================================

def do_cook_json(args):
    """Cook .gdtf files into per-fixture JSON files for the online catalog."""
    parser = GdtfParser()
    profiles_by_source = []

    for filepath in args.files:
        if not os.path.exists(filepath):
            print(f"Warning: File not found: {filepath}")
            continue
        try:
            profiles = parser.parse(filepath)
            profiles_by_source.append((os.path.basename(filepath), profiles))
            print(f"Parsed {filepath}: {len(profiles)} mode(s)")
        except Exception as e:
            print(f"Error parsing {filepath}: {e}")

    if args.max_modes > 0:
        before = sum(len(p) for _, p in profiles_by_source)
        all_profiles = _filter_modes(profiles_by_source, args.max_modes)
        print(f"Mode filter: {before} -> {len(all_profiles)} profiles")
    else:
        all_profiles = [p for _, profiles in profiles_by_source for p in profiles]

    if not all_profiles:
        print("No profiles to write.")
        return

    writer = JsonWriter()
    written = writer.write(all_profiles, args.output_dir)
    print(f"\nWrote {len(written)} profile(s) under {args.output_dir}/")
    print("Run `python tools/build_index.py` to refresh index.json.")


# =============================================================================
# Download Command
# =============================================================================

def do_download(args):
    """Download fixtures from GDTF Share."""
    client = GdtfShareClient()
    username, password = GdtfShareClient.get_credentials()

    if not username or not password:
        print("GDTF Share credentials not found.")
        print("Set GDTF_USER and GDTF_PASSWORD environment variables,")
        print("or create tools/.env with those values.")
        return

    if not client.login(username, password):
        return

    if args.list or args.search:
        fixtures = client.get_fixture_list()
        if args.search:
            search = args.search.lower()
            fixtures = [f for f in fixtures
                       if search in f.get('fixture', f.get('name', '')).lower()
                       or search in f.get('manufacturer', '').lower()]

        print(f"\n{'RID':>8}  {'Manufacturer':<30} {'Name':<40}")
        print("-" * 80)
        for f in fixtures:
            fname = f.get('fixture', f.get('name', ''))
            print(f"{f.get('rid', ''):>8}  {f.get('manufacturer', ''):30} {fname:40}")
        print(f"\nTotal: {len(fixtures)} fixtures")

    if args.rid:
        for rid in args.rid:
            client.download_fixture(rid, force=args.force)


# =============================================================================
# Validate Command
# =============================================================================

def do_validate(args):
    """Validate parsed GDTF output against expected values from profiles.csv."""
    parser = GdtfParser()
    profiles = parser.parse(args.file)

    if not profiles:
        print("No profiles parsed from GDTF file.")
        return

    if not args.expected_csv:
        # Just print what was parsed
        print("No expected CSV provided. Showing parsed values:")
        for p in profiles:
            print(f"\n  {p.name} ({p.fixture_type})")
            print(f"  Pan={p.panChannel},{p.panFineChannel} Tilt={p.tiltChannel},{p.tiltFineChannel}")
            print(f"  Speed={p.speedChannel} Dimmer={p.dimmerChannel} Shutter={p.shutterChannel}")
            print(f"  Color={p.colorChannel}({p.colorCount}) Gobo={p.goboChannel}({p.goboCount})")
            print(f"  Prism={p.prismChannel} Focus={p.focusChannel} Reset={p.resetChannel}")
        return

    csv_profiles = load_csv_profiles(args.expected_csv)
    parsed = profiles[0]  # Compare first mode

    # Find matching CSV profile by name similarity
    best_match = None
    best_score = 0
    for csv_p in csv_profiles:
        # Simple substring match
        name_lower = parsed.name.lower()
        csv_lower = csv_p.name.lower()
        if csv_lower in name_lower or name_lower in csv_lower:
            score = len(csv_lower)
            if score > best_score:
                best_score = score
                best_match = csv_p

    if not best_match:
        print(f"No matching profile found in CSV for '{parsed.name}'")
        print("Available CSV profiles:")
        for p in csv_profiles:
            print(f"  - {p.name}")
        return

    print(f"\nValidating: {parsed.name}")
    print(f"Against:    {best_match.name}")
    print()

    errors = 0

    def check(field_name, parsed_val, expected_val, label):
        nonlocal errors
        status = "PASS" if parsed_val == expected_val else "FAIL"
        if status == "FAIL":
            errors += 1
            print(f"  {status}  {label}: got {parsed_val}, expected {expected_val}")
        else:
            print(f"  {status}  {label}: {parsed_val}")

    check('type', parsed.fixture_type, best_match.fixture_type, 'Fixture type')
    check('pan', parsed.panChannel, best_match.panChannel, 'Pan channel')
    check('panFine', parsed.panFineChannel, best_match.panFineChannel, 'Pan fine')
    check('tilt', parsed.tiltChannel, best_match.tiltChannel, 'Tilt channel')
    check('tiltFine', parsed.tiltFineChannel, best_match.tiltFineChannel, 'Tilt fine')
    check('speed', parsed.speedChannel, best_match.speedChannel, 'Speed channel')
    check('dimmer', parsed.dimmerChannel, best_match.dimmerChannel, 'Dimmer channel')
    check('shutter', parsed.shutterChannel, best_match.shutterChannel, 'Shutter channel')
    check('color', parsed.colorChannel, best_match.colorChannel, 'Color channel')
    check('gobo', parsed.goboChannel, best_match.goboChannel, 'Gobo channel')
    check('prism', parsed.prismChannel, best_match.prismChannel, 'Prism channel')
    check('reset', parsed.resetChannel, best_match.resetChannel, 'Reset channel')

    if parsed.fixture_type == 'FIXTURE_TYPE_HID':
        check('lamp', parsed.lampControlChannel, best_match.lampControlChannel, 'Lamp channel')

    # Wheel data
    if best_match.colorCount > 0:
        check('colorCount', parsed.colorCount, best_match.colorCount, 'Color count')
        if parsed.colorValues != best_match.colorValues:
            errors += 1
            print(f"  FAIL  Color values: got {parsed.colorValues}, expected {best_match.colorValues}")
        else:
            print(f"  PASS  Color values match")
        if parsed.colorNames != best_match.colorNames:
            errors += 1
            print(f"  FAIL  Color names: got {parsed.colorNames}, expected {best_match.colorNames}")
        else:
            print(f"  PASS  Color names match")

    if best_match.goboCount > 0:
        check('goboCount', parsed.goboCount, best_match.goboCount, 'Gobo count')
        if parsed.goboValues != best_match.goboValues:
            errors += 1
            print(f"  FAIL  Gobo values: got {parsed.goboValues}, expected {best_match.goboValues}")
        else:
            print(f"  PASS  Gobo values match")
        if parsed.goboNames != best_match.goboNames:
            errors += 1
            print(f"  FAIL  Gobo names: got {parsed.goboNames}, expected {best_match.goboNames}")
        else:
            print(f"  PASS  Gobo names match")

    print()
    if errors == 0:
        print("ALL CHECKS PASSED")
    else:
        print(f"{errors} CHECK(S) FAILED")


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='GDTF Cooker - Convert GDTF fixtures to VECTR profiles',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s inspect fixture.gdtf              Inspect a GDTF file
  %(prog)s cook fixture.gdtf                 Generate factory_profiles.h
  %(prog)s cook *.gdtf --append-csv profiles.csv  Merge GDTF + CSV profiles
  %(prog)s download --list                   List all GDTF Share fixtures
  %(prog)s download --search "YPS"           Search fixtures by name
  %(prog)s download --rid 12345              Download by revision ID
  %(prog)s validate fixture.gdtf --expected-csv profiles.csv
"""
    )
    subparsers = parser.add_subparsers(dest='command')

    # inspect
    inspect_p = subparsers.add_parser('inspect', help='Inspect a .gdtf file')
    inspect_p.add_argument('file', help='Path to .gdtf file')

    # cook (legacy C header output)
    cook_p = subparsers.add_parser('cook', help='Cook .gdtf files into factory_profiles.h')
    cook_p.add_argument('files', nargs='*', help='Path(s) to .gdtf files')
    cook_p.add_argument('--output-dir', default='.', help='Output directory (default: .)')
    cook_p.add_argument('--append-csv', help='Also include profiles from CSV file')
    cook_p.add_argument('--max-modes', type=int, default=0,
                        help='Max modes per fixture (0=all, 1=smallest, 2=smallest+largest)')

    # cook-json (online catalog output)
    cookj_p = subparsers.add_parser('cook-json', help='Cook .gdtf files into per-fixture JSON for the online catalog')
    cookj_p.add_argument('files', nargs='*', help='Path(s) to .gdtf files')
    cookj_p.add_argument('--output-dir', default='sources', help='Output directory (default: sources)')
    cookj_p.add_argument('--max-modes', type=int, default=0,
                         help='Max modes per fixture (0=all, 1=smallest, 2=smallest+largest)')

    # download
    dl_p = subparsers.add_parser('download', help='Download fixtures from GDTF Share')
    dl_p.add_argument('--list', action='store_true', help='List all available fixtures')
    dl_p.add_argument('--search', help='Search fixtures by name or manufacturer')
    dl_p.add_argument('--rid', type=int, nargs='+', help='Revision ID(s) to download')
    dl_p.add_argument('--force', action='store_true', help='Re-download even if cached')

    # validate
    val_p = subparsers.add_parser('validate', help='Validate against expected values')
    val_p.add_argument('file', help='Path to .gdtf file')
    val_p.add_argument('--expected-csv', help='CSV with expected values for comparison')

    args = parser.parse_args()

    if args.command == 'inspect':
        do_inspect(args)
    elif args.command == 'cook':
        do_cook(args)
    elif args.command == 'cook-json':
        do_cook_json(args)
    elif args.command == 'download':
        do_download(args)
    elif args.command == 'validate':
        do_validate(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
