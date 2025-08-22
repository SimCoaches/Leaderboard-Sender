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
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QObject, QUrl
from PyQt6.QtGui import QImage, QPixmap, QIcon, QFont
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget
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

def validate_lap_time(lap_time, context=""):
    """Validate if a lap time is realistic for racing
    
    Args:
        lap_time (float): Lap time in seconds
        context (str): Additional context for logging
        
    Returns:
        bool: True if lap time is valid, False otherwise
    """
    if lap_time is None or lap_time <= 0:
        return False
    
    # Realistic lap time bounds
    MIN_LAP_TIME = 15.0   # 15 seconds (very short tracks)
    MAX_LAP_TIME = 600.0  # 10 minutes (very long tracks)
    
    if lap_time < MIN_LAP_TIME:
        print(f"⚠️ Lap time {lap_time:.3f}s too fast - likely invalid {context}")
        return False
    
    if lap_time > MAX_LAP_TIME:
        print(f"⚠️ Lap time {lap_time:.3f}s too slow - likely invalid {context}")
        return False
    
    return True

def is_valid_racing_context(graphics_data, physics_data=None, context=""):
    """Check if current racing context is valid for lap time recording
    
    Args:
        graphics_data: Game graphics data structure
        physics_data: Game physics data structure (optional)
        context (str): Game context for logging
        
    Returns:
        bool: True if context is valid for recording lap times
    """
    try:
        # For Assetto Corsa
        if context == "Assetto Corsa" and graphics_data:
            # Skip if in pit lane or pit box
            if hasattr(graphics_data, 'isInPit') and graphics_data.isInPit:
                print(f"⚠️ Skipping lap - driver is in pit")
                return False
            
            if hasattr(graphics_data, 'isInPitLane') and graphics_data.isInPitLane:
                print(f"⚠️ Skipping lap - driver is in pit lane")
                return False
            
            # Only accept race or practice sessions (status 2 = Live/Racing)
            if hasattr(graphics_data, 'status') and graphics_data.status != 2:
                print(f"⚠️ Skipping lap - not in active session (status: {graphics_data.status})")
                return False
        
        # For iRacing, we could add similar checks here in the future
        # when we have access to pit/session status from iRacing SDK
        
        return True
        
    except Exception as e:
        print(f"Error validating racing context: {e}")
        return True  # Default to accepting if we can't validate

