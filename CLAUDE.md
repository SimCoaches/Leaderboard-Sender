# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Lap Time Sender is a Windows desktop application for sim racing leaderboard integration. It captures lap times from racing simulators (Assetto Corsa, iRacing, BeamNG.drive) and sends them to a leaderboard server. The application provides both a PyQt6 desktop GUI and a Flask web interface for driver registration.

## Build Commands

### Development
```bash
pip install -r requirements.txt
python lap_time_sender.py
```

### Build Executable
```powershell
pyinstaller "Lap Time Sender.spec"
```

### Create Installer (requires Inno Setup 6)
```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" "setup.iss"
```

### Clean Build
```powershell
Remove-Item -Path build -Recurse -Force
Remove-Item -Path dist -Recurse -Force
```

## Architecture

### Main Application (`lap_time_sender.py`)

Single-file application (~5000+ lines) containing:

- **LapTimeSenderWindow**: Main Qt window managing game selection, driver info, and leaderboard connections
- **GameMonitor (base class)**: Abstract QThread for game telemetry. Subclasses:
  - `AssettoCorsaMonitor`: Reads shared memory via ctypes/mmap
  - `iRacingMonitor`: Uses pyirsdk for iRacing telemetry
  - `BeamNGMonitor`: UDP listener on port 5002 for BeamNG mod data
- **Flask Server**: Runs on port 5001, serves driver registration web UI and REST API
- **SimulatorDiscovery/SimulatorBroadcastListener**: UDP broadcast (port 5003) for discovering other simulators on the network
- **ReceiverDiscoveryThread**: Discovers leaderboard display receivers via Zeroconf/mDNS
- **UIBridge**: Qt signal bridge for Flask-to-UI thread communication

### Network Ports
- **5000**: Leaderboard server (target for lap submissions)
- **5001**: Local Flask web interface (driver registration, API)
- **5002**: BeamNG UDP listener (receives lap data from game mod)
- **5003**: Simulator discovery broadcast

### BeamNG Mod (`beamng_mod/`)

Lua extension for BeamNG.drive that sends lap times via UDP:
- `lua/ge/extensions/leaderboardSender.lua`: Hooks into game events (`onRaceWaypointReached`, `onMissionAttemptAggregated`)
- Sends UDP packets in format: `LAP|time|vehicle|track|valid|timestamp`

### Custom Sector Challenge (Assetto Corsa)

Event mode where the track is divided into any number of custom sectors (defined
as track-position percentages in `sector_config.json`), each with a target time.
Runs are judged live in `AssettoCorsaMonitor` using `normalizedCarPosition` +
`iCurrentTime` from shared memory (20ms polling while enabled):

- Beat a sector target → keep driving (green flash via `SectorFlashOverlay`)
- Time expires mid-sector → instant fail: partial run is submitted with its
  `distance_pct`, the car is put back at start/finish, and the waiting screen
  comes up
- Complete all sectors → `challenge_completed(total_s)` is emitted at the
  line crossing with the accumulated run time (AC's `iLastTime` is not
  trusted after a teleport); the run is submitted with `distance_pct: 100`,
  a gold "Challenge Complete!" screen with the total time is held ≥7 s, and
  the car is reset for the next driver exactly like a failure

The waiting / end-of-run overlay covers the primary monitor itself and a
`ScreenBlanker` per extra monitor chosen under Advanced → "Cover Screens"
(setting `waiting_screen_monitors`, default all). Each blanker shows the
same waiting image/video as the main screen, scaled to its own screen
(video muted; sound comes from the primary), and goes dark during the
session-over screens. On a rig with a 4th broadcast monitor, leave that
one unticked or the overlay hides OBS.

Putting the car back uses the `LeaderboardSector` CSP companion app (`ac_app/`)
when it is installed in AC: the app polls `GET /api/ac/command` on the local
Flask server (plain text `teleport|<id>|<spawn_set>`, acked via
`GET /api/ac/ack`; every poll is a heartbeat) and calls
`physics.teleportCarTo(0, spawn_set)` — default spawn `HOTLAP_START`
(configurable as `teleport_spawn` in `sector_config.json`). After a confirmed
teleport the monitor's challenge state machine is re-armed via
`request_challenge_reset()`. If the app is not polling (or doesn't ack within
2.5s), the Sender falls back to a simulated Ctrl+O session restart (AC's
default "Restart session" hotkey must be unchanged).

The Receiver's `ranking_mode: "distance"` config ranks these runs by distance
traveled descending, lap/elapsed time as tiebreaker. Configured in the
"Custom Sectors" section of the main window; edits auto-save to
`sector_config.json` (silently, only when the table is fully valid).

