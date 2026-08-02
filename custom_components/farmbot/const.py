"""Constants for the FarmBot integration, including the FarmBot Vision bridge."""

DOMAIN = "farmbot"
API_BASE_URL = "https://my.farm.bot/api"
MQTT_PORT = 8883

# MQTT topic templates
TOPIC_STATUS = "bot/{device_id}/status"
TOPIC_COMMAND = "bot/{device_id}/from_clients"
TOPIC_FROM_DEVICE = "bot/{device_id}/from_device"
TOPIC_LOGS = "bot/{device_id}/logs"

# Dispatcher signals
SIGNAL_STATE = "farmbot_state_update"
SIGNAL_BUTTON_INPUT = "farmbot_button_input_update"
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
INTEGRATION_VERSION = "2.7.0"
VISION_CAPABILITIES = [
    "photo_grid_repair",
    "verified_photo_grid_repair",
    "position_verified_photo_grid_repair",
    "illuminated_photo_grid_capture",
    "vision_image_deletion",
    # A whole bed grid fits in one call, so the run's lighting, its entry to
    # the grid and its return to the staging position happen exactly once
    # instead of once per twelve-cell chunk. See
    # GRID_REPAIR_MAX_TARGETS_PER_CALL.
    "continuous_photo_grid_capture",
    # Each target may carry a caller-owned `index`; every frame, completed
    # target and failed target is echoed back with it, so the caller tracks
    # cells by stable identity instead of by coordinate proximity.
    "indexed_photo_grid_targets",
    # EXPERIMENTAL. Raw firmware G-code, forwarded to the Farmduino through
    # FarmBot OS's Lua `gcode()` escape hatch. Bypasses FarmBot OS's motion
    # planning entirely, so this integration validates the whole program
    # itself before any of it is sent. See gcode.py.
    "experimental_raw_gcode",
    # Purpose-built adaptive rotary-tool mowing. Unlike raw G-code this accepts
    # only pre-validated straight cuts and keeps current recovery inside one
    # FarmBot OS Lua command so an overload can turn the tool off immediately.
    "adaptive_rotary_weeding",
]

# Service names (existing)
SERVICE_EXECUTE_SEQUENCE = "execute_sequence"
SERVICE_MOVE_TO = "move_to"

# Service names (FarmBot Vision bridge)
SERVICE_LIST_VISION_BOTS = "list_vision_bots"
SERVICE_GET_VISION_INVENTORY = "get_vision_inventory"
SERVICE_GET_VISION_IMAGE = "get_vision_image"
SERVICE_GET_VISION_SOIL_POINTS = "get_vision_soil_points"
SERVICE_START_VISION_SOIL_CAPTURE = "start_vision_soil_capture"
SERVICE_GET_VISION_SOIL_CAPTURE = "get_vision_soil_capture"
SERVICE_START_VISION_GRID_REPAIR = "start_vision_grid_repair"
SERVICE_GET_VISION_GRID_REPAIR = "get_vision_grid_repair"
SERVICE_DELETE_VISION_IMAGE = "delete_vision_image"
SERVICE_APPLY_VISION_SOIL_HEIGHT = "apply_vision_soil_height"
SERVICE_APPLY_VISION_RADIUS = "apply_vision_radius"
SERVICE_APPLY_VISION_REMOVAL = "apply_vision_removal"
SERVICE_APPLY_VISION_PLANT_CENTER = "apply_vision_plant_center"
SERVICE_CREATE_VISION_WEED = "create_vision_weed"
SERVICE_UPDATE_VISION_WEED_RADIUS = "update_vision_weed_radius"
SERVICE_REMOVE_VISION_WEED = "remove_vision_weed"
SERVICE_UPSERT_VISION_SPREAD_CURVE = "upsert_vision_spread_curve"
SERVICE_REPORT_VISION_STATUS = "report_vision_status"
SERVICE_REQUEST_VISION_ANALYSIS = "request_vision_analysis"
SERVICE_START_VISION_GCODE = "start_vision_gcode"
SERVICE_GET_VISION_GCODE = "get_vision_gcode"
SERVICE_START_VISION_WEEDING = "start_vision_weeding"
SERVICE_GET_VISION_WEEDING = "get_vision_weeding"

# Home Assistant event fired for farmbot.request_vision_analysis
EVENT_VISION_REQUEST = "farmbot_vision_request"
EVENT_BUTTON_INPUT = "farmbot_button_input"

# Integration options (OptionsFlow) - keys and defaults
#
# Policy limits (max radius, confidence thresholds, automatic-write
# permissions) live entirely in the FarmBot Vision app's own settings, which
# already govern every write this integration is asked to make. The
# integration only keeps the master enable switch and its own liveness
# bookkeeping, neither of which the app can own.
OPTION_VISION_ENABLED = "vision_enabled"
OPTION_VISION_HEARTBEAT_TIMEOUT_MINUTES = "vision_heartbeat_timeout_minutes"

DEFAULT_VISION_ENABLED = False
DEFAULT_VISION_HEARTBEAT_TIMEOUT_MINUTES = 10

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
MAX_IMAGE_LOOKBACK_HOURS = 24 * 30  # re-analysis picker supports one month

# Image resize defaults for farmbot.get_vision_image
DEFAULT_IMAGE_MAX_WIDTH = 640
DEFAULT_IMAGE_MAX_HEIGHT = 480
# Hard upper bound on a requested output bounding-box side. Comfortably above
# the analysis resolutions the FarmBot Vision app asks for (640x480, 960x720,
# 1280x960) and the native FarmBot camera (2592x1944), but still bounded so a
# caller can never request an arbitrarily large re-encode.
MAX_IMAGE_DIMENSION = 4096

