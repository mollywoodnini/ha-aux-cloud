"""AUX HOME dehumidifier profile, entities and backend wiring."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.humidifier import HumidifierDeviceClass
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aux_cloud.api.models import AuxCredentials
from custom_components.aux_cloud.coordinator import AuxCloudCoordinator
from custom_components.aux_cloud.devices import (
    DH_CHILD_LOCK,
    DH_FAN_SPEED,
    DH_FILTER_RUNTIME,
    DH_MODE,
    DH_POWER,
    DH_TARGET_HUMIDITY,
    DeviceType,
    get_product_profile,
)
from custom_components.aux_cloud.humidifier import (
    DEHUMIDIFIER_DESCRIPTION,
    AuxDehumidifierEntity,
)
from custom_components.aux_cloud.identifiers import account_unique_id_from_user_id
from custom_components.aux_cloud.select import SELECTS, AuxSelectEntity
from custom_components.aux_cloud.sensor import SENSORS
from custom_components.aux_cloud.switch import SWITCHES, AuxSwitchEntity

pytest_plugins = "pytest_homeassistant_custom_component"

PRODUCT_ID = "00010007"
DEVICE_ID = "d50001000700000000aa00"


def _device(**params):
    return {
        "endpointId": DEVICE_ID,
        "friendlyName": "Dehumidifier",
        "productId": PRODUCT_ID,
        "state": 1,
        "params": {
            DH_POWER: 1,
            DH_MODE: 3,
            DH_FAN_SPEED: 5,
            DH_TARGET_HUMIDITY: 50,
            DH_CHILD_LOCK: 0,
            "environment_humidity": 55,
            DH_FILTER_RUNTIME: 31,
            **params,
        },
    }


def _coordinator(hass, device, *, entry_data=None):
    api = MagicMock()
    api.set_device_params = AsyncMock(side_effect=lambda _device, params: params)
    entry = MockConfigEntry(
        domain="aux_cloud",
        data=entry_data
        or {"email": "user@example.com", "password": "secret", "region": "eu"},
    )
    entry.add_to_hass(hass)
    coordinator = AuxCloudCoordinator(hass, api, config_entry=entry)
    coordinator._state.reconcile(
        [device], complete=True, scan_revision=coordinator._state.revision
    )
    coordinator._publish_devices()
    return coordinator


def test_profile_describes_the_captured_device():
    """Parameter meanings come from the captured app protocol."""
    profile = get_product_profile(PRODUCT_ID)

    assert profile.device_type is DeviceType.DEHUMIDIFIER
    assert profile.model_name == "AUX Dehumidifier"
    assert profile.initial_param_queries(_device()) == []
    assert set(profile.writable_params) == {
        "onoff",
        "mode",
        "wind_speed",
        "humidity",
        "child_lock",
    }
    # This unit reports no pump or healthy support, so it must not get entities.
    assert "pump" not in profile.params
    assert "healthy" not in profile.params
    assert "support_function" in profile.query_tags


@pytest.mark.parametrize(
    ("values", "invalid"),
    [
        ({"mode": 1}, None),
        ({"mode": 5, "wind_speed": 5}, None),
        ({"mode": 2}, "mode"),
        ({"wind_speed": 2}, "wind_speed"),
        ({"humidity": 30}, None),
        ({"humidity": 80}, None),
        ({"humidity": 29}, "humidity"),
        ({"humidity": 81}, "humidity"),
        ({"humidity": 50.5}, "humidity"),
        ({"pump": 1}, "pump"),
    ],
)
def test_profile_validates_commands(values, invalid):
    """Only audited parameters and value ranges are accepted."""
    profile = get_product_profile(PRODUCT_ID)
    assert profile.invalid_command_parameter(values) == invalid


async def test_humidifier_entity_state_and_commands(hass):
    """The dehumidifier exposes cloud state and sends the app's commands."""
    coordinator = _coordinator(hass, _device())
    entity = AuxDehumidifierEntity(coordinator, DEVICE_ID, DEHUMIDIFIER_DESCRIPTION)

    assert entity.device_class is HumidifierDeviceClass.DEHUMIDIFIER
    assert entity.is_on is True
    assert entity.target_humidity == 50.0
    assert entity.current_humidity == 55.0
    assert entity.mode == "manual"
    assert entity.min_humidity == 30
    assert entity.max_humidity == 80
    assert entity.available_modes == ["continuous", "manual", "drying"]

    sent = coordinator.api.set_device_params
    await entity.async_turn_off()
    assert sent.await_args.args[1] == {"onoff": 0}
    await entity.async_turn_on()
    assert sent.await_args.args[1] == {"onoff": 1}
    await entity.async_set_mode("continuous")
    assert sent.await_args.args[1] == {"mode": 1}
    # Drying always travels with the high fan speed, like in the app.
    await entity.async_set_mode("drying")
    assert sent.await_args.args[1] == {"mode": 5, "wind_speed": 5}
    await entity.async_set_humidity(47)
    assert sent.await_args.args[1] == {"humidity": 45}
    await entity.async_set_humidity(99)
    assert sent.await_args.args[1] == {"humidity": 80}
    await entity.async_set_humidity(10)
    assert sent.await_args.args[1] == {"humidity": 30}
    with pytest.raises(HomeAssistantError):
        await entity.async_set_mode("turbo")


