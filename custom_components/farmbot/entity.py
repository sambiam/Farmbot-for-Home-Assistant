"""Shared entity plumbing for the FarmBot integration.

Two things live here because every controllable FarmBot entity needs them
and none of them should re-invent them:

``FarmbotEntity``
    device grouping, plus availability derived from FarmBot's real
    connection health rather than from whether the newest state payload
    happened to mention this entity.

``FarmbotOptimisticEntity``
    the optimistic-state reconciler shared by the peripheral switches
    (lighting, water, vacuum, rotary tool, spare peripherals) and the
    emergency-stop switch. Any future FarmBot pin-backed entity -- a light,
    a valve -- gets the same behaviour by inheriting from it.
"""

import functools
import logging
import time
from typing import Optional

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, OPTIMISTIC_STATE_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)


class FarmbotEntity(Entity):
    """Base class for all FarmBot entities."""

    def __init__(self, manager):
        self._manager = manager

    @property
    def device_info(self):
        """Group everything under one FarmBot device."""
        return {
            "identifiers": {(DOMAIN, self._manager.device_id)},
            "name":        f"FarmBot {self._manager.device_id}",
            "manufacturer":"FarmBot.io",
        }


class FarmbotDeviceStateEntity(FarmbotEntity):
    """A FarmBot entity backed by the manager's cached MQTT device state.

    Availability answers one question only: is FarmBot reachable? It is
    never derived from the contents of an individual state payload. FarmBot
    may report a subset of its state tree at any time, and an update that
    does not mention this entity's pin says nothing at all about whether
    that pin can be controlled -- the manager keeps the last reported value
    for it (see ``manager.merge_state``).
    """

    @property
    def available(self) -> bool:
        return self._manager.device_is_available()


