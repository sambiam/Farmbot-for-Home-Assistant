"""Sequence select entity: transient FarmBot API failures must not break it.

The reported failure was a DNS blip turning into a full traceback every
polling cycle:

    Update for select.farmbot_sequence fails
    requests.exceptions.ConnectionError: HTTPSConnectionPool(host='my.farm.bot')
    ... NameResolutionError: Failed to resolve 'my.farm.bot'

Sequence retrieval now goes through the integration's async API client,
which classifies that as a FarmbotApiError, and the entity keeps the options
it already has.
"""
import logging

import pytest

from custom_components.farmbot.api import FarmbotApiError, FarmbotAuthError
from custom_components.farmbot.manager import FarmbotManager
from custom_components.farmbot.select import FarmbotSequenceSelect

from .helpers import FakeHass

DEVICE_ID = "42"

SEQUENCES = [{"id": 1, "name": "Water plants"}, {"id": 2, "name": "Mow weeds"}]


def _make_select():
    hass = FakeHass()
    manager = FarmbotManager(
        hass, token="tok", device_id=DEVICE_ID, mqtt_host="mqtt.example.com"
    )
    entity = FarmbotSequenceSelect(manager)
    entity.hass = hass
    entity.entity_id = "select.farmbot_sequence"
    return hass, manager, entity


def _responder(*results):
    """Return an async callable yielding each result (or raising it) in turn."""
    remaining = list(results)

    async def _call():
        item = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(item, Exception):
            raise item
        return item

    return _call


@pytest.mark.asyncio
async def test_dns_failure_after_a_good_fetch_retains_options(caplog):
    _hass, manager, entity = _make_select()
    manager.api.async_get_sequences = _responder(SEQUENCES)

    await entity.async_added_to_hass()
    assert entity.options == ["1: Water plants", "2: Mow weeds"]
    assert entity.available is True

    manager.api.async_get_sequences = _responder(
        FarmbotApiError(
            "Network error calling FarmBot (/sequences): Cannot connect to host "
            "my.farm.bot:443 [Failed to resolve 'my.farm.bot']"
        )
    )
    await entity.async_update()  # must not raise

    assert entity.options == ["1: Water plants", "2: Mow weeds"]
    assert entity.available is True
    assert entity.state != "unavailable"
    assert not [r for r in caplog.records if r.exc_info], "no traceback should be logged"


@pytest.mark.asyncio
async def test_repeated_failures_are_logged_once_not_every_cycle(caplog):
    _hass, manager, entity = _make_select()
    manager.api.async_get_sequences = _responder(SEQUENCES)
    await entity.async_added_to_hass()

    manager.api.async_get_sequences = _responder(FarmbotApiError("DNS failure"))
    for _ in range(6):
        await entity.async_update()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert entity._fetch_failures == 6


@pytest.mark.asyncio
async def test_recovery_refreshes_options_and_clears_the_failure_count(caplog):
    caplog.set_level(logging.INFO)
    _hass, manager, entity = _make_select()
    manager.api.async_get_sequences = _responder(FarmbotApiError("DNS failure"))

    await entity.async_added_to_hass()
    assert entity.available is False  # never fetched successfully
    assert entity.options == []

    manager.api.async_get_sequences = _responder([*SEQUENCES, {"id": 3, "name": "Seed"}])
    await entity.async_update()

    assert entity.available is True
    assert entity.options == ["1: Water plants", "2: Mow weeds", "3: Seed"]
    assert entity._fetch_failures == 0
    assert any("refreshed after" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_expired_credentials_do_not_wipe_the_cached_options():
    """A 401 used to return [] and empty the entity's option list."""
    _hass, manager, entity = _make_select()
    manager.api.async_get_sequences = _responder(SEQUENCES)
    await entity.async_added_to_hass()

    manager.api.async_get_sequences = _responder(
        FarmbotAuthError("FarmBot rejected credentials (401) for /sequences")
    )
    await entity.async_update()

    assert entity.options == ["1: Water plants", "2: Mow weeds"]


@pytest.mark.asyncio
async def test_unexpected_errors_still_propagate():
    """Only expected network failures are swallowed; bugs stay visible."""
    _hass, manager, entity = _make_select()
    manager.api.async_get_sequences = _responder(TypeError("programming error"))

    with pytest.raises(TypeError):
        await entity.async_update()


@pytest.mark.asyncio
async def test_selecting_an_option_that_cannot_be_sent_raises_and_does_not_stick():
    from homeassistant.exceptions import HomeAssistantError

    _hass, manager, entity = _make_select()
    manager.api.async_get_sequences = _responder(SEQUENCES)
    await entity.async_added_to_hass()

    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("1: Water plants")

    assert entity.current_option is None
    assert manager.selected_sequence is None
