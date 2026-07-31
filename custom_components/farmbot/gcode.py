"""Experimental raw G-code execution for FarmBot.

FarmBot OS normally accepts only CeleryScript, which it compiles into the
G-code dialect its Farmduino firmware actually speaks. FarmBot OS v15 added a
Lua escape hatch -- ``gcode(command, params)`` -- that forwards a command to
the Farmduino verbatim, with *no* validation and no motion planning in
between. Wrapping that call in a ``lua`` CeleryScript node is the only way to
put raw firmware G-code on the wire, and it is what this module builds.

That function does not exist before FarmBot OS v15, and nothing in FarmBot's
status report names the Lua API's version, so an older bot cannot be detected
before the fact: the first chunk fails with a Lua error and the run reports it.

Because FarmBot OS validates nothing on that path, this module is the only
thing standing between a caller's text and the hardware. It therefore:

- accepts a deliberately tiny allowlist of codes (``G21``, ``G90``, ``G91``,
  ``G00``, and a modal ``F``) and rejects everything else by name,
- tracks the resolved absolute position of every move and refuses the whole
  program if any point leaves the axis bounds derived from firmware config,
- converts a conventional ``F`` feed rate in mm/min into the per-axis
  ``A``/``B``/``C`` speeds in steps/second that the Farmduino actually wants,
  scaled so all axes finish together, and
- clamps every speed into a range the firmware configuration allows.

Two firmware facts shape the design:

``G01`` is not implemented by the FarmBot firmware, and ``G00`` is documented
as "move to location at given speed for axis (don't have to be a straight
line)". There is no coordinated interpolation to rely on, so a curve has to
arrive as many short ``G00`` segments, and each segment is only approximately
straight -- hence the proportional per-axis speed scaling below, which makes
the axes finish together and keeps a segment close to its chord.

The firmware appends its own ``Q`` (queue) parameter. Setting ``Q`` explicitly
crashes FarmBot OS, so ``Q`` is rejected as an input word.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .const import (
    GCODE_CALLS_PER_LUA_CHUNK,
    GCODE_FALLBACK_MAX_STEPS_PER_SECOND,
    GCODE_MAX_FEED_MM_PER_MIN,
    GCODE_MAX_LINES,
    GCODE_MAX_MOVES,
    GCODE_MIN_FEED_MM_PER_MIN,
    GCODE_MIN_STEPS_PER_SECOND,
)

AXES = ("x", "y", "z")
# G-code parameter letter per axis, and the firmware's speed letter for it.
_AXIS_WORD = {"x": "X", "y": "Y", "z": "Z"}
_SPEED_WORD = {"x": "A", "y": "B", "z": "C"}

# One word is a letter followed by a number: `X-12.5`, `G0`, `F400`.
_WORD = re.compile(r"([A-Za-z])\s*([+-]?(?:\d+\.?\d*|\.\d+))")
_BLOCK_COMMENT = re.compile(r"\([^)]*\)")

_SUPPORTED_SUMMARY = "G21, G90, G91, G00 (X/Y/Z/F/A/B/C) and a standalone F"


class GcodeError(ValueError):
    """A program was rejected before any of it reached the firmware.

    Every rejection is all-or-nothing: the caller's program either validates
    completely or nothing is sent, so a bad line halfway down can never leave
    the gantry stranded mid-shape.
    """


@dataclass(frozen=True)
class GcodeMove:
    """One resolved ``G00`` ready to hand to the firmware verbatim."""

    line_number: int
    source: str
    # Absolute millimetre target for every axis, including axes the line did
    # not mention -- the firmware is told exactly where to be, never "leave
    # this one alone", so a resolved program is unambiguous on replay.
    target: dict[str, float]
    # Per-axis speeds in steps/second (the firmware's A/B/C words).
    speeds: dict[str, float]
    distance_mm: float
    clamped_axes: tuple[str, ...] = ()

    def params(self) -> dict[str, float]:
        """The G-code parameter table, as ``gcode()`` wants it."""
        params: dict[str, float] = {}
        for axis in AXES:
            params[_AXIS_WORD[axis]] = round(self.target[axis], 3)
        for axis in AXES:
            params[_SPEED_WORD[axis]] = round(self.speeds[axis])
        return params


@dataclass
class GcodeProgram:
    """A validated program plus what it will do, for display before sending."""

    moves: list[GcodeMove]
    start_position: dict[str, float]
    feed_mm_per_min: float
    clamped_axes: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def total_distance_mm(self) -> float:
        return sum(move.distance_mm for move in self.moves)

    def extent(self) -> dict[str, tuple[float, float]]:
        """Bounding box of everywhere the gantry will be, start included."""
        result = {}
        for axis in AXES:
            values = [self.start_position[axis]] + [m.target[axis] for m in self.moves]
            result[axis] = (min(values), max(values))
        return result


def _axis_steps_per_mm(firmware_config: dict[str, Any], axis: str) -> float:
    value = firmware_config.get(f"movement_step_per_mm_{axis}")
    try:
        steps = float(value)
    except (TypeError, ValueError) as err:
        raise GcodeError(
            f"FarmBot did not report steps-per-mm for the {axis.upper()} axis"
        ) from err
    if not math.isfinite(steps) or steps <= 0:
        raise GcodeError(f"FarmBot reported an unusable steps-per-mm for {axis.upper()}")
    return steps


def _axis_max_steps_per_second(firmware_config: dict[str, Any], axis: str) -> float:
    """Upper speed bound for one axis, in steps/second.

    ``movement_max_spd_*`` is the firmware's own configured ceiling. When it is
    missing or nonsensical a conservative constant is used instead -- this path
    has no motion planner behind it, so an unknown limit must mean "slow", not
    "unlimited".
    """
    try:
        value = float(firmware_config.get(f"movement_max_spd_{axis}"))
    except (TypeError, ValueError):
        return GCODE_FALLBACK_MAX_STEPS_PER_SECOND
    if not math.isfinite(value) or value <= 0:
        return GCODE_FALLBACK_MAX_STEPS_PER_SECOND
    return value


def _strip_comment(line: str) -> str:
    line = _BLOCK_COMMENT.sub(" ", line)
    for marker in (";", "//"):
        index = line.find(marker)
        if index != -1:
            line = line[:index]
    return line.strip()


def _words(line: str, line_number: int) -> list[tuple[str, float]]:
    """Split a line into (letter, value) pairs, rejecting anything left over."""
    parsed = [(match.group(1).upper(), float(match.group(2))) for match in _WORD.finditer(line)]
    residue = _WORD.sub("", line).strip()
    if residue:
        raise GcodeError(f"Line {line_number}: could not parse {residue!r}")
    for letter, value in parsed:
        if not math.isfinite(value):
            raise GcodeError(f"Line {line_number}: {letter} value is not a finite number")
    return parsed


def parse_program(
    lines: list[str],
    *,
    start_position: dict[str, float],
    axis_bounds: dict[str, list[float] | None],
    firmware_config: dict[str, Any],
    default_feed_mm_per_min: float,
) -> GcodeProgram:
    """Validate a G-code program and resolve it into firmware-ready moves.

    Raises :class:`GcodeError` -- naming the offending line -- rather than
    partially accepting a program.
    """
    if len(lines) > GCODE_MAX_LINES:
        raise GcodeError(f"Program has {len(lines)} lines; the limit is {GCODE_MAX_LINES}")
    for axis in AXES:
        value = start_position.get(axis)
        if value is None or not math.isfinite(float(value)):
            raise GcodeError(
                "FarmBot has not reported a position for every axis; home or move it "
                "once before running raw G-code"
            )
        if axis_bounds.get(axis) is None:
            raise GcodeError("FarmBot axis bounds are unavailable")

    steps_per_mm = {axis: _axis_steps_per_mm(firmware_config, axis) for axis in AXES}
    max_steps = {axis: _axis_max_steps_per_second(firmware_config, axis) for axis in AXES}

    position = {axis: float(start_position[axis]) for axis in AXES}
    program_start = dict(position)
    feed = float(default_feed_mm_per_min)
    absolute = True
    moves: list[GcodeMove] = []
    clamped: set[str] = set()
    warnings: list[str] = []

    for line_number, raw in enumerate(lines, start=1):
        text = _strip_comment(raw)
        if not text:
            continue
        words = _words(text, line_number)
        letters = [letter for letter, _ in words]
        if "Q" in letters:
            raise GcodeError(
                f"Line {line_number}: Q is added by FarmBot OS and must not be set "
                "(setting it crashes FarmBot OS)"
            )
        if "N" in letters:
            raise GcodeError(f"Line {line_number}: line numbers (N) are not supported")

        g_values = [value for letter, value in words if letter == "G"]
        m_values = [value for letter, value in words if letter == "M"]
        if m_values:
            raise GcodeError(
                f"Line {line_number}: M{int(m_values[0]):02d} is not supported. "
                f"Supported: {_SUPPORTED_SUMMARY}"
            )
        if len(g_values) > 1:
            raise GcodeError(f"Line {line_number}: more than one G code on a line")

        if not g_values:
            # A bare `F400` sets the modal feed rate for everything after it.
            if letters == ["F"]:
                feed = _validated_feed(dict(words)["F"], line_number)
                continue
            raise GcodeError(
                f"Line {line_number}: no G code and not a feed rate. "
                f"Supported: {_SUPPORTED_SUMMARY}"
            )

        code = int(round(g_values[0]))
        values = {letter: value for letter, value in words if letter != "G"}

        if code in (20, 21):
            if code == 20:
                raise GcodeError(
                    f"Line {line_number}: G20 (inches) is not supported; FarmBot works in "
                    "millimetres (G21)"
                )
            if values:
                raise GcodeError(f"Line {line_number}: G21 takes no parameters")
            continue
        if code in (90, 91):
            if values:
                raise GcodeError(f"Line {line_number}: G{code} takes no parameters")
            absolute = code == 90
            continue
        if code == 1:
            raise GcodeError(
                f"Line {line_number}: the FarmBot firmware does not implement G01. Use G00 -- "
                "note it is not guaranteed to move in a straight line, so curves need short "
                "segments"
            )
        if code != 0:
            raise GcodeError(
                f"Line {line_number}: G{code:02d} is not supported. Supported: "
                f"{_SUPPORTED_SUMMARY}"
            )

        unknown = set(values) - {"X", "Y", "Z", "F", "A", "B", "C"}
        if unknown:
            raise GcodeError(
                f"Line {line_number}: G00 does not accept {', '.join(sorted(unknown))}"
            )
        if "F" in values:
            feed = _validated_feed(values["F"], line_number)

        target = dict(position)
        moved = False
        for axis in AXES:
            word = _AXIS_WORD[axis]
            if word not in values:
                continue
            moved = True
            target[axis] = values[word] if absolute else position[axis] + values[word]

        if not moved:
            # A G00 with only an F is just a feed-rate change; nothing to send.
            continue

        for axis in AXES:
            low, high = axis_bounds[axis]  # type: ignore[misc]
            if not low - 0.001 <= target[axis] <= high + 0.001:
                raise GcodeError(
                    f"Line {line_number}: {_AXIS_WORD[axis]}{target[axis]:.1f} is outside the "
                    f"{axis.upper()} axis range {low:.0f} to {high:.0f} mm"
                )

        explicit_speeds = {
            axis: values[_SPEED_WORD[axis]]
            for axis in AXES
            if _SPEED_WORD[axis] in values
        }
        speeds, distance, move_clamped = _resolve_speeds(
            start=position,
            target=target,
            feed_mm_per_min=feed,
            steps_per_mm=steps_per_mm,
            max_steps=max_steps,
            explicit=explicit_speeds,
            line_number=line_number,
        )
        clamped.update(move_clamped)
        moves.append(
            GcodeMove(
                line_number=line_number,
                source=text,
                target=dict(target),
                speeds=speeds,
                distance_mm=distance,
                clamped_axes=move_clamped,
            )
        )
        if len(moves) > GCODE_MAX_MOVES:
            raise GcodeError(f"Program has more than {GCODE_MAX_MOVES} moves")
        position = target

    if not moves:
        raise GcodeError("Program contains no movement")
    if clamped:
        warnings.append(
            "Speed was clamped to the firmware's configured maximum on the "
            + ", ".join(axis.upper() for axis in sorted(clamped))
            + " axis"
        )

    return GcodeProgram(
        moves=moves,
        start_position=program_start,
        feed_mm_per_min=feed,
        clamped_axes=tuple(sorted(clamped)),
        warnings=warnings,
    )


def _validated_feed(value: float, line_number: int) -> float:
    if not GCODE_MIN_FEED_MM_PER_MIN <= value <= GCODE_MAX_FEED_MM_PER_MIN:
        raise GcodeError(
            f"Line {line_number}: feed rate {value:g} mm/min is outside the permitted "
            f"{GCODE_MIN_FEED_MM_PER_MIN:g}-{GCODE_MAX_FEED_MM_PER_MIN:g} mm/min"
        )
    return float(value)


def _resolve_speeds(
    *,
    start: dict[str, float],
    target: dict[str, float],
    feed_mm_per_min: float,
    steps_per_mm: dict[str, float],
    max_steps: dict[str, float],
    explicit: dict[str, float],
    line_number: int,
) -> tuple[dict[str, float], float, tuple[str, ...]]:
    """Turn a feed rate into the firmware's per-axis steps/second speeds.

    Each axis gets a speed proportional to how far it has to travel, so every
    axis finishes at the same moment. G00 does not interpolate, so this is what
    keeps a segment close to the straight chord the caller drew: without it the
    short axis arrives first and the path dog-legs.
    """
    deltas = {axis: target[axis] - start[axis] for axis in AXES}
    distance = math.sqrt(sum(delta * delta for delta in deltas.values()))
    seconds = distance / (feed_mm_per_min / 60.0) if distance > 0 else 0.0

    speeds: dict[str, float] = {}
    clamped: list[str] = []
    for axis in AXES:
        if axis in explicit:
            requested = explicit[axis]
            if requested <= 0:
                raise GcodeError(
                    f"Line {line_number}: {_SPEED_WORD[axis]} speed must be positive"
                )
        elif seconds > 0:
            requested = abs(deltas[axis]) / seconds * steps_per_mm[axis]
        else:
            requested = GCODE_MIN_STEPS_PER_SECOND
        ceiling = max_steps[axis]
        if requested > ceiling:
            requested = ceiling
            clamped.append(axis)
        # An axis that is not moving still needs a speed word; a floor also
        # stops a very short segment asking the firmware for ~0 steps/second.
        speeds[axis] = max(requested, GCODE_MIN_STEPS_PER_SECOND)
    return speeds, distance, tuple(clamped)


def _lua_number(value: float) -> str:
    """Format without exponent notation, which Lua's tonumber-free parse needs."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def lua_chunks(moves: list[GcodeMove]) -> list[str]:
    """Render moves as Lua source, split into bounded chunks.

    ``gcode()`` blocks until the firmware answers, so a whole shape in one Lua
    node would hold a single RPC open for the entire run. Chunking bounds each
    acknowledgement and gives the caller real progress between them.
    """
    chunks: list[str] = []
    for index in range(0, len(moves), GCODE_CALLS_PER_LUA_CHUNK):
        batch = moves[index : index + GCODE_CALLS_PER_LUA_CHUNK]
        statements = []
        for move in batch:
            params = ", ".join(
                f"{word} = {_lua_number(value)}" for word, value in move.params().items()
            )
            statements.append(f'gcode("G00", {{ {params} }})')
        chunks.append("\n".join(statements))
    return chunks


def lua_node(source: str) -> dict[str, Any]:
    """Wrap Lua source in the CeleryScript node FarmBot OS executes."""
    return {"kind": "lua", "args": {"lua": source}}
