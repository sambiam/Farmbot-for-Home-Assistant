# Home Assistant FarmBot Integration

A custom integration to control your FarmBot via MQTT & HTTP API.

## Features
- Exposes peripherals (rotary tool, lighting, vacuum, water, reverse, …) as switches  
- Fetches & lists sequences in a `select` dropdown  
- Secure MQTT connection using your FarmBot credentials

## Installation

1. Copy the `farmbot` folder into `<config>/custom_components/`.  
2. Restart Home Assistant.  
3. In Settings → Integrations, click ➕ and add **FarmBot**.  
4. Enter your FarmBot email/password; the integration fetches your token & bot ID automatically.

## Dependencies

- `requests>=2.0.0`  
- `paho-mqtt>=1.6.0`

## Configuration

No YAML needed—everything is done in the UI Config Flow.

## License

This project is licensed under the MIT License. See LICENSE.
