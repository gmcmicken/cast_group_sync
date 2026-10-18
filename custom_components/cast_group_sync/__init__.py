"""Group-aware Cast speakers, using the Google Cast integration's connections."""

from __future__ import annotations

from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CAST_DOMAIN, CONF_HIDDEN
from .session import CastSessions

PLATFORMS = [Platform.MEDIA_PLAYER]

type CastGroupSyncConfigEntry = ConfigEntry[CastSessions]


async def async_setup_entry(hass: HomeAssistant, entry: CastGroupSyncConfigEntry) -> bool:
    """Set up from a config entry."""
    entry.runtime_data = CastSessions(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CastGroupSyncConfigEntry) -> bool:
    """Unload a config entry; show the Google Cast entities again if disabled."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and entry.disabled_by is not None:
        _unhide_cast_entities(hass, entry)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HIDDEN: []}
        )
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: CastGroupSyncConfigEntry) -> None:
    """Show the Google Cast entities again."""
    _unhide_cast_entities(hass, entry)


@callback
def _unhide_cast_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = er.async_get(hass)
    for unique_id in entry.data.get(CONF_HIDDEN, []):
        entity_id = registry.async_get_entity_id(MEDIA_PLAYER_DOMAIN, CAST_DOMAIN, unique_id)
        entity = registry.async_get(entity_id) if entity_id else None
        if entity and entity.hidden_by is er.RegistryEntryHider.INTEGRATION:
            registry.async_update_entity(entity.entity_id, hidden_by=None)
