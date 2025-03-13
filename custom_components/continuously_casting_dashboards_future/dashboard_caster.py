"""Dashboard casting functionality for Home Assistant."""
import asyncio
import logging
import os
import subprocess
import time
from datetime import datetime, time as dt_time, timedelta
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
        self.device_scan_interval = config.get(CONF_SCAN_INTERVAL, 30)
        self.cast_delay = config.get('cast_delay', 0)
        self.start_time = config.get('start_time', '00:00')
        self.end_time = config.get('end_time', '23:59')
        self.switch_entity_id = config.get('switch_entity_id')
        self.devices = config.get(CONF_DEVICES, {})
        self.running = True
        self.unsubscribe_listeners = []
        self.device_ip_cache = {}  # Cache for device IPs
        
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
                timedelta(seconds=self.device_scan_interval)
            )
        )
        
        # Generate initial status
        await self.async_generate_status_data()
        
        # Schedule regular status updates
        self.unsubscribe_listeners.append(
            async_track_time_interval(
                self.hass,
                self.async_generate_status_data,
                timedelta(minutes=5)
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
        
        # Perform a single scan to find all devices
        device_ip_map = {}
        for device_name in self.devices.keys():
            ip = await self.async_get_device_ip(device_name)
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
                if not await self.async_is_within_time_window(device_name, device_config):
                    _LOGGER.info(f"Outside casting time window for {device_name}, skipping initial cast")
                    continue
                
                # Check if media is playing
                if await self.async_is_media_playing(ip):
                    _LOGGER.info(f"Media is currently playing on {device_name}, skipping initial cast")
                    device_key = f"{device_name}_{ip}"
                    self.active_devices[device_key] = {
                        'name': device_name,
                        'ip': ip,
                        'status': 'media_playing',
                        'first_seen': datetime.now().isoformat(),
                        'last_checked': datetime.now().isoformat(),
                        'reconnect_attempts': 0
                    }
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
            ip = await self.async_get_device_ip(device_name)
            if not ip:
                _LOGGER.error(f"Could not get IP for {device_name}, skipping")
                return
        
        # Check if media is playing before casting
        if await self.async_is_media_playing(ip):
            _LOGGER.info(f"Media is currently playing on {device_name}, skipping cast")
            device_key = f"{device_name}_{ip}"
            self.active_devices[device_key] = {
                'name': device_name,
                'ip': ip,
                'status': 'media_playing',
                'first_seen': datetime.now().isoformat(),
                'last_checked': datetime.now().isoformat(),
                'reconnect_attempts': 0
            }
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

    async def async_is_media_playing(self, ip):
        """Check if media (like Spotify or YouTube) is playing or paused on the device."""
        try:
            _LOGGER.debug(f"Checking if media is playing on device at {ip}")
            cmd = ['catt', '-d', ip, 'status']
            _LOGGER.debug(f"Executing command: {' '.join(cmd)}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            # Log full output
            stdout_str = stdout.decode().strip()
            stderr_str = stderr.decode().strip()
            _LOGGER.debug(f"Status command stdout: {stdout_str}")
            _LOGGER.debug(f"Status command stderr: {stderr_str}")
            
            if process.returncode != 0:
                _LOGGER.warning(f"Status check failed with return code {process.returncode}: {stderr_str}")
                return False
                
            # Check for "idle" state that only shows volume info
            if len(stdout_str.splitlines()) <= 2 and all(line.startswith("Volume") for line in stdout_str.splitlines()):
                _LOGGER.debug(f"Device at {ip} is idle (only volume info returned)")
                return False
            
            # Check for a status line that contains "Casting: Starting" which indicates media is about to play
            if "Casting: Starting" in stdout_str:
                _LOGGER.info(f"Device at {ip} is starting to cast media")
                return True
                
            # Check for references to Google Assistant, which means a voice command is being handled
            if "assistant" in stdout_str.lower():
                _LOGGER.info(f"Device at {ip} is processing a Google Assistant command")
                return True
                
            # If we get "Idle" or "Nothing is currently playing", no media is playing
            if "Idle" in stdout_str or "Nothing is currently playing" in stdout_str:
                _LOGGER.debug(f"Device at {ip} is idle or not playing anything")
                return False
                
            # Check if we have a "State: PLAYING" or "State: PAUSED" or "State: BUFFERING" line
            for line in stdout_str.splitlines():
                if "State:" in line and ("PLAYING" in line or "PAUSED" in line or "BUFFERING" in line):
                    _LOGGER.info(f"Found {line} - media is active on device at {ip}")
                    return True
                    
            # Check for a "Title:" line that is not "Dummy" (dashboard)
            for line in stdout_str.splitlines():
                if "Title:" in line and "Dummy" not in line:
                    _LOGGER.info(f"Found '{line}' - media content is active on device at {ip}")
                    return True
                    
            # Check if any known media app name is in the output
            status_lower = stdout_str.lower()
            media_apps = ["spotify", "youtube", "netflix", "plex", "disney+", "hulu", "amazon prime", "music", "audio", "video", "cast"]
            for app in media_apps:
                if app in status_lower:
                    _LOGGER.info(f"Found '{app}' in status - media app is active on device at {ip}")
                    return True
                    
            # At this point, check if anything is playing at all (that's not our dashboard)
            if "Dummy" not in stdout_str and ("playing" in status_lower or "paused" in status_lower or "buffering" in status_lower):
                _LOGGER.info(f"Found playing/paused/buffering state but not our dashboard - media is active on device at {ip}")
                return True
                
            _LOGGER.debug(f"No media playing on device at {ip}")
            return False
        except Exception as e:
            _LOGGER.error(f"Error checking media status on device at {ip}: {str(e)}")
            return False

    async def async_cast_dashboard(self, ip, dashboard_url, device_config):
        """Cast a dashboard to a device with retry logic."""
        volume = device_config.get('volume', 5)
        max_retries = 5
        retry_delay = 10  # seconds
        verification_wait_time = 15  # Increased from 5 to 15 seconds to give devices more time to load
        
        for attempt in range(max_retries):
            try:
                # Check if media is playing before casting
                if await self.async_is_media_playing(ip):
                    _LOGGER.info(f"Media is currently playing on device at {ip}, skipping cast attempt")
                    return False
                
                # Use catt to cast the dashboard
                _LOGGER.debug(f"Casting {dashboard_url} to {ip} (attempt {attempt+1}/{max_retries})")
                
                # Run catt command - add full command logging
                cmd = ['catt', '-d', ip, 'cast_site', dashboard_url]
                _LOGGER.debug(f"Executing command: {' '.join(cmd)}")
                
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                # Log the full output
                stdout_str = stdout.decode().strip()
                stderr_str = stderr.decode().strip()
                _LOGGER.debug(f"Command stdout: {stdout_str}")
                _LOGGER.debug(f"Command stderr: {stderr_str}")
                _LOGGER.debug(f"Command return code: {process.returncode}")
                
                # Check if the cast command itself failed
                if process.returncode != 0:
                    error_msg = stderr_str or "Unknown error"
                    _LOGGER.error(f"Catt command failed: {error_msg}")
                    raise Exception(f"Catt command failed: {error_msg}")
                
                # If stdout contains success message like "Casting ... on device", consider it likely successful
                cast_likely_succeeded = "Casting" in stdout_str and "on" in stdout_str
                
                # Set volume after successful cast
                if volume is not None:
                    _LOGGER.debug(f"Setting volume to {volume} for device at {ip}")
                    vol_cmd = ['catt', '-d', ip, 'volume', str(volume)]
                    _LOGGER.debug(f"Executing command: {' '.join(vol_cmd)}")
                    
                    vol_process = await asyncio.create_subprocess_exec(
                        *vol_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    vol_stdout, vol_stderr = await vol_process.communicate()
                    
                    # Log volume command output
                    _LOGGER.debug(f"Volume command stdout: {vol_stdout.decode().strip()}")
                    _LOGGER.debug(f"Volume command stderr: {vol_stderr.decode().strip()}")
                    _LOGGER.debug(f"Volume command return code: {vol_process.returncode}")
                    
                    if vol_process.returncode != 0:
                        vol_error = vol_stderr.decode().strip()
                        _LOGGER.warning(f"Failed to set volume for {ip}: {vol_error}")
                
                # Verify the device is actually casting
                _LOGGER.debug(f"Waiting {verification_wait_time} seconds to verify casting...")
                await asyncio.sleep(verification_wait_time)  # Give it more time to start casting
                
                status_check = await self.async_check_device_status(ip)
                _LOGGER.debug(f"Status check result: {status_check}")
                
                # If status check passes or the cast command looked successful, consider it a success
                if status_check:
                    _LOGGER.info(f"Successfully cast to device at {ip}")
                    return True
                elif cast_likely_succeeded:
                    _LOGGER.info(f"Cast command succeeded but status check didn't detect dashboard yet. Assuming success.")
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
        """Check if a device is still casting our dashboard specifically."""
        try:
            _LOGGER.debug(f"Checking status for device at {ip}")
            cmd = ['catt', '-d', ip, 'status']
            _LOGGER.debug(f"Executing command: {' '.join(cmd)}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            # Log full output
            stdout_str = stdout.decode().strip()
            stderr_str = stderr.decode().strip()
            _LOGGER.debug(f"Status command stdout: {stdout_str}")
            _LOGGER.debug(f"Status command stderr: {stderr_str}")
            _LOGGER.debug(f"Status command return code: {process.returncode}")
            
            # Parse output to check if it's actually casting our dashboard
            if process.returncode == 0:
                output = stdout_str
                
                # Check for "idle" state that only shows volume info
                if len(stdout_str.splitlines()) <= 2 and all(line.startswith("Volume") for line in stdout_str.splitlines()):
                    _LOGGER.debug(f"Device at {ip} is idle (only volume info returned)")
                    return False
                    
                # If device explicitly says idle or nothing playing, return False
                if "Idle" in output or "Nothing is currently playing" in output:
                    _LOGGER.debug(f"Device at {ip} is idle or not casting")
                    return False
                    
                # Look for "Dummy" or our dashboard URL, which indicates our dashboard is casting
                if "Dummy" in output:
                    dummy_line = next((line for line in output.splitlines() if "Dummy" in line), "")
                    _LOGGER.debug(f"Dashboard found: {dummy_line}")
                    return True
                
                # Check for dashboard-specific indicators in the output
                dashboard_indicators = ["8123", "dashboard", "kiosk", "homeassistant"]
                if any(indicator in output.lower() for indicator in dashboard_indicators):
                    _LOGGER.debug(f"Dashboard indicators found in status")
                    return True
                
                # If we get here, device is playing something but not our dashboard
                _LOGGER.debug(f"Device at {ip} is playing something, but not our dashboard")
                return False
            else:
                _LOGGER.warning(f"Status check failed with return code {process.returncode}: {stderr_str}")
                return False
        except Exception as e:
            _LOGGER.error(f"Error checking device status at {ip}: {str(e)}")
            return False

    async def async_get_device_ip(self, device_name):
        """Get IP address for a device name using catt scan without relying on cached mappings."""
        try:
            _LOGGER.info(f"Scanning for device: {device_name}")
            # Check if we've already cached the device to speed up lookups
            if hasattr(self, 'device_ip_cache'):
                # Cache exists and device is in cache
                if device_name in self.device_ip_cache and self.device_ip_cache[device_name]['timestamp'] > (time.time() - 300):
                    _LOGGER.debug(f"Using cached IP for {device_name}: {self.device_ip_cache[device_name]['ip']}")
                    return self.device_ip_cache[device_name]['ip']
            else:
                # Initialize cache
                self.device_ip_cache = {}
                
            # Do a fresh scan
            process = await asyncio.create_subprocess_exec(
                'catt', 'scan',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            scan_output = stdout.decode()
            _LOGGER.debug(f"Full scan output: {scan_output}")
            
            if process.returncode != 0:
                _LOGGER.error(f"Catt scan failed: {stderr.decode().strip()}")
                return None
            
            # Parse scan results and find exact matching device
            found_devices = []
            for line in scan_output.splitlines():
                # Skip the header line or empty lines
                if "Scanning Chromecasts..." in line or not line.strip():
                    continue
                
                # Parse format: "192.168.0.16 - Basement display - Google Inc. Google Nest Hub"
                parts = line.split(' - ')
                if len(parts) < 2:
                    continue
                    
                ip = parts[0].strip()
                found_name = parts[1].strip() if len(parts) > 1 else ""
                
                # Collect all found devices for logging
                found_devices.append((found_name, ip))
                
                # Update the cache for all found devices to speed up future lookups
                self.device_ip_cache[found_name] = {
                    'ip': ip,
                    'timestamp': time.time()
                }
                
                # Exact match check (case-insensitive)
                if found_name.lower() == device_name.lower():
                    _LOGGER.info(f"Matched device '{device_name}' with IP {ip}")
                    return ip
            
            # If we get here, no exact match was found
            found_names = [name for name, _ in found_devices]
            _LOGGER.error(f"Device '{device_name}' not found in scan results. Found devices: {found_names}")
            _LOGGER.error(f"Make sure the name matches exactly what appears in the scan output.")
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
            
        # Scan for all devices at once and store IPs
        device_ip_map = {}
        for device_name in self.devices.keys():
            ip = await self.async_get_device_ip(device_name)
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
                if not await self.async_is_within_time_window(device_name, device_config):
                    _LOGGER.debug(f"Outside casting time window for {device_name}, skipping check")
                    continue
                
                # Check if media is playing before attempting to reconnect
                is_media_playing = await self.async_is_media_playing(ip)
                if is_media_playing:
                    _LOGGER.info(f"Media is currently playing on {device_name}, skipping status check")
                    # Update device status to media_playing
                    if device_key in self.active_devices:
                        # If device was previously connected to our dashboard, add a delay before marking as media_playing
                        # This prevents rapid switching when "Hey Google" commands are being processed
                        if self.active_devices[device_key].get('status') == 'connected':
                            _LOGGER.info(f"Device {device_name} was showing our dashboard but now has media - giving it time to stabilize")
                            # Don't update the status yet, let it remain as 'connected' for this cycle
                        else:
                            self.active_devices[device_key]['status'] = 'media_playing'
                        self.active_devices[device_key]['last_checked'] = datetime.now().isoformat()
                    else:
                        # First time seeing this device
                        self.active_devices[device_key] = {
                            'name': device_name,
                            'ip': ip,
                            'status': 'media_playing',
                            'first_seen': datetime.now().isoformat(),
                            'last_checked': datetime.now().isoformat(),
                            'reconnect_attempts': 0
                        }
                    continue
                
                # Check if device is still casting our dashboard
                is_casting = await self.async_check_device_status(ip)
                
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
                if device_key in self.active_devices:
                    previous_status = self.active_devices[device_key].get('status', 'unknown')
                    last_status_change = self.active_devices[device_key].get('last_status_change', 0)
                    current_time = time.time()
                    
                    # Determine current state and take appropriate action
                    if is_casting:
                        # Device is showing our dashboard
                        if previous_status != 'connected':
                            self.active_devices[device_key]['last_status_change'] = current_time
                        self.active_devices[device_key]['status'] = 'connected'
                        if previous_status != 'connected':
                            _LOGGER.info(f"Device {device_name} ({ip}) is now connected")
                            self.active_devices[device_key]['reconnect_attempts'] = 0
                            await self.async_update_health_stats(device_key, 'reconnected')
                    elif is_idle:
                        # Device is idle, should show our dashboard
                        # Add a delay after any status change to prevent rapid reconnects
                        # This gives voice commands time to be processed
                        min_time_between_reconnects = 30  # seconds
                        time_since_last_change = current_time - last_status_change
                        
                        if previous_status != 'disconnected':
                            _LOGGER.info(f"Device {device_name} ({ip}) is idle and not casting our dashboard")
                            self.active_devices[device_key]['status'] = 'disconnected'
                            self.active_devices[device_key]['last_status_change'] = current_time
                        else:
                            # Only attempt to reconnect if enough time has passed since last status change
                            if time_since_last_change > min_time_between_reconnects:
                                _LOGGER.info(f"Device {device_name} ({ip}) is still idle after waiting period, attempting reconnect")
                                await self.async_reconnect_device(device_name, ip, device_config)
                            else:
                                _LOGGER.debug(f"Device {device_name} ({ip}) is idle but waiting {int(min_time_between_reconnects - time_since_last_change)}s before reconnecting")
                    else:
                        # Device has other content
                        if previous_status != 'other_content':
                            self.active_devices[device_key]['last_status_change'] = current_time
                        _LOGGER.info(f"Device {device_name} ({ip}) has other content (not our dashboard and not idle)")
                        self.active_devices[device_key]['status'] = 'other_content'
                    
                    # Update timestamp regardless
                    self.active_devices[device_key]['last_checked'] = datetime.now().isoformat()
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
                    
                    self.active_devices[device_key] = {
                        'name': device_name,
                        'ip': ip,
                        'status': status,
                        'first_seen': datetime.now().isoformat(),
                        'last_checked': datetime.now().isoformat(),
                        'last_status_change': time.time(),
                        'reconnect_attempts': 0
                    }
                    
                    # Add a delay before first reconnect attempt to allow device to stabilize
                    # This prevents reconnecting during voice command processing
                    if status == 'disconnected':
                        _LOGGER.info(f"Will attempt first connection to {device_name} in next monitoring cycle")
                        # No immediate reconnect on first sight

    async def async_reconnect_device(self, device_name, ip, device_config):
        """Attempt to reconnect a disconnected device."""
        device_key = f"{device_name}_{ip}"
        
        # Skip if outside time window
        if not await self.async_is_within_time_window(device_name, device_config):
            _LOGGER.info(f"Outside casting time window for {device_name}, skipping reconnect")
            return False
        
        # Check if media is playing before attempting to reconnect
        if await self.async_is_media_playing(ip):
            _LOGGER.info(f"Media is currently playing on {device_name}, skipping reconnect")
            if device_key in self.active_devices:
                self.active_devices[device_key]['status'] = 'media_playing'
            return False
        
        # Increment reconnect attempts
        if device_key in self.active_devices:
            self.active_devices[device_key]['reconnect_attempts'] += 1
            attempts = self.active_devices[device_key]['reconnect_attempts']
            
            # If too many reconnect attempts, back off
            if attempts > 10:  # Increased from 5 to 10
                _LOGGER.warning(f"Device {device_name} ({ip}) has had {attempts} reconnect attempts, backing off")
                await self.async_update_health_stats(device_key, 'reconnect_failed')
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
                if device_key in self.active_devices:
                    self.active_devices[device_key]['status'] = 'other_content'
                return False
        
        _LOGGER.info(f"Attempting to reconnect to {device_name} ({ip})")
        await self.async_update_health_stats(device_key, 'reconnect_attempt')
        dashboard_url = device_config.get('dashboard_url')
        _LOGGER.debug(f"Casting URL {dashboard_url} to device {device_name} ({ip})")
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

    async def async_generate_status_data(self, *args):
        """Generate status data for Home Assistant sensors."""
        connected_count = sum(1 for d in self.active_devices.values() if d.get('status') == 'connected')
        disconnected_count = sum(1 for d in self.active_devices.values() if d.get('status') == 'disconnected')
        media_playing_count = sum(1 for d in self.active_devices.values() if d.get('status') == 'media_playing')
        other_content_count = sum(1 for d in self.active_devices.values() if d.get('status') == 'other_content')
        
        # Format for Home Assistant sensors
        status_data = {
            'total_devices': len(self.active_devices),
            'connected_devices': connected_count,
            'disconnected_devices': disconnected_count,
            'media_playing_devices': media_playing_count,
            'other_content_devices': other_content_count,
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
            def write_status_file():
                os.makedirs('/config/continuously_casting_dashboards', exist_ok=True)
                with open('/config/continuously_casting_dashboards/status.json', 'w') as f:
                    json.dump(status_data, f, indent=2)
                    
            await self.hass.async_add_executor_job(write_status_file)
        except Exception as e:
            _LOGGER.error(f"Failed to save status data: {str(e)}")
            
        return status_data
    
    async def async_save_health_stats(self):
        """Save health statistics to file."""
        try:
            def write_health_stats():
                stats_dir = '/config/continuously_casting_dashboards'
                os.makedirs(stats_dir, exist_ok=True)
                with open(f'{stats_dir}/health_stats.json', 'w') as f:
                    json.dump(self.health_stats, f, indent=2)
                    
            await self.hass.async_add_executor_job(write_health_stats)
        except Exception as e:
            _LOGGER.error(f"Failed to save health stats: {str(e)}")
