"""Config flow for Cast Group Sync."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class CastGroupSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Confirm setup; every Google Cast speaker is picked up automatically."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            return self.async_create_entry(title="Cast Group Sync", data={})
        return self.async_show_form(step_id="user")
