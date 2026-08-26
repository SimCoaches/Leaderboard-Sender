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
- Complete all sectors → lap submitted normally with `distance_pct: 100`

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
"Custom Sectors" section of the main window.

### Broadcast Screen (`broadcast/`)

Optional 4th-monitor exterior camera view (chase cam or AC's trackside TV
cameras) for venues, using CSP's official OBS plugin: CSP renders an extra
camera inside the same AC process, the plugin (`accsp_capture` OBS source)
hands the texture to OBS, and OBS fullscreen-projects it on the extra monitor.
Single PC / single AC instance; no Sender code involved. `setup_broadcast.ps1`
automates the rig setup (install plugin DLL, import the `AC_Broadcast` scene
collection, save projector monitor); see `broadcast/README.md`.

### Data Flow

1. Game monitor reads telemetry (shared memory/SDK/UDP)
2. Lap completion triggers `lap_completed` signal with time and driver
3. Main window sends lap to leaderboard server via HTTP POST
4. Failed submissions queued and retried automatically

### Key Dependencies
- PyQt6 (GUI + multimedia for overlay videos)
- Flask + flask-cors (web interface)
- pyirsdk (iRacing SDK)
- zeroconf (mDNS service discovery)
- qrcode (QR code for mobile access)
