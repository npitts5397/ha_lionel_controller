"""Config flow for Lionel Train Controller integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_MAC_ADDRESS,
    CONF_SERVICE_UUID,
    DEFAULT_SERVICE_UUID,
    DOMAIN,
    LIONCHIEF_SERVICE_UUID,
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Lionel Train Controller."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    # ------------------------------------------------------------------
    # Bluetooth auto-discovery path
    # ------------------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        if not any(
            uuid.lower() == LIONCHIEF_SERVICE_UUID.lower()
            for uuid in discovery_info.service_uuids
        ):
            return self.async_abort(reason="not_lionel_device")

        self._discovered_devices[discovery_info.address] = discovery_info

        device_name = discovery_info.name or f"Lionel Train ({discovery_info.address[-5:]})"
        self.context["title_placeholders"] = {"name": device_name}

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm auto-discovered device."""
        discovery_info = self._discovered_devices[self.unique_id]
        device_name = discovery_info.name
        if not device_name:
            mac_suffix = discovery_info.address[-5:].replace(":", "")
            device_name = f"Lionel Train {mac_suffix}"

        if user_input is not None:
            return self.async_create_entry(
                title=device_name,
                data={
                    CONF_MAC_ADDRESS: discovery_info.address,
                    CONF_NAME: device_name,
                    CONF_SERVICE_UUID: DEFAULT_SERVICE_UUID,
                },
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": device_name,
                "address": discovery_info.address,
            },
        )

    # ------------------------------------------------------------------
    # Manual / user-initiated path
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick from already-discovered LionChief devices."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()

            discovery_info = self._discovered_devices[address]
            device_name = discovery_info.name or f"Lionel Train {address[-5:].replace(':', '')}"

            return self.async_create_entry(
                title=device_name,
                data={
                    CONF_MAC_ADDRESS: address,
                    CONF_NAME: device_name,
                    CONF_SERVICE_UUID: DEFAULT_SERVICE_UUID,
                },
            )

        # Scan through what HA's Bluetooth subsystem has already seen
        current_addresses = self._async_current_ids(include_ignore=False)
        for discovery_info in async_discovered_service_info(self.hass, connectable=True):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            if any(
                uuid.lower() == LIONCHIEF_SERVICE_UUID.lower()
                for uuid in discovery_info.service_uuids
            ):
                self._discovered_devices[address] = discovery_info

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        device_names = {
            addr: info.name or f"Lionel Train ({addr[-5:]})"
            for addr, info in self._discovered_devices.items()
        }

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(device_names)}
            ),
        )
