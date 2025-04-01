# Lap Time Sender Build Documentation

This document outlines the process for building the Lap Time Sender application from source code into a Windows installer.

## Prerequisites

- Python 3.6 or higher
- PyInstaller (`pip install pyinstaller`)
- Inno Setup 6 or higher (download from [jrsoftware.org](https://jrsoftware.org/isdl.php))

## Build Process

The build process consists of two main steps:

1. Compile the Python application into an executable using PyInstaller
2. Create a Windows installer using Inno Setup

### Step 1: Build the Executable

Run the following command in PowerShell:

```powershell
pyinstaller "Lap Time Sender.spec"
```

This will:
- Create a `build` folder containing temporary build files
- Create a `dist` folder containing the final executable and all dependencies

### Step 2: Create the Installer

Run the following command in PowerShell:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" "setup.iss"
```

This will:
- Package the executable and all files in the `dist` folder
- Create an installer in the `installer` folder named `LapTimeSenderSetup.exe`

## Troubleshooting

- If you get "command not found" for PyInstaller, make sure it's installed and your Python Scripts folder is in your PATH
- If the Inno Setup command fails, make sure to use the `&` operator in PowerShell as shown above
- Make sure the path to ISCC.exe matches your Inno Setup installation location

## Clean Build

To perform a clean build:

1. Delete the existing `build` and `dist` folders:
   ```powershell
   Remove-Item -Path build -Recurse -Force
   Remove-Item -Path dist -Recurse -Force
   ```

2. Follow the build steps above

## Distribution

The final installer will be located at `installer/LapTimeSenderSetup.exe` ready for distribution to users. 