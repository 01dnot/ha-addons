"""Gradient-strip detection + segment expansion.

Anchored on a real ``zigbee2mqtt/bridge/devices`` payload for a Hue Play
Gradient Lightstrip 55" (LCX001, definition.model 929002422702, firmware
1.129.5) - the same JSON we harvested from the live device. If Z2M changes
this shape, these tests break loudly rather than silently defaulting to
non-gradient mode.

Run from the repo root: ``python -m pytest hue_ent/tests``
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main as main_mod
from app import registry

# Real LCX001 device object as z2m emits in bridge/devices.
# Trimmed to fields the parser actually reads; the "gradient" expose is kept
# with its exact top-level "type": "list" shape (it is NOT nested inside the
# "light" expose's features - Z2M places it separately).
LCX001_DEVICE = {
    "friendly_name": "hue_gradient_tv",
    "ieee_address": "0x001788010b8e9b95",
    "network_address": 0x2A37,
    "model_id": "LCX001",
    "definition": {
        "model": "929002422702",
        "vendor": "Philips",
        "description": "Hue Play gradient lightstrip 55",
        "exposes": [
            {
                "type": "light",
                "features": [
                    {"name": "state", "type": "binary"},
                    {"name": "brightness", "type": "numeric"},
                    {"name": "color_temp", "type": "numeric"},
                    {"name": "color_xy", "type": "composite"},
                    {"name": "color_hs", "type": "composite"},
                ],
            },
            {
                "name": "gradient",
                "type": "list",
                "length_max": 9,
                "length_min": 1,
                "item_type": {"name": "hex", "type": "text"},
            },
            {"name": "gradient_style", "type": "enum",
             "values": ["linear", "scattered", "mirrored"]},
        ],
    },
}


# A plain white-ambiance bulb (color_temp only, no color_xy) - must be
# recognized as a light but with color=False, so registry drops it from zones.
PHILIPS_WHITE_BULB = {
    "friendly_name": "hue_hall_white",
    "ieee_address": "0x001788010d94aabb",
    "network_address": 0x1111,
    "model_id": "LWB010",
    "definition": {
        "model": "9290011370",
        "vendor": "Philips",
        "exposes": [
            {"type": "light", "features": [
                {"name": "state", "type": "binary"},
                {"name": "brightness", "type": "numeric"},
                {"name": "color_temp", "type": "numeric"},
            ]},
        ],
    },
}


# A plain color bulb - no gradient, segments=1
PHILIPS_COLOR_BULB = {
    "friendly_name": "hue_living_bulb_1",
    "ieee_address": "0x001788010d94ccdd",
    "network_address": 0x2222,
    "model_id": "LCT015",
    "definition": {
        "model": "9290012573A",
        "vendor": "Philips",
        "exposes": [
            {"type": "light", "features": [
                {"name": "state", "type": "binary"},
                {"name": "brightness", "type": "numeric"},
                {"name": "color_temp", "type": "numeric"},
                {"name": "color_xy", "type": "composite"},
                {"name": "color_hs", "type": "composite"},
            ]},
        ],
    },
}


# A non-Philips device (e.g. some IKEA bulb) - must be ignored entirely
NON_PHILIPS_BULB = {
    "friendly_name": "ikea_something",
    "ieee_address": "0x000d6fabcdef0123",
    "network_address": 0x3333,
    "model_id": "LED1836G9",
    "definition": {
        "model": "LED1836G9",
        "vendor": "IKEA",
        "exposes": [{"type": "light", "features": [
            {"name": "state", "type": "binary"},
            {"name": "color_xy", "type": "composite"},
        ]}],
    },
}


def test_lcx001_detected_as_gradient_with_7_segments():
    nwk, lights = main_mod.parse_z2m_devices([LCX001_DEVICE])
    assert nwk == {"hue_gradient_tv": 0x2A37}
    entry = lights["hue_gradient_tv"]
    assert entry["gradient"] is True
    assert entry["color"] is True
    assert entry["segments"] == 7
    assert entry["ieee"] == "0x001788010b8e9b95"
    assert entry["zigbee_model"] == "LCX001"
    assert entry["model"] == "929002422702"


def test_color_bulb_gets_one_segment_and_no_gradient_flag():
    _, lights = main_mod.parse_z2m_devices([PHILIPS_COLOR_BULB])
    entry = lights["hue_living_bulb_1"]
    assert entry["gradient"] is False
    assert entry["color"] is True
    assert entry["segments"] == 1


def test_white_ambiance_bulb_is_a_light_but_not_color():
    _, lights = main_mod.parse_z2m_devices([PHILIPS_WHITE_BULB])
    entry = lights["hue_hall_white"]
    assert entry["color"] is False
    assert entry["segments"] == 1


def test_non_philips_device_is_ignored():
    nwk, lights = main_mod.parse_z2m_devices([NON_PHILIPS_BULB])
    # nwk-map still records everything with a network address (used elsewhere
    # only for looking up Philips lights, so extra entries are harmless)...
    assert "ikea_something" in nwk
    # ...but the lights map is Philips-only.
    assert "ikea_something" not in lights


def test_unknown_gradient_device_falls_back_to_seven_segments():
    unknown = dict(LCX001_DEVICE)
    unknown["model_id"] = "LCX999_FUTURE"
    unknown["definition"] = dict(LCX001_DEVICE["definition"], model="999999999999")
    _, lights = main_mod.parse_z2m_devices([unknown])
    assert lights[unknown["friendly_name"]]["segments"] == main_mod.DEFAULT_GRADIENT_SEGMENTS
    assert lights[unknown["friendly_name"]]["segments"] == 7


def test_lcx005_pc_strip_gets_ten_segments():
    pc = dict(LCX001_DEVICE, friendly_name="hue_pc")
    pc["model_id"] = "LCX005"
    _, lights = main_mod.parse_z2m_devices([pc])
    assert lights["hue_pc"]["segments"] == 10


def test_gradient_lookup_hits_via_article_number_too():
    # Simulate an entry where model_id is missing but definition.model is the
    # Signify article number for LCX001.
    dev = dict(LCX001_DEVICE)
    dev.pop("model_id", None)
    _, lights = main_mod.parse_z2m_devices([dev])
    assert lights[dev["friendly_name"]]["segments"] == 7


# --- registry: 10-record cap now counts segments ---------------------------

def _room_map_with(fn: str, area: str) -> registry.AreaMap:
    m = registry.AreaMap()
    m.by_ieee[LCX001_DEVICE["ieee_address"].lower()] = area
    m.by_ieee["0x001788010d94ccdd"] = area  # color bulb
    return m


def test_room_with_gradient_and_three_bulbs_fits():
    _, lights = main_mod.parse_z2m_devices([LCX001_DEVICE, PHILIPS_COLOR_BULB])
    # Force the two lights into the same area
    lights["hue_gradient_tv"]["ieee"] = "0x001788010b8e9b95"
    m = registry.AreaMap(
        by_ieee={
            "0x001788010b8e9b95": "Living Room",
            "0x001788010d94ccdd": "Living Room",
        }
    )
    rooms = registry.synthesize_rooms(lights, m)
    assert len(rooms) == 1
    assert set(rooms[0]["lights"]) == {"hue_gradient_tv", "hue_living_bulb_1"}


def test_room_overflowing_the_ten_record_cap_skips_the_extras():
    # Gradient strip (7) + 4 color bulbs (4) = 11 records -> last bulb dropped.
    lights = {}
    _, lights = main_mod.parse_z2m_devices([LCX001_DEVICE])
    ieees = {"0x001788010b8e9b95": "Living Room"}
    for i in range(4):
        ieee = f"0x001788010d94aa{i:02d}"
        bulb = dict(PHILIPS_COLOR_BULB,
                    friendly_name=f"bulb_{i}",
                    ieee_address=ieee,
                    network_address=0x4000 + i)
        _, l2 = main_mod.parse_z2m_devices([bulb])
        lights.update(l2)
        ieees[ieee] = "Living Room"
    m = registry.AreaMap(by_ieee=ieees)

    rooms = registry.synthesize_rooms(lights, m)
    assert len(rooms) == 1
    # 7 + 3 = 10 records fit; the 4th bulb overflows and goes to skipped.
    assert len(rooms[0]["lights"]) == 4  # 1 gradient + 3 bulbs
    assert any("zone full" in s for s in rooms[0]["skipped"])


# --- Zone: computes pixel count and virtual segment addressing --------------

def _base_zone_cfg(lights, segments_map):
    return {
        "name": "TV",
        "lights": lights,
        "ddp_port": 4048,
        "light_meta": {fn: {"segments": n} for fn, n in segments_map.items()},
    }


def test_zone_pixel_count_expands_to_segments():
    cfg = _base_zone_cfg(["hue_gradient_tv"], {"hue_gradient_tv": 7})
    zone = main_mod.Zone(cfg)
    assert zone.pixel_count == 7
    assert zone.records_per_frame == 7
    assert zone.segments == {"hue_gradient_tv": 7}
    assert zone.proxy == "hue_gradient_tv"  # sole light defaults to proxy


def test_zone_rejects_when_records_exceed_ten():
    cfg = _base_zone_cfg(
        ["strip", "b1", "b2", "b3", "b4"],
        {"strip": 7, "b1": 1, "b2": 1, "b3": 1, "b4": 1},
    )
    with pytest.raises(ValueError, match="records/frame"):
        main_mod.Zone(cfg)


def test_zone_with_only_bulbs_still_works_backcompat():
    cfg = {
        "name": "Kitchen",
        "lights": ["k1", "k2", "k3"],
        "ddp_port": 4049,
        # No light_meta -> defaults everyone to 1 segment.
    }
    zone = main_mod.Zone(cfg)
    assert zone.pixel_count == 3
    assert zone.segments == {"k1": 1, "k2": 1, "k3": 1}
