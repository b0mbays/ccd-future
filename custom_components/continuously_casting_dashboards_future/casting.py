"""Casting functionality for Continuously Casting Dashboards."""
import asyncio
import logging
from datetime import datetime
from homeassistant.core import HomeAssistant
from .const import DEFAULT_MAX_RETRIES, DEFAULT_RETRY_DELAY, DEFAULT_VERIFICATION_WAIT_TIME

_LOGGER = logging.getLogger(__name__)

class CastingManager:
    """Class to handle casting to devices."""

    def __init__(self, hass: HomeAssistant, config: dict, device_manager):
        """Initialize the casting manager."""
        self.hass = hass
        self.config = config
        self.device_manager = device_manager
        self.cast_delay = config.get('cast_delay', 0)

    async def async_cast_dashboard(self, ip, dashboard_url, device_config):
        """Cast a dashboard to a device with retry logic."""
        volume = device_config.get('volume', 5)
        max_retries = self.config.get('max_retries', DEFAULT_MAX_RETRIES)
        retry_delay = self.config.get('retry_delay', DEFAULT_RETRY_DELAY)
        verification_wait_time = self.config.get('verification_wait_time', DEFAULT_VERIFICATION_WAIT_TIME)
        
        for attempt in range(max_retries):
            try:
                # Check if media is playing before casting
                if await self.device_manager.async_is_media_playing(ip):
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
                
                status_check = await self.device_manager.async_check_device_status(ip)
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