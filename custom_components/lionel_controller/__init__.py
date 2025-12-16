"""The Lionel Train Controller integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from bleak import BleakError
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    BluetoothChange,
    BluetoothScanningMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CMD_MASTER_VOLUME,
    CMD_SMOKE,
    CMD_SOUND_VOLUME,
    CONF_MAC_ADDRESS,
    CONF_SERVICE_UUID,
    DEFAULT_RETRY_COUNT,
    DEFAULT_TIMEOUT,
    DEVICE_INFO_SERVICE_UUID,
    DOMAIN,
    FIRMWARE_REVISION_CHAR_UUID,
    HARDWARE_REVISION_CHAR_UUID,
    LIONCHIEF_SERVICE_UUID,
    MANUFACTURER_NAME_CHAR_UUID,
    MODEL_NUMBER_CHAR_UUID,
    NOTIFY_CHARACTERISTIC_UUID,
    SERIAL_NUMBER_CHAR_UUID,
    SOFTWARE_REVISION_CHAR_UUID,
    SOUND_SOURCE_BELL,
    SOUND_SOURCE_ENGINE,
    SOUND_SOURCE_HORN,
    SOUND_SOURCE_SPEECH,
    WRITE_CHARACTERISTIC_UUID,
    build_command,
    build_simple_command,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]


@callback
def _async_discovered_device(
    service_info: BluetoothServiceInfoBleak, change: BluetoothChange
) -> bool:
    """Check if discovered device is a Lionel LionChief locomotive."""
    if change != BluetoothChange.ADVERTISEMENT:
        return False

    lionel_service_uuid = LIONCHIEF_SERVICE_UUID.lower()
    return any(
        service_uuid.lower() == lionel_service_uuid
        for service_uuid in service_info.service_uuids
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lionel Train Controller from a config entry."""
    # Normalize MAC for consistent address matching
    mac_address = entry.data[CONF_MAC_ADDRESS].upper()
    name = entry.data[CONF_NAME]
    service_uuid = entry.data[CONF_SERVICE_UUID]

    coordinator = LionelTrainCoordinator(hass, mac_address, name, service_uuid)

    @callback
    def _async_update_ble(
        service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        """Update from a Bluetooth advertisement."""
        coordinator.async_set_ble_device(service_info, change)

    # Listen for this device's advertisements
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            {"address": mac_address},
            BluetoothScanningMode.ACTIVE,
        )
    )

    # Initial setup: do not fail the entry if train is off
    try:
        await coordinator.async_setup()
        _LOGGER.info("Successfully connected to Lionel train at %s", mac_address)
    except (BleakError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Initial connection failed: %s", err)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    async def reload_integration_service(call):
        """Service to reload the integration for manual recovery."""
        _LOGGER.info("Reloading Lionel integration via service call")
        await hass.config_entries.async_reload(entry.entry_id)

    if not hass.services.has_service(DOMAIN, "reload_integration"):
        hass.services.async_register(DOMAIN, "reload_integration", reload_integration_service)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: LionelTrainCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok


class LionelTrainCoordinator:
    """Coordinator for managing the Lionel train connection."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac_address: str,
        name: str,
        service_uuid: str,
    ) -> None:
        """Initialize the coordinator."""
        self.hass = hass
        self.mac_address = mac_address
        self.name = name
        self.service_uuid = service_uuid

        self._client: BleakClientWithServiceCache | None = None
        self._connected = False
        self._lock = asyncio.Lock()
        self._retry_count = 0
        self._update_callbacks: set[callable] = set()

        # Start very negative so first reconnect isn't throttled
        self._last_reconnect_attempt = -100.0

        # BLE device cache from latest advertisement
        self._client_ble_device = None

        # State tracking
        self._speed = 0
        self._direction_forward = True
        self._lights_on = True
        self._horn_on = False
        self._bell_on = False

        # Advanced feature state
        self._master_volume = 5
        self._horn_volume = 5
        self._bell_volume = 5
        self._speech_volume = 5
        self._engine_volume = 5
        self._horn_pitch = 0
        self._bell_pitch = 0
        self._speech_pitch = 0
        self._engine_pitch = 0

        self._smoke_on = False

        # Device info
        self._model_number = None
        self._serial_number = None
        self._firmware_revision = None
        self._hardware_revision = None
        self._software_revision = None
        self._manufacturer_name = None

        # Dynamic characteristic discovery
        self._discovered_write_char = None
        self._discovered_notify_char = None
        self._discovered_lionchief_service = None

        # Status info
        self._last_notification_hex: str | None = None

    # -------------------------------------------------------------------------
    # BLE advertisement handling (Option A: advertisement-driven reconnect)
    # -------------------------------------------------------------------------

    def async_set_ble_device(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Update the BLE device from an advertisement and trigger reconnect if needed."""
        if change != BluetoothChange.ADVERTISEMENT:
            return

        # Cache the most recent BLEDevice
        self._client_ble_device = service_info.device

        # If we think we're connected, but client isn't, fix state
        if self._connected and (self._client is None or not self._client.is_connected):
            _LOGGER.debug(
                "State desync detected: _connected=True but client is not connected. Resetting state."
            )
            self._connected = False

        # If we receive a connectable advertisement while we think we're connected,
        # and the client disagrees, treat this as a ghost and reset.
        if (
            self._connected
            and service_info.connectable
            and (self._client is None or not self._client.is_connected)
        ):
            _LOGGER.debug(
                "Connectable advertisement while client not connected -> ghost connection. Resetting."
            )
            self._connected = False

        # If already connected and client agrees, do nothing.
        if self.connected:
            return

        # Throttle reconnect attempts
        if self._lock.locked():
            return

        now = self.hass.loop.time()
        if now - self._last_reconnect_attempt < 5.0:
            return

        self._last_reconnect_attempt = now

        # Only try auto-reconnect when the advertisement says we are connectable
        if service_info.connectable:
            _LOGGER.debug(
                "Connectable advertisement received for %s. Attempting auto-reconnect...",
                self.mac_address,
            )
            self.hass.async_create_task(self.async_connect(service_info.device))

    # -------------------------------------------------------------------------
    # Connection state and helpers
    # -------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Return True if connected to the train."""
        return self._connected and self._client is not None and self._client.is_connected

    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Handle disconnection event."""
        _LOGGER.warning("🚂 Disconnected from Lionel train!")
        self._connected = False
        self._notify_state_change()

    # -------------------------------------------------------------------------
    # Public API for setup/shutdown/connection
    # -------------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Set up the coordinator (attempt initial connection)."""
        try:
            await self.async_connect()
        except (BleakError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Initial connection failed during setup: %s", err)

    async def async_shutdown(self) -> None:
        """Shut down the coordinator."""
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self._connected = False

    async def async_connect(self, specific_ble_device=None) -> None:
        """Public method to connect. Acquires lock, then calls internal logic."""
        async with self._lock:
            await self._connect_internal(specific_ble_device)

    async def _connect_internal(self, specific_ble_device=None) -> None:
        """Internal connection logic. ASSUMES LOCK IS ALREADY HELD."""
        if self.connected:
            return

        # Preferred: specific BLEDevice from fresh advertisement
        ble_device = specific_ble_device

        # Next: last cached BLEDevice from advertisement callback
        if ble_device is None:
            ble_device = self._client_ble_device

        # If we have a device but it is not connectable, treat it as stale
        if ble_device is not None and not getattr(ble_device, "connectable", False):
            _LOGGER.debug(
                "Ignoring non-connectable BLEDevice from cache (likely stale proxy entry)."
            )
            ble_device = None

        # Fallback: ask HA for a fresh, connectable BLEDevice
        if ble_device is None:
            _LOGGER.debug(
                "No fresh BLEDevice available, querying HA for connectable device..."
            )
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass,
                self.mac_address,
                connectable=True,  # critical: must be True to avoid stale devices
            )

        if ble_device is None:
            raise BleakError(
                f"Could not find connectable Bluetooth device with address {self.mac_address}"
            )

        try:
            _LOGGER.debug("Establishing connection to %s", self.mac_address)
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self.mac_address,
                max_attempts=3,
                disconnected_callback=self._on_disconnected,
            )

            # Read device information if available
            await self._read_device_info()

            # Log services/characteristics once per connection for debugging
            await self._log_ble_characteristics()

            # Set up notification handler for status updates
            try:
                notify_char_uuid = NOTIFY_CHARACTERISTIC_UUID
                await self._client.start_notify(
                    notify_char_uuid, self._notification_handler
                )
                _LOGGER.info("📡 Set up notifications on %s", notify_char_uuid)
            except BleakError as err:
                _LOGGER.debug(
                    "Could not set up notifications (train may not support them): %s", err
                )

            self._connected = True
            self._retry_count = 0
            _LOGGER.info("Connected to Lionel train at %s", self.mac_address)

            self._notify_state_change()

        except BleakError as err:
            _LOGGER.error("Failed to connect to train: %s", err)
            self._connected = False
            raise

    # -------------------------------------------------------------------------
    # Notification handling
    # -------------------------------------------------------------------------

    async def _notification_handler(self, sender: int, data: bytearray) -> None:
        """Handle notifications from the train."""
        _LOGGER.debug("Received notification: %s", data.hex())

        # Store the raw notification hex string
        self._last_notification_hex = data.hex()

        # Parse locomotive status data based on protocol analysis
        if len(data) >= 8 and data[0] == 0x00 and data[1] == 0x81 and data[2] == 0x02:
            # [0x00, 0x81, 0x02, speed, direction, 0x03, 0x0C, flags]
            try:
                self._speed = int((data[3] / 31) * 100)  # 0–31 -> 0–100%
                self._direction_forward = data[4] == 0x01

                flags = data[7]
                self._lights_on = (flags & 0x04) != 0
                self._bell_on = (flags & 0x02) != 0

                _LOGGER.debug(
                    "Parsed train status: speed=%d%%, forward=%s, lights=%s, bell=%s",
                    self._speed,
                    self._direction_forward,
                    self._lights_on,
                    self._bell_on,
                )

                self._notify_state_change()

            except (IndexError, ValueError) as err:
                _LOGGER.debug("Error parsing train status: %s", err)
        else:
            # For any notification, notify state change to update hex sensor
            self._notify_state_change()

    # -------------------------------------------------------------------------
    # Device info / characteristics logging
    # -------------------------------------------------------------------------

    async def _read_device_info(self) -> None:
        """Read device information characteristics."""
        device_info_chars = {
            MODEL_NUMBER_CHAR_UUID: "_model_number",
            SERIAL_NUMBER_CHAR_UUID: "_serial_number",
            FIRMWARE_REVISION_CHAR_UUID: "_firmware_revision",
            HARDWARE_REVISION_CHAR_UUID: "_hardware_revision",
            SOFTWARE_REVISION_CHAR_UUID: "_software_revision",
            MANUFACTURER_NAME_CHAR_UUID: "_manufacturer_name",
        }

        for char_uuid, attr_name in device_info_chars.items():
            try:
                result = await self._client.read_gatt_char(char_uuid)
                value = result.decode("utf-8", errors="ignore").strip()
                if value:
                    setattr(self, attr_name, value)
                    _LOGGER.debug("Read %s: %s", attr_name, value)
            except BleakError:
                _LOGGER.debug("Could not read characteristic %s", char_uuid)

    async def _log_ble_characteristics(self) -> None:
        """Log all BLE services and characteristics for debugging and discover dynamic characteristics."""
        try:
            _LOGGER.debug("=== BLE Service Discovery for %s ===", self.mac_address)

            services = self._client.services
            service_list = list(services)
            _LOGGER.debug("Found %d services", len(service_list))

            self._discovered_write_char = None
            self._discovered_notify_char = None
            self._discovered_lionchief_service = None

            service_count = 0
            for service in service_list:
                service_count += 1
                _LOGGER.debug(
                    "Service %d: %s (UUID: %s)",
                    service_count,
                    service.description,
                    service.uuid,
                )

                is_potential_lionchief = (
                    str(service.uuid).lower()
                    not in [
                        "0000180a-0000-1000-8000-00805f9b34fb",  # Device Information
                        "0000180f-0000-1000-8000-00805f9b34fb",  # Battery Service
                        "00001800-0000-1000-8000-00805f9b34fb",  # Generic Access
                        "00001801-0000-1000-8000-00805f9b34fb",  # Generic Attribute
                    ]
                )

                char_count = 0
                for char in service.characteristics:
                    char_count += 1
                    properties = []
                    has_write = False
                    has_notify = False

                    if "read" in char.properties:
                        properties.append("READ")
                    if "write" in char.properties:
                        properties.append("WRITE")
                        has_write = True
                    if "write-without-response" in char.properties:
                        properties.append("WRITE-NO-RESP")
                        has_write = True
                    if "notify" in char.properties:
                        properties.append("NOTIFY")
                        has_notify = True
                    if "indicate" in char.properties:
                        properties.append("INDICATE")
                        has_notify = True

                    _LOGGER.debug(
                        "  Char %d: %s (UUID: %s) [%s]",
                        char_count,
                        char.description,
                        char.uuid,
                        ", ".join(properties),
                    )

                    if is_potential_lionchief:
                        if has_write and not self._discovered_write_char:
                            self._discovered_write_char = str(char.uuid)
                            _LOGGER.debug(
                                "    *** POTENTIAL LIONCHIEF WRITE CHARACTERISTIC ***"
                            )
                        if has_notify and not self._discovered_notify_char:
                            self._discovered_notify_char = str(char.uuid)
                            _LOGGER.debug(
                                "    *** POTENTIAL LIONCHIEF NOTIFY CHARACTERISTIC ***"
                            )

                        if has_write or has_notify:
                            self._discovered_lionchief_service = str(service.uuid)

                    if "read" in char.properties:
                        try:
                            _LOGGER.debug("    Attempting to read characteristic value...")
                            value = await self._client.read_gatt_char(char.uuid)
                            if value and len(value) <= 50:
                                try:
                                    decoded = value.decode("utf-8").strip("\x00")
                                    _LOGGER.debug("    Value (text): '%s'", decoded)
                                except UnicodeDecodeError:
                                    _LOGGER.debug("    Value (hex): %s", value.hex())
                            elif value:
                                _LOGGER.debug(
                                    "    Value: <large data, %d bytes>", len(value)
                                )
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.debug("    Could not read value: %s", err)

                _LOGGER.debug("  Found %d characteristics in this service", char_count)

            _LOGGER.debug("=== End BLE Service Discovery ===")

            if self._discovered_lionchief_service:
                _LOGGER.debug(
                    "🎯 DISCOVERED LIONCHIEF SERVICE: %s",
                    self._discovered_lionchief_service,
                )
            if self._discovered_write_char:
                _LOGGER.debug(
                    "🎯 DISCOVERED WRITE CHARACTERISTIC: %s",
                    self._discovered_write_char,
                )
            if self._discovered_notify_char:
                _LOGGER.debug(
                    "🎯 DISCOVERED NOTIFY CHARACTERISTIC: %s",
                    self._discovered_notify_char,
                )

            if (
                self._discovered_write_char
                and self._discovered_write_char != WRITE_CHARACTERISTIC_UUID
            ):
                _LOGGER.info(
                    "💡 Consider updating WRITE_CHARACTERISTIC_UUID to: %s",
                    self._discovered_write_char,
                )
            if (
                self._discovered_notify_char
                and self._discovered_notify_char != NOTIFY_CHARACTERISTIC_UUID
            ):
                _LOGGER.info(
                    "💡 Consider updating NOTIFY_CHARACTERISTIC_UUID to: %s",
                    self._discovered_notify_char,
                )

        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error during BLE service discovery: %s", err)
            import traceback

            _LOGGER.error("Full traceback: %s", traceback.format_exc())

    # -------------------------------------------------------------------------
    # Command sending
    # -------------------------------------------------------------------------

    async def async_send_command(self, command_data: list[int]) -> bool:
        """Send a command to the train."""
        async with self._lock:
            if not self.connected:
                try:
                    await self._connect_internal()
                except BleakError as err:
                    _LOGGER.error("Failed to connect before sending command: %s", err)
                    return False

            write_char_uuid = WRITE_CHARACTERISTIC_UUID

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    await self._client.write_gatt_char(
                        write_char_uuid, bytearray(command_data)
                    )
                    hex_string = "".join(f"{b:02x}" for b in command_data)
                    _LOGGER.info(
                        "✅ Sent command successfully to %s: %s (hex: %s)",
                        write_char_uuid,
                        command_data,
                        hex_string,
                    )

                    self._last_notification_hex = hex_string
                    self._notify_state_change()
                    return True

                except BleakError as err:
                    _LOGGER.warning(
                        "Failed to send command to %s (attempt %d/%d): %s",
                        write_char_uuid,
                        attempt + 1,
                        max_retries,
                        err,
                    )
                    self._connected = False

                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        try:
                            await self._connect_internal()
                        except BleakError:
                            _LOGGER.debug(
                                "Reconnection attempt %d failed", attempt + 1
                            )
                            continue
                    else:
                        _LOGGER.error(
                            "Failed to send command after %d attempts: %s",
                            max_retries,
                            err,
                        )

            return False

    # -------------------------------------------------------------------------
    # Public control methods
    # -------------------------------------------------------------------------

    async def async_set_speed(self, speed: int) -> bool:
        """Set train speed (0-100)."""
        if not 0 <= speed <= 100:
            raise ValueError("Speed must be between 0 and 100")

        hex_speed = int((speed / 100) * 31)
        command = build_simple_command(0x45, [hex_speed])

        success = await self.async_send_command(command)
        if success:
            self._speed = speed
            self._notify_state_change()
        return success

    async def async_set_direction(self, forward: bool) -> bool:
        """Set train direction."""
        direction_value = 0x01 if forward else 0x02
        command = build_simple_command(0x46, [direction_value])

        success = await self.async_send_command(command)
        if success:
            self._direction_forward = forward
        return success

    async def async_set_lights(self, on: bool) -> bool:
        """Set train lights."""
        command = build_simple_command(0x51, [0x01 if on else 0x00])
        success = await self.async_send_command(command)
        if success:
            self._lights_on = on
        return success

    async def async_set_horn(self, on: bool) -> bool:
        """Set train horn."""
        command = build_simple_command(0x48, [0x01 if on else 0x00])
        success = await self.async_send_command(command)
        if success:
            self._horn_on = on
        return success

    async def async_set_bell(self, on: bool) -> bool:
        """Set train bell."""
        command = build_simple_command(0x47, [0x01 if on else 0x00])
        success = await self.async_send_command(command)
        if success:
            self._bell_on = on
        return success

    async def async_play_announcement(self, announcement_code: int) -> bool:
        """Play announcement sound."""
        command = build_simple_command(0x4D, [announcement_code, 0x00])
        return await self.async_send_command(command)

    async def async_disconnect(self) -> bool:
        """Send a disconnect command to train (not BLE disconnect)."""
        command = build_simple_command(0x4B, [0x00, 0x00])
        return await self.async_send_command(command)

    async def async_force_reconnect(self) -> bool:
        """Force reconnection to the train."""
        _LOGGER.info("Force reconnecting to Lionel train at %s", self.mac_address)

        self._connected = False
        if self._client:
            try:
                if self._client.is_connected:
                    await self._client.disconnect()
                    _LOGGER.debug("Disconnected existing client")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Error disconnecting client (expected if already disconnected): %s",
                    err,
                )
            finally:
                self._client = None

        await asyncio.sleep(1.0)

        try:
            await self.async_connect()
            return True
        except BleakError as err:
            _LOGGER.error("Force reconnect failed: %s", err)
            return False

    # -------------------------------------------------------------------------
    # State exposure
    # -------------------------------------------------------------------------

    @property
    def speed(self) -> int:
        """Return current speed (0-100)."""
        return self._speed

    @property
    def direction_forward(self) -> bool:
        """Return True if direction is forward."""
        return self._direction_forward

    @property
    def lights_on(self) -> bool:
        """Return True if lights are on."""
        return self._lights_on

    @property
    def horn_on(self) -> bool:
        """Return True if horn is on."""
        return self._horn_on

    @property
    def bell_on(self) -> bool:
        """Return True if bell is on."""
        return self._bell_on

    @property
    def master_volume(self) -> int:
        """Return master volume (0-7)."""
        return self._master_volume

    @property
    def horn_volume(self) -> int:
        """Return horn volume (0-7)."""
        return self._horn_volume

    @property
    def bell_volume(self) -> int:
        """Return bell volume (0-7)."""
        return self._bell_volume

    @property
    def speech_volume(self) -> int:
        """Return speech volume (0-7)."""
        return self._speech_volume

    @property
    def engine_volume(self) -> int:
        """Return engine volume (0-7)."""
        return self._engine_volume

    @property
    def smoke_on(self) -> bool:
        """Return True if smoke unit is on."""
        return self._smoke_on

    @property
    def last_notification_hex(self) -> str | None:
        """Return the last notification hex string."""
        return self._last_notification_hex

    @property
    def device_info(self) -> dict:
        """Return device information."""
        return {
            "model": self._model_number or "LionChief Locomotive",
            "manufacturer": self._manufacturer_name or "Lionel",
            "sw_version": self._software_revision or "Unknown",
            "hw_version": self._hardware_revision or "Unknown",
            "serial_number": self._serial_number,
        }

    # -------------------------------------------------------------------------
    # Callbacks and state notification
    # -------------------------------------------------------------------------

    def add_update_callback(self, callback):
        """Add a callback to be called when the state changes."""
        self._update_callbacks.add(callback)

    def remove_update_callback(self, callback):
        """Remove a callback."""
        self._update_callbacks.discard(callback)

    def _notify_state_change(self):
        """Notify all registered callbacks of state changes."""
        for callback in list(self._update_callbacks):
            try:
                callback()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error calling update callback: %s", err)

    # -------------------------------------------------------------------------
    # Advanced feature control
    # -------------------------------------------------------------------------

    async def async_set_master_volume(self, volume: int) -> bool:
        """Set master volume (0-7)."""
        if not 0 <= volume <= 7:
            raise ValueError("Volume must be between 0 and 7")

        command = build_simple_command(CMD_MASTER_VOLUME, [volume])
        success = await self.async_send_command(command)
        if success:
            self._master_volume = volume
            self._notify_state_change()
        return success

    async def async_set_sound_volume(
        self, sound_source: int, volume: int, pitch: int | None = None
    ) -> bool:
        """Set volume and optionally pitch for specific sound source."""
        if not 0 <= volume <= 7:
            raise ValueError("Volume must be between 0 and 7")
        if pitch is not None and not -2 <= pitch <= 2:
            raise ValueError("Pitch must be between -2 and 2")

        if pitch is not None:
            command = build_simple_command(
                CMD_SOUND_VOLUME, [sound_source, volume, pitch & 0xFF]
            )
        else:
            command = build_simple_command(CMD_SOUND_VOLUME, [sound_source, volume])

        success = await self.async_send_command(command)

        if success:
            if sound_source == SOUND_SOURCE_HORN:
                self._horn_volume = volume
                if pitch is not None:
                    self._horn_pitch = pitch
            elif sound_source == SOUND_SOURCE_BELL:
                self._bell_volume = volume
                if pitch is not None:
                    self._bell_pitch = pitch
            elif sound_source == SOUND_SOURCE_SPEECH:
                self._speech_volume = volume
                if pitch is not None:
                    self._speech_pitch = pitch
            elif sound_source == SOUND_SOURCE_ENGINE:
                self._engine_volume = volume
                if pitch is not None:
                    self._engine_pitch = pitch

            self._notify_state_change()
        return success

    async def async_set_smoke(self, on: bool) -> bool:
        """Set smoke unit on/off."""
        command = build_simple_command(CMD_SMOKE, [0x01 if on else 0x00])
        success = await self.async_send_command(command)
        if success:
            self._smoke_on = on
            self._notify_state_change()
        return success
