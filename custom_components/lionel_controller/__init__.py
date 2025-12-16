"""The Lionel Train Controller integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from bleak import BleakClient, BleakError
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, BluetoothChange, BluetoothScanningMode, async_register_callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, Platform, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
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

PLATFORMS: list[Platform] = [Platform.NUMBER, Platform.SWITCH, Platform.BUTTON, Platform.BINARY_SENSOR, Platform.SENSOR]


@callback
def _async_discovered_device(
    service_info: BluetoothServiceInfoBleak, change: BluetoothChange
) -> bool:
    """Check if discovered device is a Lionel LionChief locomotive."""
    if change != BluetoothChange.ADVERTISEMENT:
        return False
    
    # Check for Lionel LionChief service UUID
    lionel_service_uuid = LIONCHIEF_SERVICE_UUID.lower()
    return any(
        service_uuid.lower() == lionel_service_uuid
        for service_uuid in service_info.service_uuids
    )

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lionel Train Controller from a config entry."""
    # CRITICAL FIX 1: Normalize MAC to uppercase to ensure the listener filter matches
    mac_address = entry.data[CONF_MAC_ADDRESS].upper()
    name = entry.data[CONF_NAME]
    service_uuid = entry.data[CONF_SERVICE_UUID]

    coordinator = LionelTrainCoordinator(hass, mac_address, name, service_uuid)
    
    # --- LISTENER ---
    @callback
    def _async_update_ble(service_info: BluetoothServiceInfoBleak, change: BluetoothChange):
        """Update from a Bluetooth advertisement."""
        coordinator.async_set_ble_device(service_info)

    # Tell Home Assistant to listen for this device forever
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            {"address": mac_address},
            BluetoothScanningMode.ACTIVE  # Matches your aggressive Proxy settings
        )
    )

    # --- WATCHDOG & BOOT LOGIC ---
    
    # 1. Boot Retry: Retry connection once Home Assistant has fully started.
    async def _on_hass_start(event):
        if not coordinator.connected:
            _LOGGER.debug("HA Started: Attempting delayed connection to Lionel Train")
            await coordinator.async_setup()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_hass_start)
    )

    # 2. Watchdog: A safety net that checks every 30s if the listener missed something.
    async def _async_watchdog(now):
        if not coordinator.connected:
            # Pass None to force a cache lookup since we don't have a fresh packet here
            # Use the PUBLIC method (async_connect) which handles locking safely
            hass.async_create_task(coordinator.async_connect(None))
        
        # Reschedule self for 30 seconds later
        coordinator._watchdog_unsub = async_call_later(hass, 30.0, _async_watchdog)

    # Start the watchdog
    coordinator._watchdog_unsub = async_call_later(hass, 30.0, _async_watchdog)
    
    # Ensure watchdog stops on unload
    entry.async_on_unload(lambda: coordinator.cancel_watchdog())

    # --- INITIAL SETUP ---
    # Don't require initial connection - allow integration to load even if locomotive is off
    try:
        await coordinator.async_setup()
        _LOGGER.info("Successfully connected to Lionel train at %s", mac_address)
    except (BleakError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Initial connection failed: %s", err)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Register services
    async def reload_integration_service(call):
        """Service to reload the integration for better reconnection."""
        _LOGGER.info("Reloading integration via service call")
        await hass.config_entries.async_reload(entry.entry_id)
    
    # Register the service if not already registered
    if not hass.services.has_service(DOMAIN, "reload_integration"):
        hass.services.async_register(DOMAIN, "reload_integration", reload_integration_service)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
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
        self._update_callbacks = set()
        
        # Initialize to negative so throttle doesn't block immediately on fresh boot
        self._last_reconnect_attempt = -100.0
        
        # Internal references
        self._client_ble_device = None
        self._watchdog_unsub = None
        
        # State tracking
        self._speed = 0
        self._direction_forward = True
        self._lights_on = True  # Default to on since locomotive lights are on when reconnected
        self._horn_on = False
        self._bell_on = False
        
        # Advanced feature state tracking
        self._master_volume = 5  # Default mid-range volume
        self._horn_volume = 5
        self._bell_volume = 5
        self._speech_volume = 5
        self._engine_volume = 5
        self._horn_pitch = 0
        self._bell_pitch = 0
        self._speech_pitch = 0
        self._engine_pitch = 0
        
        self._smoke_on = False
        
        # Device information
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
        
        # Status information
        self._last_notification_hex = None

    def cancel_watchdog(self):
        """Stop the background watchdog timer."""
        if self._watchdog_unsub:
            self._watchdog_unsub()
            self._watchdog_unsub = None

    def async_set_ble_device(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Update the BLE device from an advertisement."""
        
        # 1. Update internal reference using the fresh advertisement
        self._client_ble_device = service_info.device

        # 2. Check for State Desync (Logic says connected, Client says disconnected)
        if self._connected and (self._client is None or not self._client.is_connected):
            _LOGGER.debug("🚂 State Desync: Logic says connected, Client says disconnected. Resetting.")
            self._connected = False

        # 3. Aggressive Ghost Check
        # If we see ANY advertisement while we think we are connected, force reset.
        if self._connected:
            _LOGGER.debug("🚂 Advertisement received while connected -> Ghost Connection detected. Resetting.")
            self._connected = False

        # 4. Connect if needed (Logic: Disconnected + Unlocked + Throttled)
        if not self.connected and not self._lock.locked():
            
            # THROTTLE: Check if it has been 5 seconds since the last try
            now = self.hass.loop.time()
            if now - self._last_reconnect_attempt > 5.0:
                self._last_reconnect_attempt = now
                
                _LOGGER.debug("🚂 Train found! Attempting auto-reconnect using fresh advertisement...")
                # Call the PUBLIC method which handles the lock
                self.hass.async_create_task(self.async_connect(service_info.device))

    @property
    def connected(self) -> bool:
        """Return True if connected to the train."""
        return self._connected and self._client is not None and self._client.is_connected

    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Handle disconnection event."""
        _LOGGER.warning("🚂 Disconnected from Lionel train!")
        self._connected = False
        self._notify_state_change()

    # --- Properties ---
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

    # Advanced feature properties
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

    def add_update_callback(self, callback):
        """Add a callback to be called when the state changes."""
        self._update_callbacks.add(callback)

    def remove_update_callback(self, callback):
        """Remove a callback."""
        self._update_callbacks.discard(callback)

    def _notify_state_change(self):
        """Notify all registered callbacks of state changes."""
        for callback in self._update_callbacks:
            try:
                callback()
            except Exception as err:
                _LOGGER.error("Error calling update callback: %s", err)

    async def async_setup(self) -> None:
        """Set up the coordinator."""
        # Use the public method to ensure locks are handled
        try:
            await self.async_connect()
        except (BleakError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Initial connection failed during setup: %s", err)

    async def async_shutdown(self) -> None:
        """Shut down the coordinator."""
        self.cancel_watchdog()  # Ensure timer stops
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self._connected = False

    # --- DEADLOCK FIX: Split Connection Logic ---

    async def async_connect(self, specific_ble_device=None) -> None:
        """Public method to connect. Acquires lock, then calls internal logic."""
        async with self._lock:
            await self._connect_internal(specific_ble_device)

    async def _connect_internal(self, specific_ble_device=None) -> None:
        """Internal connection logic. ASSUMES LOCK IS ALREADY HELD."""
        if self._connected:
            return

        # OPTION A: Use the specific fresh device passed from the advertisement
        ble_device = specific_ble_device

        # OPTION B: Use our internal fresh cache if Option A is missing
        if not ble_device:
             ble_device = self._client_ble_device

        # OPTION C: Fallback to HA Cache (connectable=False for safety)
        if not ble_device:
            _LOGGER.debug("No fresh device found, checking HA cache...")
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.mac_address, connectable=True
            )
        
        if not ble_device:
            raise BleakError(f"Could not find Bluetooth device with address {self.mac_address}")

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
            
            # Log all BLE services and characteristics for debugging
            await self._log_ble_characteristics()
            
            # Set up notification handler for status updates
            try:
                # Always use the known-good notify characteristic UUID
                notify_char_uuid = NOTIFY_CHARACTERISTIC_UUID
                await self._client.start_notify(
                    notify_char_uuid, self._notification_handler
                )
                _LOGGER.info("📡 Set up notifications on %s", notify_char_uuid)
            except BleakError as err:
                _LOGGER.debug("Could not set up notifications (train may not support them): %s", err)
            
            self._connected = True
            self._retry_count = 0
            _LOGGER.info("Successfully reconnected to train")
            
            # Notify all entities of the reconnection
            self._notify_state_change()

        except BleakError as err:
            _LOGGER.error("Failed to connect to train: %s", err)
            self._connected = False
            raise

    async def _notification_handler(self, sender: int, data: bytearray) -> None:
        """Handle notifications from the train."""
        _LOGGER.debug("Received notification: %s", data.hex())
        
        # Store the raw notification hex string
        self._last_notification_hex = data.hex()
        
        # Parse locomotive status data based on protocol analysis
        if len(data) >= 8 and data[0] == 0x00 and data[1] == 0x81 and data[2] == 0x02:
            # This is train status data: [0x00, 0x81, 0x02, speed, direction, 0x03, 0x0C, flags]
            try:
                self._speed = int((data[3] / 31) * 100)  # Convert 0-31 to 0-100%
                self._direction_forward = data[4] == 0x01
                
                # Parse flags byte (data[7])
                flags = data[7]
                self._lights_on = (flags & 0x04) != 0
                self._bell_on = (flags & 0x02) != 0
                
                _LOGGER.debug("Parsed train status: speed=%d%%, forward=%s, lights=%s, bell=%s", 
                             self._speed, self._direction_forward, self._lights_on, self._bell_on)
                
                # Notify entities of state change
                self._notify_state_change()
                
            except (IndexError, ValueError) as err:
                _LOGGER.debug("Error parsing train status: %s", err)
        else:
            # For any notification, notify state change to update the hex sensor
            self._notify_state_change()

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
                value = result.decode('utf-8', errors='ignore').strip()
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
            # Convert to list to get length safely
            service_list = list(services)
            _LOGGER.debug("Found %d services", len(service_list))
            
            # Store discovered characteristics for dynamic usage
            self._discovered_write_char = None
            self._discovered_notify_char = None
            self._discovered_lionchief_service = None
            
            service_count = 0
            for service in service_list:
                service_count += 1
                _LOGGER.debug("Service %d: %s (UUID: %s)", service_count, service.description, service.uuid)
                
                # Check if this might be the LionChief control service
                # Look for services with writable characteristics that aren't standard BLE services
                is_potential_lionchief = (
                    str(service.uuid).lower() not in [
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
                    
                    _LOGGER.debug("  Char %d: %s (UUID: %s) [%s]", 
                               char_count, char.description, char.uuid, ", ".join(properties))
                    
                    # Identify potential LionChief characteristics
                    if is_potential_lionchief:
                        if has_write and not self._discovered_write_char:
                            self._discovered_write_char = str(char.uuid)
                            _LOGGER.debug("    *** POTENTIAL LIONCHIEF WRITE CHARACTERISTIC ***")
                        if has_notify and not self._discovered_notify_char:
                            self._discovered_notify_char = str(char.uuid)
                            _LOGGER.debug("    *** POTENTIAL LIONCHIEF NOTIFY CHARACTERISTIC ***")
                        
                        if has_write or has_notify:
                            self._discovered_lionchief_service = str(service.uuid)
                    
                    # Try to read characteristics that support reading (with better error handling)
                    if "read" in char.properties:
                        try:
                            _LOGGER.debug("    Attempting to read characteristic value...")
                            value = await self._client.read_gatt_char(char.uuid)
                            if value and len(value) <= 50:  # Increased limit and null check
                                try:
                                    # Try to decode as string first
                                    decoded = value.decode('utf-8').strip('\x00')
                                    _LOGGER.debug("    Value (text): '%s'", decoded)
                                except UnicodeDecodeError:
                                    # Fall back to hex
                                    _LOGGER.debug("    Value (hex): %s", value.hex())
                            elif value:
                                _LOGGER.debug("    Value: <large data, %d bytes>", len(value))
                        except Exception as err:
                            _LOGGER.debug("    Could not read value: %s", err)
                
                _LOGGER.debug("  Found %d characteristics in this service", char_count)
                            
            _LOGGER.debug("=== End BLE Service Discovery ===")
            
            # Log discovered LionChief characteristics at debug level
            if self._discovered_lionchief_service:
                _LOGGER.debug("🎯 DISCOVERED LIONCHIEF SERVICE: %s", self._discovered_lionchief_service)
            if self._discovered_write_char:
                _LOGGER.debug("🎯 DISCOVERED WRITE CHARACTERISTIC: %s", self._discovered_write_char)
            if self._discovered_notify_char:
                _LOGGER.debug("🎯 DISCOVERED NOTIFY CHARACTERISTIC: %s", self._discovered_notify_char)
                
            # Update constants if we found better characteristics
            if self._discovered_write_char and self._discovered_write_char != WRITE_CHARACTERISTIC_UUID:
                _LOGGER.info("💡 Consider updating WRITE_CHARACTERISTIC_UUID to: %s", self._discovered_write_char)
            if self._discovered_notify_char and self._discovered_notify_char != NOTIFY_CHARACTERISTIC_UUID:
                _LOGGER.info("💡 Consider updating NOTIFY_CHARACTERISTIC_UUID to: %s", self._discovered_notify_char)
            
        except Exception as err:
            _LOGGER.error("Error during BLE service discovery: %s", err)
            import traceback
            _LOGGER.error("Full traceback: %s", traceback.format_exc())

    async def async_send_command(self, command_data: list[int]) -> bool:
        """Send a command to the train."""
        async with self._lock:
            # DEADLOCK FIX: If not connected, call INTERNAL method (which does NOT try to grab the lock again)
            if not self.connected:
                try:
                    await self._connect_internal()
                except BleakError as err:
                    _LOGGER.error("Failed to connect before sending command: %s", err)
                    return False

            # Always use the known-good write characteristic UUID
            write_char_uuid = WRITE_CHARACTERISTIC_UUID
            
            # Retry command sending with better error handling
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    await self._client.write_gatt_char(
                        write_char_uuid, bytearray(command_data)
                    )
                    hex_string = ''.join(f'{b:02x}' for b in command_data)
                    _LOGGER.info("✅ Sent command successfully to %s: %s (hex: %s)", 
                               write_char_uuid, command_data, hex_string)
                    
                    # Update the status sensor with the sent command
                    self._last_notification_hex = hex_string
                    self._notify_state_change()
                    
                    return True

                except BleakError as err:
                    _LOGGER.warning("Failed to send command to %s (attempt %d/%d): %s", 
                                  write_char_uuid, attempt + 1, max_retries, err)
                    self._connected = False
                    
                    # Try to reconnect on subsequent attempts
                    if attempt < max_retries - 1:
                        try:
                            await asyncio.sleep(0.5 * (attempt + 1))  # Exponential backoff
                            # Call internal connect to avoid deadlock
                            await self._connect_internal()
                        except BleakError:
                            _LOGGER.debug("Reconnection attempt %d failed", attempt + 1)
                            continue
                    else:
                        _LOGGER.error("Failed to send command after %d attempts: %s", max_retries, err)
                        
            return False

    async def async_set_speed(self, speed: int) -> bool:
        """Set train speed (0-100)."""
        if not 0 <= speed <= 100:
            raise ValueError("Speed must be between 0 and 100")
        
        # Convert 0-100 to 0-31 (0x00-0x1F) hex scale
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
        """Disconnect from train."""
        command = build_simple_command(0x4B, [0x00, 0x00])
        return await self.async_send_command(command)

    async def async_force_reconnect(self) -> bool:
        """Force reconnection to the train."""
        _LOGGER.info("Force reconnecting to Lionel train at %s", self.mac_address)
        
        # Clear connection state first - don't try to send disconnect commands
        # since the locomotive might already be disconnected/powered off
        self._connected = False
        if self._client:
            try:
                if self._client.is_connected:
                    await self._client.disconnect()
                    _LOGGER.debug("Disconnected existing client")
            except Exception as err:
                _LOGGER.debug("Error disconnecting client (expected if already disconnected): %s", err)
            finally:
                self._client = None
        
        # Wait for any existing connections to clear
        await asyncio.sleep(1.0)
        
        # Now try to establish a fresh connection
        # Use public method to handle locks
        return await self.async_connect()

    # Advanced feature control methods
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

    async def async_set_sound_volume(self, sound_source: int, volume: int, pitch: int = None) -> bool:
        """Set volume and optionally pitch for specific sound source."""
        if not 0 <= volume <= 7:
            raise ValueError("Volume must be between 0 and 7")
        if pitch is not None and not -2 <= pitch <= 2:
            raise ValueError("Pitch must be between -2 and 2")
        
        # Use simple command for better compatibility
        if pitch is not None:
            command = build_simple_command(CMD_SOUND_VOLUME, [sound_source, volume, pitch & 0xFF])
        else:
            command = build_simple_command(CMD_SOUND_VOLUME, [sound_source, volume])
        
        success = await self.async_send_command(command)
        
        if success:
            # Update state tracking based on sound source
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
