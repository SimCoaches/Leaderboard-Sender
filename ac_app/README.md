# LeaderboardSector — CSP companion app for Assetto Corsa

Companion app for the Lap Time Sender's **Custom Sector Challenge**. When a
driver misses a sector target, the Sender tells this app to teleport the car
back to the start/finish spawn — a clean in-game teleport via Custom Shaders
Patch, with no need to send keystrokes to the game window.

Without this app installed, the Sender falls back to simulating AC's default
"Restart session" hotkey (Ctrl+O), which requires the AC window to take
keyboard focus and AC's hotkey binding to be unchanged.

## Requirements

- Assetto Corsa with **Custom Shaders Patch (CSP)** installed
  (any reasonably recent build; installing via Content Manager is easiest)
- The Lap Time Sender running on the **same PC** as Assetto Corsa
  (the app talks to it on `http://127.0.0.1:5001`)

## Install

1. Copy the `LeaderboardSector` folder into your AC Lua apps folder:

   ```
   <steam>\steamapps\common\assettocorsa\apps\lua\LeaderboardSector\
   ```

   so that you end up with `...\apps\lua\LeaderboardSector\LeaderboardSector.lua`.

2. Start Assetto Corsa (with CSP active) and enter a session. The app loads
   automatically in the background — you do **not** need to open its window.

3. Optional: open the "Leaderboard Sector" app from the in-game apps taskbar
   to verify it shows "Connected to Lap Time Sender".

## How it works

- The app polls `GET /api/ac/command` on the Sender's Flask server four times
  a second. Each poll doubles as a heartbeat, so the Sender knows whether the
  app is alive and picks teleport vs. the Ctrl+O fallback automatically.
- On `teleport|<id>|<spawn_set>` it calls `physics.teleportCarTo(0, spawn_set)`
  and acks back via `GET /api/ac/ack?id=<id>&status=ok`.
- Commands are one-shot and expire after 10 seconds if unclaimed.

## Spawn point

By default the car is teleported to the `HOTLAP_START` spawn set — just before
the start/finish line, so the next driver's timing arms when they cross it
(this matches Hotlap mode's starting spot). To change it, edit
`teleport_spawn` in the Sender's `sector_config.json`:

| Value          | Where the car ends up                          |
|----------------|------------------------------------------------|
| `HOTLAP_START` | Just before start/finish (default)             |
| `START`        | Grid / starting line                           |
| `PIT`          | Pit box                                        |
| `TIME_ATTACK`  | Time-attack spawn (not all tracks have one)    |
