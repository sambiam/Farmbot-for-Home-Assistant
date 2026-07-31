"""Raw firmware G-code parsing, bounds checking and Lua generation.

FarmBot OS's Lua ``gcode()`` function applies no validation before handing a
command to the Farmduino, so ``custom_components/farmbot/gcode.py`` is the only
thing between a caller's text and the stepper drivers. These tests pin the
rejections that keep it that way, and the arithmetic that turns a conventional
feed rate into the firmware's per-axis steps/second.
"""

import math

import pytest

from custom_components.farmbot.const import (
    GCODE_FALLBACK_MAX_STEPS_PER_SECOND,
    GCODE_MAX_FEED_MM_PER_MIN,
    GCODE_MIN_STEPS_PER_SECOND,
)
from custom_components.farmbot.gcode import (
    GcodeError,
    lua_chunks,
    lua_node,
    parse_program,
)

# 100 steps/mm on every axis keeps steps/second numerically equal to mm/second
# times 100, so the expected speeds below can be read off by hand.
FIRMWARE = {
    "movement_step_per_mm_x": 100,
    "movement_step_per_mm_y": 100,
    "movement_step_per_mm_z": 100,
    "movement_max_spd_x": 1000,
    "movement_max_spd_y": 1000,
    "movement_max_spd_z": 500,
}

BOUNDS = {"x": [0.0, 2000.0], "y": [0.0, 1000.0], "z": [-500.0, 0.0]}
START = {"x": 100.0, "y": 100.0, "z": 0.0}


def parse(lines, *, firmware=None, bounds=None, start=None, feed=600.0):
    return parse_program(
        lines,
        start_position=dict(start or START),
        axis_bounds=dict(bounds or BOUNDS),
        firmware_config=dict(firmware or FIRMWARE),
        default_feed_mm_per_min=feed,
    )


def test_absolute_moves_resolve_every_axis():
    """A line that names only X still commands Y and Z, at their current value.

    The firmware is told exactly where to be on all three axes rather than
    "leave this one alone", so a resolved program is unambiguous on replay.
    """
    program = parse(["G21", "G90", "G00 X200", "G00 Y300"])

    assert [move.target for move in program.moves] == [
        {"x": 200.0, "y": 100.0, "z": 0.0},
        {"x": 200.0, "y": 300.0, "z": 0.0},
    ]
    assert program.moves[0].params()["X"] == 200.0
    assert program.moves[0].params()["Z"] == 0.0


def test_relative_mode_accumulates_from_the_previous_point():
    program = parse(["G91", "G00 X10", "G00 X10 Y-20"])

    assert [move.target for move in program.moves] == [
        {"x": 110.0, "y": 100.0, "z": 0.0},
        {"x": 120.0, "y": 80.0, "z": 0.0},
    ]


def test_axis_speeds_are_scaled_so_every_axis_finishes_together():
    """G00 does not interpolate, so proportional speeds keep a segment straight.

    A 300 x 400 mm move at 600 mm/min (10 mm/s) covers 500 mm in 50 s, so X
    runs at 6 mm/s and Y at 8 mm/s -- 600 and 800 steps/second here.
    """
    program = parse(["G90", "G00 X400 Y500 F600"])
    move = program.moves[0]

    assert move.distance_mm == pytest.approx(500.0)
    assert move.speeds["x"] == pytest.approx(600.0)
    assert move.speeds["y"] == pytest.approx(800.0)
    assert move.params()["A"] == 600
    assert move.params()["B"] == 800


def test_speed_is_clamped_to_the_firmware_maximum_and_reported():
    """An axis is never asked for more steps/second than firmware config allows."""
    program = parse(["G90", "G00 X1100 F3000"])
    move = program.moves[0]

    assert move.speeds["x"] == FIRMWARE["movement_max_spd_x"]
    assert move.clamped_axes == ("x",)
    assert program.clamped_axes == ("x",)
    assert program.warnings and "clamped" in program.warnings[0]


def test_missing_firmware_maximum_falls_back_to_the_slow_ceiling():
    """An unknown limit must mean "slow", not "unlimited" -- nothing plans behind us."""
    firmware = {k: v for k, v in FIRMWARE.items() if not k.startswith("movement_max_spd")}
    program = parse(["G90", "G00 X1900 F3000"], firmware=firmware)

    assert program.moves[0].speeds["x"] == GCODE_FALLBACK_MAX_STEPS_PER_SECOND


def test_a_stationary_axis_still_gets_a_usable_speed_word():
    program = parse(["G90", "G00 X200"])

    assert program.moves[0].speeds["z"] == GCODE_MIN_STEPS_PER_SECOND


def test_explicit_axis_speeds_pass_through_untouched():
    """A caller writing real firmware G-code by hand keeps their A/B/C."""
    program = parse(["G90", "G00 X200 Y200 A123 B456"])
    move = program.moves[0]

    assert move.speeds["x"] == 123
    assert move.speeds["y"] == 456


def test_explicit_axis_speed_is_still_clamped():
    program = parse(["G90", "G00 X200 A99999"])

    assert program.moves[0].speeds["x"] == FIRMWARE["movement_max_spd_x"]


def test_modal_feed_rate_persists_until_changed():
    program = parse(["G90", "F60", "G00 X160", "G00 X220"])

    # 60 mm/min is 1 mm/s, so a 60 mm move takes 60 s: 1 mm/s -> 100 steps/s.
    assert program.moves[0].speeds["x"] == pytest.approx(100.0)
    assert program.moves[1].speeds["x"] == pytest.approx(100.0)


def test_comments_and_blank_lines_are_ignored():
    program = parse(
        [
            "; a leading comment",
            "",
            "   ",
            "(a block comment) G90",
            "G00 X200 ; trailing",
        ]
    )

    assert len(program.moves) == 1


