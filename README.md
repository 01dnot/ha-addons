# Hue Entertainment (with gradient-strip support)

*A personal fork of Adam Oberley's `hue_ent` add-on, extended so a Hue Play
Gradient Lightstrip is driven per-segment (7 pixels) instead of as one colour —
all over zigbee2mqtt, no Hue Bridge.*

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Home Assistant app](https://img.shields.io/badge/Home%20Assistant-app-41BDF5.svg)](https://www.home-assistant.io/)

The three other apps from the upstream repo (REFRAMED Gallery, Local Faces,
LedFX) are not kept here — see the
[upstream repository](https://github.com/adamoberley/ha-addons) for those.

| App | What it does | Version |
| --- | --- | --- |
| **[Hue Entertainment](hue_ent/DOCS.md)** | Stream LedFX/HyperHDR effects to **Philips Hue Zigbee bulbs** on zigbee2mqtt at 20–25 fps — no Hue Bridge. Fork adds Hue Play Gradient Lightstrip per-segment addressing. | `0.4.0` |

## Install

1. **Settings → Add-ons → App Store → ⋮ (top-right) → Repositories** and paste
   this fork's URL.
2. **Hue Entertainment** appears under the store. Install, **Start**, and open
   its sidebar panel.

---

## Hue Entertainment

*Audio-reactive **Philips Hue** — driven by LedFX, straight over zigbee2mqtt, no
Hue Bridge.*

Most Hue setups get audio-reactive lighting only through a Hue Bridge and the
Entertainment API. If your Hue bulbs are paired to **zigbee2mqtt** instead, the
obvious path — one MQTT `set` per bulb per frame — tops out around **6 fps** and
floods the Zigbee queue, so it's unusable. This app makes it actually work: it
takes LedFX's pixel output and speaks the reverse-engineered **Hue Entertainment**
Zigbee protocol through Z2M, hitting a smooth **20–25 fps** with no Bridge and no
special coordinator firmware.

- **The fast path Hue uses itself** — one compact Zigbee frame per zone per tick,
  unicast to a single *proxy* bulb that re-broadcasts to the rest. Bulbs
  **interpolate** between targets (the same trick a real Hue Bridge uses), so
  20–25 fps looks continuous. That's ~4× the naive rate, without the queue flood.
- **Stock Z2M, no Bridge** — rides zigbee2mqtt's generic `zclcommand` passthrough
  (needs Z2M **2.1.1+**); no forked Z2M, no exotic radio firmware. Verified on a
  plain TI CC2652.
- **Per-room zones** — up to 10 bulbs each (the Zigbee frame limit). Each zone gets
  its own DDP input and target frame rate.
- **Zones build themselves** — color Hue bulbs are grouped by HA area into one
  zone per room (Adaptive Lighting switch auto-detected too); a **sidebar panel**
  handles the human parts: pixel order with a per-bulb *Blink* identifier, proxy
  choice, brightness, and per-room enable toggles. Edits apply live.
- **HA-native & tidy** — one **Hue Entertainment \<zone\>** switch per zone via MQTT
  discovery. Arming captures each bulb's state and **pauses your Adaptive Lighting**
  for that room; disarming **restores every bulb** to exactly where it was. Auto-arms
  when LedFX starts sending and releases the lights when the music stops — and *off
  means off* if you kill a zone by hand mid-song.
- **Feeds straight from LedFX, zero mirroring** — it auto-creates one matching
  DDP device per zone in the LedFX app (also in this repo) through LedFX's API;
  you just pick effects.

> **Heads up:** this rides a reverse-engineered protocol, so treat it as
> experimental. It's been validated end-to-end on real hardware, but firmware and
> Z2M updates could move things.

→ Full setup, zones, and LedFX wiring: [`hue_ent/DOCS.md`](hue_ent/DOCS.md)

---

## Repo layout & development

The app lives in `hue_ent/` — its own `Dockerfile`, `config.yaml` (manifest +
version + options schema), `DOCS.md`, and `CHANGELOG.md` (Home Assistant shows
that one in the store). Repo-wide notes are collected in
[`CHANGELOG.md`](CHANGELOG.md).

Python is linted with [ruff](https://docs.astral.sh/ruff/) (`ruff.toml`) and
tested with pytest — from the repo root:

```bash
python -m pytest && ruff check .
```

Tests live next to what they cover (`hue_ent/tests/`, `local_faces/tests/`) plus
`tests/test_repo_consistency.py`, which keeps the manifests, this README's
version table, and the changelogs from drifting apart. They need only the pure
Python deps (`pytest pytest-asyncio pyyaml aiohttp aiomqtt requests numpy
opencv-python-headless`) — nothing compiles. Both run in CI
([`.github/workflows/ci.yaml`](.github/workflows/ci.yaml)) on every push and
pull request.

**Releasing:** bump `hue_ent/config.yaml` version, add the entry to both
changelogs (the consistency tests check both), then tag `hue_ent-v<version>`
and write the GitHub release from the app's changelog entry.

## Credits & license

Code: **MIT** (see [LICENSE](LICENSE)). Upstream app by
[adamoberley/ha-addons](https://github.com/adamoberley/ha-addons); this fork
adds Hue Play Gradient Lightstrip segment support.

**Hue Entertainment** builds on the reverse-engineering of Hue's Zigbee
Entertainment protocol by [Hypfer/BambiHeavy](https://github.com/Hypfer/BambiHeavy)
and [chrivers/bifrost](https://github.com/chrivers/bifrost), and the
[zigbee2mqtt discussion](https://github.com/Koenkk/zigbee2mqtt/discussions/5830)
that started it. Gradient segment wire format cross-checked against Bifrost's
`hue::zigbee` unit tests. Drives bulbs through stock
[zigbee2mqtt](https://github.com/Koenkk/zigbee2mqtt).

Not affiliated with Philips/Signify, the Home Assistant project, or the
zigbee2mqtt project.
