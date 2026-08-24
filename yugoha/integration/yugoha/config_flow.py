from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import YuGoHAApi
from .const import DOMAIN, CONF_URL, CONF_API_KEY, DEFAULT_URL


class YuGoHAConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _create_entry(self, url: str, api_key: str):
        health = await YuGoHAApi(url, api_key).health()
        if not health.get("ok"):
            raise RuntimeError("health not ok")

        await self.async_set_unique_id("yugoha_server")
        self._abort_if_unique_id_configured(
            updates={
                CONF_URL: url,
                CONF_API_KEY: api_key,
            }
        )

        project = health.get("firebase_project") or "без Firebase"
        return self.async_create_entry(
            title=f"yuGoHA Server ({project})",
            data={
                CONF_URL: url,
                CONF_API_KEY: api_key,
            },
        )

    async def async_step_hassio(
        self,
        discovery_info: HassioServiceInfo,
    ):
        """Automatic setup from the yuGoHA Home Assistant App."""
        api_key = str(discovery_info.config.get("api_key", ""))

        # discovery_info.slug is the full Supervisor app slug including
        # repository prefix. Internal Docker DNS uses '-' instead of '_'.
        host = discovery_info.slug.replace("_", "-")
        port = int(discovery_info.config.get("port", 8099))
        url = f"http://{host}:{port}"

        try:
            return await self._create_entry(url, api_key)
        except Exception:
            return self.async_abort(reason="cannot_connect")

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            api_key = user_input[CONF_API_KEY].strip()

            try:
                return await self._create_entry(url, api_key)
            except Exception:
                errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=DEFAULT_URL): str,
                vol.Required(CONF_API_KEY): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
