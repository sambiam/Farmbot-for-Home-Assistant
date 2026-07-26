# Home Assistant FarmBot Integration

A custom integration to control your FarmBot via MQTT & HTTP API, and a secure
bridge to a separate **FarmBot Vision** companion app.

> **Prerelease note:** Version 1.1.0 is a prerelease intended for testing with
> Home Assistant Core **2026.7.x**. It has not been validated against other
> Home Assistant versions, so broader compatibility is not currently claimed.

## Features
- Exposes peripherals (rotary tool, lighting, vacuum, water, reverse, …) as switches
- Fetches & lists sequences in a `select` dropdown
- Secure MQTT connection using your FarmBot credentials
- `farmbot.execute_sequence` and `farmbot.move_to` services for scripts/automations
- A FarmBot Vision bridge: services, events and entities that let a separate
  computer-vision app read FarmBot plant/image/curve data and propose
  plant-radius or spread-curve changes, without ever receiving your FarmBot
  credentials

## Installation

1. Copy the `farmbot` folder into `<config>/custom_components/`.
2. Restart Home Assistant.
3. In Settings → Integrations, click ➕ and add **FarmBot**.
4. Enter your FarmBot email/password; the integration fetches your token & bot ID automatically.

Each FarmBot account/device can only be added once — attempting to add the
same FarmBot a second time will abort with "Already configured".

## Dependencies

Home Assistant installs these automatically from `manifest.json`; no manual
`pip install` is needed.

- `requests==2.34.2`
- `paho-mqtt==2.1.0`

**Pillow is intentionally not a manifest requirement.** Home Assistant Core
already provides Pillow and pins it in its own `package_constraints.txt`
(`Pillow==12.2.0` on Core 2026.7.x). If a custom integration *also* declares
a Pillow pin, Core resolves the manifest requirements against that constraint
file; a mismatched pin (the earlier `Pillow==12.3.0`) is unsatisfiable and
setup fails with:

```
Setup failed for custom integration 'farmbot': Requirements for farmbot not found: ['Pillow==12.3.0'].
```

The integration therefore imports `PIL` from the Core-provided Pillow and
declares no Pillow requirement of its own. See the CHANGELOG for the full
root-cause write-up and migration notes.

## Configuration

No YAML needed—everything is done in the UI Config Flow.

### FarmBot Vision bridge options

Open the FarmBot integration entry and click **Configure** to reach the
options flow. FarmBot credentials are never asked for again here. Options:

| Option | Default | Purpose |
| --- | --- | --- |
| `vision_enabled` | off | Enables treating the bridge as active (informational; services are always registered, but enable this once you actually run the companion app) |
| `vision_heartbeat_timeout_minutes` | 10 | How long since the last `farmbot.report_vision_status` call before "FarmBot Vision Available" turns off |

Everything else -- whether to write automatically, radius/confidence
thresholds, curve-write permission -- is configured in the FarmBot Vision
app itself, which already governs every write it asks this integration to
make via `apply`/`human_approved`. Keeping those settings in one place (the
app) avoids the two config surfaces silently disagreeing with each other.

Changing these options takes effect immediately on the next service call —
no reload of the FarmBot integration entry is required, because the option
values are read fresh from the config entry each time a service runs.

## FarmBot Vision bridge

The FarmBot Vision app is a **separate** project (not included in, or
installed by, this repository) that performs computer vision over FarmBot
camera images. This integration is the only thing that ever touches FarmBot
credentials (email/password, JWT, MQTT username/password); the Vision app
only ever talks to Home Assistant's service/event API.

### What the Vision app can do through this bridge

- List loaded FarmBot bots (`farmbot.list_vision_bots`)
- Read a snapshot of active plants, recently processed image metadata, and
  relevant spread curves (`farmbot.get_vision_inventory`)
- Download one resized, EXIF-corrected JPEG at a time
  (`farmbot.get_vision_image`)
- Inventory recognized soil-height points, run acknowledged safe-motion
  virtual-stereo captures, inspect their status, and apply a reviewed Z value
  (`farmbot.get_vision_soil_points`, `farmbot.start_vision_soil_capture`,
  `farmbot.get_vision_soil_capture`, `farmbot.apply_vision_soil_height`)
- Propose a plant-radius change, either as a dry-run or an actual write
  (`farmbot.apply_vision_radius`)
- Create/update a FarmBot Vision-owned spread curve and assign it to plants
  (`farmbot.upsert_vision_spread_curve`)
