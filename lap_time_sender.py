import sys
import json
import requests
import time
import threading
import socket
import ctypes  # Needed for Assetto Corsa shared memory
import mmap    # Needed for Assetto Corsa shared memory
import irsdk   # Needed for iRacing SDK
from datetime import datetime
from abc import ABC, abstractmethod
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                            QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                            QMessageBox, QFormLayout, QComboBox, QGridLayout, QFrame)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QObject
from PyQt6.QtGui import QImage, QPixmap, QIcon
import qrcode
from io import BytesIO
import os

def format_time_ms(milliseconds):
    """Format milliseconds into a readable time string (mm:ss.xxx)"""
    if milliseconds is None:
        return "--:--.---"
    total_seconds = milliseconds / 1000.0
    minutes = int(total_seconds // 60)
    seconds = total_seconds % 60
    return f"{minutes}:{seconds:06.3f}"

def get_local_ip():
    try:
        # Create a socket to get the local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Doesn't actually connect, just helps determine the local IP
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return '127.0.0.1'  # Fallback to localhost if we can't determine IP

# Create Flask app with no output
app = Flask(__name__)
app.logger.disabled = True
import logging
log = logging.getLogger('werkzeug')
log.disabled = True
CORS(app)  # Enable CORS for all routes

# Create a simple HTML page for driver name input
HTML_CONTENT = r"""
<!DOCTYPE html>
<html>
<head>
    <title>Lap Time Sender - Driver Setup</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black">
    <style>
        /* Reset all margins and padding */
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        /* Force html and body to full height with no overflow */
        html, body {
            width: 100%;
            height: 100vh;
            overflow: hidden;
            position: fixed;
            top: 0;
            left: 0;
            background-color: #000000;
        }
        
        /* Set body as a flex container with background */
        body {
            font-family: Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: flex-start;
            padding-top: 5vh;
            background-repeat: no-repeat;
            background-position: center;
            background-size: cover;
            background-attachment: fixed;
            min-height: 100%;
        }
        
        /* Container for the form */
        .container {
            width: 90%;
            max-width: 500px;
            background-color: rgba(255, 255, 255, 0.9);
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
            z-index: 10;
            margin-bottom: 20px;
        }
        
        h1 {
            color: #333;
            margin-bottom: 20px;
            text-align: center;
        }
        
        .input-group {
            margin-bottom: 20px;
        }
        
        label {
            display: block;
            margin-bottom: 5px;
            color: #666;
            font-weight: bold;
        }
        
        input[type="text"], input[type="email"] {
            width: 100%;
            padding: 10px;
            font-size: 16px;
            border: 1px solid #ddd;
            border-radius: 4px;
            box-sizing: border-box;
        }
        
        button {
            background-color: #4CAF50;
            color: white;
            padding: 12px 20px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 16px;
            width: 100%;
            font-weight: bold;
        }
        
        button:hover {
            background-color: #45a049;
        }
        
        .status {
            margin-top: 20px;
            padding: 10px;
            border-radius: 4px;
            text-align: center;
        }
        
        .success {
            background-color: #dff0d8;
            color: #3c763d;
        }
        
        .error {
            background-color: #f2dede;
            color: #a94442;
        }
        
        .timer-alert {
            animation: flash 1s infinite;
        }
        
        @keyframes flash {
            0%, 100% { background-color: rgba(255, 255, 255, 0.9); }
            50% { background-color: rgba(255, 0, 0, 0.7); }
        }

        @keyframes highlight-field {
            0%, 100% { background-color: transparent; }
            50% { background-color: rgba(255, 230, 230, 0.8); }
        }
        
        /* iPad-specific styles - more aggressive with exact sizing */
        @media only screen and (min-device-width: 768px) and (max-device-width: 1024px),
               only screen and (min-device-width: 834px) and (max-device-width: 1194px),
               only screen and (min-device-width: 1024px) and (max-device-width: 1366px) {
            body {
                padding-top: 15vh; /* Move content down further to show logo */
            }
        }
    </style>
</head>
<body>
    <div class="container" id="container">
        <h1>Driver Sign Up</h1>
        <div class="input-group">
            <label for="driverName">Driver Name:</label>
            <input type="text" id="driverName" placeholder="Enter driver name" required>
        </div>
        <div class="input-group" id="emailGroup" style="display: none;">
            <label for="driverEmail">Email Address: <span style="color: red;">*</span></label>
            <input type="email" id="driverEmail" placeholder="Enter email address (required)" required>
            <small style="color: #666; font-style: italic;">This field is required</small>
        </div>
        <button onclick="updateDriver()">Set Driver</button>
        <div id="status" class="status" style="display: none;"></div>
        <div id="timerStatus" style="display: none; margin-top: 15px;"></div>
    </div>
    
    <script>
        // Log screen dimensions for debugging
        console.log(`Screen dimensions: ${window.innerWidth}x${window.innerHeight}`);
        console.log(`Screen ratio: ${window.innerWidth/window.innerHeight}`);
        console.log(`Device pixel ratio: ${window.devicePixelRatio}`);
        
        function showStatus(message, isError) {
            const status = document.getElementById('status');
            status.textContent = message;
            status.style.display = 'block';
            status.className = 'status ' + (isError ? 'error' : 'success');
            setTimeout(() => {
                status.style.display = 'none';
            }, 3000);
        }

        function updateDriver() {
            const driverName = document.getElementById('driverName').value;
            if (!driverName) {
                showStatus('Please enter a driver name', true);
                return;
            }
            
            let payload = { driver_name: driverName };
            
            // Add email if required
            const emailGroup = document.getElementById('emailGroup');
            if (emailGroup.style.display !== 'none') {
                const driverEmail = document.getElementById('driverEmail').value;
                if (!driverEmail) {
                    showStatus('⚠️ Email address is required!', true);
                    // Highlight the email field
                    document.getElementById('driverEmail').style.border = '2px solid red';
                    document.getElementById('driverEmail').focus();
                    return;
                }
                if (!validateEmail(driverEmail)) {
                    showStatus('⚠️ Please enter a valid email address (e.g., name@example.com)', true);
                    // Highlight the email field
                    document.getElementById('driverEmail').style.border = '2px solid red';
                    document.getElementById('driverEmail').focus();
                    return;
                }
                
                // Clear any previous highlighting
                document.getElementById('driverEmail').style.border = '1px solid #ddd';
                
                payload.driver_email = driverEmail;
            }

            fetch('/set_driver', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload)
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showStatus('Driver information updated successfully!', false);
                } else {
                    showStatus('Failed to update driver information', true);
                }
            })
            .catch(error => {
                showStatus('Error connecting to server', true);
                console.error('Error:', error);
            });
        }
        
        function validateEmail(email) {
            const re = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
            return re.test(email);
        }
        
        function checkTimerAndSettings() {
            fetch('/get_settings')
                .then(response => response.json())
                .then(data => {
                    // Update email field visibility
                    const emailGroup = document.getElementById('emailGroup');
                    const wasHidden = emailGroup.style.display === 'none';
                    emailGroup.style.display = data.require_email ? 'block' : 'none';
                    
                    // Highlight email field when it first appears
                    if (wasHidden && data.require_email) {
                        // Add attention animation
                        emailGroup.style.animation = 'none';
                        setTimeout(() => {
                            emailGroup.style.animation = 'highlight-field 1.5s';
                        }, 10);
                    }
                    
                    // Update background image - set directly to body in a more reliable way
                    if (data.background_image) {
                        const imageUrl = `/backgrounds/${data.background_image}`;
                        document.body.style.backgroundImage = `url('${imageUrl}')`;
                        
                        // Preload image to ensure it's fully loaded
                        const img = new Image();
                        img.onload = function() {
                            console.log(`Background image loaded: ${img.width}x${img.height}`);
                            
                            // Force background size based on image dimensions vs screen
                            const screenRatio = window.innerWidth / window.innerHeight;
                            const imageRatio = img.width / img.height;
                            
                            if (Math.abs(screenRatio - imageRatio) > 0.2) {
                                // If ratios are very different, use 100% 100%
                                document.body.style.backgroundSize = '100% 100%';
                                console.log('Using 100% 100% sizing due to ratio difference');
                            } else {
                                // Otherwise cover works better
                                document.body.style.backgroundSize = 'cover';
                                console.log('Using cover sizing due to similar ratios');
                            }
                        };
                        img.src = imageUrl;
                    } else {
                        document.body.style.backgroundImage = 'none';
                    }
                    
                    // Check timer
                    const timerStatus = document.getElementById('timerStatus');
                    const container = document.getElementById('container');
                    
                    if (data.timer_active && data.timer_seconds_left <= 0) {
                        // Timer expired
                        timerStatus.textContent = 'Driver time expired! Please change driver.';
                        timerStatus.style.display = 'block';
                        container.classList.add('timer-alert');
                    } else if (data.timer_active) {
                        // Timer active
                        const minutes = Math.floor(data.timer_seconds_left / 60);
                        const seconds = data.timer_seconds_left % 60;
                        timerStatus.textContent = `Driver time remaining: ${minutes}m ${seconds}s`;
                        timerStatus.style.display = 'block';
                        container.classList.remove('timer-alert');
                    } else {
                        // No timer
                        timerStatus.style.display = 'none';
                        container.classList.remove('timer-alert');
                    }
                })
                .catch(error => console.error('Error:', error));
        }

        // Load current driver name when page loads
        fetch('/get_driver')
            .then(response => response.json())
            .then(data => {
                if (data.driver_name) {
                    document.getElementById('driverName').value = data.driver_name;
                }
            })
            .catch(error => console.error('Error:', error));
         
        // Apply a secondary fix after page fully loads
        window.addEventListener('load', function() {
            // Force body to full viewport height again after load
            document.body.style.minHeight = '100vh';
            document.documentElement.style.minHeight = '100vh';
            
            // Force background to cover screen
            if (document.body.style.backgroundImage && document.body.style.backgroundImage !== 'none') {
                document.body.style.backgroundSize = '100% 100%';
            }
            
            console.log('Applied additional fixes on load');
        });
            
        // Check settings and timer immediately and then every second
        checkTimerAndSettings();
        setInterval(checkTimerAndSettings, 1000);
    </script>
</body>
</html>
"""

# Store the main window instance globally
main_window = None

@app.route('/')
def index():
    return HTML_CONTENT

@app.route('/get_driver', methods=['GET'])
def get_driver():
    if main_window:
        return jsonify({'driver_name': main_window.driver_name.text()})
    return jsonify({'driver_name': ''})

@app.route('/set_driver', methods=['POST'])
def set_driver():
    try:
        data = request.get_json()
        driver_name = data.get('driver_name', '')
        driver_email = data.get('driver_email', '')
        
        if main_window:
            main_window.update_driver_name(driver_name)
            # Store email if provided
            if driver_email:
                main_window.last_driver_email = driver_email
            return jsonify({'success': True})
    except Exception as e:
        print(f"Error setting driver: {str(e)}")
    return jsonify({'success': False})

@app.route('/get_settings', methods=['GET'])
def get_settings():
    if not main_window:
        return jsonify({
            'require_email': False,
            'background_image': None,
            'timer_active': False,
            'timer_seconds_left': 0
        })
    
    # Get email requirement
    require_email = main_window.email_field_enabled.isChecked()
    
    # Get background image
    background_image = os.path.basename(main_window.background_image_path.text()) if main_window.background_image_path.text() else None
    
    # Check timer
    timer_active = main_window.driver_timer_enabled.isChecked()
    timer_seconds_left = 0
    
    if timer_active and hasattr(main_window, 'timer_end_timestamp'):
        current_time = int(time.time())
        end_time = main_window.timer_end_timestamp
        timer_seconds_left = max(0, end_time - current_time)
    
    return jsonify({
        'require_email': require_email,
        'background_image': background_image,
        'timer_active': timer_active,
        'timer_seconds_left': timer_seconds_left
    })

@app.route('/backgrounds/<filename>')
def background_file(filename):
    return send_from_directory('resources', filename)

def run_flask():
    app.run(host='0.0.0.0', port=5001)

class GameMonitor(QThread):
    lap_completed = pyqtSignal(float)  # Signal emitted when a valid lap is completed
    connection_status = pyqtSignal(str, str)  # Signal for status updates (message, color)
    game_name = "Unknown Game"
    
    def __init__(self):
        super().__init__()
        self.running = True
        self.last_lap_time = 0
        self.is_connected = False
        
    def connect_to_game(self):
        """Attempt to connect to the game. Should return (success, data_handle)"""
        pass
        
    def read_lap_time(self, data_handle):
        """Read lap time from the game. Should return (is_valid_lap, lap_time)"""
        pass
        
    def run(self):
        try:
            while self.running:
                try:
                    if not self.is_connected:
                        self.connection_status.emit(f"Attempting to connect to {self.game_name}...", "orange")
                    
                    success, data_handle = self.connect_to_game()
                    if not success:
                        if self.game_name == "iRacing":
                            self.connection_status.emit("Waiting for iRacing... 🔄", "rgb(107, 114, 128)")
                        else:
                            self.connection_status.emit(f"{self.game_name} not running", "orange")
                        time.sleep(1)
                        continue
                    
                    if not self.is_connected:
                        self.is_connected = True
                        self.connection_status.emit(f"Connected to {self.game_name}! ✅", "green")
                    
                    is_valid_lap, lap_time = self.read_lap_time(data_handle)
                    
                    if is_valid_lap and lap_time > 0 and lap_time != self.last_lap_time:
                        self.last_lap_time = lap_time
                        self.lap_completed.emit(lap_time)
                    
                    self.msleep(100)  # Check every 100ms
                    
                except Exception as e:
                    if self.is_connected:
                        self.is_connected = False
                        self.connection_status.emit(f"Lost connection to {self.game_name} - will retry automatically... 🔄", "orange")
                    self.msleep(1000)
                    
        except Exception as e:
            print(f"{self.game_name} Monitor error: {str(e)}")
            self.connection_status.emit(f"Error monitoring {self.game_name}: {str(e)}", "red")
            
    def stop(self):
        print(f"\n=== Stopping {self.game_name} Monitor ===")
        self.running = False
        self.wait()

class AssettoCorsaData(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('identifier', ctypes.c_wchar * 12),     # AC identifier
        ('size', ctypes.c_int),                  # Size of the struct
        ('speedKmh', ctypes.c_float),            # Speed in KM/h
        ('gas', ctypes.c_float),                 # Gas pedal position [0,1]
        ('brake', ctypes.c_float),               # Brake pedal position [0,1]
        ('fuel', ctypes.c_float),                # Amount of fuel remaining
        ('gear', ctypes.c_int),                  # Current gear
        ('rpms', ctypes.c_int),                  # Current RPM
        ('steerAngle', ctypes.c_float),          # Steering angle
        ('speedMs', ctypes.c_float),             # Speed in m/s
        ('velocity', ctypes.c_float * 3),        # Velocity vector
        ('accG', ctypes.c_float * 3),            # Acceleration vector
        ('wheelSlip', ctypes.c_float * 4),       # Wheel slip
        ('wheelLoad', ctypes.c_float * 4),       # Wheel load
        ('wheelsPressure', ctypes.c_float * 4),  # Wheels pressure
        ('wheelAngularSpeed', ctypes.c_float * 4),# Wheel angular speed
        ('tyreWear', ctypes.c_float * 4),        # Tyre wear
        ('tyreDirtyLevel', ctypes.c_float * 4),  # Tyre dirt
        ('tyreCoreTemperature', ctypes.c_float * 4),# Tyre core temperature
        ('camberRAD', ctypes.c_float * 4),       # Camber in radians
        ('suspensionTravel', ctypes.c_float * 4),# Suspension travel
        ('drs', ctypes.c_float),                 # DRS
        ('tc', ctypes.c_float),                  # Traction control
        ('heading', ctypes.c_float),             # Car heading
        ('pitch', ctypes.c_float),               # Car pitch
        ('roll', ctypes.c_float),                # Car roll
        ('cgHeight', ctypes.c_float),            # Center of gravity height
        ('carDamage', ctypes.c_float * 5),       # Car damage
        ('numberOfTyresOut', ctypes.c_int),      # Number of tyres out
        ('pitLimiterOn', ctypes.c_int),          # Pit limiter
        ('abs', ctypes.c_float),                 # ABS
        ('kersCharge', ctypes.c_float),          # KERS charge
        ('kersInput', ctypes.c_float),           # KERS input
        ('autoShifterOn', ctypes.c_int),         # Auto shifter
        ('rideHeight', ctypes.c_float * 2),      # Ride height
        ('turboBoost', ctypes.c_float),          # Turbo boost
        ('ballast', ctypes.c_float),             # Ballast
        ('airDensity', ctypes.c_float),          # Air density
        ('airTemp', ctypes.c_float),             # Air temperature
        ('roadTemp', ctypes.c_float),            # Road temperature
        ('localAngularVel', ctypes.c_float * 3), # Local angular velocity
        ('finalFF', ctypes.c_float),             # Final force feedback
        ('performanceMeter', ctypes.c_float),    # Performance meter
        ('engineBrake', ctypes.c_int),           # Engine brake
        ('ersRecoveryLevel', ctypes.c_int),      # ERS recovery level
        ('ersPowerLevel', ctypes.c_int),         # ERS power level
        ('ersHeatCharging', ctypes.c_int),       # ERS heat charging
        ('ersIsCharging', ctypes.c_int),         # ERS is charging
        ('kersCurrentKJ', ctypes.c_float),       # KERS current kJ
        ('drsAvailable', ctypes.c_int),          # DRS available
        ('drsEnabled', ctypes.c_int),            # DRS enabled
        ('brakeTemp', ctypes.c_float * 4),       # Brake temperature
        ('clutch', ctypes.c_float),              # Clutch
        ('tyreTempI', ctypes.c_float * 4),       # Tyre inner temperature
        ('tyreTempM', ctypes.c_float * 4),       # Tyre middle temperature
        ('tyreTempO', ctypes.c_float * 4),       # Tyre outer temperature
        ('isAIControlled', ctypes.c_int),        # Is AI controlled
        ('tyreContactPoint', ctypes.c_float * 4 * 3), # Tyre contact point
        ('tyreContactNormal', ctypes.c_float * 4 * 3),# Tyre contact normal
        ('tyreContactHeading', ctypes.c_float * 4 * 3),# Tyre contact heading
        ('brakeBias', ctypes.c_float),           # Brake bias
        ('localVelocity', ctypes.c_float * 3),   # Local velocity
    ]

class AssettoCorsaGraphicsData(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packetId', ctypes.c_int),              # Packet ID
        ('status', ctypes.c_int),                # AC status
        ('session', ctypes.c_int),               # Current session
        ('currentTime', ctypes.c_wchar * 15),    # Current time
        ('lastTime', ctypes.c_wchar * 15),       # Last lap time
        ('bestTime', ctypes.c_wchar * 15),       # Best lap time
        ('split', ctypes.c_wchar * 15),          # Split time
        ('completedLaps', ctypes.c_int),         # Number of completed laps
        ('position', ctypes.c_int),              # Current position
        ('iCurrentTime', ctypes.c_int),          # Current time in milliseconds
        ('iLastTime', ctypes.c_int),             # Last time in milliseconds
        ('iBestTime', ctypes.c_int),             # Best time in milliseconds
        ('sessionTimeLeft', ctypes.c_float),     # Session time left
        ('distanceTraveled', ctypes.c_float),    # Distance traveled
        ('isInPit', ctypes.c_int),               # Is car in pit
        ('currentSectorIndex', ctypes.c_int),    # Current sector
        ('lastSectorTime', ctypes.c_int),        # Last sector time
        ('numberOfLaps', ctypes.c_int),          # Number of laps
        ('tyreCompound', ctypes.c_wchar * 33),   # Tyre compound
        ('replayTimeMultiplier', ctypes.c_float),# Replay time multiplier
        ('normalizedCarPosition', ctypes.c_float),# Normalized car position
        ('carCoordinates', ctypes.c_float * 3),  # Car coordinates
        ('penaltyTime', ctypes.c_float),         # Penalty time
        ('flag', ctypes.c_int),                  # Flag
        ('idealLineOn', ctypes.c_int),           # Ideal line on
        ('isInPitLane', ctypes.c_int),           # Is in pit lane
        ('surfaceGrip', ctypes.c_float),         # Surface grip
        ('mandatoryPitDone', ctypes.c_int),      # Mandatory pit done
        ('windSpeed', ctypes.c_float),           # Wind speed
        ('windDirection', ctypes.c_float),       # Wind direction
    ]

class AssettoCorsaMonitor(GameMonitor):
    game_name = "Assetto Corsa"
    
    def __init__(self):
        super().__init__()
        self.last_completed_laps = 0
        self.physics_handle = None
        self.graphics_handle = None
    
    def connect_to_game(self):
        """Attempt to connect to Assetto Corsa via shared memory"""
        try:
            print("\n=== Attempting AC Connection ===")
            
            # Connect to physics shared memory
            physics_mem = "Local\\acpmf_physics"
            graphics_mem = "Local\\acpmf_graphics"
            
            print(f"Connecting to physics: {physics_mem}")
            print(f"Connecting to graphics: {graphics_mem}")
            
            try:
                self.physics_handle = mmap.mmap(-1, ctypes.sizeof(AssettoCorsaData), physics_mem)
                self.graphics_handle = mmap.mmap(-1, ctypes.sizeof(AssettoCorsaGraphicsData), graphics_mem)
            except Exception as e:
                print(f"Failed to open shared memory: {e}")
                return False, None
            
            # Read initial data to verify connection
            graphics = AssettoCorsaGraphicsData()
            raw_data = self.graphics_handle.read(ctypes.sizeof(AssettoCorsaGraphicsData))
            self.graphics_handle.seek(0)
            ctypes.memmove(ctypes.byref(graphics), raw_data, ctypes.sizeof(AssettoCorsaGraphicsData))
            
            # Verify we're getting valid data by checking status
            if graphics.status == 0:  # AC is not running
                print("AC is not running (status = 0)")
                self.physics_handle.close()
                self.graphics_handle.close()
                return False, None
            
            print("Successfully connected to Assetto Corsa")
            return True, None
            
        except Exception as e:
            print(f"\n=== Connection Error ===")
            print(f"Failed to connect to AC: {str(e)}")
            print("=====================")
            if self.physics_handle:
                self.physics_handle.close()
            if self.graphics_handle:
                self.graphics_handle.close()
            return False, None
    
    def read_lap_time(self, _):
        """Read lap time from Assetto Corsa shared memory"""
        try:
            # Read graphics data for lap times
            graphics = AssettoCorsaGraphicsData()
            raw_data = self.graphics_handle.read(ctypes.sizeof(AssettoCorsaGraphicsData))
            self.graphics_handle.seek(0)
            ctypes.memmove(ctypes.byref(graphics), raw_data, ctypes.sizeof(AssettoCorsaGraphicsData))
            
            # Read physics data for additional validation
            physics = AssettoCorsaData()
            raw_data = self.physics_handle.read(ctypes.sizeof(AssettoCorsaData))
            self.physics_handle.seek(0)
            ctypes.memmove(ctypes.byref(physics), raw_data, ctypes.sizeof(AssettoCorsaData))
            
            # For debugging
            print(f"\nDebug Info:")
            print(f"Speed: {physics.speedKmh:.1f} km/h")
            print(f"Completed Laps: {graphics.completedLaps}")
            print(f"Current Time: {graphics.currentTime}")
            print(f"Last Time: {graphics.lastTime}")
            print(f"Best Time: {graphics.bestTime}")
            print(f"Status: {graphics.status}")
            
            # Check if we've completed a new lap
            if graphics.completedLaps > self.last_completed_laps:
                self.last_completed_laps = graphics.completedLaps
                # Convert last time from string (mm:ss.xxx) to seconds
                try:
                    last_time = graphics.iLastTime / 1000.0  # Convert from ms to seconds
                    if last_time > 0 and last_time < 600:  # Max 10 minutes per lap
                        return True, last_time
                except:
                    pass
            
            return False, 0
            
        except Exception as e:
            print(f"Error reading lap time: {str(e)}")
            return False, 0
            
    def stop(self):
        """Clean up Assetto Corsa shared memory handles"""
        self.running = False
        try:
            if self.physics_handle and hasattr(self.physics_handle, 'close'):
                self.physics_handle.close()
            if self.graphics_handle and hasattr(self.graphics_handle, 'close'):
                self.graphics_handle.close()
        except ValueError:
            # Handle case where mmaps are already closed
            pass
        self.wait()

class iRacingMonitor(QThread):
    game_name = "iRacing"
    # Declare signals at class level
    lap_completed = pyqtSignal(float)
    connection_status = pyqtSignal(str, str)
    
    def __init__(self):
        super().__init__()
        self.ir = irsdk.IRSDK()
        self.state = State()  # Use the exact State class
        self.running = True
        self.last_sent_lap_time = None  # Track the last lap time we sent
        
    def format_time(self, seconds):
        if seconds is None:
            return "--:--.---"
        minutes = int(seconds // 60)
        seconds = seconds % 60
        return f"{minutes}:{seconds:06.3f}"

    def check_iracing(self):
        if self.state.ir_connected and not (self.ir.is_initialized and self.ir.is_connected):
            self.state.ir_connected = False
            self.ir.shutdown()
            self.connection_status.emit('iRacing disconnected', 'orange')
        elif not self.state.ir_connected and self.ir.startup() and self.ir.is_initialized and self.ir.is_connected:
            self.state.ir_connected = True
            self.connection_status.emit('iRacing connected', 'green')

    def display_telemetry(self):
        # Freeze the data buffer
        self.ir.freeze_var_buffer_latest()
        
        # Get telemetry data
        speed = self.ir['Speed']  # m/s
        if speed is not None:
            speed_mph = speed * 2.23694  # Convert to MPH
        else:
            speed_mph = 0
            
        gear = self.ir['Gear']
        lap_time = self.ir['LapCurrentLapTime']
        last_lap_time = self.ir['LapLastLapTime']
        lap_number = self.ir['Lap']
        
        # Display data
        print(f"{'='*40}")
        print(f"iRacing Telemetry - {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*40}")
        print(f"Speed: {speed_mph:.1f} MPH")
        print(f"Gear: {gear if gear is not None else 'N'}")
        print(f"Current Lap: {lap_number if lap_number is not None else 0}")
        print(f"Lap Time: {self.format_time(lap_time)}")
        print(f"{'='*40}")
        
        # Check for completed lap - match AC's format and prevent duplicates
        if (last_lap_time is not None and last_lap_time > 0 and last_lap_time < 600  # Same validation as AC
            and last_lap_time != self.last_sent_lap_time):  # Only send if it's a new lap time
            self.last_sent_lap_time = last_lap_time  # Update the last sent time
            self.lap_completed.emit(float(last_lap_time))  # Ensure it's a float like AC

    def run(self):
        try:
            while self.running:
                self.check_iracing()
                
                if self.state.ir_connected:
                    self.display_telemetry()
                else:
                    self.connection_status.emit("Waiting for iRacing connection...", "rgb(107, 114, 128)")
                
                # Update every 0.1 seconds
                self.msleep(100)
                
        except Exception as e:
            print(f"iRacing monitor error: {str(e)}")
            self.connection_status.emit(f"Error monitoring iRacing: {str(e)}", "red")
            
    def stop(self):
        self.running = False
        if self.state.ir_connected:
            self.ir.shutdown()
        self.wait()

class State:
    ir_connected = False

class LapTimeSenderWindow(QMainWindow):
    AVAILABLE_GAMES = {
        "Assetto Corsa": AssettoCorsaMonitor,
        "iRacing": iRacingMonitor,
    }
    
    def __init__(self):
        super().__init__()
        self.current_monitor = None
        self.local_ip = get_local_ip()
        self.failed_lap_times = []  # Store failed lap times for retry
        self.retry_timer = None
        self.current_server_url = f"http://{self.local_ip}:5000"  # Store current working URL
        self.timer_end_timestamp = 0  # Initialize driver swap timer end timestamp
        self.setup_ui()
        
        # Start with no monitor active
        self.current_monitor = None
        
        # Start the Flask server in a separate thread
        global main_window
        main_window = self
        self.web_thread = threading.Thread(target=run_flask, daemon=True)
        self.web_thread.start()
        
        # Start retry timer
        self.start_retry_timer()
        
        # Start monitoring the initially selected game
        self.start_monitor(self.game_selector.currentText())
        
    def setup_ui(self):
        # Main window settings
        self.setWindowTitle("Lap Time Sender")
        self.setMinimumSize(650, 600)  # Slightly increased width for better text display
        self.setWindowIcon(QIcon("resources/icon.png"))
        self.setStyleSheet("""
            QMainWindow {
                background-color: rgb(243, 244, 246);
            }
            QLabel {
                font-size: 13px;
                color: rgb(55, 65, 81);
            }
            QLineEdit {
                padding: 6px;
                border: 1px solid rgb(209, 213, 219);
                border-radius: 6px;
                background-color: white;
                min-height: 20px;
                font-size: 13px;
            }
            QLineEdit:read-only {
                background-color: rgb(249, 250, 251);
            }
            QComboBox {
                padding: 6px;
                border: 1px solid rgb(209, 213, 219);
                border-radius: 6px;
                background-color: white;
                min-height: 20px;
                font-size: 13px;
            }
            QPushButton {
                background-color: rgb(37, 99, 235);
                color: white;
                border: none;
                padding: 6px 12px;
                border-radius: 6px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: rgb(29, 78, 216);
            }
            QPushButton:checked {
                background-color: #45a049;
            }
            QFormLayout {
                fieldGrowthPolicy: ExpandingFieldsGrow;
                labelAlignment: AlignRight;
            }
        """)

        # Central widget setup
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)  # Reduce margins further
        main_layout.setSpacing(8)  # Reduce spacing
        
        # Create a "card" container with shadow effect
        card = QWidget()
        card.setStyleSheet("""
            QWidget {
                background-color: white;
                border-radius: 8px;
            }
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(15, 15, 15, 15)  # Smaller margins
        card_layout.setSpacing(8)  # Reduced spacing
        
        # Title - slightly smaller for compact UI
        title = QLabel("Lap Time Sender")
        title.setStyleSheet("""
            QLabel {
                font-size: 20px;
                font-weight: bold;
                color: #111827;
            }
        """)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(title)
        
        # Web interface info
        url_info_layout = QHBoxLayout()
        url_info_layout.setSpacing(8)  # Reduced spacing
        
        # Create a widget for the blue info box
        info_widget = QWidget()
        info_widget.setStyleSheet("""
            QWidget {
                background-color: #dbeafe;
                border-radius: 6px;
            }
        """)
        info_layout = QVBoxLayout(info_widget)
        info_layout.setContentsMargins(8, 8, 8, 8)  # Smaller padding
        
        # Web interface info text - more compact
        url_info_text = QLabel(f"Web interface available at: http://{get_local_ip()}:5001")
        url_info_text.setStyleSheet("""
            QLabel {
                color: #1e40af;
                font-size: 12px;
            }
        """)
        url_info_text.setWordWrap(True)
        
        # Access info text - more compact
        access_info = QLabel("Access from any device on the same network to set your driver name.")
        access_info.setStyleSheet("""
            QLabel {
                color: #1e40af;
                font-size: 12px;
            }
        """)
        access_info.setWordWrap(True)
        
        # Add labels to info layout
        info_layout.addWidget(url_info_text)
        info_layout.addWidget(access_info)
        
        # Add info widget to main layout
        url_info_layout.addWidget(info_widget, stretch=1)
        
        # Generate and add QR code - larger size
        qr_code = self.generate_qr_code(f"http://{get_local_ip()}:5001")
        qr_label = QLabel()
        qr_label.setPixmap(qr_code)
        qr_label.setFixedSize(100, 100)  # Increased size for better scanning
        qr_label.setStyleSheet("""
            QLabel {
                background-color: white;
                padding: 8px;
                border-radius: 4px;
                margin: 4px;
            }
        """)
        qr_label.setToolTip("Scan to open web interface")
        qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        url_info_layout.addWidget(qr_label)
        
        card_layout.addLayout(url_info_layout)
        
        # Form setup
        form_layout = QFormLayout()
        form_layout.setVerticalSpacing(8)  # Further reduce spacing
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        
        # Add Server URL
        server_url_widget = QWidget()
        server_url_layout = QHBoxLayout(server_url_widget)
        server_url_layout.setContentsMargins(0, 0, 0, 0)
        server_url_layout.setSpacing(5)
        
        self.server_url = QLineEdit(self.current_server_url)
        self.server_url.textChanged.connect(self.on_server_url_changed)
        server_url_layout.addWidget(self.server_url)
        
        test_connection_btn = QPushButton("Test")
        test_connection_btn.setFixedWidth(60)  # Narrower button
        test_connection_btn.setStyleSheet("padding: 4px 8px;")
        test_connection_btn.setObjectName("test-connection")  # Set object name for finding the button
        test_connection_btn.clicked.connect(self.test_and_save_connection)
        server_url_layout.addWidget(test_connection_btn)
        
        url_label = QLabel("Server URL:")
        url_label.setStyleSheet("font-weight: bold;")
        url_label.setFixedWidth(80)  # Fixed width like other labels
        form_layout.addRow(url_label, server_url_widget)
        
        # Add Simulator ID (more compact)
        simulator_row = QHBoxLayout()
        simulator_id_label = QLabel("Simulator ID:")
        simulator_id_label.setStyleSheet("font-weight: bold;")
        simulator_id_label.setFixedWidth(80)
        
        self.simulator_id = QComboBox()
        self.simulator_id.addItems([str(i) for i in range(1, 100)])
        self.simulator_id.setFixedWidth(60)  # Narrower dropdown
        
        # Game selection
        game_label = QLabel("Racing Sim:")
        game_label.setStyleSheet("font-weight: bold;")
        game_label.setFixedWidth(70)
        
        self.game_selector = QComboBox()
        for game_name in self.AVAILABLE_GAMES.keys():
            self.game_selector.addItem(game_name)
        self.game_selector.currentTextChanged.connect(self.on_game_changed)
        
        # Add these in a single row to save space
        simulator_row.addWidget(simulator_id_label)
        simulator_row.addWidget(self.simulator_id)
        simulator_row.addSpacing(10)
        simulator_row.addWidget(game_label)
        simulator_row.addWidget(self.game_selector, 1)  # Give the game selector stretch priority
        
        card_layout.addLayout(simulator_row)
        
        # Add the form layout to the card after the simulator row
        card_layout.addLayout(form_layout)
        
        # Add divider for clarity
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        divider.setStyleSheet("background-color: #e5e7eb; max-height: 1px;")
        card_layout.addWidget(divider)
        
        # Add driver tab box
        driver_box = QWidget()
        driver_box.setStyleSheet("""
            QWidget {
                background-color: #f8f9fa;
                border-radius: 8px;
                padding: 8px;
                margin-top: 5px;
                margin-bottom: 5px;
            }
        """)
        driver_box_layout = QVBoxLayout(driver_box)
        driver_box_layout.setContentsMargins(8, 8, 8, 8)  # Reduce margins evenly for better alignment
        driver_box_layout.setSpacing(2)  # Minimal spacing
        
        # Driver section header (more compact)
        header_layout = QHBoxLayout()
        driver_section_label = QLabel("Driver Settings")
        driver_section_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        header_layout.addWidget(driver_section_label)
        
        # Add settings update button to the header to save vertical space
        driver_settings_update = QPushButton("Update Settings")
        driver_settings_update.setFixedWidth(120)
        driver_settings_update.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6;
                color: white;
                font-weight: bold;
                padding: 6px 10px;
                font-size: 12px;
                border-radius: 6px;
                border: 1px solid #2563eb;
            }
            QPushButton:hover {
                background-color: #2563eb;
            }
            QPushButton:pressed {
                background-color: #1d4ed8;
            }
        """)
        driver_settings_update.clicked.connect(self.on_update_driver_settings)
        header_layout.addStretch(1)
        header_layout.addWidget(driver_settings_update)
        
        driver_box_layout.addLayout(header_layout)
        
        # Use a grid layout for more compact control placement
        settings_grid = QWidget()
        grid_layout = QGridLayout(settings_grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setHorizontalSpacing(15)  # Increased horizontal spacing between columns
        grid_layout.setVerticalSpacing(8)  # Keep vertical spacing the same
        
        # Row 0: Driver name
        name_label = QLabel("Driver Name:")
        name_label.setFixedWidth(100)  # Increased width to prevent truncation
        name_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid_layout.addWidget(name_label, 0, 0)
        
        self.driver_name = QLineEdit()
        self.driver_name.setReadOnly(True)
        self.driver_name.setPlaceholderText("Set via web interface")
        grid_layout.addWidget(self.driver_name, 0, 1, 1, 3)  # Span 3 columns
        
        # Row 1: Timer settings
        timer_label = QLabel("Driver Timer:")
        timer_label.setFixedWidth(100)  # Increased width to prevent truncation
        timer_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid_layout.addWidget(timer_label, 1, 0)
        
        self.driver_timer_enabled = QPushButton("⊠ Timer On")
        self.driver_timer_enabled.setCheckable(True)
        self.driver_timer_enabled.setChecked(False)
        self.driver_timer_enabled.setFixedWidth(100)
        self.driver_timer_enabled.setStyleSheet("""
            QPushButton {
                background-color: #e5e7eb;
                color: #4b5563;
                border: 1px solid #d1d5db;
                padding: 4px 8px;
                border-radius: 6px;
                font-weight: bold;
                font-size: 12px;
                text-align: center;
            }
            QPushButton:checked {
                background-color: #10b981;
                color: white;
                border: 1px solid #059669;
            }
        """)
        # Connect to text change when toggled
        self.driver_timer_enabled.toggled.connect(lambda checked: self.driver_timer_enabled.setText("⊠ Timer On" if checked else "☐ Timer Off"))
        self.driver_timer_enabled.setText("☐ Timer Off")  # Initial state
        grid_layout.addWidget(self.driver_timer_enabled, 1, 1)
        
        minutes_label = QLabel("Minutes:")
        minutes_label.setFixedWidth(60)  # Increased width to prevent truncation
        grid_layout.addWidget(minutes_label, 1, 2)
        
        self.driver_timer_minutes = QLineEdit("5")
        self.driver_timer_minutes.setFixedWidth(40)
        grid_layout.addWidget(self.driver_timer_minutes, 1, 3)
        
        # Row 2: Email toggle
        email_label = QLabel("Email Field:")
        email_label.setFixedWidth(100)  # Increased width to prevent truncation
        email_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid_layout.addWidget(email_label, 2, 0)
        
        self.email_field_enabled = QPushButton("⊠ Email Required")
        self.email_field_enabled.setCheckable(True)
        self.email_field_enabled.setChecked(False)
        self.email_field_enabled.setFixedWidth(120)  # Slightly wider to fit text
        self.email_field_enabled.setStyleSheet("""
            QPushButton {
                background-color: #e5e7eb;
                color: #4b5563;
                border: 1px solid #d1d5db;
                padding: 4px 8px;
                border-radius: 6px;
                font-weight: bold;
                font-size: 12px;
                text-align: center;
            }
            QPushButton:checked {
                background-color: #10b981;
                color: white;
                border: 1px solid #059669;
            }
        """)
        # Connect to text change when toggled
        self.email_field_enabled.toggled.connect(lambda checked: self.email_field_enabled.setText("⊠ Email Required" if checked else "☐ Email Optional"))
        self.email_field_enabled.setText("☐ Email Optional")  # Initial state
        grid_layout.addWidget(self.email_field_enabled, 2, 1, 1, 3)  # Span 3 columns
        
        # Row 3: Background image
        bg_label = QLabel("Background:")
        bg_label.setFixedWidth(100)  # Increased width to prevent truncation
        bg_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid_layout.addWidget(bg_label, 3, 0)
        
        self.background_image_path = QLineEdit()
        self.background_image_path.setPlaceholderText("Select image file")
        grid_layout.addWidget(self.background_image_path, 3, 1, 1, 2)  # Span 2 columns
        
        self.select_bg_button = QPushButton("Browse...")
        self.select_bg_button.setFixedWidth(70)
        self.select_bg_button.setStyleSheet("""
            QPushButton {
                background-color: #6366f1;
                color: white;
                border: 1px solid #4f46e5;
                padding: 4px 8px;
                border-radius: 6px;
                font-weight: bold;
                font-size: 12px;
                text-align: center;
            }
            QPushButton:hover {
                background-color: #4f46e5;
            }
            QPushButton:pressed {
                background-color: #4338ca;
            }
        """)
        self.select_bg_button.clicked.connect(self.select_background_image)
        grid_layout.addWidget(self.select_bg_button, 3, 3)
        
        # Current driver info (hidden)
        self.last_driver_email = ""
        
        # Add grid to driver box
        driver_box_layout.addWidget(settings_grid)
        card_layout.addWidget(driver_box)
        
        # Game Status Card
        status_card = QWidget()
        status_card.setStyleSheet("""
            QWidget {
                background-color: #f9fafb;
                border-radius: 6px;
                padding: 6px;
                margin-bottom: 6px;
            }
        """)
        status_layout = QHBoxLayout(status_card)  # Use horizontal layout
        status_layout.setContentsMargins(8, 6, 8, 6)
        status_layout.setSpacing(5)
        
        status_title = QLabel("Game Status:")
        status_title.setStyleSheet("font-weight: bold;")
        status_title.setFixedWidth(100)  # Increased width from 80 to 100
        status_layout.addWidget(status_title)
        
        self.game_status = QLabel("Waiting to connect to game...")
        self.game_status.setWordWrap(True)
        self.game_status.setStyleSheet("""
            QLabel {
                color: #9a3412;
                font-size: 12px;
            }
        """)
        status_layout.addWidget(self.game_status, 1)  # Stretch factor
        
        card_layout.addWidget(status_card)
        
        # Last Lap Time Card - use horizontal layout like game status
        lap_time_card = QWidget()
        lap_time_card.setStyleSheet("""
            QWidget {
                background-color: #f9fafb;
                border-radius: 6px;
                padding: 6px;
                margin-bottom: 6px;
            }
        """)
        lap_time_layout = QHBoxLayout(lap_time_card)
        lap_time_layout.setContentsMargins(8, 6, 8, 6)
        lap_time_layout.setSpacing(5)
        
        lap_time_title = QLabel("Last Lap Time:")
        lap_time_title.setStyleSheet("font-weight: bold;")
        lap_time_title.setFixedWidth(120)  # Increased width from 80 to 100
        lap_time_layout.addWidget(lap_time_title)
        
        self.status_label = QLabel("No lap time recorded")
        self.status_label.setStyleSheet("""
            QLabel {
                color: #6b7280;
                font-size: 16px;
                font-weight: bold;
            }
        """)
        lap_time_layout.addWidget(self.status_label, 1)  # Stretch factor
        
        card_layout.addWidget(lap_time_card)
        
        main_layout.addWidget(card)
        
    def start_monitor(self, game_name):
        """Start monitoring the selected game"""
        try:
            print(f"\n=== Starting {game_name} Monitor ===")
            
            # Stop current monitor if it exists
            if self.current_monitor:
                print(f"Stopping {type(self.current_monitor).__name__}")
                try:
                    self.current_monitor.stop()
                except Exception as e:
                    # Ignore mmap errors when switching to iRacing
                    if game_name != "iRacing" or "mmap" not in str(e).lower():
                        print(f"Error stopping monitor: {e}")
                self.current_monitor = None
                time.sleep(0.5)  # Give it time to clean up
            
            # Clear previous status
            self.game_status.setText("Initializing...")
            self.game_status.setStyleSheet("color: rgb(107, 114, 128)")
                
            # Create and start new monitor
            monitor_class = self.AVAILABLE_GAMES.get(game_name)
            if monitor_class:
                print(f"Creating new {monitor_class.__name__}")
                try:
                    # Create the monitor
                    self.current_monitor = monitor_class()
                    
                    # Connect signals - all monitors are QThread subclasses now
                    print("Connecting monitor signals")
                    self.current_monitor.connection_status.connect(self.update_game_status)
                    self.current_monitor.lap_completed.connect(self.on_lap_completed)
                    
                    # Start the monitor
                    print(f"Starting {game_name} monitor")
                    self.current_monitor.start()
                    
                except Exception as monitor_error:
                    print(f"Error creating/starting monitor: {monitor_error}")
                    self.game_status.setText(f"Error starting monitor: {str(monitor_error)}")
                    self.game_status.setStyleSheet("color: rgb(239, 68, 68)")
            else:
                print(f"Unknown game: {game_name}")
                self.game_status.setText(f"Error: Unknown game {game_name}")
                self.game_status.setStyleSheet("color: red")
                
        except Exception as e:
            print(f"\n=== Monitor Start Error ===")
            print(f"Error type: {type(e).__name__}")
            print(f"Error message: {str(e)}")
            self.game_status.setText(f"Error: {str(e)}")
            self.game_status.setStyleSheet("color: red")
    
    def on_game_changed(self, game_name):
        """Handle game selection changes"""
        print(f"\nSwitching to {game_name}")
        self.start_monitor(game_name)
        
    def on_lap_completed(self, lap_time):
        # Automatically send the lap time
        self.send_lap_time(lap_time, from_game=True)
        
    def start_retry_timer(self):
        """Start a timer to retry failed lap times every 30 seconds"""
        self.retry_timer = threading.Timer(30.0, self.retry_failed_laps)
        self.retry_timer.daemon = True
        self.retry_timer.start()

    def retry_failed_laps(self):
        """Attempt to resend any failed lap times"""
        if self.failed_lap_times:
            print(f"\nAttempting to resend {len(self.failed_lap_times)} failed lap times...")
            
            # Try to send each failed lap time
            successful_sends = []
            for lap_data in self.failed_lap_times:
                try:
                    headers = {'Content-Type': 'application/json'}
                    response = requests.post(
                        self.server_url.text().strip(),
                        json=lap_data,
                        headers=headers,
                        timeout=5
                    )
                    
                    if response.status_code == 200:
                        successful_sends.append(lap_data)
                        print(f"Successfully resent lap time: {lap_data['lap_time']:.3f}s")
                except:
                    print(f"Failed to resend lap time: {lap_data['lap_time']:.3f}s")
            
            # Remove successfully sent lap times from the queue
            for lap_data in successful_sends:
                self.failed_lap_times.remove(lap_data)
            
            # Update status if any laps were resent
            if successful_sends:
                self.show_success(f"✅ Successfully resent {len(successful_sends)} lap time(s)!")
            
            if self.failed_lap_times:
                self.status_label.setText(f"⚠️ {len(self.failed_lap_times)} lap time(s) pending resend...")
        
        # Restart the timer
        self.start_retry_timer()

    def send_lap_time(self, lap_time=None, from_game=True):
        """Send the lap time to the server"""
        try:
            # Get the formatted lap time (if provided)
            formatted_time = None
            if lap_time is not None:
                formatted_time = format_time_ms(int(lap_time * 1000))

            # Get driver name (handle both QLineEdit and string)
            driver_name = self.driver_name.text() if hasattr(self.driver_name, 'text') else str(self.driver_name)

            # Check if we have all necessary data
            if not driver_name or driver_name == "":
                self.show_error("Please set a driver name first!")
                return False
            
            # Skip sending if we don't have a lap time
            if lap_time is None and from_game:
                self.show_error("No valid lap time to send!")
                return False
                
            # Check if email is required but not provided
            email_required = self.email_field_enabled.isChecked() if hasattr(self, 'email_field_enabled') else False
            if email_required and (not hasattr(self, 'last_driver_email') or not self.last_driver_email):
                self.show_error("Email is required but not provided. Please set driver email via the web interface.")
                return False

            # Prepare data with strict type conversion
            data = {
                "simulator_id": int(self.simulator_id.currentText()),  # Get selected simulator ID
                "driver_name": str(self.driver_name.text()).strip(),
                "lap_time": float(round(lap_time, 3)),  # Round to 3 decimal places
            }
            
            # Include formatted time if available
            if formatted_time:
                data["formatted_time"] = formatted_time
                
            # Include driver email if available
            if hasattr(self, 'last_driver_email') and self.last_driver_email:
                data["email"] = self.last_driver_email  # Changed from "driver_email" to "email"
                print(f"Including driver email: {self.last_driver_email}")
                
            # Get the current URL from the UI
            server_url = self.server_url.text().strip()
            
            print(f"\nSending data to server: {data}")
            print(f"Server URL: {server_url}")
            
            # Update status to show we're sending
            self.status_label.setText("📤 Sending lap time to server...")
            self.status_label.setStyleSheet("color: #0066cc; background-color: #f0f0f0; padding: 10px; border-radius: 5px; font-size: 12px;")
            
            # Send to server with detailed error handling
            try:
                headers = {'Content-Type': 'application/json'}
                response = requests.post(
                    server_url, 
                    json=data,
                    headers=headers,
                    timeout=5  # Add timeout
                )
                print(f"Response status: {response.status_code}")
                print(f"Response headers: {response.headers}")
                
                if response.status_code == 200:
                    self.show_success(f"✅ Lap time {lap_time:.3f}s sent successfully!")
                else:
                    # Add to retry queue on server error
                    if data not in self.failed_lap_times:
                        self.failed_lap_times.append(data)
                    try:
                        error_details = response.json()
                        error_msg = error_details.get('message', str(response.content))
                        self.show_error(f"❌ Server error {response.status_code}: {error_msg}")
                    except:
                        self.show_error(f"❌ Server error {response.status_code}: {response.content}")
                    print(f"Server response content: {response.content}")
            except requests.exceptions.Timeout:
                # Add to retry queue on timeout
                if data not in self.failed_lap_times:
                    self.failed_lap_times.append(data)
                self.show_error(f"⚠️ Request timed out. Will retry automatically. {len(self.failed_lap_times)} lap(s) pending.")
            except requests.exceptions.ConnectionError:
                # Add to retry queue on connection error
                if data not in self.failed_lap_times:
                    self.failed_lap_times.append(data)
                self.show_error(f"❌ Connection failed. Will retry automatically. {len(self.failed_lap_times)} lap(s) pending.")
            except Exception as e:
                # Add to retry queue on any other error
                if data not in self.failed_lap_times:
                    self.failed_lap_times.append(data)
                self.show_error(f"❌ Error sending lap time: {str(e)}")
                print(f"Error details: {str(e)}")
        except Exception as e:
            self.show_error(f"❌ Error preparing data: {str(e)}")
            
    def show_success(self, message):
        if "Lap time" in message:
            # Extract lap time for the last lap time display
            try:
                lap_time = float(message.split(" ")[2].replace("s", ""))
                self.status_label.setText(f"{lap_time:.3f}s")
                self.status_label.setStyleSheet("""
                    QLabel {
                        color: #111827;
                        font-size: 24px;
                        font-weight: bold;
                    }
                """)
            except:
                self.status_label.setText(message)
                self.status_label.setStyleSheet("""
                    QLabel {
                        color: #166534;
                        background-color: #dcfce7;
                        padding: 10px;
                        border-radius: 6px;
                        font-size: 13px;
                    }
                """)
        else:
            self.status_label.setText(message)
            self.status_label.setStyleSheet("""
                QLabel {
                    color: #166534;
                    background-color: #dcfce7;
                    padding: 10px;
                    border-radius: 6px;
                    font-size: 13px;
                }
            """)
            
    def show_error(self, message):
        self.status_label.setText(message)
        self.status_label.setStyleSheet("""
            QLabel {
                color: #991b1b;
                background-color: #fee2e2;
                padding: 10px;
                border-radius: 6px;
                font-size: 13px;
            }
        """)
        
    def update_game_status(self, message, color):
        # Add emoji indicators based on status
        if "Connected" in message:
            message = "🟢 " + message
            self.game_status.setStyleSheet("""
                QLabel {
                    color: #166534;
                    font-size: 13px;
                }
            """)
        elif "Attempting" in message or "Lost connection" in message:
            message = "🟡 " + message
            self.game_status.setStyleSheet("""
                QLabel {
                    color: #9a3412;
                    font-size: 13px;
                }
            """)
        elif "Error" in message:
            message = "🔴 " + message
            self.game_status.setStyleSheet("""
                QLabel {
                    color: #991b1b;
                    font-size: 13px;
                }
            """)
            
        self.game_status.setText(message)
        
    def closeEvent(self, event):
        if self.current_monitor:
            self.current_monitor.stop()
        if self.retry_timer:
            self.retry_timer.cancel()
        event.accept()

    def show_web_interface_message(self):
        pass  # Remove the popup message
    
    def update_driver_name(self, name):
        self.driver_name.setText(name)
        # Start driver timer if enabled
        if self.driver_timer_enabled.isChecked():
            try:
                # Get timer duration in minutes
                minutes = int(self.driver_timer_minutes.text())
                # Store the end time
                current_timestamp = int(time.time())
                self.timer_end_timestamp = current_timestamp + (minutes * 60)
            except ValueError:
                # Invalid number format
                self.show_error("Invalid timer duration. Using default 5 minutes.")
                self.timer_end_timestamp = int(time.time()) + (5 * 60)

    def select_background_image(self):
        from PyQt6.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Background Image", "", "Image Files (*.png *.jpg *.jpeg)"
        )
        if file_path:
            self.background_image_path.setText(file_path)
            # Copy the file to the resources directory
            import shutil
            import os
            os.makedirs('resources', exist_ok=True)
            # Get the filename from the path
            filename = os.path.basename(file_path)
            destination = os.path.join('resources', filename)
            shutil.copy(file_path, destination)
            self.show_success(f"Background image set to: {filename}")
            
    def on_update_driver_settings(self):
        """Update driver-related settings (timer, email field, background)"""
        # Process email field setting
        email_required = self.email_field_enabled.isChecked()
        print(f"Email requirement updated: {'Enabled' if email_required else 'Disabled'}")
        
        # Process driver timer setting
        timer_enabled = self.driver_timer_enabled.isChecked()
        try:
            timer_minutes = int(self.driver_timer_minutes.text())
            if timer_enabled:
                # Update the timer end timestamp if timer is enabled
                current_timestamp = int(time.time())
                self.timer_end_timestamp = current_timestamp + (timer_minutes * 60)
                print(f"Driver timer enabled: {timer_minutes} minutes")
            else:
                print("Driver timer disabled")
        except ValueError:
            self.show_error("Invalid timer duration. Please enter a valid number.")
            self.driver_timer_minutes.setText("5")  # Reset to default
        
        # Process background image
        if self.background_image_path.text():
            # Make sure the background image exists
            try:
                from PyQt6.QtWidgets import QFileDialog
                import shutil
                import os
                # Get the filename from the path
                image_path = self.background_image_path.text()
                filename = os.path.basename(image_path)
                # Create resources directory if it doesn't exist
                os.makedirs('resources', exist_ok=True)
                destination = os.path.join('resources', filename)
                # Copy file if it exists
                if os.path.exists(image_path) and os.path.isfile(image_path):
                    shutil.copy(image_path, destination)
                    print(f"Background image updated: {filename}")
            except Exception as e:
                self.show_error(f"Error setting background image: {str(e)}")
        
        # Show confirmation message based on what was updated
        message = "Driver settings updated successfully"
        if email_required:
            message += " - Email required"
        if timer_enabled:
            message += f" - Timer: {timer_minutes}min"
        
        self.show_success(message)

    def on_server_url_changed(self):
        """Handle server URL changes with validation"""
        url = self.server_url.text().strip()
        
        # Basic URL validation
        is_valid = url.startswith(('http://', 'https://')) and ':' in url.split('/')[-1]

        if is_valid:
            # Clear any previous error styling
            self.server_url.setStyleSheet("""
                QLineEdit {
                    padding: 8px;
                    border: 1px solid rgb(209, 213, 219);
                    border-radius: 6px;
                    background-color: white;
                    min-height: 24px;
                    font-size: 13px;
                }
            """)
        else:
            # Add error styling
            self.server_url.setStyleSheet("""
                QLineEdit {
                    padding: 8px;
                    border: 1px solid #ef4444;
                    border-radius: 6px;
                    background-color: white;
                    min-height: 24px;
                    font-size: 13px;
                }
            """)

    def test_and_save_connection(self):
        """Test connection and save URL if successful"""
        url = self.server_url.text().strip()
        test_button = self.findChild(QPushButton, "test-connection")
        
        # Update button to show testing state
        test_button.setText("Testing...")
        test_button.setEnabled(False)
        test_button.setStyleSheet("""
            QPushButton#test-connection {
                background-color: rgb(156, 163, 175);
                color: white;
                border: none;
                padding: 4px 8px;
                border-radius: 6px;
                font-weight: bold;
                font-size: 13px;
            }
        """)
        QApplication.processEvents()  # Update the UI immediately
        
        try:
            # Test connection with a 5-second timeout
            response = requests.get(url, timeout=5)
            
            if response.status_code == 501:  # Expected for GET request
                # Connection successful, save the URL
                self.current_server_url = url
                test_button.setText("Connected!")
                test_button.setStyleSheet("""
                    QPushButton#test-connection {
                        background-color: rgb(22, 163, 74);
                        color: white;
                        border: none;
                        padding: 4px 8px;
                        border-radius: 6px;
                        font-weight: bold;
                        font-size: 13px;
                    }
                """)
                
                # After 2 seconds, revert button text
                QTimer.singleShot(2000, lambda: self._reset_button_state(test_button))
                
            else:
                # Unexpected response
                test_button.setText("Failed!")
                test_button.setStyleSheet("""
                    QPushButton#test-connection {
                        background-color: rgb(220, 38, 38);
                        color: white;
                        border: none;
                        padding: 4px 8px;
                        border-radius: 6px;
                        font-weight: bold;
                        font-size: 13px;
                    }
                """)
                test_button.setToolTip(
                    f"Unexpected response from server (Code: {response.status_code}).\n"
                    "• Check if this is the correct server URL\n"
                    "• Verify the server is running properly"
                )
                # Revert to previous URL
                self.server_url.setText(self.current_server_url)
                # After 2 seconds, revert button state
                QTimer.singleShot(2000, lambda: self._reset_button_state(test_button))
                
        except requests.exceptions.Timeout:
            test_button.setText("Timeout!")
            test_button.setStyleSheet("""
                QPushButton#test-connection {
                    background-color: rgb(220, 38, 38);
                    color: white;
                    border: none;
                    padding: 4px 8px;
                    border-radius: 6px;
                    font-weight: bold;
                    font-size: 13px;
                }
            """)
            test_button.setToolTip(
                "Connection timed out.\n"
                "• Server might be slow or unresponsive\n"
                "• Check your network connection"
            )
            # Revert to previous URL
            self.server_url.setText(self.current_server_url)
            # After 2 seconds, revert button state
            QTimer.singleShot(2000, lambda: self._reset_button_state(test_button))
            
        except requests.exceptions.ConnectionError:
            test_button.setText("No Connection!")
            test_button.setStyleSheet("""
                QPushButton#test-connection {
                    background-color: rgb(220, 38, 38);
                    color: white;
                    border: none;
                    padding: 4px 8px;
                    border-radius: 6px;
                    font-weight: bold;
                    font-size: 13px;
                }
            """)
            test_button.setToolTip(
                "Connection failed.\n"
                "• Verify the server URL is correct\n"
                "• Check if the server is running\n"
                "• Ensure you're on the same network"
            )
            # Revert to previous URL
            self.server_url.setText(self.current_server_url)
            # After 2 seconds, revert button state
            QTimer.singleShot(2000, lambda: self._reset_button_state(test_button))
            
        except Exception as e:
            test_button.setText("Error!")
            test_button.setStyleSheet("""
                QPushButton#test-connection {
                    background-color: rgb(220, 38, 38);
                    color: white;
                    border: none;
                    padding: 4px 8px;
                    border-radius: 6px;
                    font-weight: bold;
                    font-size: 13px;
                }
            """)
            test_button.setToolTip(
                "Connection error.\n"
                "• Check the console for error details\n"
                "• Verify all settings are correct"
            )
            print(f"Connection test failed: {str(e)}")
            # Revert to previous URL
            self.server_url.setText(self.current_server_url)
            # After 2 seconds, revert button state
            QTimer.singleShot(2000, lambda: self._reset_button_state(test_button))

    def _reset_button_state(self, button):
        """Reset the button to its default state"""
        button.setText("Test")  # Update reset text
        button.setEnabled(True)
        button.setStyleSheet("""
            QPushButton#test-connection {
                background-color: rgb(37, 99, 235);
                color: white;
                border: none;
                padding: 8px 12px;
                border-radius: 6px;
                font-weight: bold;
                font-size: 13px;
                min-width: 120px;
            }
            QPushButton#test-connection:hover {
                background-color: rgb(29, 78, 216);
            }
        """)

    def generate_qr_code(self, url):
        """Generate QR code for the web interface URL"""
        # Create QR code instance
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,  # Improved error correction
            box_size=10,  # Larger box size for better scanning
            border=4,    # Increased border for better spacing
        )
        
        # Add data
        qr.add_data(url)
        qr.make(fit=True)
        
        # Create image
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Convert PIL image to QPixmap
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        
        # Create QImage from buffer
        qimage = QImage.fromData(buffer.getvalue())
        
        # Create and return scaled QPixmap
        return QPixmap.fromImage(qimage).scaled(
            90, 90,  # Larger size to fit in container
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )

    def validate_simulator_id(self):
        """Validate the simulator ID input"""
        try:
            text = self.simulator_id.text().strip()
            if text:
                value = int(text)
                if 1 <= value <= 99:
                    # Valid ID - clear any error styling
                    self.simulator_id.setStyleSheet("""
                        QLineEdit {
                            padding: 8px;
                            border: 1px solid rgb(209, 213, 219);
                            border-radius: 6px;
                            background-color: white;
                            min-height: 24px;
                            font-size: 13px;
                        }
                    """)
                    return True
                
            # Invalid ID - add error styling
            self.simulator_id.setStyleSheet("""
                QLineEdit {
                    padding: 8px;
                    border: 1px solid #ef4444;
                    border-radius: 6px;
                    background-color: white;
                    min-height: 24px;
                    font-size: 13px;
                }
            """)
            return False
            
        except ValueError:
            # Not a number - add error styling
            self.simulator_id.setStyleSheet("""
                QLineEdit {
                    padding: 8px;
                    border: 1px solid #ef4444;
                    border-radius: 6px;
                    background-color: white;
                    min-height: 24px;
                    font-size: 13px;
                }
            """)
            return False

def main():
    app = QApplication(sys.argv)
    
    # Set application icon - use absolute path and handle resource path properly
    if getattr(sys, 'frozen', False):
        # Running as compiled
        base_path = sys._MEIPASS
    else:
        # Running from script
        base_path = os.path.dirname(os.path.abspath(__file__))
        
    icon_path = os.path.join(base_path, 'resources', 'icon.png')
    
    if os.path.exists(icon_path):
        app_icon = QIcon(icon_path)
        app.setWindowIcon(app_icon)
        
        # Also set the taskbar icon for Windows
        if os.name == 'nt':  # Windows
            myappid = 'simcoaches.leaderboardsender.1.0'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    else:
        print(f"Warning: Icon not found at {icon_path}")
    
    window = LapTimeSenderWindow()
    if os.path.exists(icon_path):
        window.setWindowIcon(app_icon)
    window.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()