async def test_humidifier_entity_handles_missing_state(hass):
    """Unreported values stay unknown instead of guessed."""
    device = _device()
    device["params"] = {DH_FAN_SPEED: 1}
    coordinator = _coordinator(hass, device)
    entity = AuxDehumidifierEntity(coordinator, DEVICE_ID, DEHUMIDIFIER_DESCRIPTION)

    assert entity.is_on is None
    assert entity.target_humidity is None
    assert entity.current_humidity is None
    assert entity.mode is None


async def test_companion_entities(hass):
    """Fan speed select, child-lock switch and sensors follow the profile."""
    coordinator = _coordinator(hass, _device())
    fan = next(item for item in SELECTS if item.key == DH_FAN_SPEED)
    select = AuxSelectEntity(coordinator, DEVICE_ID, fan)
    assert select.options == ["low", "medium", "high"]
    assert select.current_option == "high"
    await select.async_select_option("medium")
    assert coordinator.api.set_device_params.await_args.args[1] == {"wind_speed": 3}

    lock = next(item for item in SWITCHES if item.key == DH_CHILD_LOCK)
    switch = AuxSwitchEntity(coordinator, DEVICE_ID, lock)
    assert switch.is_on is False
    await switch.async_turn_on()
    assert coordinator.api.set_device_params.await_args.args[1] == {"child_lock": 1}

    device = coordinator.get_device_by_endpoint_id(DEVICE_ID)
    assert SENSORS["environment_humidity"].value_fn(device) == 55
    assert SENSORS[DH_FILTER_RUNTIME].value_fn(device) == 31


def test_aux_home_accounts_are_namespaced_and_case_preserved():
    """AUX HOME IDs cannot collide with DNA ones; the email keeps its case."""
    assert account_unique_id_from_user_id("EU", "u1") == "eu:user:u1"
    assert account_unique_id_from_user_id("eu", "u1", "aux_home") == (
        "aux_home:eu:user:u1"
    )
    assert AuxCredentials.exact_email(" Aa@B.c ", "pw").username == "Aa@B.c"
    assert AuxCredentials.email(" Aa@B.c ", "pw").username == "aa@b.c"


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("aux_home", "User@Example.com"), ("dna", "user@example.com")],
)
async def test_coordinator_login_keeps_case_only_for_aux_home(hass, backend, expected):
    """Stored credentials are normalised per backend."""
    data = {
        "email": "User@Example.com",
        "password": "secret",
        "region": "eu",
        "backend": backend,
    }
    coordinator = _coordinator(hass, _device(), entry_data=data)
    coordinator.api.is_logged_in = MagicMock(return_value=False)
    coordinator.api.login = AsyncMock()

    await coordinator._async_ensure_login()

    credentials = coordinator.api.login.await_args.args[0]
    assert credentials.username == expected
