# Changelog

All notable changes to this integration are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

## 1.3.2 - Ship the Vision status fix to stable

### Fixed

- **FarmBot Vision status entities still stuck for stable (non-beta) users.**
  - **Root cause:** the `report_vision_status` schema fix (accepting the
    nullable `job_id` / `last_completed_at` the companion app sends) landed in
    the `1.3.1` build, but that build was only published as a *prerelease*.
    HACS hides prereleases by default, so instances tracking stable releases
    stayed on the older build whose schema rejects every status report with an
    HTTP 400 — leaving Vision Available on `Disconnected`, Vision Status on
    `Unavailable`, and no state history. Separately, `manifest.json` had never
    been bumped past `1.2.0`, so the manifest version no longer tracked the
    released tags.
  - **Fix:** `manifest.json` version is bumped to `1.3.2` and this is published
    as a stable (non-prerelease) release so HACS delivers the
    `report_vision_status` fix to every user without requiring the beta channel.
    No integration logic changed relative to `1.3.1`.

## 1.2.0 - Vision setup fix & high-resolution image handling

### Fixed

- **FarmBot Vision status entities never updated** (Vision Available stuck on
  `Disconnected`, Vision Status on `Unavailable`, zero state history).
  - **Root cause:** the `farmbot.report_vision_status` service schema declared
    the nullable `job_id` and `last_completed_at` fields with a bare
    `cv.string`. The `farmbot-vision-v2` companion app sends `job_id: null` on
    every idle heartbeat and `last_completed_at: null` while a job is running,
    but `cv.string` rejects `None`, so voluptuous raised and Home Assistant
    returned HTTP 400 for **every** report — including the very first one. The
    handler never ran, so no entity ever received an update. (The read path —
    `list_vision_bots` / `get_vision_inventory` / `get_vision_image` — was
    unaffected, which is why inventory and image loading worked.)
  - **Fix:** `job_id`, `last_completed_at` and `app_version` now accept `None`
    (`vol.Any(None, cv.string)`), and `message` is capped at the contract's 240
    characters. A regression test drives `report_vision_status` twice with the
    real null-bearing payloads and asserts the entities refresh on both calls.

- **Integration setup failure** `Requirements for farmbot not found:
  ['Pillow==12.3.0']`.
  - **Root cause:** Home Assistant Core already provides Pillow and pins it in
    its own `homeassistant/package_constraints.txt` (`Pillow==12.2.0` on Core
    2026.7.x, which also requires Python >= 3.14.2). When a custom integration
    *also* declares a Pillow requirement, Core installs the manifest
    requirements with that constraint file applied; the manifest's
    `Pillow==12.3.0` is unsatisfiable against the `Pillow==12.2.0` constraint,
    so the install resolves to nothing and setup fails. A compatible prebuilt
    aarch64 wheel for Pillow 12.2.0 exists, so this was never a
    compile/architecture problem — purely a version-pin conflict with Core.
  - **Fix:** Pillow is removed from `manifest.json`; the integration now
    imports `PIL` from the Core-provided Pillow. The manifest declares only
    `requests` and `paho-mqtt`, which resolve cleanly under Core's constraints.

### Changed

- `farmbot.get_vision_image` now returns full image-scaling metadata and a
  corrected checksum contract (all additions are backward compatible; the
  1.1.0 fields `image_id`, `content_type`, `sha256`, `width`, `height`,
  `image_base64`, `meta` are unchanged):
  - `sha256` is now the hash of the **returned** JPEG bytes (previously it
    hashed the original download while returning a resized/re-encoded image, so
    an app verifying the base64 payload against `sha256` could never match).
  - New optional `source_sha256` covers the original download and never
    replaces `sha256`.
  - New `source_width`/`source_height` (post-decode, pre-EXIF),
    `oriented_width`/`oriented_height` (post-EXIF, pre-resize),
    `width`/`height` (returned JPEG), and `resize_scale_x`/`resize_scale_y`
    (`width / oriented_width`, `height / oriented_height`).
  - New `processed_calibration` — FarmBot camera calibration rescaled onto the
    exact returned image, or `{"available": false, "basis": "processed_image"}`.
- `farmbot.get_vision_inventory`'s `camera_calibration` is now **normalized**
  (`pixels_per_mm_x/y`, `rotation_degrees`, `offset_x_mm/y`,
  `reference_width/height`, `basis: "oriented_native_image"`) instead of raw
  `CAMERA_CALIBRATION_*` Farmware values. `coord_scale` (mm/pixel) is inverted
  to pixels-per-mm; native reference dimensions are derived from
  `center_pixel_location * 2`; meanings were verified against FarmBot's
  plant-detection Farmware (`plant_detection/P2C.py`). Ambiguous or unverifiable
  calibration returns `{"available": false}` rather than a guess, preserving
  the companion app's manual-calibration fallback.
- Image handling hardened for high-resolution frames: decompression-bomb guards
  on decoded dimensions (`MAX_SOURCE_IMAGE_DIMENSION`) and pixel count
  (`MAX_SOURCE_IMAGE_PIXELS`) applied before full decode; image resources and
  `BytesIO` buffers are closed promptly; Lanczos downsampling; still Pillow-only
  and still one image per call, decoded in the executor. Requested output boxes
  up to 4096 per side are supported (e.g. 640x480, 960x720, 1280x960); a native
  2592x1944 frame is never returned unless explicitly requested within that
  bound.

### Added

- Dependency-contract regression tests (`tests/test_dependencies.py`): the
  manifest must not declare Pillow, and `from PIL import Image, ImageOps` must
  import.
