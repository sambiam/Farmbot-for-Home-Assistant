"""Constants for the FarmBot integration, including the FarmBot Vision bridge."""

DOMAIN = "farmbot"
API_BASE_URL = "https://my.farm.bot/api"
MQTT_PORT = 8883

# MQTT topic templates
TOPIC_STATUS  = "bot/{device_id}/status"
TOPIC_COMMAND = "bot/{device_id}/from_clients"
TOPIC_LOGS    = "bot/{device_id}/logs"

# Dispatcher signals
SIGNAL_STATE = "farmbot_state_update"
SIGNAL_VISION_STATE = "farmbot_vision_state_update"
SIGNAL_SEQUENCE_SELECTED = "farmbot_sequence_selected"

# Token refresh settings
TOKEN_REFRESH_WINDOW = 7 * 24 * 60 * 60  # 7 days in seconds
TOKEN_REFRESH_INTERVAL = 6 * 60 * 60  # Check every 6 hours

# Poll FarmBot's image metadata frequently enough for a completed camera upload
# to become a vision request without depending on a particular log message or
# firmware version. Only metadata is fetched here; JPEG bytes remain on-demand.
VISION_IMAGE_POLL_INTERVAL_SECONDS = 15

# --------------------------------------------------------------------------
# FarmBot Vision bridge
# --------------------------------------------------------------------------

MIN_VISION_APP_VERSION = "0.2.0"

# Service names (existing)
SERVICE_EXECUTE_SEQUENCE = "execute_sequence"
SERVICE_MOVE_TO = "move_to"

# Service names (FarmBot Vision bridge)
SERVICE_LIST_VISION_BOTS = "list_vision_bots"
SERVICE_GET_VISION_INVENTORY = "get_vision_inventory"
SERVICE_GET_VISION_IMAGE = "get_vision_image"
SERVICE_APPLY_VISION_RADIUS = "apply_vision_radius"
SERVICE_APPLY_VISION_REMOVAL = "apply_vision_removal"
SERVICE_UPSERT_VISION_SPREAD_CURVE = "upsert_vision_spread_curve"
SERVICE_REPORT_VISION_STATUS = "report_vision_status"
SERVICE_REQUEST_VISION_ANALYSIS = "request_vision_analysis"

# Home Assistant event fired for farmbot.request_vision_analysis
EVENT_VISION_REQUEST = "farmbot_vision_request"

# Integration options (OptionsFlow) - keys and defaults
OPTION_VISION_ENABLED = "vision_enabled"
OPTION_VISION_HEARTBEAT_TIMEOUT_MINUTES = "vision_heartbeat_timeout_minutes"
OPTION_ALLOW_AUTOMATIC_RADIUS_INCREASES = "allow_automatic_radius_increases"
OPTION_ALLOW_AUTOMATIC_PLANT_REMOVAL = "allow_automatic_plant_removal"
OPTION_ALLOW_VISION_CURVE_WRITES = "allow_vision_curve_writes"
OPTION_MAXIMUM_PLANT_RADIUS_MM = "maximum_plant_radius_mm"
OPTION_MINIMUM_AUTOMATIC_CONFIDENCE = "minimum_automatic_confidence"

DEFAULT_VISION_ENABLED = False
DEFAULT_VISION_HEARTBEAT_TIMEOUT_MINUTES = 10
DEFAULT_ALLOW_AUTOMATIC_RADIUS_INCREASES = False
DEFAULT_ALLOW_AUTOMATIC_PLANT_REMOVAL = False
DEFAULT_ALLOW_VISION_CURVE_WRITES = False
DEFAULT_MAXIMUM_PLANT_RADIUS_MM = 500
DEFAULT_MINIMUM_AUTOMATIC_CONFIDENCE = 0.90

# FarmBot point/plant filtering
POINTER_TYPE_PLANT = "Plant"
ACTIVE_PLANT_STAGES = {"planted", "sprouted", "active"}

# FarmBot Vision-owned spread curves
CURVE_NAME_PREFIX = "[FarmBot Vision]"
VISION_CURVE_TYPE = "spread"
MAX_CURVE_CONTROL_POINTS = 10

# Radius validation
RADIUS_TOLERANCE_MM = 0.5

# Analysis request modes for farmbot.request_vision_analysis
VISION_ANALYSIS_MODES = ("observe", "recommend", "auto_radius")

# Vision status sensor values
VISION_STATUS_VALUES = ("unavailable", "idle", "running", "warning", "error")

# Image lookback bounds for farmbot.get_vision_inventory
DEFAULT_IMAGE_LOOKBACK_HOURS = 72
MAX_IMAGE_LOOKBACK_HOURS = 24 * 14  # two weeks

# Image resize defaults for farmbot.get_vision_image
DEFAULT_IMAGE_MAX_WIDTH = 640
DEFAULT_IMAGE_MAX_HEIGHT = 480
# Hard upper bound on a requested output bounding-box side. Comfortably above
# the analysis resolutions the FarmBot Vision app asks for (640x480, 960x720,
# 1280x960) and the native FarmBot camera (2592x1944), but still bounded so a
# caller can never request an arbitrarily large re-encode.
MAX_IMAGE_DIMENSION = 4096

# Decompression-bomb guards applied to the *decoded* source image, before any
# resize. A native FarmBot frame is 2592x1944 (~5 MP); these limits leave
# generous headroom for larger cameras while rejecting images whose pixel
# geometry would blow up memory regardless of how small the compressed file is.
MAX_SOURCE_IMAGE_DIMENSION = 12000
MAX_SOURCE_IMAGE_PIXELS = 60_000_000  # 60 MP

# HTTP client limits/behaviour (custom_components/farmbot/api.py)
HTTP_TIMEOUT_SECONDS = 15
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 0.5
MAX_JSON_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_IMAGE_DOWNLOAD_BYTES = 15 * 1024 * 1024  # 15 MB
AUTH_FAILURE_LOG_INTERVAL_SECONDS = 60
