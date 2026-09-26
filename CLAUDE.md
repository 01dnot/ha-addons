# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Personal fork of `adamoberley/ha-addons`, stripped down to just the `hue_ent` Home Assistant add-on and extended with Hue Play Gradient Lightstrip per-segment support. The three other upstream apps (`frame_gallery`, `ledfx`, `local_faces`) are intentionally removed — the upstream repo remains the place for those.

The addon translates a stream of DDP pixel frames into the reverse-engineered Hue Entertainment Zigbee protocol, so a HyperHDR/piccap setup can drive Hue bulbs (and gradient strips) at ~25 fps over zigbee2mqtt — no Hue Bridge, no forked Z2M, no special coordinator firmware. Validated end-to-end against a Play Gradient Lightstrip 55" (LCX001) on a SMLIGHT CC2652 coordinator.

## Commands

Everything runs from the repo root:

- `python -m pytest hue_ent/tests` — the addon's own tests (protocol, registry, zone runner, gradient detection, stats).
- `python -m pytest` — all of the above plus `tests/test_repo_consistency.py`, which keeps manifests, the README version table, and the changelogs from drifting apart.
- `python -m pytest hue_ent/tests/test_gradient.py::test_lcx001_detected_as_gradient_with_7_segments -v` — one test.
- `ruff check .` / `ruff check --fix .` — lint (config in `ruff.toml`; line length 100).

No build step: the addon runs from source inside its Docker container in Home Assistant. Test deps (pytest, pyyaml, aiohttp, aiomqtt, numpy, opencv-python-headless) are pure-Python; nothing compiles.

## The pipeline this addon lives in

```
piccap / OBS  →  HyperHDR  →  DDP over UDP  →  hue_ent  →  MQTT zclcommand  →  zigbee2mqtt  →  Zigbee coordinator (SMLIGHT/CC2652)  →  Hue bulbs / gradient strip
```

`hue_ent` is the translator. It receives DDP frames on one UDP port per zone and emits `0xFC01`-cluster Zigbee frames aimed at a single "proxy" device per zone, which re-broadcasts to the rest.

## Wire format (cluster 0xFC01, manufacturer 0x100B / Signify)

Implemented in `hue_ent/app/protocol.py`. Three commands are used:

- **CMD 1 (stream)** — 6-byte header (counter LE u32 + smoothing LE u16) followed by N 7-byte records; sent to one proxy device per zone.
- **CMD 3 (sync/reset)** — 2 header bytes + counter LE u32; arms the sequence counter and doubles as clean-stop.
- **CMD 7 (segment_map)** — count BE u16 + N virtual addresses LE u16; sent once per arm to a gradient device to assign per-segment virtual addresses. Convention: `nwk, nwk+1, …, nwk+N-1`. Ordinary bulbs reply "Command Not Supported" — useful as a capability probe.

Each 7-byte record: address u16 LE, then a u16 LE with brightness (top 11 bits) + mode (bottom 5 bits), then 3 packed color bytes (12-bit CIE xy). `MODE_DEVICE = 0b01011` addresses a whole bulb by its real Zigbee nwk address; `MODE_SEGMENT = 0b00000` addresses one segment of a gradient device by its virtual address (assigned via CMD 7 at arm time). The 10-records-per-frame cap is a hard protocol limit: a 7-segment gradient strip + 3 bulbs fits; the 4th bulb overflows.

**When touching protocol code, the fasit is Bifrost's `hue::zigbee` unit tests** (`chrivers/bifrost` on GitHub — `crates/hue/src/zigbee/{entertainment,stream}.rs`). `hue_ent/tests/test_protocol.py` mirrors Bifrost's testvectors byte-for-byte on purpose: if a byte flips here, it flips against Bifrost too and something is genuinely wrong. `bifrost/doc/hue-zigbee-clusters.md` in that repo is the readable spec.

## Zone assembly flow

Reading order to understand where a zone comes from:

1. `main.py::parse_z2m_devices` — parses `zigbee2mqtt/bridge/devices`, returns `(nwk_map, lights_map)`. `lights_map` entries carry `{ieee, color, gradient, segments, model, zigbee_model}`. Segment counts come from `GRADIENT_SEGMENTS_BY_MODEL` (keyed on both the Zigbee `model_id` like `LCX001` and the Z2M `definition.model` article number like `929002422702`), with a 7-segment fallback for unknown gradient devices.
2. `registry.py::discover_rooms` — talks to HA's core registries over the Supervisor WebSocket proxy, folds area / device / entity registries into one "where does this light live" view (shape-tolerant on purpose — a single malformed registry entry must not sink the whole pass; see the `_walk_strings` / `_ieees` helpers and the github-issue-12 comment). `synthesize_rooms` groups color-capable Philips lights by area and enforces the **10 records** cap (`MAX_ZONE_RECORDS`).
3. `zonestore.py::ZoneStore.assemble` — merges auto-discovered rooms, saved sidebar-panel overrides (`/data/zones.json`), and manual `zones:` from the addon options. Manual zones win on slug collision. DDP ports are sticky per zone slug (base 4048) — adding a room later never renumbers existing zones, so LedFX devices don't churn.
4. `main.py::Bridge.rebuild_zones` — enriches each cfg with `light_meta` from `z2m_lights` (so `Zone` learns segment counts), constructs `Zone` + `ZoneRunner`, binds one UDP socket per zone.
5. `main.py::Zone` — validates, computes `pixel_count = sum(segments)`, tracks per-light segment counts.

