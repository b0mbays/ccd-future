"""Utility functions for Continuously Casting Dashboards."""
import logging
from datetime import time as dt_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from .const import DEFAULT_START_TIME, DEFAULT_END_TIME, CONF_SWITCH_ENTITY

_LOGGER = logging.getLogger(__name__)

class TimeWindowChecker:
    """Class to handle time window checking."""
    
    def __init__(self, config: dict):
        """Initialize the time window checker."""
        self.config = config
        self.default_start_time = config.get('start_time', DEFAULT_START_TIME)
        self.default_end_time = config.get('end_time', DEFAULT_END_TIME)
    
    async def async_is_within_time_window(self, device_name, device_config):
        """Check if current time is within the casting window for a device."""
        now = dt_util.now().time()
        
        # First check device-specific time window
        device_start = device_config.get('start_time', self.default_start_time)
        device_end = device_config.get('end_time', self.default_end_time)
        
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


class SwitchEntityChecker:
    """Class to handle switch entity checking."""
    
    def __init__(self, hass: HomeAssistant, config: dict):
        """Initialize the switch entity checker."""
        self.hass = hass
        self.switch_entity_id = config.get(CONF_SWITCH_ENTITY)
    
    async def async_check_switch_entity(self):
        """Check if the switch entity is enabled (if configured)."""
        if not self.switch_entity_id:
            return True  # No switch configured, always enabled
        
        state = self.hass.states.get(self.switch_entity_id)
        if state is None:
            _LOGGER.warning(f"Switch entity {self.switch_entity_id} not found")
            return True  # If entity doesn't exist, default to enabled
        
        return state.state == 'on'