@pytest.mark.parametrize(
    "line, expected",
    [
        ("G01 X200", "does not implement G01"),
        ("G02 X200 I5 J5", "G02 is not supported"),
        ("G28", "G28 is not supported"),
        ("M3 S1000", "M03 is not supported"),
        ("G20", "inches"),
        ("G00 X200 Q4", "Q is added by FarmBot OS"),
        ("N10 G00 X200", "line numbers"),
        ("G00 X200 E5", "does not accept E"),
        ("G00 G90 X200", "more than one G code"),
        ("hello world", "could not parse"),
        ("G90 X5", "takes no parameters"),
    ],
)
def test_unsupported_input_is_rejected_by_name(line, expected):
    """Every rejection names what was wrong; nothing is silently reinterpreted."""
    with pytest.raises(GcodeError, match=expected):
        parse(["G90", line])


def test_g01_rejection_points_at_the_firmware_limitation():
    """G01 is a firmware gap, not our restriction, so the message must say so."""
    with pytest.raises(GcodeError) as err:
        parse(["G90", "G01 X200"])

    assert "Use G00" in str(err.value)
    assert "straight line" in str(err.value)


@pytest.mark.parametrize(
    "line",
    ["G00 X2500", "G00 X-1", "G00 Y1500", "G00 Z50", "G00 Z-900"],
)
def test_a_point_outside_the_axis_bounds_rejects_the_whole_program(line):
    with pytest.raises(GcodeError, match="outside the"):
        parse(["G90", "G00 X200", line, "G00 X300"])


def test_bounds_are_checked_on_the_resolved_point_not_the_written_one():
    """Relative moves are only unsafe once accumulated, so check after resolving."""
    with pytest.raises(GcodeError, match="outside the"):
        parse(["G91"] + ["G00 X500"] * 5)


def test_feed_rate_outside_the_permitted_range_is_rejected():
    with pytest.raises(GcodeError, match="feed rate"):
        parse(["G90", f"G00 X200 F{GCODE_MAX_FEED_MM_PER_MIN + 1:g}"])
    with pytest.raises(GcodeError, match="feed rate"):
        parse(["G90", "G00 X200 F0"])


def test_an_unknown_start_position_is_refused():
    """Without a known position there is nothing to resolve deltas or bounds against."""
    with pytest.raises(GcodeError, match="has not reported a position"):
        parse(["G90", "G00 X200"], start={"x": 100.0, "y": None, "z": 0.0})


def test_missing_axis_bounds_are_refused():
    with pytest.raises(GcodeError, match="bounds are unavailable"):
        parse(["G90", "G00 X200"], bounds={**BOUNDS, "z": None})


def test_missing_steps_per_mm_is_refused():
    firmware = {k: v for k, v in FIRMWARE.items() if k != "movement_step_per_mm_y"}
    with pytest.raises(GcodeError, match="steps-per-mm"):
        parse(["G90", "G00 X200"], firmware=firmware)


def test_a_program_with_no_movement_is_refused():
    with pytest.raises(GcodeError, match="no movement"):
        parse(["G21", "G90", "F600", "; nothing to do"])


def test_a_g00_that_only_sets_the_feed_rate_is_not_a_move():
    program = parse(["G90", "G00 F900", "G00 X200"])

    assert len(program.moves) == 1
    assert program.feed_mm_per_min == 900.0
    # ...and the feed it set still applies to the move that follows: 900 mm/min
    # is 15 mm/s, i.e. 1500 steps/s, clamped to this firmware's 1000.
    assert program.moves[0].speeds["x"] == FIRMWARE["movement_max_spd_x"]


def test_extent_covers_the_start_position_as_well_as_every_target():
    """The reported bounding box is where the gantry will be, not just where it stops."""
    program = parse(["G90", "G00 X500", "G00 X300 Y700"])

    extent = program.extent()
    assert extent["x"] == (100.0, 500.0)
    assert extent["y"] == (100.0, 700.0)


def test_total_distance_sums_the_segments():
    program = parse(["G90", "G00 X200", "G00 Y200"])

    assert program.total_distance_mm == pytest.approx(200.0)


def test_lua_chunks_emit_raw_gcode_calls_and_stay_bounded():
    """One Lua node per bounded batch: `gcode()` blocks until the firmware answers."""
    program = parse(["G90"] + [f"G00 X{100 + n}" for n in range(1, 46)])
    chunks = lua_chunks(program.moves)

    assert len(chunks) == 3  # 45 moves at 20 calls per chunk
    assert chunks[0].startswith('gcode("G00", { X = ')
    assert chunks[0].count("gcode(") == 20
    assert chunks[-1].count("gcode(") == 5


def test_lua_output_never_uses_exponent_notation():
    """Lua source is text; a `1e-05` in a parameter would be a syntax hazard."""
    program = parse(["G90", "G00 X100.0001 Y100.0001"])

    assert "e-" not in lua_chunks(program.moves)[0].lower()


def test_lua_node_is_the_celeryscript_shape_farmbot_os_executes():
    assert lua_node("gcode(\"G00\", { X = 1 })") == {
        "kind": "lua",
        "args": {"lua": 'gcode("G00", { X = 1 })'},
    }


def test_q_is_never_emitted():
    """FarmBot OS appends Q itself; emitting one crashes FarmBot OS."""
    program = parse(["G90", "G00 X200 Y200"])

    assert "Q" not in program.moves[0].params()
    assert " Q " not in lua_chunks(program.moves)[0]


def test_every_emitted_parameter_is_finite():
    program = parse(["G90", "G00 X200 Y200 Z-50"])

    assert all(math.isfinite(value) for value in program.moves[0].params().values())
