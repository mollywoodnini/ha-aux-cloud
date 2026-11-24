"""Humidifier platform (dehumidifiers) for AUX Cloud."""

from __future__ import annotations

from typing import Any

from homeassistant.components.humidifier import (
    HumidifierDeviceClass,
    HumidifierEntity,
    HumidifierEntityDescription,
)
from homeassistant.components.humidifier.const import HumidifierEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import AuxDevice
from .const import DOMAIN
from .coordinator import AuxCloudConfigEntry, AuxCloudCoordinator
from .devices import (
    DH_AMBIENT_HUMIDITY,
    DH_DRYING_FAN_SPEED,
    DH_FAN_SPEED,
    DH_MODE,
    DH_MODE_CONTINUOUS,
    DH_MODE_DRYING,
    DH_MODE_MANUAL,
    DH_POWER,
    DH_TARGET_HUMIDITY,
    DeviceType,
    ProductProfile,
    get_device_profile,
)
from .entity import BaseEntity, setup_dynamic_entities

PARALLEL_UPDATES = 0

MODE_BY_VALUE = {
    DH_MODE_CONTINUOUS: "continuous",
    DH_MODE_MANUAL: "manual",
    DH_MODE_DRYING: "drying",
}
VALUE_BY_MODE = {mode: value for value, mode in MODE_BY_VALUE.items()}

DEHUMIDIFIER_DESCRIPTION = HumidifierEntityDescription(
    key=DH_POWER,
    translation_key="aux_dehumidifier",
    device_class=HumidifierDeviceClass.DEHUMIDIFIER,
    name=None,
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: AuxCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the AUX dehumidifier platform."""
    coordinator = entry.runtime_data

    def entities_for_device(device: AuxDevice) -> list[AuxDehumidifierEntity]:
        if get_device_profile(device).device_type is not DeviceType.DEHUMIDIFIER:
            return []
        return [
            AuxDehumidifierEntity(
                coordinator, device["endpointId"], DEHUMIDIFIER_DESCRIPTION
            )
        ]

    setup_dynamic_entities(entry, coordinator, async_add_entities, entities_for_device)


class AuxDehumidifierEntity(BaseEntity, HumidifierEntity):
    """AUX HOME dehumidifier."""

    _attr_supported_features = HumidifierEntityFeature.MODES

    def __init__(
        self,
        coordinator: AuxCloudCoordinator,
        device_id: str,
        entity_description: HumidifierEntityDescription,
    ) -> None:
        """Initialize limits and modes from the product profile."""
        super().__init__(coordinator, device_id, entity_description)
        profile: ProductProfile = get_device_profile(self._device)
        self._profile = profile
        self._attr_min_humidity = profile.min_humidity
        self._attr_max_humidity = profile.max_humidity
        self._attr_available_modes = [
            MODE_BY_VALUE[mode]
            for mode in profile.dehumidifier_modes
            if mode in MODE_BY_VALUE
        ]

    @property
    def is_on(self) -> bool | None:
        """Return whether the dehumidifier is running."""
        value = self._get_device_params().get(DH_POWER)
        return value == 1 if value is not None else None

    @property
    def target_humidity(self) -> float | None:
        """Return the target relative humidity."""
        value = self._get_device_params().get(DH_TARGET_HUMIDITY)
        return float(value) if isinstance(value, int) else None

    @property
    def current_humidity(self) -> float | None:
        """Return the measured relative humidity."""
        value = self._get_device_params().get(DH_AMBIENT_HUMIDITY)
        return float(value) if isinstance(value, int) else None

    @property
    def mode(self) -> str | None:
        """Return the active dehumidification mode."""
        value = self._get_device_params().get(DH_MODE)
        return MODE_BY_VALUE.get(value) if isinstance(value, int) else None

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the dehumidifier on."""
        await self._set_device_params({DH_POWER: 1})

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the dehumidifier off."""
        await self._set_device_params({DH_POWER: 0})

    async def async_set_humidity(self, humidity: int) -> None:
        """Set the target humidity, snapped to the device's step."""
        step = self._profile.humidity_step
        snapped = round(humidity / step) * step
        clamped = min(
            max(snapped, self._profile.min_humidity), self._profile.max_humidity
        )
        await self._set_device_params({DH_TARGET_HUMIDITY: clamped})

    async def async_set_mode(self, mode: str) -> None:
        """Select a dehumidification mode."""
        if mode not in VALUE_BY_MODE or mode not in (self._attr_available_modes or []):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_option",
                translation_placeholders={"option": mode},
            )
        command: dict[str, Any] = {DH_MODE: VALUE_BY_MODE[mode]}
        if VALUE_BY_MODE[mode] == DH_MODE_DRYING:
            # The AUX HOME app always sends the drying fan speed with this mode.
            command[DH_FAN_SPEED] = DH_DRYING_FAN_SPEED
        await self._set_device_params(command)
