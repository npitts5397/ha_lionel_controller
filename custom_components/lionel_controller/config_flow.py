"""Config flow for Lionel Train Controller integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_MAC_ADDRESS,
    CONF_SERVICE_UUID,
    DEFAULT_NAME,
    DEFAULT_SERVICE_UUID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MAC_ADDRESS): str,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Optional(CONF_SERVICE_UUID, default=DEFAULT_SERVICE_UUID): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    mac_address = data[CONF_MAC_ADDRESS].upper()
    
    # Validate MAC address format
    if not _is_valid_mac_address(mac_address):
        raise InvalidMacAddress

    # Use HA's shared cache instead of spawning a new scanner (which causes contention)
    device = bluetooth.async_ble_device_from_address(
        hass, mac_address, connectable=True
    )
    
    if not device:
        # Fallback search for non-connectable (just in case it's in a weird state)
        device = bluetooth.async_ble_device_from_address(
            hass, mac_address, connectable=False
        )

    if not device:
        raise CannotConnect

    return {
        "title": data[CONF_NAME],
        "mac_address": mac_address,
        "service_uuid": data[CONF_SERVICE_UUID],
    }


def _is_valid_mac_address(mac: str) -> bool:
    """Check if MAC address is valid."""
    parts = mac.split(":")
    if len(parts) != 6:
        return False
    
    for part in parts:
        if len(part) != 2:
            return False
        try:
            int(part, 16)
        except ValueError:
            return False
    return True


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Lionel Train Controller."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidMacAddress:
                errors[CONF_MAC_ADDRESS] = "invalid_mac"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["mac_address"])
                self._abort_if_unique_id_configured()
                
                return self.async_create_entry(title=info["title"], data={
                    CONF_MAC_ADDRESS: info["mac_address"],
                    CONF_NAME: info["title"],
                    CONF_SERVICE_UUID: info["service_uuid"],
                })

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        lionel_service_found = False
        for service_uuid in discovery_info.service_uuids:
            if service_uuid.lower() == DEFAULT_SERVICE_UUID.lower():
                lionel_service_found = True
                break
        
        if not lionel_service_found:
            return self.async_abort(reason="not_lionel_device")

        self._discovered_devices[discovery_info.address] = discovery_info
        
        self.context["title_placeholders"] = {
            "name": discovery_info.name or f"Lionel Train ({discovery_info.address[-5:]})"
        }
        
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        if user_input is not None:
            discovery_info = self._discovered_devices[self.unique_id]
            device_name = discovery_info.name
            if not device_name:
                mac_suffix = discovery_info.address[-5:].replace(":", "")
                device_name = f"Lionel Train {mac_suffix}"
            
            return self.async_create_entry(
                title=device_name,
                data={
                    CONF_MAC_ADDRESS: discovery_info.address,
                    CONF_NAME: device_name,
                    CONF_SERVICE_UUID: DEFAULT_SERVICE_UUID,
                },
            )

        discovery_info = self._discovered_devices[self.unique_id]
        device_name = discovery_info.name or f"Lionel Train ({discovery_info.address[-5:]})"
        
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": device_name,
                "address": discovery_info.address,
            },
        )

class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""

class InvalidMacAddress(HomeAssistantError):
    """Error to indicate there is invalid MAC address."""
