"""Minimal stand-in for homeassistant.helpers.entity.

Models the parts of ``Entity`` the FarmBot platforms rely on, and -- this is
the point of the stub -- Home Assistant's *thread* rules:

``async_write_ha_state`` may only be called from the event loop. Real Home
Assistant checks ``hass.loop_thread_id`` and reports

    Detected that custom integration 'farmbot' calls async_write_ha_state
    from a thread other than the event loop

Here the same check raises ``RuntimeError`` so a regression fails loudly in
tests instead of only in a user's log. ``schedule_update_ha_state`` is the
thread-safe variant and marshals onto the loop, exactly as Core does.

See https://developers.home-assistant.io/docs/asyncio_thread_safety/.
"""
import threading


class Entity:
    # Class attributes only, like Core: FarmBot entities do not chain up to
    # an Entity.__init__ and must not have to.
    hass = None
    entity_id = None
    platform = None
    _attr_should_poll = True
    write_count = 0

    # -------------------- properties Core provides --------------------

    @property
    def should_poll(self):
        return self._attr_should_poll

    @property
    def available(self):
        return True

    @property
    def name(self):
        return None

    @property
    def state(self):
        return None

    # -------------------- state writing --------------------

    def async_write_ha_state(self):
        """Write state. Event loop only, like Home Assistant."""
        if self.hass is None:
            raise RuntimeError(f"Attribute hass is None for {self}")
        loop_thread_id = getattr(self.hass, "loop_thread_id", None)
        if loop_thread_id is not None and loop_thread_id != threading.get_ident():
            raise RuntimeError(
                "calls async_write_ha_state from a thread other than the event loop"
            )
        self.write_count += 1
        self.hass.states.async_set(self.entity_id, self)

    def schedule_update_ha_state(self, force_refresh=False):
        """Thread-safe variant: hops onto the event loop first."""
        self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

    async def async_update_ha_state(self, force_refresh=False):
        if force_refresh:
            await self.async_update()
        self.async_write_ha_state()

    # -------------------- lifecycle --------------------

    def async_on_remove(self, func):
        if not hasattr(self, "_on_remove"):
            self._on_remove = []
        self._on_remove.append(func)

    async def async_added_to_hass(self):
        return None

    async def async_will_remove_from_hass(self):
        return None

    async def async_remove(self):
        await self.async_will_remove_from_hass()
        for func in getattr(self, "_on_remove", []):
            func()
        if hasattr(self, "_on_remove"):
            self._on_remove.clear()
