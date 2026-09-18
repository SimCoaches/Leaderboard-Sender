# iRacing: brake to a stop, then return to pits

All controls and setup instructions are inside Sender under **Session End >
iRacing**. Sender holds **Second Brake** immediately when a session ends,
waits for telemetry to confirm a full stop, then holds **Enter/Exit/Tow Car**.
The brake stays applied through the reset, then Sender releases its keys.
No extra application or virtual controller driver is required.

## 1. Assign the two keys inside iRacing

1. In Sender, open **Session End > iRacing**. The defaults are **F8** for
   **Second Brake** and **F9** for **Enter/Exit/Tow Car**. You can change them,
   but use two different single keys without Ctrl, Alt or Shift. Shift+R
   interferes with keyboard braking and is not accepted for this sequence.
2. Load a private test session in iRacing. Open **Settings > Keybinds > In Car >
   Cockpit**. On older interfaces, use **Options > Controls**.
3. Click the assignment for **Second Brake**, then press your brake key.
   Follow any calibration prompts, holding the key for fully pressed and
   releasing it for released. Keep your physical pedal on regular **Brake**.
4. Assign **Enter/Exit/Tow Car** to your reset key. Remove any other assignments
   using either key. This is the return-to-pits action, not Active Reset's
   practice-snapshot action.
5. Save the iRacing controls. While parked, hold the brake key and verify the
   brake meter reaches full brake. Release it and check the meter returns to
   zero. Confirm your physical pedal still works.

If a car uses its own control profile, make these assignments in that profile
as well. Existing working brake/reset assignments can continue to be used.

## 2. Save the settings in Sender

1. Select **iRacing** as the game on this PC.
2. Open **Session End > iRacing** and enter the two keys you assigned.
3. Choose **Minimum reset delay** (default **1.5 seconds**, range 1 to 2) and
   reset **Hold** duration (default **3.5 seconds**, range 1 to 10).
4. Check **Automatically stop and reset at the end of iRacing sessions**, then
   click **Save iRacing settings**.

Braking starts immediately. The delay runs alongside braking: Reset waits
until both that time has elapsed and the car has stopped. It does not add
another delay after stopping. Assetto Corsa has separate controls and delay
under **Session End > Assetto Corsa**.

Valid saved brake/reset keys, hold duration, delay, and automatic-enabled state
carry forward when updating Sender. An old modifier reset such as Shift+R is
changed to the proposed F9 key and automatic operation is disabled until you
make the corresponding assignment in iRacing, enable the option, and save.

## 3. Test on the rig

Use a private iRacing test session. Register a test driver, enter the car, drive
at a modest speed, and end the driver's session from the tablet or Sender.
**Test stop and reset** runs the same saved settings between registered driver
sessions, even when automatic operation is off.

Expect: immediate braking, a full stop, the single reset key held while braking
continues, then all Sender inputs released. Confirm the brake meter returns to
zero and your physical pedal works normally afterward. The status reports
confirmed towing, a pit-stall transition, or exiting the car. iRacing can impose
its own towing delay. A shortcut sent without telemetry confirmation is reported
as unconfirmed.

Repeat with keyboard driving, physical pedals, automatic time/lap expiry, and
the tablet End button. **Cancel / release controls** stops the sequence and can
retry a blocked key release. Starting a new driver, changing games, saving
settings, or closing Sender also cancels it.

## Troubleshooting

- **No brake input detected:** hold the saved brake key yourself in iRacing.
  Check Second Brake's assignment, the current car's profile, and brake meter.
- **Key triggers another action:** clear its other binding or choose a different
  key in both Sender and iRacing.
- **Brake drops when Reset begins:** use one reset key with no Shift/Ctrl/Alt.
- **No live telemetry:** enter the car; this does not run from replay or menus.
- **Input blocked:** Sender and iRacing must run at the same administrator
  permission level. Release held keys and retry with iRacing visible.
- **Reset unconfirmed:** check Enter/Exit/Tow Car's binding and hold duration.
  Sender does not blindly repeat Reset.

The sequence cancels on stale telemetry, lost game focus, a changed session,
an undetected brake input, or failure to stop within 30 seconds. Reset is sent
only after fresh speed readings confirm a stop. Automated tests simulate input
and telemetry; a rig test verifies the actual in-game behavior.

Official references:

- [Control binding instructions](https://support.iracing.com/support/solutions/articles/31000133517-sim-hot-key-combinations)
- [Secondary brake inputs](https://support.iracing.com/support/solutions/articles/31000170166-hardware-and-software-for-iracers-with-physical-challenges-and-other-disabilities)
- [Default keyboard shortcuts](https://www.iracing.com/keyboard-shortcuts/)
- [Illustrated settings reference](https://support.iracing.com/support/solutions/articles/31000133335)
