"""Binary sensor platform for Lionel Train Controller integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    """Set up the Lionel Train binary sensor platform."""
    coordinator: LionelTrainCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    name = config_entry.data[CONF_NAME]

    async_add_entities(
        [
            LionelTrainConnectionSensor(coordinator, name),
            LionelMovingSensor(coordinator, name),
        ],
        True,
    )


class LionelTrainConnectionSensor(BinarySensorEntity):
    """Binary sensor for Lionel Train connection status."""

    _attr_has_entity_name = True
    _attr_name = "Connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: LionelTrainCoordinator, device_name: str) -> None:
        """Initialize the binary sensor."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.mac_address}_connection"
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
    def is_on(self) -> bool:
        """Return True if the train is connected."""
        return self._coordinator.connected

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return True


class LionelMovingSensor(BinarySensorEntity):
    """Binary sensor indicating if the train is moving."""

    _attr_has_entity_name = True
    _attr_name = "Moving"
    _attr_device_class = BinarySensorDeviceClass.MOVING

    def __init__(self, coordinator: LionelTrainCoordinator, device_name: str) -> None:
        """Initialize the binary sensor."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.mac_address}_is_moving"
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
    def is_on(self) -> bool:
        """Return true if speed > 0."""
        return self._coordinator.speed > 0

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._coordinator.connected
