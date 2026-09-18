# Lap Time Sender 1.5.0

## Tablet registration

- Show only connected simulators and remove stale entries automatically.
- Update the selected simulator promptly after driver registration, with confirmation from Sender before reporting success.
- Keep connection checks in the background so they do not stall the Sender interface.

## Session End

- Separate Assetto Corsa and iRacing settings under **Session End**.
- Improve repeated Assetto Corsa shortcut delivery, including **Test saved shortcut**, and add a configurable delay before sending the command.
- Add integrated iRacing braking followed by a held reset key once telemetry confirms the car has stopped. No additional application or virtual controller is required.
- Start iRacing braking immediately while the minimum reset delay runs alongside deceleration.
- Use two different single keys for iRacing brake and reset; defaults are **F8** and **F9**. Map these to **Second Brake** and **Enter/Exit/Tow Car** inside iRacing. See the [setup guide](support/IRACING_RETURN_SETUP.md).
- Remove the experimental clutch controls and ignore their old settings while preserving valid brake/reset preferences.

## Validation and installer

- 177 automated tests passed, covering registration, discovery, shortcut delivery, iRacing telemetry and control cleanup, and the Session End interface.
- Windows installer built with PyInstaller and Inno Setup.
- This installer is unsigned because the release signing tools are unavailable on the build PC.
