from __future__ import annotations
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from .api import YuGoHAApi
from .const import DOMAIN, CONF_URL, CONF_API_KEY, SERVICE_SEND

SEND_SCHEMA = vol.Schema({
    vol.Required("message"): cv.string,
    vol.Optional("title", default="Home Assistant"): cv.string,
    vol.Optional("priority", default=5): vol.All(vol.Coerce(int), vol.Range(min=0, max=10)),
    vol.Optional("recipient", default=""): cv.string,
})

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api = YuGoHAApi(entry.data[CONF_URL], entry.data[CONF_API_KEY])
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api

    async def handle_send(call: ServiceCall) -> None:
        await api.send(
            call.data["message"],
            call.data["title"],
            call.data["priority"],
            call.data["recipient"],
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SEND):
        hass.services.async_register(DOMAIN, SERVICE_SEND, handle_send, schema=SEND_SCHEMA)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_SEND)
    return True