- Report its own status/heartbeat for display on Home Assistant entities
  (`farmbot.report_vision_status`)
- Automatically fire a targeted `farmbot_vision_request` when a newly uploaded
  FarmBot photo finishes processing, or fire one manually with
  `farmbot.request_vision_analysis`

### Safety model

Policy -- whether to write automatically, radius/confidence thresholds,
growth caps, curve-write permission -- lives entirely in the FarmBot Vision
app's own settings. The app decides both *whether* to recommend a change and
*whether* to apply it (`apply`/`human_approved`); this integration trusts
that decision.

What the integration still does independently, regardless of app settings,
is re-verify plant/curve *identity and freshness* against FarmBot itself --
never trusting a caller-supplied "current" value, unit or plant ID at face
value:

- A stale `expected_current_radius_mm` (someone else already changed the
  plant) is rejected as a `conflict`, not silently overwritten.
- Only curves whose name starts with `[FarmBot Vision]` can ever be
  modified -- a user-created curve is never touched.
- Every plant/curve write is re-checked against the FarmBot device_id of
  the config entry the call was made against; a plant belonging to a
  different bot is always rejected.
- Soil-height writes require explicit human approval, recognized soil metadata,
  and an unchanged GenericPointer snapshot. Only its `z` field is patched.
- Soil captures refuse disconnected, busy, emergency-stopped, or out-of-bounds
  bots; use acknowledged safe-Z movement; and restore the initial position when
  possible. Stopping the app workflow never sends an emergency stop.

### `get_vision_image` response contract

`farmbot.get_vision_image` downloads one FarmBot image, corrects its EXIF
orientation, downsamples it (Lanczos, aspect-ratio preserved, never upscaled)
to fit inside the requested `max_width` x `max_height`, re-encodes it as JPEG,
and returns:

| Field | Meaning |
| --- | --- |
| `image_id` | FarmBot image ID requested |
| `content_type` | Always `image/jpeg` |
| `sha256` | SHA-256 of the **returned** JPEG bytes (decode `image_base64` and this hash must match) |
| `source_sha256` | *(optional)* SHA-256 of the original downloaded image bytes; never replaces `sha256` |
| `source_width` / `source_height` | Dimensions immediately after decode, **before** EXIF orientation |
| `oriented_width` / `oriented_height` | Dimensions **after** EXIF orientation, before resize (the calibration coordinate system) |
| `width` / `height` | Dimensions of the returned JPEG |
| `resize_scale_x` | `width / oriented_width` |
| `resize_scale_y` | `height / oriented_height` |
| `image_base64` | Base64 of the returned JPEG |
| `processed_calibration` | Camera calibration rescaled to the returned image, or `{"available": false, "basis": "processed_image"}` |
| `meta` | `{x, y, z, created_at}` from the FarmBot image record |

The default bounding box is 640 x 480; the app may request larger analysis
resolutions such as 960 x 720 or 1280 x 960 (each side is capped at a hard
maximum of 4096, so a native 2592 x 1944 frame is never returned unless a
future, explicitly bounded request asks for it). Only one image is decoded
per call, and all Pillow work runs in the executor.

Example response for a 2592 x 1944 source requested at `max_width: 960`,
`max_height: 720`:

```json
{
  "image_id": 456,
  "content_type": "image/jpeg",
  "sha256": "hash-of-returned-jpeg",
  "source_sha256": "hash-of-original-download",
  "source_width": 2592,
  "source_height": 1944,
  "oriented_width": 2592,
  "oriented_height": 1944,
  "width": 960,
  "height": 720,
  "resize_scale_x": 0.37037037,
  "resize_scale_y": 0.37037037,
  "image_base64": "base64-of-the-960x720-jpeg",
  "processed_calibration": {
    "available": true,
    "pixels_per_mm_x": 0.455,
    "pixels_per_mm_y": 0.455,
    "rotation_degrees": 0,
    "offset_x_mm": 0,
    "offset_y_mm": 0,
    "basis": "processed_image",
    "width": 960,
    "height": 720
  },
  "meta": { "x": 500.0, "y": 300.0, "z": 0.0, "created_at": "ISO-8601" }
}
```

### Camera calibration normalization

