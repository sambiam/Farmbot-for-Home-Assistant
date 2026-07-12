# Home Assistant FarmBot Integration

A custom integration to control your FarmBot via MQTT & HTTP API.

> **Prerelease note:** Version 1.0.1 is a prerelease intended for testing with
> Home Assistant Core **2026.7.x**. It has not been validated against other
> Home Assistant versions, so broader compatibility is not currently claimed.

## Features
- Exposes peripherals (rotary tool, lighting, vacuum, water, reverse, …) as switches  
- Fetches & lists sequences in a `select` dropdown  
- Secure MQTT connection using your FarmBot credentials
- `farmbot.execute_sequence` and `farmbot.move_to` services for scripts/automations

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

## Configuration

No YAML needed—everything is done in the UI Config Flow.

## License

This project is licensed under the MIT License. See LICENSE.
