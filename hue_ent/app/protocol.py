"""Hue Entertainment Zigbee frame construction (reverse-engineered, cluster 0xFC01).

Wire format verified against live bulbs 2026-07-01/02 (see the project plan):
every command is a manufacturer-specific cluster-specific ZCL command published
through zigbee2mqtt's generic ``zclcommand`` passthrough on
``zigbee2mqtt/<friendly_name>/set``.

- arm      = write manufacturer attribute 0x0005 = 0xFE, then SYNC (cmd 3)
- seg_map  = cmd 7 to a gradient device: assign N virtual u16 addresses to its
             segments (Bifrost convention: nwk, nwk+1, ..., nwk+N-1). Ordinary
             bulbs reply "Command Not Supported" - useful as a capability probe.
- stream   = cmd 1 to ONE proxy device; it re-broadcasts (non-repeating MAC
             broadcast) to every bulb in direct RF range. Records may mix
             MODE_DEVICE (whole bulb) and MODE_SEGMENT (gradient segment).
- stop     = cmd 3 to each device (doubles as the sequence-sync command)

The per-frame ``smoothing`` field is a fade time (0xFFFF = 2.56 s); deriving it
from the frame interval is what makes 20-25 fps look continuous. At 25 fps it
computes to 0x0400 - the constant real Hue bridges hardcode.
"""

from __future__ import annotations

import json
import struct

CLUSTER = 0xFC01
MANUFACTURER = 0x100B  # Signify
CMD_STREAM = 1
CMD_SYNC = 3  # sync/stop ("reset" in Bifrost)
CMD_SEGMENT_MAP = 7  # configure virtual per-segment addresses on a gradient device
SMOOTHING_MAX_US = 2_560_000.0
# Bottom 5 bits of the brightness field:
#   Device  = 0b01011 - whole bulb, addressed by its real Zigbee nwk address
#   Segment = 0b00000 - one gradient segment, addressed by a virtual u16 handed
#                      to the device via CMD_SEGMENT_MAP before streaming
MODE_DEVICE = 0b01011
MODE_SEGMENT = 0b00000
# Hard protocol limit: 6-byte frame header + 7 bytes/record + 5-byte ZCL header
# hits the ~82-byte single-APS-frame ceiling at exactly 10 records. A gradient
# segment counts as one record, same as a whole bulb.
MAX_RECORDS_PER_FRAME = 10
MAX_LIGHTS_PER_FRAME = MAX_RECORDS_PER_FRAME  # backwards-compatible alias


def zclcommand(cmd: int, data: bytes) -> str:
    """JSON payload for zigbee2mqtt/<name>/set carrying one raw FC01 command."""
    return json.dumps(
        {
            "zclcommand": {
                "cluster": CLUSTER,
                "command": cmd,
                "payload": {"data": list(data)},
                "frametype": 1,
                "options": {
                    "manufacturerCode": MANUFACTURER,
                    "disableDefaultResponse": True,
                },
            }
        }
    )


def arm_write_payload() -> str:
    """Attribute write that precedes the sync when arming a bulb."""
    return json.dumps(
        {
            "write": {
                "cluster": CLUSTER,
                "payload": {
                    "5": {"manufacturerCode": MANUFACTURER, "type": 0x20, "value": 0xFE}
                },
            }
        }
    )


def sync_payload(counter: int) -> str:
    """cmd 3 - arms the sequence counter; also the clean-stop command."""
    return zclcommand(CMD_SYNC, bytes([0, 1]) + struct.pack("<I", counter & 0xFFFFFFFF))


def smoothing_for_fps(fps: float) -> int:
    interval_us = 1_000_000.0 / fps
    return min(0xFFFF, round(interval_us / SMOOTHING_MAX_US * 0xFFFF))


def light_record(
    nwk_addr: int, bri11: int, x12: int, y12: int, mode: int = MODE_DEVICE
) -> bytes:
    """One 7-byte per-record: nwk addr (or virtual segment addr), brightness+mode, packed 12-bit xy.

    ``mode`` is ``MODE_DEVICE`` for a whole bulb (``nwk_addr`` is the real Zigbee
    short address) or ``MODE_SEGMENT`` for one segment of a gradient light
    (``nwk_addr`` is a virtual address previously handed to the device via
    ``segment_map_payload``).
    """
    packed = ((bri11 & 0x7FF) << 5) | (mode & 0x1F)
    return struct.pack("<HH", nwk_addr, packed) + bytes(
        [x12 & 0xFF, ((x12 >> 8) & 0x0F) | ((y12 & 0x0F) << 4), (y12 >> 4) & 0xFF]
    )


def segment_map_payload(virtual_addrs: list[int]) -> str:
    """cmd 7 - configure the virtual segment addresses on a gradient device.

    Sent once (per arm) to a multi-segment light (e.g. Hue Play Gradient
    Lightstrip). After this, each ``virtual_addrs[i]`` addresses segment ``i``
    in subsequent stream frames whose records use ``MODE_SEGMENT``.

    Payload layout (matches Bifrost's ``HueEntSegmentConfig`` wire format):
    count as **big-endian** u16, then each address as **little-endian** u16.
    Ordinary bulbs reply "Command Not Supported" - useful as a capability probe.
    """
    if not virtual_addrs:
        raise ValueError("segment_map_payload requires at least one address")
    data = struct.pack(">H", len(virtual_addrs)) + b"".join(
        struct.pack("<H", a & 0xFFFF) for a in virtual_addrs
    )
    return zclcommand(CMD_SEGMENT_MAP, data)


def stream_frame_payload(counter: int, smoothing: int, records: list[bytes]) -> str:
    """cmd 1 - one frame for the whole zone, sent only to the proxy device.

    ``records`` may mix ``MODE_DEVICE`` (whole bulb) and ``MODE_SEGMENT``
    (gradient segment) entries; the 10-per-frame cap counts both alike.
    """
    if len(records) > MAX_RECORDS_PER_FRAME:
        raise ValueError(
            f"{len(records)} records exceeds the {MAX_RECORDS_PER_FRAME}-record frame limit"
        )
    data = struct.pack("<IH", counter & 0xFFFFFFFF, smoothing) + b"".join(records)
    return zclcommand(CMD_STREAM, data)
