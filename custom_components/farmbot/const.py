DOMAIN = "farmbot"
API_BASE_URL = "https://my.farm.bot/api"
MQTT_PORT = 8883

# MQTT topic templates
TOPIC_STATUS  = "bot/{device_id}/status"
TOPIC_COMMAND = "bot/{device_id}/from_clients"
TOPIC_LOGS    = "bot/{device_id}/logs"

# Dispatcher signal
SIGNAL_STATE = "farmbot_state_update"
