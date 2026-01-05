DOMAIN = "farmbot"
API_BASE_URL = "https://my.farm.bot/api"
MQTT_PORT = 8883

# MQTT topic templates
TOPIC_STATUS  = "bot/{device_id}/status"
TOPIC_COMMAND = "bot/{device_id}/from_clients"
TOPIC_LOGS    = "bot/{device_id}/logs"

# Dispatcher signal
SIGNAL_STATE = "farmbot_state_update"

# Token refresh settings
TOKEN_REFRESH_WINDOW = 7 * 24 * 60 * 60  # 7 days in seconds
TOKEN_REFRESH_INTERVAL = 6 * 60 * 60  # Check every 6 hours
