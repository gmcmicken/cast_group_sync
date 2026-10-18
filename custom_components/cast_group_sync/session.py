"""Find Cast playback sessions and change which speakers they play on.

Reads the stock Google Cast integration's live pychromecast connections and its
multizone manager, so no extra connections are opened in normal use.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging
from time import monotonic
from typing import Any
from uuid import UUID

from pychromecast import Chromecast, get_chromecast_from_cast_info
from pychromecast.const import CAST_TYPE_GROUP
from pychromecast.controllers.media import (
    MEDIA_PLAYER_STATE_BUFFERING,
    MEDIA_PLAYER_STATE_PAUSED,
    MEDIA_PLAYER_STATE_PLAYING,
    MEDIA_PLAYER_STATE_UNKNOWN,
    MediaController,
    MediaStatus,
)
from pychromecast.controllers.multizone import MULTIZONE_NAMESPACE
from pychromecast.models import CastInfo, HostServiceInfo

from homeassistant.components.media_player import DATA_COMPONENT
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import CAST_DOMAIN

_LOGGER = logging.getLogger(__name__)

STATUS_TIMEOUT = 5.0
CHANGE_TIMEOUT = 15.0
CONNECT_TIMEOUT = 10.0
# Membership from the last change is shown until the stock integration catches up.
PENDING_HOLD = 15.0

_ACTIVE_RANK = {
    MEDIA_PLAYER_STATE_PLAYING: 3,
    MEDIA_PLAYER_STATE_BUFFERING: 3,
    MEDIA_PLAYER_STATE_PAUSED: 2,
}


class FinalError(HomeAssistantError):
    """A change failure that trying another endpoint can't fix."""


def as_uuid(value: Any) -> UUID | None:
    """Normalise a Cast device ID; both dashed and dashless forms occur."""
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _rank(status: MediaStatus | None) -> int:
    """How useful a media status is: 0 = none, 1 = idle, 2 = paused, 3 = playing."""
    if status is None or status.player_state in (None, MEDIA_PLAYER_STATE_UNKNOWN):
        return 0
    return _ACTIVE_RANK.get(status.player_state, 1)


def _running(entity: Any) -> bool:
    """Whether a stock speaker is connected and running an app."""
    chromecast = getattr(entity, "_chromecast", None)
    return chromecast is not None and not chromecast.is_idle


@dataclass
class Session:
    """One playback session and the speakers it plays on."""

    members: list[UUID]
    """Speakers showing this session, the speaker holding the media status first."""
    endpoints: list[Chromecast]
    """Connections that may own the session, most likely first."""
    source: Chromecast
    """Connection whose media status and controller are used."""
    media_status: MediaStatus | None
    media_status_received: datetime | None
    dynamic: bool = False

    @property
    def controller(self) -> MediaController:
        """Media controller for play/pause/skip."""
        return self.source.media_controller

    @property
    def app_id(self) -> str | None:
        """App running the session."""
        return self.source.app_id

    @property
    def app_name(self) -> str | None:
        """Display name of the app running the session."""
        return self.source.app_display_name


@dataclass
class _Pending:
    """Membership set by our last change, held while the devices catch up."""

    session_ids: set[str]
    leader: UUID | None
    members: list[UUID]
    endpoint: tuple[str, int, UUID] | None
    until: float = field(default_factory=lambda: monotonic() + PENDING_HOLD)


