# Changelog

Curated, user-facing notes per release. Add a `## <version>` section before tagging — the release workflow puts the matching section into the GitHub Release body.

## 5.3.0

### Added
- **Tracks carry their track and disc numbers.** Kalinka server 5.13 passes them on to the app with the rest of a track's details.

### Changed
- **Editorial playlists keep their banner's shape.** A playlist's large picture now says it is Qobuz's wide banner, so Kalinka app 0.21.0 shows it whole instead of cropping it to a square.
- List rows show a playlist's square cover instead of a small copy of its wide banner.
- Needs Kalinka server 5.13 or newer, which brings plugin SDK 3.16. The package will not install on an older server.

## 5.2.0

### Added
- **Qobuz is offered as a home source.** The plugin reports that its browse root leads to catalogs, so the Kalinka app can list Qobuz among the music sources on Home and pin its catalogs.

### Changed
- Needs Kalinka server 5.12 or newer, which brings plugin SDK 3.14. The package will not install on an older server.

## 5.1.2

### Fixed
- **Qobuz Connect plays a track handed over at its start.** Choosing Kalinka in the Qobuz app as a track began could leave Kalinka and the app buffering, with nothing playing: the app can name a start a fraction of a second before the track's, which the player could not take. Kalinka now plays such a track from its start. Handing over partway through a track was not affected.
- A track just handed over at its start is no longer sought back to its start while it is still starting.

## 5.1.1

### Fixed
- **Qobuz autoplay adds recommendations again.** It failed while asking Qobuz for recommendations, so it added no tracks. It now sends the album's first genre, or none when the album has no genre.
- **Qobuz Connect keeps working after the first hour.** Once pairing exchanged the Qobuz app's one-hour token for a regular Qobuz user auth token, the plugin stopped renewing the app's token, which Qobuz Connect still needs to renew its session. After an hour the player waited for the Qobuz app to hand it over again. The plugin now renews that token too, keeps it across restarts, and takes the newer one from each handoff. If its renewal fails, browsing and playing Qobuz from Kalinka carry on, and choosing the player in the Qobuz app brings Qobuz Connect back.
- A renewal that fails after a newer handoff no longer marks the newer link expired.

## 5.1.0

### Added
- **Qobuz Connect plays gapless.** Shortly before a track ends, the plugin lines up the next track from the Qobuz app's queue on the output, so a live album or a DJ mix plays on without a pause between tracks. Repeating one track or the whole queue joins up the same way. If the queue changes or the next track changes in the Qobuz app, the plugin lines up the new one. If the next track cannot be fetched in time, it still plays after a short gap, as before.

### Changed
- Needs Kalinka server 5.5 or newer, which brings plugin SDK 3.6. The package will not install on an older server.
- Needs Python 3.11 or newer, as the plugin SDK and the Kalinka server do.

## 5.0.1

### Fixed
- **Switching the Qobuz app to another device stops Kalinka.** Kalinka used to play on, and the Qobuz app no longer showed it. Now Kalinka stops and its own queue takes the output back, not playing. The same happens when the app takes playback onto the phone's own speakers.
- After reconnecting, Kalinka carries on playing only if the Qobuz Connect session is the same and no other device took over meanwhile.
- Chosen again after being switched away, Kalinka shows as stopped until the Qobuz app says what to play. It no longer shows the last state it had.

### Added
- The server log now records each change of the device the Qobuz Connect session plays on, with this player's own number. It also records every play, pause, seek or track the Qobuz app asks for, and a warning when the app asks to play with no track to play. Run the server with `--debug` to also log every message from the Qobuz Connect cloud.

## 5.0.0

### Changed
- **Link Qobuz from the Qobuz app instead of pasting a token.** While Qobuz is not linked, the player shows up in the Qobuz app's device picker. Choosing it there hands the player a token, which the plugin exchanges for a regular Qobuz user auth token and checks against your account, favourites, playlists, the catalogue and a stream before using it. Browsing, search, favourites, playlists, playback quality and streaming reports work as before. Kalinka's settings say whether the link worked.
- **The *User auth token* setting is gone.** A token saved by an older version is ignored; the server logs one warning about it on each start until the entry is removed from `/etc/kalinka/kalinka_conf.cfg`. Link again from the Qobuz app after upgrading.
- Qobuz now starts without waiting for the network, so a player that boots before its network still shows Qobuz and how to link it.
- Until an account is linked, Qobuz is marked unavailable and lists nothing: no shelves, search results, favourites or playlists. It never empties favourites, playlists or search results from other sources.
- Qobuz's startup checks no longer rely on one fixed test track, which can disappear from the catalogue or a region; they use a track from current new releases.

