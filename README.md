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
- `Pillow==12.3.0`

## Configuration

No YAML needed—everything is done in the UI Config Flow.

### FarmBot Vision bridge options

Open the FarmBot integration entry and click **Configure** to reach the
options flow. FarmBot credentials are never asked for again here. Options:

| Option | Default | Purpose |
| --- | --- | --- |
| `vision_enabled` | off | Enables treating the bridge as active (informational; services are always registered, but enable this once you actually run the companion app) |
| `vision_heartbeat_timeout_minutes` | 10 | How long since the last `farmbot.report_vision_status` call before "FarmBot Vision Available" turns off |
| `allow_automatic_radius_increases` | off | Must be on for `farmbot.apply_vision_radius` to actually write a radius change, even when `apply: true` is passed |
| `allow_vision_curve_writes` | off | Must be on for `farmbot.upsert_vision_spread_curve` to run at all |
| `maximum_plant_radius_mm` | 500 | Hard ceiling enforced independently of the app's recommendation |
| `minimum_automatic_confidence` | 0.90 | `farmbot.apply_vision_radius` rejects an `apply: true` write whose `confidence` is below this, even if everything else validates |

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
- Propose a plant-radius change, either as a dry-run or an actual write
  (`farmbot.apply_vision_radius`)
- Create/update a FarmBot Vision-owned spread curve and assign it to plants,
  if curve writes are explicitly enabled (`farmbot.upsert_vision_spread_curve`)
- Report its own status/heartbeat for display on Home Assistant entities
  (`farmbot.report_vision_status`)
- Ask Home Assistant to fire a `farmbot_vision_request` event that an
  automation can relay to the app (`farmbot.request_vision_analysis`)

### Safety model

There are two independent layers:

1. The FarmBot Vision app decides whether it *recommends* a change.
2. This integration independently re-validates whether the change is
   *permitted* -- re-fetching the plant/curve from FarmBot itself and
   never trusting a caller-supplied "current" value, unit, plant ID, or
   confidence score at face value.

Consequences of that:

- A stale `expected_current_radius_mm` (someone else already changed the
  plant) is rejected as a `conflict`, not silently overwritten.
- Automatic radius **shrinking** is not implemented in this release, full
  stop -- it is rejected regardless of options.
- Automatic radius **increases** require `allow_automatic_radius_increases`,
  and the reported `confidence` must meet `minimum_automatic_confidence`.
- Curve writes require `allow_vision_curve_writes`, and only curves whose
  name starts with `[FarmBot Vision]` can ever be modified -- a
  user-created curve is never touched.
- Every plant/curve write is re-checked against the FarmBot device_id of
  the config entry the call was made against; a plant belonging to a
  different bot is always rejected.

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
- `farmbot.request_vision_analysis` only fires a Home Assistant event; it
  does not connect to the Vision app's container directly. Wire an
  automation to that event (or to the **FarmBot Analyse Plant Radii**
  button, which fires the same event) to actually kick off analysis.

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

This bridge's service/event contract is documented as version `1.1.0` of
this integration. A companion FarmBot Vision app targeting this bridge
should declare a minimum required integration version of **1.1.0**.

## License

This project is licensed under the MIT License. See LICENSE.
