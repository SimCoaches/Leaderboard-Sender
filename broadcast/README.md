# Broadcast screen — outside-camera view on a 4th monitor

Shows a live **exterior camera** (chase cam or AC's trackside TV cameras) on a
4th screen while the driver uses the triple-screen view. One PC, one Steam
account, one Assetto Corsa instance.

How it works: Custom Shaders Patch renders an extra camera inside the same AC
process, and the official [CSP OBS plugin](https://github.com/ac-custom-shaders-patch/acc-obs-plugin)
hands that image to OBS Studio, which projects it fullscreen on the 4th
monitor. The driver's screens are unaffected.

## Requirements

- Assetto Corsa with **CSP v0.1.80+** (already required for the sector
  challenge companion app)
- **OBS Studio** (64-bit, free — the setup script can install it via winget)
- Enough GPU headroom to render the scene twice (the broadcast view is capped
  at 1920×1080 and half refresh rate by default to keep the driver's FPS safe)

## Easiest: via the Lap Time Sender installer

Run `LapTimeSenderSetup.exe` — the tasks page handles the whole rig:

- **"Install Assetto Corsa companion app"** (on by default): copies the
  sector-challenge teleport app into AC and enables CSP's OBS integration
  flag automatically.
- **"Set up 4th-screen broadcast camera"**: installs OBS (via winget, if
  missing) and the CSP plugin — one UAC prompt — imports the scene
  collection, and suppresses OBS's first-run wizard.
- **"Start the broadcast screen automatically with Windows"** (sub-option):
  adds OBS minimized-to-tray to the user's Startup folder, so the 4th screen
  comes up on its own after boot. The launcher also clears OBS's
  unclean-shutdown marker first, so a power-cycled rig never gets stuck on
  OBS's "start in Safe Mode?" dialog (OBS 30+ shows one, and OBS 32 removed
  the command-line flag that used to suppress it).

Logs land in `broadcast\setup_broadcast.<stage>.log` and
`ac_app\install_ac_app.log` in the install folder. Afterwards run
**Start Menu → Lap Time Sender → Rig Health Check** for a PASS/FAIL report
on the whole chain. The only things the installer cannot do are installing
CSP itself (done once via Content Manager) and the first-use projector
check below.

## Automated setup (standalone script)

On the customer rig, right-click PowerShell → Run as Administrator, then:

```bash
powershell -ExecutionPolicy Bypass -File setup_broadcast.ps1
```

The script:

1. Finds OBS (or installs it via winget with your consent)
2. Downloads the official CSP OBS plugin v1.0.0.1 (22 KB) from the
   `ac-custom-shaders-patch` GitHub and copies the DLL into
   `obs-studio\obs-plugins\64bit`
3. Installs the preconfigured **AC Broadcast** scene collection (chase camera
   at 1920×1080, fullscreen projector saved for the 4th monitor) and makes it
   the active collection
4. Prints the two remaining one-time steps (below)

If the 4th screen is not Windows display #4, pass the monitor index:
`-ProjectorMonitor 2` (0 = first monitor).

## One-time steps after the script

1. **Enable CSP's OBS integration**: Content Manager → Settings → Custom
   Shaders Patch → **Small Tweaks** → enable **OBS integration**.
2. **Verify the projector**: start AC into a session, then start OBS. The
   "AC Broadcast" scene shows the chase camera. If it isn't already filling
   the 4th screen: right-click the scene → **Fullscreen Projector (Scene)** →
   pick the 4th monitor. OBS remembers it from then on ("Save projectors on
   exit" is enabled by the script).

## Choosing the camera

Double-click the **AC External Camera** source in OBS and pick from the
Texture dropdown:

| Texture                  | View                                              |
|--------------------------|---------------------------------------------------|
| `Camera: Chase`          | Third-person chase camera behind the car (default)|
| `Camera: Track camera`   | AC's trackside TV cameras, auto-switching         |
| `Camera: Track camera 2…`| Additional TV camera sets, where the track has them|
| `Camera: Above`          | Overhead view                                     |
| `Basic: Clean view`      | Driver's view without HUD (for streaming/recording)|

The Nürburgring layouts ship with proper TV camera sets, so
`Camera: Track camera` gives a real broadcast feel there.

## Performance notes

- The source's "Skip frames" is set to 1 (half refresh rate) by default; raise
  it if the driver's FPS dips, or lower the source width/height.
- Keep OBS on the **same GPU** as AC (it exchanges textures directly).
- The plugin only renders a camera while it's actually visible in OBS, so
  closing OBS removes all overhead.

## Manual install (fallback)

1. Download `acc-obs-plugin.zip` from the plugin's GitHub releases page and
   extract the DLL into `C:\Program Files\obs-studio\obs-plugins\64bit\`.
2. Enable OBS integration in CSP Small Tweaks (see above).
3. In OBS: add source → **Assetto Corsa** → pick `Camera: Chase`, set size
   1920×1080.
4. Right-click the scene → Fullscreen Projector → 4th monitor, and enable
   File → Settings → General → "Save projectors on exit".

## Operator notes

- Start order doesn't matter; the OBS source shows black until AC is in a
  session.
- OBS can auto-start with Windows: add a shortcut to `obs64.exe` with
  `--minimize-to-tray` to the Startup folder so the rig boots straight into
  broadcast mode.
