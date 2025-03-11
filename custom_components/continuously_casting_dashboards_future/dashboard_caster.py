"""Dashboard casting functionality for Home Assistant."""
import asyncio
import logging
import os
import subprocess
import time
from datetime import datetime, time as dt_time
import json
import voluptuous as vol
from homeassistant.const import CONF_DEVICES, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval, async_track_point_in_time
from homeassistant.util import dt as dt_util
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

class ContinuouslyCastingDashboardsFuture:
    """Class to handle casting dashboards to Chromecast devices."""

    def __init__(self, hass: HomeAssistant, config: dict):
        """Initialize the dashboard caster."""
        self.hass = hass
        self.config = config
        self.active_devices = {}
        self.health_stats = {}
        self.device_scan_interval = config.get(CONF_SCAN_INTERVAL, 300)
        self.cast_delay = config.get('cast_delay', 0)
        self.start_time = config.get('start_time', '00:00')
        self.end_time = config.get('end_time', '23:59')
        self.switch_entity_id = config.get('switch_entity_id')
        self.devices = config.get(CONF_DEVICES, {})
        self.running = True
        self.unsubscribe_listeners = []
        
        # Ensure directory exists
        os.makedirs('/config/continuously_casting_dashboards', exist_ok=True)
        
        # Set up logging based on config
        log_level = config.get('logging_level', 'INFO').upper()
        logging.getLogger(__name__).setLevel(getattr(logging, log_level))

    async def start(self):
        """Start the casting process."""
        _LOGGER.info("Starting Continuously Casting Dashboards integration")
        
        # Initial setup of devices
        await self.initialize_devices()
        
        # Set up recurring monitoring
        self.unsubscribe_listeners.append(
            async_track_time_interval(
                self.hass, 
                self.async_monitor_devices, 
                dt_util.timedelta(seconds=self.device_scan_interval)
            )
        )
        
        # Generate initial status
        await self.async_generate_status_data()
        
        # Schedule regular status updates
        self.unsubscribe_listeners.append(
            async_track_time_interval(
                self.hass,
                self.async_generate_status_data,
                dt_util.timedelta(minutes=5)
            )
        )
        
        return True

    async def stop(self):
        """Stop the casting process."""
        _LOGGER.info("Stopping Continuously Casting Dashboards integration")
        self.running = False
        
        # Unsubscribe from all listeners
        for unsubscribe in self.unsubscribe_listeners:
            unsubscribe()
        
        return True

    async def initialize_devices(self):
        """Initialize all configured devices."""
        # Check if switch entity allows casting
        if not await self.async_check_switch_entity():
            _LOGGER.info("Switch entity disabled, skipping initial device setup")
            return True
        
        # Start each device with appropriate delay
        for device_name, device_configs in self.devices.items():
            for device_config in device_configs:
                # Check if device is within casting time window
                if not await self.async_is_within_time_window(device_name, device_config):
                    _LOGGER.info(f"Outside casting time window for {device_name}, skipping initial cast")
                    continue
                
                # Create task for each device
                self.hass.async_create_task(
                    self.async_start_device(device_name, device_config)
                )
                
                # Apply cast delay between devices
                if self.cast_delay > 0:
                    await asyncio.sleep(self.cast_delay)
        
        return True

    async def async_start_device(self, device_name, device_config):
        """Start casting to a specific device."""
        _LOGGER.info(f"Starting casting to {device_name}")
        
        # Get device IP from name
        ip = await self.async_get_device_ip(device_name)
        if not ip:
            _LOGGER.error(f"Could not get IP for {device_name}, skipping")
            return
        
        device_key = f"{device_name}_{ip}"
        await self.async_update_health_stats(device_key, 'connection_attempt')
        
        # Cast dashboard to device
        dashboard_url = device_config.get('dashboard_url')
        success = await self.async_cast_dashboard(ip, dashboard_url, device_config)
        
        if success:
            _LOGGER.info(f"Successfully connected to {device_name} ({ip})")
            self.active_devices[device_key] = {
                'name': device_name,
                'ip': ip,
                'status': 'connected',
                'first_seen': datetime.now().isoformat(),
                'last_checked': datetime.now().isoformat(),
                'reconnect_attempts': 0
            }
            await self.async_update_health_stats(device_key, 'connection_success')
        else:
            _LOGGER.error(f"Failed to connect to {device_name} ({ip})")
            self.active_devices[device_key] = {
                'name': device_name,
                'ip': ip,
                'status': 'disconnected',
                'first_seen': datetime.now().isoformat(),
                'last_checked': datetime.now().isoformat(),
                'reconnect_attempts': 0
            }

    async def async_cast_dashboard(self, ip, dashboard_url, device_config):
        """Cast a dashboard to a device with retry logic."""
        volume = device_config.get('volume', 5)
        max_retries = 5
        retry_delay = 10  # seconds
        
        for attempt in range(max_retries):
            try:
                # Use catt to cast the dashboard
                _LOGGER.debug(f"Casting {dashboard_url} to {ip} (attempt {attempt+1}/{max_retries})")
                
                # Run catt command
                cmd = ['catt', '-d', ip, 'cast_site', dashboard_url]
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                if process.returncode != 0:
                    error_msg = stderr.decode().strip() or "Unknown error"
                    _LOGGER.error(f"Catt command failed: {error_msg}")
                    raise Exception(f"Catt command failed: {error_msg}")
                
                # Set volume after successful cast
                if volume is not None:
                    _LOGGER.debug(f"Setting volume to {volume} for device at {ip}")
                    vol_cmd = ['catt', '-d', ip, 'volume', str(volume)]
                    vol_process = await asyncio.create_subprocess_exec(
                        *vol_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    vol_stdout, vol_stderr = await vol_process.communicate()
                    
                    if vol_process.returncode != 0:
                        vol_error = vol_stderr.decode().strip()
                        _LOGGER.warning(f"Failed to set volume for {ip}: {vol_error}")
                
                # Verify the device is actually casting
                await asyncio.sleep(5)  # Give it a moment to start casting
                if await self.async_check_device_status(ip):
                    _LOGGER.info(f"Successfully cast to device at {ip}")
                    return True
                else:
                    _LOGGER.warning(f"Cast command appeared to succeed but device status check failed")
                    raise Exception("Device not casting after command")
                
            except Exception as e:
                _LOGGER.error(f"Cast error on attempt {attempt+1}/{max_retries}: {str(e)}")
                
                if attempt < max_retries - 1:
                    _LOGGER.info(f"Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5  # Exponential backoff
                else:
                    _LOGGER.error(f"Failed to cast to device at {ip} after {max_retries} attempts")
                    return False
        
        return False

    async def async_check_device_status(self, ip):
        """Check if a device is still casting using catt."""
        try:
            process = await asyncio.create_subprocess_exec(
                'catt', '-d', ip, 'status',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            # Parse output to check if it's actually casting
            if process.returncode == 0:
                output = stdout.decode().strip()
                # If device is idle or not casting, return False
                if "Idle" in output or "Nothing is currently playing" in output:
                    return False
                return True
            return False
        except Exception as e:
            _LOGGER.error(f"Error checking device status at {ip}: {str(e)}")
            return False

async def async_get_device_ip(self, device_name):
    """Get IP address for a device name using catt scan or a cached mapping."""
    # First check if we have a cached mapping
    device_map_file = '/config/continuously_casting_dashboards/device_map.json'
    device_map = {}
    
    if os.path.exists(device_map_file):
        try:
            with open(device_map_file, 'r') as f:
                device_map = json.load(f)
        except Exception as e:
            _LOGGER.warning(f"Failed to load device map: {str(e)}")
    
    # If we have a cached IP for this device, use it
    if device_name in device_map and device_map[device_name]:
        _LOGGER.debug(f"Using cached IP {device_map[device_name]} for {device_name}")
        return device_map[device_name]
    
    # Otherwise, scan for devices
    try:
        _LOGGER.info(f"Scanning for device: {device_name}")
        process = await asyncio.create_subprocess_exec(
            'catt', 'scan', '--timeout', '10',  # Longer timeout
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        scan_output = stdout.decode()
        _LOGGER.info(f"Full scan output: {scan_output}")
        
        if process.returncode != 0:
            _LOGGER.error(f"Catt scan failed: {stderr.decode().strip()}")
            return None
        
        # Parse scan results - now with correct format parsing
        for line in scan_output.splitlines():
            # Skip the header line or empty lines
            if "Scanning Chromecasts..." in line or not line.strip():
                continue
                
            _LOGGER.debug(f"Processing scan line: {line}")
            
            # Parse format: "192.168.0.16 - Basement display - Google Inc. Google Nest Hub"
            parts = line.split(' - ')
            if len(parts) < 2:
                continue
                
            ip = parts[0].strip()
            found_name = parts[1].strip() if len(parts) > 1 else ""
            
            _LOGGER.debug(f"Found device: {found_name} with IP: {ip}")
            
            # Case-insensitive exact match or substring match as fallback
            if device_name.lower() == found_name.lower() or device_name.lower() in found_name.lower():
                _LOGGER.info(f"Matched device '{device_name}' to scan result '{found_name}' with IP {ip}")
                
                # Cache the mapping
                device_map[device_name] = ip
                try:
                    os.makedirs('/config/continuously_casting_dashboards', exist_ok=True)
                    with open(device_map_file, 'w') as f:
                        json.dump(device_map, f, indent=2)
                except Exception as e:
                    _LOGGER.warning(f"Failed to save device map: {str(e)}")
                
                return ip
        
        _LOGGER.error(f"Device {device_name} not found in scan results")
        return None
    except Exception as e:
        _LOGGER.error(f"Error scanning for devices: {str(e)}")
        return None

    async def async_is_within_time_window(self, device_name, device_config):
        """Check if current time is within the casting window for a device."""
        now = dt_util.now().time()
        
        # First check device-specific time window
        device_start = device_config.get('start_time', self.start_time)
        device_end = device_config.get('end_time', self.end_time)
        
        # Parse times to time objects
        try:
            start_time = dt_time(*map(int, device_start.split(':')))
            end_time = dt_time(*map(int, device_end.split(':')))
        except Exception as e:
            _LOGGER.error(f"Error parsing time window for {device_name}: {str(e)}")
            return True  # Default to casting if time parsing fails
        
        # Check if casting should be active now
        if start_time <= end_time:
            # Simple case: start_time is before end_time in the same day
            return start_time <= now <= end_time
        else:
            # Complex case: time window spans midnight
            return now >= start_time or now <= end_time

    async def async_check_switch_entity(self):
        """Check if the switch entity is enabled (if configured)."""
        if not self.switch_entity_id:
            return True  # No switch configured, always enabled
        
        state = self.hass.states.get(self.switch_entity_id)
        if state is None:
            _LOGGER.warning(f"Switch entity {self.switch_entity_id} not found")
            return True  # If entity doesn't exist, default to enabled
        
        return state.state == 'on'

    async def async_monitor_devices(self, *args):
        """Monitor all devices and reconnect if needed."""
        _LOGGER.debug("Running device status check")
        
        # First check if switch entity allows casting
        if not await self.async_check_switch_entity():
            _LOGGER.info("Switch entity disabled, skipping device monitoring")
            return
        
        for device_name, device_configs in self.devices.items():
            for device_config in device_configs:
                # Skip devices outside their time window
                if not await self.async_is_within_time_window(device_name, device_config):
                    _LOGGER.debug(f"Outside casting time window for {device_name}, skipping check")
                    continue
                
                ip = await self.async_get_device_ip(device_name)
                if not ip:
                    _LOGGER.warning(f"Could not get IP for {device_name}, skipping check")
                    continue
                
                # Check if device is still casting
                is_casting = await self.async_check_device_status(ip)
                
                # Update device status
                device_key = f"{device_name}_{ip}"
                if device_key in self.active_devices:
                    previous_status = self.active_devices[device_key].get('status', 'unknown')
                    self.active_devices[device_key]['status'] = 'connected' if is_casting else 'disconnected'
                    self.active_devices[device_key]['last_checked'] = datetime.now().isoformat()
                    
                    # Device was connected but now disconnected
                    if previous_status == 'connected' and not is_casting:
                        _LOGGER.warning(f"Device {device_name} ({ip}) has disconnected, attempting to reconnect")
                        await self.async_reconnect_device(device_name, ip, device_config)
                    
                    # Device was disconnected but now connected
                    elif previous_status == 'disconnected' and is_casting:
                        _LOGGER.info(f"Device {device_name} ({ip}) has reconnected")
                        self.active_devices[device_key]['reconnect_attempts'] = 0
                        await self.async_update_health_stats(device_key, 'reconnected')
                else:
                    # First time seeing this device
                    self.active_devices[device_key] = {
                        'name': device_name,
                        'ip': ip,
                        'status': 'connected' if is_casting else 'disconnected',
                        'first_seen': datetime.now().isoformat(),
                        'last_checked': datetime.now().isoformat(),
                        'reconnect_attempts': 0
                    }
                    
                    # If not casting on first check, try to connect
                    if not is_casting:
                        _LOGGER.info(f"Device {device_name} ({ip}) not casting on first check, attempting to connect")
                        await self.async_reconnect_device(device_name, ip, device_config)

    async def async_reconnect_device(self, device_name, ip, device_config):
        """Attempt to reconnect a disconnected device."""
        device_key = f"{device_name}_{ip}"
        
        # Skip if outside time window
        if not await self.async_is_within_time_window(device_name, device_config):
            _LOGGER.info(f"Outside casting time window for {device_name}, skipping reconnect")
            return False
        
        # Increment reconnect attempts
        if device_key in self.active_devices:
            self.active_devices[device_key]['reconnect_attempts'] += 1
            attempts = self.active_devices[device_key]['reconnect_attempts']
            
            # If too many reconnect attempts, back off
            if attempts > 5:
                _LOGGER.warning(f"Device {device_name} ({ip}) has had {attempts} reconnect attempts, backing off")
                await self.async_update_health_stats(device_key, 'reconnect_failed')
                return False
        
        _LOGGER.info(f"Attempting to reconnect to {device_name} ({ip})")
        dashboard_url = device_config.get('dashboard_url')
        success = await self.async_cast_dashboard(ip, dashboard_url, device_config)
        
        if success:
            _LOGGER.info(f"Successfully reconnected to {device_name} ({ip})")
            if device_key in self.active_devices:
                self.active_devices[device_key]['status'] = 'connected'
                self.active_devices[device_key]['reconnect_attempts'] = 0
                self.active_devices[device_key]['last_reconnect'] = datetime.now().isoformat()
            await self.async_update_health_stats(device_key, 'reconnect_success')
            return True
        else:
            _LOGGER.error(f"Failed to reconnect to {device_name} ({ip})")
            await self.async_update_health_stats(device_key, 'reconnect_failed')
            return False

    async def async_update_health_stats(self, device_key, event_type):
        """Update health statistics for a device."""
        if device_key not in self.health_stats:
            self.health_stats[device_key] = {
                'first_seen': datetime.now().isoformat(),
                'connection_attempts': 0,
                'successful_connections': 0,
                'disconnections': 0,
                'reconnect_attempts': 0,
                'successful_reconnects': 0,
                'failed_reconnects': 0,
                'uptime_seconds': 0,
                'last_connection': None,
                'last_disconnection': None
            }
        
        now = datetime.now().isoformat()
        
        if event_type == 'connection_attempt':
            self.health_stats[device_key]['connection_attempts'] += 1
        elif event_type == 'connection_success':
            self.health_stats[device_key]['successful_connections'] += 1
            self.health_stats[device_key]['last_connection'] = now
        elif event_type == 'disconnected':
            self.health_stats[device_key]['disconnections'] += 1
            self.health_stats[device_key]['last_disconnection'] = now
        elif event_type == 'reconnect_attempt':
            self.health_stats[device_key]['reconnect_attempts'] += 1
        elif event_type == 'reconnect_success':
            self.health_stats[device_key]['successful_reconnects'] += 1
            self.health_stats[device_key]['last_connection'] = now
        elif event_type == 'reconnect_failed':
            self.health_stats[device_key]['failed_reconnects'] += 1
        
        # Save health stats to file
        await self.async_save_health_stats()

    async def async_save_health_stats(self):
        """Save health statistics to file."""
        try:
            stats_dir = '/config/continuously_casting_dashboards'
            os.makedirs(stats_dir, exist_ok=True)
            
            with open(f'{stats_dir}/health_stats.json', 'w') as f:
                json.dump(self.health_stats, f, indent=2)
        except Exception as e:
            _LOGGER.error(f"Failed to save health stats: {str(e)}")

    async def async_generate_status_data(self, *args):
        """Generate status data for Home Assistant sensors."""
        connected_count = sum(1 for d in self.active_devices.values() if d.get('status') == 'connected')
        disconnected_count = sum(1 for d in self.active_devices.values() if d.get('status') != 'connected')
        
        # Format for Home Assistant sensors
        status_data = {
            'total_devices': len(self.active_devices),
            'connected_devices': connected_count,
            'disconnected_devices': disconnected_count,
            'last_updated': datetime.now().isoformat(),
            'devices': {}
        }
        
        for device_key, device in self.active_devices.items():
            device_name = device.get('name', 'Unknown')
            ip = device.get('ip', 'Unknown')
            
            status_data['devices'][device_name] = {
                'ip': ip,
                'status': device.get('status', 'unknown'),
                'last_checked': device.get('last_checked', ''),
                'reconnect_attempts': device.get('reconnect_attempts', 0)
            }
        
        # Save status data to file for Home Assistant
        try:
            with open('/config/continuously_casting_dashboards/status.json', 'w') as f:
                json.dump(status_data, f, indent=2)
        except Exception as e:
            _LOGGER.error(f"Failed to save status data: {str(e)}")
            
        # Update the Home Assistant state if needed
        # You could add state entities here if desired
        
        return status_data