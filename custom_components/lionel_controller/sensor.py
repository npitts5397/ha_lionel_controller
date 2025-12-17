"""Sensor platform for Lionel Train Controller integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
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
    """Set up the Lionel Train sensor platform."""
    coordinator: LionelTrainCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    name = config_entry.data[CONF_NAME]

    async_add_entities([LionelTrainStatusSensor(coordinator, name)], True)


class LionelTrainStatusSensor(SensorEntity):
    """Sensor for Lionel Train status information."""

    _attr_has_entity_name = True
    _attr_name = "Status"
    _attr_icon = "mdi:train"

    def __init__(self, coordinator: LionelTrainCoordinator, device_name: str) -> None:
        """Initialize the sensor."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.mac_address}_status"
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
    def native_value(self) -> str | None:
        """Return the state of the sensor."""
        hex_value = self._coordinator.last_notification_hex
        if hex_value is None:
            return "No data"
        return hex_value

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._coordinator.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        return {
            "speed": self._coordinator.speed,
            "direction_forward": self._coordinator.direction_forward,
            "lights_on": self._coordinator.lights_on,
            "bell_on": self._coordinator.bell_on,
            "horn_on": self._coordinator.horn_on,
        }
