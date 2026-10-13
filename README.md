# Qobuz Plugin for Kalinka Music Player

## Overview

This plugin was generated from the Kalinka Plugin cookiecutter template and provides a input_module implementation for the Kalinka Music Player system.

This is an experimental integration with [Qobuz](https://www.qobuz.com), allowing you to mix local tracks and Qobuz items inside the same play queue. Qobuz support is minimal and may change at any time.

**For using the plugin you must have a valid Qobuz subscription**

>**Disclaimer**: This plugin is an independent, community-developed integration and is not affiliated with, endorsed by, or supported by Qobuz or its parent companies. It is provided "as is", without any guarantees or warranties of any kind. Use of this plugin is entirely at your own risk. The author assumes no responsibility or liability for any consequences, including but not limited to potential violations of Qobuz's Terms of Service or any other issues arising from its use.

## Installation

The plugin installs on a Kalinka server set up from its `.deb` packages: a Raspberry Pi, any other Debian or Ubuntu machine, or a virtual machine. The package is architecture-independent, so the same file installs on all of them. Version 5.1 of the plugin needs Kalinka server 5.5 or newer.

### With the install script

Run this on the Kalinka machine, over SSH for a Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/madenvel/kalinka-plugin-qobuz/main/scripts/install-latest.sh | sudo bash
```

The script finds the latest release, checks the `.deb` against the release's `SHA256SUMS`, and installs it with apt. Kalinka restarts by itself to load the plugin.

Run the same command to upgrade. It does nothing when the latest version is already installed. Nothing upgrades the plugin automatically yet.

If apt says the release needs a newer `kalinka-plugin-sdk`, upgrade Kalinka first, then run the script again.

### By hand

Download `kalinka-plugin-qobuz_<version>_all.deb` and `SHA256SUMS` from the [releases page](https://github.com/madenvel/kalinka-plugin-qobuz/releases). Check the download, then install it with apt, which also checks that the installed Kalinka can load it:

```bash
sha256sum -c SHA256SUMS --ignore-missing
sudo apt install ./kalinka-plugin-qobuz_<version>_all.deb
```

Kalinka restarts by itself to load the plugin. To try a local build, build the `.deb` as described under [Building](#building) and install it the same way.

Then link your Qobuz account, as below.

## Linking your Qobuz account

The plugin links to your account through the official Qobuz app. There is no token to copy.

1. Enable Qobuz in Kalinka's settings. While it is not linked, the player advertises itself on your network as **Kalinka (hostname)**. You can change that name in Kalinka's Qobuz settings, under *Qobuz Connect device name*.
2. On a phone or computer on the same network, open the Qobuz app and open its device picker, where you would choose a speaker.
3. Choose Kalinka. The app hands the player a token that lasts an hour. The plugin exchanges it for a regular Qobuz user auth token, the same kind the old *User auth token* setting held, and checks it: it reads your account, favourites, playlists, new releases and a stream URL.
4. The *Qobuz account* line in Qobuz's settings then reads **Linked**, and you browse and play Qobuz in Kalinka as before.

The player also joins the Qobuz app's session and becomes the device the app plays on (see [Playing from the Qobuz app](#playing-from-the-qobuz-app)). Kalinka's status line tells you whether linking worked; reopen its settings to see it.

### What the status line means

| Status | Meaning |
|---|---|
| Starting | Loading the Qobuz web player's app details; waits for the network if needed. |
| Not linked | Waiting for you to choose this player in the Qobuz app. A failed attempt says why. |
| Pairing | Checking the account the app handed over. |
| Linked | Qobuz works. If Qobuz issued no user auth token, the app's one-hour token renews itself before it expires. |
| Reconnecting | After a restart, checking the stored link; retries while Qobuz is unreachable. |
| Link expired | Qobuz refused the stored link or its renewal. Unpair, then pair again. |
| Pairing unavailable | The pairing port is taken or there is no network address; retried every 30 seconds. |

The status is read when the settings page opens, so reopen it to see a change.

Until an account is linked, Qobuz reports itself as unavailable (an error badge in the module list) and lists nothing: no shelves, search results, favourites or playlists. Starting and reconnecting with a stored link show as a warning instead, since the link is on its way.

### Playing from the Qobuz app

A linked player is a Qobuz Connect speaker. Choose it in the Qobuz app's device picker and press play: the plugin takes over the output Kalinka is playing through and plays the Qobuz app's queue on it.

- **Kalinka shows what plays.** The mini player and the now-playing screen show the track, its artwork and position, with Qobuz's badge, and the now-playing screen says Qobuz Connect is controlling playback. The queue screen shows the Qobuz Connect queue is the one playing, managed in the Qobuz app, and lists Kalinka's own queue, kept as it was, as not playing.
- **Both apps control it.** Pause, seek, next and previous work from either app, and each follows the other. Volume from the Qobuz app goes to whatever controls the output's volume in Kalinka, including an amplifier Kalinka drives, and a change made in Kalinka or on the device itself shows in the Qobuz app.
- **Kalinka takes the output back when you play from it.** Playing anything from Kalinka's queue stops the Qobuz playback, and the Qobuz app shows it stopped. Stopping from Kalinka does the same without starting the queue. Press play in the Qobuz app to take the output again.
- **Tracks follow one another without a gap.** Shortly before a track ends, the plugin hands the output the one that follows in the Qobuz app's queue, so a live album or a DJ mix plays on as it does in the Qobuz app. Repeating one track or the whole queue works the same way.
- **Quality follows both settings.** The stream is the lower of the Qobuz app's streaming quality and this plugin's *Format*. Changing the quality in the Qobuz app applies to the playing track at once, from where it was, and the Qobuz app shows the format that plays and the one the output runs at.
- **Switching the output takes it along.** Choosing another output in Kalinka moves the Qobuz playback there, carrying on from where it had reached. The Qobuz app briefly shows it buffering.
- **On Android, the Qobuz app has the media controls.** While the Qobuz app plays on Kalinka, Kalinka's own media notification steps aside, so the Qobuz app's notification and the volume keys control the playback.

The player stays in the Qobuz app's device list while linked. After a restart it rejoins on its own; if the Qobuz session has ended meanwhile, choose the player in the Qobuz app again. The status line has a *Qobuz Connect* line: connecting, available in the device list, selected, or what it is playing.

While linked, the player keeps listening for handoffs so the Qobuz app can hand its session over again. It accepts them only from the linked account.

### Unpairing

A linked player refuses every other pairing attempt, from any phone, until you unpair it. This also holds after a restart or when the link has expired. To link another account:

1. Turn on **Unpair Qobuz account on next restart** and apply. Kalinka restarts.
2. The plugin forgets the account and its tokens, and the switch turns itself off.
3. The player advertises again. Choose it in the Qobuz app.

### Network

Pairing needs mDNS (UDP 5353) and one TCP port, 8183 by default (*Qobuz Connect pairing port*), reachable from the phone over IPv4 on the same LAN. Both stay open once the account is linked, so the Qobuz app can hand its session over. Playing from the Qobuz app also needs an outbound WebSocket connection to Qobuz.

The linked token and the Qobuz Connect session are kept in `/var/lib/kalinka/qobuz/connect.json`, readable only by the Kalinka service user.

### Upgrading from 4.x or older

The *User auth token* setting is gone. A token saved by an older version is ignored, and the server logs one warning about it on each start. To silence it, delete the `input_modules.qobuz.user_auth_token` entry from `/etc/kalinka/kalinka_conf.cfg`.

### Limitations

- This is a prototype. The Qobuz Connect protocol is not documented by Qobuz and was learnt from other receivers.
- User auth tokens have lasted months in practice. If Qobuz does not issue one, the plugin keeps the app's one-hour token and renews it every hour, which has not been proven over days.
- The Qobuz Connect session token is renewed with the app's one-hour token, which the plugin renews every hour as well, even when Qobuz issued a user auth token. If that renewal fails, Qobuz still works in Kalinka, but Qobuz Connect waits for the Qobuz app to hand the token over again: choose the player in the app.

## Building

### Prerequisites
- Python 3.11+
- `kalinka-plugin-sdk` package
- Build tools: `python3-build`, `setuptools`, `setuptools-scm`, `wheel`
- For Debian packaging: `dpkg-dev`
- Git repository with proper tags for version detection

### Version Management
This plugin uses **setuptools_scm** for automatic version detection:
- **Release builds**: Tag your release with `kalinka-plugin-qobuz-v1.2.3` format
- **Development builds**: setuptools_scm automatically generates dev versions like `1.2.4.dev0+gc1e6070.d20250928`
- **Clean releases**: Commit all changes and tag for clean release versions

### Build Python Wheel
```bash
./scripts/build_wheel.sh
```
The script automatically:
- Detects version from git tags using setuptools_scm
- Generates `_version.py` with the detected version
- Builds the wheel with proper version metadata

### Build Debian Package
```bash
./scripts/build_deb.sh
```
The script automatically:
- Builds the wheel first to detect the version
- Generates Debian control files with the correct version
- Creates a `.deb` package ready for installation

The Debian package puts the wheel in `/opt/kalinka/wheels/` and tells the Kalinka server, which restarts and installs it into its venv.

## Live smoke test

Run this on a real player with a Qobuz subscription before releasing. Use throwaway favourites and playlists for the steps that change your account.

1. Install the build (`./scripts/build_deb.sh`, then `sudo apt install ./kalinka-plugin-qobuz_*_all.deb`). Check the player is advertised and answers:
   ```sh
   avahi-browse -rt _qobuz-connect._tcp
   curl http://<player-ip>:8183/streamcore/get-display-info
   ```
2. Choose the player in the Qobuz app. Follow the server log (`journalctl -u kalinka -f`). Note the handoff's key names and claim names, which account endpoint answered, and each check. The "Qobuz account linked" line names the credential kept: `user_auth_token`, or `bearer` if Qobuz issued none.
3. In Kalinka, confirm the status reads **Linked**. Then:
   - browse Qobuz, your favourites and your playlists;
   - add and remove a favourite, and create and delete a playlist;
   - play a Qobuz track, then a local track in the same queue;
   - confirm `reportStreamingStart` and `reportStreamingEnd` succeed in the log.
4. Send a second handoff, from another phone or with `curl -X POST` and any body. It must be refused with HTTP 400, and the status must not change.
5. Confirm playback still works after `sudo systemctl restart kalinka`, and again the next day. If the link kept a `bearer` credential, also wait past the first renewal; the log shows the expiry before and after.
6. Turn on Unpair and apply. Confirm the player is advertised again and that pairing works a second time.

Then check playback from the Qobuz app:

7. Choose the player in the Qobuz app and play a track. The log shows the cloud connection, the session joined as the active renderer and the renderer id Qobuz gave the player; the Qobuz app shows the player as the active device, and its position advances.
8. In Kalinka, the mini player shows the track, and the queue screen shows the *Qobuz Connect queue* card above *Saved Kalinka queue · Not playing*. Pause, seek and skip from Kalinka and from the Qobuz app; each app follows the other.
   Change the volume in Kalinka, and on the renderer's host or amplifier; the Qobuz app's volume follows.
   The Qobuz app shows the format Kalinka shows for the track. Change the streaming quality in the Qobuz app; the track carries on from where it was in the new format.
9. Play something from Kalinka's queue. The Qobuz app shows playback stopped. Press play in the Qobuz app; it takes the output back.
10. Let a track end; the next one plays and the Qobuz app follows. At the end of the queue the output goes back to Kalinka.
11. Switch the Qobuz app to another device; Kalinka stops. Switch back.
12. Restart the server. The player rejoins as an available device without being chosen again. Leave it overnight and choose it again the next day.
13. Run the speaker test from Kalinka's settings while the Qobuz app plays; the Qobuz app shows stopped.
14. Switch Kalinka's output while the Qobuz app plays, and again while paused. Playback carries on from where it was on the new output, paused if it was.
15. On an Android phone running both apps, Kalinka's media notification disappears while the Qobuz app plays, and the volume keys change the output's volume. Stop from Kalinka; its notification comes back.

## Development

### Testing

The tests need the plugin SDK, which is not on PyPI. Install it from the KalinkaPlayer repo in the same command as the plugin, so pip never looks the name up on PyPI:

```bash
python3 -m venv .venv
.venv/bin/pip install \
  "kalinka-plugin-sdk @ git+https://github.com/Kalinka-Player/KalinkaPlayer#subdirectory=packages/kalinka-plugin-sdk" \
  -e ".[dev]"
.venv/bin/pytest
```

Add `@<branch>` after `KalinkaPlayer` to test against an SDK that is not on `main` yet.

GitHub runs the same tests on Python 3.11 and 3.13 for every pull request and push to `main` (`.github/workflows/tests.yml`), and a release is built only once they pass. **Tests pass** is the check to require in a ruleset. The SDK comes from KalinkaPlayer's `main`, unless the repository variable `KALINKA_SDK_REF` names another branch or tag.

## License

This plugin is released under the [Apache License 2.0](LICENSE).

## Support

For questions about this plugin or plugin development in general:
- Check the Kalinka Plugin SDK documentation
- Review other plugins in the ecosystem
- Consult the main Kalinka project documentation
