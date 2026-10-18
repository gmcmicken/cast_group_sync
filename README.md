# Cast Group Sync

Adds a **Synced** media player for every Google Cast speaker and stereo pair (e.g. `media_player.kitchen_nest_synced`) that fixes two problems when playing music to groups of Nest speakers:

- **Every speaker in a group reports the group's playback.** With the built-in Google Cast integration, only the speaker leading a group reports `playing`; the others show `idle` or `off`. The Synced player shows the same state, track, artwork, and position on every speaker the music is playing on, and `group_members` lists them all.
- **Speakers can be added to or removed from music that is already playing** without restarting it (Cast "stream expansion", the same as the Google Home app). This works with `media_player.join` / `media_player.unjoin` and the group controls in the media player dialog.

It reuses the connections of the built-in Google Cast integration, so no extra connections are opened in normal use.

Each Synced player is on its own device and has the speaker's own name (e.g. "Kitchen Nest"). Stereo pairs get one player for the pair, since Cast plays them as a single speaker. TVs and other video devices are skipped, as are speaker groups you've made in the Google Home app (e.g. "Home Audio"); use `join` to play to several speakers instead.

Volume and mute stay per speaker. Play, pause, skip, and seek control the whole group from any of its speakers. Turning off a grouped speaker removes it from the group and the others keep playing; turning off the last speaker stops playback.

**Group Audio** (`media_player.group_audio`) shows whatever is playing on more than one speaker, always under that name, and is `off` when nothing is. Setting its volume sets every speaker in the group to exactly that level (it shows their average); mute mutes them all. Joining speakers to it adds them to the group, and `unjoin` breaks the group up, leaving the first speaker playing. Only one group is expected at a time; if there are more, it shows the one playing on the most speakers.

The built-in Google Cast entities that these replace (speakers, stereo pairs, and the "Speaker + N" groups Cast creates while music plays on several speakers) are hidden, so they drop out of auto-generated dashboards and area views but keep working. They aren't disabled, because this integration uses their connections. Unhiding one yourself sticks, and they're all unhidden if Cast Group Sync is disabled or removed.

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
