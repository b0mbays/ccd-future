"""Monitoring functionality for Continuously Casting Dashboards."""
import asyncio
import logging
import time
from datetime import datetime
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_DEVICES
from .const import EVENT_CONNECTION_ATTEMPT, EVENT_CONNECTION_SUCCESS, EVENT_RECONNECT_ATTEMPT, EVENT_RECONNECT_SUCCESS, EVENT_RECONNECT_FAILED

_LOGGER = logging.getLogger(__name__)

class MonitoringManager:
    """Class to handle device monitoring and reconnection."""

    def __init__(self, hass: HomeAssistant, config: dict, device_manager, casting_manager, 
                 time_window_checker, switch_checker):
        """Initialize the monitoring manager."""
        self.hass = hass
        self.config = config
        self.device_manager = device_manager
        self.casting_manager = casting_manager
        self.time_window_checker = time_window_checker
        self.switch_checker = switch_checker
        self.stats_manager = None  # Will be set later
        self.devices = config.get(CONF_DEVICES, {})
        self.cast_delay = config.get('cast_delay', 0)
    
    def set_stats_manager(self, stats_manager):
        """Set the stats manager reference."""
        self.stats_manager = stats_manager
        # Share the device manager with stats manager
        self.stats_manager.set_device_manager(self.device_manager)
    
    async def initialize_devices(self):
        """Initialize all configured devices."""
        # Check if switch entity allows casting
        if not await self.switch_checker.async_check_switch_entity():
            _LOGGER.info("Switch entity disabled, skipping initial device setup")
            return True
        
        # Perform a single scan to find all devices
        device_ip_map = {}
        for device_name in self.devices.keys():
            ip = await self.device_manager.async_get_device_ip(device_name)
            if ip:
                device_ip_map[device_name] = ip
            else:
                _LOGGER.error(f"Could not get IP for {device_name}, skipping initial setup for this device")
                
        # Add delay between scanning and casting to avoid overwhelming the network
        await asyncio.sleep(2)
        
        # Start each device with appropriate delay
        for device_name, device_configs in self.devices.items():
            if device_name not in device_ip_map:
                continue
                
            ip = device_ip_map[device_name]
            
            for device_config in device_configs:
                # Check if device is within casting time window
                if not await self.time_window_checker.async_is_within_time_window(device_name, device_config):
                    _LOGGER.info(f"Outside casting time window for {device_name}, skipping initial cast")
                    continue
                
                # Check if media is playing
                if await self.device_manager.async_is_media_playing(ip):
                    _LOGGER.info(f"Media is currently playing on {device_name}, skipping initial cast")
                    device_key = f"{device_name}_{ip}"
                    self.device_manager.update_active_device(
                        device_key=device_key,
                        status='media_playing',
                        name=device_name,
                        ip=ip,
                        first_seen=datetime.now().isoformat(),
                        last_checked=datetime.now().isoformat(),
                        reconnect_attempts=0
                    )
                    continue
                
                # Create task for each device
                self.hass.async_create_task(
                    self.async_start_device(device_name, device_config, ip)
                )
                
                # Apply cast delay between devices
                if self.cast_delay > 0:
                    await asyncio.sleep(self.cast_delay)
        
        return True
    
    async def async_start_device(self, device_name, device_config, ip=None):
        """Start casting to a specific device."""
        _LOGGER.info(f"Starting casting to {device_name}")
        
        # Get device IP if not provided
        if not ip:
            ip = await self.device_manager.async_get_device_ip(device_name)
            if not ip:
                _LOGGER.error(f"Could not get IP for {device_name}, skipping")
                return
        
        # Check if media is playing before casting
        if await self.device_manager.async_is_media_playing(ip):
            _LOGGER.info(f"Media is currently playing on {device_name}, skipping cast")
            device_key = f"{device_name}_{ip}"
            self.device_manager.update_active_device(
                device_key=device_key,
                status='media_playing',
                name=device_name,
                ip=ip,
                first_seen=datetime.now().isoformat(),
                last_checked=datetime.now().isoformat(),
                reconnect_attempts=0
            )
            return
        
        device_key = f"{device_name}_{ip}"
        if self.stats_manager:
            await self.stats_manager.async_update_health_stats(device_key, EVENT_CONNECTION_ATTEMPT)
        
        # Cast dashboard to device
        dashboard_url = device_config.get('dashboard_url')
        success = await self.casting_manager.async_cast_dashboard(ip, dashboard_url, device_config)
        
        if success:
            _LOGGER.info(f"Successfully connected to {device_name} ({ip})")
            self.device_manager.update_active_device(
                device_key=device_key,
                status='connected',
                name=device_name,
                ip=ip,
                first_seen=datetime.now().isoformat(),
                last_checked=datetime.now().isoformat(),
                reconnect_attempts=0
            )
            if self.stats_manager:
                await self.stats_manager.async_update_health_stats(device_key, EVENT_CONNECTION_SUCCESS)
        else:
            _LOGGER.error(f"Failed to connect to {device_name} ({ip})")
            self.device_manager.update_active_device(
                device_key=device_key,
                status='disconnected',
                name=device_name,
                ip=ip,
                first_seen=datetime.now().isoformat(),
                last_checked=datetime.now().isoformat(),
                reconnect_attempts=0
            )
    
    async def async_monitor_devices(self, *args):
        """Monitor all devices and reconnect if needed."""
        _LOGGER.debug("Running device status check")
        
        # First check if switch entity allows casting
        if not await self.switch_checker.async_check_switch_entity():
            _LOGGER.info("Switch entity disabled, skipping device monitoring")
            return
            
        # Scan for all devices at once and store IPs
        device_ip_map = {}
        for device_name in self.devices.keys():
            ip = await self.device_manager.async_get_device_ip(device_name)
            if ip:
                device_ip_map[device_name] = ip
            else:
                _LOGGER.warning(f"Could not get IP for {device_name}, skipping check")
        
        # Process each device with its known IP
        for device_name, device_configs in self.devices.items():
            # Skip if we couldn't get the IP
            if device_name not in device_ip_map:
                continue
                
            ip = device_ip_map[device_name]
            device_key = f"{device_name}_{ip}"
                
            for device_config in device_configs:
                # Skip devices outside their time window
                if not await self.time_window_checker.async_is_within_time_window(device_name, device_config):
                    _LOGGER.debug(f"Outside casting time window for {device_name}, skipping check")
                    continue
                
                # Check if media is playing before attempting to reconnect
                is_media_playing = await self.device_manager.async_is_media_playing(ip)
                if is_media_playing:
                    _LOGGER.info(f"Media is currently playing on {device_name}, skipping status check")
                    # Update device status to media_playing
                    active_device = self.device_manager.get_active_device(device_key)
                    if active_device:
                        # If device was previously connected to our dashboard, add a delay before marking as media_playing
                        # This prevents rapid switching when "Hey Google" commands are being processed
                        if active_device.get('status') == 'connected':
                            _LOGGER.info(f"Device {device_name} was showing our dashboard but now has media - giving it time to stabilize")
                            # Don't update the status yet, let it remain as 'connected' for this cycle
                        else:
                            self.device_manager.update_active_device(device_key, 'media_playing', last_checked=datetime.now().isoformat())
                    else:
                        # First time seeing this device
                        self.device_manager.update_active_device(
                            device_key=device_key,
                            status='media_playing',
                            name=device_name,
                            ip=ip,
                            first_seen=datetime.now().isoformat(),
                            last_checked=datetime.now().isoformat(),
                            reconnect_attempts=0
                        )
                    continue
                
                # Check if device is still casting our dashboard
                is_casting = await self.device_manager.async_check_device_status(ip)
                
                # Check if device is idle with just volume info
                cmd = ['catt', '-d', ip, 'status']
                status_process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                status_stdout, status_stderr = await status_process.communicate()
                status_output = status_stdout.decode().strip()
                
                # If only volume info is returned, device is truly idle
                is_idle = len(status_output.splitlines()) <= 2 and all(line.startswith("Volume") for line in status_output.splitlines())
                
                # Update device status
                active_device = self.device_manager.get_active_device(device_key)
                if active_device:
                    previous_status = active_device.get('status', 'unknown')
                    last_status_change = active_device.get('last_status_change', 0)
                    current_time = time.time()
                    
                    # Determine current state and take appropriate action
                    if is_casting:
                        # Device is showing our dashboard
                        if previous_status != 'connected':
                            self.device_manager.update_active_device(device_key, 'connected', last_status_change=current_time)
                            _LOGGER.info(f"Device {device_name} ({ip}) is now connected")
                            self.device_manager.update_active_device(device_key, 'connected', reconnect_attempts=0)
                            if self.stats_manager:
                                await self.stats_manager.async_update_health_stats(device_key, EVENT_RECONNECT_SUCCESS)
                        else:
                            self.device_manager.update_active_device(device_key, 'connected', last_checked=datetime.now().isoformat())
                    elif is_idle:
                        # Device is idle, should show our dashboard
                        # Add a delay after any status change to prevent rapid reconnects
                        # This gives voice commands time to be processed
                        min_time_between_reconnects = 30  # seconds
                        time_since_last_change = current_time - last_status_change
                        
                        if previous_status != 'disconnected':
                            _LOGGER.info(f"Device {device_name} ({ip}) is idle and not casting our dashboard")
                            self.device_manager.update_active_device(device_key, 'disconnected', 
                                                                     last_status_change=current_time,
                                                                     last_checked=datetime.now().isoformat())
                        else:
                            # Only attempt to reconnect if enough time has passed since last status change
                            if time_since_last_change > min_time_between_reconnects:
                                _LOGGER.info(f"Device {device_name} ({ip}) is still idle after waiting period, attempting reconnect")
                                await self.async_reconnect_device(device_name, ip, device_config)
                            else:
                                _LOGGER.debug(f"Device {device_name} ({ip}) is idle but waiting {int(min_time_between_reconnects - time_since_last_change)}s before reconnecting")
                                self.device_manager.update_active_device(device_key, 'disconnected', last_checked=datetime.now().isoformat())
                    else:
                        # Device has other content
                        if previous_status != 'other_content':
                            self.device_manager.update_active_device(device_key, 'other_content', 
                                                                     last_status_change=current_time,
                                                                     last_checked=datetime.now().isoformat())
                        else:
                            self.device_manager.update_active_device(device_key, 'other_content', last_checked=datetime.now().isoformat())
                        _LOGGER.info(f"Device {device_name} ({ip}) has other content (not our dashboard and not idle)")
                else:
                    # First time seeing this device
                    if is_casting:
                        status = 'connected'
                        _LOGGER.info(f"Device {device_name} ({ip}) is casting our dashboard")
                    elif is_idle:
                        status = 'disconnected'
                        _LOGGER.info(f"Device {device_name} ({ip}) is idle, will attempt to connect after stabilization period")
                    else:
                        status = 'other_content'
                        _LOGGER.info(f"Device {device_name} ({ip}) has other content, will not connect")
                    
                    self.device_manager.update_active_device(
                        device_key=device_key,
                        status=status,
                        name=device_name,
                        ip=ip,
                        first_seen=datetime.now().isoformat(),
                        last_checked=datetime.now().isoformat(),
                        last_status_change=time.time(),
                        reconnect_attempts=0
                    )

    async def async_reconnect_device(self, device_name, ip, device_config):
        """Attempt to reconnect a disconnected device."""
        device_key = f"{device_name}_{ip}"
        
        # Skip if outside time window
        if not await self.time_window_checker.async_is_within_time_window(device_name, device_config):
            _LOGGER.info(f"Outside casting time window for {device_name}, skipping reconnect")
            return False
        
        # Check if media is playing before attempting to reconnect
        if await self.device_manager.async_is_media_playing(ip):
            _LOGGER.info(f"Media is currently playing on {device_name}, skipping reconnect")
            active_device = self.device_manager.get_active_device(device_key)
            if active_device:
                self.device_manager.update_active_device(device_key, 'media_playing')
            return False
        
        # Increment reconnect attempts
        active_device = self.device_manager.get_active_device(device_key)
        if active_device:
            attempts = active_device.get('reconnect_attempts', 0) + 1
            self.device_manager.update_active_device(device_key, active_device.get('status'), reconnect_attempts=attempts)
            
            # If too many reconnect attempts, back off
            if attempts > 10:  # Increased from 5 to 10
                _LOGGER.warning(f"Device {device_name} ({ip}) has had {attempts} reconnect attempts, backing off")
                if self.stats_manager:
                    await self.stats_manager.async_update_health_stats(device_key, EVENT_RECONNECT_FAILED)
                return False
        
        # Check status one more time to see if it's truly idle
        cmd = ['catt', '-d', ip, 'status']
        status_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        status_stdout, status_stderr = await status_process.communicate()
        status_output = status_stdout.decode().strip()
        
        # If device isn't idle (has more than just volume info), don't attempt to cast
        if len(status_output.splitlines()) > 2 or not all(line.startswith("Volume") for line in status_output.splitlines()):
            if "Dummy" not in status_output and "8123" not in status_output:
                _LOGGER.info(f"Device {device_name} ({ip}) shows non-idle status, skipping reconnect")
                if active_device:
                    self.device_manager.update_active_device(device_key, 'other_content')
                return False
        
        _LOGGER.info(f"Attempting to reconnect to {device_name} ({ip})")
        if self.stats_manager:
            await self.stats_manager.async_update_health_stats(device_key, EVENT_RECONNECT_ATTEMPT)
        dashboard_url = device_config.get('dashboard_url')
        _LOGGER.debug(f"Casting URL {dashboard_url} to device {device_name} ({ip})")
        success = await self.casting_manager.async_cast_dashboard(ip, dashboard_url, device_config)
        
        if success:
            _LOGGER.info(f"Successfully reconnected to {device_name} ({ip})")
            if active_device:
                self.device_manager.update_active_device(
                    device_key=device_key,
                    status='connected',
                    reconnect_attempts=0,
                    last_reconnect=datetime.now().isoformat()
                )
            if self.stats_manager:
                await self.stats_manager.async_update_health_stats(device_key, EVENT_RECONNECT_SUCCESS)
            return True
        else:
            _LOGGER.error(f"Failed to reconnect to {device_name} ({ip})")
            if self.stats_manager:
                await self.stats_manager.async_update_health_stats(device_key, EVENT_RECONNECT_FAILED)
            return False