`farmbot.get_vision_inventory` returns `camera_calibration` in a normalized
form the companion app can consume directly, instead of FarmBot's raw
`CAMERA_CALIBRATION_*` Farmware env values. The raw field meanings were
verified against FarmBot's own plant-detection Farmware
([`plant_detection/P2C.py`](https://github.com/FarmBot-Labs/plant-detection)):

- `coord_scale` is **millimetres per pixel**, so `pixels_per_mm = 1 / coord_scale`
  (FarmBot uses a single isotropic scale, so x and y are equal).
- `center_pixel_location_x` / `_y` are the image-centre pixel coordinates in
  the native, EXIF-oriented capture, computed by FarmBot as `int(dimension / 2)`.
  The native reference dimensions are therefore `center_pixel_location * 2`.
- `total_rotation_angle` is in **degrees**, passed through unchanged (sign
  follows FarmBot's stored whole-image-rotation convention).
- `camera_offset_x` / `_y` are **millimetre** offsets from the bot (UTM)
  position to the camera centre, in FarmBot coordinates.

Normalized structure (`basis: "oriented_native_image"`):

```json
{
  "available": true,
  "pixels_per_mm_x": 1.23,
  "pixels_per_mm_y": 1.23,
  "rotation_degrees": 0.0,
  "offset_x_mm": 0.0,
  "offset_y_mm": 0.0,
  "reference_width": 2592,
  "reference_height": 1944,
  "basis": "oriented_native_image"
}
```

When any required value is missing, non-finite, or the native reference
dimensions cannot be derived, `camera_calibration` and `processed_calibration`
report `{"available": false}` rather than guessing — this preserves the
companion app's own manual-calibration fallback. `processed_calibration` is
only marked available when the returned image's *oriented* native dimensions
match the calibration's reference dimensions, so the app never has to guess
which coordinate system a calibration belongs to.

### Data update behaviour

- **Vision status entities** (`FarmBot Vision Status`, `Last Analysis`,
  `Recommendations`, `Uncertain Plants`, `Vision Available`) are never
  polled. They update only when `farmbot.report_vision_status` is called
  (a "heartbeat"), and `FarmBot Vision Available` additionally re-evaluates
  once a minute purely to notice when a heartbeat has aged past the
  configured timeout -- no FarmBot or Vision-app network traffic is
  involved in that check.
- `farmbot.get_vision_inventory`/`get_vision_image` are on-demand, synchronous
  reads: nothing is cached across calls.
- Newly processed FarmBot photos are detected from bounded metadata polling and
  automatically sent to the companion app as targeted analysis events. The
  **FarmBot Analyse Plant Radii** button and `farmbot.request_vision_analysis`
  remain available for full-history/manual runs.

### Privacy and security

- FarmBot email, password, JWT and MQTT credentials never leave
  `custom_components/farmbot/`; no service response or log line includes
  them (`farmbot.get_vision_image`'s base64 payload is also never logged).
- Images are downloaded through FarmBot's authenticated API for metadata,
  but the actual image bytes come from a pre-signed, presumed public URL --
  no FarmBot bearer token is sent to that (third-party) storage host.
- The FarmBot API base URL is derived from the trusted JWT's issuer when
  present (supporting self-hosted FarmBot servers), but only accepted if it
  is `https` and not a private/loopback/link-local address, to avoid a
  malformed token redirecting requests to an internal host.
- All FarmBot HTTP calls go through one client
  (`custom_components/farmbot/api.py`) with request timeouts, response-size
  limits, bounded retries (GET only, never for validation/auth failures),
  and rate-limited error logging.
- Image decoding/resizing uses Pillow only, runs in the executor (never
  blocking the event loop), and never processes more than one image at a
  time.

### Minimum FarmBot Vision app version

The soil-height bridge requires integration version **1.7.0**. It adds the
four typed soil services and FarmBot RPC acknowledgement handling while
retaining the existing image contract. Earlier app features remain compatible
with older integrations.

The `1.2.0` `get_vision_image` response added
`source_sha256`, `source_width`/`source_height`, `oriented_width`/
`oriented_height`, `resize_scale_x`/`resize_scale_y`, and
`processed_calibration` as **optional, additive** fields; the `1.1.0` fields
(`image_id`, `content_type`, `sha256`, `width`, `height`, `image_base64`,
`meta`) are unchanged, so a `1.1.0`-era app keeps working. The one behavioural
change is that `sha256` now hashes the returned JPEG (as it always should
have) rather than the original download; an app that verified `sha256`
against the base64 payload now succeeds where it previously would have
mismatched. A companion app that consumes the new fields should declare a
minimum required integration version of **1.2.0**; soil-height acquisition
requires **1.7.0**.

## License

This project is licensed under the MIT License. See LICENSE.
