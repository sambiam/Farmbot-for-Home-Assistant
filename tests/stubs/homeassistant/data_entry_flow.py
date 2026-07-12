"""Minimal stand-in for homeassistant.data_entry_flow."""


class AbortFlow(Exception):
    """Raised by guard helpers such as _abort_if_unique_id_configured.

    In real Home Assistant this is caught by the FlowManager and converted
    into an ``{"type": "abort", "reason": ...}`` result. Tests that call
    flow steps directly must emulate that same catch, matching how the real
    flow manager behaves.
    """

    def __init__(self, reason, description_placeholders=None):
        super().__init__(reason)
        self.reason = reason
        self.description_placeholders = description_placeholders
