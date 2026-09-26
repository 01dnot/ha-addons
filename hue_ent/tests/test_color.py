"""sRGB -> Hue Entertainment conversion.

Anchors the corner behaviors that firmware quirks and user-facing knobs
depend on: the min-brightness floor (0 = undefined on some firmware),
the bri_floor knob that lets a user push past a strip's warm-white
fallback, and the brightness_scale multiplier (>1 boosts and clips).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import color


def test_black_pixel_clamps_to_default_floor_of_one():
    # A pure black input still gets bri=1 (protocol minimum) and the D65
    # chromaticity fallback (undefined xy division-by-zero avoided).
    bri, _x, _y = color.rgb8_to_entertainment(0, 0, 0)
    assert bri == 1


def test_bri_floor_overrides_default_for_black():
    # Same black input, but bri_floor=20 => 20 goes on the wire. Useful
    # when the strip firmware drops to warm-white below its PWM threshold
    # and the user wants "at least this bright" as a workaround.
    bri, _x, _y = color.rgb8_to_entertainment(0, 0, 0, bri_floor=20)
    assert bri == 20


def test_bri_floor_does_not_gate_brighter_pixels():
    # A pixel that would compute above the floor keeps its computed value.
    bri, _x, _y = color.rgb8_to_entertainment(255, 255, 255, bri_floor=20)
    assert bri == 2047  # full white, unaffected


def test_bri_floor_cannot_go_below_protocol_minimum():
    # Passing bri_floor=0 must be silently clamped to 1 (0 is undefined
    # on some Hue firmware).
    bri, _x, _y = color.rgb8_to_entertainment(0, 0, 0, bri_floor=0)
    assert bri == 1


def test_brightness_scale_boost_clips_at_max():
    # Mid-gray at 3x boost: linear max ~= 0.217, * 2047 * 3.0 = ~1333 (no clip).
    # White at 3x: 2047 * 3 = 6141, clamps to 2047.
    bri, _x, _y = color.rgb8_to_entertainment(255, 255, 255, brightness_scale=3.0)
    assert bri == 2047
    bri_mid, _, _ = color.rgb8_to_entertainment(128, 128, 128, brightness_scale=3.0)
    assert 1000 < bri_mid < 2047  # boosted but not clipped


def test_white_maps_to_d65_chromaticity():
    _bri, x12, y12 = color.rgb8_to_entertainment(255, 255, 255)
    # D65 = (0.3127, 0.3290); packed into 12-bit widegamut coords.
    # Just sanity-check the round trip is close to D65 not something wild.
    x_norm = x12 / 4095 * color.WIDE_GAMUT_MAX_X
    y_norm = y12 / 4095 * color.WIDE_GAMUT_MAX_Y
    assert abs(x_norm - color.D65[0]) < 0.01
    assert abs(y_norm - color.D65[1]) < 0.01