def get_local_ip():
    """Get local IP address with improved fallback handling"""
    try:
        # Create a socket to get the local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Doesn't actually connect, just helps determine the local IP
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        print(f"Warning: Could not determine local IP ({e}), using localhost")
        try:
            # Fallback: try connecting to local network gateway
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('192.168.1.1', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return '127.0.0.1'  # Final fallback to localhost

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
        
        .current-driver {
            background-color: #f0f8ff; /* Light blue background */
            border-left: 4px solid #4169e1; /* Royal blue border */
            padding: 10px 15px;
            margin-bottom: 20px;
            border-radius: 4px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .current-driver p {
            margin: 0;
            font-weight: bold;
            color: #333;
        }
        
        .current-driver span {
            color: #4169e1;
            font-weight: bold;
        }
        
        .current-driver-title {
            font-size: 0.85em;
            color: #666;
            margin-bottom: 3px;
        }
        
        /* Logo styling */
        .logo-container {
            text-align: center;
            margin-top: 20px;
            padding-top: 15px;
            border-top: 1px solid #ddd;
            background-color: transparent; /* Change to transparent */
        }
        
        .logo-container img {
            max-width: 100%;
            height: auto;
            max-height: 120px;
            width: auto;
            margin: 10px 0;
            background-color: transparent; /* Keep image background transparent */
            /* Remove any potential box-shadow or border */
            box-shadow: none;
            border: none;
        }
    </style>
</head>
<body>
    <div class="container" id="container">
        <h1>🏁 Driver Registration</h1>
        <p style="text-align: center; color: #666; margin-bottom: 20px; font-style: italic;">
            Set your driver name for lap time tracking
        </p>
        
        <div class="current-driver" id="currentDriverBox" style="display:none;">
            <div>
                <p class="current-driver-title">🏆 Active Driver:</p>
                <p><span id="currentDriverName">None</span></p>
            </div>
            <div style="font-size: 0.8em; color: #666;">
                Ready for racing!
            </div>
        </div>
        
        <div class="input-group">
            <label for="driverName">👤 Driver Name:</label>
            <input type="text" id="driverName" placeholder="Enter your name (e.g., John Smith)" required>
            <small style="color: #666; font-style: italic;">This will appear on the leaderboard</small>
        </div>
        <div class="input-group" id="emailGroup" style="display: none;">
            <label for="driverEmail">📧 Email Address: <span style="color: red;">*</span></label>
            <input type="email" id="driverEmail" placeholder="Enter email address (required)" required>
            <small style="color: #666; font-style: italic;">Required for driver communication and results</small>
        </div>
        <button onclick="updateDriver()">🏁 Register as Active Driver</button>
        <div id="status" class="status" style="display: none;"></div>
        <div id="timerStatus" style="display: none; margin-top: 15px;"></div>
        
        <!-- Logo at the bottom of the form -->
        <div class="logo-container">
            <img src="/logo" alt="Company Logo" id="companyLogo">
        </div>
    </div>
    
    <script>
        // Log screen dimensions for debugging
        console.log(`Screen dimensions: ${window.innerWidth}x${window.innerHeight}`);
        console.log(`Screen ratio: ${window.innerWidth/window.innerHeight}`);
        console.log(`Device pixel ratio: ${window.devicePixelRatio}`);
        
        // Variable to track if this is the initial page load
        let isInitialLoad = true;
        
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
                    // Update current driver display
                    document.getElementById('currentDriverName').textContent = data.driver_name;
                    document.getElementById('currentDriverBox').style.display = 'flex';
                    
                    // Clear form fields
                    document.getElementById('driverName').value = '';
                    if (emailGroup.style.display !== 'none') {
                        document.getElementById('driverEmail').value = '';
                    }
                    
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
                    
                    // Render rotation status: time or laps
                    if (data.rotation_mode === 'Laps' && data.laps_active) {
                        if (!data.laps_progress.started) {
                            timerStatus.textContent = 'Out lap in progress...';
                        } else {
                            timerStatus.textContent = `Laps: ${data.laps_progress.count}/${data.laps_progress.target}`;
                        }
                        timerStatus.style.display = 'block';
                        container.classList.remove('timer-alert');
                    } else if (data.timer_active && data.timer_seconds_left <= 0) {
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
                        // No timer or lap-based rotation
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
                    // On initial load, populate the form with current driver
                    if (isInitialLoad) {
                        document.getElementById('driverName').value = data.driver_name;
                        isInitialLoad = false;
                    }
                    
                    // Always update the current driver display
                    document.getElementById('currentDriverName').textContent = data.driver_name;
                    document.getElementById('currentDriverBox').style.display = 'flex';
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

# Store the main window instance and UI bridge globally
main_window = None
ui_bridge = None

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
            # Emit signal to UI thread to update driver and start video
            try:
                if ui_bridge:
                    ui_bridge.driver_registered.emit(driver_name)
                else:
                    # Fallback (not ideal): direct call
                    main_window.update_driver_name(driver_name)
            except Exception:
                main_window.update_driver_name(driver_name)
            # Store email if provided
            if driver_email:
                main_window.last_driver_email = driver_email
            return jsonify({'success': True, 'driver_name': driver_name})
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
            'timer_seconds_left': 0,
            'rotation_mode': 'Time',
            'laps_active': False,
            'laps_progress': {
                'started': False,
                'count': 0,
                'target': 0
            }
        })
    
    # Get email requirement
    require_email = main_window.email_field_enabled.isChecked()
    
    # Get background image
    background_image = os.path.basename(main_window.background_image_path.text()) if main_window.background_image_path.text() else None
    
    # Check timer - only show as active if it has actually started (after video ends)
    timer_active = (hasattr(main_window, 'rotation_mode_selector') and 
                   main_window.rotation_mode_selector.currentText() == 'Time' and
                   hasattr(main_window, 'timer_started') and 
                   main_window.timer_started)
    timer_seconds_left = 0
    
    if timer_active and hasattr(main_window, 'timer_end_timestamp'):
        current_time = int(time.time())
        end_time = main_window.timer_end_timestamp
        timer_seconds_left = max(0, end_time - current_time)
    
    # Lap-based rotation status
    rotation_mode = getattr(main_window, 'rotation_mode_selector').currentText() if hasattr(main_window, 'rotation_mode_selector') else 'Time'
    laps_active = bool(getattr(main_window, 'lap_rotation_active', False))
    laps_progress = {
        'started': bool(getattr(main_window, 'lap_count_started', False)),
        'count': int(getattr(main_window, 'lap_rotation_count', 0)),
        'target': int(getattr(main_window, 'lap_rotation_target', 0)),
    }
    
    return jsonify({
        'require_email': require_email,
        'background_image': background_image,
        'timer_active': timer_active,
        'timer_seconds_left': timer_seconds_left,
        'rotation_mode': rotation_mode,
        'laps_active': laps_active,
        'laps_progress': laps_progress
    })

@app.route('/backgrounds/<filename>')
def background_file(filename):
    return send_from_directory('resources', filename)

@app.route('/logo')
def serve_logo():
    # Serve the logo.png file from the resources directory
    return send_from_directory('resources', 'logo.png')

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
                        print(f"Connection lost: {e}")
                        # Clean up handles on connection loss for AC
                        if hasattr(self, 'cleanup_handles'):
                            self.cleanup_handles()
                    self.msleep(1000)
                    
        except Exception as e:
            print(f"{self.game_name} Monitor error: {str(e)}")
            self.connection_status.emit(f"Error monitoring {self.game_name}: {str(e)}", "red")
            
    def stop(self):
        print(f"\n=== Stopping {self.game_name} Monitor ===")
        self.running = False
        self.wait()

class UIBridge(QObject):
    """Bridge signals to ensure Flask thread requests are handled on the UI thread"""
    driver_registered = pyqtSignal(str)

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
            
            # Debug info only when lap completed (reduce spam)
            if graphics.completedLaps > self.last_completed_laps:
                print(f"\n=== Lap Completed ===")
                print(f"Completed Laps: {graphics.completedLaps}")
                print(f"Last Time: {graphics.lastTime}")
                print(f"Speed: {physics.speedKmh:.1f} km/h")
            
            # Check if we've completed a new lap
            if graphics.completedLaps > self.last_completed_laps:
                self.last_completed_laps = graphics.completedLaps
                
                # Validate racing context first
                if not is_valid_racing_context(graphics, physics, "Assetto Corsa"):
                    return False, 0
                
                # Convert last time from string (mm:ss.xxx) to seconds
                try:
                    last_time = graphics.iLastTime / 1000.0  # Convert from ms to seconds
                    if validate_lap_time(last_time, "Assetto Corsa"):
                        print(f"✅ Valid lap time recorded: {last_time:.3f}s")
                        return True, last_time
                    else:
                        print(f"Invalid lap time rejected: {last_time:.3f}s")
                except Exception as e:
                    print(f"Error processing lap time: {e}")
                    pass
            
            return False, 0
            
        except Exception as e:
            print(f"Error reading lap time: {str(e)}")
            return False, 0
            
    def cleanup_handles(self):
        """Safely cleanup memory mapped file handles"""
        try:
            if hasattr(self, 'physics_handle') and self.physics_handle:
                try:
                    self.physics_handle.close()
                    print("✅ Physics handle closed")
                except (ValueError, OSError) as e:
                    print(f"Physics handle already closed: {e}")
                finally:
                    self.physics_handle = None
                    
            if hasattr(self, 'graphics_handle') and self.graphics_handle:
                try:
                    self.graphics_handle.close()
                    print("✅ Graphics handle closed")
                except (ValueError, OSError) as e:
                    print(f"Graphics handle already closed: {e}")
                finally:
                    self.graphics_handle = None
                    
        except Exception as e:
            print(f"Error during cleanup: {e}")

    def stop(self):
        """Clean up Assetto Corsa shared memory handles"""
        print(f"\n=== Stopping {self.game_name} Monitor ===")
        self.running = False
        self.cleanup_handles()
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
        
        # Only log telemetry when lap completed (reduce spam)
        if (last_lap_time is not None and last_lap_time > 0 
            and last_lap_time != self.last_sent_lap_time):
            print(f"\n=== iRacing Lap Completed ===")
            print(f"Lap Number: {lap_number if lap_number is not None else 0}")
            print(f"Lap Time: {self.format_time(last_lap_time)}")
            print(f"Speed: {speed_mph:.1f} MPH")
        
        # Check for completed lap with proper validation
        if (last_lap_time is not None and last_lap_time != self.last_sent_lap_time 
            and validate_lap_time(last_lap_time, "iRacing")):
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
            # Clean up connection on major error
            if self.state.ir_connected:
                try:
                    self.ir.shutdown()
                    self.state.ir_connected = False
                    print("iRacing SDK disconnected due to error")
                except:
                    pass
            
    def stop(self):
        print(f"\n=== Stopping {self.game_name} Monitor ===")
        self.running = False
        try:
            if self.state.ir_connected:
                print("Shutting down iRacing SDK connection...")
                self.ir.shutdown()
                self.state.ir_connected = False
                print("✅ iRacing SDK disconnected")
        except Exception as e:
            print(f"Error shutting down iRacing SDK: {e}")
        self.wait()

class State:
    ir_connected = False

class WaitingOverlay(QWidget):
    """Full-screen overlay that shows custom image or looping video until a driver signs up"""
    
    def __init__(self):
        super().__init__()
        self.media_path = ""
        self.is_video = False
        self.media_player = None
        self.video_widget = None
        self.image_label = None
        self.setup_ui()
        
    def setup_ui(self):
        # Make it a full-screen overlay window
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | 
                           Qt.WindowType.WindowStaysOnTopHint |
                           Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        
        # Get screen geometry and make it full screen
        screen = QApplication.primaryScreen()
        screen_geometry = screen.geometry()
        self.setGeometry(screen_geometry)
        
        # Default dark background
        self.setStyleSheet("""
            QWidget {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #000000, stop:1 #1a1a2e);
            }
        """)
        
        # Main layout
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        
        # Show default content if no media is set
        self.show_default_content()
        
    def show_default_content(self):
        """Show default waiting screen content when no custom media is set"""
        # Clear existing content
        self.clear_media()
        
        # Create centered content
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Title
        title = QLabel("🏁 Racing Session")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = QFont("Segoe UI", 48, QFont.Weight.Bold)
        title.setFont(title_font)
        title.setStyleSheet("color: #ffffff; margin-bottom: 20px;")
        content_layout.addWidget(title)
        
        # Subtitle
        subtitle = QLabel("Waiting for driver to sign up...")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle_font = QFont("Segoe UI", 24)
        subtitle.setFont(subtitle_font)
        subtitle.setStyleSheet("color: #94a3b8; margin-bottom: 40px;")
        content_layout.addWidget(subtitle)
        
        # Instructions
        instructions = QLabel("📱 Use your mobile device to scan the QR code\nand register as the active driver")
        instructions.setAlignment(Qt.AlignmentFlag.AlignCenter)
        instructions_font = QFont("Segoe UI", 18)
        instructions.setFont(instructions_font)
        instructions.setStyleSheet("color: #6ee7b7; line-height: 1.5;")
        content_layout.addWidget(instructions)
        
        # Close instruction
        close_info = QLabel("Press ESC to close this overlay")
        close_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        close_info_font = QFont("Segoe UI", 14)
        close_info.setFont(close_info_font)
        close_info.setStyleSheet("color: #64748b; margin-top: 40px;")
        content_layout.addWidget(close_info)
        
        self.layout.addWidget(content_widget)

    def show_session_complete(self):
        """Show a prominent 'Session Complete' interstitial screen"""
        # Stop and clear any media, then show completion message
        self.clear_media()
        
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        title = QLabel("✅ Session Complete")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = QFont("Segoe UI", 52, QFont.Weight.Bold)
        title.setFont(title_font)
        title.setStyleSheet("color: #10b981; margin-bottom: 20px;")
        content_layout.addWidget(title)
        
        subtitle = QLabel("Please change driver. Returning to waiting screen...")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle_font = QFont("Segoe UI", 22)
        subtitle.setFont(subtitle_font)
        subtitle.setStyleSheet("color: #cbd5e1; margin-bottom: 16px;")
        content_layout.addWidget(subtitle)
        
        hint = QLabel("Tip: You can manually show/hide the waiting screen from the desktop app.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_font = QFont("Segoe UI", 14)
        hint.setFont(hint_font)
        hint.setStyleSheet("color: #94a3b8;")
        content_layout.addWidget(hint)
        
        self.layout.addWidget(content_widget)
        
    def keyPressEvent(self, event):
        """Handle key press events"""
        from PyQt6.QtCore import Qt
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        super().keyPressEvent(event)
        
    def set_media(self, file_path):
        """Set the media (image or video) for the waiting screen"""
        self.media_path = file_path
        
        # Determine if it's a video or image
        video_extensions = ['.mp4', '.mov', '.mkv', '.avi']
        self.is_video = any(file_path.lower().endswith(ext) for ext in video_extensions)
        
        # Clear existing widgets
        self.clear_media()
        
        if self.is_video:
            self.setup_video()
        else:
            self.setup_image()
            
    def clear_media(self):
        """Clear any existing media widgets"""
        # Stop video if playing
        if self.media_player:
            self.media_player.stop()
            
        # Remove all widgets from layout
        while self.layout.count():
            child = self.layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
                
        self.image_label = None
        self.video_widget = None
        
    def setup_image(self):
        """Setup image display"""
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setScaledContents(True)
        
        # Load and display image
        pixmap = QPixmap(self.media_path)
        if not pixmap.isNull():
            # Scale to fill screen while maintaining aspect ratio
            screen = QApplication.primaryScreen()
            screen_size = screen.geometry().size()
            scaled_pixmap = pixmap.scaled(screen_size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            self.image_label.setPixmap(scaled_pixmap)
        
        self.layout.addWidget(self.image_label)
        
    def setup_video(self):
        """Setup looping video display"""
        # Initialize video components
        self.media_player = QMediaPlayer(self)
        self.video_widget = QVideoWidget()
        
        # Create audio output
        audio_output = QAudioOutput(self)
        audio_output.setVolume(1.0)
        self.media_player.setAudioOutput(audio_output)
        self.media_player.setVideoOutput(self.video_widget)
        
        # Set video to fill the overlay
        self.layout.addWidget(self.video_widget)
        
        # Setup looping
        def on_status_changed(status):
            from PyQt6.QtMultimedia import QMediaPlayer as _QMP
            if status == _QMP.MediaStatus.EndOfMedia:
                # Loop the video
                self.media_player.setPosition(0)
                self.media_player.play()
        
        self.media_player.mediaStatusChanged.connect(on_status_changed)
        
        # Load and start playing
        self.media_player.setSource(QUrl.fromLocalFile(self.media_path))
        
    def show(self):
        """Show the overlay and start video if applicable"""
        super().show()
        if self.is_video and self.media_player:
            self.media_player.play()
            
    def hide(self):
        """Hide the overlay and stop video if applicable"""
        if self.is_video and self.media_player:
            self.media_player.stop()
        super().hide()
        
    def update_message(self, message):
        """Legacy method - no longer used with custom media"""
        pass

class CollapsibleSection(QWidget):
    """Simple collapsible section with a header button and a content area"""
    
    def __init__(self, title: str):
        super().__init__()
        self.toggle_button = QPushButton(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(False)
        self.toggle_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0ea5b7, stop:1 #0d9488);
                color: #ecfeff;
                border: none;
                padding: 6px 10px;
                border-radius: 6px;
                font-weight: 700;
                font-size: 12px;
                text-align: left;
            }
            QPushButton:checked {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0d9488, stop:1 #0f766e);
            }
        """)
        
        self.content_area = QWidget()
        self.content_area.setVisible(False)
        self.content_layout = QGridLayout(self.content_area)
        self.content_layout.setContentsMargins(8, 8, 8, 8)
        self.content_layout.setHorizontalSpacing(12)
        self.content_layout.setVerticalSpacing(8)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self.content_area)
        
        self.toggle_button.toggled.connect(self.on_toggled)
    
    def on_toggled(self, checked: bool):
        self.content_area.setVisible(checked)
    
    def setCollapsed(self, collapsed: bool):
        self.toggle_button.setChecked(not collapsed)

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
        # Lap-based rotation state
        self.lap_rotation_active = False
        self.lap_rotation_target = 0
        self.lap_rotation_count = 0
        self.lap_count_started = False
        self.setup_ui()
        
        # Start with no monitor active
        self.current_monitor = None
        
        # Start the Flask server in a separate thread
        global main_window, ui_bridge
        main_window = self
        ui_bridge = UIBridge()
        ui_bridge.driver_registered.connect(self.update_driver_name)
        self.web_thread = threading.Thread(target=run_flask, daemon=True)
        self.web_thread.start()
        
        # Start retry timer
        self.start_retry_timer()
        
        # Start periodic session timer check
        self.session_timer = QTimer()
        self.session_timer.timeout.connect(self.check_session_timeout)
        self.session_timer.start(1000)  # Check every second
        
        # Start monitoring the initially selected game
        self.start_monitor(self.game_selector.currentText())
        
        # Show waiting overlay on startup
        self.show_waiting_screen()
        
    def setup_ui(self):
        # Main window settings
        self.setWindowTitle("Lap Time Sender - Racing Leaderboard Interface")
        self.setMinimumSize(820, 980)  # Taller to accommodate new settings rows
        self.resize(950, 1020)  # Wider/taller to give the Mobile section more space
        self.setWindowIcon(QIcon("resources/icon.png"))
        self.setStyleSheet("""
            QMainWindow {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #1a1a2e, stop:1 #16213e);
                color: #e2e8f0;
            }
            QLabel {
                font-size: 13px;
                color: #e2e8f0;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QLineEdit {
                padding: 8px 12px;
                border: 2px solid #334155;
                border-radius: 8px;
                background-color: #1e293b;
                color: #e2e8f0;
                min-height: 20px;
                font-size: 13px;
                selection-background-color: #3b82f6;
            }
            QLineEdit:read-only {
                background-color: #0f172a;
                border: 2px solid #1e293b;
                color: #94a3b8;
            }
            QLineEdit:focus {
                border: 2px solid #3b82f6;
                background-color: #0f172a;
            }
            QComboBox {
                padding: 8px 12px;
                border: 2px solid #334155;
                border-radius: 8px;
                background-color: #1e293b;
                color: #e2e8f0;
                min-height: 20px;
                font-size: 13px;
            }
            QComboBox:hover {
                border: 2px solid #475569;
                background-color: #334155;
            }
            QComboBox::drop-down {
                border: none;
                background-color: #334155;
                border-radius: 4px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #e2e8f0;
                margin-right: 8px;
            }
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #3b82f6, stop:1 #2563eb);
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 8px;
                font-weight: 600;
                font-size: 13px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #2563eb, stop:1 #1d4ed8);
                transform: translateY(-1px);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #1d4ed8, stop:1 #1e40af);
            }
            QPushButton:checked {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #10b981, stop:1 #059669);
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
        main_layout.setContentsMargins(12, 12, 12, 12)  # Proper margins
        main_layout.setSpacing(10)  # Adequate spacing between sections
        
        # Create a "card" container with modern dark styling
        card = QWidget()
        # Scope style to this container only to avoid bleeding into children
        card.setObjectName("mainCard")
        card.setStyleSheet("""
            #mainCard {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #1e293b, stop:1 #0f172a);
                border-radius: 12px;
                border: 1px solid #334155;
            }
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)  # Generous margins
        card_layout.setSpacing(12)  # Proper spacing between sections
        
        # Title with subtitle for clarity
        title_widget = QWidget()
        title_layout = QVBoxLayout(title_widget)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(2)
        
        title = QLabel("🏁 Lap Time Sender")
        title.setStyleSheet("""
            QLabel {
                font-size: 24px;
                font-weight: 700;
                color: #ffffff;
                margin-bottom: 4px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                text-shadow: 0px 2px 4px rgba(0, 0, 0, 0.5);
            }
        """)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_layout.addWidget(title)
        
        subtitle = QLabel("Professional Racing Telemetry & Leaderboard Interface")
        subtitle.setStyleSheet("""
            QLabel {
                font-size: 13px;
                color: #94a3b8;
                font-style: italic;
                margin-bottom: 6px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        title_layout.addWidget(subtitle)
        
        card_layout.addWidget(title_widget)
        
        # Web interface info
        url_info_layout = QHBoxLayout()
        url_info_layout.setSpacing(12)  # Proper spacing for readability
        
        # Create a widget for the mobile interface info box
        info_widget = QWidget()
        info_widget.setObjectName("infoWidget")
        info_widget.setStyleSheet("""
            #infoWidget {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                    stop:0 #1e3a8a, stop:1 #3730a3);
                border-radius: 8px;
                border: 1px solid #3b82f6;
            }
        """)
        info_layout = QVBoxLayout(info_widget)
        info_layout.setContentsMargins(12, 10, 12, 10)  # Proper padding for readability
        
        # Web interface info text - modern dark styling
        url_info_text = QLabel(f"📱 Mobile Interface: http://{get_local_ip()}:5001")
        url_info_text.setStyleSheet("""
            QLabel {
                color: #ffffff;
                font-size: 13px;
                font-weight: 600;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        url_info_text.setWordWrap(True)
        
        # Access info text - elegant styling
        access_info = QLabel("✨ Scan QR code or visit URL from any mobile device")
        access_info.setStyleSheet("""
            QLabel {
                color: #bfdbfe;
                font-size: 11px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        access_info.setWordWrap(True)
        
        # Add labels to info layout
        info_layout.addWidget(url_info_text)
        info_layout.addWidget(access_info)
        
        # Add info widget to main layout
        url_info_layout.addWidget(info_widget, stretch=1)
        
        # Generate and add QR code - more compact
        qr_code = self.generate_qr_code(f"http://{get_local_ip()}:5001")
        qr_label = QLabel()
        qr_label.setPixmap(qr_code)
        qr_label.setFixedSize(90, 90)  # Slightly smaller
        qr_label.setStyleSheet("""
            QLabel {
                background-color: #ffffff;
                padding: 6px;
                border-radius: 8px;
                border: 2px solid #3b82f6;
                box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.3);
            }
        """)
        qr_label.setToolTip("Scan with phone/tablet to access driver interface")
        qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        url_info_layout.addWidget(qr_label)
        
        card_layout.addLayout(url_info_layout)
        
        # Form setup
        form_layout = QFormLayout()
        form_layout.setVerticalSpacing(8)  # Further reduce spacing
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        
        # Server Configuration Section
        server_section = QWidget()
        server_section.setObjectName("serverSection")
        server_section.setStyleSheet("""
            #serverSection {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                    stop:0 #0f172a, stop:1 #1e293b);
                border-radius: 10px;
                padding: 12px;
                margin: 6px 0;
                border: 1px solid #3b82f6;
            }
        """)
        server_section_layout = QVBoxLayout(server_section)
        server_section_layout.setContentsMargins(15, 12, 15, 12)
        server_section_layout.setSpacing(10)
        
        # Server section header
        server_header = QLabel("🌐 Leaderboard Server Connection")
        server_header.setStyleSheet("""
            QLabel {
                font-weight: 700;
                font-size: 14px;
                color: #ffffff;
                margin-bottom: 4px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        server_section_layout.addWidget(server_header)
        
        # Server URL input
        server_url_widget = QWidget()
        server_url_layout = QHBoxLayout(server_url_widget)
        server_url_layout.setContentsMargins(0, 0, 0, 0)
        server_url_layout.setSpacing(8)
        
        url_label = QLabel("Server URL:")
        url_label.setStyleSheet("""
            QLabel {
                font-weight: 600;
                font-size: 13px;
                color: #bfdbfe;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        url_label.setFixedWidth(80)
        url_label.setToolTip("Address of the leaderboard server where lap times will be sent")
        server_url_layout.addWidget(url_label)
        
        self.server_url = QLineEdit(self.current_server_url)
        self.server_url.textChanged.connect(self.on_server_url_changed)
        self.server_url.setToolTip("Enter the full URL (e.g., http://192.168.1.100:5000)")
        self.server_url.setStyleSheet("""
            QLineEdit {
                background-color: #0f172a;
                border: 2px solid #475569;
                color: #e2e8f0;
                font-family: 'Consolas', 'Monaco', monospace;
            }
        """)
        server_url_layout.addWidget(self.server_url)
        
        test_connection_btn = QPushButton("Test Connection")
        test_connection_btn.setFixedWidth(120)
        test_connection_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #10b981, stop:1 #059669);
                color: white;
                border: none;
                padding: 8px 12px;
                border-radius: 8px;
                font-weight: 600;
                font-size: 12px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #059669, stop:1 #047857);
            }
        """)
        test_connection_btn.setObjectName("test-connection")
        test_connection_btn.clicked.connect(self.test_and_save_connection)
        test_connection_btn.setToolTip("Test if the server is reachable and save URL if successful")
        server_url_layout.addWidget(test_connection_btn)
        
        server_section_layout.addWidget(server_url_widget)
        # Add the server configuration section to the main card
        card_layout.addWidget(server_section)
        
        # Racing Configuration Section
        racing_section = QWidget()
        racing_section.setObjectName("racingSection")
        racing_section.setStyleSheet("""
            #racingSection {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                    stop:0 #1a1a2e, stop:1 #16213e);
                border-radius: 10px;
                padding: 12px;
                margin: 6px 0;
                border: 1px solid #f59e0b;
            }
        """)
        racing_section_layout = QVBoxLayout(racing_section)
        racing_section_layout.setContentsMargins(15, 12, 15, 12)
        racing_section_layout.setSpacing(10)
        
        # Racing section header
        racing_header = QLabel("🏁 Racing Simulator Configuration")
        racing_header.setStyleSheet("""
            QLabel {
                font-weight: 700;
                font-size: 14px;
                color: #ffffff;
                margin-bottom: 4px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        racing_section_layout.addWidget(racing_header)
        
        # Simulator configuration row - proper spacing
        simulator_row = QHBoxLayout()
        simulator_row.setSpacing(12)
        
        simulator_id_label = QLabel("Simulator ID:")
        simulator_id_label.setStyleSheet("""
            QLabel {
                font-weight: 600;
                font-size: 13px;
                color: #fbbf24;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        simulator_id_label.setFixedWidth(80)
        simulator_id_label.setToolTip("Unique identifier for this racing setup (1-99)")
        
        self.simulator_id = QComboBox()
        self.simulator_id.addItems([str(i) for i in range(1, 100)])
        self.simulator_id.setFixedWidth(60)
        self.simulator_id.setToolTip("Select a unique ID number for this racing simulator")
        
        # Game selection
        game_label = QLabel("Racing Game:")
        game_label.setStyleSheet("""
            QLabel {
                font-weight: 600;
                font-size: 13px;
                color: #fbbf24;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        game_label.setFixedWidth(90)
        game_label.setToolTip("Select which racing simulator you'll be using")
        
        self.game_selector = QComboBox()
        for game_name in self.AVAILABLE_GAMES.keys():
            self.game_selector.addItem(game_name)
        self.game_selector.currentTextChanged.connect(self.on_game_changed)
        self.game_selector.setToolTip("Choose between supported racing simulators")
        
        # Add these in a single row with proper spacing
        simulator_row.addWidget(simulator_id_label)
        simulator_row.addWidget(self.simulator_id)
        simulator_row.addSpacing(8)
        simulator_row.addWidget(game_label)
        simulator_row.addWidget(self.game_selector, 1)  # Give the game selector stretch priority
        
        # Only the simulator row belongs here; the server URL widget is part of the server section above
        racing_section_layout.addLayout(simulator_row)
        
        card_layout.addWidget(racing_section)

        
        # Add elegant divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        divider.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                    stop:0 #334155, stop:1 #475569);
                max-height: 2px;
                border-radius: 1px;
                margin: 4px 0;
            }
        """)
        card_layout.addWidget(divider)
        
        # Add iPad settings box
        ipad_box = QWidget()
        ipad_box.setObjectName("ipadBox")
        ipad_box.setStyleSheet("""
            #ipadBox {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                    stop:0 #064e3b, stop:1 #065f46);
                border-radius: 10px;
                padding: 12px;
                margin: 6px 0;
                border: 1px solid #10b981;
            }
        """)
        ipad_box_layout = QVBoxLayout(ipad_box)
        ipad_box_layout.setContentsMargins(15, 10, 15, 10)
        ipad_box_layout.setSpacing(8)
        
        # iPad Settings section header with explanation
        header_layout = QHBoxLayout()
        ipad_section_label = QLabel("📱 Mobile Interface Settings")
        ipad_section_label.setStyleSheet("""
            QLabel {
                font-weight: 700;
                font-size: 14px;
                color: #ffffff;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        header_layout.addWidget(ipad_section_label)
        
        # Add explanatory text
        ipad_explanation = QLabel("Configure iPad/tablet experience")
        ipad_explanation.setStyleSheet("""
            QLabel {
                font-size: 11px;
                color: #6ee7b7;
                font-style: italic;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        header_layout.addWidget(ipad_explanation)
        
        # Add settings update button to the header to save vertical space
        self.advanced_toggle_btn = QPushButton("Show Advanced")
        self.advanced_toggle_btn.setCheckable(True)
        self.advanced_toggle_btn.setChecked(False)
        self.advanced_toggle_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #22d3ee, stop:1 #06b6d4);
                color: #064e3b;
                font-weight: 700;
                padding: 6px 10px;
                font-size: 12px;
                border-radius: 8px;
                border: none;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:checked {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #06b6d4, stop:1 #0891b2);
                color: #022c22;
            }
        """)
        self.advanced_toggle_btn.toggled.connect(lambda checked: self.on_toggle_advanced(checked))
        header_layout.addWidget(self.advanced_toggle_btn)
        
        ipad_settings_update = QPushButton("Apply Settings")
        # Allow button to grow with DPI/scaling while maintaining a sensible minimum
        ipad_settings_update.setMinimumWidth(120)
        ipad_settings_update.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #34d399, stop:1 #10b981);
                color: #065f46;
                font-weight: 700;
                padding: 8px 12px;
                font-size: 12px;
                border-radius: 8px;
                border: none;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #6ee7b7, stop:1 #34d399);
                transform: translateY(-1px);
            }
        """)
        ipad_settings_update.clicked.connect(self.on_update_driver_settings)
        ipad_settings_update.setToolTip("Apply changes to the mobile web interface settings")
        header_layout.addStretch(1)
        header_layout.addWidget(ipad_settings_update)
        
        ipad_box_layout.addLayout(header_layout)
        
        # Use a grid layout for more compact control placement
        settings_grid = QWidget()
        grid_layout = QGridLayout(settings_grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setHorizontalSpacing(12)  # Slightly tighter horizontal spacing
        grid_layout.setVerticalSpacing(10)  # Slightly tighter vertical spacing
        
        # Set column stretch factors to ensure proper distribution
        grid_layout.setColumnStretch(0, 0)  # Fixed width for labels
        grid_layout.setColumnStretch(1, 4)  # More room to main input column
        grid_layout.setColumnStretch(2, 2)
        grid_layout.setColumnStretch(3, 2)
        
        # Row 0: Current driver name
        name_label = QLabel("Active Driver:")
        name_label.setMinimumWidth(90)
        name_label.setMaximumWidth(110)
        name_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        name_label.setStyleSheet("""
            QLabel {
                font-size: 13px;
                font-weight: 600;
                color: #ffffff;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        name_label.setToolTip("Shows current active driver - change via mobile interface")
        grid_layout.addWidget(name_label, 0, 0)
        
        self.driver_name = QLineEdit()
        self.driver_name.setReadOnly(True)
        self.driver_name.setPlaceholderText("Set via mobile interface above")
        self.driver_name.setToolTip("Driver name can only be changed via the mobile web interface")
        self.driver_name.setMinimumWidth(320)  # More room for names
        self.driver_name.setStyleSheet("""
            QLineEdit {
                background-color: #1e293b;
                border: 2px solid #10b981;
                color: #ffffff;
                font-size: 13px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                font-weight: 600;
                padding: 8px 12px;
                min-height: 24px;
            }
            QLineEdit::placeholder {
                color: #94a3b8;
            }
        """)
        grid_layout.addWidget(self.driver_name, 0, 1, 1, 3)  # Span 3 columns
        
        # Row 1: Timer settings
        self.timer_label = QLabel("Driver Timer:")
        self.timer_label.setMinimumWidth(90)
        self.timer_label.setMaximumWidth(110)
        self.timer_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.timer_label.setStyleSheet("""
            QLabel {
                font-size: 13px;
                font-weight: 600;
                color: #ffffff;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        self.timer_label.setToolTip("Automatic driver rotation alerts")
        grid_layout.addWidget(self.timer_label, 1, 0)
        
        self.driver_timer_enabled = QPushButton("⊠ Timer On")
        self.driver_timer_enabled.setCheckable(True)
        self.driver_timer_enabled.setChecked(False)
        self.driver_timer_enabled.setMinimumWidth(85)
        self.driver_timer_enabled.setMaximumWidth(110)
        self.driver_timer_enabled.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #475569, stop:1 #334155);
                color: #ffffff;
                border: 2px solid #64748b;
                padding: 8px 10px;
                border-radius: 6px;
                font-weight: 600;
                font-size: 11px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                min-height: 28px;
            }
            QPushButton:checked {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #34d399, stop:1 #10b981);
                color: #ffffff;
                border: 2px solid #10b981;
            }
            QPushButton:hover {
                border: 2px solid #10b981;
                color: #6ee7b7;
            }
        """)
        self.driver_timer_enabled.setToolTip("Show countdown timer on mobile interface")
        # Connect to text change when toggled
        self.driver_timer_enabled.toggled.connect(lambda checked: self.driver_timer_enabled.setText("⊠ Enabled" if checked else "☐ Disabled"))
        self.driver_timer_enabled.setText("☐ Disabled")  # Initial state
        grid_layout.addWidget(self.driver_timer_enabled, 1, 1)
        
        self.minutes_label = QLabel("Minutes:")
        self.minutes_label.setMinimumWidth(45)
        self.minutes_label.setMaximumWidth(65)
        self.minutes_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.minutes_label.setStyleSheet("""
            QLabel {
                font-size: 12px;
                color: #ffffff;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        grid_layout.addWidget(self.minutes_label, 1, 2)
        
        self.driver_timer_minutes = QLineEdit("5")
        self.driver_timer_minutes.setMinimumWidth(60)
        self.driver_timer_minutes.setMaximumWidth(90)
        self.driver_timer_minutes.setStyleSheet("""
            QLineEdit {
                background-color: #1e293b;
                border: 2px solid #10b981;
                color: #ffffff;
                font-size: 13px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                font-weight: 600;
                padding: 8px 6px;
                min-height: 24px;
            }
        """)
        self.driver_timer_minutes.setToolTip("Duration in minutes for each driver's turn")
        grid_layout.addWidget(self.driver_timer_minutes, 1, 3)
        
        # Row 2: Email toggle
        email_label = QLabel("Email Required:")
        email_label.setMinimumWidth(90)
        email_label.setMaximumWidth(110)
        email_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        email_label.setStyleSheet("""
            QLabel {
                font-size: 13px;
                font-weight: 600;
                color: #ffffff;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        email_label.setToolTip("Require drivers to enter email addresses")
        grid_layout.addWidget(email_label, 2, 0)
        
        self.email_field_enabled = QPushButton("⊠ Required")
        self.email_field_enabled.setCheckable(True)
        self.email_field_enabled.setChecked(False)
        self.email_field_enabled.setMinimumWidth(85)
        self.email_field_enabled.setMaximumWidth(130)
        self.email_field_enabled.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #475569, stop:1 #334155);
                color: #ffffff;
                border: 2px solid #64748b;
                padding: 8px 10px;
                border-radius: 6px;
                font-weight: 600;
                font-size: 11px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                min-height: 28px;
            }
            QPushButton:checked {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #34d399, stop:1 #10b981);
                color: #ffffff;
                border: 2px solid #10b981;
            }
            QPushButton:hover {
                border: 2px solid #10b981;
                color: #6ee7b7;
            }
        """)
        # Connect to text change when toggled
        self.email_field_enabled.toggled.connect(lambda checked: self.email_field_enabled.setText("⊠ Required" if checked else "☐ Optional"))
        self.email_field_enabled.setText("☐ Optional")  # Initial state
        grid_layout.addWidget(self.email_field_enabled, 2, 1, 1, 3)  # Span 3 columns
        
        # Row 3: Background image (Advanced)
        bg_label = QLabel("Background:")
        bg_label.setMinimumWidth(90)
        bg_label.setMaximumWidth(110)
        bg_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bg_label.setStyleSheet("""
            QLabel {
                font-size: 13px;
                font-weight: 600;
                color: #ffffff;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        bg_label.setToolTip("Custom background image for mobile interface")
        grid_layout.addWidget(bg_label, 3, 0)
        
        self.background_image_path = QLineEdit()
        self.background_image_path.setPlaceholderText("Select custom background image")
        self.background_image_path.setMinimumWidth(320)  # Ensure adequate minimum width
        self.background_image_path.setStyleSheet("""
            QLineEdit {
                background-color: #1e293b;
                border: 2px solid #10b981;
                color: #ffffff;
                font-size: 12px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                padding: 8px 12px;
                min-height: 24px;
            }
            QLineEdit::placeholder {
                color: #94a3b8;
            }
        """)
        grid_layout.addWidget(self.background_image_path, 3, 1, 1, 2)  # Span 2 columns
        
        self.select_bg_button = QPushButton("Browse")
        self.select_bg_button.setMinimumWidth(90)
        self.select_bg_button.setMaximumWidth(120)
        self.select_bg_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #8b5cf6, stop:1 #7c3aed);
                color: #ffffff;
                border: none;
                padding: 8px 10px;
                border-radius: 6px;
                font-weight: 600;
                font-size: 11px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                min-height: 28px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #a855f7, stop:1 #8b5cf6);
                transform: translateY(-1px);
            }
        """)
        self.select_bg_button.clicked.connect(self.select_background_image)
        grid_layout.addWidget(self.select_bg_button, 3, 3)

        # Row 4: Pre-session video enable (Advanced)
        video_enable_label = QLabel("Pre-Session Video:")
        video_enable_label.setMinimumWidth(90)
        video_enable_label.setMaximumWidth(110)
        video_enable_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        video_enable_label.setStyleSheet("""
            QLabel {
                font-size: 13px;
                font-weight: 600;
                color: #ffffff;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        video_enable_label.setToolTip("Require a 1920x1080 video to play on PC after signup")
        grid_layout.addWidget(video_enable_label, 4, 0)

        self.video_required_toggle = QPushButton("☐ Disabled")
        self.video_required_toggle.setCheckable(True)
        self.video_required_toggle.setChecked(False)
        self.video_required_toggle.setMinimumWidth(85)
        self.video_required_toggle.setMaximumWidth(130)
        self.video_required_toggle.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #475569, stop:1 #334155);
                color: #ffffff;
                border: 2px solid #64748b;
                padding: 8px 10px;
                border-radius: 6px;
                font-weight: 600;
                font-size: 11px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                min-height: 28px;
            }
            QPushButton:checked {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #34d399, stop:1 #10b981);
                color: #ffffff;
                border: 2px solid #10b981;
            }
            QPushButton:hover {
                border: 2px solid #10b981;
                color: #6ee7b7;
            }
        """)
        self.video_required_toggle.setToolTip("Play video on PC when a driver is set")
        self.video_required_toggle.toggled.connect(lambda checked: self.video_required_toggle.setText("⊠ Enabled" if checked else "☐ Disabled"))
        grid_layout.addWidget(self.video_required_toggle, 4, 1)

        self.video_path_input = QLineEdit()
        self.video_path_input.setPlaceholderText("Select MP4 video (1920x1080 recommended)")
        self.video_path_input.setMinimumWidth(320)
        self.video_path_input.setStyleSheet("""
            QLineEdit {
                background-color: #1e293b;
                border: 2px solid #10b981;
                color: #ffffff;
                font-size: 12px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                padding: 8px 12px;
                min-height: 24px;
            }
            QLineEdit::placeholder {
                color: #94a3b8;
            }
        """)
        grid_layout.addWidget(self.video_path_input, 4, 2)

        self.select_video_button = QPushButton("Browse")
        self.select_video_button.setMinimumWidth(90)
        self.select_video_button.setMaximumWidth(120)
        self.select_video_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #8b5cf6, stop:1 #7c3aed);
                color: #ffffff;
                border: none;
                padding: 8px 10px;
                border-radius: 6px;
                font-weight: 600;
                font-size: 11px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                min-height: 28px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #a855f7, stop:1 #8b5cf6);
                transform: translateY(-1px);
            }
        """)
        self.select_video_button.clicked.connect(self.select_video_file)
        grid_layout.addWidget(self.select_video_button, 4, 3)

        # Row 5: Waiting screen media (Advanced)
        waiting_media_label = QLabel("Waiting Screen:")
        waiting_media_label.setMinimumWidth(90)
        waiting_media_label.setMaximumWidth(110)
        waiting_media_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        waiting_media_label.setStyleSheet("""
            QLabel {
                font-size: 13px;
                font-weight: 600;
                color: #ffffff;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        waiting_media_label.setToolTip("Custom image or looping video for waiting screen")
        grid_layout.addWidget(waiting_media_label, 5, 0)

        self.waiting_media_path = QLineEdit()
        self.waiting_media_path.setPlaceholderText("Select JPG image or MP4 video for waiting screen")
        self.waiting_media_path.setMinimumWidth(320)
        self.waiting_media_path.setStyleSheet("""
            QLineEdit {
                background-color: #1e293b;
                border: 2px solid #10b981;
                color: #ffffff;
                font-size: 12px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                padding: 8px 12px;
                min-height: 24px;
            }
            QLineEdit::placeholder {
                color: #94a3b8;
            }
        """)
        grid_layout.addWidget(self.waiting_media_path, 5, 1, 1, 2)

        self.select_waiting_media_button = QPushButton("Browse")
        self.select_waiting_media_button.setMinimumWidth(90)
        self.select_waiting_media_button.setMaximumWidth(120)
        self.select_waiting_media_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #8b5cf6, stop:1 #7c3aed);
                color: #ffffff;
                border: none;
                padding: 8px 10px;
                border-radius: 6px;
                font-weight: 600;
                font-size: 11px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                min-height: 28px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #a855f7, stop:1 #8b5cf6);
                transform: translateY(-1px);
            }
        """)
        self.select_waiting_media_button.clicked.connect(self.select_waiting_media_file)
        grid_layout.addWidget(self.select_waiting_media_button, 5, 3)

        # Row 6: Rotation mode (Basic)
        rotation_mode_label = QLabel("Rotation Mode:")
        rotation_mode_label.setMinimumWidth(90)
        rotation_mode_label.setMaximumWidth(110)
        rotation_mode_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rotation_mode_label.setStyleSheet("""
            QLabel {
                font-size: 13px;
                font-weight: 600;
                color: #ffffff;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        rotation_mode_label.setToolTip("Switch between time-based and lap-based driver rotation")
        grid_layout.addWidget(rotation_mode_label, 6, 0)

        self.rotation_mode_selector = QComboBox()
        self.rotation_mode_selector.addItems(["Time", "Laps"])
        self.rotation_mode_selector.setToolTip("Choose rotation mode: by time or by laps")
        grid_layout.addWidget(self.rotation_mode_selector, 6, 1)

        # Laps input (only used in Laps mode)
        self.lap_count_label = QLabel("Laps:")
        self.lap_count_label.setMinimumWidth(45)
        self.lap_count_label.setMaximumWidth(65)
        self.lap_count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lap_count_label.setStyleSheet("""
            QLabel {
                font-size: 12px;
                color: #ffffff;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        grid_layout.addWidget(self.lap_count_label, 6, 2)

        self.lap_target_input = QLineEdit("10")
        self.lap_target_input.setMinimumWidth(60)
        self.lap_target_input.setMaximumWidth(90)
        self.lap_target_input.setStyleSheet("""
            QLineEdit {
                background-color: #1e293b;
                border: 2px solid #10b981;
                color: #ffffff;
                font-size: 13px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                font-weight: 600;
                padding: 8px 6px;
                min-height: 24px;
            }
        """)
        self.lap_target_input.setToolTip("Number of laps before driver swap alert")
        grid_layout.addWidget(self.lap_target_input, 6, 3)

        # Time input (only used in Time mode) - styled same as laps
        self.time_count_label = QLabel("Minutes:")
        self.time_count_label.setMinimumWidth(45)
        self.time_count_label.setMaximumWidth(65)
        self.time_count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.time_count_label.setStyleSheet("""
            QLabel {
                font-size: 12px;
                color: #ffffff;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        grid_layout.addWidget(self.time_count_label, 6, 2)

        self.time_target_input = QLineEdit("5")
        self.time_target_input.setMinimumWidth(60)
        self.time_target_input.setMaximumWidth(90)
        self.time_target_input.setStyleSheet("""
            QLineEdit {
                background-color: #1e293b;
                border: 2px solid #10b981;
                color: #ffffff;
                font-size: 13px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                font-weight: 600;
                padding: 8px 6px;
                min-height: 24px;
            }
        """)
        self.time_target_input.setToolTip("Duration in minutes before driver swap alert")
        grid_layout.addWidget(self.time_target_input, 6, 3)

        # React to mode changes (hide/show appropriate inputs)
        self.rotation_mode_selector.currentTextChanged.connect(self.on_rotation_mode_changed)
        self.on_rotation_mode_changed(self.rotation_mode_selector.currentText())
        
        # Current driver info (hidden)
        self.last_driver_email = ""
        # Video playback state
        self.pre_session_video_required = False
        self.pre_session_video_path = ""
        self.media_player = None
        self.video_widget = None
        
        # Overlay system
        self.waiting_overlay = WaitingOverlay()
        self.session_active = False  # Track if someone is actively racing
        self.timer_started = False  # Track if session timer has actually started
        
        # Set minimum size for the settings grid to ensure proper display
        settings_grid.setMinimumWidth(480)
        
        # Hide advanced rows by default
        self._advanced_rows = [
            (bg_label, self.background_image_path, self.select_bg_button),
            (video_enable_label, self.video_required_toggle, self.video_path_input, self.select_video_button),
            (waiting_media_label, self.waiting_media_path, self.select_waiting_media_button),
        ]
        self.set_advanced_visible(False)
        # Let the grid determine its own height based on contents to avoid crowding
        
        # Add grid to iPad box
        ipad_box_layout.addWidget(settings_grid)
        card_layout.addWidget(ipad_box)
        
        # Live Status Section
        status_section = QWidget()
        status_section.setObjectName("statusSection")
        status_section.setStyleSheet("""
            #statusSection {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                    stop:0 #581c87, stop:1 #7c2d92);
                border-radius: 10px;
                padding: 12px;
                margin: 6px 0;
                border: 1px solid #a855f7;
            }
        """)
        status_section_layout = QVBoxLayout(status_section)
        status_section_layout.setContentsMargins(15, 10, 15, 10)
        status_section_layout.setSpacing(8)
        
        # Status section header
        status_header = QLabel("📊 Live Racing Status")
        status_header.setStyleSheet("""
            QLabel {
                font-weight: 700;
                font-size: 14px;
                color: #ffffff;
                margin-bottom: 4px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        status_section_layout.addWidget(status_header)
        
        # Game connection status
        game_status_row = QHBoxLayout()
        status_title = QLabel("Game Connection:")
        status_title.setStyleSheet("""
            QLabel {
                font-weight: 600;
                color: #d8b4fe;
                font-size: 13px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        status_title.setFixedWidth(120)
        game_status_row.addWidget(status_title)
        
        self.game_status = QLabel("Initializing...")
        self.game_status.setWordWrap(True)
        self.game_status.setStyleSheet("""
            QLabel {
                color: #e2e8f0;
                font-size: 13px;
                padding: 6px 12px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #1e293b, stop:1 #0f172a);
                border-radius: 6px;
                border: 1px solid #475569;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        game_status_row.addWidget(self.game_status, 1)
        status_section_layout.addLayout(game_status_row)
        
        # Last lap time display
        lap_time_row = QHBoxLayout()
        lap_time_title = QLabel("Last Lap Time:")
        lap_time_title.setStyleSheet("""
            QLabel {
                font-weight: 600;
                color: #d8b4fe;
                font-size: 13px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        lap_time_title.setFixedWidth(120)
        lap_time_row.addWidget(lap_time_title)
        
        self.status_label = QLabel("Waiting for lap completion...")
        self.status_label.setStyleSheet("""
            QLabel {
                color: #e2e8f0;
                font-size: 16px;
                font-weight: 700;
                padding: 8px 16px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #1e293b, stop:1 #0f172a);
                border-radius: 8px;
                border: 2px solid #475569;
                font-family: 'Consolas', 'Monaco', monospace;
            }
        """)
        self.status_label.setToolTip("Shows the most recent lap time sent to the leaderboard")
        lap_time_row.addWidget(self.status_label, 1)
        status_section_layout.addLayout(lap_time_row)
        
        # Add waiting screen control button
        waiting_screen_row = QHBoxLayout()
        waiting_screen_label = QLabel("Waiting Screen:")
        waiting_screen_label.setStyleSheet("""
            QLabel {
                color: #d8b4fe;
                font-weight: 600;
                font-size: 13px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        waiting_screen_label.setFixedWidth(120)
        waiting_screen_row.addWidget(waiting_screen_label)
        
        self.toggle_waiting_screen_btn = QPushButton("Show Waiting Screen")
        self.toggle_waiting_screen_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #3b82f6, stop:1 #2563eb);
                color: #ffffff;
                border: none;
                padding: 8px 16px;
                border-radius: 6px;
                font-weight: 600;
                font-size: 13px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #2563eb, stop:1 #1d4ed8);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #1d4ed8, stop:1 #1e40af);
            }
        """)
        self.toggle_waiting_screen_btn.clicked.connect(self.toggle_waiting_screen)
        self.toggle_waiting_screen_btn.setToolTip("Show/hide the full-screen waiting overlay")
        waiting_screen_row.addWidget(self.toggle_waiting_screen_btn, 1)
        status_section_layout.addLayout(waiting_screen_row)
        
        card_layout.addWidget(status_section)
        
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
                    # Log cleanup errors but don't fail
                    print(f"Warning during monitor cleanup: {e}")
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
        
        # Check if session should end (time or lap limits reached)
        session_ended = False
        
        # Handle lap-based rotation if enabled
        if self.lap_rotation_active:
            # Start counting from first valid lap after out-lap
            if not self.lap_count_started:
                # First crossing after enabling: this was the out-lap; start counting now
                self.lap_count_started = True
                self.lap_rotation_count = 0
                print("Lap counting started after out-lap")
            else:
                self.lap_rotation_count += 1
                print(f"Lap-based rotation progress: {self.lap_rotation_count}/{self.lap_rotation_target}")
                if self.lap_rotation_count >= self.lap_rotation_target:
                    # Session ended - trigger overlay
                    self.show_success("🏁 Target laps reached! Please change driver.")
                    session_ended = True
                    # Reset for next stint, but keep mode active
                    self.lap_rotation_count = 0
                    self.lap_count_started = False
        
        # Check time-based rotation
        elif hasattr(self, 'rotation_mode_selector') and self.rotation_mode_selector.currentText() == "Time":
            if hasattr(self, 'timer_end_timestamp'):
                current_time = int(time.time())
                if current_time >= self.timer_end_timestamp:
                    # Time expired - session ended
                    self.show_success("⏰ Driver time expired! Please change driver.")
                    session_ended = True
        
        # End session if limits reached
        if session_ended and self.session_active:
            print(f"🏁 Session ending due to limits reached")
            self.end_session()
        
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
            
            # Additional validation for manual entries
            if lap_time is not None and not validate_lap_time(lap_time, "Manual Entry"):
                self.show_error(f"Invalid lap time: {lap_time:.3f}s. Must be between 15s and 10 minutes.")
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
                
            # Get the server URL from the saved value, not from the UI
            server_url = self.current_server_url
            
            # Fallback to UI value if current_server_url isn't set
            if not server_url:
                server_url = self.server_url.text().strip()
                print("Using URL from UI as no saved URL exists yet")
            
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
                        color: #10b981;
                        font-size: 24px;
                        font-weight: 700;
                        padding: 8px 16px;
                        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                            stop:0 #064e3b, stop:1 #065f46);
                        border-radius: 8px;
                        border: 2px solid #10b981;
                        font-family: 'Consolas', 'Monaco', monospace;
                        text-shadow: 0px 1px 2px rgba(0, 0, 0, 0.5);
                    }
                """)
            except:
                self.status_label.setText(message)
                self.status_label.setStyleSheet("""
                    QLabel {
                        color: #6ee7b7;
                        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                            stop:0 #064e3b, stop:1 #065f46);
                        padding: 10px;
                        border-radius: 8px;
                        border: 1px solid #10b981;
                        font-size: 13px;
                        font-family: 'Segoe UI', 'Arial', sans-serif;
                    }
                """)
        else:
            self.status_label.setText(message)
            self.status_label.setStyleSheet("""
                QLabel {
                    color: #6ee7b7;
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                        stop:0 #064e3b, stop:1 #065f46);
                    padding: 10px;
                    border-radius: 8px;
                    border: 1px solid #10b981;
                    font-size: 13px;
                    font-family: 'Segoe UI', 'Arial', sans-serif;
                }
            """)
            
    def show_error(self, message):
        self.status_label.setText(message)
        self.status_label.setStyleSheet("""
            QLabel {
                color: #fca5a5;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #7f1d1d, stop:1 #991b1b);
                padding: 10px;
                border-radius: 8px;
                border: 1px solid #ef4444;
                font-size: 13px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                font-weight: 600;
            }
        """)
        
    def update_game_status(self, message, color):
        # Add emoji indicators based on status with modern dark styling
        if "Connected" in message:
            message = "🟢 " + message
            self.game_status.setStyleSheet("""
                QLabel {
                    color: #6ee7b7;
                    font-size: 13px;
                    padding: 6px 12px;
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                        stop:0 #064e3b, stop:1 #065f46);
                    border-radius: 6px;
                    border: 1px solid #10b981;
                    font-family: 'Segoe UI', 'Arial', sans-serif;
                    font-weight: 600;
                }
            """)
        elif "Attempting" in message or "Lost connection" in message or "Waiting" in message:
            message = "🟡 " + message
            self.game_status.setStyleSheet("""
                QLabel {
                    color: #fbbf24;
                    font-size: 13px;
                    padding: 6px 12px;
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                        stop:0 #78350f, stop:1 #92400e);
                    border-radius: 6px;
                    border: 1px solid #f59e0b;
                    font-family: 'Segoe UI', 'Arial', sans-serif;
                    font-weight: 600;
                }
            """)
        elif "Error" in message:
            message = "🔴 " + message
            self.game_status.setStyleSheet("""
                QLabel {
                    color: #fca5a5;
                    font-size: 13px;
                    padding: 6px 12px;
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                        stop:0 #7f1d1d, stop:1 #991b1b);
                    border-radius: 6px;
                    border: 1px solid #ef4444;
                    font-family: 'Segoe UI', 'Arial', sans-serif;
                    font-weight: 600;
                }
            """)
        else:
            # Default styling for other messages
            self.game_status.setStyleSheet("""
                QLabel {
                    color: #e2e8f0;
                    font-size: 13px;
                    padding: 6px 12px;
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                        stop:0 #1e293b, stop:1 #0f172a);
                    border-radius: 6px;
                    border: 1px solid #475569;
                    font-family: 'Segoe UI', 'Arial', sans-serif;
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
        
        # Check if video is required and configured (read live UI state to avoid needing Apply)
        try:
            video_enabled = False
            video_file_path = ""
            if hasattr(self, 'video_required_toggle') and hasattr(self, 'video_path_input'):
                video_enabled = bool(self.video_required_toggle.isChecked())
                video_file_path = self.video_path_input.text().strip()
            # Fallback to any previously stored path/setting
            if not video_enabled:
                video_enabled = bool(getattr(self, 'pre_session_video_required', False))
            if not video_file_path:
                video_file_path = getattr(self, 'pre_session_video_path', "")

            if video_enabled and video_file_path and os.path.exists(video_file_path):
                # Play video - this will hide waiting screen and show video
                self.play_pre_session_video(video_file_path)
            else:
                # No video - just hide waiting screen and start session
                self.hide_waiting_screen()
                # Start timer immediately since no video to wait for
                self.start_session_timer()
        except Exception:
            # If anything goes wrong, don't block the session
            self.hide_waiting_screen()
            # Start timer since no video played
            self.start_session_timer()

        # Don't start timer yet - wait until after pre-session video ends
        # Timer will be started in start_session_timer() method
        self.timer_started = False  # Reset timer state for new driver
        
        # Reset lap-based counters on new driver
        if hasattr(self, 'rotation_mode_selector') and self.rotation_mode_selector.currentText() == "Laps":
            self.lap_rotation_count = 0
            self.lap_count_started = False

    def show_waiting_screen(self):
        """Show the waiting overlay covering the game"""
        # Load custom media if configured
        if hasattr(self, 'waiting_media_path') and self.waiting_media_path.text().strip():
            media_path = self.waiting_media_path.text().strip()
            if os.path.exists(media_path):
                self.waiting_overlay.set_media(media_path)
            else:
                # Media path set but file doesn't exist - show default
                self.waiting_overlay.show_default_content()
        else:
            # No custom media configured - show default content
            self.waiting_overlay.show_default_content()
        
        self.waiting_overlay.show()
        self.session_active = False
        # Update button text
        if hasattr(self, 'toggle_waiting_screen_btn'):
            self.toggle_waiting_screen_btn.setText("Hide Waiting Screen")
        
    def hide_waiting_screen(self):
        """Hide the waiting overlay to reveal the game"""
        self.waiting_overlay.hide()
        self.session_active = True
        # Update button text
        if hasattr(self, 'toggle_waiting_screen_btn'):
            self.toggle_waiting_screen_btn.setText("Show Waiting Screen")
        
    def end_session(self):
        """End the current session and return to waiting screen"""
        print("🔚 end_session() called - showing session complete screen")
        self.session_active = False
        self.timer_started = False  # Reset timer state
        # Show a brief "session complete" interstitial, then return to waiting
        try:
            self.waiting_overlay.show_session_complete()
            self.waiting_overlay.show()
            print("✅ Session complete screen should now be visible")
        except Exception as e:
            print(f"❌ Error showing session complete screen: {e}")
            # Fallback to waiting if anything goes wrong
            self.waiting_overlay.show_default_content()
            self.waiting_overlay.show()
        
        # After 4 seconds, go to waiting screen (with custom media if set)
        def to_waiting():
            if hasattr(self, 'waiting_media_path') and self.waiting_media_path.text().strip():
                media_path = self.waiting_media_path.text().strip()
                if os.path.exists(media_path):
                    self.waiting_overlay.set_media(media_path)
                else:
                    self.waiting_overlay.show_default_content()
            else:
                self.waiting_overlay.show_default_content()
            self.waiting_overlay.show()
        
        QTimer.singleShot(4000, to_waiting)
        # Update button text
        if hasattr(self, 'toggle_waiting_screen_btn'):
            self.toggle_waiting_screen_btn.setText("Hide Waiting Screen")

    def play_pre_session_video(self, path):
        # Hide waiting screen first
        self.waiting_overlay.hide()
        
        # Initialize player components once
        if self.media_player is None:
            self.media_player = QMediaPlayer(self)
        if self.video_widget is None:
            self.video_widget = QVideoWidget()
            self.video_widget.setWindowTitle("Pre-Session Video")
            # Force 1920x1080 size; center is OS-managed
            self.video_widget.resize(1920, 1080)
            # Show on top
            self.video_widget.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            self.video_widget.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        # Create audio output each time to ensure sound
        audio_output = QAudioOutput(self)
        audio_output.setVolume(1.0)
        self.media_player.setAudioOutput(audio_output)
        self.media_player.setVideoOutput(self.video_widget)

        # Load and play
        self.media_player.setSource(QUrl.fromLocalFile(path))
        self.video_widget.show()
        self.media_player.play()

        # When video ends, hide video and allow game to be visible (no overlay)
        def handle_finished():
            try:
                self.media_player.stop()
            except Exception:
                pass
            self.video_widget.hide()
            # Game is now visible - session is active
            self.session_active = True
            # Start the session timer now that video is done
            self.start_session_timer()

        # Connect signals once; disconnect previous if reconnected
        try:
            self.media_player.mediaStatusChanged.disconnect()
        except Exception:
            pass

        def on_status_changed(status):
            # QMediaPlayer.MediaStatus enum; 6 is EndOfMedia in Qt6
            from PyQt6.QtMultimedia import QMediaPlayer as _QMP
            if status == _QMP.MediaStatus.EndOfMedia:
                handle_finished()

        self.media_player.mediaStatusChanged.connect(on_status_changed)

    def toggle_waiting_screen(self):
        """Toggle the waiting screen on/off"""
        if self.waiting_overlay.isVisible():
            self.hide_waiting_screen()
        else:
            self.show_waiting_screen()

    def start_session_timer(self):
        """Start the session timer (called after video ends or if no video)"""
        # In Time mode, start timer based on the current input
        if hasattr(self, 'rotation_mode_selector') and self.rotation_mode_selector.currentText() == "Time":
            try:
                minutes = max(1, int(self.time_target_input.text()))
                current_timestamp = int(time.time())
                self.timer_end_timestamp = current_timestamp + (minutes * 60)
                self.timer_started = True  # Mark timer as actually started
                print(f"⏱️ Session timer started: {minutes} minutes (ends at {self.timer_end_timestamp})")
            except Exception:
                self.time_target_input.setText("5")
                self.timer_end_timestamp = int(time.time()) + (5 * 60)
                self.timer_started = True  # Mark timer as actually started
                print(f"⏱️ Session timer started: 5 minutes (fallback)")

    def check_session_timeout(self):
        """Periodically check if the session has timed out (even without lap completion)"""
        if not self.session_active:
            return
            
        # Only check time-based rotation
        if hasattr(self, 'rotation_mode_selector') and self.rotation_mode_selector.currentText() == "Time":
            if hasattr(self, 'timer_end_timestamp') and self.timer_end_timestamp > 0:
                current_time = int(time.time())
                if current_time >= self.timer_end_timestamp:
                    print(f"⏰ Session timeout detected: {current_time} >= {self.timer_end_timestamp}")
                    self.show_success("⏰ Driver time expired! Please change driver.")
                    self.end_session()

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

    def select_video_file(self):
        from PyQt6.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Pre-Session Video", "", "Video Files (*.mp4 *.mov *.mkv *.avi)"
        )
        if file_path:
            self.video_path_input.setText(file_path)
            self.pre_session_video_path = file_path
            # Persist into resources if desired (keep as external path to support large files)
            self.show_success("Pre-session video selected")

    def select_waiting_media_file(self):
        from PyQt6.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Waiting Screen Media", "", 
            "Media Files (*.jpg *.jpeg *.png *.mp4 *.mov *.mkv *.avi);;Image Files (*.jpg *.jpeg *.png);;Video Files (*.mp4 *.mov *.mkv *.avi)"
        )
        if file_path:
            self.waiting_media_path.setText(file_path)
            # Update the waiting overlay with new media
            if hasattr(self, 'waiting_overlay'):
                self.waiting_overlay.set_media(file_path)
            self.show_success("Waiting screen media selected")
            
    def on_update_driver_settings(self):
        """Update driver-related settings (timer, email field, background)"""
        # Process email field setting
        email_required = self.email_field_enabled.isChecked()
        print(f"Email requirement updated: {'Enabled' if email_required else 'Disabled'}")
        
        # Process rotation mode first, and drive timer/laps from it
        timer_enabled = False
        try:
            # We'll compute timer from time_target_input when in Time mode
            pass
        except Exception:
            pass
        
        # Process rotation mode
        mode = self.rotation_mode_selector.currentText()
        if mode == "Laps":
            try:
                self.lap_rotation_target = max(1, int(self.lap_target_input.text()))
                self.lap_rotation_active = True
                self.lap_rotation_count = 0
                self.lap_count_started = False
                print(f"Lap-based rotation enabled: target {self.lap_rotation_target} laps (out-lap ignored)")
            except ValueError:
                self.show_error("Invalid lap count. Please enter a whole number.")
                self.lap_target_input.setText("10")
                self.lap_rotation_active = False
        else:
            # Time mode
            self.lap_rotation_active = False
            try:
                timer_minutes = max(1, int(self.time_target_input.text()))
                current_timestamp = int(time.time())
                self.timer_end_timestamp = current_timestamp + (timer_minutes * 60)
                print(f"Time-based rotation enabled: {timer_minutes} minutes")
            except ValueError:
                self.show_error("Invalid minutes. Please enter a whole number.")
                self.time_target_input.setText("5")

        # Process pre-session video toggle
        self.pre_session_video_required = self.video_required_toggle.isChecked()
        if self.pre_session_video_required and not self.video_path_input.text():
            # If video required but no path, disable requirement and warn
            self.pre_session_video_required = False
            self.video_required_toggle.setChecked(False)
            self.show_error("Please select a video file to enable pre-session video")

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
        message = "📱 iPad interface settings updated successfully"
        details = []
        if email_required:
            details.append("Email required")
        if timer_enabled:
            details.append(f"Timer: {timer_minutes}min")
        if self.background_image_path.text():
            details.append("Custom background")
        
        if self.pre_session_video_required:
            details.append("Pre-session video ON")
        if details:
            message += f" ({', '.join(details)})"
        
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

    def on_rotation_mode_changed(self, mode):
        """Toggle visibility of time vs lap inputs based on rotation mode"""
        is_lap_mode = (mode == "Laps")
        # Always hide legacy timer row; use Rotation Mode row instead
        self.driver_timer_enabled.setVisible(False)
        self.driver_timer_minutes.setVisible(False)
        self.timer_label.setVisible(False)
        self.minutes_label.setVisible(False)
        # Lap inputs
        self.lap_count_label.setVisible(is_lap_mode)
        self.lap_target_input.setVisible(is_lap_mode)
        # Time inputs on Rotation row
        self.time_count_label.setVisible(not is_lap_mode)
        self.time_target_input.setVisible(not is_lap_mode)

    def set_advanced_visible(self, visible: bool):
        """Show/hide advanced settings rows to keep the UI compact"""
        for widgets in getattr(self, '_advanced_rows', []):
            for w in widgets:
                if w:
                    w.setVisible(visible)

    def on_toggle_advanced(self, checked: bool):
        self.advanced_toggle_btn.setText("Hide Advanced" if checked else "Show Advanced")
        self.set_advanced_visible(checked)

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
            
            # Accept any response as valid - the server is responding
            # 501 is expected for some servers, but others might return 200, 404, etc.
            print(f"Server responded with status code: {response.status_code}")
            
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
            # Still save the URL - user may want to use it anyway
            self.current_server_url = url
            print("Timeout occurred, but URL saved for future attempts")
            # After 2 seconds, revert button state
            QTimer.singleShot(2000, lambda: self._reset_button_state(test_button))
            
        except requests.exceptions.ConnectionError as e:
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
            print(f"Connection error: {str(e)}")
            # Still save the URL - user may want to use it anyway
            self.current_server_url = url
            print("Connection error occurred, but URL saved for future attempts")
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
            # Still save the URL - user may want to use it anyway
            self.current_server_url = url
            print("Error occurred, but URL saved for future attempts")
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
                padding: 4px 8px;
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

