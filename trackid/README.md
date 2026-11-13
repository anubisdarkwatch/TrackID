# TrackID v2.2: always-listening track ID + setlist logger

For a Raspberry Pi Zero 2W with the PiSugar Whisplay HAT. Point it at the speaker and it logs every track into a
timestamped setlist, ready to paste into YouTube chapters or Mixcloud.

**What's new in v2.2**
- **AUTO listens for track changes.** Instead of a fixed timer, it notices when the music turns into a different
  track and identifies it ~15–20 s after it comes in. It still checks every 7 min as a safety net, and an hourly cap
  protects your AudD quota. Fewer requests, and the ID lands right after the transition so start times are tighter.
  (The old timer is still there: `[auto] trigger = interval`.)
- **Your own fingerprint library.** Drop your productions, edits and promos into `trackid/library/`. They're
  fingerprinted on the Pi and matched **offline, before AudD** (no quota used), even pitched ±8 % on the decks,
  with or without key lock.
- **Misses are logged, and the setlist is editable.** A track nobody can identify goes in as `ID - ID` with its
  10 s clip saved. On the web page, hit **✎ Edit** to listen to it, name it, fix start times, delete or add rows,
  and re-run the ID. Exports update instantly.

**What's in v2**
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

LED: amber = matching, green = logged, magenta = no match (logged as ID - ID), blue = saved offline (retried automatically).

In AUTO (change mode) the status line reads **AUTO · WATCHING** with a small bar: how different the last few
seconds sound from the track so far. When it passes the tick mark for a few seconds it switches to
**NEW TRACK · ID IN Ns**.

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

## Your own tracks (local library)
AudD only knows released music. For everything else, build a library on the Pi:

1. Copy audio files into `~/TrackID/trackid/library/` (mp3, wav, flac, aiff, m4a, ogg; sub-folders are fine), e.g.
   `scp *.mp3 <user>@trackid.local:~/TrackID/trackid/library/`, or drag them in with WinSCP.
2. That's it. TrackID checks the folder at startup and every 10 min and indexes new files in the background
   (a few seconds per track on a Zero 2W). To index right away:
   `python3 localid.py add library`
3. Artist/title come from the file's tags, or from the file name if it's `Artist - Title.mp3`.

Every ID tries the library first. A library hit takes about a second, needs no internet and uses no AudD request.
It copes with tracks played up to ±8 % fast or slow, with or without key lock (`[local] speed_range`, `keylock`).

Handy commands (run in `~/TrackID/trackid`):
```
python3 localid.py list                    # what's in the library
python3 localid.py remove 12               # drop a track (id from list)
python3 localid.py match some-recording.wav --start 60    # test: does this clip match?
python3 localid.py add library --prune     # re-scan and forget files you deleted
```
Big library? Index it on your PC instead: it needs Python 3 with numpy and ffmpeg. Run
`python3 localid.py --db library.db add D:\Music\MyTracks`, then copy `library.db`, `library.h.npy` and
`library.v.npy` into `~/TrackID/trackid/data/`.

## Getting your tracklists
Open `http://trackid.local:8080` (or the IP at the bottom of the screen). It shows live BPM, mode and track count. Each set
has tabs for **Tracklist / YouTube chapters / Mixcloud / CSV**, each with a Copy button, plus downloads for every
format. The files also live in `~/trackid/data/sets/`.

**✎ Edit** opens the set editor:
- **ID?** rows are tracks nothing could identify. Press play to hear the saved clip, type the artist and title, Save.
  Or press **Re-ID** to try the library and AudD again (handy after adding the track to your library).
- Change **Start** (h:mm:ss into the set) to fix a chapter time. Faded **repeat** rows are left out of exports;
  **Count it** puts one back in.
- **Add a track** for anything TrackID missed completely.

Unknown tracks appear in the exports as `ID - ID` so the timeline stays complete (`[export] include_unknown`).

Gadgets can also press the button: `POST /api/identify` (identify now) and `POST /api/auto` (toggle, or
`{"on": true}`), with the header `X-TrackID: 1`. TrackID Ink's touch screen uses these.

For other gadgets, `http://trackid.local:8080/api/state` returns the live BPM, mode and last track as JSON. In change
mode it also includes `change.novelty` (how different the music sounds right now) for tuning `sensitivity`.

## Config (`config.ini`)
| Section | Key | Default | Notes |
|---|---|---|---|
| audd | token | none | required for real use |
| capture | seconds | 10 | 5–15 s per ID |
| capture | gain | 1.0 | digital boost for quiet rooms |
| auto | enabled | false | start in AUTO |
| auto | trigger | change | `change` = ID each new track; `interval` = fixed timer |
| auto | sensitivity | 0.4 | change mode: lower catches smoother blends, higher ignores breakdowns |
| auto | min_gap / max_interval | 60 / 420 | change mode: seconds between IDs, at least / at most |
| auto | max_per_hour | 30 | hard cap on automatic IDs |
| auto | interval | 150 | interval mode: seconds between auto IDs |
| auto | min_level | 0.01 | what counts as "music playing" |
| local | enabled / folder | true / library | your own fingerprint library |
| local | speed_range / keylock | 8 / true | match tracks pitched up to ±8 %, with or without key lock |
| local | min_score | 12 | raise it if a library match is ever wrong |
| local | rescan | 10 | minutes between folder checks |
| export | include_unknown / unknown_label | true / ID - ID | unidentified tracks in exports |
| display | brightness / led / led_beat / fps | 70 / on / on / 12 | |
| web | enabled / port / edit | on / 8080 / on | LAN only. `edit = false` makes the page read-only |
| web | remote | on | let TrackID Ink's touch screen (or other gadgets) identify / toggle AUTO |
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
- AUTO fires on breakdowns: raise `[auto] sensitivity` (e.g. 0.5). Misses smooth blends: lower it (0.3).
  Repeats of the same track are never logged twice, so an extra ID only costs a request.
- Library never matches: `python3 localid.py list` to check it's indexed, then test with a recording:
  `python3 localid.py match clip.wav`. Very quiet rooms may need `[capture] gain`.
- Preview the screens without hardware: `python3 trackid.py --preview out/`

## Notes
- Fingerprinting matches released recordings. Unreleased edits, mashups and live blends often won't match.
- AUTO in change mode typically uses one AudD request per track (a few more around long blends), capped by
  `max_per_hour`. Library matches don't count. Check your AudD plan's monthly quota before a long set.
- Track-change detection works on how the music sounds (harmony, tone, tempo). Two very similar tracks blended
  slowly can slip through; the `max_interval` check catches those.
