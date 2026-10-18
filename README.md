# Cast Group Sync

Adds a **Synced** media player for every Google Cast speaker (e.g. `media_player.kitchen_nest_synced`) that fixes two problems when playing music to groups of Nest speakers:

- **Every speaker in a group reports the group's playback.** With the built-in Google Cast integration, only the speaker leading a group reports `playing`; the others show `idle` or `off`. The Synced player shows the same state, track, artwork, and position on every speaker the music is playing on, and `group_members` lists them all.
- **Speakers can be added to or removed from music that is already playing** without restarting it (Cast "stream expansion", the same as the Google Home app). This works with `media_player.join` / `media_player.unjoin` and the group controls in the media player dialog.

It reuses the connections of the built-in Google Cast integration, so no extra connections are opened in normal use.

Volume and mute stay per speaker. Play, pause, skip, and seek control the whole group from any of its speakers. Turning off a grouped speaker removes it from the group and the others keep playing; turning off the last speaker stops playback.

For example, this action adds the deck and hot tub to whatever the kitchen is playing:

```yaml
action: media_player.join
target:
  entity_id: media_player.kitchen_nest_synced
data:
  group_members:
    - media_player.deck_nest_synced
    - media_player.hot_tub_nest_synced
```

And this removes the deck again:

```yaml
action: media_player.unjoin
target:
  entity_id: media_player.deck_nest_synced
```

If the target speaker isn't playing anything, it joins the music playing on one of the `group_members` instead.

Replace the example entity IDs with the ones shown for your speakers in Home Assistant.

## Limitations

- Adding speakers only works when the app allows it (YouTube Music does). If an app doesn't, the action fails with an error that says so.
- Group membership is read from internal parts of the built-in Google Cast integration. If a Home Assistant update changes those, a warning is logged and the Synced players behave like the built-in ones until this integration is updated.
- If the speaker leading a group is removed, the remaining speakers may show `idle` until the app reports again.

## Install

1. HACS → Custom repositories → add this repository as an **Integration**.
2. Install, restart Home Assistant.
3. Settings → Devices & services → Add integration → **Cast Group Sync**.

The Google Cast integration must already be set up.

## Troubleshooting

To report a problem with group detection or joining, enable debug logging and include the log:

```yaml
logger:
  logs:
    custom_components.cast_group_sync: debug
    pychromecast.controllers.multizone: debug
```