class CastSessions:
    """Session lookup and stream expansion over the stock Cast connections."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise."""
        self.hass = hass
        self._lock = asyncio.Lock()
        self._pending: list[_Pending] = []
        self._warned_private_api = False

    # ---------- stock integration access ----------

    def _cast_entry(self) -> ConfigEntry | None:
        for entry in self.hass.config_entries.async_entries(CAST_DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                return entry
        return None

    def speakers(self) -> dict[UUID, Any]:
        """Stock Cast speaker entities by device UUID; groups are excluded."""
        component = self.hass.data.get(DATA_COMPONENT)
        if component is None:
            return {}
        speakers = {}
        for entity in component.entities:
            platform = entity.platform
            info = getattr(entity, "_cast_info", None)
            if (
                platform is None
                or platform.platform_name != CAST_DOMAIN
                or info is None
                or info.is_audio_group
            ):
                continue
            speakers[info.uuid] = entity
        return speakers

    def _groups(self) -> list[tuple[UUID, Chromecast, list[UUID]]]:
        """Static and dynamic groups the stock integration is connected to, with members."""
        entry = self._cast_entry()
        manager = getattr(entry.runtime_data, "multizone_manager", None) if entry else None
        if manager is None:
            return []
        groups = []
        try:
            for group_id, info in list(manager._groups.items()):  # noqa: SLF001
                group_uuid = as_uuid(group_id)
                members = [
                    uuid
                    for member in info["listener"]._mz.members  # noqa: SLF001
                    if (uuid := as_uuid(member))
                ]
                if group_uuid:
                    groups.append((group_uuid, info["chromecast"], members))
        except (AttributeError, KeyError, TypeError):
            if not self._warned_private_api:
                self._warned_private_api = True
                _LOGGER.warning(
                    "The Google Cast integration's group tracking has changed; "
                    "group membership can't be read"
                )
            return []
        return groups

    def _cast_info(self, uuid: UUID, speakers: dict[UUID, Any]) -> CastInfo | None:
        if (chromecast := getattr(speakers.get(uuid), "_chromecast", None)) is not None:
            return chromecast.cast_info
        entry = self._cast_entry()
        browser = getattr(entry.runtime_data, "browser", None) if entry else None
        return browser.devices.get(uuid) if browser else None

    def name(self, uuid: UUID) -> str:
        """Friendly name of a Cast device."""
        info = self._cast_info(uuid, self.speakers())
        return (info.friendly_name if info else None) or str(uuid)

    # ---------- session discovery ----------

    def _registered_uuids(self) -> set[UUID]:
        """Cast devices with entities: speakers and static groups, never dynamic groups."""
        return {
            uuid
            for entry in er.async_get(self.hass).entities.values()
            if entry.platform == CAST_DOMAIN and (uuid := as_uuid(entry.unique_id))
        }

    def sessions(self) -> list[Session]:
        """Every playback session, with each speaker in at most one."""
        speakers = self.speakers()
        registered = self._registered_uuids()

        candidates: list[tuple[tuple[int, bool, int], Session]] = []
        for group_uuid, group_cast, raw_members in self._groups():
            if not raw_members:
                continue
            # Dynamic groups exist only while a session spans several speakers.
            dynamic = group_uuid not in registered
            app_running = not group_cast.is_idle
            if not dynamic and not app_running:
                continue

            source, status, received = self._best_member(raw_members, speakers)
            group_status = group_cast.media_controller.status
            if _rank(group_status) and _rank(group_status) >= _rank(status):
                source, status = None, group_status
                received = group_status.last_updated
            if not _rank(status) and not app_running:
                continue

            # Speakers that left a session stop running its app; followers keep it.
            members = [
                uuid
                for uuid in raw_members
                if uuid == source or uuid not in speakers or _running(speakers[uuid])
            ]
            if source in members:
                members.remove(source)
                members.insert(0, source)
            source_cast = speakers[source]._chromecast if source else group_cast  # noqa: SLF001
            endpoints = [group_cast]
            if source and source_cast is not None:
                # A lone member's own connection owns the session; otherwise the group does.
                endpoints.insert(0 if len(members) == 1 else 1, source_cast)
            session = Session(
                members=members,
                endpoints=endpoints,
                source=source_cast or group_cast,
                media_status=status,
                media_status_received=received,
                dynamic=dynamic,
            )
            candidates.append(((_rank(status), dynamic, len(members)), session))

        sessions: list[Session] = []
        claimed: set[UUID] = set()
        for _, session in sorted(candidates, key=lambda item: item[0], reverse=True):
            session.members = [uuid for uuid in session.members if uuid not in claimed]
            if not any(uuid in speakers for uuid in session.members):
                continue
            claimed.update(session.members)
            sessions.append(session)

        for uuid, entity in speakers.items():
            if uuid in claimed or not _running(entity):
                continue
            if not _rank(status := entity.media_status):
                # A follower without its own media status, or nothing playing.
                continue
            chromecast = entity._chromecast  # noqa: SLF001
            sessions.append(
                Session(
                    members=[uuid],
                    endpoints=[chromecast],
                    source=chromecast,
                    media_status=status,
                    media_status_received=entity.media_status_received,
                )
            )

        return self._apply_pending(sessions)

    @staticmethod
    def _best_member(
        members: list[UUID], speakers: dict[UUID, Any]
    ) -> tuple[UUID | None, MediaStatus | None, datetime | None]:
        """The member whose own connection carries the session's media status."""
        best: tuple[UUID | None, MediaStatus | None, datetime | None] = (None, None, None)
        for uuid in members:
            entity = speakers.get(uuid)
            if entity is None or not _running(entity):
                continue
            if _rank(entity.media_status) > _rank(best[1]):
                best = (uuid, entity.media_status, entity.media_status_received)
        return best

    @staticmethod
    def _session_ids(session: Session) -> set[str]:
        return {
            chromecast.status.session_id
            for chromecast in session.endpoints
            if chromecast.status and chromecast.status.session_id
        }

    def _apply_pending(self, sessions: list[Session]) -> list[Session]:
        now = monotonic()
        self._pending = [pending for pending in self._pending if pending.until > now]
        for pending in self._pending:
            match = next(
                (
                    session
                    for session in sessions
                    if pending.session_ids & self._session_ids(session)
                    or (pending.leader and pending.leader in session.members)
                ),
                None,
            )
            if match is None:
                continue
            if set(match.members) == set(pending.members):
                pending.until = 0.0
                continue
            match.members = list(pending.members)
            for session in sessions:
                if session is not match:
                    session.members = [
                        uuid for uuid in session.members if uuid not in pending.members
                    ]
        self._pending = [pending for pending in self._pending if pending.until > now]
        return [session for session in sessions if session.members]

    def session_for(self, uuid: UUID) -> Session | None:
        """The session a speaker is playing, if any."""
        return next(
            (session for session in self.sessions() if uuid in session.members), None
        )

    # ---------- multizone protocol ----------

    async def _request(
        self, chromecast: Chromecast, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """Send a multizone message to the platform and wait for the matching reply."""
        loop = self.hass.loop
        future: asyncio.Future[dict[str, Any]] = loop.create_future()

        def _set(ok: bool, response: dict[str, Any] | None) -> None:
            if future.done():
                return
            if ok and response is not None:
                future.set_result(response)
            else:
                future.set_exception(
                    HomeAssistantError(f"No reply from {chromecast.uri}")
                )

        def _callback(ok: bool, response: dict[str, Any] | None) -> None:
            loop.call_soon_threadsafe(_set, ok, response)

        def _send() -> None:
            chromecast.socket_client.send_platform_message(
                MULTIZONE_NAMESPACE, payload, callback_function=_callback
            )

        try:
            await self.hass.async_add_executor_job(_send)
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as err:
            raise HomeAssistantError(f"Timed out waiting for {chromecast.uri}") from err
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Couldn't reach {chromecast.uri}: {err}") from err
        finally:
            # The socket client keeps unanswered callbacks; drop ours.
            if (request_id := payload.get("requestId")) is not None:
                chromecast.socket_client._request_callbacks.pop(request_id, None)  # noqa: SLF001

    async def _connect(self, host: str, port: int, uuid: UUID) -> Chromecast:
        """Connect to a session endpoint the stock integration hasn't discovered yet."""
        cast_info = CastInfo(
            {HostServiceInfo(host, port)},
            uuid,
            None,
            None,
            host,
            port,
            CAST_TYPE_GROUP,
            None,
        )

        def _start() -> Chromecast:
            chromecast = get_chromecast_from_cast_info(cast_info, None, tries=1)
            chromecast.wait(timeout=CONNECT_TIMEOUT)
            return chromecast

        return await self.hass.async_add_executor_job(_start)

    def _device_entry(self, uuid: UUID, speakers: dict[UUID, Any]) -> dict[str, Any]:
        entry: dict[str, Any] = {"deviceId": str(uuid)}
        if (info := self._cast_info(uuid, speakers)) and info.host:
            entry["ipAddress"] = info.host
            entry["port"] = info.port or 8009
        return entry

    async def async_change_members(
        self,
        anchor: UUID,
        change: Callable[[list[UUID]], list[UUID]],
        *,
        adding: bool,
    ) -> None:
        """Replace the speaker set of the session playing on `anchor`.

        `change` receives the session's current speakers and returns the new set.
        """
        async with self._lock:
            session = self.session_for(anchor)
            if session is None:
                raise HomeAssistantError(f"Nothing is playing on {self.name(anchor)}")
            speakers = self.speakers()

            temporary: list[Chromecast] = []
            try:
                endpoints = await self._endpoints(session, temporary)
                errors: list[str] = []
                for chromecast in endpoints:
                    try:
                        if await self._change_at(
                            chromecast, session, change, speakers, adding=adding
                        ):
                            return
                    except FinalError:
                        raise
                    except HomeAssistantError as err:
                        _LOGGER.debug("Change at %s failed: %s", chromecast.uri, err)
                        errors.append(str(err))
                raise HomeAssistantError(
                    "Couldn't change the speakers: " + ("; ".join(errors) or "no endpoint")
                )
            finally:
                for chromecast in temporary:
                    await self.hass.async_add_executor_job(chromecast.disconnect, 5)

    async def _endpoints(
        self, session: Session, temporary: list[Chromecast]
    ) -> list[Chromecast]:
        """Candidate endpoints; ones that report a playback session go first."""
        endpoints = list(session.endpoints)
        known = {(c.socket_client.host, c.socket_client.port) for c in endpoints}
        for pending in self._pending:
            if pending.endpoint is None or not (
                pending.session_ids & self._session_ids(session)
                or pending.leader in session.members
            ):
                continue
            host, port, uuid = pending.endpoint
            if (host, port) in known:
                continue
            try:
                chromecast = await self._connect(host, port, uuid)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Couldn't connect to %s:%s", host, port, exc_info=True)
                continue
            temporary.append(chromecast)
            endpoints.insert(0, chromecast)
            known.add((host, port))
        return endpoints

    async def _change_at(
        self,
        chromecast: Chromecast,
        session: Session,
        change: Callable[[list[UUID]], list[UUID]],
        speakers: dict[UUID, Any],
        *,
        adding: bool,
    ) -> bool:
        """Try the change at one endpoint. False means it isn't the live endpoint."""
        status = chromecast.status
        session_id = status.session_id if status else None
        if not session_id:
            return False

        reply = await self._request(chromecast, {"type": "GET_STATUS"}, STATUS_TIMEOUT)
        multizone = reply.get("status") or {}
        playback = multizone.get("playbackSession")
        if reply.get("type") != "MULTIZONE_STATUS" or playback is None:
            _LOGGER.debug("%s holds no playback session: %s", chromecast.uri, reply)
            return False
        current = [
            uuid
            for device in multizone.get("devices", [])
            if (uuid := as_uuid(device.get("deviceId")))
        ] or list(session.members)

        target = list(dict.fromkeys(change(current)))
        if not target:
            raise FinalError("At least one speaker has to keep playing")
        if set(target) == set(current):
            return True
        if adding and playback.get("appAllowsGrouping") is False:
            raise FinalError(
                f"{session.app_name or 'This app'} doesn't allow adding speakers"
            )
        transfer = not set(target) & set(current)
        if transfer and playback.get("streamTransferSupported") is False:
            raise FinalError(
                f"{session.app_name or 'This app'} doesn't support moving playback"
            )

        _LOGGER.debug(
            "Moving session %s at %s from %s to %s (transfer=%s)",
            session_id,
            chromecast.uri,
            current,
            target,
            transfer,
        )
        reply = await self._request(
            chromecast,
            {
                "type": "SET_PLAYBACK_DEVICES",
                "sessionId": session_id,
                "deviceList": [self._device_entry(uuid, speakers) for uuid in target],
            },
            CHANGE_TIMEOUT,
        )
        if reply.get("type") == "INVALID_REQUEST":
            raise HomeAssistantError(
                f"{chromecast.uri} rejected the change ({reply.get('reason', 'stale')})"
            )
        if reply.get("type") != "PLAYBACK_SESSION_UPDATED":
            raise HomeAssistantError(f"Unexpected reply from {chromecast.uri}: {reply}")

        endpoint = None
        session_ids = {session_id}
        if (moved := reply.get("sessionEndpoint")) and moved.get("sessionId") in (
            None,
            session_id,
        ):
            if (uuid := as_uuid(moved.get("deviceId"))) and moved.get("ipAddress"):
                endpoint = (moved["ipAddress"], int(moved.get("port") or 8009), uuid)

        leader = next(
            (uuid for uuid in session.members if uuid in target and uuid in speakers),
            None,
        )
        members = [uuid for uuid in target if uuid in speakers]
        if leader:
            members.remove(leader)
            members.insert(0, leader)
        self._pending.append(_Pending(session_ids, leader, members, endpoint))

        if failed := [as_uuid(device) for device in reply.get("failedDevices") or []]:
            names = ", ".join(self.name(uuid) for uuid in failed if uuid)
            raise FinalError(f"These speakers couldn't be changed: {names}")
        return True
