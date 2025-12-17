"""Number platform for Lionel Train Controller integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SOUND_SOURCE_BELL,
    SOUND_SOURCE_ENGINE,
    SOUND_SOURCE_HORN,
    SOUND_SOURCE_SPEECH,
)

if TYPE_CHECKING:
    from . import LionelTrainCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Lionel Train number platform."""
    coordinator: LionelTrainCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    name = config_entry.data[CONF_NAME]

    async_add_entities(
        [
            LionelTrainThrottle(coordinator, name),
            LionelTrainMasterVolume(coordinator, name),
            LionelTrainSoundVolume(
                coordinator, name, "Horn Volume", "mdi:bullhorn", SOUND_SOURCE_HORN
            ),
            LionelTrainSoundVolume(
                coordinator, name, "Bell Volume", "mdi:bell", SOUND_SOURCE_BELL
            ),
            LionelTrainSoundVolume(
                coordinator, name, "Speech Volume", "mdi:account-voice", SOUND_SOURCE_SPEECH
            ),
            LionelTrainSoundVolume(
                coordinator, name, "Engine Volume", "mdi:train", SOUND_SOURCE_ENGINE
            ),
        ],
        True,
    )


class LionelNumberBase(NumberEntity):
    """Base class for Lionel Train number entities."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: LionelTrainCoordinator, device_name: str) -> None:
        """Initialize the number entity."""
        self._coordinator = coordinator
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.mac_address)},
            "name": device_name,
            **coordinator.device_info,
        }

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        self.async_on_remove(
            self._coordinator.add_update_callback(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._coordinator.connected


class LionelTrainThrottle(LionelNumberBase):
    """Representation of a Lionel Train throttle as a number entity."""

    _attr_name = "Throttle"
    _attr_icon = "mdi:train"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: LionelTrainCoordinator, device_name: str) -> None:
        super().__init__(coordinator, device_name)
        self._attr_unique_id = f"{coordinator.mac_address}_throttle"

    @property
    def native_value(self) -> float | None:
        return self._coordinator.speed

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_speed(int(value))


class LionelTrainMasterVolume(LionelNumberBase):
    """Representation of master volume control."""

    _attr_name = "Master Volume"
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 0
    _attr_native_max_value = 7
    _attr_native_step = 1

    def __init__(self, coordinator: LionelTrainCoordinator, device_name: str) -> None:
        super().__init__(coordinator, device_name)
        self._attr_unique_id = f"{coordinator.mac_address}_master_volume"

    @property
    def native_value(self) -> float | None:
        return self._coordinator.master_volume

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_master_volume(int(value))


class LionelTrainSoundVolume(LionelNumberBase):
    """Generic class for individual sound source volumes."""

    _attr_native_min_value = 0
    _attr_native_max_value = 7
    _attr_native_step = 1

    def __init__(
        self,
        coordinator: LionelTrainCoordinator,
        device_name: str,
        name: str,
        icon: str,
        source_id: int,
    ) -> None:
        """Initialize the sound volume entity."""
        super().__init__(coordinator, device_name)
        self._source_id = source_id
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{coordinator.mac_address}_{name.lower().replace(' ', '_')}"

    @property
    def native_value(self) -> float | None:
        if self._source_id == SOUND_SOURCE_HORN:
            return self._coordinator.horn_volume
        if self._source_id == SOUND_SOURCE_BELL:
            return self._coordinator.bell_volume
        if self._source_id == SOUND_SOURCE_SPEECH:
            return self._coordinator.speech_volume
        if self._source_id == SOUND_SOURCE_ENGINE:
            return self._coordinator.engine_volume
        return 0

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_sound_volume(self._source_id, int(value))
