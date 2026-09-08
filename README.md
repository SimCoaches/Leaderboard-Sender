# Lap Time Sender

A simple application to send lap times to the SimRacing Leaderboard server.

## Usage - Executable Version

1. Make sure the SimRacing Leaderboard server is running
2. Run `LapTimeSender.exe` from the `dist` folder
3. Enter the following information:
   - Server URL (default: http://localhost:5000)
   - Driver Name
   - Lap Time in mm:ss.xxx format (e.g., 1:23.456)
4. Click "Send Lap Time" to submit the time to the server

## Usage - Python Version (for development)

1. Install Python 3.8 or higher
2. Install dependencies:
```bash
pip install -r requirements.txt
```
3. Run the sender:
```bash
python lap_time_sender.py
```

## Notes

- The server must be running to receive lap times
- Times are automatically converted to seconds before sending
- The status label will show success/error messages
- Invalid inputs will be caught and displayed as errors

## Nurburgring Sector Challenge

1. Enable the custom sector challenge and confirm all 13 sector boundaries and target times.
2. In Assetto Corsa, park the car on the racing line just before start/finish.
3. In Sender, click **Save Current Car as Start**. The saved point is kept per track layout and is used for automatic resets.
4. Use **Reset Car Now** once as a pre-event check.

Sector progress advances only when telemetry crosses the next configured boundary in order. Starting or restarting near sector 13 cannot count the earlier sectors. A reset is disarmed before the car moves, so it cannot be recorded as a completed run.

## iPad Registration Branding

In Sender settings, choose **iPad logo > Browse**, select the customer's PNG, JPG, GIF, or WebP image, and click **Apply**. Sender keeps a managed copy and updates open registration and queue pages automatically. Clear the field and apply to restore the bundled Sim Coaches logo.

## iPad Manual Top 10 Scoring

Open the staff portal at `http://<sender-ip>:5001/admin` and choose **Score Top
10**. Staff can change the ranking and visible columns, then add, update, or
remove a driver's Cars Passed, Finishing Position, and Lap Time. These entries
go directly to Receiver's Manual Top 10; the game does not send telemetry or
lap results for this workflow.

For the NASCAR format, every driver starts 40th. Cars Passed and Finishing
Position are linked (`Finishing Position = 40 - Cars Passed`), so the iPad
calculates either value from the other. For example, 12 cars passed is a 28th-
place finish. Lap Time remains an independent value.

## Building the Executable

To rebuild the executable:

```bash
python -m PyInstaller --name=LapTimeSender --onefile --noconsole --hidden-import PyQt6.QtCore --hidden-import PyQt6.QtGui --hidden-import PyQt6.QtWidgets --hidden-import requests lap_time_sender.py
```

The executable will be created in the `dist` directory "# Leaderboard-Sender" 
