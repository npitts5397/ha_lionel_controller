"""Switch platform for Lionel Train Controller integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

if TYPE_CHECKING:
    from . import LionelTrainCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Lionel Train switch platform."""
    coordinator: LionelTrainCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    name = config_entry.data[CONF_NAME]

    switches = [
        LionelTrainLightsSwitch(coordinator, name),
        LionelTrainHornSwitch(coordinator, name),
        LionelTrainBellSwitch(coordinator, name),
        LionelTrainSmokeSwitch(coordinator, name),
    ]

    async_add_entities(switches, True)


class LionelTrainSwitchBase(SwitchEntity):
    """Base class for Lionel Train switches."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LionelTrainCoordinator, device_name: str) -> None:
        """Initialize the switch."""
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


class LionelTrainLightsSwitch(LionelTrainSwitchBase):
    """Switch for controlling train lights."""

    _attr_name = "Headlight"
    _attr_icon = "mdi:car-light-high"

    def __init__(self, coordinator: LionelTrainCoordinator, device_name: str) -> None:
        super().__init__(coordinator, device_name)
        self._attr_unique_id = f"{coordinator.mac_address}_lights"

    @property
    def is_on(self) -> bool:
        return self._coordinator.lights_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_lights(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_lights(False)


class LionelTrainHornSwitch(LionelTrainSwitchBase):
    """Switch for controlling train horn."""

    _attr_name = "Horn"
    _attr_icon = "mdi:bugle"

    def __init__(self, coordinator: LionelTrainCoordinator, device_name: str) -> None:
        super().__init__(coordinator, device_name)
        self._attr_unique_id = f"{coordinator.mac_address}_horn"

    @property
    def is_on(self) -> bool:
        return self._coordinator.horn_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_horn(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_horn(False)


class LionelTrainBellSwitch(LionelTrainSwitchBase):
    """Switch for controlling train bell."""

    _attr_name = "Bell"
    _attr_icon = "mdi:bell"

    def __init__(self, coordinator: LionelTrainCoordinator, device_name: str) -> None:
        super().__init__(coordinator, device_name)
        self._attr_unique_id = f"{coordinator.mac_address}_bell"

    @property
    def is_on(self) -> bool:
        return self._coordinator.bell_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_bell(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_bell(False)


class LionelTrainSmokeSwitch(LionelTrainSwitchBase):
    """Switch for controlling train smoke generator."""

    _attr_name = "Smoke Generator"
    _attr_icon = "mdi:smoke"

    def __init__(self, coordinator: LionelTrainCoordinator, device_name: str) -> None:
        super().__init__(coordinator, device_name)
        self._attr_unique_id = f"{coordinator.mac_address}_smoke"

    @property
    def is_on(self) -> bool:
        return self._coordinator.smoke_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_smoke(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_smoke(False)