## Streaming lifecycle

- **Arm** (`ZoneRunner._arm_ritual`) — stop-all with CMD 3, then per light: attribute write `0x0005 = 0xFE`, sequence sync, and (only for gradient devices) `segment_map_payload([nwk+i for i in range(segs)])`. Bulbs enter entertainment mode.
- **Stream** (`_run_ticker`) — 20–25 fps ticker; each tick reads the newest DDP frame (latest-wins), expands each light into 1 record (bulb) or N records (gradient) with the right mode + virtual address, and publishes one Zigbee frame to the proxy. Keepalive re-send every 4s even without change, since bulbs drop out of entertainment mode after a few silent seconds. A send-gap greater than 6s triggers a re-arm ritual.
- **Disarm** — send a black frame, per-light sync, restore each bulb to the state captured at arm time, unpause `pause_entities`.
- **Auto-arm** on incoming DDP; **auto-disarm** after `idle_timeout_s` (default 30s) without frames or from arm-time if nothing ever arrived. A non-loopback DDP source cancels the LedFX auto-provisioning task via `Bridge.notify_external_ddp` — that's the HyperHDR / piccap / remote-LedFX escape hatch that prevents 30-second retry-spam in the log.

## Repo conventions

- **Comments** in this codebase carry non-obvious *why* (firmware quirks, hidden protocol constraints, workarounds tied to a specific issue). Read them before editing near them; don't strip them. Don't add descriptive comments for what the code obviously does. The existing style is dense but each comment earns its place — match that.
- **Tests live next to the code they cover** (`hue_ent/tests/`) plus the repo-wide `tests/test_repo_consistency.py`. Running `pytest` from root runs both.
- **Version bumps** trigger the consistency tests. To bump `hue_ent`, update all four: `hue_ent/config.yaml` `version`, the row in `README.md`'s version table, a new `## X.Y.Z — YYYY-MM-DD` heading at the top of `hue_ent/CHANGELOG.md`, and a matching `## Hue Entertainment X.Y.Z — YYYY-MM-DD` heading at the top of the root `CHANGELOG.md`. Skip bumps for personal-fork iteration (the fork stays at the upstream version until we cut a real release), but do bump when handing something to another human via the HA store's "update available" badge.
- **Attribution on commits**: end each commit body with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` (the session-specific system-reminder is the source of truth for the exact line).

## Fork-specific extensions (over upstream v0.4.0)

- `MODE_SEGMENT` + `segment_map_payload` in `protocol.py`; `light_record()` grew a `mode=MODE_DEVICE` default parameter (backwards-compatible with existing callers).
- Gradient detection in `parse_z2m_devices` (looks for a top-level `type: list` expose named `gradient`); per-light `segments` field carried through the pipeline.
- Record-count cap (`MAX_RECORDS_PER_FRAME = 10`, aliased as `MAX_LIGHTS_PER_FRAME` for back-compat) replaces the old light-count cap; `synthesize_rooms` skips overflow with a `(zone full)` note.
- Per-segment record expansion in `_send_frame` / `_send_black`; pixel index walks the DDP frame so segments consume consecutive DDP pixels (gradient renders correctly).
- Auto-skip of LedFX provisioning when a non-loopback DDP source appears (HyperHDR / piccap use case).
- 3-preset "Livlighet" UI in `static/index.html` (Rolig / Medium / Livlig → `brightness_scale` 0.7 / 1.2 / 2.0) with slider retained for fine-tune.
- `brightness_scale` schema cap raised from 1.0 to 3.0 so >1.0 boost is reachable from the UI.
- HyperHDR quickstart + tuning section added to `hue_ent/DOCS.md`; other-DDP-senders section.

Reference implementations for cross-checking:
- **`chrivers/bifrost`** — Rust Hue-Bridge emulator; the canonical readable wire-format spec lives in `bifrost/doc/hue-zigbee-clusters.md` and unit tests in `crates/hue/src/zigbee/`.
- **`Hypfer/BambiHeavy`** — original C# reverse-engineering.
- **`zigbee2mqtt` discussion #5830** — where the protocol was first sketched.
