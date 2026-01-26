"""The Lionel Train Controller integration."""
from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakError
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    BluetoothChange,
    BluetoothScanningMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, Platform, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import (
    CMD_MASTER_VOLUME,
    CMD_SMOKE,
    CMD_SOUND_VOLUME,
    CONF_MAC_ADDRESS,
    CONF_SERVICE_UUID,
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

# Grace period in seconds. If reconnected within this time, resume speed/smoke.
RESYNC_GRACE_PERIOD = 60.0


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

    # Use PASSIVE scanning to reduce load on ESPHome proxies
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            {"address": mac_address},
            BluetoothScanningMode.PASSIVE,
        )
    )

    async def _on_hass_start(event):
        if not coordinator.connected:
            _LOGGER.debug("HA Started: Attempting delayed connection to Lionel Train")
            await coordinator.async_setup()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_hass_start)
    )

    async def _async_watchdog(now):
        if not coordinator.connected:
            # Only log once per minute to avoid spam
            current_time = time.monotonic()
            if current_time - coordinator._last_watchdog_log > 60.0:
                _LOGGER.debug("🐕 Watchdog: Train disconnected. Monitoring for reconnection.")
                coordinator._last_watchdog_log = current_time
            hass.async_create_task(coordinator.async_connect())
        else:
            hass.async_create_task(coordinator.async_send_heartbeat())
        
        coordinator.watchdog_unsub = async_call_later(hass, 30.0, _async_watchdog)

    coordinator.watchdog_unsub = async_call_later(hass, 30.0, _async_watchdog)
    entry.async_on_unload(coordinator.cancel_watchdog)

    # Store coordinator BEFORE trying to connect
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Load platforms BEFORE initial connection attempt
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Now attempt initial connection (non-blocking)
    try:
        await coordinator.async_setup()
        _LOGGER.info("Successfully connected to Lionel train at %s", mac_address)
    except (BleakError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Initial connection failed (will retry): %s", err)

    async def reload_integration_service(call):
        _LOGGER.info("Reloading Lionel integration via service call")
        await hass.config_entries.async_reload(entry.entry_id)

    if not hass.services.has_service(DOMAIN, "reload_integration"):
        hass.services.async_register(
            DOMAIN, "reload_integration", reload_integration_service
        )

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
        
        self.watchdog_unsub = None
        self._last_watchdog_log = 0.0
        self._last_reconnect_attempt = 0.0
        self._client_ble_device = None
        self._disconnect_time: float = 0.0
        self._connect_time: float = 0.0
        self._reconnect_task: asyncio.Task | None = None

        # State tracking
        self._speed = 0
        self._direction_forward = True
        self._lights_on = True
        self._horn_on = False
        self._bell_on = False

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

        self._model_number = None
        self._serial_number = None
        self._firmware_revision = None
        self._hardware_revision = None
        self._software_revision = None
        self._manufacturer_name = None

        self._last_notification_hex: str | None = None

    def cancel_watchdog(self):
        if self.watchdog_unsub:
            self.watchdog_unsub()
            self.watchdog_unsub = None

    def async_set_ble_device(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Update the BLE device from an advertisement."""
        self._client_ble_device = service_info.device

        # Detect phantom connections (but only if we've been "connected" for >10s)
        # This prevents false positives during the connection establishment phase
        if self.connected and service_info.connectable:
            time_since_connect = time.monotonic() - self._connect_time
            if time_since_connect > 10.0:
                _LOGGER.warning("👻 Phantom connection detected (connected for %.1fs but device still advertising). Forcing reset.", time_since_connect)
                self._connected = False
                if self._client:
                    self.hass.async_create_task(self._async_disconnect_client())

        if change != BluetoothChange.ADVERTISEMENT:
            return

        # Sync check
        if self._connected and (self._client is None or not self._client.is_connected):
            _LOGGER.debug("State desync detected. Resetting connection state.")
            self._connected = False

        if self.connected or self._lock.locked():
            return

        # Rate limit reconnection attempts
        now = time.monotonic()
        if now - self._last_reconnect_attempt < 5.0:
            return

        self._last_reconnect_attempt = now
        _LOGGER.debug("Advertisement received for disconnected device. Triggering reconnect.")
        self.hass.async_create_task(self.async_connect())

    @property
    def connected(self) -> bool:
        return self._connected and self._client is not None and self._client.is_connected

    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Handle disconnection event."""
        _LOGGER.warning("🚂 Disconnected from Lionel train!")
        self._connected = False
        
        # Only clear client if it matches the disconnected one
        if self._client == client:
            self._client = None
        
        self._disconnect_time = time.monotonic()
        self._notify_state_change()
        
        # Start aggressive reconnection loop
        if not self._reconnect_task or self._reconnect_task.done():
            self._reconnect_task = self.hass.async_create_task(
                self._async_reconnect_loop()
            )

    async def _async_reconnect_loop(self) -> None:
        """Aggressive reconnection loop after unexpected disconnect."""
        disconnect_start = time.monotonic()
        _LOGGER.info("🔄 Starting aggressive reconnection loop...")
        attempts = 0
        max_attempts = 12  # Try for 1 minute (12 * 5s = 60s), then let watchdog take over

        while not self.connected and attempts < max_attempts:
            attempts += 1

            # Try to get device from cache
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.mac_address, connectable=True
            )

            if ble_device:
                _LOGGER.debug(f"Reconnection attempt {attempts}/{max_attempts}: Device found in cache")
                try:
                    await self.async_connect()
                    if self.connected:
                        disconnect_duration = time.monotonic() - disconnect_start
                        _LOGGER.info("✅ Reconnection successful after %.1fs (%d attempts)", disconnect_duration, attempts)
                        return
                except Exception as err:
                    _LOGGER.debug(f"Reconnection attempt {attempts} failed: {err}")
            else:
                _LOGGER.debug(f"Reconnection attempt {attempts}/{max_attempts}: Device not in cache yet")

            # Wait 5 seconds before next attempt
            await asyncio.sleep(5.0)

        if not self.connected:
            disconnect_duration = time.monotonic() - disconnect_start
            _LOGGER.info("⚠️ Reconnection loop finished after %.1fs (%d attempts). Watchdog will continue monitoring.", disconnect_duration, attempts)

    async def _async_disconnect_client(self) -> None:
        """Safely disconnect the client."""
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            finally:
                self._client = None

    async def async_setup(self) -> None:
        try:
            await self.async_connect()
        except (BleakError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Initial connection failed during setup: %s", err)

    async def async_shutdown(self) -> None:
        self.cancel_watchdog()
        
        # Cancel reconnection task if running
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        
        await self._async_disconnect_client()
        self._connected = False

    async def async_connect(self) -> None:
        """Public method to connect. Protected by lock with timeout."""
        try:
            async with asyncio.timeout(60.0):
                # Lock is ONLY held during actual connection establishment
                async with self._lock:
                    await self._connect_internal()

                # Device initialization happens OUTSIDE lock so commands work immediately
                if self.connected:
                    await self._initialize_device()
        except asyncio.TimeoutError:
            _LOGGER.error("Connection attempt timed out after 60s")

    async def _connect_internal(self) -> None:
        """Internal connection logic. Must be called with lock held. Only establishes BLE connection."""
        if self.connected:
            return

        # Clean up any lingering client
        if self._client:
            _LOGGER.debug("Cleaning up lingering client before new connection.")
            await self._async_disconnect_client()

        # Get BLE device from HA's cache
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.mac_address, connectable=True
        )

        if ble_device is None:
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.mac_address, connectable=False
            )

        if ble_device is None:
            _LOGGER.debug("Device not found in Bluetooth cache yet")
            return

        try:
            _LOGGER.debug("Attempting connection to %s...", self.mac_address)

            # Establish connection with retries
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self.mac_address,
                max_attempts=3,
                disconnected_callback=self._on_disconnected,
            )

            # Mark connected and store client
            self._client = client
            self._connected = True
            self._connect_time = time.monotonic()
            self._retry_count = 0

            _LOGGER.info("✅ BLE connection established to %s", self.mac_address)

        except (BleakError, asyncio.TimeoutError) as err:
            _LOGGER.error("Failed to connect to train: %s", err)
            self._connected = False
            self._client = None
            raise

    async def _initialize_device(self) -> None:
        """Initialize device after connection. Called OUTSIDE the connection lock."""
        if not self.connected:
            _LOGGER.debug("Connection lost before device initialization could complete")
            return

        # Notify state change so entities become available right away
        self._notify_state_change()

        # Read device info (non-blocking if it fails, with timeout)
        try:
            async with asyncio.timeout(5.0):
                await self._read_device_info()
        except asyncio.TimeoutError:
            _LOGGER.debug("Device info read timed out (skipping)")
        except Exception as e:
            _LOGGER.debug("Could not read device info (skipping): %s", e)

        # Start notifications
        try:
            await self._client.start_notify(
                NOTIFY_CHARACTERISTIC_UUID, self._notification_handler
            )
        except BleakError as e:
            _LOGGER.debug("Could not start notifications: %s", e)

        _LOGGER.info("✅ Device configured. Resyncing state...")

        # Run resync in background so commands work immediately
        self.hass.async_create_task(self._resync_device_state())

    async def _resync_device_state(self) -> None:
        """Resend the last known state to the train after a reconnect."""

        # Wait for BLE stack to stabilize (crucial for proxies)
        await asyncio.sleep(2.0)

        if not self.connected:
            _LOGGER.debug("Connection dropped during settlement. Aborting resync.")
            return

        time_since_disconnect = time.monotonic() - self._disconnect_time
        _LOGGER.debug("Resyncing state. Disconnected for %.1f seconds.", time_since_disconnect)

        is_safe_restore = (self._disconnect_time > 0) and (time_since_disconnect < RESYNC_GRACE_PERIOD)

        try:
            # Always restore Volume and Lights with delays between commands
            await self.async_set_master_volume(self._master_volume)
            await asyncio.sleep(0.2)
            await self.async_set_lights(self._lights_on)
            await asyncio.sleep(0.2)

            # Smoke
            if self._smoke_on:
                if is_safe_restore:
                    await self.async_set_smoke(True)
                    await asyncio.sleep(0.2)
                else:
                    _LOGGER.info("Resync: Smoke OFF for safety.")
                    self._smoke_on = False

            # Speed/Direction
            if self._speed > 0:
                if is_safe_restore:
                    _LOGGER.info("Resync: Resuming speed %s%%", self._speed)
                    await self.async_set_direction(self._direction_forward)
                    await asyncio.sleep(0.2)
                    await self.async_set_speed(self._speed)
                else:
                    _LOGGER.info("Resync: Resetting speed to 0 for safety.")
                    self._speed = 0

        except Exception as e:
            _LOGGER.error("Error during state resync: %s", e)

    async def _notification_handler(self, sender: int, data: bytearray) -> None:
        self._last_notification_hex = data.hex()
        if len(data) >= 8 and data[0] == 0x00 and data[1] == 0x81 and data[2] == 0x02:
            try:
                self._speed = int((data[3] / 31) * 100)
                self._direction_forward = data[4] == 0x01
                flags = data[7]
                self._lights_on = (flags & 0x04) != 0
                self._bell_on = (flags & 0x02) != 0
                self._notify_state_change()
            except (IndexError, ValueError):
                pass
        else:
            self._notify_state_change()

    async def _read_device_info(self) -> None:
        chars = {
            MODEL_NUMBER_CHAR_UUID: "_model_number",
            SERIAL_NUMBER_CHAR_UUID: "_serial_number",
            FIRMWARE_REVISION_CHAR_UUID: "_firmware_revision",
            HARDWARE_REVISION_CHAR_UUID: "_hardware_revision",
            SOFTWARE_REVISION_CHAR_UUID: "_software_revision",
            MANUFACTURER_NAME_CHAR_UUID: "_manufacturer_name",
        }
        for uuid, attr in chars.items():
            try:
                res = await self._client.read_gatt_char(uuid)
                val = res.decode("utf-8", errors="ignore").strip()
                if val:
                    setattr(self, attr, val)
            except BleakError:
                pass

    async def async_send_heartbeat(self) -> None:
        if not self.connected:
            return
        hex_speed = int((self._speed / 100) * 31)
        command = build_simple_command(0x45, [hex_speed])

        # Try to acquire lock with timeout - don't block forever
        try:
            async with asyncio.timeout(2.0):
                async with self._lock:
                    try:
                        await self._client.write_gatt_char(
                            WRITE_CHARACTERISTIC_UUID, bytearray(command), response=False
                        )
                        _LOGGER.debug("💓 Heartbeat sent")
                    except BleakError:
                        _LOGGER.debug("Heartbeat failed - connection likely dropped")
        except asyncio.TimeoutError:
            _LOGGER.debug("Heartbeat skipped - lock busy")

    async def async_send_command(self, command_data: list[int]) -> bool:
        async with self._lock:
            if not self.connected:
                try:
                    await self._connect_internal()
                except BleakError:
                    return False
            try:
                await self._client.write_gatt_char(
                    WRITE_CHARACTERISTIC_UUID, bytearray(command_data), response=False
                )
                self._last_notification_hex = "".join(f"{b:02x}" for b in command_data)
                self._notify_state_change()
                return True
            except BleakError:
                self._connected = False
                return False

    async def async_set_speed(self, speed: int) -> bool:
        if not 0 <= speed <= 100:
            raise ValueError("Speed must be 0-100")
        hex_speed = int((speed / 100) * 31)
        if await self.async_send_command(build_simple_command(0x45, [hex_speed])):
            self._speed = speed
            self._notify_state_change()
            return True
        return False

    async def async_set_direction(self, forward: bool) -> bool:
        """Set train direction."""
        command = build_simple_command(0x46, [0x01 if forward else 0x02])
        if await self.async_send_command(command):
            self._direction_forward = forward
            if self._speed > 0:
                self._speed = 0
                self._notify_state_change()
            return True
        return False

    async def async_set_lights(self, on: bool) -> bool:
        if await self.async_send_command(build_simple_command(0x51, [0x01 if on else 0x00])):
            self._lights_on = on
            return True
        return False

    async def async_set_horn(self, on: bool) -> bool:
        if await self.async_send_command(build_simple_command(0x48, [0x01 if on else 0x00])):
            self._horn_on = on
            return True
        return False

    async def async_set_bell(self, on: bool) -> bool:
        if await self.async_send_command(build_simple_command(0x47, [0x01 if on else 0x00])):
            self._bell_on = on
            return True
        return False

    async def async_play_announcement(self, code: int) -> bool:
        return await self.async_send_command(build_simple_command(0x4D, [code, 0x00]))

    async def async_disconnect(self) -> bool:
        return await self.async_send_command(build_simple_command(0x4B, [0x00, 0x00]))

    async def async_force_reconnect(self) -> bool:
        self._connected = False
        self._client_ble_device = None
        await self._async_disconnect_client()
        await asyncio.sleep(1.0)
        try:
            await self.async_connect()
            return True
        except BleakError:
            return False

    @property
    def speed(self) -> int:
        return self._speed

    @property
    def direction_forward(self) -> bool:
        return self._direction_forward

    @property
    def lights_on(self) -> bool:
        return self._lights_on

    @property
    def horn_on(self) -> bool:
        return self._horn_on

    @property
    def bell_on(self) -> bool:
        return self._bell_on

    @property
    def master_volume(self) -> int:
        return self._master_volume

    @property
    def horn_volume(self) -> int:
        return self._horn_volume

    @property
    def bell_volume(self) -> int:
        return self._bell_volume

    @property
    def speech_volume(self) -> int:
        return self._speech_volume

    @property
    def engine_volume(self) -> int:
        return self._engine_volume

    @property
    def smoke_on(self) -> bool:
        return self._smoke_on

    @property
    def last_notification_hex(self) -> str | None:
        return self._last_notification_hex

    @property
    def device_info(self) -> dict:
        return {
            "model": self._model_number or "LionChief Locomotive",
            "manufacturer": self._manufacturer_name or "Lionel",
            "sw_version": self._software_revision or "Unknown",
            "hw_version": self._hardware_revision or "Unknown",
            "serial_number": self._serial_number,
        }

    def add_update_callback(self, callback):
        self._update_callbacks.add(callback)

    def remove_update_callback(self, callback):
        self._update_callbacks.discard(callback)

    def _notify_state_change(self):
        for cb in list(self._update_callbacks):
            try:
                cb()
            except Exception:
                pass

    async def async_set_master_volume(self, volume: int) -> bool:
        if await self.async_send_command(build_simple_command(CMD_MASTER_VOLUME, [volume])):
            self._master_volume = volume
            self._notify_state_change()
            return True
        return False

    async def async_set_sound_volume(self, source: int, volume: int, pitch: int = None) -> bool:
        params = [source, volume, pitch & 0xFF] if pitch else [source, volume]
        if await self.async_send_command(build_simple_command(CMD_SOUND_VOLUME, params)):
            if source == SOUND_SOURCE_HORN:
                self._horn_volume = volume
            elif source == SOUND_SOURCE_BELL:
                self._bell_volume = volume
            elif source == SOUND_SOURCE_SPEECH:
                self._speech_volume = volume
            elif source == SOUND_SOURCE_ENGINE:
                self._engine_volume = volume
            self._notify_state_change()
            return True
        return False

    async def async_set_smoke(self, on: bool) -> bool:
        if await self.async_send_command(build_simple_command(CMD_SMOKE, [0x01 if on else 0x00])):
            self._smoke_on = on
            self._notify_state_change()
            return True
        return False