class FarmbotOptimisticEntity(FarmbotDeviceStateEntity):
    """Optimistic state with bounded reconciliation against FarmBot.

    Why this exists: FarmBot acknowledges a ``write_pin`` long before it
    reports the resulting pin state, and Home Assistant force-refreshes a
    polled entity immediately after a successful service call. Reading the
    cached (not-yet-updated) FarmBot state at that moment wrote the *old*
    value back, so a switch went ON (frontend guess) -> OFF (stale read) ->
    ON (FarmBot, ~5 s later). Automations saw two transitions that never
    physically happened.

    The contract:

    * A command that Home Assistant successfully dispatched sets the entity
      to the requested value straight away and records it as pending.
    * While a command is pending, a FarmBot report equal to the request
      confirms it and clears the pending command.
    * A contradictory FarmBot report is treated as a packet generated
      before the command was applied and does not revert the entity -- but
      it *is* remembered, so nothing is invented if the command fails.
    * The newest command always wins; an older command's pending value and
      its timeout are discarded when a new one is issued.
    * The pending state is bounded by ``OPTIMISTIC_STATE_TIMEOUT_SECONDS``.
      Once it lapses, FarmBot is authoritative again, the remembered report
      is applied, and the disagreement is logged.

    Concurrency: every mutation below happens on the Home Assistant event
    loop -- commands from the service call, FarmBot reports from
    ``SIGNAL_STATE`` (dispatched via ``manager._notify_state_listeners``,
    which marshals off the MQTT thread) and the expiry from
    ``async_call_later``. No state here is touched from a worker thread, so
    the sequencing above cannot interleave.
    """

    def __init__(self, manager):
        super().__init__(manager)
        # Last value FarmBot actually reported (None = never reported).
        self._reported_state: Optional[bool] = None
        # Value the user asked for and we are still waiting to see confirmed.
        self._pending_state: Optional[bool] = None
        self._pending_deadline: Optional[float] = None
        self._pending_generation = 0
        self._cancel_pending_timeout = None

    # -------------------- state --------------------

    @property
    def _current_state(self) -> Optional[bool]:
        """What the entity should report right now."""
        if self._pending_state is not None:
            return self._pending_state
        return self._reported_state

    def _pending_has_lapsed(self) -> bool:
        """True when the grace period ran out without a confirmation.

        Checked whenever state is reconciled as well as from the scheduled
        timer, so the entity still recovers if the timer never runs (for
        example when the event loop was blocked past the deadline).
        """
        return (
            self._pending_state is not None
            and self._pending_deadline is not None
            and time.monotonic() >= self._pending_deadline
        )

    @property
    def is_on(self) -> Optional[bool]:
        """None until FarmBot has reported this entity at least once.

        Reporting ``unknown`` for those first moments is honest; reporting
        ``off`` would be a guess that automations could act on.
        """
        return self._current_state

    # -------------------- command side --------------------

    @callback
    def _async_command_sent(self, value: bool) -> None:
        """Record a successfully dispatched command and show it immediately.

        Call this *after* the command has been handed to FarmBot, never
        before: a command that could not be published must not move the
        entity.
        """
        self._pending_state = bool(value)
        self._pending_deadline = time.monotonic() + OPTIMISTIC_STATE_TIMEOUT_SECONDS
        self._pending_generation += 1
        generation = self._pending_generation
        self._cancel_pending_timeout_timer()
        # functools.partial, not a lambda: Home Assistant unwraps partials to
        # find the @callback marker and would otherwise run the expiry in an
        # executor thread -- the exact mistake this class exists to undo.
        self._cancel_pending_timeout = async_call_later(
            self.hass,
            OPTIMISTIC_STATE_TIMEOUT_SECONDS,
            functools.partial(self._async_pending_timed_out, generation),
        )
        self.async_write_ha_state()

    # -------------------- report side --------------------

    @callback
    def _reconcile_report(self, value: Optional[bool]) -> None:
        """Reconcile an authoritative FarmBot value with any pending command.

        Mutates state only; writing it is the caller's job, so this can also
        be used from ``async_update`` (Home Assistant writes the state itself
        after a polled update).

        ``value`` is None when FarmBot has never reported this entity; that
        carries no information, so the last known state is kept.
        """
        if self._pending_has_lapsed():
            self._async_pending_timed_out(self._pending_generation, write=False)
        if value is None:
            return
        value = bool(value)
        self._reported_state = value

        if self._pending_state is None:
            return
        if value == self._pending_state:
            _LOGGER.debug("%s: FarmBot confirmed the requested state %s", self.entity_id, value)
            self._async_clear_pending()
            return
        # Plausibly a status packet generated before the command was applied.
        # Remembered above, surfaced only if the command is never confirmed.
        _LOGGER.debug(
            "%s: ignoring FarmBot state %s while %s is pending",
            self.entity_id,
            value,
            self._pending_state,
        )

    @callback
    def _async_farmbot_reported(self, value: Optional[bool]) -> None:
        """Reconcile a FarmBot report and publish the result."""
        self._reconcile_report(value)
        self.async_write_ha_state()

    @callback
    def _async_pending_timed_out(self, generation: int, _now=None, *, write: bool = True) -> None:
        """Give FarmBot the last word once the grace period lapses."""
        if generation != self._pending_generation or self._pending_state is None:
            return  # superseded by a newer command, or already confirmed
        requested = self._pending_state
        self._async_clear_pending()
        if self._reported_state is not None and self._reported_state != requested:
            _LOGGER.warning(
                "%s: FarmBot did not confirm the requested state %s within %ss; "
                "reverting to the reported state %s",
                self.entity_id,
                requested,
                OPTIMISTIC_STATE_TIMEOUT_SECONDS,
                self._reported_state,
            )
        else:
            _LOGGER.debug(
                "%s: no FarmBot confirmation of %s within %ss",
                self.entity_id,
                requested,
                OPTIMISTIC_STATE_TIMEOUT_SECONDS,
            )
        if write:
            self.async_write_ha_state()

    @callback
    def _async_clear_pending(self) -> None:
        self._pending_state = None
        self._pending_deadline = None
        self._cancel_pending_timeout_timer()

    @callback
    def _cancel_pending_timeout_timer(self) -> None:
        if self._cancel_pending_timeout is not None:
            self._cancel_pending_timeout()
            self._cancel_pending_timeout = None

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_pending_timeout_timer()
