"""Tests for the ring and refresh location buttons."""
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.exceptions import HomeAssistantError

from custom_components.smarttags_nextgen.api import operation_status
from custom_components.smarttags_nextgen.const import DOMAIN

from .common import TAG_A, create_entry

FIND = "https://smartthingsfind.samsung.com"
ADD = f"{FIND}/dm/addOperation.do"
RESULT = f"{FIND}/dm/getOperationResult.do"


async def _setup(hass, mock_devices):
    mock_devices.append(TAG_A)
    entry = create_entry(hass, unique_id="12345")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.api.csrf_token = "token"
    return entry


async def _press(hass, entity_id):
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


async def test_buttons_created(hass, mock_devices):
    await _setup(hass, mock_devices)
    assert hass.states.get("button.keys_ring").attributes["friendly_name"] == "Keys Ring"
    assert hass.states.get("button.keys_refresh_location") is not None


async def test_ring(hass, mock_devices, aioclient_mock):
    await _setup(hass, mock_devices)
    aioclient_mock.post(ADD, json={"resultCode": "00", "reqId": "r1"})

    await _press(hass, "button.keys_ring")

    ((_, url, data, _),) = aioclient_mock.mock_calls
    assert url.query["_csrf"] == "token"
    assert data == {"dvceId": "a", "operation": "RING", "usrId": 12345, "status": "start"}


async def test_ring_rejected(hass, mock_devices, aioclient_mock):
    await _setup(hass, mock_devices)
    aioclient_mock.post(ADD, json={"resultCode": "99"})
    with pytest.raises(HomeAssistantError) as err:
        await _press(hass, "button.keys_ring")
    assert err.value.translation_key == "operation_failed"
    assert "RING" in err.value.translation_placeholders["error"]


async def test_ring_expired_session_starts_reauth(hass, mock_devices, aioclient_mock):
    entry = await _setup(hass, mock_devices)
    aioclient_mock.post(ADD, status=401)
    with pytest.raises(HomeAssistantError) as err:
        await _press(hass, "button.keys_ring")
    assert err.value.translation_key == "session_expired"
    await hass.async_block_till_done()
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == SOURCE_REAUTH
    assert flow["context"]["entry_id"] == entry.entry_id


async def test_refresh_location(hass, mock_devices, aioclient_mock):
    await _setup(hass, mock_devices)
    aioclient_mock.post(ADD, json={"resultCode": "00", "reqId": "r1"})
    aioclient_mock.post(
        RESULT,
        json={
            "operation": [
                # results of other requests are ignored
                {"oprnType": "LOCATION", "reqId": "older", "oprnStsCd": "9000"},
                {"oprnType": "LOCATION", "reqId": "r1", "oprnStsCd": "2800", "oprnResultCode": "1200"},
            ]
        },
    )
    fetch_state = mock_devices_state_fetch(hass)
    calls_before = fetch_state.await_count

    with patch("custom_components.smarttags_nextgen.coordinator.LOCATION_POLL_INTERVAL", 0):
        await _press(hass, "button.keys_refresh_location")
        # the location is awaited in a background task
        await hass.async_block_till_done(wait_background_tasks=True)

    add, result = aioclient_mock.mock_calls
    assert add[2] == {"dvceId": "a", "operation": "LOCATION", "usrId": 12345}
    assert result[2] == {"dvceId": "a", "operation": ["LOCATION"], "userId": 12345}
    # the tag state was fetched again once the location arrived
    assert fetch_state.await_count > calls_before


async def test_refresh_location_times_out(hass, mock_devices, aioclient_mock):
    await _setup(hass, mock_devices)
    aioclient_mock.post(ADD, json={"resultCode": "00", "reqId": "r1"})
    aioclient_mock.post(RESULT, json={"operation": [{"oprnType": "LOCATION", "reqId": "r1", "oprnStsCd": "1100"}]})
    fetch_state = mock_devices_state_fetch(hass)
    calls_before = fetch_state.await_count

    with (
        patch("custom_components.smarttags_nextgen.coordinator.LOCATION_POLL_INTERVAL", 0),
        patch("custom_components.smarttags_nextgen.coordinator.LOCATION_TIMEOUT", 0.05),
    ):
        await _press(hass, "button.keys_refresh_location")
        # the location is awaited in a background task
        await hass.async_block_till_done(wait_background_tasks=True)

    assert len(aioclient_mock.mock_calls) >= 2
    # the entities are still updated with whatever the tag reported
    assert fetch_state.await_count > calls_before


def mock_devices_state_fetch(hass):
    """The mocked set_last_select of the mock_devices fixture."""
    from custom_components.smarttags_nextgen.api import SmartTagsAPI

    return SmartTagsAPI.set_last_select


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"oprnStsCd": "2800", "oprnResultCode": "1200"}, "success"),
        ({"oprnStsCd": "200"}, "success"),
        ({"oprnStsCd": "1100"}, "pending"),
        ({"oprnStsCd": "2100"}, "pending"),
        ({}, "pending"),
        ({"oprnStsCd": "2800", "oprnResultCode": "9999"}, "failed"),
        ({"oprnStsCd": "9000"}, "failed"),
    ],
)
def test_operation_status(entry, expected):
    assert operation_status(entry) == expected
