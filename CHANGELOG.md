# Changelog

All notable changes to this integration are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

## 1.2.0 - Contract farmbot-vision-v2 (high-resolution image contract)

### Added

- `farmbot.get_vision_image` now returns the contract v2 fields: `source_width`/
  `source_height` (before EXIF orientation), `oriented_width`/`oriented_height`
  (after), `resize_scale_x`/`resize_scale_y`, an optional `source_sha256` over
  the original download, and an optional `processed_calibration` (basis
  `processed_image`) scaled to the exact returned pixels.
- `farmbot.get_vision_inventory` now maps FarmBot's farmware camera calibration
  into the app's reference shape: `pixels_per_mm_x/y` (from `coord_scale`),
  `rotation_degrees`, offsets, and `reference_width`/`reference_height`/`basis`
  derived from the calibration's centre-pixel location. Missing or non-positive
  core values report `available: false` rather than a guessed calibration.
- `image_utils.process_image` records source/oriented dimensions and resize
  scales alongside the JPEG.
- Raised the default image request size to 960×720 (max 1280×960 unchanged).
- Minimum compatible FarmBot Vision app version raised to 0.2.0.

### Changed

- `sha256` in the image response is now computed over the **returned** JPEG
  bytes (so the app can verify what it receives); the original download hash is
  exposed separately as `source_sha256`.

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
