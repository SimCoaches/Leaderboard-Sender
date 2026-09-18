# Simulator test plan — sector challenge + broadcast screen

Run through in order on the rig. Each step lists what to do and what you
should see. If something fails, note which step number — each maps to one
subsystem.

## 0. Install (5 min)

1. Run `installer\LapTimeSenderSetup.exe`.
2. On the tasks page tick everything: companion app (already ticked),
   broadcast camera, auto-start with Windows.
3. Approve the one UAC prompt (OBS plugin install).
4. When done: **Start Menu → Lap Time Sender → Rig Health Check**.
   - Expect all PASS. CSP row needs Content Manager's CSP installed (v0.1.80+).
   - "Sender running" row shows `--` until the Sender is started — that's fine.

## 1. Companion app connects (2 min)

1. Start the Lap Time Sender.
2. Start AC (with CSP) into a **Hotlap** session on a Nürburgring layout.
3. In AC's right-side app taskbar, open **Leaderboard Sector**.
   - Expect: "Connected to Lap Time Sender (port 5001)" (or a fallback
     port — also fine, it says which).

## 2. Broadcast screen basics (5 min)

1. Start OBS (or reboot and let the startup shortcut do it).
2. Expect: 4th monitor fills with the TV-style track cameras following
   the car; cuts happen automatically as you drive.
   - If it landed on the wrong monitor: right-click the "AC Broadcast"
     scene → Fullscreen Projector (Scene) → correct monitor. Sticks after
     that.
   - If the source shows black: check CSP Small Tweaks → OBS integration
     is on (Rig Health Check verifies this), and that AC is in a session.

## 3. Sector challenge + teleport (10 min)

1. In the Sender: enable Sector Challenge with 2–3 sectors and generous
   targets (e.g. sector times you can beat easily). Sector edits save
   automatically as you type (and via Save Sectors) — restart the Sender
   once and confirm the sectors and the enable checkbox come back.
2. Register a driver, drive, beat all sectors.
   - Expect: green sector flashes; lap submitted; session ends normally.
3. Register again, deliberately park until a sector time expires.
   - Expect, in order:
     - Red ✘ sector flash on the driver's screens
     - The **Out of Time** screen appears centered on every monitor checked
       under Advanced → Cover screens; no selected side screen stays on the
       game or falls back to a plain dark panel
     - Car teleports to just before start/finish (**no** Ctrl+O restart —
       the Sender console should log "Teleport command N sent to CSP app"
       and "CSP teleport confirmed")
     - The custom waiting image returns on every checked monitor, fills each
       screen edge-to-edge, and remains centered without stretching; driver
       is cleared
   - Next driver registers → crossing the line arms sector 1 fresh.
4. Pull the CSP app (close AC, or rename the LeaderboardSector folder)
   and fail a sector again → expect the Ctrl+O fallback to restart the
   session instead. Restore afterwards.

## 4. Broadcast modes between drivers (10 min)

Sender → Advanced → **4th Screen**:

1. **Waiting screen between drivers**: fail/finish a run.
   - Expect: 4th monitor cuts to the "NEXT DRIVER UP..." scene the moment
     the sim shows its waiting screen; cuts back to TV cameras when the
     next driver's session starts.
2. **Replay of last run between drivers**: drive ~30s+, then fail a sector.
   - Expect: 4th monitor plays the last ~60s of the run with a red
     "● REPLAY" tag, looping, until the next driver starts.
   - First time only: OBS's replay buffer must be running — the Sender
     starts it automatically at the first session start after OBS launches.
     If the very first fail shows the Waiting scene instead of a replay,
     that's the documented fallback; the second run should replay.
3. **Off**: fail a run → 4th monitor just keeps the live camera (parked car).

The 4th OBS screen is separate from **Cover screens**. Leave the broadcast
monitor unchecked there; check all of the simulator's driver-facing monitors.
For the three 3440×1440 driver screens shown in the field video, use a
3440×1440 waiting image. The image is centered and fitted independently on
each checked monitor.

## 5. Leaderboard flow + run log (5 min)

1. With the Receiver running, confirm a completed sector-challenge run
   arrives and distance-ranked partial runs slot in below full runs.
2. Open `sector_runs.csv` in the Sender folder: every run from today
   should be there - driver, result (COMPLETE / FAILED S#), distance %,
   total time, each sector's time vs. target, and the questionnaire
   answers. `leads.csv` holds the registrations.

## Tablet registration and session-end shortcut regression

1. Run the patched Sender on the tablet's host PC and each simulator. Open
   `/admin`, register distinct names on each connected simulator, and confirm
   the selected PC changes its driver before the tablet reports success. The
   card should show the confirmed name immediately. Repeat while a receiver
   address is unreachable; its connection check must not freeze registration.
2. Close Sender on one peer. Within the next discovery checks its card and
   registration option should disappear. Reopen it and confirm it returns.
   Previously saved addresses may remain in Settings, but must not appear as
   disconnected cards or targets.
3. In **Session End > Assetto Corsa**, keep the rig's remapped **Ctrl+R** shortcut, choose the
   **1.5 seconds** delay (or 1/2 seconds), and save. With no driver registered,
   drive away from the pits and press **Test saved shortcut**. Expect the delay,
   then a pit reset. Repeat at least 10 times, driving away between attempts.
   Each test must work; alternating success is a failure. Check the status
   message for focus or key-delivery errors if an attempt fails.
4. Repeat with manual End, time expiry, and lap expiry. Expect the completion
   screen immediately and one shortcut after the saved delay. Duplicate End
   requests must not send another shortcut. Registering a new driver during
   the delay must cancel the previous driver's pending shortcut.

Automated tests use real Qt and local HTTP servers but mock game keyboard
delivery. They cannot confirm that the rig's Assetto Corsa binding actually
returns the car to the pits; steps 3–4 are required before event use.

## iRacing keyboard stop and reset

Follow [the iRacing setup and rig test](support/IRACING_RETURN_SETUP.md).
The settings and setup instructions are in **Session End > iRacing**; no
virtual-controller application is required. Bind different single keys to
Second Brake (F8) and Enter/Exit/Tow Car (F9). Verify the
physical pedals still work. End a driving session: braking
should start immediately, the car must stop before Reset, and all keys
must release afterward. Repeat with keyboard driving and the rig's pedals.
Test both the manual test button and actual
session expiry. Check that switching games or registering the next driver
cancels any pending action. The two games have independent controls and delays.

Automated tests simulate keyboard delivery and telemetry. They validate
cancellation, key cleanup, settings migration, and game-specific routing;
the physical rig check verifies the brake/reset bindings and stopping behavior
for the selected car. Modifier reset shortcuts such as Shift+R must be rejected.

## Notes / known one-time adjustments

- **Monitor index**: Windows numbering varies; the projector may need the
  one-time right-click fix in step 2.
- **Waiting scene branding**: customize freely in OBS (logo/colors);
  only the scene name "Waiting" matters.
- **Non-16:9 4th monitor**: works with letterboxing; for edge-to-edge see
  broadcast/README.md.
- Logs if anything misbehaves: Sender console window,
  `ac_app\install_ac_app.log`, `broadcast\setup_broadcast.*.log`, and the
  in-game Leaderboard Sector app's status line.
