# TrackID v2: always-listening track ID + setlist logger

For a Raspberry Pi Zero 2W with the PiSugar Whisplay HAT. Point it at the speaker and it logs every track into a
timestamped setlist, ready to paste into YouTube chapters or Mixcloud.

**What's new in v2**
- **Always listening.** The mic records into a 20 s buffer, so a tap identifies the *last 10 seconds* right away.
  There's no waiting while it records.
- **AUTO mode.** It identifies on its own every few minutes while music is playing, and stays quiet while the same track is
  still on. Misses aren't logged.
- **Live BPM.** It runs the same beat engine as Beat Commander's W10 conductor on the mic, shows the BPM with a beat pulse, and saves the BPM
  with every track.
- **Better start times.** AudD reports where in the song the clip landed, so TrackID works out when each track
  actually started, not just when you pressed.
- **Exports:** plain tracklist, **YouTube chapters** (00:00 first, chapters ≥ 10 s apart), **Mixcloud** start
  times, CSV.
- **Live level meter + onset scope** on screen, a scrolling title for long names, and a soft LED pulse on the beat.

## Button
| Gesture | Result |
|---|---|
| Tap | Identify the last 10 s now |
| Double tap | AUTO on / off |
| Hold 1.5 s | Start a new set |

LED: amber = matching, green = logged, magenta = no match, blue = saved offline (retried automatically).

## What you need
- Pi Zero 2W with the Whisplay HAT seated
- **Raspberry Pi OS Lite (64-bit)**, flashed with Raspberry Pi Imager. Set the hostname (`trackid`), user, Wi-Fi
  (2.4 GHz) and SSH in Imager's customisation screen.
- An AudD API token: sign up at dashboard.audd.io

## Install
On the Pi (`ssh <user>@trackid.local`):
```
sudo apt-get install -y git
git clone https://github.com/anubisdarkwatch/TrackID.git && cd TrackID/trackid
sudo bash install.sh
nano config.ini          # paste your AudD token under [audd]
sudo reboot              # first time only (sound + screen driver)
```
On Windows you can double-click [`../windows/Install-TrackID.bat`](../windows/Install-TrackID.bat) instead; it does all of the above.
The installer pulls dependencies, installs PiSugar's Whisplay driver if needed, and sets TrackID up to start on boot.
Update later with `git pull` and `sudo systemctl restart trackid`.

## Getting your tracklists
Open `http://trackid.local:8080` (or the IP at the bottom of the screen). It shows live BPM, mode and track count. Each set
has tabs for **Tracklist / YouTube chapters / Mixcloud / CSV**, each with a Copy button, plus downloads for every
format. The files also live in `~/trackid/data/sets/`.

For other gadgets, `http://trackid.local:8080/api/state` returns the live BPM, mode and last track as JSON.

## Config (`config.ini`)
| Section | Key | Default | Notes |
|---|---|---|---|
| audd | token | none | required for real use |
| capture | seconds | 10 | 5–15 s per ID |
| capture | gain | 1.0 | digital boost for quiet rooms |
| auto | enabled | false | start in AUTO |
| auto | interval | 150 | seconds between auto IDs (one AudD request each) |
| auto | min_level | 0.01 | what counts as "music playing" |
| display | brightness / led / led_beat / fps | 70 / on / on / 12 | |
| web | enabled / port | on / 8080 | LAN only, read-only |
| conductor | url | none | Beat Hub address, e.g. `http://localhost:8090` (see below) |

After editing: `sudo systemctl restart trackid`

## BPM from Beat Commander (optional)
With a [Beat Gateway](https://github.com/anubisdarkwatch/Beat-Commander#beat-gateway-xiao-esp32-s3--beat-hub)
and Beat Hub running (on this Pi or another one), TrackID can take its BPM and beat from the conductor instead of
guessing from the mic. Set `[conductor] url = http://localhost:8090` (or the other Pi's address) and restart.
While the conductor is live, the screen shows **LINK** next to the BPM, and every logged track gets the conductor's
BPM. When it goes quiet, the mic takes over again by itself.

## Troubleshooting
- Logs: `journalctl -u trackid -f`
- Screen blank: run PiSugar's test (`cd ~/Whisplay/example && sudo bash run_test.sh`).
- Bottom line says `mic: …`: `arecord -l` should list the Whisplay card. Set `[capture] device` if needed.
- BPM shows `---`: it needs a few seconds of music with a steady kick. Quiet rooms may need `gain = 2.0`.
- Preview the screens without hardware: `python3 trackid.py --preview out/`

## Notes
- Fingerprinting matches released recordings. Unreleased edits, mashups and live blends often won't match.
- AUTO uses one AudD request per interval while music plays. Check your AudD plan's monthly quota before a long set.