### Added
- **Play from the Qobuz app (Qobuz Connect).** The linked player is a speaker in the Qobuz app's device picker. Playing on it takes over the output Kalinka plays through; Kalinka's mini player and now-playing screen show the track, its queue screen says the Qobuz Connect queue is playing and Kalinka's is not, and pause, seek, next and previous work from either app. The volume follows both ways: the Qobuz app sets it on whatever controls Kalinka's volume, and a change made in Kalinka or on the device shows in the Qobuz app. The Qobuz app shows the format that plays, and changing its streaming quality switches the playing track to it. Playing from Kalinka's queue takes the output back, and the Qobuz app shows playback stopped. Switching Kalinka's output moves the Qobuz playback along, and a stream that fails is fetched again once, from where it was.
- A *Qobuz account* status line in Qobuz's settings: not linked, pairing, linked (with the account), reconnecting, or expired.
- **Unpair Qobuz account on next restart**, to forget the linked account and pair another. A linked player refuses every other pairing until it is unpaired, across restarts and expiry.
- If Qobuz issues no user auth token, the app's one-hour token is kept and renews itself before it expires.
- A *Qobuz Connect device name* setting for the name the player has in the Qobuz app, and an expert setting for the pairing port.
- **One command installs or upgrades the plugin.** `scripts/install-latest.sh` fetches the latest release, checks it against the release's checksums and installs it with apt, on a Raspberry Pi or any other Kalinka machine; the README shows how to run it.

Needs Kalinka server 5.2 or newer, which brings plugin SDK 3.4; the package will not install on an older one.

This is a prototype: the Qobuz Connect protocol is undocumented, and the hourly renewal fallback has not yet run against a real account for days.

## 3.0.0

### Changed
- **Requires Kalinka plugin SDK 2.0 or newer, and a server built against it.** The SDK changed how a track says where its audio comes from, and this release follows that change. It will not load on an older server, and an older Qobuz plugin will not load on a new one — upgrade the server and the plugin together.

Playback itself is unchanged: Qobuz still signs a URL per play and your renderer still fetches the audio straight from Qobuz, not through the Kalinka server.

## 2.3.0

### Changed
- The web bundle download during Qobuz startup no longer blocks the server's event loop, so everything else stays responsive while Qobuz starts.
- That download now runs under a two-minute deadline: a network black hole (such as a DNS server that never answers) fails Qobuz startup with a clear error instead of stalling it indefinitely.

Nothing about playback, authentication or catalogue browsing changed in this release.

## 2.2.0

### Added
- Qobuz can now be set up from the app's first-run wizard. The plugin marks its **User auth token** as required and its **Audio quality** as worth asking, so the wizard knows what to ask for and what it can leave alone.
- Qobuz now describes itself — "Streaming from a Qobuz subscription, up to studio-quality hi-res. Needs your Qobuz account token." — so the app can explain the source while you are deciding whether to enable it, before it is configured.

### Changed
- Relicensed from GPL-3.0-or-later to the Apache License 2.0.

Needs a server and app new enough to read the setup information; older ones ignore it and behave as before. Nothing about playback, authentication or catalogue browsing changed in this release.

## 2.1.0

### Added
- Sections on the Qobuz root page now carry a short subname, so it is clearer what each row of the catalogue is.

### Changed
- Playlists browsed by category are shown as text tiles instead of image cards.
- Qobuz gets more time to start up on slow networks.
- HTTP requests fail fast (3 second timeout, one retry) instead of hanging, and timeouts are no longer retried.
- The web bundle load is logged with start and finish timing, and retry logs are no longer blank.

## 2.0.0

### Changed
- The Qobuz API client is now asynchronous, authentication uses a user auth token, and the packaging was reworked.

## 1.0.0

First release.
