"""Group-aware media player for each Google Cast speaker."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any
from uuid import UUID

from pychromecast.controllers.media import (
    MEDIA_PLAYER_STATE_BUFFERING,
    MEDIA_PLAYER_STATE_PAUSED,
    MEDIA_PLAYER_STATE_PLAYING,
)
from pychromecast.error import PyChromecastError

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    DATA_COMPONENT,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    BrowseMedia,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.const import ATTR_ENTITY_ID, EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import CastGroupSyncConfigEntry
from .const import CAST_DOMAIN, CAST_GROUP_MODEL, DOMAIN
from .session import CastSessions, Session, as_uuid

_LOGGER = logging.getLogger(__name__)

# Group membership changes don't always produce a stock state change.
SCAN_INTERVAL = timedelta(seconds=5)
PARALLEL_UPDATES = 0
REFRESH_DELAY = 2.0

STATE_MAP = {
    MEDIA_PLAYER_STATE_PLAYING: MediaPlayerState.PLAYING,
    MEDIA_PLAYER_STATE_BUFFERING: MediaPlayerState.BUFFERING,
    MEDIA_PLAYER_STATE_PAUSED: MediaPlayerState.PAUSED,
}

# Copied from the stock entity when this speaker isn't part of a session.
MIRRORED = (
    "state",
    "app_id",
    "app_name",
    "media_content_id",
    "media_content_type",
    "media_title",
    "media_artist",
    "media_album_name",
    "media_album_artist",
    "media_track",
    "media_duration",
    "media_position",
    "media_position_updated_at",
    "media_image_url",
    "media_series_title",
    "media_season",
    "media_episode",
)


def _is_cast_group(hass: HomeAssistant, entry: er.RegistryEntry) -> bool:
    component = hass.data.get(DATA_COMPONENT)
    stock = component.get_entity(entry.entity_id) if component else None
    if (info := getattr(stock, "_cast_info", None)) is not None:
        return info.is_audio_group
    device = dr.async_get(hass).async_get(entry.device_id) if entry.device_id else None
    return device is not None and device.model == CAST_GROUP_MODEL


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CastGroupSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a synced player for every stock Cast speaker, including ones found later."""
    sessions = entry.runtime_data
    added: set[str] = set()

    @callback
    def _add_new(_event: Event | None = None) -> None:
        new = []
        for registry_entry in list(er.async_get(hass).entities.values()):
            if (
                registry_entry.platform != CAST_DOMAIN
                or registry_entry.domain != MEDIA_PLAYER_DOMAIN
                or registry_entry.disabled
                or registry_entry.unique_id in added
                or as_uuid(registry_entry.unique_id) is None
                or _is_cast_group(hass, registry_entry)
            ):
                continue
            added.add(registry_entry.unique_id)
            new.append(SyncedCastPlayer(sessions, registry_entry))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _add_new)
    )


