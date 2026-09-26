"""Wire-format tests for the Hue Entertainment Zigbee protocol.

Run from the repo root: ``python -m pytest hue_ent/tests``

Test vectors mirror Bifrost's ``hue::zigbee`` unit tests (see
``bifrost/crates/hue/src/zigbee/{entertainment,stream}.rs``) - the same
reverse-engineered format checked against real bulbs. If a byte flips here,
it flips against Bifrost's fasit too and something is genuinely wrong.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import protocol


def _cmd_data(payload: str) -> bytes:
    """Pull the raw payload bytes back out of a zclcommand JSON envelope."""
    envelope = json.loads(payload)["zclcommand"]
    return bytes(envelope["payload"]["data"])


def test_light_record_device_matches_bifrost_vector():
    # Bifrost: HueEntFrameLightRecord::new(0x1122, 0x7FF, Device, [0xAA, 0xBB, 0xCC])
    #         packs to 2211ebffaabbcc
    # xy fields chosen so the packed 3-byte tail equals [0xAA, 0xBB, 0xCC]:
    #   x12 low 8 = 0xAA, x12 high 4 = 0xB -> x12 = 0xBAA
    #   y12 low 4 = 0xB, y12 upper 8 = 0xCC -> y12 = 0xCCB
    rec = protocol.light_record(0x1122, 0x7FF, 0xBAA, 0xCCB, mode=protocol.MODE_DEVICE)
    assert rec.hex() == "2211ebffaabbcc"


def test_light_record_segment_matches_bifrost_vector():
    # Same fields but mode=Segment (0b00000) -> low 5 bits of the packed word go to 0.
    rec = protocol.light_record(0x1122, 0x7FF, 0xBAA, 0xCCB, mode=protocol.MODE_SEGMENT)
    assert rec.hex() == "2211e0ffaabbcc"


def test_light_record_default_mode_is_device_backcompat():
    # Existing callers pass no mode; must still get MODE_DEVICE.
    default = protocol.light_record(0x1122, 0x7FF, 0xBAA, 0xCCB)
    explicit = protocol.light_record(0x1122, 0x7FF, 0xBAA, 0xCCB, mode=protocol.MODE_DEVICE)
    assert default == explicit


def test_segment_map_matches_bifrost_vector():
    # Bifrost: segment_mapping(&[0xA0A1, 0xB0B1]) -> data [0x00, 0x02, 0xA1, 0xA0, 0xB1, 0xB0]
    payload = protocol.segment_map_payload([0xA0A1, 0xB0B1])
    envelope = json.loads(payload)["zclcommand"]
    assert envelope["cluster"] == protocol.CLUSTER
    assert envelope["command"] == protocol.CMD_SEGMENT_MAP
    assert envelope["options"]["manufacturerCode"] == protocol.MANUFACTURER
    assert _cmd_data(payload) == bytes([0x00, 0x02, 0xA1, 0xA0, 0xB1, 0xB0])


def test_segment_map_gradient_strip_7_segments():
    # Bifrost doc example: seven virtual addresses D297..D29D packs as
    #   00 07 97 d2 98 d2 99 d2 9a d2 9b d2 9c d2 9d d2
    payload = protocol.segment_map_payload(list(range(0xD297, 0xD29E)))
    assert _cmd_data(payload).hex() == "000797d298d299d29ad29bd29cd29dd2"


def test_segment_map_rejects_empty():
    import pytest

    with pytest.raises(ValueError):
        protocol.segment_map_payload([])


def test_stream_frame_matches_bifrost_vector():
    # Bifrost hue_ent_frame test: counter=0x11223344, smoothing=0xAABB, one record
    # {addr=0x7788, brightness=0x0123, raw=[0xCC,0xDD,0xEE]}.
    # brightness 0x0123 encodes as (bri>>5, mode) = (9, 3), a combination our
    # public helper doesn't expose, so build the record inline like Bifrost does.
    import struct
    record = struct.pack("<HH", 0x7788, 0x0123) + bytes([0xCC, 0xDD, 0xEE])
    payload = protocol.stream_frame_payload(0x11223344, 0xAABB, [record])
    # 4 counter (LE) + 2 smoothing (LE) + 7-byte record = 13 bytes
    expected = bytes(
        [0x44, 0x33, 0x22, 0x11, 0xBB, 0xAA, 0x88, 0x77, 0x23, 0x01, 0xCC, 0xDD, 0xEE]
    )
    assert _cmd_data(payload) == expected


def test_stream_frame_enforces_10_record_cap():
    import pytest

    dummy = bytes(7)
    # 10 records: OK.
    protocol.stream_frame_payload(0, 0, [dummy] * 10)
    # 11 records: rejected.
    with pytest.raises(ValueError):
        protocol.stream_frame_payload(0, 0, [dummy] * 11)


def test_mode_constants_are_5_bit():
    assert protocol.MODE_DEVICE == 0b01011
    assert protocol.MODE_SEGMENT == 0b00000
    assert protocol.MODE_DEVICE & ~0x1F == 0
    assert protocol.MODE_SEGMENT & ~0x1F == 0