Every finished run - completed or failed - is appended to `sector_runs.csv`
(next to the exe): driver, email/phone, result, distance %, total time (raw +
M:SS.mmm), each judged sector's time vs. target, and the driver's lead-gen
questionnaire answers. Questionnaire registrations also land in `leads.csv`.

### Host Stand (`/admin`) and multi-sim control

The iPad page at `/admin` ("Host Stand") controls every simulator on the LAN
from one page. Tapping a sim card never navigates the browser to that sim
(it used to, which silently re-targeted every later action); it opens the
Register form with that sim pre-selected. Register Driver and Manual Lap
both carry a "Simulator" picker, and a banner at the top says which sim the
page is served from. Actions on other sims go direct (cross-origin, CORS is
on) and fall back to `POST /api/sim_proxy` on the host sim (allow-listed
paths only) if Safari refuses or times out.

Occupied cards have **+5 MIN** (or **+1 LAP** in Laps mode) backed by
`POST /api/session/extend {minutes|laps}`, which adds to the running timer
without touching the driver; while the pre-session video is still playing
the extension is queued and applied when the timer starts. Re-submitting
the driver already in the seat to `/set_driver` or
`/api/session/start_remote` is a no-op (`already_active: true`) instead of
a session restart.

Discovery of other sims is `SimulatorDiscovery.discover()`: UDP presence
broadcasts (port 5003, every 3 s) merged with the saved list in
`simulators.json`, probing only those addresses. Nothing scans the subnet
inside a web request. A full /24 sweep runs in a background thread on
`POST /api/discover_simulators/scan` (the "Scan network" button in the
Host Stand settings modal), or automatically, at most every 2 minutes, when
this PC could not bind the UDP port (`udp_discovery_ok: false` in
`/api/discover_simulators`). Sweep hits are added to `simulators.json`.
Manually added sims live on the server, not in the browser.

The Simulator ID combo is persisted in `settings.json` (`simulator_id`).
Another PC announcing the same ID shows a red warning under the combo, in
`id_conflicts` of `/api/simulator_info`, and in the Host Stand banner.

Lap submission, session-start and session-end notifications to receivers
run on daemon threads (`_send_lap_to_receivers` reports back through the
`lap_send_finished` signal); nothing on the GUI thread waits on a receiver.
Waitress runs 32 workers; the queue SSE stream is capped at
`MAX_SSE_CLIENTS`. `main()` reconfigures stdout/stderr to UTF-8 because an
emoji `print` inside a Qt slot under a cp1252 console raised and PyQt then
aborted the process.

### Broadcast Screen (`broadcast/`)

Optional 4th-monitor exterior camera view (chase cam or AC's trackside TV
cameras) for venues, using CSP's official OBS plugin: CSP renders an extra
camera inside the same AC process, the plugin (`accsp_capture` OBS source)
hands the texture to OBS, and OBS fullscreen-projects it on the extra monitor.
Single PC / single AC instance. `setup_broadcast.ps1` automates the rig setup
(install plugin DLL, import the `AC_Broadcast` scene collection with
Waiting/Replay scenes, enable OBS's WebSocket server with a generated
password, install an OBS profile with the replay buffer armed); see
`broadcast/README.md`.

`BroadcastDirector` (a daemon thread in `lap_time_sender.py`) syncs the 4th
screen to the simulator state over OBS's WebSocket (v5, localhost; auth
details read from OBS's own obs-websocket config.json). Cues come from
`show_waiting_screen`/`hide_waiting_screen`/`end_session`: 'live' switches to
the TV-camera scene, 'waiting' switches to the Waiting scene or — in replay
mode — saves OBS's replay buffer and loops the clip on the Replay scene until
the next driver starts. Mode ('off'/'waiting'/'replay') is the "4th Screen"
combo in Advanced settings, persisted as `broadcast_mode` in app settings.
All OBS calls fail soft; the sim is never blocked.

### Data Flow

1. Game monitor reads telemetry (shared memory/SDK/UDP)
2. Lap completion triggers `lap_completed` signal with time and driver
3. Main window sends lap to leaderboard server via HTTP POST
4. Failed submissions queued and retried automatically

For one-off manually scored events, the staff iPad uses `/admin` → **Score Top
10**. Sender proxies `/api/manual_leaderboard/*` to the configured Receiver;
the Receiver owns the manual results and display settings. This path does not
invoke a game monitor or the ordinary lap-submission signal.

### Key Dependencies
- PyQt6 (GUI + multimedia for overlay videos)
- Flask + flask-cors (web interface)
- pyirsdk (iRacing SDK)
- zeroconf (mDNS service discovery)
- qrcode (QR code for mobile access)
