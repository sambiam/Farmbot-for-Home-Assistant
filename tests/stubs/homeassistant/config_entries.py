"""Minimal stand-in for homeassistant.config_entries.

Reimplements just enough of ``ConfigFlow``/``OptionsFlow`` (unique-id
de-duplication, form/entry/abort results, reauth-update-and-abort, options
storage) to exercise custom_components/farmbot/config_flow.py's behaviour
in isolation. Mirrors the real Home Assistant semantics closely enough to
be a faithful test double, but is not a substitute for testing against
real Home Assistant.
"""
from .data_entry_flow import AbortFlow

SOURCE_REAUTH = "reauth"


class ConfigEntry:
    """A bare-bones stand-in for a loaded config entry."""

    def __init__(
        self,
        entry_id,
        unique_id=None,
        data=None,
        domain=None,
        title=None,
        version=1,
        options=None,
    ):
        self.entry_id = entry_id
        self.unique_id = unique_id
        self.data = dict(data or {})
        self.domain = domain
        self.title = title
        self.version = version
        self.options = dict(options or {})
        self._on_unload = []
        self._update_listeners = []

    def async_on_unload(self, func):
        """Stand-in for ConfigEntry.async_on_unload; records `func` to call on unload."""
        self._on_unload.append(func)
        return func

    def add_update_listener(self, listener):
        """Stand-in for ConfigEntry.add_update_listener; returns an unsub callable."""
        self._update_listeners.append(listener)

        def _unsub():
            if listener in self._update_listeners:
                self._update_listeners.remove(listener)

        return _unsub


class ConfigEntries:
    """Stand-in for ``hass.config_entries``."""

    def __init__(self, entries=None):
        self._entries = list(entries or [])

    def async_entries(self, domain=None):
        if domain is None:
            return list(self._entries)
        return [e for e in self._entries if e.domain == domain]

    def async_get_entry(self, entry_id):
        return next((e for e in self._entries if e.entry_id == entry_id), None)

    def async_update_entry(self, entry, data=None, unique_id=None, version=None, options=None, **kwargs):
        if data is not None:
            entry.data = dict(data)
        if unique_id is not None:
            entry.unique_id = unique_id
        if version is not None:
            entry.version = version
        if options is not None:
            entry.options = dict(options)


class ConfigFlow:
    """Stand-in for ``homeassistant.config_entries.ConfigFlow``."""

    domain = None

    def __init_subclass__(cls, *, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)
        if domain is not None:
            cls.domain = domain

    def __init__(self):
        self.hass = None
        self.context = {}
        self.unique_id = None

    async def async_set_unique_id(self, unique_id, raise_on_progress=True):
        self.unique_id = unique_id
        return None

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True):
        if self.unique_id is None:
            return
        for entry in self.hass.config_entries.async_entries(self.domain):
            if entry.unique_id == self.unique_id:
                if updates:
                    entry.data = {**entry.data, **updates}
                raise AbortFlow("already_configured")

    def async_show_form(
        self, *, step_id, data_schema=None, errors=None, description_placeholders=None
    ):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            "description_placeholders": description_placeholders,
        }

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_abort(self, *, reason):
        return {"type": "abort", "reason": reason}

    def async_update_reload_and_abort(self, entry, *, data=None, reason="reauth_successful", **kwargs):
        if data is not None and self.hass is not None:
            self.hass.config_entries.async_update_entry(entry, data=data)
        elif data is not None:
            entry.data = dict(data)
        return self.async_abort(reason=reason)


class OptionsFlow:
    """Stand-in for ``homeassistant.config_entries.OptionsFlow``.

    Real Home Assistant (2024.12+) sets ``self.config_entry`` automatically
    before a step is called; this stub's ``FakeConfigEntries``-driven tests
    set it explicitly on the instance after construction instead.
    """

    def __init__(self):
        self.config_entry = None
        self.hass = None

    def async_show_form(self, *, step_id, data_schema=None, errors=None, description_placeholders=None):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            "description_placeholders": description_placeholders,
        }

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}