- Calibration normalization/rescaling tests (`tests/test_vision_calibration.py`).
- Expanded image tests: native-frame resizes to 640x480 / 960x720 / 1280x960,
  non-4:3 fitting, no-upscale, EXIF width/height swap, source/oriented/processed
  dimensions, resize scales, checksum-of-returned-JPEG, malformed and
  oversized/decompression-bomb rejection.
- New CI job **Requirements install (target HA env)**: on Python 3.14 it
  installs the manifest requirements under Core's real
  `package_constraints.txt`, installs the Core-provided Pillow, verifies
  `from PIL import Image, ImageOps`, and confirms prebuilt aarch64 wheels exist
  (no source compile on HA OS). The `pytest` job also runs an explicit
  returned-JPEG checksum-contract guard.

### Migration notes

- **No manual `pip install` and no `/config/deps` editing is expected.** After
  updating, restart Home Assistant. Core installs the corrected requirements on
  the next start and the integration loads.
- If a previous failed load left a stale `Pillow==12.3.0` under
  `/config/deps`, it is harmless (Core's own `Pillow==12.2.0` takes
  precedence), but you may delete `config/deps/**/Pillow*` /
  `config/deps/**/PIL*` and restart if you want a clean tree. This is optional
  cleanup, not a required step.
- A **full Home Assistant restart** is required for the manifest change to take
  effect — reloading the config entry alone does not re-run requirement
  installation.
- Verify the loaded version in **Settings → Devices & Services → FarmBot →
  (⋮) → integration info**, or in **Settings → System → Repairs → System
  information**; the manifest version reads **1.2.0**.

## 1.1.0 - FarmBot Vision bridge

### Added

- A secure bridge to a separate, not-included FarmBot Vision computer-vision
  app. This integration remains the only place FarmBot credentials
  (email/password, JWT, MQTT credentials) are stored or used -- the app never
  receives them.
- New async FarmBot REST client (`custom_components/farmbot/api.py`) used for
  all Vision-bridge HTTP calls: centralised auth headers, timeouts,
  bounded retries (idempotent GET only), response-size limits, JSON
  validation, and rate-limited error logging. Resolves the FarmBot API host
  from the JWT issuer when present (for self-hosted FarmBot servers), with
  SSRF guards against non-https or private/loopback issuers.
- Seven new services:
  - `farmbot.list_vision_bots`
  - `farmbot.get_vision_inventory`
  - `farmbot.get_vision_image`
  - `farmbot.apply_vision_radius`
  - `farmbot.upsert_vision_spread_curve`
  - `farmbot.report_vision_status`
  - `farmbot.request_vision_analysis`
- A `farmbot_vision_request` Home Assistant event, fired by
  `farmbot.request_vision_analysis` and by the new **FarmBot Analyse Plant
  Radii** button.
- New entities: `binary_sensor.*_vision_available`, `sensor.*_vision_status`,
  `sensor.*_vision_last_analysis`, `sensor.*_vision_recommendations`,
  `sensor.*_vision_uncertain_plants`, `button.*_vision_analyse_plant_radii`.
  All are dispatch-driven (no polling); vision availability is derived from
  heartbeat recency, never from the app's self-reported status alone.
- An options flow (Settings → FarmBot → Configure) exposing
  `vision_enabled`, `vision_heartbeat_timeout_minutes`,
  `allow_automatic_radius_increases`, `allow_vision_curve_writes`,
  `maximum_plant_radius_mm`, and `minimum_automatic_confidence`. No FarmBot
  credentials are re-entered here, and options take effect on the next
  service call without reloading the config entry.
- `custom_components/farmbot/vision.py`: pure, unit-tested validation and
  projection helpers (plant filtering by pointer_type/stage, image
  lookback/processing-state filtering, spread-curve ownership/monotonicity
  validation, plant-radius change validation, and explicit
  radius<->diameter unit conversion so the two millimetre quantities are
  never confused).
- `custom_components/farmbot/image_utils.py`: Pillow-only decode/EXIF-orient/
  resize/JPEG-encode helper, always run via the executor.
- `Pillow` added as a manifest dependency for image processing.

### Changed

- `custom_components/farmbot/manager.py`: `FarmbotManager` now owns a
  `FarmbotApiClient` instance and FarmBot Vision runtime state (heartbeat,
  status, job ID, counters). A shared `_auth_failed` flag now also guards
  the new API-client reauth path, so a FarmBot auth failure detected by
  MQTT, token refresh, or the Vision bridge only ever triggers reauth once.
- JWT payload decoding was extracted from `manager.py` into
  `custom_components/farmbot/jwt_util.py` (identical behaviour, shared with
  `api.py`).
- `custom_components/farmbot/button.py`: sequence-specific buttons (Mow
  Weeds, Water Plants) no longer silently disappear if fetching sequences
  fails; only those two buttons are skipped, so the FarmBot Vision button
  (which does not depend on sequences) is always present.

### Preserved (no behaviour change)

- Config flow, reauthentication, token refresh, MQTT connection and status
  updates, switches, coordinate sensors, existing buttons, sequence
  selection, `farmbot.execute_sequence`, `farmbot.move_to`,
  duplicate-device prevention, and all existing entity unique IDs.

### Migration notes

- No config-entry migration is required; existing entries keep working
  unchanged. `vision_enabled` and friends default to their safest settings
  (`false`) so no existing installation starts writing to FarmBot or
  exposing new write-capable services differently than before -- the new
  services are read-safe (dry-run) unless `apply: true` is explicitly
  passed **and** the corresponding option is enabled.
- Minimum compatible FarmBot Vision companion-app version: **1.1.0** of this
  integration's service/event contract (see README "FarmBot Vision bridge").