class SyncedCastPlayer(MediaPlayerEntity):
    """A Cast speaker that reports, and controls, the session it's playing."""

    _attr_has_entity_name = True
    _attr_name = "Synced"
    _attr_should_poll = True
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_media_image_remotely_accessible = True

    def __init__(self, sessions: CastSessions, registry_entry: er.RegistryEntry) -> None:
        """Initialise."""
        self._sessions = sessions
        self._stock_registry_id = registry_entry.id
        self._uuid = UUID(registry_entry.unique_id)
        self._session: Session | None = None
        self._attr_unique_id = registry_entry.unique_id
        # Attach to the stock device so area and naming follow the speaker.
        self._attr_device_info = DeviceInfo(identifiers={(CAST_DOMAIN, self._uuid.hex)})

    # ---------- stock entity access ----------

    def _stock_entity_id(self) -> str | None:
        entry = er.async_get(self.hass).async_get(self._stock_registry_id)
        return entry.entity_id if entry else None

    def _stock(self) -> MediaPlayerEntity | None:
        component = self.hass.data.get(DATA_COMPONENT)
        entity_id = self._stock_entity_id()
        return component.get_entity(entity_id) if component and entity_id else None

    def _entity_id_for(self, uuid: UUID) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(
            MEDIA_PLAYER_DOMAIN, DOMAIN, str(uuid)
        )

    def _uuid_for(self, entity_id: str) -> UUID | None:
        """Speaker UUID for a synced or stock Cast entity ID."""
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None or entry.platform not in (DOMAIN, CAST_DOMAIN):
            return None
        return as_uuid(entry.unique_id)

    # ---------- state ----------

    async def async_added_to_hass(self) -> None:
        """Refresh whenever any stock Cast player changes."""
        registry = er.async_get(self.hass)

        @callback
        def _is_cast(event_data: EventStateChangedData) -> bool:
            entity_id = event_data["entity_id"]
            if not entity_id.startswith(f"{MEDIA_PLAYER_DOMAIN}."):
                return False
            entry = registry.async_get(entity_id)
            return entry is not None and entry.platform == CAST_DOMAIN

        @callback
        def _changed(_event: Event[EventStateChangedData]) -> None:
            self.async_schedule_update_ha_state(True)

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, _changed, event_filter=_is_cast)
        )
        self.async_schedule_update_ha_state(True)

    async def async_update(self) -> None:
        """Mirror the session this speaker plays, else the stock entity."""
        stock = self._stock()
        if stock is None or not stock.available:
            self._attr_available = False
            self._session = None
            return
        self._attr_available = True
        self._attr_volume_level = stock.volume_level
        self._attr_is_volume_muted = stock.is_volume_muted

        session = self._session = self._sessions.session_for(self._uuid)
        if session is None or session.media_status is None:
            for attribute in MIRRORED:
                setattr(self, f"_attr_{attribute}", getattr(stock, attribute))
            if session is not None:
                # Group endpoint running an app but nothing reports a track yet.
                self._attr_state = MediaPlayerState.IDLE
                self._attr_app_id = session.app_id
                self._attr_app_name = session.app_name
        else:
            self._mirror_session(session)

        members = (
            [entity_id for uuid in session.members if (entity_id := self._entity_id_for(uuid))]
            if session
            else []
        )
        self._attr_group_members = members or ([self.entity_id] if self.entity_id else [])

    def _mirror_session(self, session: Session) -> None:
        status = session.media_status
        assert status is not None
        self._attr_state = STATE_MAP.get(status.player_state, MediaPlayerState.IDLE)
        self._attr_app_id = session.app_id
        self._attr_app_name = session.app_name
        self._attr_media_content_id = status.content_id
        if status.media_is_tvshow:
            self._attr_media_content_type = MediaType.TVSHOW
        elif status.media_is_movie:
            self._attr_media_content_type = MediaType.MOVIE
        else:
            self._attr_media_content_type = MediaType.MUSIC
        self._attr_media_title = status.title
        self._attr_media_artist = status.artist
        self._attr_media_album_name = status.album_name
        self._attr_media_album_artist = status.album_artist
        self._attr_media_track = status.track
        self._attr_media_duration = status.duration
        self._attr_media_series_title = status.series_title
        self._attr_media_season = status.season
        self._attr_media_episode = status.episode
        images = status.images
        self._attr_media_image_url = images[0].url if images and images[0].url else None
        if status.player_is_playing or status.player_is_paused or status.player_is_idle:
            self._attr_media_position = status.current_time
            self._attr_media_position_updated_at = session.media_status_received
        else:
            self._attr_media_position = None
            self._attr_media_position_updated_at = None

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Volume is per speaker; transport controls follow the session."""
        stock = self._stock() if self.hass else None
        stock_features = stock.supported_features if stock else MediaPlayerEntityFeature(0)
        features = (
            MediaPlayerEntityFeature.GROUPING
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            | (
                stock_features
                & (
                    MediaPlayerEntityFeature.PLAY_MEDIA
                    | MediaPlayerEntityFeature.BROWSE_MEDIA
                    | MediaPlayerEntityFeature.VOLUME_SET
                    | MediaPlayerEntityFeature.VOLUME_MUTE
                )
            )
        )
        if features & MediaPlayerEntityFeature.VOLUME_SET:
            features |= MediaPlayerEntityFeature.VOLUME_STEP

        session = self._session
        status = session.media_status if session else None
        if status is None:
            return features | (
                stock_features
                & (
                    MediaPlayerEntityFeature.PLAY
                    | MediaPlayerEntityFeature.PAUSE
                    | MediaPlayerEntityFeature.STOP
                    | MediaPlayerEntityFeature.NEXT_TRACK
                    | MediaPlayerEntityFeature.PREVIOUS_TRACK
                    | MediaPlayerEntityFeature.SEEK
                )
            )
        features |= (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
        )
        if status.supports_queue_next:
            features |= (
                MediaPlayerEntityFeature.NEXT_TRACK | MediaPlayerEntityFeature.PREVIOUS_TRACK
            )
        if status.supports_seek:
            features |= MediaPlayerEntityFeature.SEEK
        return features

    # ---------- commands ----------

    @callback
    def _refresh_soon(self) -> None:
        self.async_schedule_update_ha_state(True)

        @callback
        def _later(_now) -> None:
            self.async_schedule_update_ha_state(True)

        async_call_later(self.hass, REFRESH_DELAY, _later)

    async def _call_stock(self, service: str, data: dict[str, Any] | None = None) -> None:
        if (entity_id := self._stock_entity_id()) is None:
            raise HomeAssistantError("The Google Cast entity for this speaker is missing")
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            service,
            {ATTR_ENTITY_ID: entity_id, **(data or {})},
            blocking=True,
        )
        self._refresh_soon()

    async def _session_command(self, service: str, method: str, *args: Any) -> None:
        """Send a transport command to the session's source, else the stock entity."""
        session = self._sessions.session_for(self._uuid)
        if session is None or session.media_status is None:
            await self._call_stock(service)
            return
        try:
            await self.hass.async_add_executor_job(
                getattr(session.controller, method), *args
            )
        except PyChromecastError as err:
            raise HomeAssistantError(f"{method} failed: {err}") from err
        self._refresh_soon()

    async def async_media_play(self) -> None:
        """Resume the session."""
        await self._session_command("media_play", "play")

    async def async_media_pause(self) -> None:
        """Pause the session."""
        await self._session_command("media_pause", "pause")

    async def async_media_stop(self) -> None:
        """Stop the session."""
        await self._session_command("media_stop", "stop")

    async def async_media_next_track(self) -> None:
        """Skip forward."""
        await self._session_command("media_next_track", "queue_next")

    async def async_media_previous_track(self) -> None:
        """Skip back."""
        await self._session_command("media_previous_track", "queue_prev")

    async def async_media_seek(self, position: float) -> None:
        """Seek within the track."""
        session = self._sessions.session_for(self._uuid)
        if session is None or session.media_status is None:
            await self._call_stock("media_seek", {"seek_position": position})
            return
        await self._session_command("media_seek", "seek", position)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set this speaker's volume."""
        await self._call_stock("volume_set", {ATTR_MEDIA_VOLUME_LEVEL: volume})

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute this speaker."""
        await self._call_stock("volume_mute", {ATTR_MEDIA_VOLUME_MUTED: mute})

    async def async_turn_on(self) -> None:
        """Turn the speaker on."""
        await self._call_stock("turn_on")

    async def async_turn_off(self) -> None:
        """Leave the group if grouped, so the others keep playing; else stop the app."""
        session = self._sessions.session_for(self._uuid)
        if session is not None and len(session.members) > 1:
            await self.async_unjoin_player()
            return
        await self._call_stock("turn_off")

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play media through the stock entity."""
        data: dict[str, Any] = {
            ATTR_MEDIA_CONTENT_TYPE: media_type,
            ATTR_MEDIA_CONTENT_ID: media_id,
        }
        if extra := kwargs.get(ATTR_MEDIA_EXTRA):
            data[ATTR_MEDIA_EXTRA] = extra
        await self._call_stock("play_media", data)

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse with the stock entity."""
        if (stock := self._stock()) is None:
            raise HomeAssistantError("The Google Cast entity for this speaker is missing")
        return await stock.async_browse_media(media_content_type, media_content_id)

    async def async_join_players(self, group_members: list[str]) -> None:
        """Add speakers to what this speaker is playing, without interrupting it."""
        joining: list[UUID] = []
        for entity_id in group_members:
            if (uuid := self._uuid_for(entity_id)) is None:
                raise HomeAssistantError(f"{entity_id} isn't a Google Cast speaker")
            joining.append(uuid)

        anchor = self._uuid
        if self._sessions.session_for(anchor) is None:
            # Nothing here: pull this speaker into a session one of the others is playing.
            anchor = next(
                (uuid for uuid in joining if self._sessions.session_for(uuid)), anchor
            )
            joining.append(self._uuid)

        await self._sessions.async_change_members(
            anchor,
            lambda current: [*current, *(u for u in joining if u not in current)],
            adding=True,
        )
        self._refresh_soon()

    async def async_unjoin_player(self) -> None:
        """Take this speaker out of its group; the rest keep playing."""
        session = self._sessions.session_for(self._uuid)
        if session is None or len(session.members) < 2:
            return
        await self._sessions.async_change_members(
            self._uuid,
            lambda current: [uuid for uuid in current if uuid != self._uuid],
            adding=False,
        )
        self._refresh_soon()
