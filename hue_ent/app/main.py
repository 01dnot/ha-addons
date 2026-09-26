"""Hue Entertainment bridge daemon.

Receives per-zone pixel streams from LedFX (DDP, one pixel per bulb) and drives
Philips Hue bulbs on zigbee2mqtt at 20-25 fps via the reverse-engineered Hue
Entertainment Zigbee protocol (see protocol.py).

Zones are auto-discovered by default: color-capable Philips lights are grouped
by their Home Assistant area (registry.py), refined by the user's edits from
the ingress GUI (zonestore.py / web.py), and can be overridden entirely with
manual ``zones:`` in the app options. Each zone gets an HA switch via MQTT
discovery; arming captures bulb state (and pauses e.g. Adaptive Lighting via
``pause_entities``), disarming restores everything.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import signal
import time
import urllib.request

import aiomqtt

from . import color, ledfx, protocol, registry, web, zonestore

LOG = logging.getLogger("hue_ent")

Z2M_BASE = os.environ.get("Z2M_BASE_TOPIC", "zigbee2mqtt")
DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant")
BASE_TOPIC = "hue_ent"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/availability"
KEEPALIVE_S = 4.0  # bulbs drop out of entertainment mode after a few silent seconds
REARM_GAP_S = 6.0  # a zigbee-send gap longer than this means the mode has expired


def _rate(stamps) -> float:
    """Frames per second over a deque of monotonic timestamps (0 if stale/empty)."""
    if len(stamps) < 2:
        return 0.0
    span = stamps[-1] - stamps[0]
    if span <= 0 or time.monotonic() - stamps[-1] > 2.0:
        return 0.0
    return round((len(stamps) - 1) / span, 1)


def load_options() -> dict:
    path = os.environ.get("OPTIONS_FILE", "/data/options.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# Segment counts for known Hue gradient devices, keyed by either the Zigbee
# ``model_id`` (LCX001, ...) or the Z2M ``definition.model`` (Signify article
# number, e.g. 929002422702). Anything else with a ``gradient`` expose but no
# entry here falls back to DEFAULT_GRADIENT_SEGMENTS - 7, which fits the
# LCX001/003/004 TV strip family (the most common case).
GRADIENT_SEGMENTS_BY_MODEL: dict[str, int] = {
    # TV strips (7 segments) - LCX001 = 55", LCX002 = 65", LCX003 = 75", LCX004 = 55" newer
    "LCX001": 7, "LCX002": 7, "LCX003": 7, "LCX004": 7,
    "929002422702": 7,  # Hue Play gradient lightstrip 55 (article number)
    # PC strips (10 segments) - Bifrost's HueLightstripPc archetype
    "LCX005": 10, "LCX006": 10, "LCX007": 10,
    # Signe gradient floor/table (5 segments)
    "915005987201": 5, "4080248U9": 5, "4080148U9": 5,
}
DEFAULT_GRADIENT_SEGMENTS = 7


def _segments_for(dev: dict, has_gradient: bool) -> int:
    """How many independently addressable segments this device exposes.

    Consults the Zigbee ``model_id`` first (LCX001, ...) then the Z2M
    ``definition.model`` (article number), so both spellings hit the table.
    """
    if not has_gradient:
        return 1
    definition = dev.get("definition") or {}
    zigbee_model = str(dev.get("model_id") or "").upper()
    def_model = str(definition.get("model") or "").upper()
    return (
        GRADIENT_SEGMENTS_BY_MODEL.get(zigbee_model)
        or GRADIENT_SEGMENTS_BY_MODEL.get(def_model)
        or DEFAULT_GRADIENT_SEGMENTS
    )


def parse_z2m_devices(devices: list) -> tuple[dict[str, int], dict[str, dict]]:
    """Extract (nwk-map, Philips-light-info) from a zigbee2mqtt bridge/devices payload.

    Returns:
      nwk_map:    {friendly_name: network_address}, for every device with one
      lights_map: {friendly_name: {"ieee": ..., "color": ..., "segments": ...,
                                    "model": ...}} for every Philips light seen

    A ``gradient`` feature in the exposes marks a multi-segment device
    (Hue Play Gradient Lightstrip and family); segment counts come from a
    per-model table, with a 7-segment fallback for unknown gradient devices.
    """
    nwk_map: dict[str, int] = {}
    lights: dict[str, dict] = {}
    for dev in devices:
        fn = dev.get("friendly_name")
        if fn and dev.get("network_address") is not None:
            nwk_map[fn] = dev["network_address"]
        definition = dev.get("definition") or {}
        if not fn or definition.get("vendor") != "Philips":
            continue
        has_color = False
        is_light = False
        has_gradient = False
        for expose in definition.get("exposes") or []:
            if expose.get("type") == "light":
                is_light = True
                for feature in expose.get("features") or []:
                    name = feature.get("name")
                    if name == "color_xy":
                        has_color = True
                    elif name == "gradient":
                        has_gradient = True
            # z2m sometimes lists gradient as a top-level expose alongside the
            # light expose, not nested inside its features - accept both.
            elif expose.get("name") == "gradient":
                has_gradient = True
        if is_light:
            lights[fn] = {
                "ieee": str(dev.get("ieee_address", "")).lower(),
                "color": has_color,
                "gradient": has_gradient,
                "segments": _segments_for(dev, has_gradient),
                "model": str(definition.get("model") or ""),
                "zigbee_model": str(dev.get("model_id") or ""),
            }
    return nwk_map, lights


class Zone:
    def __init__(self, cfg: dict):
        self.name: str = cfg["name"]
        self.slug = zonestore._slug(self.name)
        self.lights: list[str] = list(cfg["lights"])
        if not self.lights:
            raise ValueError(f"zone '{self.name}' has no lights")
        light_meta = cfg.get("light_meta") or {}
        self.segments: dict[str, int] = {
            fn: max(1, int((light_meta.get(fn) or {}).get("segments") or 1))
            for fn in self.lights
        }
        # A gradient segment is one Zigbee record just like a whole bulb, so the
        # 10-per-frame protocol cap counts across both. A 7-segment gradient
        # strip + up to 3 other bulbs fits; +4 or more does not.
        self.records_per_frame: int = sum(self.segments.values())
        if self.records_per_frame > protocol.MAX_RECORDS_PER_FRAME:
            raise ValueError(
                f"zone '{self.name}' needs {self.records_per_frame} records/frame "
                f"({len(self.lights)} lights, segments={list(self.segments.values())}); "
                f"the protocol caps a zone at {protocol.MAX_RECORDS_PER_FRAME}"
            )
        self.proxy: str = cfg.get("proxy") or self.lights[0]
        if self.proxy not in self.lights:
            raise ValueError(f"zone '{self.name}': proxy '{self.proxy}' is not one of its lights")
        self.fps: float = float(cfg.get("fps") or 25)
        self.ddp_port: int = int(cfg["ddp_port"])
        self.idle_timeout_s: float = float(cfg.get("idle_timeout_s") or 30)
        self.auto_start: bool = bool(cfg.get("auto_start", True))
        self.pause_entities: list[str] = [e for e in (cfg.get("pause_entities") or []) if e]
        self.brightness_scale: float = float(cfg.get("brightness_scale") or 1.0)
        # Protocol minimum is 1; raise (~20-50) if your strip firmware drops
        # to a warm-white fallback at very low computed brightness.
        self.bri_floor: int = max(1, min(2047, int(cfg.get("bri_floor") or 1)))

    @property
    def pixel_count(self) -> int:
        """DDP pixels expected per frame: one per bulb, N per gradient light."""
        return self.records_per_frame

    @property
    def switch_command_topic(self) -> str:
        return f"{BASE_TOPIC}/{self.slug}/set"

    @property
    def switch_state_topic(self) -> str:
        return f"{BASE_TOPIC}/{self.slug}/state"


class DdpProtocol(asyncio.DatagramProtocol):
    """Keeps only the newest frame - the zone ticker samples latest-wins."""

    def __init__(self, pixel_count: int, on_activity):
        self.pixel_count = pixel_count
        self.on_activity = on_activity
        self.latest: list[tuple[int, int, int]] | None = None
        self.last_rx = 0.0
        self.frames_rx = 0
        # Arrival times of the last few frames, for the rate the panel shows:
        # "is LedFX actually sending to this zone?" is the first question of
        # every setup problem, and the answer used to be invisible.
        self._arrivals: collections.deque[float] = collections.deque(maxlen=64)

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < 10 + self.pixel_count * 3:
            return
        body = data[10 : 10 + self.pixel_count * 3]
        px = range(self.pixel_count)
        self.latest = [(body[i * 3], body[i * 3 + 1], body[i * 3 + 2]) for i in px]
        self.last_rx = time.monotonic()
        self.frames_rx += 1
        self._arrivals.append(self.last_rx)
        self.on_activity(addr)

    @property
    def rx_fps(self) -> float:
        return _rate(self._arrivals)


class ZoneRunner:
    def __init__(self, zone: Zone, bridge: Bridge):
        self.zone = zone
        self.bridge = bridge
        self.ddp: DdpProtocol | None = None
        self.ddp_transport: asyncio.DatagramTransport | None = None
        self.armed = False
        self.counter = 0
        self.saved_states: dict[str, dict | None] = {}
        self._ticker: asyncio.Task | None = None
        self._armed_at = 0.0
        self.sends = 0                     # Zigbee frames pushed to the proxy
        self._sent_at: collections.deque[float] = collections.deque(maxlen=64)
        self._last_zig_send = 0.0
        self._last_sent_frame: list[tuple[int, int, int]] | None = None
        # Set by a manual switch-off: don't auto-arm again for the SAME DDP
        # stream - only once it stops (>5 s gap) and a new one begins.
        self._suppress_auto = False
        self._prev_rx = 0.0
        # Ticker sleeps on this event; each DDP arrival wakes it, so a fresh
        # frame is sent within a rate-limited slot rather than waiting up to
        # one full tick interval. Rate-limit still enforces the fps cap.
        self._frame_event = asyncio.Event()

    def on_ddp_activity(self, addr=None) -> None:
        now = time.monotonic()
        stream_gap = now - self._prev_rx if self._prev_rx else float("inf")
        self._prev_rx = now
        if self._suppress_auto and stream_gap > 5.0:
            LOG.info("[%s] new DDP stream detected - auto-start re-enabled", self.zone.name)
            self._suppress_auto = False
        # Loopback = LedFX on same host (whose auto-provisioning we may be
        # running). Anything else means HyperHDR / piccap / a remote LedFX -
        # the user brought their own DDP source, so we should stop the
        # provisioning retry loop instead of spamming the log with
        # "LedFX not reachable" every 30s.
        if addr and addr[0] not in ("127.0.0.1", "::1"):
            self.bridge.notify_external_ddp(addr[0])
        if not self.armed and self.zone.auto_start and not self._suppress_auto:
            self.bridge.schedule_arm(self.zone.slug, reason="ddp")
        # Wake the ticker so a fresh frame goes out on its next rate-limit
        # slot rather than sitting through the rest of an interval sleep.
        self._frame_event.set()

    @property
    def stats(self) -> dict:
        """What the zone is doing right now, for the sidebar panel.

        Ages are None when the thing hasn't happened yet, so the panel can tell
        "nothing has ever arrived on this port" (a LedFX device pointed
        somewhere else) apart from "the stream stopped a minute ago".
        """
        now = time.monotonic()
        ddp = self.ddp
        return {
            "armed": self.armed,
            "armed_for_s": round(now - self._armed_at, 1) if self.armed else None,
            "frames_rx": ddp.frames_rx if ddp else 0,
            "rx_fps": ddp.rx_fps if ddp else 0.0,
            "last_rx_age_s": (round(now - ddp.last_rx, 1)
                              if ddp and ddp.last_rx else None),
            "sends": self.sends,
            "tx_fps": _rate(self._sent_at),
        }

    def manual_off(self) -> asyncio.Task:
        """Switch turned off in HA: stay off for the rest of this DDP stream."""
        self._suppress_auto = True
        return asyncio.get_running_loop().create_task(self.disarm())

    # -- lifecycle -------------------------------------------------------

    async def arm(self) -> None:
        if self.armed:
            return
        LOG.info(
            "[%s] arming (%d lights, proxy=%s, %g fps)",
            self.zone.name, len(self.zone.lights), self.zone.proxy, self.zone.fps,
        )
        self.saved_states = {fn: self.bridge.light_states.get(fn) for fn in self.zone.lights}
        await self.bridge.set_pause_entities(self.zone, paused=True)
        # Lights must be on to render; turn them on without disturbing color.
        for fn in self.zone.lights:
            prev = self.saved_states.get(fn)
            if not prev or prev.get("state") != "ON":
                await self.bridge.publish(f"{Z2M_BASE}/{fn}/set", json.dumps({"state": "ON"}))
        await asyncio.sleep(0.3)
        await self._arm_ritual()
        self.armed = True
        self._armed_at = time.monotonic()
        self._last_zig_send = 0.0
        self._last_sent_frame = None
        await self.bridge.publish(self.zone.switch_state_topic, "ON", retain=True)
        self._ticker = asyncio.create_task(self._run_ticker())

    async def _arm_ritual(self) -> None:
        """Stop-all, then per light: attribute write + sequence sync (+ segment map)."""
        for fn in self.zone.lights:
            await self.bridge.publish(f"{Z2M_BASE}/{fn}/set", protocol.sync_payload(self.counter))
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        for fn in self.zone.lights:
            await self.bridge.publish(f"{Z2M_BASE}/{fn}/set", protocol.arm_write_payload())
            await asyncio.sleep(0.15)
            # A gradient device gets its segments assigned virtual addresses
            # nwk..nwk+N-1 (Bifrost convention), used in every subsequent
            # segment-mode record. Ordinary bulbs reply "Command Not Supported"
            # to CMD_SEGMENT_MAP - harmless but noisy, so we skip them.
            segs = self.zone.segments[fn]
            nwk = self.bridge.nwk.get(fn)
            if segs > 1 and nwk is not None:
                virtual_addrs = [(nwk + i) & 0xFFFF for i in range(segs)]
                await self.bridge.publish(
                    f"{Z2M_BASE}/{fn}/set", protocol.segment_map_payload(virtual_addrs)
                )
                await asyncio.sleep(0.15)
            await self.bridge.publish(f"{Z2M_BASE}/{fn}/set", protocol.sync_payload(self.counter))
            await asyncio.sleep(0.15)

    async def disarm(self) -> None:
        if not self.armed:
            return
        LOG.info("[%s] disarming", self.zone.name)
        self.armed = False
        if self._ticker:
            self._ticker.cancel()
            self._ticker = None
        try:
            await self._send_black()
            await asyncio.sleep(0.3)
            for fn in self.zone.lights:
                topic = f"{Z2M_BASE}/{fn}/set"
                await self.bridge.publish(topic, protocol.sync_payload(self.counter))
                await asyncio.sleep(0.05)
            await self._restore_states()
        finally:
            await self.bridge.set_pause_entities(self.zone, paused=False)
            await self.bridge.publish(self.zone.switch_state_topic, "OFF", retain=True)

    def close(self) -> None:
        """Release runtime resources (ticker + UDP socket) for a live rebuild."""
        if self._ticker:
            self._ticker.cancel()
            self._ticker = None
        if self.ddp_transport is not None:
            self.ddp_transport.close()
            self.ddp_transport = None

    async def _send_black(self) -> None:
        records = []
        for fn in self.zone.lights:
            nwk = self.bridge.nwk.get(fn)
            if nwk is None:
                continue
            segs = self.zone.segments[fn]
            if segs == 1:
                records.append(protocol.light_record(nwk, 1, 1743, 1631))  # dim D65
            else:
                for i in range(segs):
                    records.append(
                        protocol.light_record(
                            (nwk + i) & 0xFFFF, 1, 1743, 1631, mode=protocol.MODE_SEGMENT
                        )
                    )
        if records:
            self.counter += 1
            await self.bridge.publish(
                f"{Z2M_BASE}/{self.zone.proxy}/set",
                protocol.stream_frame_payload(self.counter, 0x0100, records),
            )

    async def _restore_states(self) -> None:
        for fn, prev in self.saved_states.items():
            if prev is None:
                continue
            if prev.get("state") != "ON":
                payload: dict = {"state": "OFF"}
            else:
                payload = {"state": "ON"}
                if prev.get("brightness") is not None:
                    payload["brightness"] = prev["brightness"]
                if prev.get("color_mode") == "xy" and isinstance(prev.get("color"), dict):
                    payload["color"] = {"x": prev["color"].get("x"), "y": prev["color"].get("y")}
                elif prev.get("color_temp") is not None:
                    payload["color_temp"] = prev["color_temp"]
            await self.bridge.publish(f"{Z2M_BASE}/{fn}/set", json.dumps(payload))
            await asyncio.sleep(0.05)

    # -- streaming -------------------------------------------------------

    async def _run_ticker(self) -> None:
        """Event-driven send loop: wakes on DDP arrival (or on a
        short timeout so idle-checks and keepalives still fire),
        then rate-limits to zone.fps.

        The old implementation slept a full interval between sends,
        so a DDP frame that arrived right after a tick sat waiting up
        to ~50 ms before going out. Waking on arrival cuts that
        (~20-25 ms average at 20-25 fps) with no other tradeoff:
        the fps cap is still enforced by the min-interval sleep, and
        latest-wins dedup in DdpProtocol keeps us from
        bursting past the source rate.
        """
        interval = 1.0 / self.zone.fps
        smoothing = protocol.smoothing_for_fps(self.zone.fps)
        # Cap the idle wait so keepalive + idle-timeout still fire
        # when HyperHDR is silent. A short cap here is fine - the
        # ticker just runs a few checks and goes back to sleep.
        idle_wait = min(interval, 0.5)
        self._frame_event.clear()
        try:
            while self.armed:
                # Wait for the next DDP frame, but no longer than idle_wait
                # so we still fire keepalives and the idle-timeout check.
                # Both aliases matter: on Python 3.9-3.10 asyncio.TimeoutError
                # is distinct from the builtin; from 3.11 they alias.
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(self._frame_event.wait(), timeout=idle_wait)
                self._frame_event.clear()

                ddp = self.ddp
                # Idle is measured from the last frame, or from the arm when no
                # frame has ever arrived - otherwise a zone armed with nothing
                # streaming (an HA switch, the panel's test button, a LedFX that
                # never starts) stays armed forever, holding its pause entities
                # off and its switch on.
                last_rx = ddp.last_rx if (ddp and ddp.last_rx) else self._armed_at
                idle_for = time.monotonic() - last_rx
                if idle_for > self.zone.idle_timeout_s:
                    LOG.info("[%s] no DDP for %.0fs - auto-disarming", self.zone.name, idle_for)
                    asyncio.get_running_loop().create_task(self.disarm())
                    return
                if ddp is None or ddp.latest is None:
                    continue

                # Rate-limit: at most one send per interval. Sleeping AFTER
                # the event wait keeps this event-driven while still capping
                # the outbound rate at zone.fps.
                since_last = time.monotonic() - self._last_zig_send
                if self._last_zig_send and since_last < interval:
                    await asyncio.sleep(interval - since_last)

                frame = ddp.latest  # re-read after the sleep; latest-wins
                fresh = frame != self._last_sent_frame
                due_keepalive = time.monotonic() - self._last_zig_send >= KEEPALIVE_S
                if not fresh and not due_keepalive:
                    continue
                # If the mode has expired (long send gap), re-arm before streaming.
                if self._last_zig_send and time.monotonic() - self._last_zig_send > REARM_GAP_S:
                    LOG.info("[%s] send gap > %.0fs - re-arming", self.zone.name, REARM_GAP_S)
                    await self._arm_ritual()
                await self._send_frame(frame, smoothing)
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.exception("[%s] ticker crashed - disarming", self.zone.name)
            asyncio.get_running_loop().create_task(self.disarm())

    async def _send_frame(self, frame: list[tuple[int, int, int]], smoothing: int) -> None:
        records = []
        # Pixel index walks the DDP frame; a gradient light with N segments
        # consumes N consecutive pixels so the effect renders as a gradient.
        pixel_idx = 0
        for fn in self.zone.lights:
            segs = self.zone.segments[fn]
            nwk = self.bridge.nwk.get(fn)
            if nwk is None:
                pixel_idx += segs  # keep the frame alignment even if we can't send
                continue
            if segs == 1:
                r, g, b = frame[pixel_idx] if pixel_idx < len(frame) else frame[-1]
                bri, x12, y12 = color.rgb8_to_entertainment(
                    r, g, b, brightness_scale=self.zone.brightness_scale,
                    bri_floor=self.zone.bri_floor,
                )
                records.append(protocol.light_record(nwk, bri, x12, y12))
            else:
                for i in range(segs):
                    px = pixel_idx + i
                    r, g, b = frame[px] if px < len(frame) else frame[-1]
                    bri, x12, y12 = color.rgb8_to_entertainment(
                        r, g, b, brightness_scale=self.zone.brightness_scale,
                        bri_floor=self.zone.bri_floor,
                    )
                    records.append(
                        protocol.light_record(
                            (nwk + i) & 0xFFFF, bri, x12, y12, mode=protocol.MODE_SEGMENT
                        )
                    )
            pixel_idx += segs
        if not records:
            return
        self.counter += 1
        await self.bridge.publish(
            f"{Z2M_BASE}/{self.zone.proxy}/set",
            protocol.stream_frame_payload(self.counter, smoothing, records),
        )
        self._last_zig_send = time.monotonic()
        self._sent_at.append(self._last_zig_send)
        self.sends += 1
        self._last_sent_frame = list(frame)


class Bridge:
    def __init__(self, options: dict, store: zonestore.ZoneStore):
        self.options = options
        self.store = store
        self.zones: dict[str, Zone] = {}
        self.runners: dict[str, ZoneRunner] = {}
        self.nwk: dict[str, int] = {}
        self.z2m_lights: dict[str, dict] = {}  # Philips lights: fn -> {ieee, color}
        self.light_states: dict[str, dict] = {}
        self.auto_rooms: list[dict] = []
        self.discovery = registry.Discovery()  # last room-discovery result (for the GUI)
        self.room_views: list[dict] = []
        self.client: aiomqtt.Client | None = None
        self.stopping = False
        self.devices_seen = asyncio.Event()
        self._pending_arms: set[str] = set()
        self._known_slugs: set[str] = set()
        self.provision_task: asyncio.Task | None = None
        self._rebuild_lock = asyncio.Lock()
        self._external_ddp_source: str | None = None

    # -- zone assembly / live rebuild -------------------------------------

    async def rebuild_zones(self) -> None:
        """(Re)assemble effective zones from auto rooms + overrides and apply live."""
        async with self._rebuild_lock:
            manual = self.options.get("zones") or []
            auto_enabled = bool(self.options.get("auto_zones", True))
            configs, views = self.store.assemble(self.auto_rooms, manual, auto_enabled)
            self.room_views = views

            for runner in self.runners.values():
                if runner.armed:
                    await runner.disarm()
                runner.close()
            await asyncio.sleep(0.2)  # let UDP sockets fully release before rebinding

            old_slugs = set(self.zones)
            zones: dict[str, Zone] = {}
            for cfg in configs:
                # Enrich every cfg with per-light segment info from what we
                # learned in bridge/devices, so a gradient strip renders as
                # N pixels instead of one.
                cfg = dict(cfg)
                cfg["light_meta"] = {
                    fn: {"segments": (self.z2m_lights.get(fn) or {}).get("segments", 1)}
                    for fn in cfg.get("lights", [])
                }
                try:
                    zone = Zone(cfg)
                    zones[zone.slug] = zone
                except (ValueError, KeyError) as exc:
                    LOG.error("skipping zone: %s", exc)
            self.zones = zones
            self.runners = {slug: ZoneRunner(zone, self) for slug, zone in zones.items()}

            loop = asyncio.get_running_loop()
            for slug, zone in self.zones.items():
                runner = self.runners[slug]
                try:
                    transport, proto = await loop.create_datagram_endpoint(
                        lambda z=zone, r=runner: DdpProtocol(z.pixel_count, r.on_ddp_activity),
                        local_addr=("0.0.0.0", zone.ddp_port),
                    )
                except OSError as exc:
                    LOG.error("[%s] cannot bind DDP port %d: %s", zone.name, zone.ddp_port, exc)
                    continue
                runner.ddp = proto
                runner.ddp_transport = transport
                grad = [f"{fn} x{n}" for fn, n in zone.segments.items() if n > 1]
                LOG.info(
                    "[%s] DDP listener on :%d (%d px, %g fps%s)",
                    zone.name, zone.ddp_port, zone.pixel_count, zone.fps,
                    f", gradient: {', '.join(grad)}" if grad else "",
                )

            if self.client is not None:
                await self._subscribe_zones()
                await self.publish_discovery()
                for slug in (old_slugs | self._known_slugs) - set(self.zones):
                    await self._clear_discovery(slug)
            self._known_slugs |= set(self.zones)
            self._kick_provisioning()

    def notify_external_ddp(self, src_ip: str) -> None:
        """A non-loopback DDP source is delivering frames - the user has
        their own sender (HyperHDR, piccap, remote LedFX). Stop the LedFX
        auto-provisioning loop so the log doesn't fill with reachability
        errors while their real setup works fine.

        Logged unconditionally on the first frame from a new source so a
        human debugging "is HyperHDR actually reaching me?" always has a
        yes/no answer in the log, even when ledfx_url is empty (nothing
        to cancel).
        """
        if self._external_ddp_source == src_ip:
            return
        self._external_ddp_source = src_ip
        cancelled = False
        if self.provision_task is not None and not self.provision_task.done():
            self.provision_task.cancel()
            self.provision_task = None
            cancelled = True
        LOG.info(
            "external DDP source detected (%s)%s",
            src_ip,
            " - LedFX auto-provisioning stopped" if cancelled else "",
        )

    def _kick_provisioning(self) -> None:
        ledfx_url = str(self.options.get("ledfx_url") or "").strip()
        ledfx_target = str(self.options.get("ledfx_ddp_target") or "127.0.0.1").strip()
        if not ledfx_url or not self.zones:
            return
        if self._external_ddp_source:
            # Already know the user is streaming from HyperHDR / piccap / a
            # remote LedFX. Don't restart a doomed provisioning loop on rebuild.
            return
        if self.provision_task is not None and not self.provision_task.done():
            self.provision_task.cancel()
        self.provision_task = asyncio.ensure_future(
            ledfx.provision_forever(ledfx_url, ledfx_target, list(self.zones.values()))
        )

    async def rescan_rooms(self) -> None:
        self.discovery = await registry.discover_rooms(self.z2m_lights, retries=1)
        self.auto_rooms = self.discovery.rooms
        await self.rebuild_zones()

    # -- MQTT plumbing -----------------------------------------------------

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if self.client is None:
            return
        try:
            await self.client.publish(topic, payload, qos=0, retain=retain)
        except aiomqtt.MqttError as exc:
            LOG.debug("publish to %s failed: %s", topic, exc)

    async def _subscribe_zones(self) -> None:
        if self.client is None:
            return
        for zone in self.zones.values():
            await self.client.subscribe(zone.switch_command_topic)
            for fn in zone.lights:
                await self.client.subscribe(f"{Z2M_BASE}/{fn}")

    async def publish_discovery(self) -> None:
        device = {
            "identifiers": ["hue_ent_bridge"],
            "name": "Hue Entertainment",
            "manufacturer": "adamoberley/ha-addons",
            "model": "LedFX Zigbee streaming bridge",
        }
        for zone in self.zones.values():
            config = {
                "name": zone.name,  # device name provides the "Hue Entertainment" context
                "unique_id": f"hue_ent_{zone.slug}",
                "command_topic": zone.switch_command_topic,
                "state_topic": zone.switch_state_topic,
                "availability_topic": AVAILABILITY_TOPIC,
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:track-light",
                "device": device,
            }
            await self.publish(
                f"{DISCOVERY_PREFIX}/switch/hue_ent_{zone.slug}/config",
                json.dumps(config),
                retain=True,
            )
            # Seed the retained state so the entity isn't "unknown" on first boot.
            state = "ON" if self.runners[zone.slug].armed else "OFF"
            await self.publish(zone.switch_state_topic, state, retain=True)

    async def _clear_discovery(self, slug: str) -> None:
        await self.publish(f"{DISCOVERY_PREFIX}/switch/hue_ent_{slug}/config", "", retain=True)
        await self.publish(f"{BASE_TOPIC}/{slug}/state", "", retain=True)

    # -- arming ------------------------------------------------------------

    def schedule_arm(self, slug: str, reason: str) -> None:
        if self.stopping or slug in self._pending_arms:
            return
        self._pending_arms.add(slug)

        async def _do() -> None:
            try:
                await self.arm_zone(slug)
            finally:
                self._pending_arms.discard(slug)

        asyncio.get_running_loop().create_task(_do())

    async def arm_zone(self, slug: str) -> None:
        if slug not in self.zones:
            return
        # Only one zone streams at a time (single coordinator airtime budget,
        # single proxy broadcast domain) - arming a zone stops the active one.
        for other_slug, runner in self.runners.items():
            if other_slug != slug and runner.armed:
                LOG.info("zone '%s' requested while '%s' active - stopping it", slug, other_slug)
                await runner.disarm()
        deadline = time.monotonic() + 5.0
        while (
            any(fn not in self.nwk for fn in self.zones[slug].lights)
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.2)
        missing = [fn for fn in self.zones[slug].lights if fn not in self.nwk]
        if missing:
            LOG.error(
                "[%s] cannot arm - no network address for %s (renamed or not paired?)",
                slug, missing,
            )
            await self.publish(self.zones[slug].switch_state_topic, "OFF", retain=True)
            return
        await self.runners[slug].arm()

    # -- Home Assistant Core service calls (pause entities) --------------

    async def set_pause_entities(self, zone: Zone, paused: bool) -> None:
        if not zone.pause_entities:
            return
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            LOG.warning("[%s] pause_entities set but no SUPERVISOR_TOKEN; skipping", zone.name)
            return
        service = "turn_off" if paused else "turn_on"

        def _call() -> None:
            body = json.dumps({"entity_id": zone.pause_entities}).encode()
            req = urllib.request.Request(
                f"http://supervisor/core/api/services/homeassistant/{service}",
                data=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)

        try:
            await asyncio.to_thread(_call)
            LOG.info("[%s] %s: %s", zone.name, service, ", ".join(zone.pause_entities))
        except Exception as exc:
            LOG.warning("[%s] pause entity call failed: %s", zone.name, exc)

    # -- message handling ---------------------------------------------------

    def _parse_z2m_devices(self, payload: bytes) -> None:
        try:
            devices = json.loads(payload)
        except Exception:
            LOG.exception("failed to parse bridge/devices")
            return
        nwk, lights = parse_z2m_devices(devices)
        self.nwk.update(nwk)
        self.z2m_lights = lights
        LOG.info(
            "device list updated (%d addresses, %d Philips lights)", len(self.nwk), len(lights)
        )
        self.devices_seen.set()

    def handle_message(self, topic: str, payload: bytes) -> None:
        if topic == f"{Z2M_BASE}/bridge/devices":
            self._parse_z2m_devices(payload)
            return
        for zone in self.zones.values():
            if topic == zone.switch_command_topic:
                want_on = payload.decode(errors="replace").strip().upper() == "ON"
                if want_on:
                    self.schedule_arm(zone.slug, reason="switch")
                else:
                    self.runners[zone.slug].manual_off()
                return
            for fn in zone.lights:
                if topic == f"{Z2M_BASE}/{fn}":
                    runner = self.runners[zone.slug]
                    if not runner.armed:  # don't let mid-stream reports pollute the snapshot
                        with contextlib.suppress(Exception):
                            self.light_states[fn] = json.loads(payload)

    # -- main loop -----------------------------------------------------------

    async def run(self) -> None:
        host = os.environ.get("MQTT_HOST", "127.0.0.1")
        port = int(os.environ.get("MQTT_PORT", "1883"))
        user = os.environ.get("MQTT_USER") or None
        password = os.environ.get("MQTT_PASS") or None
        will = aiomqtt.Will(AVAILABILITY_TOPIC, "offline", qos=0, retain=True)
        while True:
            try:
                async with aiomqtt.Client(
                    host, port, username=user, password=password,
                    will=will, identifier="hue_ent_bridge",
                ) as client:
                    self.client = client
                    LOG.info("connected to MQTT %s:%d", host, port)
                    await client.subscribe(f"{Z2M_BASE}/bridge/devices")
                    await self._subscribe_zones()
                    await self.publish_discovery()
                    await self.publish(AVAILABILITY_TOPIC, "online", retain=True)
                    try:
                        async for message in client.messages:
                            self.handle_message(str(message.topic), bytes(message.payload))
                    except asyncio.CancelledError:
                        LOG.info("shutdown signal received - restoring zones")
                        await self.shutdown()
                        raise
            except aiomqtt.MqttError as exc:
                self.client = None
                for runner in self.runners.values():
                    runner.armed = False  # tickers stop; bulbs time out of mode on their own
                LOG.warning("MQTT connection lost (%s); reconnecting in 5s", exc)
                await asyncio.sleep(5)

    async def shutdown(self) -> None:
        self.stopping = True
        for runner in self.runners.values():
            if runner.armed:
                await runner.disarm()
        await self.publish(AVAILABILITY_TOPIC, "offline", retain=True)


async def _bootstrap(bridge: Bridge) -> None:
    """Once Z2M's device list is in: discover rooms, then build zones."""
    await bridge.devices_seen.wait()
    if bridge.options.get("auto_zones", True):
        bridge.discovery = await registry.discover_rooms(bridge.z2m_lights)
        bridge.auto_rooms = bridge.discovery.rooms
    await bridge.rebuild_zones()
    if not bridge.zones:
        LOG.warning(
            "no zones active - %s Enable rooms in the sidebar panel, "
            "or add manual zones in the app configuration.",
            bridge.discovery.summary,
        )


async def async_main() -> None:
    options = load_options()
    logging.basicConfig(
        level=getattr(logging, str(options.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    store = zonestore.ZoneStore()
    bridge = Bridge(options, store)

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, main_task.cancel)

    web_runner = await web.start(bridge, port=int(os.environ.get("WEB_PORT", "8127")))
    bootstrap_task = asyncio.ensure_future(_bootstrap(bridge))
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await bridge.run()
    finally:
        bootstrap_task.cancel()
        if bridge.provision_task is not None:
            bridge.provision_task.cancel()
        with contextlib.suppress(Exception):
            await web_runner.cleanup()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(async_main())


if __name__ == "__main__":
    main()