# Soil-height capture limits. The companion app may choose stricter values,
# but the integration is the final authority before any movement is sent.
MIN_SOIL_BASELINE_MM = 5.0
MAX_SOIL_BASELINE_MM = 30.0
MAX_SOIL_Z_OFFSET_MM = 75.0
MAX_SOIL_RELOCATION_MM = 200.0
SOIL_POINT_STALE_DAYS = 14
SOIL_RPC_TIMEOUT_SECONDS = 120
SOIL_IMAGE_TIMEOUT_SECONDS = 180
SOIL_CAPTURE_SETTLE_MILLISECONDS = 1500

# Photo-grid repairs deliberately verify each capture through the REST API.
# FarmBot's take_photo command reports camera failures asynchronously, so an
# rpc_ok only proves the command was accepted, not that an image was created.
GRID_REPAIR_IMAGE_TIMEOUT_SECONDS = 60
# 60 s of image-wait per attempt means a single dead cell could previously
# burn ~6 minutes at 6 attempts. Failures no longer abort the whole batch
# (see GRID_REPAIR_MAX_CONSECUTIVE_FAILURES below), so a run of bad cells
# must stay bounded instead.
GRID_REPAIR_MAX_PHOTO_ATTEMPTS = 3
GRID_REPAIR_COORDINATE_TOLERANCE_MM = 25.0
# 5 mm was tighter than FarmBot's real steady-state positioning accuracy and
# caused spurious "did not reach the cell" aborts. 15 mm is still comfortably
# below GRID_REPAIR_COORDINATE_TOLERANCE_MM (25 mm) above, which is what
# actually validates the resulting image, so a photo accepted at 15 mm
# position error still passes image-coordinate matching.
GRID_REPAIR_POSITION_TOLERANCE_MM = 15.0
GRID_REPAIR_POSITION_TIMEOUT_SECONDS = 60
GRID_REPAIR_LIGHTING_PIN = 7
# One `start_vision_grid_repair` call carries a whole bed grid.
#
# The previous cap was twelve, which forced the Vision app to slice a 77-cell
# serpentine route into seven separate calls. Each call is its own run: it
# switched the lighting on, drove in from wherever the gantry was parked,
# photographed twelve cells, then switched the lighting off and drove back to
# the staging position -- six pointless round trips out of the bed, six
# lighting cycles, and rows cut in half and resumed later. Nothing about the
# hardware required that; only this schema limit did.
#
# 256 covers the largest bed the camera footprint can produce (a 2.6 m x 6 m
# bed at the tightest sensible overlap is well under 200 cells) while keeping
# a bound on how much movement a single service call can queue.
GRID_REPAIR_MAX_TARGETS_PER_CALL = 256
# What integrations before 2.5.0 accepted. The Vision app falls back to this
# chunk size when `continuous_photo_grid_capture` is not advertised, so it
# must not be raised here without also raising it there.
GRID_REPAIR_LEGACY_MAX_TARGETS_PER_CALL = 12
# Cell-to-cell travel inside the grid skips FarmBot's `safe_z` retract only
# when every cell shares one Z *and* that Z is within this margin of the top
# of the Z axis -- i.e. the gantry is already as retracted as `safe_z` could
# make it, so the retract/descend cycle adds wear and time without adding
# clearance. Any lower capture height keeps `safe_z` on every move.
GRID_REPAIR_FLAT_TRAVEL_TOP_MARGIN_MM = 25.0
# If this many photo-grid targets fail back-to-back, the bot is likely stuck,
# disconnected, or the camera is dead; grinding through the rest of a large
# grid is pointless, so the batch aborts early instead.
GRID_REPAIR_MAX_CONSECUTIVE_FAILURES = 5

# --------------------------------------------------------------------------
# Experimental raw G-code execution (see gcode.py)
#
# This path reaches the Farmduino through FarmBot OS's Lua `gcode()` function,
# which applies no validation of its own. Every limit below is therefore a real
# safety bound, not a nicety: nothing else sits between a caller's text and the
# stepper drivers.
# --------------------------------------------------------------------------
GCODE_MAX_LINES = 2000
GCODE_MAX_MOVES = 1000
# `gcode()` blocks until the firmware answers, so one Lua node holding a whole
# shape would keep a single RPC open for the entire run. Twenty calls per node
# bounds each acknowledgement and gives the caller progress between chunks.
GCODE_CALLS_PER_LUA_CHUNK = 20
GCODE_CHUNK_RPC_TIMEOUT_SECONDS = 240
GCODE_DEFAULT_FEED_MM_PER_MIN = 400.0
GCODE_MIN_FEED_MM_PER_MIN = 1.0
# Roughly FarmBot's own maximum traverse. Higher belongs in firmware config,
# not in a program that arrives over a service call.
GCODE_MAX_FEED_MM_PER_MIN = 3000.0
# Firmware speeds are steps/second. The floor keeps a very short segment from
# asking for ~0 steps/second; the fallback ceiling applies only when
# `movement_max_spd_*` is missing, where "unknown" must mean slow.
GCODE_MIN_STEPS_PER_SECOND = 10.0
GCODE_FALLBACK_MAX_STEPS_PER_SECOND = 800.0

# Adaptive rotary-tool weeding. These are hard integration-side ceilings; the
# Vision app may choose more conservative values.
WEEDING_MAX_WEEDS_PER_RUN = 100
WEEDING_RPC_TIMEOUT_SECONDS = 300
WEEDING_MAX_PATH_MM = 500.0
WEEDING_MAX_ATTEMPTS = 5

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
