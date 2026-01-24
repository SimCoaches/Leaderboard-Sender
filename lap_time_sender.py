import sys
import json
import requests
import time
import threading
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
import ctypes
import mmap
import irsdk
import shutil
from pathlib import Path
from datetime import datetime
from abc import ABC, abstractmethod
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from zeroconf import ServiceInfo, Zeroconf
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
    
    # Very permissive lap time bounds to support all track types
    # From short ovals (~10s) to ultra-endurance events (60+ minutes)
    MIN_LAP_TIME = 5.0     # 5 seconds (minimum realistic lap)
    MAX_LAP_TIME = 3600.0  # 60 minutes (ultra-endurance events like King of Hammers)
    
    if lap_time < MIN_LAP_TIME:
        print(f"WARNING: Lap time {lap_time:.3f}s too fast - likely invalid {context}")
        return False
    
    if lap_time > MAX_LAP_TIME:
        print(f"WARNING: Lap time {lap_time:.3f}s too slow - likely invalid {context}")
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
                print(f"WARNING: Skipping lap - driver is in pit")
                return False
            
            if hasattr(graphics_data, 'isInPitLane') and graphics_data.isInPitLane:
                print(f"WARNING: Skipping lap - driver is in pit lane")
                return False
            
            # Only accept race or practice sessions (status 2 = Live/Racing)
            if hasattr(graphics_data, 'status') and graphics_data.status != 2:
                print(f"WARNING: Skipping lap - not in active session (status: {graphics_data.status})")
                return False
        
        # For iRacing, we could add similar checks here in the future
        # when we have access to pit/session status from iRacing SDK
        
        return True
        
    except Exception as e:
        print(f"Error validating racing context: {e}")
        return True  # Default to accepting if we can't validate

def get_local_ip():
    """Get local IP address with improved fallback handling for offline networks"""
    try:
        # Method 1: Create UDP socket to determine routing
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass
    
    try:
        # Method 2: Try common gateway
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(('192.168.1.1', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass
    
    # Method 3: Enumerate network interfaces (offline-safe)
    try:
        hostname = socket.gethostname()
        addresses = socket.getaddrinfo(hostname, None, socket.AF_INET)
        for addr in addresses:
            ip = addr[4][0]
            if ip and not ip.startswith('127.'):
                return ip
    except Exception:
        pass
    
    return '127.0.0.1'  # Final fallback

def configure_firewall():
    """Add Windows Firewall rules to allow inbound connections.
    - Port 5001 TCP: Web interface for iPads and other simulators
    - Port 5003 UDP: Simulator discovery broadcast
    """
    import subprocess
    
    rules = [
        ("Lap Time Sender - Web Interface", "TCP", "5001"),
        ("Lap Time Sender - Discovery", "UDP", "5003"),
    ]
    
    for rule_name, protocol, port in rules:
        try:
            # Check if rule already exists
            check = subprocess.run(
                ['netsh', 'advfirewall', 'firewall', 'show', 'rule', f'name={rule_name}'],
                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW
            )
            if 'No rules match' in check.stdout or check.returncode != 0:
                # Add the firewall rule
                result = subprocess.run([
                    'netsh', 'advfirewall', 'firewall', 'add', 'rule',
                    f'name={rule_name}', 'dir=in', 'action=allow',
                    f'protocol={protocol}', f'localport={port}'
                ], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode == 0:
                    print(f"[OK] Firewall rule added for port {port} ({protocol})")
                else:
                    print(f"WARNING: Could not add firewall rule for port {port}: {result.stderr}")
            else:
                print(f"[OK] Firewall rule already exists for port {port} ({protocol})")
        except Exception as e:
            print(f"WARNING: Could not configure firewall for port {port}: {e}")

def get_config_path():
    """Get the path to the simulators config file."""
    if getattr(sys, 'frozen', False):
        # Running as compiled executable
        base_path = Path(sys.executable).parent
    else:
        # Running from script
        base_path = Path(__file__).parent
    return base_path / 'simulators.json'

def load_known_simulators():
    """Load known simulators from config file."""
    config_path = get_config_path()
    try:
        if config_path.exists():
            with open(config_path, 'r') as f:
                data = json.load(f)
                return data.get('known_simulators', [])
    except Exception as e:
        print(f"WARNING: Could not load simulators config: {e}")
    return []

def save_known_simulators(simulators):
    """Save known simulators to config file."""
    config_path = get_config_path()
    try:
        with open(config_path, 'w') as f:
            json.dump({'known_simulators': simulators}, f, indent=2)
        print(f"[OK] Saved {len(simulators)} known simulators to config")
        return True
    except Exception as e:
        print(f"WARNING: Could not save simulators config: {e}")
        return False

def add_known_simulator(ip, name=None):
    """Add a simulator to the known list."""
    simulators = load_known_simulators()
    # Check if already exists
    for sim in simulators:
        if sim.get('ip') == ip:
            return False  # Already exists
    simulators.append({
        'ip': ip,
        'name': name or f'Sim ({ip})',
        'port': 5001
    })
    return save_known_simulators(simulators)

def remove_known_simulator(ip):
    """Remove a simulator from the known list."""
    simulators = load_known_simulators()
    simulators = [s for s in simulators if s.get('ip') != ip]
    return save_known_simulators(simulators)

# Create Flask app with no output
app = Flask(__name__)
app.logger.disabled = True
import logging
log = logging.getLogger('werkzeug')
log.disabled = True
CORS(app)  # Enable CORS for all routes

# Simple kiosk HTML page - driver registration only (no admin features)
HTML_CONTENT = r"""
<!DOCTYPE html>
<html>
<head>
    <title>Driver Registration</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body {
            width: 100%; height: 100vh; overflow: hidden;
            position: fixed; top: 0; left: 0;
            background-color: #1e1e1e;
        }
        body {
            font-family: 'Segoe UI', Arial, sans-serif;
            display: flex; justify-content: center; align-items: flex-start;
            padding-top: 10vh;
            background-repeat: no-repeat; background-position: center;
            background-size: cover; background-attachment: fixed;
        }
        .container {
            width: 90%; max-width: 500px;
            background-color: #252526; padding: 25px;
            border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.4);
            border: 1px solid #3e3e42; z-index: 10;
        }
        h1 { color: #cccccc; margin-bottom: 20px; text-align: center; }
        .input-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 5px; color: #9d9d9d; font-weight: bold; }
        input[type="text"], input[type="email"] {
            width: 100%; padding: 12px; font-size: 18px;
            border: 1px solid #3e3e42; border-radius: 4px;
            background-color: #2d2d30; color: #cccccc;
        }
        input:focus { border-color: #007acc; outline: none; background-color: #1e1e1e; }
        button {
            background-color: #007acc; color: white;
            padding: 14px 20px; border: none; border-radius: 4px;
            cursor: pointer; font-size: 18px; width: 100%; font-weight: bold;
        }
        button:hover { background-color: #0098ff; }
        .status { margin-top: 20px; padding: 10px; border-radius: 4px; text-align: center; }
        .success { background-color: rgba(78, 201, 176, 0.2); color: #4ec9b0; border: 1px solid #4ec9b0; }
        .error { background-color: rgba(244, 135, 113, 0.2); color: #f48771; border: 1px solid #f48771; }
        .current-driver {
            background-color: #2d2d30; border-left: 4px solid #007acc;
            padding: 10px 15px; margin-bottom: 20px; border-radius: 4px;
            display: flex; justify-content: space-between; align-items: center;
        }
        .current-driver p { margin: 0; font-weight: bold; color: #cccccc; }
        .current-driver span { color: #007acc; font-weight: bold; }
        .current-driver-title { font-size: 0.85em; color: #9d9d9d; margin-bottom: 3px; }
        #timerStatus {
            font-size: 28px; font-weight: bold; padding: 18px 24px;
            margin: 20px 0; border-radius: 12px; text-align: center;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        }
        #timerStatus .timer-text { display: block; font-size: 36px; margin: 8px 0; letter-spacing: 2px; }
        #timerStatus .timer-label { display: block; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; opacity: 0.9; }
        #timerStatus.timer-active { background-color: #2d2d30; color: #4ec9b0; border: 2px solid #4ec9b0; }
        #timerStatus.timer-warning { background-color: #2d2d30; color: #ce9178; border: 2px solid #ce9178; animation: pulse 1.5s infinite; }
        #timerStatus.timer-expired { background-color: #2d2d30; color: #f48771; border: 2px solid #f48771; animation: timerFlash 0.8s infinite; }
        #timerStatus.laps-active { background-color: #2d2d30; color: #007acc; border: 2px solid #007acc; }
        #timerStatus.laps-outlap { background-color: #2d2d30; color: #9d9d9d; border: 2px solid #454545; }
        .progress-bar-container { width: 100%; height: 12px; background-color: #3e3e42; border-radius: 6px; margin-top: 12px; overflow: hidden; }
        .progress-bar-fill { height: 100%; background-color: #007acc; border-radius: 6px; transition: width 0.5s ease; }
        @keyframes pulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.02); } }
        @keyframes timerFlash { 0%, 100% { opacity: 1; } 50% { opacity: 0.7; } }
        .logo-container { text-align: center; margin-top: 20px; padding-top: 15px; border-top: 1px solid #3e3e42; }
        .logo-container img { max-width: 100%; height: auto; max-height: 120px; }
        .admin-link { text-align: center; margin-top: 15px; }
        .admin-link a { color: #6e6e6e; font-size: 11px; text-decoration: none; }
        .admin-link a:hover { color: #9d9d9d; text-decoration: underline; }
        @media (max-width: 480px) {
            #timerStatus { font-size: 22px; padding: 14px 18px; }
            #timerStatus .timer-text { font-size: 28px; }
        }
        .timer-alert { animation: flash 1s infinite; }
        @keyframes flash { 0%, 100% { background-color: #252526; } 50% { background-color: rgba(244, 135, 113, 0.4); } }
    </style>
</head>
<body>
    <div class="container" id="container">
        <h1>🏁 Driver Registration</h1>
        <p style="text-align: center; color: #9d9d9d; margin-bottom: 20px; font-style: italic;">
            Set your driver name for lap time tracking
        </p>

        <div class="current-driver" id="currentDriverBox" style="display:none;">
            <div>
                <p class="current-driver-title">🏆 Active Driver:</p>
                <p><span id="currentDriverName">None</span></p>
            </div>
            <div style="font-size: 0.8em; color: #9d9d9d;">Ready for racing!</div>
        </div>

        <div class="input-group">
            <label for="driverName">👤 Driver Name:</label>
            <input type="text" id="driverName" placeholder="Enter your name" required>
            <small style="color: #6e6e6e; font-style: italic;">This will appear on the leaderboard</small>
        </div>
        <div class="input-group" id="emailGroup" style="display: none;">
            <label for="driverEmail">📧 Email Address: <span style="color: #f48771;">*</span></label>
            <input type="email" id="driverEmail" placeholder="Enter email address (required)" required>
            <small style="color: #6e6e6e; font-style: italic;">Required for driver communication</small>
        </div>
        <button onclick="updateDriver()">🏁 Register as Active Driver</button>
        <div id="status" class="status" style="display: none;"></div>
        <div id="timerStatus" style="display: none; margin-top: 15px;"></div>

        <div class="logo-container">
            <img src="/logo" alt="Company Logo" id="companyLogo">
        </div>
        <div class="admin-link">
            <a href="/admin">Staff Admin Portal</a>
        </div>
    </div>

    <script>
        function updateDriver() {
            const driverName = document.getElementById('driverName').value.trim();
            const driverEmail = document.getElementById('driverEmail')?.value?.trim() || '';
            const statusDiv = document.getElementById('status');
            const emailRequired = document.getElementById('emailGroup').style.display !== 'none';

            if (!driverName) {
                statusDiv.textContent = 'Please enter your name';
                statusDiv.className = 'status error';
                statusDiv.style.display = 'block';
                return;
            }
            if (emailRequired && !driverEmail) {
                statusDiv.textContent = 'Please enter your email address';
                statusDiv.className = 'status error';
                statusDiv.style.display = 'block';
                return;
            }

            fetch('/set_driver', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ driver_name: driverName, driver_email: driverEmail })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    statusDiv.textContent = '✅ Registered as ' + driverName + '!';
                    statusDiv.className = 'status success';
                    document.getElementById('currentDriverName').textContent = driverName;
                    document.getElementById('currentDriverBox').style.display = 'flex';
                } else {
                    statusDiv.textContent = '❌ Failed to register';
                    statusDiv.className = 'status error';
                }
                statusDiv.style.display = 'block';
            })
            .catch(error => {
                statusDiv.textContent = '❌ Connection error';
                statusDiv.className = 'status error';
                statusDiv.style.display = 'block';
            });
        }

        function clearTimerClasses() {
            const el = document.getElementById('timerStatus');
            el.classList.remove('timer-active', 'timer-warning', 'timer-expired', 'laps-active', 'laps-outlap');
        }

        function checkTimerAndSettings() {
            fetch('/get_settings')
                .then(response => response.json())
                .then(data => {
                    // Email field
                    document.getElementById('emailGroup').style.display = data.require_email ? 'block' : 'none';
                    // Background
                    if (data.background_image) {
                        document.body.style.backgroundImage = 'url("/backgrounds/' + data.background_image + '")';
                    }
                    // Timer/Laps display
                    const timerStatus = document.getElementById('timerStatus');
                    const container = document.getElementById('container');

                    if (data.laps_active && data.laps_progress) {
                        clearTimerClasses();
                        timerStatus.style.display = 'block';
                        const { count, target } = data.laps_progress;
                        const progress = target > 0 ? (count / target) * 100 : 0;

                        if (count === 0) {
                            timerStatus.classList.add('laps-outlap');
                            timerStatus.innerHTML = '<span class="timer-label">Out Lap</span><span class="timer-text">🏁 Warming Up...</span>';
                        } else if (count >= target && target > 0) {
                            timerStatus.classList.add('timer-expired');
                            container.classList.add('timer-alert');
                            timerStatus.innerHTML = '<span class="timer-text">🚨 LAPS COMPLETE 🚨</span><span class="timer-label">Please Change Driver</span>';
                        } else {
                            timerStatus.classList.add('laps-active');
                            container.classList.remove('timer-alert');
                            timerStatus.innerHTML = '<span class="timer-label">Laps Completed</span><span class="timer-text">🏁 ' + count + ' / ' + target + '</span><div class="progress-bar-container"><div class="progress-bar-fill" style="width: ' + progress + '%"></div></div>';
                        }
                    } else if (data.timer_active) {
                        clearTimerClasses();
                        timerStatus.style.display = 'block';
                        const seconds = data.timer_seconds_left || 0;
                        const mins = Math.floor(seconds / 60);
                        const secs = seconds % 60;
                        const formattedTime = mins + ':' + secs.toString().padStart(2, '0');

                        if (seconds === 0) {
                            timerStatus.classList.add('timer-expired');
                            container.classList.add('timer-alert');
                            timerStatus.innerHTML = '<span class="timer-text">🚨 TIME EXPIRED 🚨</span><span class="timer-label">Please Change Driver</span>';
                        } else if (seconds <= 60) {
                            timerStatus.classList.add('timer-warning');
                            container.classList.remove('timer-alert');
                            timerStatus.innerHTML = '<span class="timer-label">⚠️ Time Running Low!</span><span class="timer-text">⏱️ ' + formattedTime + '</span>';
                        } else {
                            timerStatus.classList.add('timer-active');
                            container.classList.remove('timer-alert');
                            timerStatus.innerHTML = '<span class="timer-label">Time Remaining</span><span class="timer-text">⏱️ ' + formattedTime + '</span>';
                        }
                    } else {
                        clearTimerClasses();
                        timerStatus.style.display = 'none';
                        container.classList.remove('timer-alert');
                    }
                })
                .catch(error => console.error('Error:', error));
        }

        // Load current driver
        fetch('/get_driver')
            .then(response => response.json())
            .then(data => {
                if (data.driver_name) {
                    document.getElementById('driverName').value = data.driver_name;
                    document.getElementById('currentDriverName').textContent = data.driver_name;
                    document.getElementById('currentDriverBox').style.display = 'flex';
                }
            })
            .catch(error => console.error('Error:', error));

        checkTimerAndSettings();
        setInterval(checkTimerAndSettings, 1000);
    </script>
</body>
</html>
"""

# Full admin HTML page - includes manual entry, simulator tabs, all features
ADMIN_HTML_CONTENT = r"""
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
        
        /* Force html and body to full height - allow scroll for queue panel */
        html, body {
            width: 100%;
            min-height: 100vh;
            overflow-x: hidden;
            overflow-y: auto;
            background-color: #1e1e1e;
        }

        /* Set body as a flex container with background */
        body {
            font-family: 'Segoe UI', Arial, sans-serif;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding-top: 5vh;
            padding-bottom: 20px;
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
            background-color: #252526;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
            border: 1px solid #3e3e42;
            z-index: 10;
            margin-bottom: 20px;
        }
        
        h1 {
            color: #cccccc;
            margin-bottom: 20px;
            text-align: center;
        }
        
        .input-group {
            margin-bottom: 20px;
        }
        
        label {
            display: block;
            margin-bottom: 5px;
            color: #9d9d9d;
            font-weight: bold;
        }
        
        input[type="text"], input[type="email"] {
            width: 100%;
            padding: 10px;
            font-size: 16px;
            border: 1px solid #3e3e42;
            border-radius: 4px;
            box-sizing: border-box;
            background-color: #2d2d30;
            color: #cccccc;
        }
        
        input[type="text"]:focus, input[type="email"]:focus {
            border-color: #007acc;
            outline: none;
            background-color: #1e1e1e;
        }
        
        button {
            background-color: #007acc;
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
            background-color: #0098ff;
        }
        
        .status {
            margin-top: 20px;
            padding: 10px;
            border-radius: 4px;
            text-align: center;
        }
        
        .success {
            background-color: rgba(78, 201, 176, 0.2);
            color: #4ec9b0;
            border: 1px solid #4ec9b0;
        }
        
        .error {
            background-color: rgba(244, 135, 113, 0.2);
            color: #f48771;
            border: 1px solid #f48771;
        }
        
        .timer-alert {
            animation: flash 1s infinite;
        }
        
        @keyframes flash {
            0%, 100% { background-color: #252526; }
            50% { background-color: rgba(244, 135, 113, 0.4); }
        }

        /* Timer/Lap Status Display - Prominent styling */
        #timerStatus {
            font-size: 28px;
            font-weight: bold;
            padding: 18px 24px;
            margin: 20px 0;
            border-radius: 12px;
            text-align: center;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
            transition: all 0.3s ease;
        }
        
        #timerStatus .timer-icon {
            font-size: 32px;
            margin-right: 8px;
        }
        
        #timerStatus .timer-text {
            display: block;
            font-size: 36px;
            margin: 8px 0;
            letter-spacing: 2px;
        }
        
        #timerStatus .timer-label {
            display: block;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
            opacity: 0.9;
        }
        
        /* Progress bar for laps */
        .progress-bar-container {
            width: 100%;
            height: 12px;
            background-color: #3e3e42;
            border-radius: 6px;
            margin-top: 12px;
            overflow: hidden;
        }
        
        .progress-bar-fill {
            height: 100%;
            background-color: #007acc;
            border-radius: 6px;
            transition: width 0.5s ease;
        }
        
        /* Timer active state - muted teal */
        #timerStatus.timer-active {
            background-color: #2d2d30;
            color: #4ec9b0;
            border: 2px solid #4ec9b0;
        }
        
        /* Timer warning state - muted orange */
        #timerStatus.timer-warning {
            background-color: #2d2d30;
            color: #ce9178;
            border: 2px solid #ce9178;
            animation: pulse 1.5s infinite;
        }
        
        /* Timer expired state - muted red */
        #timerStatus.timer-expired {
            background-color: #2d2d30;
            color: #f48771;
            border: 2px solid #f48771;
            animation: timerFlash 0.8s infinite;
        }
        
        /* Laps active state - accent blue */
        #timerStatus.laps-active {
            background-color: #2d2d30;
            color: #007acc;
            border: 2px solid #007acc;
        }
        
        /* Out lap state - muted purple */
        #timerStatus.laps-outlap {
            background-color: #2d2d30;
            color: #9d9d9d;
            border: 2px solid #454545;
        }
        
        @keyframes pulse {
            0%, 100% { transform: scale(1); box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
            50% { transform: scale(1.02); box-shadow: 0 6px 16px rgba(206,145,120,0.3); }
        }
        
        @keyframes timerFlash {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        
        /* Responsive sizing for mobile */
        @media (max-width: 480px) {
            #timerStatus {
                font-size: 22px;
                padding: 14px 18px;
            }
            #timerStatus .timer-text {
                font-size: 28px;
            }
        }

        @keyframes highlight-field {
            0%, 100% { background-color: transparent; }
            50% { background-color: rgba(0, 122, 204, 0.2); }
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
            background-color: #2d2d30;
            border-left: 4px solid #007acc;
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
            color: #cccccc;
        }
        
        .current-driver span {
            color: #007acc;
            font-weight: bold;
        }
        
        .current-driver-title {
            font-size: 0.85em;
            color: #9d9d9d;
            margin-bottom: 3px;
        }
        
        /* Logo styling */
        .logo-container {
            text-align: center;
            margin-top: 20px;
            padding-top: 15px;
            border-top: 1px solid #3e3e42;
            background-color: transparent;
        }
        
        .logo-container img {
            max-width: 100%;
            height: auto;
            max-height: 120px;
            width: auto;
            margin: 10px 0;
            background-color: transparent;
            box-shadow: none;
            border: none;
        }
        
        /* Simulator Tabs Styles */
        .simulator-tabs {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            background-color: #252526;
            padding: 8px 10px;
            display: flex;
            align-items: center;
            gap: 8px;
            z-index: 1000;
            box-shadow: 0 2px 8px rgba(0,0,0,0.4);
            border-bottom: 1px solid #3e3e42;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }
        
        .tabs-container {
            display: flex;
            gap: 6px;
            flex: 1;
            overflow-x: auto;
            padding: 2px 0;
        }
        
        .sim-tab {
            padding: 10px 16px;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.2s ease;
            border: 1px solid transparent;
            min-width: 80px;
            text-align: center;
            /* Prevent text selection on long-press */
            -webkit-user-select: none;
            -moz-user-select: none;
            -ms-user-select: none;
            user-select: none;
            -webkit-touch-callout: none;
            -webkit-tap-highlight-color: transparent;
            touch-action: manipulation;
        }
        
        .sim-tab * {
            -webkit-user-select: none;
            -moz-user-select: none;
            -ms-user-select: none;
            user-select: none;
            -webkit-touch-callout: none;
            pointer-events: none;
        }
        
        .sim-tab.no-driver {
            background-color: #2d2d30;
            color: #9d9d9d;
            border-color: #3e3e42;
        }
        
        .sim-tab.has-driver {
            background-color: #2d2d30;
            color: #4ec9b0;
            border-color: #4ec9b0;
        }
        
        .sim-tab.current {
            background-color: #37373d;
            color: #ffffff;
            border-color: #007acc;
            box-shadow: 0 0 8px rgba(0, 122, 204, 0.4);
        }
        
        .sim-tab.unreachable {
            background-color: #2d2d30;
            color: #f48771;
            border-color: rgba(244, 135, 113, 0.5);
            opacity: 0.7;
        }
        
        .sim-tab:hover:not(.current) {
            background-color: #37373d;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        
        .sim-tab .driver-name {
            font-size: 11px;
            font-weight: normal;
            opacity: 0.9;
            display: block;
            margin-top: 2px;
            max-width: 80px;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        
        .sim-tab .status-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 4px;
            vertical-align: middle;
        }
        
        .sim-tab .status-dot.online {
            background: #4ec9b0;
            box-shadow: 0 0 4px #4ec9b0;
        }
        
        .sim-tab .status-dot.offline {
            background: #f48771;
            box-shadow: 0 0 4px #f48771;
        }
        
        .sim-tab .session-info {
            font-size: 10px;
            font-weight: normal;
            opacity: 0.8;
            display: block;
            margin-top: 1px;
            color: #9d9d9d;
        }
        
        .sim-tab.time-expired {
            background-color: rgba(239, 68, 68, 0.3) !important;
            border-color: #ef4444 !important;
            animation: pulse-red 1.5s ease-in-out infinite;
        }
        
        .sim-tab.time-expired .driver-name {
            color: #ef4444;
        }
        
        @keyframes pulse-red {
            0%, 100% { box-shadow: 0 0 5px rgba(239, 68, 68, 0.4); }
            50% { box-shadow: 0 0 15px rgba(239, 68, 68, 0.8); }
        }
        
        /* Context Menu for Long-Press */
        .context-menu-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.6);
            z-index: 3000;
            align-items: center;
            justify-content: center;
        }
        
        .context-menu-overlay.active {
            display: flex;
        }
        
        .context-menu {
            background-color: #2d2d30;
            border-radius: 8px;
            border: 1px solid #3e3e42;
            padding: 8px 0;
            min-width: 200px;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
        }
        
        .context-menu-header {
            padding: 12px 16px;
            border-bottom: 1px solid #3e3e42;
            color: #cccccc;
            font-weight: 600;
            font-size: 14px;
        }
        
        .context-menu-item {
            padding: 12px 16px;
            color: #cccccc;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 14px;
        }
        
        .context-menu-item:hover {
            background-color: #37373d;
        }
        
        .context-menu-item.delete {
            color: #f48771;
        }
        
        .context-menu-item.delete:hover {
            background-color: rgba(244, 135, 113, 0.15);
        }
        
        /* Edit Simulator Modal */
        .edit-sim-modal {
            background-color: #252526;
            border-radius: 8px;
            border: 1px solid #3e3e42;
            padding: 24px;
            width: 90%;
            max-width: 350px;
        }
        
        .edit-sim-modal h3 {
            margin: 0 0 20px 0;
            color: #cccccc;
            font-size: 16px;
        }
        
        .edit-sim-modal input {
            width: 100%;
            padding: 10px 12px;
            border: 1px solid #3e3e42;
            border-radius: 6px;
            background-color: #1e1e1e;
            color: #cccccc;
            font-size: 14px;
            margin-bottom: 12px;
            box-sizing: border-box;
        }
        
        .edit-sim-modal input:focus {
            border-color: #007acc;
            outline: none;
        }
        
        .edit-sim-modal .btn-row {
            display: flex;
            gap: 10px;
            margin-top: 16px;
        }
        
        .edit-sim-modal button {
            flex: 1;
            padding: 10px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
        }
        
        .edit-sim-modal .save-btn {
            background-color: #007acc;
            color: white;
        }
        
        .edit-sim-modal .cancel-btn {
            background-color: #3e3e42;
            color: #cccccc;
        }
        
        .settings-btn {
            width: 40px;
            height: 40px;
            min-width: 40px;
            border-radius: 50%;
            background-color: #2d2d30;
            color: #cccccc;
            border: 1px solid #3e3e42;
            font-size: 20px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s ease;
        }
        
        .settings-btn:hover {
            background-color: #37373d;
            border-color: #007acc;
            color: #007acc;
        }
        
        /* Settings Modal */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.8);
            z-index: 2000;
            align-items: center;
            justify-content: center;
        }
        
        .modal-overlay.active {
            display: flex;
        }
        
        .modal {
            background-color: #252526;
            border-radius: 8px;
            border: 1px solid #3e3e42;
            padding: 24px;
            width: 90%;
            max-width: 400px;
            max-height: 80vh;
            overflow-y: auto;
        }
        
        .modal h3 {
            margin: 0 0 20px 0;
            color: #cccccc;
            font-size: 18px;
        }
        
        .modal-close {
            position: absolute;
            top: 10px;
            right: 15px;
            font-size: 24px;
            cursor: pointer;
            color: #9d9d9d;
        }
        
        .manual-sim-list {
            margin-bottom: 20px;
        }
        
        .manual-sim-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px;
            background-color: #2d2d30;
            border: 1px solid #3e3e42;
            border-radius: 6px;
            margin-bottom: 8px;
            color: #cccccc;
        }
        
        .manual-sim-item .remove-btn {
            background-color: #f48771;
            color: #1e1e1e;
            border: none;
            border-radius: 4px;
            padding: 4px 10px;
            cursor: pointer;
            font-size: 12px;
        }
        
        .add-sim-form {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        
        .add-sim-form input {
            padding: 10px;
            border: 1px solid #3e3e42;
            border-radius: 6px;
            font-size: 14px;
            background-color: #2d2d30;
            color: #cccccc;
        }
        
        .add-sim-form button {
            background-color: #007acc;
            color: white;
            border: none;
            padding: 12px;
            border-radius: 6px;
            font-weight: 600;
            cursor: pointer;
        }
        
        .add-sim-form button:hover {
            background-color: #0098ff;
        }
        
        .discovery-status {
            text-align: center;
            padding: 10px;
            color: #9d9d9d;
            font-size: 13px;
        }
        
        /* Manual Lap Time Entry Modal */
        .manual-lap-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.85);
            z-index: 3000;
            align-items: center;
            justify-content: center;
        }
        
        .manual-lap-modal.active {
            display: flex;
        }
        
        .manual-lap-content {
            background-color: #252526;
            border: 1px solid #3e3e42;
            border-radius: 8px;
            padding: 30px;
            width: 90%;
            max-width: 500px;
            max-height: 80vh;
            overflow-y: auto;
            position: relative;
        }
        
        .manual-lap-content h2 {
            color: #cccccc;
            margin-bottom: 10px;
            text-align: center;
            font-size: 22px;
        }
        
        .manual-lap-content .close-btn {
            position: absolute;
            top: 15px;
            right: 20px;
            font-size: 28px;
            cursor: pointer;
            color: #9d9d9d;
            background: none;
            border: none;
            padding: 0;
            width: auto;
        }
        
        .manual-lap-content .close-btn:hover {
            color: #cccccc;
        }
        
        .manual-entry-button {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background-color: #007acc;
            color: white;
            border: none;
            border-radius: 50%;
            width: 60px;
            height: 60px;
            font-size: 24px;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
            z-index: 1500;
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        .manual-entry-button:hover {
            background-color: #0098ff;
            box-shadow: 0 6px 16px rgba(0,122,204,0.4);
        }
        
        .manual-entry-button:active {
            transform: scale(0.95);
        }
        
        /* Adjust body padding when tabs are present */
        body.has-tabs {
            padding-top: calc(5vh + 60px);
        }
        
        @media only screen and (min-device-width: 768px) and (max-device-width: 1024px),
               only screen and (min-device-width: 834px) and (max-device-width: 1194px),
               only screen and (min-device-width: 1024px) and (max-device-width: 1366px) {
            body.has-tabs {
                padding-top: calc(15vh + 60px);
            }
        }

        /* Queue Panel Styles */
        .queue-panel {
            width: 90%; max-width: 500px;
            background-color: #252526; padding: 20px;
            border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.4);
            border: 1px solid #3e3e42;
            margin-top: 20px;
        }
        .queue-panel h2 {
            color: #cccccc; font-size: 18px; margin-bottom: 15px;
            display: flex; align-items: center; gap: 8px;
        }
        .queue-panel .queue-stats {
            display: flex; gap: 15px; margin-bottom: 15px;
            font-size: 13px; color: #9d9d9d;
        }
        .queue-panel .queue-stats span {
            background: #2d2d30; padding: 6px 10px; border-radius: 4px;
        }
        .queue-list {
            max-height: 300px; overflow-y: auto;
        }
        .queue-item {
            display: flex; align-items: center; justify-content: space-between;
            padding: 12px; background-color: #2d2d30;
            border: 1px solid #3e3e42; border-radius: 6px;
            margin-bottom: 8px; color: #cccccc;
        }
        .queue-item.assigned {
            border-color: #4ec9b0; background-color: rgba(78, 201, 176, 0.1);
        }
        .queue-item .position {
            font-size: 20px; font-weight: bold; color: #007acc;
            min-width: 30px; text-align: center;
        }
        .queue-item .guest-info {
            flex: 1; margin-left: 12px;
        }
        .queue-item .guest-name {
            font-weight: 600; font-size: 15px;
        }
        .queue-item .guest-wait {
            font-size: 12px; color: #9d9d9d; margin-top: 2px;
        }
        .queue-item .actions {
            display: flex; gap: 6px; align-items: center;
        }
        .queue-item select {
            padding: 6px 8px; font-size: 12px;
            background: #1e1e1e; color: #cccccc;
            border: 1px solid #3e3e42; border-radius: 4px;
        }
        .queue-item .assign-btn {
            padding: 6px 12px; font-size: 12px;
            background: #007acc; color: white;
            border: none; border-radius: 4px; cursor: pointer;
        }
        .queue-item .assign-btn:hover { background: #0098ff; }
        .queue-item .remove-btn {
            padding: 6px 10px; font-size: 12px;
            background: transparent; color: #f48771;
            border: 1px solid #f48771; border-radius: 4px; cursor: pointer;
        }
        .queue-item .remove-btn:hover {
            background: rgba(244, 135, 113, 0.15);
        }
        .queue-empty {
            text-align: center; color: #6e6e6e;
            padding: 30px; font-style: italic;
        }

        /* Session Control Buttons in Sim Tabs */
        .session-controls {
            display: flex; gap: 8px; margin-top: 10px;
            padding: 10px; background: #2d2d30;
            border-radius: 6px; border: 1px solid #3e3e42;
        }
        .session-controls button {
            flex: 1; padding: 10px 12px; font-size: 13px;
            border: none; border-radius: 4px; cursor: pointer;
            font-weight: 600;
        }
        .session-controls .start-btn {
            background: #4ec9b0; color: #1e1e1e;
        }
        .session-controls .start-btn:hover { background: #5fd9c0; }
        .session-controls .start-btn:disabled {
            background: #3e3e42; color: #6e6e6e; cursor: not-allowed;
        }
        .session-controls .end-btn {
            background: #f48771; color: #1e1e1e;
        }
        .session-controls .end-btn:hover { background: #ff9785; }
        .session-controls .end-btn:disabled {
            background: #3e3e42; color: #6e6e6e; cursor: not-allowed;
        }
    </style>
</head>
<body class="has-tabs">
    <!-- Simulator Tabs Bar -->
    <div class="simulator-tabs" id="simulatorTabs">
        <div class="tabs-container" id="tabsContainer">
            <!-- Tabs populated dynamically -->
        </div>
        <button class="settings-btn" onclick="openSettingsModal()">+</button>
    </div>
    
    <!-- Settings Modal for Manual Simulator Configuration -->
    <div class="modal-overlay" id="settingsModal">
        <div class="modal">
            <h3>⚙️ Simulator Configuration</h3>
            <div class="discovery-status" id="discoveryStatus">
                Searching for simulators...
            </div>
            <div class="manual-sim-list" id="manualSimList">
                <!-- Manual entries appear here -->
            </div>
            <div class="add-sim-form">
                <input type="text" id="manualSimIp" placeholder="IP Address (e.g., 192.168.1.10)">
                <input type="text" id="manualSimName" placeholder="Name (e.g., Sim 3)">
                <button onclick="addManualSimulator()">➕ Add Simulator</button>
                <button onclick="closeSettingsModal()" style="background-color: #3e3e42; margin-top: 5px;">Close</button>
            </div>
        </div>
    </div>
    
    <!-- Context Menu for Long-Press on Simulator Tabs -->
    <div class="context-menu-overlay" id="contextMenuOverlay" onclick="closeContextMenu()">
        <div class="context-menu" onclick="event.stopPropagation()">
            <div class="context-menu-header" id="contextMenuHeader">Sim 1</div>
            <div class="context-menu-item" onclick="editSimulator()">
                ✏️ Edit
            </div>
            <div class="context-menu-item delete" onclick="deleteSimulator()">
                🗑️ Delete
            </div>
            <div class="context-menu-item" onclick="closeContextMenu()">
                ✖️ Cancel
            </div>
        </div>
    </div>
    
    <!-- Edit Simulator Modal -->
    <div class="context-menu-overlay" id="editSimModal">
        <div class="edit-sim-modal" onclick="event.stopPropagation()">
            <h3>✏️ Edit Simulator</h3>
            <input type="text" id="editSimIp" placeholder="IP Address">
            <input type="text" id="editSimName" placeholder="Name">
            <div class="btn-row">
                <button class="cancel-btn" onclick="closeEditModal()">Cancel</button>
                <button class="save-btn" onclick="saveSimulatorEdit()">Save</button>
            </div>
        </div>
    </div>

    <div class="container" id="container">
        <h1>🏁 Driver Registration</h1>
        <p style="text-align: center; color: #9d9d9d; margin-bottom: 20px; font-style: italic;">
            Set your driver name for lap time tracking
        </p>
        
        <div class="current-driver" id="currentDriverBox" style="display:none;">
            <div>
                <p class="current-driver-title">🏆 Active Driver:</p>
                <p><span id="currentDriverName">None</span></p>
            </div>
            <div style="font-size: 0.8em; color: #9d9d9d;">
                Ready for racing!
            </div>
        </div>
        
        <div class="input-group">
            <label for="driverName">👤 Driver Name:</label>
            <input type="text" id="driverName" placeholder="Enter your name (e.g., John Smith)" required>
            <small style="color: #6e6e6e; font-style: italic;">This will appear on the leaderboard</small>
        </div>
        <div class="input-group" id="emailGroup" style="display: none;">
            <label for="driverEmail">📧 Email Address: <span style="color: #f48771;">*</span></label>
            <input type="email" id="driverEmail" placeholder="Enter email address (required)" required>
            <small style="color: #6e6e6e; font-style: italic;">Required for driver communication and results</small>
        </div>
        <button onclick="updateDriver()">🏁 Register as Active Driver</button>
        <div id="status" class="status" style="display: none;"></div>
        <div id="timerStatus" style="display: none; margin-top: 15px;"></div>
        
        <!-- Logo at the bottom of the form -->
        <div class="logo-container">
            <img src="/logo" alt="Company Logo" id="companyLogo">
        </div>
    </div>

    <!-- Queue Management Panel -->
    <div class="queue-panel" id="queuePanel">
        <h2>📋 Guest Queue</h2>
        <div class="queue-stats">
            <span id="queueCount">0 waiting</span>
            <span id="avgWaitTime">Avg: --</span>
        </div>
        <div class="queue-list" id="queueList">
            <div class="queue-empty">Queue is empty</div>
        </div>

        <!-- Session Controls for Current Simulator -->
        <div class="session-controls" id="sessionControls" style="margin-top: 15px;">
            <button class="start-btn" id="startSessionBtn" onclick="startSessionForAssigned()" disabled>
                ▶️ Start Session
            </button>
            <button class="end-btn" id="endSessionBtn" onclick="forceEndSession()" disabled>
                ⏹️ End Session
            </button>
        </div>
        <div id="sessionStatus" style="text-align: center; margin-top: 10px; font-size: 12px; color: #9d9d9d;"></div>
    </div>

    <!-- Floating button to open manual lap entry (discrete, bottom right) -->
    <button class="manual-entry-button" onclick="openManualLapModal()" title="Manual Lap Time Entry">
        ⏱️
    </button>
    
    <!-- Manual Lap Time Entry Modal -->
    <div class="manual-lap-modal" id="manualLapModal">
        <div class="manual-lap-content">
            <button class="close-btn" onclick="closeManualLapModal()">&times;</button>
            <h2>⏱️ Manual Lap Time Entry</h2>
            <p style="text-align: center; color: #9d9d9d; margin-bottom: 25px; font-size: 14px; font-style: italic;">
                Manually submit a lap time (for offline or manual timing)
            </p>
            
            <div class="input-group">
                <label for="manualDriverName">👤 Driver Name:</label>
                <input type="text" id="manualDriverName" placeholder="Enter driver name">
                <small style="color: #6e6e6e; font-style: italic;">Name that will appear on the leaderboard</small>
            </div>
            
            <div class="input-group">
                <label for="manualLapTime">⏱️ Lap Time:</label>
                <input type="text" id="manualLapTime" placeholder="e.g., 1:23.456 or 83.456">
                <small style="color: #6e6e6e; font-style: italic;">Format: mm:ss.xxx or total seconds</small>
            </div>
            
            <button onclick="submitManualLapTime()" style="background-color: #007acc;">📤 Send Manual Lap Time</button>
            <div id="manualStatus" class="status" style="display: none;"></div>
        </div>
    </div>
    
    <script>
        // Log screen dimensions for debugging
        console.log(`Screen dimensions: ${window.innerWidth}x${window.innerHeight}`);
        console.log(`Screen ratio: ${window.innerWidth/window.innerHeight}`);
        console.log(`Device pixel ratio: ${window.devicePixelRatio}`);
        
        // Variable to track if this is the initial page load
        let isInitialLoad = true;
        
        // ============================================
        // MULTI-SIMULATOR TABS FUNCTIONALITY
        // ============================================
        
        const STORAGE_KEY = 'lapTimeSender_manualSimulators';
        let discoveredSimulators = [];
        let currentSimulatorIp = null;
        let receiverUrl = null;
        let queueData = { queue: [], active_sessions: {} };
        let assignedQueueId = null;  // Track which queue entry is assigned to this sim

        // ============================================
        // QUEUE MANAGEMENT FUNCTIONALITY
        // ============================================

        async function getReceiverUrl() {
            if (receiverUrl) return receiverUrl;
            try {
                const resp = await fetch('/api/get_receiver_url');
                const data = await resp.json();
                if (data.success && data.receiver_url) {
                    receiverUrl = data.receiver_url;
                    return receiverUrl;
                }
            } catch (e) {
                console.error('Error getting receiver URL:', e);
            }
            return null;
        }

        async function refreshQueue() {
            const url = await getReceiverUrl();
            if (!url) {
                document.getElementById('queueList').innerHTML = '<div class="queue-empty">Cannot connect to queue server</div>';
                return;
            }

            try {
                const response = await fetch(url + '/api/queue');
                const data = await response.json();
                if (data.success) {
                    queueData = data;
                    renderQueue(data.queue);
                    updateQueueStats(data);
                    updateSessionControls();
                }
            } catch (e) {
                console.error('Error fetching queue:', e);
            }
        }

        function renderQueue(queue) {
            const list = document.getElementById('queueList');
            if (!queue || queue.length === 0) {
                list.innerHTML = '<div class="queue-empty">Queue is empty</div>';
                return;
            }

            // Build simulator options for assign dropdown
            let simOptions = '<option value="">Assign to...</option>';
            discoveredSimulators.forEach(sim => {
                const hasDriver = sim.current_driver && sim.current_driver.trim();
                const label = sim.simulator_name || `Sim ${sim.simulator_id}`;
                const status = hasDriver ? ` (${sim.current_driver})` : ' (Available)';
                simOptions += `<option value="${sim.ip}">${label}${status}</option>`;
            });

            list.innerHTML = queue.map((entry, idx) => {
                const isAssigned = entry.assigned_to;
                const assignedClass = isAssigned ? 'assigned' : '';
                const assignedText = isAssigned ? `Assigned to ${entry.assigned_to}` : entry.wait_display;

                return `
                    <div class="queue-item ${assignedClass}" data-id="${entry.id}">
                        <div class="position">#${entry.position}</div>
                        <div class="guest-info">
                            <div class="guest-name">${escapeHtml(entry.name)}</div>
                            <div class="guest-wait">${assignedText}</div>
                        </div>
                        <div class="actions">
                            ${!isAssigned ? `
                                <select onchange="assignToSimulator('${entry.id}', this.value)">
                                    ${simOptions}
                                </select>
                            ` : `
                                <button class="assign-btn" onclick="startRemoteSession('${entry.id}', '${escapeHtml(entry.name)}', '${entry.assigned_to}')">
                                    Start
                                </button>
                            `}
                            <button class="remove-btn" onclick="removeFromQueue('${entry.id}')">✕</button>
                        </div>
                    </div>
                `;
            }).join('');
        }

        function updateQueueStats(data) {
            document.getElementById('queueCount').textContent = `${data.total_in_queue || 0} waiting`;

            // Get average wait time from stats endpoint
            getReceiverUrl().then(url => {
                if (!url) return;
                fetch(url + '/api/queue/stats')
                    .then(r => r.json())
                    .then(stats => {
                        if (stats.avg_session_time_display) {
                            document.getElementById('avgWaitTime').textContent = `Avg session: ${stats.avg_session_time_display}`;
                        } else {
                            document.getElementById('avgWaitTime').textContent = 'Avg: calculating...';
                        }
                    })
                    .catch(() => {});
            });
        }

        function updateSessionControls() {
            const startBtn = document.getElementById('startSessionBtn');
            const endBtn = document.getElementById('endSessionBtn');
            const statusEl = document.getElementById('sessionStatus');

            // Check if current simulator has an assigned guest
            const myIp = currentSimulatorIp;
            const assignedEntry = queueData.queue?.find(e => e.assigned_to === myIp);
            const activeSession = queueData.active_sessions?.[myIp];

            if (activeSession) {
                // Session active - enable end button
                startBtn.disabled = true;
                endBtn.disabled = false;
                statusEl.textContent = `Session active: ${activeSession.driver_name || 'Unknown'}`;
            } else if (assignedEntry) {
                // Guest assigned but session not started
                startBtn.disabled = false;
                endBtn.disabled = true;
                assignedQueueId = assignedEntry.id;
                statusEl.textContent = `Ready to start: ${assignedEntry.name}`;
            } else {
                // No assignment, no session
                startBtn.disabled = true;
                endBtn.disabled = true;
                assignedQueueId = null;
                statusEl.textContent = 'Assign a guest to start';
            }
        }

        async function assignToSimulator(queueId, simulatorIp) {
            if (!simulatorIp) return;

            const url = await getReceiverUrl();
            if (!url) return;

            // Find simulator name for SMS
            const sim = discoveredSimulators.find(s => s.ip === simulatorIp);
            const simulatorName = sim ? (sim.simulator_name || `Sim ${sim.simulator_id}`) : simulatorIp;

            try {
                const response = await fetch(url + '/api/queue/assign', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        queue_id: queueId,
                        simulator_ip: simulatorIp,
                        simulator_name: simulatorName
                    })
                });
                const data = await response.json();
                if (data.success) {
                    if (data.sms_sent) {
                        console.log('SMS notification sent to driver');
                    }
                    refreshQueue();
                }
            } catch (e) {
                console.error('Error assigning to simulator:', e);
            }
        }

        async function removeFromQueue(queueId) {
            const url = await getReceiverUrl();
            if (!url) return;

            try {
                await fetch(url + '/api/queue/remove', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: queueId })
                });
                refreshQueue();
            } catch (e) {
                console.error('Error removing from queue:', e);
            }
        }

        async function startRemoteSession(queueId, driverName, simulatorIp) {
            try {
                const response = await fetch(`http://${simulatorIp}:5001/api/session/start_remote`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ driver_name: driverName, queue_id: queueId })
                });
                const data = await response.json();
                if (data.success) {
                    // Remove from queue after session starts
                    await removeFromQueue(queueId);
                    refreshQueue();
                } else {
                    alert('Failed to start session: ' + (data.error || 'Unknown error'));
                }
            } catch (e) {
                console.error('Error starting remote session:', e);
                alert('Failed to connect to simulator');
            }
        }

        async function startSessionForAssigned() {
            if (!assignedQueueId) return;

            const entry = queueData.queue?.find(e => e.id === assignedQueueId);
            if (!entry) return;

            // Start session on current simulator
            try {
                const response = await fetch('/api/session/start_remote', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ driver_name: entry.name, queue_id: entry.id })
                });
                const data = await response.json();
                if (data.success) {
                    await removeFromQueue(entry.id);
                    refreshQueue();
                }
            } catch (e) {
                console.error('Error starting session:', e);
            }
        }

        async function forceEndSession() {
            try {
                const response = await fetch('/api/session/force_end', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' }
                });
                const data = await response.json();
                if (data.success) {
                    refreshQueue();
                }
            } catch (e) {
                console.error('Error ending session:', e);
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text || '';
            return div.innerHTML;
        }

        // Start queue polling
        refreshQueue();
        setInterval(refreshQueue, 5000);

        // Get manual simulators from localStorage
        function getManualSimulators() {
            try {
                const stored = localStorage.getItem(STORAGE_KEY);
                return stored ? JSON.parse(stored) : [];
            } catch (e) {
                console.error('Error reading manual simulators:', e);
                return [];
            }
        }
        
        // Save manual simulators to localStorage
        function saveManualSimulators(simulators) {
            try {
                localStorage.setItem(STORAGE_KEY, JSON.stringify(simulators));
            } catch (e) {
                console.error('Error saving manual simulators:', e);
            }
        }
        
        // Discover simulators on the network
        async function discoverSimulators() {
            try {
                const response = await fetch('/api/discover_simulators');
                const data = await response.json();
                discoveredSimulators = data.simulators || [];
                currentSimulatorIp = data.current_ip;
                return discoveredSimulators;
            } catch (e) {
                console.error('Error discovering simulators:', e);
                return [];
            }
        }
        
        // Refresh simulator info for a specific IP (with short timeout for live updates)
        async function refreshSimulatorInfo(ip, port = 5001) {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 1500);
            
            try {
                const response = await fetch(`http://${ip}:${port}/api/simulator_info`, {
                    mode: 'cors',
                    signal: controller.signal
                });
                clearTimeout(timeoutId);
                if (response.ok) {
                    const data = await response.json();
                    data.ip = ip;
                    data.port = port;
                    data.reachable = true;
                    return data;
                }
            } catch (e) {
                clearTimeout(timeoutId);
            }
            return null;
        }
        
        // Fast refresh of tab data without full network discovery
        async function refreshTabsLive() {
            if (discoveredSimulators.length === 0) return;
            
            // Fetch fresh data from all known simulators in parallel
            const refreshPromises = discoveredSimulators.map(async (sim) => {
                if (sim.is_current) {
                    // For current sim, fetch fresh local data
                    try {
                        const response = await fetch('/api/simulator_info');
                        if (response.ok) {
                            const data = await response.json();
                            return { ...sim, ...data, is_current: true, reachable: true };
                        }
                    } catch (e) {}
                    return sim;
                } else {
                    // For remote sims, fetch their info
                    const freshData = await refreshSimulatorInfo(sim.ip, sim.port || 5001);
                    if (freshData) {
                        return { ...sim, ...freshData, reachable: true };
                    } else {
                        return { ...sim, reachable: false };
                    }
                }
            });
            
            const updatedSimulators = await Promise.all(refreshPromises);
            discoveredSimulators = updatedSimulators;
            renderTabs(updatedSimulators);
        }
        
        // Render the simulator tabs
        function renderTabs(simulators) {
            const container = document.getElementById('tabsContainer');
            const tabsBar = document.getElementById('simulatorTabs');
            
            if (!container) return;
            
            // Merge discovered with manual simulators
            const manualSims = getManualSimulators();
            const allSimulators = [...simulators];
            
            // Add manual simulators that aren't already in the list
            manualSims.forEach(manual => {
                const exists = allSimulators.some(s => s.ip === manual.ip);
                if (!exists) {
                    allSimulators.push({
                        simulator_id: manual.simulator_id || 99,
                        simulator_name: manual.name || `Sim (${manual.ip})`,
                        ip: manual.ip,
                        port: manual.port || 5001,
                        current_driver: null,
                        is_manual: true
                    });
                }
            });
            
            // Sort by simulator_id
            allSimulators.sort((a, b) => (a.simulator_id || 0) - (b.simulator_id || 0));
            
            // Always show tabs bar for staff portal functionality
            // This allows staff to add other simulators via the "+" button
            tabsBar.style.display = 'flex';
            document.body.classList.add('has-tabs');
            
            // Clear and rebuild tabs
            container.innerHTML = '';
            
            allSimulators.forEach(sim => {
                const tab = document.createElement('div');
                tab.className = 'sim-tab';
                
                // Determine tab state
                const isCurrent = sim.ip === currentSimulatorIp || sim.is_current;
                const hasDriver = sim.current_driver && sim.current_driver.trim() !== '';
                const isReachable = sim.reachable !== false; // Default to true if not set
                
                // Check for time expired state
                const timerExpired = sim.timer_active && sim.timer_seconds_left === 0;
                const lapsExpired = sim.laps_active && sim.laps_progress && 
                                    sim.laps_progress.count >= sim.laps_progress.target && 
                                    sim.laps_progress.target > 0;
                const sessionExpired = timerExpired || lapsExpired;
                
                if (sessionExpired && hasDriver) {
                    tab.classList.add('time-expired');
                } else if (!isReachable && !isCurrent) {
                    tab.classList.add('unreachable');
                } else if (isCurrent) {
                    tab.classList.add('current');
                } else if (hasDriver) {
                    tab.classList.add('has-driver');
                } else {
                    tab.classList.add('no-driver');
                }
                
                // Status dot (green for online, red for offline)
                const statusDot = isCurrent ? 'online' : (isReachable ? 'online' : 'offline');
                const statusText = !isReachable && !isCurrent ? 'Offline' : (hasDriver ? sim.current_driver : 'No driver');
                
                // Session info (timer or laps)
                let sessionInfo = '';
                if (sim.timer_active && sim.timer_seconds_left !== undefined) {
                    const mins = Math.floor(sim.timer_seconds_left / 60);
                    const secs = sim.timer_seconds_left % 60;
                    sessionInfo = `${mins}:${secs.toString().padStart(2, '0')}`;
                    if (sim.timer_seconds_left === 0) sessionInfo = '0:00';
                } else if (sim.laps_active && sim.laps_progress) {
                    sessionInfo = `${sim.laps_progress.count}/${sim.laps_progress.target} laps`;
                }
                
                // Tab content
                tab.innerHTML = `
                    <div><span class="status-dot ${statusDot}"></span>${sim.simulator_name}</div>
                    <span class="driver-name">${statusText}</span>
                    ${sessionInfo ? `<span class="session-info">${sessionInfo}</span>` : ''}
                `;
                
                // Setup long-press for context menu (only for non-current simulators)
                if (!isCurrent) {
                    setupLongPress(tab, sim);
                }
                
                // Click to navigate to that simulator
                if (!isCurrent) {
                    if (isReachable) {
                        tab.onclick = (e) => {
                            // Don't navigate if this was a long-press
                            if (e.defaultPrevented) return;
                            navigateToSimulator(sim.ip, sim.port || 5001);
                        };
                    } else {
                        tab.onclick = (e) => {
                            if (e.defaultPrevented) return;
                            alert(`Cannot connect to ${sim.simulator_name} (${sim.ip})\\n\\nMake sure:\\n• The simulator PC is running Lap Time Sender\\n• Both devices are on the same network\\n• Windows Firewall allows port 5001`);
                        };
                    }
                }
                
                container.appendChild(tab);
            });
        }
        
        // Navigate to a different simulator
        function navigateToSimulator(ip, port = 5001) {
            window.location.href = `http://${ip}:${port}`;
        }
        
        // Open settings modal
        function openSettingsModal() {
            const modal = document.getElementById('settingsModal');
            modal.classList.add('active');
            renderManualSimulatorList();
        }
        
        // Close settings modal
        function closeSettingsModal() {
            const modal = document.getElementById('settingsModal');
            modal.classList.remove('active');
        }
        
        // Context menu state
        let contextMenuSimulator = null;
        let longPressTimer = null;
        const LONG_PRESS_DURATION = 500; // ms
        
        // Open context menu for a simulator
        function openContextMenu(sim) {
            contextMenuSimulator = sim;
            document.getElementById('contextMenuHeader').textContent = sim.simulator_name || sim.name || 'Simulator';
            document.getElementById('contextMenuOverlay').classList.add('active');
        }
        
        // Close context menu
        function closeContextMenu() {
            document.getElementById('contextMenuOverlay').classList.remove('active');
            contextMenuSimulator = null;
        }
        
        // Edit simulator
        function editSimulator() {
            if (!contextMenuSimulator) return;
            closeContextMenu();
            
            document.getElementById('editSimIp').value = contextMenuSimulator.ip || '';
            document.getElementById('editSimName').value = contextMenuSimulator.simulator_name || contextMenuSimulator.name || '';
            document.getElementById('editSimModal').classList.add('active');
        }
        
        // Close edit modal
        function closeEditModal() {
            document.getElementById('editSimModal').classList.remove('active');
        }
        
        // Save simulator edit
        async function saveSimulatorEdit() {
            const oldIp = contextMenuSimulator?.ip;
            const newIp = document.getElementById('editSimIp').value.trim();
            const newName = document.getElementById('editSimName').value.trim();
            
            if (!newIp) {
                alert('Please enter an IP address');
                return;
            }
            
            // Basic IP validation
            if (!/^\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}$/.test(newIp)) {
                alert('Please enter a valid IP address');
                return;
            }
            
            // Update in localStorage
            const manualSims = getManualSimulators();
            const existingIndex = manualSims.findIndex(s => s.ip === oldIp);
            
            if (existingIndex >= 0) {
                // Update existing
                manualSims[existingIndex] = { ip: newIp, name: newName || `Sim (${newIp})`, port: 5001 };
            } else {
                // Add as new manual simulator
                manualSims.push({ ip: newIp, name: newName || `Sim (${newIp})`, port: 5001 });
            }
            
            // If IP changed, remove old from server
            if (oldIp && oldIp !== newIp) {
                try {
                    await fetch(`/api/known_simulators/${oldIp}`, { method: 'DELETE' });
                } catch (e) { console.log('Could not remove old IP from server'); }
            }
            
            // Save new/updated to server
            try {
                await fetch('/api/known_simulators', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ip: newIp, name: newName })
                });
            } catch (e) { console.log('Could not save to server'); }
            
            saveManualSimulators(manualSims);
            closeEditModal();
            initializeSimulatorTabs();
        }
        
        // Delete simulator
        async function deleteSimulator() {
            if (!contextMenuSimulator) return;
            
            const ip = contextMenuSimulator.ip;
            const name = contextMenuSimulator.simulator_name || contextMenuSimulator.name || 'this simulator';
            
            if (!confirm(`Delete ${name} (${ip})?`)) {
                closeContextMenu();
                return;
            }
            
            // Remove from localStorage
            const manualSims = getManualSimulators();
            const filtered = manualSims.filter(s => s.ip !== ip);
            saveManualSimulators(filtered);
            
            // Remove from server
            try {
                await fetch(`/api/known_simulators/${ip}`, { method: 'DELETE' });
            } catch (e) { console.log('Could not remove from server'); }
            
            closeContextMenu();
            initializeSimulatorTabs();
        }
        
        // Setup long-press handler on a tab element
        function setupLongPress(tab, sim) {
            let pressTimer = null;
            let didLongPress = false;
            let touchStartPos = null;
            
            const startPress = (e) => {
                didLongPress = false;
                // Store initial touch position
                if (e.touches && e.touches[0]) {
                    touchStartPos = { x: e.touches[0].clientX, y: e.touches[0].clientY };
                }
                pressTimer = setTimeout(() => {
                    didLongPress = true;
                    openContextMenu(sim);
                    // Vibrate on mobile if supported
                    if (navigator.vibrate) navigator.vibrate(50);
                }, LONG_PRESS_DURATION);
            };
            
            const endPress = (e) => {
                clearTimeout(pressTimer);
                if (didLongPress) {
                    e.preventDefault();
                    e.stopPropagation();
                    e.stopImmediatePropagation();
                }
            };
            
            const movePress = (e) => {
                // Cancel if moved more than 10px (user is scrolling)
                if (touchStartPos && e.touches && e.touches[0]) {
                    const dx = Math.abs(e.touches[0].clientX - touchStartPos.x);
                    const dy = Math.abs(e.touches[0].clientY - touchStartPos.y);
                    if (dx > 10 || dy > 10) {
                        clearTimeout(pressTimer);
                    }
                }
            };
            
            const cancelPress = () => {
                clearTimeout(pressTimer);
            };
            
            // Touch events for mobile - use passive: false to allow preventDefault
            tab.addEventListener('touchstart', startPress, { passive: false });
            tab.addEventListener('touchend', endPress, { passive: false });
            tab.addEventListener('touchmove', movePress, { passive: true });
            tab.addEventListener('touchcancel', cancelPress);
            
            // Mouse events for desktop (right-click)
            tab.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                openContextMenu(sim);
            });
        }
        
        // Render manual simulator list in modal
        function renderManualSimulatorList() {
            const list = document.getElementById('manualSimList');
            const manualSims = getManualSimulators();
            
            if (manualSims.length === 0) {
                list.innerHTML = '<p style="color: #9d9d9d; text-align: center; font-size: 13px;">No manual simulators added</p>';
                return;
            }
            
            list.innerHTML = manualSims.map((sim, index) => `
                <div class="manual-sim-item">
                    <div>
                        <strong>${sim.name}</strong>
                        <div style="font-size: 12px; color: #9d9d9d;">${sim.ip}:${sim.port || 5001}</div>
                    </div>
                    <button class="remove-btn" onclick="removeManualSimulator(${index})">Remove</button>
                </div>
            `).join('');
        }
        
        // Add manual simulator
        async function addManualSimulator() {
            const ipInput = document.getElementById('manualSimIp');
            const nameInput = document.getElementById('manualSimName');
            
            const ip = ipInput.value.trim();
            const name = nameInput.value.trim() || `Sim (${ip})`;
            
            if (!ip) {
                alert('Please enter an IP address');
                return;
            }
            
            // Basic IP validation
            if (!/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(ip)) {
                alert('Please enter a valid IP address (e.g., 192.168.1.10)');
                return;
            }
            
            const manualSims = getManualSimulators();
            
            // Check if already exists
            if (manualSims.some(s => s.ip === ip)) {
                alert('This simulator IP is already added');
                return;
            }
            
            // Save to localStorage (for browser persistence)
            manualSims.push({ ip, name, port: 5001 });
            saveManualSimulators(manualSims);
            
            // Also save to server config (so it persists across all devices)
            try {
                await fetch('/api/known_simulators', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ip, name })
                });
            } catch (e) {
                console.log('Could not save to server config:', e);
            }
            
            // Clear inputs
            ipInput.value = '';
            nameInput.value = '';
            
            // Refresh UI
            renderManualSimulatorList();
            initializeSimulatorTabs();
        }
        
        // Remove manual simulator
        async function removeManualSimulator(index) {
            const manualSims = getManualSimulators();
            const removed = manualSims[index];
            manualSims.splice(index, 1);
            saveManualSimulators(manualSims);
            
            // Also remove from server config
            if (removed && removed.ip) {
                try {
                    await fetch(`/api/known_simulators/${removed.ip}`, {
                        method: 'DELETE'
                    });
                } catch (e) {
                    console.log('Could not remove from server config:', e);
                }
            }
            
            renderManualSimulatorList();
            initializeSimulatorTabs();
        }
        
        // Initialize simulator tabs on page load
        async function initializeSimulatorTabs() {
            const statusEl = document.getElementById('discoveryStatus');
            if (statusEl) statusEl.textContent = 'Searching for simulators...';
            
            const simulators = await discoverSimulators();
            
            if (statusEl) {
                const total = simulators.length;
                const online = simulators.filter(s => s.reachable !== false).length;
                const offline = total - online;
                
                if (total <= 1) {
                    statusEl.textContent = 'Only this simulator found. Add others manually below.';
                } else if (offline > 0) {
                    statusEl.textContent = `Found ${total} simulators (${online} online, ${offline} offline)`;
                } else {
                    statusEl.textContent = `Found ${total} simulators on network`;
                }
            }
            
            renderTabs(simulators);
        }
        
        // Periodically refresh tabs with live data
        function startTabRefresh() {
            // Fast live refresh every 2 seconds for timer/driver updates
            setInterval(async () => {
                await refreshTabsLive();
            }, 2000);
            
            // Full discovery every 30 seconds to find new/removed simulators
            setInterval(async () => {
                await initializeSimulatorTabs();
            }, 30000);
        }
        
        // Initialize on page load
        document.addEventListener('DOMContentLoaded', () => {
            initializeSimulatorTabs();
            startTabRefresh();
        });
        
        // ============================================
        // END MULTI-SIMULATOR TABS FUNCTIONALITY
        // ============================================
        
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
        
        function parseLapTime(input) {
            // Parse lap time in various formats
            // Supports: "1:23.456" (mm:ss.xxx) or "83.456" (total seconds)
            input = input.trim();
            
            // Check if it's in mm:ss.xxx format
            if (input.includes(':')) {
                const parts = input.split(':');
                if (parts.length !== 2) {
                    return null;
                }
                const minutes = parseInt(parts[0]);
                const seconds = parseFloat(parts[1]);
                if (isNaN(minutes) || isNaN(seconds)) {
                    return null;
                }
                return minutes * 60 + seconds;
            } else {
                // Assume it's already in seconds
                const seconds = parseFloat(input);
                if (isNaN(seconds)) {
                    return null;
                }
                return seconds;
            }
        }
        
        function showManualStatus(message, isError) {
            const status = document.getElementById('manualStatus');
            status.textContent = message;
            status.style.display = 'block';
            status.className = 'status ' + (isError ? 'error' : 'success');
            setTimeout(() => {
                status.style.display = 'none';
            }, 5000);
        }
        
        function openManualLapModal() {
            const modal = document.getElementById('manualLapModal');
            modal.classList.add('active');
            // Focus on driver name field
            document.getElementById('manualDriverName').focus();
        }
        
        function closeManualLapModal() {
            const modal = document.getElementById('manualLapModal');
            modal.classList.remove('active');
        }
        
        // Close modal when clicking outside of content
        document.addEventListener('click', function(event) {
            const modal = document.getElementById('manualLapModal');
            if (event.target === modal) {
                closeManualLapModal();
            }
        });
        
        function submitManualLapTime() {
            const driverName = document.getElementById('manualDriverName').value.trim();
            const lapTimeInput = document.getElementById('manualLapTime').value.trim();
            
            // Validation
            if (!driverName) {
                showManualStatus('⚠️ Please enter a driver name', true);
                return;
            }
            
            if (!lapTimeInput) {
                showManualStatus('⚠️ Please enter a lap time', true);
                return;
            }
            
            // Parse the lap time
            const lapTimeSeconds = parseLapTime(lapTimeInput);
            if (lapTimeSeconds === null) {
                showManualStatus('⚠️ Invalid lap time format. Use mm:ss.xxx or seconds', true);
                return;
            }
            
            // Validate lap time range (5 seconds to 60 minutes)
            if (lapTimeSeconds < 5 || lapTimeSeconds > 3600) {
                showManualStatus('⚠️ Lap time must be between 5 seconds and 60 minutes', true);
                return;
            }
            
            // Send to server
            fetch('/api/manual_lap', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    driver_name: driverName,
                    lap_time: lapTimeSeconds
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showManualStatus(`✅ Lap time ${data.formatted_time} sent successfully!`, false);
                    // Clear the form
                    document.getElementById('manualLapTime').value = '';
                    // Optionally clear driver name too
                    // document.getElementById('manualDriverName').value = '';
                    
                    // Close modal after brief delay to show success message
                    setTimeout(() => {
                        closeManualLapModal();
                    }, 1500);
                } else {
                    showManualStatus(`❌ ${data.message || 'Failed to send lap time'}`, true);
                }
            })
            .catch(error => {
                showManualStatus('❌ Error connecting to server', true);
                console.error('Error:', error);
            });
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
                    
                    // Helper function to clear all timer CSS classes
                    function clearTimerClasses() {
                        timerStatus.classList.remove('timer-active', 'timer-warning', 'timer-expired', 'laps-active', 'laps-outlap');
                    }
                    
                    // Render rotation status: time or laps
                    if (data.rotation_mode === 'Laps' && data.laps_active) {
                        clearTimerClasses();
                        if (!data.laps_progress.started) {
                            // Out lap in progress
                            timerStatus.classList.add('laps-outlap');
                            timerStatus.innerHTML = `
                                <span class="timer-label">Out Lap</span>
                                <span class="timer-text">🏁 Warming Up...</span>
                            `;
                        } else {
                            // Laps in progress
                            timerStatus.classList.add('laps-active');
                            const lapCount = data.laps_progress.count;
                            const lapTarget = data.laps_progress.target;
                            const progressPercent = Math.min(100, (lapCount / lapTarget) * 100);
                            timerStatus.innerHTML = `
                                <span class="timer-label">Laps Completed</span>
                                <span class="timer-text">🏁 ${lapCount} / ${lapTarget}</span>
                                <div class="progress-bar-container">
                                    <div class="progress-bar-fill" style="width: ${progressPercent}%"></div>
                                </div>
                            `;
                        }
                        timerStatus.style.display = 'block';
                        container.classList.remove('timer-alert');
                    } else if (data.timer_active && data.timer_seconds_left <= 0) {
                        // Timer expired
                        clearTimerClasses();
                        timerStatus.classList.add('timer-expired');
                        timerStatus.innerHTML = `
                            <span class="timer-text">🚨 TIME EXPIRED 🚨</span>
                            <span class="timer-label">Please Change Driver</span>
                        `;
                        timerStatus.style.display = 'block';
                        container.classList.add('timer-alert');
                    } else if (data.timer_active) {
                        // Timer active
                        clearTimerClasses();
                        const minutes = Math.floor(data.timer_seconds_left / 60);
                        const seconds = data.timer_seconds_left % 60;
                        const formattedTime = `${minutes}:${seconds.toString().padStart(2, '0')}`;
                        
                        // Warning state when less than 2 minutes remain
                        if (data.timer_seconds_left <= 120) {
                            timerStatus.classList.add('timer-warning');
                            timerStatus.innerHTML = `
                                <span class="timer-label">⚠️ Time Running Low!</span>
                                <span class="timer-text">⏱️ ${formattedTime}</span>
                            `;
                        } else {
                            timerStatus.classList.add('timer-active');
                            timerStatus.innerHTML = `
                                <span class="timer-label">Time Remaining</span>
                                <span class="timer-text">⏱️ ${formattedTime}</span>
                            `;
                        }
                        timerStatus.style.display = 'block';
                        container.classList.remove('timer-alert');
                    } else {
                        // No timer or lap-based rotation
                        clearTimerClasses();
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

# Queue join page for guests
QUEUE_HTML_CONTENT = r"""
<!DOCTYPE html>
<html>
<head>
    <title>Join the Queue</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body {
            width: 100%; height: 100vh; overflow: hidden;
            position: fixed; top: 0; left: 0;
            background-color: #1e1e1e;
        }
        body {
            font-family: 'Segoe UI', Arial, sans-serif;
            display: flex; justify-content: center; align-items: flex-start;
            padding-top: 8vh;
            background-repeat: no-repeat; background-position: center;
            background-size: cover; background-attachment: fixed;
        }
        .container {
            width: 90%; max-width: 450px;
            background-color: #252526; padding: 25px;
            border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.5);
            border: 1px solid #3e3e42; z-index: 10;
        }
        h1 { color: #ffffff; margin-bottom: 10px; text-align: center; font-size: 28px; }
        .subtitle { text-align: center; color: #9d9d9d; margin-bottom: 25px; font-size: 14px; }
        .input-group { margin-bottom: 18px; }
        label { display: block; margin-bottom: 6px; color: #cccccc; font-weight: 600; font-size: 14px; }
        input[type="text"], input[type="tel"] {
            width: 100%; padding: 14px; font-size: 18px;
            border: 2px solid #3e3e42; border-radius: 8px;
            background-color: #2d2d30; color: #ffffff;
            transition: border-color 0.2s;
        }
        input:focus { border-color: #007acc; outline: none; background-color: #1e1e1e; }
        input::placeholder { color: #6e6e6e; }
        .optional { color: #6e6e6e; font-size: 12px; font-weight: normal; }
        button {
            background-color: #007acc; color: white;
            padding: 16px 24px; border: none; border-radius: 8px;
            cursor: pointer; font-size: 18px; width: 100%; font-weight: bold;
            transition: background-color 0.2s, transform 0.1s;
        }
        button:hover { background-color: #0098ff; }
        button:active { transform: scale(0.98); }
        button:disabled { background-color: #4e4e4e; cursor: not-allowed; }

        /* Success confirmation overlay */
        .success-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.9);
            z-index: 100;
            justify-content: center; align-items: center;
            flex-direction: column;
        }
        .success-overlay.active { display: flex; }
        .success-content {
            text-align: center;
            padding: 40px;
            animation: popIn 0.3s ease-out;
        }
        @keyframes popIn {
            from { transform: scale(0.8); opacity: 0; }
            to { transform: scale(1); opacity: 1; }
        }
        .success-icon {
            font-size: 80px;
            margin-bottom: 20px;
        }
        .success-name {
            font-size: 28px; color: #ffffff; font-weight: bold;
            margin-bottom: 15px;
        }
        .success-position {
            font-size: 64px; color: #007acc; font-weight: bold;
            margin-bottom: 10px;
        }
        .success-label {
            font-size: 18px; color: #9d9d9d;
            margin-bottom: 20px;
        }
        .success-wait {
            font-size: 24px; color: #4ec9b0;
            padding: 15px 30px;
            background: rgba(78, 201, 176, 0.15);
            border-radius: 8px;
            border: 1px solid #4ec9b0;
        }
        .success-note {
            margin-top: 30px;
            font-size: 14px;
            color: #6e6e6e;
        }

        .logo-container {
            text-align: center; margin-top: 20px; padding-top: 15px;
            border-top: 1px solid #3e3e42;
        }
        .logo-container img { max-width: 100%; height: auto; max-height: 120px; }

        .admin-link { text-align: center; margin-top: 15px; }
        .admin-link a { color: #6e6e6e; font-size: 11px; text-decoration: none; }
        .admin-link a:hover { color: #9d9d9d; text-decoration: underline; }

        .queue-count {
            text-align: center;
            padding: 12px;
            background: #2d2d30;
            border-radius: 8px;
            margin-bottom: 20px;
            color: #9d9d9d;
            font-size: 14px;
        }
        .queue-count strong { color: #007acc; font-size: 18px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎮 Join the Queue</h1>
        <p class="subtitle">Enter your name to get in line for a sim racing session</p>

        <!-- Current queue count -->
        <div class="queue-count" id="queueCount">
            <strong>0</strong> people waiting
        </div>

        <!-- Join Form -->
        <div id="formSection">
            <div class="input-group">
                <label for="guestName">Your Name</label>
                <input type="text" id="guestName" placeholder="Enter your name" required autocomplete="off">
            </div>
            <div class="input-group">
                <label for="guestPhone">Phone <span class="optional">(optional)</span></label>
                <input type="tel" id="guestPhone" placeholder="555-123-4567" autocomplete="off">
            </div>
            <button id="joinBtn" onclick="joinQueue()">Join Queue</button>
            <div id="errorMsg" style="color: #f48771; text-align: center; margin-top: 15px; display: none;"></div>
        </div>

        <div class="logo-container">
            <img src="/logo" alt="Logo" id="companyLogo">
        </div>

        <div class="admin-link">
            <a href="/admin">Staff Admin</a>
        </div>
    </div>

    <!-- Success Confirmation Overlay -->
    <div class="success-overlay" id="successOverlay">
        <div class="success-content">
            <div class="success-icon">✅</div>
            <div class="success-name" id="successName">You're in!</div>
            <div class="success-position" id="successPosition">#1</div>
            <div class="success-label">Your position in line</div>
            <div class="success-wait" id="successWait">~10 minutes</div>
            <div class="success-note">Listen for your name to be called</div>
        </div>
    </div>

    <script>
        let receiverUrl = null;

        // Get receiver URL from simulator
        async function getReceiverUrl() {
            if (receiverUrl) return true;
            try {
                const resp = await fetch('/api/get_receiver_url');
                const data = await resp.json();
                if (data.success && data.receiver_url) {
                    receiverUrl = data.receiver_url;
                    return true;
                }
            } catch (e) {
                console.error('Error getting receiver URL:', e);
            }
            return false;
        }

        // Update queue count display
        async function updateQueueCount() {
            if (!receiverUrl) {
                await getReceiverUrl();
            }
            if (!receiverUrl) return;

            try {
                const response = await fetch(receiverUrl + '/api/queue');
                const data = await response.json();
                if (data.success) {
                    const count = data.total_in_queue || 0;
                    document.getElementById('queueCount').innerHTML =
                        `<strong>${count}</strong> ${count === 1 ? 'person' : 'people'} waiting`;
                }
            } catch (e) {
                console.error('Error fetching queue:', e);
            }
        }

        async function joinQueue() {
            const nameInput = document.getElementById('guestName');
            const phoneInput = document.getElementById('guestPhone');
            const name = nameInput.value.trim();
            const phone = phoneInput.value.trim();
            const errorMsg = document.getElementById('errorMsg');
            const joinBtn = document.getElementById('joinBtn');

            if (!name) {
                errorMsg.textContent = 'Please enter your name';
                errorMsg.style.display = 'block';
                nameInput.focus();
                return;
            }

            // Get receiver URL first
            if (!receiverUrl) {
                const gotUrl = await getReceiverUrl();
                if (!gotUrl) {
                    errorMsg.textContent = 'Unable to connect to queue server. Please try again.';
                    errorMsg.style.display = 'block';
                    return;
                }
            }

            joinBtn.disabled = true;
            joinBtn.textContent = 'Joining...';
            errorMsg.style.display = 'none';

            try {
                const response = await fetch(receiverUrl + '/api/queue/join', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: name, phone: phone })
                });
                const data = await response.json();

                if (data.success) {
                    // Show success overlay
                    showSuccess(name, data.position, data.wait_display);

                    // Clear form for next person
                    nameInput.value = '';
                    phoneInput.value = '';

                    // Hide success after 4 seconds and reset
                    setTimeout(() => {
                        hideSuccess();
                        updateQueueCount();
                    }, 4000);
                } else {
                    errorMsg.textContent = data.error || 'Failed to join queue';
                    errorMsg.style.display = 'block';
                }
            } catch (e) {
                errorMsg.textContent = 'Connection error. Please try again.';
                errorMsg.style.display = 'block';
            }

            joinBtn.disabled = false;
            joinBtn.textContent = 'Join Queue';
        }

        function showSuccess(name, position, waitDisplay) {
            document.getElementById('successName').textContent = name + ', you\'re in!';
            document.getElementById('successPosition').textContent = '#' + position;
            document.getElementById('successWait').textContent = waitDisplay || 'Calculating...';
            document.getElementById('successOverlay').classList.add('active');
        }

        function hideSuccess() {
            document.getElementById('successOverlay').classList.remove('active');
            // Focus on name input for next person
            document.getElementById('guestName').focus();
        }

        // Click anywhere on success overlay to dismiss early
        document.getElementById('successOverlay').addEventListener('click', function() {
            hideSuccess();
            updateQueueCount();
        });

        // Load background image from settings
        function loadSettings() {
            fetch('/get_settings')
                .then(response => response.json())
                .then(data => {
                    if (data.background_image) {
                        document.body.style.backgroundImage = 'url("/backgrounds/' + data.background_image + '")';
                    }
                })
                .catch(e => console.error('Error loading settings:', e));
        }

        // Initialize
        loadSettings();
        getReceiverUrl().then(() => {
            updateQueueCount();
        });

        // Update queue count every 10 seconds
        setInterval(updateQueueCount, 10000);

        // Focus on name input on load
        document.getElementById('guestName').focus();
    </script>
</body>
</html>
"""

# Store the main window instance and UI bridge globally
main_window = None
ui_bridge = None

# Receiver URL cache
receiver_url_cache = None

@app.route('/')
def index():
    return HTML_CONTENT

@app.route('/admin')
def admin():
    return ADMIN_HTML_CONTENT

@app.route('/queue')
def queue_page():
    return QUEUE_HTML_CONTENT

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

@app.route('/api/get_receiver_url', methods=['GET'])
def get_receiver_url():
    """Get the receiver URL for queue API calls"""
    if main_window and hasattr(main_window, 'current_server_url'):
        url = main_window.current_server_url
        if url:
            # Handle comma-separated URLs - return just the first one
            first_url = url.split(',')[0].strip()
            return jsonify({'success': True, 'receiver_url': first_url})
    return jsonify({'success': False, 'error': 'Receiver URL not configured'})

@app.route('/api/session/start_remote', methods=['POST'])
def start_remote_session():
    """Start a session for an assigned driver (called by admin)"""
    global main_window, ui_bridge
    try:
        data = request.get_json() or {}
        driver_name = data.get('driver_name', '').strip()
        queue_id = data.get('queue_id', '')

        if not driver_name:
            return jsonify({'success': False, 'error': 'driver_name is required'}), 400

        if main_window:
            # Store queue ID for session tracking
            main_window.assigned_queue_id = queue_id

            # Use signal to update driver on UI thread (triggers session start)
            if ui_bridge:
                ui_bridge.driver_registered.emit(driver_name)
            else:
                main_window.update_driver_name(driver_name)

            return jsonify({'success': True, 'driver_name': driver_name})

        return jsonify({'success': False, 'error': 'Server not ready'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/session/force_end', methods=['POST'])
def force_end_session():
    """Force end the current session (called by admin)"""
    global main_window, ui_bridge
    try:
        if main_window:
            # Use signal to end session on UI thread
            if ui_bridge:
                ui_bridge.force_end_session.emit()
            else:
                main_window.end_session()

            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'Server not ready'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/simulator_info', methods=['GET'])
def simulator_info():
    """Expose simulator details for multi-simulator discovery"""
    if main_window:
        # Get timer info
        timer_active = (hasattr(main_window, 'rotation_mode_selector') and 
                       main_window.rotation_mode_selector.currentText() == 'Time' and
                       hasattr(main_window, 'timer_started') and 
                       main_window.timer_started)
        timer_seconds_left = 0
        if timer_active and hasattr(main_window, 'timer_end_timestamp'):
            timer_seconds_left = max(0, main_window.timer_end_timestamp - int(time.time()))
        
        # Get lap info
        laps_active = bool(getattr(main_window, 'lap_rotation_active', False))
        laps_progress = {
            'started': bool(getattr(main_window, 'lap_count_started', False)),
            'count': int(getattr(main_window, 'lap_rotation_count', 0)),
            'target': int(getattr(main_window, 'lap_rotation_target', 0)),
        }
        
        return jsonify({
            'simulator_id': int(main_window.simulator_id.currentText()),
            'simulator_name': f"Sim {main_window.simulator_id.currentText()}",
            'game': main_window.game_selector.currentText(),
            'current_driver': main_window.driver_name.text() or None,
            'ip': get_local_ip(),
            'port': 5001,
            'timer_active': timer_active,
            'timer_seconds_left': timer_seconds_left,
            'laps_active': laps_active,
            'laps_progress': laps_progress
        })
    return jsonify({
        'simulator_id': 1,
        'simulator_name': 'Sim 1',
        'game': 'Unknown',
        'current_driver': None,
        'ip': get_local_ip(),
        'port': 5001,
        'timer_active': False,
        'timer_seconds_left': 0,
        'laps_active': False,
        'laps_progress': {'started': False, 'count': 0, 'target': 0}
    })

@app.route('/api/discover_simulators', methods=['GET'])
def discover_simulators():
    """Discover all simulators on the network via UDP broadcast"""
    # Get current simulator info
    current_ip = get_local_ip()
    current_sim = {
        'simulator_id': 1,
        'simulator_name': 'Sim 1',
        'game': 'Unknown',
        'current_driver': None,
        'ip': current_ip,
        'port': 5001,
        'is_current': True
    }
    
    if main_window:
        # Get timer info for current sim
        timer_active = (hasattr(main_window, 'rotation_mode_selector') and 
                       main_window.rotation_mode_selector.currentText() == 'Time' and
                       hasattr(main_window, 'timer_started') and 
                       main_window.timer_started)
        timer_seconds_left = 0
        if timer_active and hasattr(main_window, 'timer_end_timestamp'):
            timer_seconds_left = max(0, main_window.timer_end_timestamp - int(time.time()))
        
        # Get lap info for current sim
        laps_active = bool(getattr(main_window, 'lap_rotation_active', False))
        laps_progress = {
            'started': bool(getattr(main_window, 'lap_count_started', False)),
            'count': int(getattr(main_window, 'lap_rotation_count', 0)),
            'target': int(getattr(main_window, 'lap_rotation_target', 0)),
        }
        
        current_sim = {
            'simulator_id': int(main_window.simulator_id.currentText()),
            'simulator_name': f"Sim {main_window.simulator_id.currentText()}",
            'game': main_window.game_selector.currentText(),
            'current_driver': main_window.driver_name.text() or None,
            'ip': current_ip,
            'port': 5001,
            'is_current': True,
            'timer_active': timer_active,
            'timer_seconds_left': timer_seconds_left,
            'laps_active': laps_active,
            'laps_progress': laps_progress
        }
    
    # Discover other simulators
    discovered = SimulatorDiscovery.discover()
    
    # Mark which is current and filter out self
    simulators = [current_sim]
    for sim in discovered:
        if sim['ip'] != current_ip:
            sim['is_current'] = False
            simulators.append(sim)
    
    # Sort by simulator_id
    simulators.sort(key=lambda x: x.get('simulator_id', 0))
    
    return jsonify({
        'simulators': simulators,
        'current_ip': current_ip
    })

@app.route('/api/known_simulators', methods=['GET'])
def get_known_simulators_api():
    """Get list of known/configured simulators"""
    simulators = load_known_simulators()
    return jsonify({'known_simulators': simulators})

@app.route('/api/known_simulators', methods=['POST'])
def add_known_simulator_api():
    """Add a simulator to the known list"""
    data = request.json
    ip = data.get('ip', '').strip()
    name = data.get('name', '').strip() or f'Sim ({ip})'
    
    if not ip:
        return jsonify({'success': False, 'error': 'IP address required'}), 400
    
    # Basic IP validation
    import re
    if not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip):
        return jsonify({'success': False, 'error': 'Invalid IP address format'}), 400
    
    success = add_known_simulator(ip, name)
    if success:
        return jsonify({'success': True, 'message': f'Added {name}'})
    else:
        return jsonify({'success': False, 'error': 'Simulator already exists'}), 400

@app.route('/api/known_simulators/<ip>', methods=['DELETE'])
def remove_known_simulator_api(ip):
    """Remove a simulator from the known list"""
    success = remove_known_simulator(ip)
    return jsonify({'success': success})

@app.route('/api/test_simulator/<ip>', methods=['GET'])
def test_simulator_connection(ip):
    """Test connection to a specific simulator"""
    try:
        response = requests.get(
            f'http://{ip}:5001/api/simulator_info',
            timeout=2.0
        )
        if response.ok:
            data = response.json()
            return jsonify({
                'reachable': True,
                'simulator_info': data
            })
    except Exception as e:
        pass
    return jsonify({'reachable': False, 'error': 'Could not connect'})

@app.route('/api/beamng_lap', methods=['POST'])
def receive_beamng_lap():
    """Receive lap data directly from BeamNG Lua mod - instant, no log parsing needed"""
    global main_window
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data received'}), 400
        
        lap_time = data.get('lap_time')
        vehicle = data.get('vehicle', 'unknown')
        track = data.get('track', 'unknown')
        valid = data.get('valid', True)
        
        if lap_time is None or lap_time <= 0:
            return jsonify({'success': False, 'error': 'Invalid lap time'}), 400
        
        # Validate lap time bounds
        if not validate_lap_time(lap_time, "BeamNG direct"):
            return jsonify({'success': False, 'error': 'Lap time out of valid range'}), 400
        
        if not valid:
            print(f"BeamNG direct: Ignoring invalid lap {lap_time:.3f}s")
            return jsonify({'success': False, 'error': 'Lap marked as invalid'}), 400
        
        print(f"\n=== BeamNG Direct Lap Received ===")
        print(f"Track: {track}")
        print(f"Vehicle: {vehicle}")
        print(f"Lap Time: {lap_time:.3f}s")
        
        # Get driver name from main window
        driver_name = ""
        if main_window:
            driver_name = main_window.driver_name.text()
            print(f"Driver: {driver_name}")
            # Emit signal to trigger lap processing (thread-safe via signal)
            if hasattr(main_window, 'current_monitor') and main_window.current_monitor:
                main_window.current_monitor.lap_completed.emit(lap_time, driver_name)
            else:
                # Fallback: call directly if no monitor running
                main_window.on_lap_completed(lap_time, driver_name)
        
        return jsonify({'success': True, 'lap_time': lap_time})
    except Exception as e:
        print(f"Error receiving BeamNG lap: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manual_lap', methods=['POST'])
def manual_lap():
    """Receive manually entered lap time from mobile interface"""
    global main_window, ui_bridge
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data received'}), 400
        
        lap_time = data.get('lap_time')
        driver_name = data.get('driver_name', '').strip()
        
        # Validation
        if not driver_name:
            return jsonify({'success': False, 'message': 'Driver name is required'}), 400
        
        if lap_time is None:
            return jsonify({'success': False, 'message': 'Lap time is required'}), 400
        
        # Convert to float and validate
        try:
            lap_time = float(lap_time)
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'Invalid lap time format'}), 400
        
        # Validate lap time bounds
        if not validate_lap_time(lap_time, "Manual Entry"):
            return jsonify({'success': False, 'message': 'Lap time must be between 5 seconds and 60 minutes'}), 400
        
        print(f"\n=== Manual Lap Entry Received ===")
        print(f"Driver: {driver_name}")
        print(f"Lap Time: {lap_time:.3f}s")
        
        # Send to main window via signal (thread-safe - signals work across threads)
        if main_window and ui_bridge:
            # Emit signal to trigger lap submission on the UI thread
            ui_bridge.manual_lap_submitted.emit(lap_time, driver_name)
            formatted_time = format_time_ms(int(lap_time * 1000))
            print(f"Signal emitted to send lap time to leaderboard server")
            return jsonify({'success': True, 'lap_time': lap_time, 'formatted_time': formatted_time})
        else:
            print("ERROR: main_window or ui_bridge not available")
            return jsonify({'success': False, 'message': 'Server not ready'}), 500
            
    except Exception as e:
        print(f"Error receiving manual lap: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

def run_flask():
    app.run(host='0.0.0.0', port=5001)

def run_beamng_udp_listener():
    """Listen for direct UDP lap data from BeamNG Lua mod - instant lap detection"""
    import socket
    
    UDP_PORT = 5002
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        sock.bind(('0.0.0.0', UDP_PORT))
        print(f"[UDP] BeamNG lap listener started on port {UDP_PORT}")
    except Exception as e:
        print(f"[UDP] Failed to bind port {UDP_PORT}: {e}")
        return
    
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            message = data.decode('utf-8').strip()
            
            # Format: LAP|time|vehicle|track|valid|timestamp
            if message.startswith("LAP|"):
                parts = message.split("|")
                if len(parts) >= 5:
                    lap_time = float(parts[1])
                    vehicle = parts[2]
                    track = parts[3]
                    valid = parts[4] == "1"
                    
                    if not valid:
                        print(f"[UDP] Ignoring invalid lap: {lap_time:.3f}s")
                        continue
                    
                    # UDP laps are pre-validated (finish line only, individual deltas)
                    # Skip validation since BeamNG can have very short tracks (< 15s)
                    
                    print(f"\n=== BeamNG UDP Lap Received ===")
                    print(f"Track: {track}")
                    print(f"Vehicle: {vehicle}")
                    print(f"Lap Time: {lap_time:.3f}s")
                    
                    # Process the lap
                    if main_window:
                        driver_name = main_window.driver_name.text()
                        print(f"Driver: {driver_name}")
                        # Use Qt signal for thread-safe UI update
                        if hasattr(main_window, 'current_monitor') and main_window.current_monitor:
                            main_window.current_monitor.lap_completed.emit(lap_time, driver_name)
                        else:
                            # Direct call if no monitor
                            from PyQt6.QtCore import QMetaObject, Qt, Q_ARG
                            QMetaObject.invokeMethod(main_window, "on_lap_completed", 
                                Qt.ConnectionType.QueuedConnection,
                                Q_ARG(float, lap_time), Q_ARG(str, driver_name))
        except Exception as e:
            print(f"[UDP] Error: {e}")

class GameMonitor(QThread):
    lap_completed = pyqtSignal(float, str)  # Signal emitted when a valid lap is completed (lap_time, driver_name)
    connection_status = pyqtSignal(str, str)  # Signal for status updates (message, color)
    game_name = "Unknown Game"
    
    def __init__(self):
        super().__init__()
        self.running = True
        self.last_lap_time = 0
        self.is_connected = False
    
    def get_current_driver(self):
        """Get the current driver name from main window"""
        if main_window and hasattr(main_window, 'driver_name'):
            return main_window.driver_name.text() or ""
        return ""
        
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
                        driver = self.get_current_driver()
                        self.lap_completed.emit(lap_time, driver)
                    
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
    manual_lap_submitted = pyqtSignal(float, str)  # lap_time, driver_name
    force_end_session = pyqtSignal()  # Force end current session

class SimulatorDiscovery:
    """Discovers other simulators via HTTP probe on port 5001"""
    PROBE_TIMEOUT = 1.0  # 1 second per host (increased for reliability)
    KNOWN_TIMEOUT = 2.0  # 2 seconds for known simulators (higher priority)
    MAX_WORKERS = 50     # Parallel connections
    CACHE_TTL = 5        # seconds (reduced - UDP discovery is primary now)
    
    _cache = []
    _cache_time = 0
    
    @staticmethod
    def probe_host(ip, timeout=1.0):
        """Probe a single host for simulator"""
        try:
            response = requests.get(
                f'http://{ip}:5001/api/simulator_info',
                timeout=timeout
            )
            if response.ok:
                data = response.json()
                data['ip'] = ip
                data['port'] = data.get('port', 5001)
                data['reachable'] = True
                return data
        except:
            pass
        return None
    
    @staticmethod
    def discover():
        """Scan local subnet and known simulators via HTTP"""
        from concurrent.futures import ThreadPoolExecutor
        
        # Return cached results if still valid
        if SimulatorDiscovery._cache and (time.time() - SimulatorDiscovery._cache_time) < SimulatorDiscovery.CACHE_TTL:
            return SimulatorDiscovery._cache
        
        local_ip = get_local_ip()
        subnet_prefix = '.'.join(local_ip.split('.')[:-1])  # e.g., "192.168.0"
        
        discovered = {}
        
        # First, probe known simulators with higher timeout (priority)
        known_sims = load_known_simulators()
        for known in known_sims:
            ip = known.get('ip')
            if ip:
                result = SimulatorDiscovery.probe_host(ip, SimulatorDiscovery.KNOWN_TIMEOUT)
                if result:
                    discovered[ip] = result
                else:
                    # Add as unreachable so it still appears in the list
                    discovered[ip] = {
                        'ip': ip,
                        'port': known.get('port', 5001),
                        'simulator_name': known.get('name', f'Sim ({ip})'),
                        'simulator_id': 99,
                        'reachable': False,
                        'is_known': True
                    }
        
        # Generate list of IPs to scan (excluding .0 and .255)
        ips_to_scan = [f"{subnet_prefix}.{i}" for i in range(1, 255)]
        # Exclude already-probed known simulators
        known_ips = set(k.get('ip') for k in known_sims)
        ips_to_scan = [ip for ip in ips_to_scan if ip not in known_ips]
        
        try:
            # Scan subnet in parallel
            with ThreadPoolExecutor(max_workers=SimulatorDiscovery.MAX_WORKERS) as executor:
                results = list(executor.map(
                    lambda ip: SimulatorDiscovery.probe_host(ip, SimulatorDiscovery.PROBE_TIMEOUT),
                    ips_to_scan
                ))
            
            for result in results:
                if result and result.get('ip'):
                    discovered[result['ip']] = result
                    
        except Exception as e:
            print(f"SimulatorDiscovery error: {e}")
        
        # Update cache
        SimulatorDiscovery._cache = list(discovered.values())
        SimulatorDiscovery._cache_time = time.time()
        
        return SimulatorDiscovery._cache


# ============================================================================
# mDNS Service - Allows iPads to connect via sim1.local, sim2.local, etc.
# ============================================================================

class MDNSService:
    """Advertises this simulator via mDNS so devices can connect using friendly names.

    Each simulator gets a unique name based on its ID:
    - Sim 1 → sim1.local:5001
    - Sim 2 → sim2.local:5001
    - etc.

    Drivers can type 'sim1.local:5001' instead of IP addresses.
    """

    def __init__(self):
        self.zeroconf = None
        self.service_info = None
        self.current_sim_id = None

    def start(self, simulator_id, port=5001):
        """Start advertising this simulator via mDNS.

        Args:
            simulator_id: The simulator number (1, 2, 3, etc.)
            port: The web interface port (default 5001)
        """
        try:
            # Stop existing service if running
            self.stop()

            self.current_sim_id = simulator_id
            local_ip = get_local_ip()

            # Create service name: sim1, sim2, etc.
            service_name = f"sim{simulator_id}"

            # Create the mDNS service info
            # Service type is _http._tcp for web services
            self.service_info = ServiceInfo(
                "_http._tcp.local.",
                f"{service_name}._http._tcp.local.",
                addresses=[socket.inet_aton(local_ip)],
                port=port,
                properties={
                    'path': '/',
                    'simulator_id': str(simulator_id),
                    'app': 'Lap Time Sender'
                },
                server=f"{service_name}.local."
            )

            # Start zeroconf and register the service
            self.zeroconf = Zeroconf()
            self.zeroconf.register_service(self.service_info)

            print(f"[mDNS] Service registered: http://{service_name}.local:{port}")
            return f"{service_name}.local"

        except Exception as e:
            print(f"[mDNS] Failed to start service: {e}")
            return None

    def stop(self):
        """Stop the mDNS service."""
        try:
            if self.zeroconf and self.service_info:
                self.zeroconf.unregister_service(self.service_info)
                self.zeroconf.close()
                print("[mDNS] Service stopped")
        except Exception as e:
            print(f"[mDNS] Error stopping service: {e}")
        finally:
            self.zeroconf = None
            self.service_info = None

    def update_simulator_id(self, new_simulator_id, port=5001):
        """Update the mDNS service when simulator ID changes."""
        if new_simulator_id != self.current_sim_id:
            return self.start(new_simulator_id, port)
        return f"sim{self.current_sim_id}.local" if self.current_sim_id else None


# Global mDNS service instance
mdns_service = MDNSService()


# ============================================================================
# UDP Broadcast Discovery - Real-time simulator discovery
# ============================================================================
DISCOVERY_PORT = 5003
DISCOVERY_MAGIC = b'LBSIM01'  # Leaderboard Simulator Discovery v1

# Receiver discovery constants (matches Receiver's DiscoveryResponder protocol)
RECEIVER_DISCOVERY_PORT = 5001
RECEIVER_DISCOVERY_REQUEST = b"LEADERBOARD_DISCOVER"
RECEIVER_DISCOVERY_RESPONSE_PREFIX = "LEADERBOARD_SERVER:"


class ReceiverDiscoveryThread(QThread):
    """Background thread that automatically discovers ALL leaderboard receivers on the network.

    Protocol:
    - Broadcasts LEADERBOARD_DISCOVER on UDP port 5001
    - Receivers respond with LEADERBOARD_SERVER:{ip}:{port}
    - Tracks all discovered receivers and keeps them updated
    - Automatically reconnects when receivers restart
    """
    receivers_updated = pyqtSignal(list)  # Emits list of all active receiver URLs
    discovery_status = pyqtSignal(str)  # Status messages for UI feedback

    # How long before a receiver is considered offline (seconds)
    RECEIVER_TIMEOUT = 45

    def __init__(self, discovery_interval=10):
        super().__init__()
        self._running = True
        self.discovery_interval = discovery_interval  # Seconds between discovery broadcasts
        self.local_ip = get_local_ip()
        # Track all known receivers: {url: last_seen_timestamp}
        self._known_receivers = {}
        self._lock = threading.Lock()

    def stop(self):
        self._running = False

    def get_active_receivers(self):
        """Return list of currently active receiver URLs"""
        with self._lock:
            now = time.time()
            return [url for url, last_seen in self._known_receivers.items()
                    if now - last_seen < self.RECEIVER_TIMEOUT]

    def run(self):
        """Continuously broadcast discovery requests and track all receivers"""
        print("[Receiver Discovery] Starting automatic receiver discovery (multi-receiver mode)...")
        self.discovery_status.emit("Searching for leaderboard receivers...")

        # Fast discovery until we find at least one receiver
        FAST_INTERVAL = 3  # seconds - aggressive search at startup
        NORMAL_INTERVAL = 10  # seconds - once connected, check less frequently
        search_attempt = 0

        while self._running:
            try:
                # Discover all receivers that respond
                found_receivers = self._discover_all_receivers()

                # Update known receivers with new timestamps
                now = time.time()
                receivers_changed = False

                with self._lock:
                    # Add/update found receivers
                    for url in found_receivers:
                        if url not in self._known_receivers:
                            print(f"[Receiver Discovery] New receiver found: {url}")
                            receivers_changed = True
                        self._known_receivers[url] = now

                    # NOTE: We do NOT remove receivers based on UDP discovery timeout.
                    # A receiver might have UDP blocked but HTTP working fine.
                    # Receivers are only removed if HTTP POST fails consistently,
                    # which is handled by the lap time retry mechanism.

                    active_receivers = list(self._known_receivers.keys())
                    has_receivers = len(active_receivers) > 0

                # Only emit signal when receivers actually change (added or removed)
                if receivers_changed:
                    if active_receivers:
                        self.receivers_updated.emit(active_receivers)
                        self.discovery_status.emit(f"Connected to {len(active_receivers)} receiver(s)")

                # Show searching status if no receivers found yet
                if not has_receivers:
                    search_attempt += 1
                    self.discovery_status.emit(f"Searching for receivers... (attempt {search_attempt})")

                # Use fast interval until we find receivers, then slow down
                sleep_time = NORMAL_INTERVAL if has_receivers else FAST_INTERVAL
                time.sleep(sleep_time)

            except Exception as e:
                print(f"[Receiver Discovery] Error: {e}")
                time.sleep(FAST_INTERVAL)  # Keep trying fast on errors

        print("[Receiver Discovery] Stopped")

    def _discover_all_receivers(self):
        """Send discovery broadcast and collect ALL responses.

        Returns:
            list: List of receiver URLs found (e.g., ["http://192.168.1.100:5000", "http://192.168.1.101:5000"])
        """
        found = []
        sock = None
        try:
            # Create UDP socket for broadcast
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.5)  # Short timeout for each recv attempt

            # Bind to any available port to receive responses
            sock.bind(('', 0))

            # Get broadcast address for the local subnet
            subnet_prefix = '.'.join(self.local_ip.split('.')[:-1])
            broadcast_addr = f"{subnet_prefix}.255"

            # Send discovery request to subnet broadcast
            sock.sendto(RECEIVER_DISCOVERY_REQUEST, (broadcast_addr, RECEIVER_DISCOVERY_PORT))

            # Also try direct broadcast to 255.255.255.255 for cross-subnet discovery
            try:
                sock.sendto(RECEIVER_DISCOVERY_REQUEST, ('255.255.255.255', RECEIVER_DISCOVERY_PORT))
            except:
                pass  # Some systems don't allow this

            # Collect all responses for 2 seconds
            start_time = time.time()
            seen_ips = set()  # Avoid duplicates from same receiver

            while time.time() - start_time < 2.0:
                try:
                    data, addr = sock.recvfrom(1024)
                    sender_ip = addr[0]

                    # Skip duplicates from same IP
                    if sender_ip in seen_ips:
                        continue

                    response = data.decode('utf-8')
                    if response.startswith(RECEIVER_DISCOVERY_RESPONSE_PREFIX):
                        # Parse response: "LEADERBOARD_SERVER:192.168.1.100:5000"
                        server_info = response[len(RECEIVER_DISCOVERY_RESPONSE_PREFIX):]
                        url = f"http://{server_info}"
                        found.append(url)
                        seen_ips.add(sender_ip)

                except socket.timeout:
                    continue  # Keep trying until 2 seconds elapsed
                except Exception as e:
                    print(f"[Receiver Discovery] Response error: {e}")
                    continue

            if found:
                print(f"[Receiver Discovery] Found {len(found)} receiver(s): {', '.join(found)}")

            return found

        except Exception as e:
            print(f"[Receiver Discovery] Broadcast error: {e}")
            return []
        finally:
            if sock:
                sock.close()


class SimulatorBroadcastListener(QThread):
    """Background thread that listens for UDP broadcast announcements from other simulators"""
    simulator_discovered = pyqtSignal(dict)  # Emitted when a new/updated simulator is found
    
    def __init__(self):
        super().__init__()
        self._running = True
        self._known_simulators = {}  # ip -> last_seen_time
        self.local_ip = get_local_ip()
    
    def stop(self):
        self._running = False
        
    def run(self):
        """Listen for UDP broadcast announcements"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)  # Allow checking _running flag periodically
        
        try:
            sock.bind(('0.0.0.0', DISCOVERY_PORT))
            print(f"[Discovery] UDP listener started on port {DISCOVERY_PORT}")
        except Exception as e:
            print(f"[Discovery] Failed to bind UDP port {DISCOVERY_PORT}: {e}")
            return
        
        while self._running:
            try:
                data, addr = sock.recvfrom(1024)
                sender_ip = addr[0]
                
                # Ignore our own broadcasts
                if sender_ip == self.local_ip:
                    continue
                
                # Validate magic header
                if not data.startswith(DISCOVERY_MAGIC):
                    continue
                
                # Parse the JSON payload after the magic header
                try:
                    payload = json.loads(data[len(DISCOVERY_MAGIC):].decode('utf-8'))
                    payload['ip'] = sender_ip
                    payload['last_seen'] = time.time()
                    payload['reachable'] = True
                    
                    # Check if this is a new or updated simulator
                    existing = self._known_simulators.get(sender_ip)
                    is_new = existing is None
                    is_updated = (existing and 
                                  (existing.get('game') != payload.get('game') or
                                   existing.get('current_driver') != payload.get('current_driver') or
                                   existing.get('simulator_name') != payload.get('simulator_name')))
                    
                    self._known_simulators[sender_ip] = payload
                    
                    if is_new or is_updated:
                        print(f"[Discovery] {'New' if is_new else 'Updated'} simulator: {payload.get('simulator_name')} at {sender_ip}")
                        self.simulator_discovered.emit(payload)
                        
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    print(f"[Discovery] Invalid payload from {sender_ip}: {e}")
                    
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    print(f"[Discovery] Listener error: {e}")
        
        sock.close()
        print("[Discovery] UDP listener stopped")
    
    def get_known_simulators(self):
        """Return list of known simulators (active within last 15 seconds)"""
        now = time.time()
        active = []
        for ip, sim in list(self._known_simulators.items()):
            if now - sim.get('last_seen', 0) < 15:  # 15 second timeout
                active.append(sim)
            else:
                del self._known_simulators[ip]  # Remove stale entries
        return active


def broadcast_simulator_presence():
    """Broadcast this simulator's presence to the network"""
    global main_window
    
    try:
        local_ip = get_local_ip()
        
        # Build simulator info payload
        payload = {
            'simulator_id': 1,
            'simulator_name': 'Sim 1',
            'game': 'Unknown',
            'current_driver': None,
            'port': 5001
        }
        
        if main_window:
            try:
                payload['simulator_id'] = int(main_window.simulator_id.currentText())
                payload['simulator_name'] = f"Sim {main_window.simulator_id.currentText()}"
                payload['game'] = main_window.game_selector.currentText()
                payload['current_driver'] = main_window.driver_name.text() or None
            except:
                pass
        
        # Create the broadcast packet
        message = DISCOVERY_MAGIC + json.dumps(payload).encode('utf-8')
        
        # Send UDP broadcast
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        
        # Broadcast to subnet
        subnet_prefix = '.'.join(local_ip.split('.')[:-1])
        broadcast_addr = f"{subnet_prefix}.255"
        
        sock.sendto(message, (broadcast_addr, DISCOVERY_PORT))
        sock.close()
        
    except Exception as e:
        print(f"[Discovery] Broadcast error: {e}")


def start_discovery_heartbeat():
    """Start a background thread that broadcasts presence every 3 seconds"""
    def heartbeat_loop():
        while True:
            broadcast_simulator_presence()
            time.sleep(3)  # Broadcast every 3 seconds
    
    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()
    print("[Discovery] Heartbeat broadcaster started (every 3s)")
    return thread


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
                        print(f"[OK] Valid lap time recorded: {last_time:.3f}s")
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
                    print("[OK] Physics handle closed")
                except (ValueError, OSError) as e:
                    print(f"Physics handle already closed: {e}")
                finally:
                    self.physics_handle = None
                    
            if hasattr(self, 'graphics_handle') and self.graphics_handle:
                try:
                    self.graphics_handle.close()
                    print("[OK] Graphics handle closed")
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
    lap_completed = pyqtSignal(float, str)  # (lap_time, driver_name)
    connection_status = pyqtSignal(str, str)
    
    def __init__(self):
        super().__init__()
        self.ir = irsdk.IRSDK()
        self.state = State()  # Use the exact State class
        self.running = True
        self.last_sent_lap_time = None  # Track the last lap time we sent
    
    def get_current_driver(self):
        """Get the current driver name from main window"""
        if main_window and hasattr(main_window, 'driver_name'):
            return main_window.driver_name.text() or ""
        return ""
        
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
            driver = self.get_current_driver()
            self.lap_completed.emit(float(last_lap_time), driver)

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
                print("[OK] iRacing SDK disconnected")
        except Exception as e:
            print(f"Error shutting down iRacing SDK: {e}")
        self.wait()

class BeamNGMonitor(GameMonitor):
    game_name = "BeamNG.drive"
    
    def __init__(self):
        super().__init__()
        self.log_file_path = None
        self.last_timestamp = 0
        self.file_found = False
        self.process_detected = False
        self.log_position = 0  # Track where we are in the log file
        self.log_paths = self._build_log_paths()
        self.pending_laps = []  # Queue for multiple laps found in one read
        self._current_lap_driver = ""  # Driver name captured when lap was detected
        
    def _build_log_paths(self):
        """Build list of possible BeamNG log file locations"""
        paths = []
        
        # Correct path: AppData\Local\BeamNG\BeamNG.drive (note the BeamNG parent folder)
        beamng_base = Path.home() / "AppData" / "Local" / "BeamNG" / "BeamNG.drive"
        
        print(f"\n=== BeamNG Log Path Detection ===")
        print(f"Checking base folder: {beamng_base}")
        print(f"Base folder exists: {beamng_base.exists()}")
        
        if beamng_base.exists():
            # List contents of beamng_base for debugging
            try:
                contents = list(beamng_base.iterdir())
                print(f"Contents: {[item.name for item in contents]}")
            except Exception as e:
                print(f"Error listing contents: {e}")
            
            # Check "current" folder first (symlink/junction to active version)
            current_folder = beamng_base / "current"
            current_log = current_folder / "beamng.log"
            print(f"Current folder: {current_folder}")
            print(f"Current folder exists: {current_folder.exists()}")
            print(f"Log file path: {current_log}")
            print(f"Log file exists: {current_log.exists()}")
            
            paths.append(current_log)
            
            # Also scan for version folders
            try:
                for item in beamng_base.iterdir():
                    if item.is_dir() and item.name[0].isdigit():
                        log_file = item / "beamng.log"
                        if log_file not in paths:
                            paths.append(log_file)
                            print(f"Added version log: {log_file} (exists: {log_file.exists()})")
            except Exception as e:
                print(f"Error scanning BeamNG folders: {e}")
            
            # Root log file
            root_log = beamng_base / "beamng.log"
            if root_log not in paths:
                paths.append(root_log)
        else:
            print(f"ERROR: BeamNG base folder not found!")
        
        print(f"Total paths to check: {len(paths)}")
        print("=== End Path Detection ===\n")
        
        return paths
    
    def _is_beamng_running(self):
        """Check if BeamNG.drive process is running"""
        try:
            import subprocess
            result = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq BeamNG.drive.x64.exe', '/NH'],
                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW
            )
            return 'BeamNG.drive.x64.exe' in result.stdout
        except:
            return False
    
    def connect_to_game(self):
        # Check if BeamNG process is running
        if self._is_beamng_running():
            if not self.process_detected:
                print("BeamNG.drive process detected!")
                self.process_detected = True
                # Print the paths we're checking on first detection
                print(f"Checking {len(self.log_paths)} potential log file locations...")
            
            # Find the log file
            for log_path in self.log_paths:
                if log_path.exists():
                    if self.log_file_path != log_path:
                        self.log_file_path = log_path
                        self.log_position = log_path.stat().st_size  # Start from end of file
                        print(f"BeamNG log file found: {log_path}")
                        print(f"Starting from position: {self.log_position}")
                        self.file_found = True
                    return True, self.log_file_path
            
            # No log found yet - print debug info once
            if not self.file_found and self.process_detected:
                print("WARNING: BeamNG process running but no log file found!")
                print("Checked paths:")
                for p in self.log_paths[:5]:  # Show first 5 paths
                    print(f"  - {p}")
                self.file_found = True  # Prevent repeated warnings
            
            return True, None
        
        self.process_detected = False
        return False, None
    
    def read_lap_time(self, log_path):
        """Read lap data from BeamNG log file - queues all laps found"""
        try:
            # First check if we have pending laps from previous read
            if self.pending_laps:
                lap = self.pending_laps.pop(0)
                print(f"\n=== BeamNG Lap Completed (Highscore) ===")
                print(f"Track: {lap['track']}")
                print(f"Vehicle: {lap['vehicle']}")
                print(f"Lap Time: {lap['formatted_time']} ({lap['lap_time']:.3f}s)")
                print(f"Driver (captured): {lap.get('driver', 'unknown')}")
                print(f"[From queue: {len(self.pending_laps)} laps remaining]")
                # Store the driver for this lap to be used by run()
                self._current_lap_driver = lap.get('driver', '')
                return True, lap['lap_time']
            
            if not log_path or not log_path.exists():
                return False, 0
            
            # Read new content from log file
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(self.log_position)
                new_content = f.read()
                self.log_position = f.tell()
            
            if not new_content:
                return False, 0
            
            lines = new_content.split('\n')
            
            # Parse native BeamNG highscore entries and collect ALL laps
            if "core_highscores.highscores|" in new_content and "Writing Highscore" in new_content:
                highscore_data = {}
                in_highscore_block = False
                track_info = ""
                
                for line in lines:
                    # Check for start of highscore block
                    if "Writing Highscore for" in line and "core_highscores.highscores|" in line:
                        in_highscore_block = True
                        highscore_data = {}
                        # Extract track info from header
                        try:
                            track_part = line.split("Writing Highscore for")[1].split("=")[0].strip()
                            track_info = track_part
                        except:
                            track_info = "unknown"
                    # Check for end of highscore block - just look for closing brace
                    elif in_highscore_block and "}" in line and "=" not in line:
                        in_highscore_block = False
                        # Process the completed highscore entry
                        if highscore_data:
                            timestamp = highscore_data.get('timestamp', 0)
                            if timestamp and timestamp > self.last_timestamp:
                                time_in_millis = highscore_data.get('time_in_millis', 0)
                                if time_in_millis > 0:
                                    lap_time = time_in_millis / 1000.0  # Convert ms to seconds
                                    if validate_lap_time(lap_time, "BeamNG"):
                                        self.last_timestamp = timestamp
                                        vehicle = highscore_data.get('vehicle_name', 'unknown')
                                        formatted_time = highscore_data.get('formatted_time', f"{lap_time:.3f}s")
                                        
                                        # Add ALL laps to the queue with current driver
                                        current_driver = self.get_current_driver()
                                        self.pending_laps.append({
                                            'lap_time': lap_time,
                                            'track': track_info,
                                            'vehicle': vehicle,
                                            'formatted_time': formatted_time,
                                            'driver': current_driver
                                        })
                    # Parse data lines within the highscore block
                    elif in_highscore_block and "=" in line:
                        try:
                            # Extract the part after the log prefix
                            if "|" in line:
                                # Get everything after the last pipe character
                                content = line.split("|")[-1].strip()
                            else:
                                content = line.strip()
                            
                            if "=" in content:
                                key_part, value_part = content.split("=", 1)
                                key = key_part.strip().lower()
                                value = value_part.strip().rstrip(",")
                                
                                if key == "timeinmillis":
                                    highscore_data['time_in_millis'] = int(value)
                                elif key == "timestamp":
                                    highscore_data['timestamp'] = int(value)
                                elif key == "vehiclename":
                                    highscore_data['vehicle_name'] = value.strip('"')
                                elif key == "formattedtime":
                                    highscore_data['formatted_time'] = value.strip('"')
                        except Exception as parse_err:
                            pass  # Skip malformed lines
                
                # Return the first lap from queue if any were found
                if self.pending_laps:
                    lap = self.pending_laps.pop(0)
                    print(f"\n=== BeamNG Lap Completed (Highscore) ===")
                    print(f"Track: {lap['track']}")
                    print(f"Vehicle: {lap['vehicle']}")
                    print(f"Lap Time: {lap['formatted_time']} ({lap['lap_time']:.3f}s)")
                    print(f"Driver (captured): {lap.get('driver', 'unknown')}")
                    if self.pending_laps:
                        print(f"[{len(self.pending_laps)} more laps queued]")
                    # Store the driver for this lap to be used by run()
                    self._current_lap_driver = lap.get('driver', '')
                    return True, lap['lap_time']
            
            # Method 2: Parse custom mod LAP_COMPLETED entries (fallback)
            if "=== LAP_COMPLETED ===" in new_content:
                lap_data = {}
                
                in_lap_block = False
                for line in lines:
                    if "=== LAP_COMPLETED ===" in line:
                        in_lap_block = True
                        lap_data = {}
                    elif "=== LAP_END ===" in line:
                        in_lap_block = False
                        # Process the completed lap
                        if lap_data:
                            timestamp = lap_data.get('timestamp', 0)
                            if timestamp and timestamp > self.last_timestamp:
                                lap_time = lap_data.get('lap_time', 0)
                                if lap_time > 0 and validate_lap_time(lap_time, "BeamNG"):
                                    self.last_timestamp = timestamp
                                    vehicle = lap_data.get('vehicle', 'unknown')
                                    track = lap_data.get('track', 'unknown')
                                    valid = lap_data.get('valid', 'false') == 'true'
                                    
                                    print(f"\n=== BeamNG Lap Completed (Mod) ===")
                                    print(f"Vehicle: {vehicle}")
                                    print(f"Track: {track}")
                                    print(f"Lap Time: {lap_time:.3f}s")
                                    print(f"Valid: {valid}")
                                    return True, lap_time
                    elif in_lap_block and "|I|GELua.leaderboardSender.LeaderboardSender|" in line:
                        # Parse data lines
                        content = line.split("|I|GELua.leaderboardSender.LeaderboardSender|", 1)
                        if len(content) == 2:
                            data_line = content[1].strip()
                            if ':' in data_line:
                                key, value = data_line.split(':', 1)
                                key = key.strip().lower()
                                value = value.strip()
                                
                                if key == 'timestamp':
                                    lap_data['timestamp'] = int(value)
                                elif key == 'lap_time':
                                    lap_data['lap_time'] = float(value)
                                elif key == 'vehicle':
                                    lap_data['vehicle'] = value
                                elif key == 'track':
                                    lap_data['track'] = value
                                elif key == 'valid':
                                    lap_data['valid'] = value
            
            return False, 0
        except Exception as e:
            print(f"Error reading BeamNG log: {e}")
            return False, 0
    
    def run(self):
        """Override base class run() - BeamNG uses UDP for lap detection, log only for connection status"""
        try:
            while self.running:
                try:
                    if not self.is_connected:
                        self.connection_status.emit(f"Attempting to connect to {self.game_name}...", "orange")
                    
                    success, data_handle = self.connect_to_game()
                    if not success:
                        self.connection_status.emit(f"{self.game_name} not running", "orange")
                        time.sleep(1)
                        continue
                    
                    if not self.is_connected:
                        self.is_connected = True
                        self.connection_status.emit(f"Connected to {self.game_name}! Laps via UDP on port 5002", "green")
                    
                    # NOTE: Log parsing disabled - using UDP for lap detection (port 5002)
                    # The Lua mod sends laps directly via UDP with accurate individual lap times
                    # Log parsing was unreliable due to:
                    # 1. Batched/delayed log writes
                    # 2. Cumulative times instead of individual laps
                    # 3. Checkpoint crossings mixed with actual laps
                    
                    self.msleep(2000)  # Just check connection status every 2 seconds
                    
                except Exception as e:
                    if self.is_connected:
                        self.is_connected = False
                        self.connection_status.emit(f"Lost connection to {self.game_name} - will retry automatically...", "orange")
                        print(f"Connection lost: {e}")
                    self.msleep(1000)
                    
        except Exception as e:
            print(f"{self.game_name} Monitor error: {str(e)}")
            self.connection_status.emit(f"Error monitoring {self.game_name}: {str(e)}", "red")
    
    def stop(self):
        print(f"\n=== Stopping {self.game_name} Monitor ===")
        self.running = False
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
                background-color: #1e1e1e;
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
        title.setStyleSheet("color: #cccccc; margin-bottom: 20px;")
        content_layout.addWidget(title)
        
        # Subtitle
        subtitle = QLabel("Waiting for driver to sign up...")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle_font = QFont("Segoe UI", 24)
        subtitle.setFont(subtitle_font)
        subtitle.setStyleSheet("color: #9d9d9d; margin-bottom: 40px;")
        content_layout.addWidget(subtitle)
        
        # Instructions
        instructions = QLabel("📱 Use your mobile device to scan the QR code\nand register as the active driver")
        instructions.setAlignment(Qt.AlignmentFlag.AlignCenter)
        instructions_font = QFont("Segoe UI", 18)
        instructions.setFont(instructions_font)
        instructions.setStyleSheet("color: #007acc; line-height: 1.5;")
        content_layout.addWidget(instructions)
        
        # Close instruction
        close_info = QLabel("Press ESC to close this overlay")
        close_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        close_info_font = QFont("Segoe UI", 14)
        close_info.setFont(close_info_font)
        close_info.setStyleSheet("color: #6e6e6e; margin-top: 40px;")
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
        title.setStyleSheet("color: #4ec9b0; margin-bottom: 20px;")
        content_layout.addWidget(title)
        
        subtitle = QLabel("Please change driver. Returning to waiting screen...")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle_font = QFont("Segoe UI", 22)
        subtitle.setFont(subtitle_font)
        subtitle.setStyleSheet("color: #cccccc; margin-bottom: 16px;")
        content_layout.addWidget(subtitle)
        
        hint = QLabel("Tip: You can manually show/hide the waiting screen from the desktop app.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_font = QFont("Segoe UI", 14)
        hint.setFont(hint_font)
        hint.setStyleSheet("color: #9d9d9d;")
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
                background-color: #2d2d30;
                color: #cccccc;
                border: 1px solid #3e3e42;
                padding: 6px 10px;
                border-radius: 6px;
                font-weight: 700;
                font-size: 12px;
                text-align: left;
            }
            QPushButton:checked {
                background-color: #37373d;
                border-color: #007acc;
            }
            QPushButton:hover {
                background-color: #37373d;
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
        "BeamNG.drive": BeamNGMonitor,
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
        
        # Configure Windows Firewall to allow connections on port 5001
        configure_firewall()

        # Start mDNS service for easy mobile access (sim1.local, sim2.local, etc.)
        self.mdns_hostname = None
        self._start_mdns_service()

        # Start network monitor to detect IP changes
        self.network_monitor_timer = QTimer()
        self.network_monitor_timer.timeout.connect(self._check_network_change)
        self.network_monitor_timer.start(5000)  # Check every 5 seconds

        # Start the Flask server in a separate thread
        global main_window, ui_bridge
        main_window = self
        ui_bridge = UIBridge()
        ui_bridge.driver_registered.connect(self.update_driver_name)
        ui_bridge.manual_lap_submitted.connect(self.on_manual_lap_submitted)
        ui_bridge.force_end_session.connect(self.end_session)
        self.web_thread = threading.Thread(target=run_flask, daemon=True)
        self.web_thread.start()
        
        # Start BeamNG UDP listener for instant lap detection
        self.udp_thread = threading.Thread(target=run_beamng_udp_listener, daemon=True)
        self.udp_thread.start()
        
        # Start UDP broadcast discovery for finding other simulators on the network
        self.discovery_listener = SimulatorBroadcastListener()
        self.discovery_listener.simulator_discovered.connect(self.on_simulator_discovered)
        self.discovery_listener.start()
        
        # Start broadcasting our presence (heartbeat every 3 seconds)
        self.discovery_heartbeat = start_discovery_heartbeat()

        # Start automatic receiver discovery (finds all leaderboard displays automatically)
        self.receiver_discovery = ReceiverDiscoveryThread(discovery_interval=10)
        self.receiver_discovery.receivers_updated.connect(self.on_receivers_updated)
        self.receiver_discovery.start()

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
        self.setMinimumSize(940, 720)  # Wider but shorter - compact multi-column layout
        self.resize(980, 760)  # Optimized for modern displays
        self.setWindowIcon(QIcon("resources/icon.png"))
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e1e;
                color: #cccccc;
            }
            QLabel {
                font-size: 13px;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QLineEdit {
                padding: 8px 12px;
                border: 1px solid #3e3e42;
                border-radius: 4px;
                background-color: #2d2d30;
                color: #cccccc;
                min-height: 20px;
                font-size: 13px;
                selection-background-color: #007acc;
            }
            QLineEdit:read-only {
                background-color: #252526;
                border: 1px solid #3e3e42;
                color: #9d9d9d;
            }
            QLineEdit:focus {
                border: 1px solid #007acc;
                background-color: #1e1e1e;
                color: #ffffff;
            }
            QComboBox {
                padding: 8px 12px;
                border: 1px solid #3e3e42;
                border-radius: 4px;
                background-color: #2d2d30;
                color: #cccccc;
                min-height: 20px;
                font-size: 13px;
            }
            QComboBox:hover {
                border: 1px solid #454545;
                background-color: #37373d;
            }
            QComboBox::drop-down {
                border: none;
                background-color: #3e3e42;
                border-radius: 2px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #cccccc;
                margin-right: 8px;
            }
            QPushButton {
                background-color: #007acc;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: 600;
                font-size: 13px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                background-color: #0098ff;
            }
            QPushButton:pressed {
                background-color: #005a9e;
            }
            QPushButton:checked {
                background-color: #4ec9b0;
                color: #1e1e1e;
            }
            QFormLayout {
                fieldGrowthPolicy: ExpandingFieldsGrow;
                labelAlignment: AlignRight;
            }
            QMessageBox {
                background-color: #252526;
                color: #cccccc;
            }
            QMessageBox QLabel {
                color: #cccccc;
                font-size: 13px;
                background-color: transparent;
            }
            QMessageBox QPushButton {
                min-width: 80px;
                padding: 8px 16px;
            }
        """)

        # Central widget setup
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(8, 8, 8, 8)  # Compact margins
        main_layout.setSpacing(6)  # Tighter spacing between sections
        
        # Create a "card" container with modern dark styling
        card = QWidget()
        # Scope style to this container only to avoid bleeding into children
        card.setObjectName("mainCard")
        card.setStyleSheet("""
            #mainCard {
                background-color: #252526;
                border-radius: 8px;
                border: 1px solid #3e3e42;
            }
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 10, 10, 10)  # Compact margins
        card_layout.setSpacing(6)  # Tighter spacing between sections
        
        # Title with subtitle for clarity
        title_widget = QWidget()
        title_layout = QVBoxLayout(title_widget)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(2)
        
        title = QLabel("🏁 Lap Time Sender")
        title.setStyleSheet("""
            QLabel {
                font-size: 20px;
                font-weight: 700;
                color: #cccccc;
                margin-bottom: 2px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_layout.addWidget(title)
        
        subtitle = QLabel("Professional Racing Telemetry & Leaderboard Interface")
        subtitle.setStyleSheet("""
            QLabel {
                font-size: 11px;
                color: #9d9d9d;
                font-style: italic;
                margin-bottom: 2px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        title_layout.addWidget(subtitle)
        
        card_layout.addWidget(title_widget)
        
        # === TOP ROW: Mobile Interface (left) + Live Status (right) ===
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        
        # LEFT: Mobile Interface section
        mobile_section = QWidget()
        mobile_section.setObjectName("mobileSection")
        mobile_section.setStyleSheet("""
            #mobileSection {
                background-color: #2d2d30;
                border-radius: 6px;
                border: 1px solid #3e3e42;
            }
        """)
        mobile_layout = QHBoxLayout(mobile_section)
        mobile_layout.setContentsMargins(10, 8, 10, 8)
        mobile_layout.setSpacing(8)
        
        # Mobile info text container
        info_text_layout = QVBoxLayout()
        info_text_layout.setSpacing(2)
        
        # URL info label - stored as instance variable for dynamic updates
        self.url_info_label = QLabel(f"📱 Mobile Interface: http://{get_local_ip()}:5001")
        self.url_info_label.setStyleSheet("""
            QLabel {
                color: #cccccc;
                font-size: 11px;
                font-weight: 600;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        self.url_info_label.setWordWrap(True)

        access_info = QLabel("✨ Scan QR or type the address above")
        access_info.setStyleSheet("""
            QLabel {
                color: #9d9d9d;
                font-size: 10px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        access_info.setWordWrap(True)

        # Available pages info
        pages_info = QLabel(
            "<b style='color:#007acc;'>📋 Available Pages:</b><br>"
            "&nbsp;&nbsp;<b>/</b> — Registration &nbsp;•&nbsp; "
            "<b>/admin</b> — Dashboard &nbsp;•&nbsp; "
            "<b>/queue</b> — Queue"
        )
        pages_info.setStyleSheet("""
            QLabel {
                color: #9d9d9d;
                font-size: 9px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                margin-top: 4px;
            }
        """)
        pages_info.setWordWrap(True)

        info_text_layout.addWidget(self.url_info_label)
        info_text_layout.addWidget(access_info)
        info_text_layout.addWidget(pages_info)
        mobile_layout.addLayout(info_text_layout, stretch=1)

        # QR code - stored as instance variable for dynamic updates
        qr_code = self.generate_qr_code(f"http://{get_local_ip()}:5001")
        self.qr_label = QLabel()
        self.qr_label.setPixmap(qr_code)
        self.qr_label.setFixedSize(170, 170)  # Fixed size for consistent display
        self.qr_label.setScaledContents(False)  # Don't scale - keep QR code sharp
        self.qr_label.setStyleSheet("""
            QLabel {
                background-color: #ffffff;
                padding: 5px;
                border-radius: 6px;
                border: 1px solid #3e3e42;
            }
        """)
        self.qr_label.setToolTip("Scan with phone/tablet to access driver interface")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mobile_layout.addWidget(self.qr_label)
        
        top_row.addWidget(mobile_section, stretch=3)
        
        # RIGHT: Live Racing Status section (compact)
        status_section = QWidget()
        status_section.setObjectName("statusSection")
        status_section.setStyleSheet("""
            #statusSection {
                background-color: #2d2d30;
                border-radius: 6px;
                border: 1px solid #3e3e42;
            }
        """)
        status_layout = QVBoxLayout(status_section)
        status_layout.setContentsMargins(10, 6, 10, 6)
        status_layout.setSpacing(4)
        
        # Status header
        status_header = QLabel("📊 Live Racing Status")
        status_header.setStyleSheet("""
            QLabel {
                font-weight: 700;
                font-size: 12px;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        status_layout.addWidget(status_header)
        
        # Game connection status
        game_status_row = QHBoxLayout()
        game_status_row.setSpacing(4)
        status_title = QLabel("Game:")
        status_title.setStyleSheet("""
            QLabel {
                font-weight: 600;
                color: #9d9d9d;
                font-size: 11px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        status_title.setFixedWidth(40)
        game_status_row.addWidget(status_title)
        
        self.game_status = QLabel("Initializing...")
        self.game_status.setWordWrap(True)
        self.game_status.setStyleSheet("""
            QLabel {
                color: #cccccc;
                font-size: 11px;
                padding: 4px 8px;
                background-color: #1e1e1e;
                border-radius: 4px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        game_status_row.addWidget(self.game_status, 1)
        status_layout.addLayout(game_status_row)
        
        # Last lap time display
        lap_time_row = QHBoxLayout()
        lap_time_row.setSpacing(4)
        lap_time_title = QLabel("Lap:")
        lap_time_title.setStyleSheet("""
            QLabel {
                font-weight: 600;
                color: #9d9d9d;
                font-size: 11px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        lap_time_title.setFixedWidth(40)
        lap_time_row.addWidget(lap_time_title)
        
        self.status_label = QLabel("Waiting...")
        self.status_label.setStyleSheet("""
            QLabel {
                color: #cccccc;
                font-size: 12px;
                font-weight: 700;
                padding: 4px 8px;
                background-color: #1e1e1e;
                border-radius: 4px;
                font-family: 'Consolas', 'Monaco', monospace;
            }
        """)
        self.status_label.setToolTip("Shows the most recent lap time sent to the leaderboard")
        lap_time_row.addWidget(self.status_label, 1)
        status_layout.addLayout(lap_time_row)
        
        # Waiting screen button
        self.toggle_waiting_screen_btn = QPushButton("Show Waiting Screen")
        self.toggle_waiting_screen_btn.setStyleSheet("""
            QPushButton {
                background-color: #007acc;
                color: #ffffff;
                border: none;
                padding: 4px 10px;
                border-radius: 4px;
                font-weight: 600;
                font-size: 10px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                background-color: #0098ff;
            }
        """)
        self.toggle_waiting_screen_btn.clicked.connect(self.toggle_waiting_screen)
        self.toggle_waiting_screen_btn.setToolTip("Show/hide the full-screen waiting overlay")
        status_layout.addWidget(self.toggle_waiting_screen_btn)
        
        top_row.addWidget(status_section, stretch=2)
        
        card_layout.addLayout(top_row)
        
        # === CONFIG ROW: Server Connection (left) + Racing Config (right) ===
        config_row = QHBoxLayout()
        config_row.setSpacing(8)
        
        # LEFT: Server Configuration Section
        server_section = QWidget()
        server_section.setObjectName("serverSection")
        server_section.setStyleSheet("""
            #serverSection {
                background-color: #2d2d30;
                border-radius: 6px;
                border: 1px solid #3e3e42;
            }
        """)
        server_section_layout = QVBoxLayout(server_section)
        server_section_layout.setContentsMargins(10, 8, 10, 8)
        server_section_layout.setSpacing(6)
        
        server_header = QLabel("🌐 Leaderboard Server Connection")
        server_header.setStyleSheet("""
            QLabel {
                font-weight: 700;
                font-size: 12px;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        server_section_layout.addWidget(server_header)
        
        # Server URL input row
        server_url_row = QHBoxLayout()
        server_url_row.setSpacing(6)
        
        url_label = QLabel("Server URL:")
        url_label.setStyleSheet("""
            QLabel {
                font-weight: 600;
                font-size: 11px;
                color: #9d9d9d;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        url_label.setFixedWidth(65)
        url_label.setToolTip("Address of the leaderboard server where lap times will be sent")
        server_url_row.addWidget(url_label)
        
        self.server_url = QLineEdit(self.current_server_url)
        self.server_url.textChanged.connect(self.on_server_url_changed)
        self.server_url.setToolTip("Enter receiver URL(s). Use commas for multiple receivers:\nhttp://192.168.1.100:5000, http://192.168.1.101:5000")
        self.server_url.setStyleSheet("""
            QLineEdit {
                background-color: #1e1e1e;
                border: 1px solid #3e3e42;
                color: #cccccc;
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 11px;
                padding: 4px 6px;
            }
            QLineEdit:focus {
                background-color: #1e1e1e;
                border: 1px solid #007acc;
                color: #ffffff;
            }
            QLineEdit::placeholder {
                color: #6e6e6e;
            }
        """)
        server_url_row.addWidget(self.server_url, 1)
        
        test_connection_btn = QPushButton("Test")
        test_connection_btn.setFixedWidth(50)
        test_connection_btn.setStyleSheet("""
            QPushButton {
                background-color: #4ec9b0;
                color: #1e1e1e;
                border: none;
                padding: 5px 8px;
                border-radius: 4px;
                font-weight: 600;
                font-size: 10px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                background-color: #6fd9c4;
            }
        """)
        test_connection_btn.setObjectName("test-connection")
        test_connection_btn.clicked.connect(self.test_and_save_connection)
        test_connection_btn.setToolTip("Test if the server is reachable and save URL if successful")
        server_url_row.addWidget(test_connection_btn)
        
        server_section_layout.addLayout(server_url_row)
        config_row.addWidget(server_section, stretch=3)
        
        # RIGHT: Racing Configuration Section
        racing_section = QWidget()
        racing_section.setObjectName("racingSection")
        racing_section.setStyleSheet("""
            #racingSection {
                background-color: #2d2d30;
                border-radius: 6px;
                border: 1px solid #3e3e42;
            }
        """)
        racing_section_layout = QVBoxLayout(racing_section)
        racing_section_layout.setContentsMargins(10, 8, 10, 8)
        racing_section_layout.setSpacing(6)
        
        racing_header = QLabel("🏁 Racing Simulator Configuration")
        racing_header.setStyleSheet("""
            QLabel {
                font-weight: 700;
                font-size: 12px;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        racing_section_layout.addWidget(racing_header)
        
        # Simulator configuration row
        simulator_row = QHBoxLayout()
        simulator_row.setSpacing(6)
        
        simulator_id_label = QLabel("Simulator ID:")
        simulator_id_label.setStyleSheet("""
            QLabel {
                font-weight: 600;
                font-size: 11px;
                color: #9d9d9d;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        simulator_id_label.setFixedWidth(70)
        simulator_id_label.setToolTip("Unique identifier for this racing setup (1-99)")
        
        self.simulator_id = QComboBox()
        self.simulator_id.addItems([str(i) for i in range(1, 100)])
        self.simulator_id.setFixedWidth(50)
        self.simulator_id.setToolTip("Select a unique ID number for this racing simulator")
        self.simulator_id.currentTextChanged.connect(self._on_simulator_id_changed)

        game_label = QLabel("Racing Game:")
        game_label.setStyleSheet("""
            QLabel {
                font-weight: 600;
                font-size: 11px;
                color: #9d9d9d;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        game_label.setFixedWidth(75)
        game_label.setToolTip("Select which racing simulator you'll be using")
        
        self.game_selector = QComboBox()
        for game_name in self.AVAILABLE_GAMES.keys():
            self.game_selector.addItem(game_name)
        self.game_selector.currentTextChanged.connect(self.on_game_changed)
        self.game_selector.setToolTip("Choose between supported racing simulators")
        
        simulator_row.addWidget(simulator_id_label)
        simulator_row.addWidget(self.simulator_id)
        simulator_row.addWidget(game_label)
        simulator_row.addWidget(self.game_selector, 1)
        
        racing_section_layout.addLayout(simulator_row)
        config_row.addWidget(racing_section, stretch=2)
        
        card_layout.addLayout(config_row)

        # Network Simulators Section - Auto-discovered, read-only display
        simulators_section = QWidget()
        simulators_section.setObjectName("simulatorsSection")
        simulators_section.setStyleSheet("""
            #simulatorsSection {
                background-color: #2d2d30;
                border-radius: 6px;
                border: 1px solid #3e3e42;
            }
        """)
        simulators_layout = QVBoxLayout(simulators_section)
        simulators_layout.setContentsMargins(10, 6, 10, 6)
        simulators_layout.setSpacing(4)
        
        # Header row with title and explanation on same line
        sim_header_row = QHBoxLayout()
        sim_header_row.setSpacing(8)
        
        sim_header = QLabel("🖥️ Network Simulators")
        sim_header.setStyleSheet("""
            QLabel {
                font-weight: 700;
                font-size: 12px;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        sim_header_row.addWidget(sim_header)
        
        sim_explanation = QLabel("Auto-discovered simulators on your network")
        sim_explanation.setStyleSheet("""
            QLabel {
                font-size: 10px;
                color: #9d9d9d;
            }
        """)
        sim_header_row.addWidget(sim_explanation)
        sim_header_row.addStretch()
        simulators_layout.addLayout(sim_header_row)
        
        # List of discovered simulators
        self.simulators_list = QVBoxLayout()
        self.simulators_list.setSpacing(2)
        simulators_layout.addLayout(self.simulators_list)
        
        card_layout.addWidget(simulators_section)
        
        # Populate the list initially and start auto-refresh
        QTimer.singleShot(500, self.refresh_network_simulators_ui)
        
        # Auto-refresh simulator list every 5 seconds (UDP discovery updates instantly via signal)
        self.simulator_refresh_timer = QTimer(self)
        self.simulator_refresh_timer.timeout.connect(self.refresh_network_simulators_ui)
        self.simulator_refresh_timer.start(5000)  # 5 seconds (reduced from 10)
        
        # Mobile Interface Settings box - Compact
        ipad_box = QWidget()
        ipad_box.setObjectName("ipadBox")
        ipad_box.setStyleSheet("""
            #ipadBox {
                background-color: #2d2d30;
                border-radius: 6px;
                border: 1px solid #3e3e42;
            }
        """)
        ipad_box_layout = QVBoxLayout(ipad_box)
        ipad_box_layout.setContentsMargins(10, 6, 10, 6)
        ipad_box_layout.setSpacing(4)
        
        # Header row - compact
        header_layout = QHBoxLayout()
        header_layout.setSpacing(6)
        
        ipad_section_label = QLabel("📱 Mobile Interface Settings")
        ipad_section_label.setStyleSheet("""
            QLabel {
                font-weight: 700;
                font-size: 12px;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        header_layout.addWidget(ipad_section_label)
        
        ipad_explanation = QLabel("Configure iPad/tablet experience")
        ipad_explanation.setStyleSheet("""
            QLabel {
                font-size: 10px;
                color: #9d9d9d;
                font-style: italic;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        header_layout.addWidget(ipad_explanation)
        
        self.advanced_toggle_btn = QPushButton("Show Advanced")
        self.advanced_toggle_btn.setCheckable(True)
        self.advanced_toggle_btn.setChecked(False)
        self.advanced_toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #37373d;
                color: #cccccc;
                font-weight: 700;
                padding: 4px 8px;
                font-size: 10px;
                border-radius: 4px;
                border: 1px solid #3e3e42;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:checked {
                background-color: #007acc;
                color: #ffffff;
                border-color: #007acc;
            }
            QPushButton:hover {
                background-color: #454545;
            }
        """)
        self.advanced_toggle_btn.toggled.connect(lambda checked: self.on_toggle_advanced(checked))
        header_layout.addWidget(self.advanced_toggle_btn)
        
        ipad_settings_update = QPushButton("Apply Settings")
        ipad_settings_update.setMinimumWidth(80)
        ipad_settings_update.setStyleSheet("""
            QPushButton {
                background-color: #4ec9b0;
                color: #1e1e1e;
                font-weight: 700;
                padding: 4px 8px;
                font-size: 10px;
                border-radius: 4px;
                border: none;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                background-color: #6fd9c4;
            }
        """)
        ipad_settings_update.clicked.connect(self.on_update_driver_settings)
        ipad_settings_update.setToolTip("Apply changes to the mobile web interface settings")
        header_layout.addStretch(1)
        header_layout.addWidget(ipad_settings_update)
        
        ipad_box_layout.addLayout(header_layout)
        
        # Compact grid layout for settings
        settings_grid = QWidget()
        grid_layout = QGridLayout(settings_grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setHorizontalSpacing(8)
        grid_layout.setVerticalSpacing(4)
        
        # Set column stretch factors
        grid_layout.setColumnStretch(0, 0)
        grid_layout.setColumnStretch(1, 4)
        grid_layout.setColumnStretch(2, 2)
        grid_layout.setColumnStretch(3, 2)
        
        # Row 0: Current driver name
        name_label = QLabel("Active Driver:")
        name_label.setMinimumWidth(80)
        name_label.setMaximumWidth(100)
        name_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        name_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                font-weight: 600;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        name_label.setToolTip("Shows current active driver - change via mobile interface")
        grid_layout.addWidget(name_label, 0, 0)
        
        self.driver_name = QLineEdit()
        self.driver_name.setReadOnly(True)
        self.driver_name.setPlaceholderText("Set via mobile interface above")
        self.driver_name.setToolTip("Driver name can only be changed via the mobile web interface")
        self.driver_name.setMinimumWidth(200)
        self.driver_name.setStyleSheet("""
            QLineEdit {
                background-color: #252526;
                border: 1px solid #3e3e42;
                color: #cccccc;
                font-size: 11px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                font-weight: 600;
                padding: 4px 8px;
            }
            QLineEdit::placeholder {
                color: #6e6e6e;
            }
        """)
        grid_layout.addWidget(self.driver_name, 0, 1, 1, 3)
        
        # Row 1: Email toggle (compact)
        email_label = QLabel("Email Required:")
        email_label.setMinimumWidth(80)
        email_label.setMaximumWidth(100)
        email_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        email_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                font-weight: 600;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        email_label.setToolTip("Require drivers to enter email addresses")
        grid_layout.addWidget(email_label, 1, 0)
        
        self.email_field_enabled = QPushButton("☐ Optional")
        self.email_field_enabled.setCheckable(True)
        self.email_field_enabled.setChecked(False)
        self.email_field_enabled.setMinimumWidth(70)
        self.email_field_enabled.setMaximumWidth(90)
        self.email_field_enabled.setStyleSheet("""
            QPushButton {
                background-color: #37373d;
                color: #cccccc;
                border: 1px solid #3e3e42;
                padding: 4px 6px;
                border-radius: 4px;
                font-weight: 600;
                font-size: 10px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:checked {
                background-color: #4ec9b0;
                color: #1e1e1e;
                border: 1px solid #4ec9b0;
            }
            QPushButton:hover {
                border: 1px solid #007acc;
            }
        """)
        self.email_field_enabled.toggled.connect(lambda checked: self.email_field_enabled.setText("⊠ Required" if checked else "☐ Optional"))
        self.email_field_enabled.setText("☐ Optional")
        grid_layout.addWidget(self.email_field_enabled, 1, 1)
        
        # Hidden timer widgets for backwards compatibility
        self.timer_label = QLabel()
        self.timer_label.setVisible(False)
        self.driver_timer_enabled = QPushButton()
        self.driver_timer_enabled.setVisible(False)
        
        # Rotation mode label + selector on same row
        rotation_mode_label = QLabel("Rotation Mode:")
        rotation_mode_label.setMinimumWidth(80)
        rotation_mode_label.setMaximumWidth(100)
        rotation_mode_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rotation_mode_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                font-weight: 600;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        rotation_mode_label.setToolTip("Switch between time-based and lap-based driver rotation")
        grid_layout.addWidget(rotation_mode_label, 1, 2)

        self.rotation_mode_selector = QComboBox()
        self.rotation_mode_selector.addItems(["Time", "Laps"])
        self.rotation_mode_selector.setToolTip("Choose rotation mode: by time or by laps")
        self.rotation_mode_selector.setStyleSheet("""
            QComboBox {
                padding: 4px 6px;
                font-size: 10px;
            }
        """)
        grid_layout.addWidget(self.rotation_mode_selector, 1, 3)
        
        # Row 2: Minutes/Laps input
        self.minutes_label = QLabel("Minutes:")
        self.minutes_label.setMinimumWidth(80)
        self.minutes_label.setMaximumWidth(100)
        self.minutes_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.minutes_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        grid_layout.addWidget(self.minutes_label, 2, 0)
        
        self.driver_timer_minutes = QLineEdit("55")
        self.driver_timer_minutes.setMinimumWidth(50)
        self.driver_timer_minutes.setMaximumWidth(70)
        self.driver_timer_minutes.setStyleSheet("""
            QLineEdit {
                background-color: #1e1e1e;
                border: 1px solid #3e3e42;
                color: #cccccc;
                font-size: 11px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                font-weight: 600;
                padding: 4px 6px;
            }
            QLineEdit:focus {
                border-color: #007acc;
            }
        """)
        self.driver_timer_minutes.setToolTip("Duration in minutes for each driver's turn")
        grid_layout.addWidget(self.driver_timer_minutes, 2, 1)
        
        # Row 3: Background image (Advanced) - Compact
        bg_label = QLabel("Background:")
        bg_label.setMinimumWidth(80)
        bg_label.setMaximumWidth(100)
        bg_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bg_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                font-weight: 600;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        bg_label.setToolTip("Custom background image for mobile interface")
        grid_layout.addWidget(bg_label, 3, 0)
        
        self.background_image_path = QLineEdit()
        self.background_image_path.setPlaceholderText("Select custom background image")
        self.background_image_path.setMinimumWidth(200)
        self.background_image_path.setStyleSheet("""
            QLineEdit {
                background-color: #1e1e1e;
                border: 1px solid #3e3e42;
                color: #cccccc;
                font-size: 10px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                padding: 4px 8px;
            }
            QLineEdit::placeholder {
                color: #6e6e6e;
            }
            QLineEdit:focus {
                border-color: #007acc;
            }
        """)
        grid_layout.addWidget(self.background_image_path, 3, 1, 1, 2)
        
        self.select_bg_button = QPushButton("Browse")
        self.select_bg_button.setMinimumWidth(60)
        self.select_bg_button.setMaximumWidth(80)
        self.select_bg_button.setStyleSheet("""
            QPushButton {
                background-color: #37373d;
                color: #cccccc;
                border: 1px solid #3e3e42;
                padding: 4px 8px;
                border-radius: 4px;
                font-weight: 600;
                font-size: 10px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                background-color: #454545;
                border-color: #007acc;
            }
        """)
        self.select_bg_button.clicked.connect(self.select_background_image)
        grid_layout.addWidget(self.select_bg_button, 3, 3)

        # Row 4: Pre-session video enable (Advanced) - Compact
        video_enable_label = QLabel("Pre-Session Video:")
        video_enable_label.setMinimumWidth(80)
        video_enable_label.setMaximumWidth(100)
        video_enable_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        video_enable_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                font-weight: 600;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        video_enable_label.setToolTip("Require a 1920x1080 video to play on PC after signup")
        grid_layout.addWidget(video_enable_label, 4, 0)

        self.video_required_toggle = QPushButton("☐ Disabled")
        self.video_required_toggle.setCheckable(True)
        self.video_required_toggle.setChecked(False)
        self.video_required_toggle.setMinimumWidth(70)
        self.video_required_toggle.setMaximumWidth(90)
        self.video_required_toggle.setStyleSheet("""
            QPushButton {
                background-color: #37373d;
                color: #cccccc;
                border: 1px solid #3e3e42;
                padding: 4px 6px;
                border-radius: 4px;
                font-weight: 600;
                font-size: 10px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:checked {
                background-color: #4ec9b0;
                color: #1e1e1e;
                border: 1px solid #4ec9b0;
            }
            QPushButton:hover {
                border: 1px solid #007acc;
            }
        """)
        self.video_required_toggle.setToolTip("Play video on PC when a driver is set")
        self.video_required_toggle.toggled.connect(lambda checked: self.video_required_toggle.setText("⊠ Enabled" if checked else "☐ Disabled"))
        grid_layout.addWidget(self.video_required_toggle, 4, 1)

        self.video_path_input = QLineEdit()
        self.video_path_input.setPlaceholderText("Select MP4 video")
        self.video_path_input.setMinimumWidth(200)
        self.video_path_input.setStyleSheet("""
            QLineEdit {
                background-color: #1e1e1e;
                border: 1px solid #3e3e42;
                color: #cccccc;
                font-size: 10px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                padding: 4px 8px;
            }
            QLineEdit::placeholder {
                color: #6e6e6e;
            }
            QLineEdit:focus {
                border-color: #007acc;
            }
        """)
        grid_layout.addWidget(self.video_path_input, 4, 2)

        self.select_video_button = QPushButton("Browse")
        self.select_video_button.setMinimumWidth(60)
        self.select_video_button.setMaximumWidth(80)
        self.select_video_button.setStyleSheet("""
            QPushButton {
                background-color: #37373d;
                color: #cccccc;
                border: 1px solid #3e3e42;
                padding: 4px 8px;
                border-radius: 4px;
                font-weight: 600;
                font-size: 10px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                background-color: #454545;
                border-color: #007acc;
            }
        """)
        self.select_video_button.clicked.connect(self.select_video_file)
        grid_layout.addWidget(self.select_video_button, 4, 3)

        # Row 5: Waiting screen media (Advanced) - Compact
        waiting_media_label = QLabel("Waiting Screen:")
        waiting_media_label.setMinimumWidth(80)
        waiting_media_label.setMaximumWidth(100)
        waiting_media_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        waiting_media_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                font-weight: 600;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        waiting_media_label.setToolTip("Custom image or looping video for waiting screen")
        grid_layout.addWidget(waiting_media_label, 5, 0)

        self.waiting_media_path = QLineEdit()
        self.waiting_media_path.setPlaceholderText("Select JPG/MP4 for waiting screen")
        self.waiting_media_path.setMinimumWidth(200)
        self.waiting_media_path.setStyleSheet("""
            QLineEdit {
                background-color: #1e1e1e;
                border: 1px solid #3e3e42;
                color: #cccccc;
                font-size: 10px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                padding: 4px 8px;
            }
            QLineEdit::placeholder {
                color: #6e6e6e;
            }
            QLineEdit:focus {
                border-color: #007acc;
            }
        """)
        grid_layout.addWidget(self.waiting_media_path, 5, 1, 1, 2)

        self.select_waiting_media_button = QPushButton("Browse")
        self.select_waiting_media_button.setMinimumWidth(60)
        self.select_waiting_media_button.setMaximumWidth(80)
        self.select_waiting_media_button.setStyleSheet("""
            QPushButton {
                background-color: #37373d;
                color: #cccccc;
                border: 1px solid #3e3e42;
                padding: 4px 8px;
                border-radius: 4px;
                font-weight: 600;
                font-size: 10px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                background-color: #454545;
                border-color: #007acc;
            }
        """)
        self.select_waiting_media_button.clicked.connect(self.select_waiting_media_file)
        grid_layout.addWidget(self.select_waiting_media_button, 5, 3)

        # Laps input (only used in Laps mode) - on same row as minutes
        self.lap_count_label = QLabel("Laps:")
        self.lap_count_label.setMinimumWidth(40)
        self.lap_count_label.setMaximumWidth(50)
        self.lap_count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lap_count_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        grid_layout.addWidget(self.lap_count_label, 2, 2)

        self.lap_target_input = QLineEdit("10")
        self.lap_target_input.setMinimumWidth(50)
        self.lap_target_input.setMaximumWidth(70)
        self.lap_target_input.setStyleSheet("""
            QLineEdit {
                background-color: #1e1e1e;
                border: 1px solid #3e3e42;
                color: #cccccc;
                font-size: 11px;
                text-align: center;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                font-weight: 600;
                padding: 4px 6px;
            }
            QLineEdit:focus {
                border-color: #007acc;
            }
        """)
        self.lap_target_input.setToolTip("Number of laps before driver swap alert")
        grid_layout.addWidget(self.lap_target_input, 2, 3)

        # Time input placeholder (shares position with laps, switched by mode)
        self.time_count_label = self.minutes_label  # Reuse minutes label
        self.time_target_input = self.driver_timer_minutes  # Reuse driver timer minutes

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
        self.session_start_timestamp = 0  # Unix timestamp when session started (for queue wait time estimates)
        self.assigned_queue_id = None  # Queue ID of assigned guest (if from queue system)
        
        # Set minimum size for the settings grid to ensure proper display
        settings_grid.setMinimumWidth(400)
        
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
        if game_name == "BeamNG.drive":
            self.install_beamng_mod()
        self.start_monitor(game_name)
    
    def find_beamng_mods_folder(self):
        """Find BeamNG.drive mods folder, checking multiple possible locations"""
        possible_parents = [
            Path.home() / "Documents" / "BeamNG.drive",
            Path.home() / "AppData" / "Local" / "BeamNG.drive",
            Path.home() / "AppData" / "Local" / "BeamNG" / "BeamNG.drive",
        ]
        for version in ["0.38", "0.37", "0.36", "0.35", "0.34", "0.33", "0.32", "0.31", "0.30"]:
            possible_parents.append(Path.home() / "AppData" / "Local" / "BeamNG.drive" / version)
            possible_parents.append(Path.home() / "AppData" / "Local" / "BeamNG" / "BeamNG.drive" / version)
        for parent in possible_parents:
            mods_folder = parent / "mods"
            if mods_folder.exists():
                print(f"Found existing mods folder: {mods_folder}")
                return mods_folder
        for parent in possible_parents:
            if parent.exists():
                has_beamng_files = any([
                    (parent / "settings").exists(),
                    (parent / "vehicles").exists(),
                    (parent / "levels").exists(),
                    (parent / "cache").exists(),
                ])
                if has_beamng_files:
                    mods_folder = parent / "mods"
                    mods_folder.mkdir(exist_ok=True)
                    print(f"Created mods folder at: {mods_folder}")
                    return mods_folder
        for parent in possible_parents[:3]:
            if parent.exists():
                mods_folder = parent / "mods"
                mods_folder.mkdir(exist_ok=True)
                print(f"Created mods folder at: {mods_folder}")
                return mods_folder
        return None
    
    def install_beamng_mod(self):
        """Auto-install or update BeamNG mod"""
        BUNDLED_VERSION = "5.3.0"  # Current bundled version - extracts laps from mission end
        
        try:
            mods_folder = self.find_beamng_mods_folder()
            if mods_folder is None:
                print("WARNING: BeamNG mods folder not found in any known location")
                QMessageBox.warning(self, "BeamNG Mods Folder Not Found",
                    "Could not find BeamNG.drive mods folder.\n\n"
                    "Please open BeamNG.drive, go to the main menu,\n"
                    "click 'Manage User Folder' > 'Open in Explorer',\n"
                    "then create a 'mods' folder if it doesn't exist.\n\n"
                    "After that, try selecting BeamNG.drive again.")
                return
            
            # Find bundled mod
            if getattr(sys, 'frozen', False):
                bundled_mod = Path(sys.executable).parent / "_internal" / "beamng_mod.zip"
            else:
                bundled_mod = Path(__file__).parent / "beamng_mod.zip"
            
            if not bundled_mod.exists():
                print(f"WARNING: BeamNG mod bundle not found at: {bundled_mod}")
                QMessageBox.warning(self, "Mod Not Found",
                    "BeamNG mod file not found in application bundle.\n\n"
                    "Please reinstall the Lap Time Sender application.")
                return
            
            # Check installed mod version
            mod_dest = mods_folder / "leaderboard_sender.zip"
            needs_update = True
            current_version = "not installed"
            
            if mod_dest.exists():
                try:
                    import zipfile
                    import json
                    with zipfile.ZipFile(mod_dest, 'r') as zf:
                        if 'modinfo.json' in zf.namelist():
                            with zf.open('modinfo.json') as f:
                                info = json.load(f)
                                current_version = info.get('version', '0.0.0')
                                # Simple version comparison
                                cur_parts = [int(x) for x in current_version.split('.')]
                                new_parts = [int(x) for x in BUNDLED_VERSION.split('.')]
                                needs_update = new_parts > cur_parts
                except Exception as e:
                    print(f"Could not read installed mod version: {e}")
                    needs_update = True
            
            if not needs_update:
                print(f"BeamNG mod v{current_version} already up to date at: {mod_dest}")
                return
            
            # Copy/update mod to mods folder
            shutil.copy(bundled_mod, mod_dest)
            is_update = current_version != "not installed"
            action = "updated" if is_update else "installed"
            print(f"[OK] BeamNG mod {action} to v{BUNDLED_VERSION} at: {mod_dest}")
            
            msg = f"BeamNG mod {action} successfully!\n\n"
            if is_update:
                msg += f"Updated: v{current_version} → v{BUNDLED_VERSION}\n\n"
                msg += "NEW: Direct HTTP lap sending for instant detection!\n"
                msg += "No more delayed or batched laps.\n\n"
            else:
                msg += f"Version: {BUNDLED_VERSION}\n"
                msg += f"Location: {mod_dest}\n\n"
            msg += "If BeamNG is running, please restart it."
            
            QMessageBox.information(self, f"BeamNG Mod {'Updated' if is_update else 'Installed'}", msg)
                    
        except Exception as e:
            print(f"Error installing BeamNG mod: {e}")
            QMessageBox.warning(self, "Installation Error", f"Failed to install BeamNG mod:\n{e}")
        
    def on_lap_completed(self, lap_time, driver_name=""):
        # Automatically send the lap time with the driver who completed it
        self.send_lap_time(lap_time, from_game=True, override_driver=driver_name)
        
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

    def send_lap_time(self, lap_time=None, from_game=True, override_driver=""):
        """Send the lap time to the server
        
        Args:
            lap_time: The lap time in seconds
            from_game: Whether this came from game telemetry
            override_driver: If provided, use this driver name instead of current
        """
        try:
            print(f"\n=== send_lap_time called ===")
            print(f"Lap time: {lap_time}, From game: {from_game}, Override driver: {override_driver}")
            
            # Get the formatted lap time (if provided)
            formatted_time = None
            if lap_time is not None:
                formatted_time = format_time_ms(int(lap_time * 1000))

            # Get driver name - use override if provided, otherwise current
            if override_driver:
                driver_name = override_driver
            else:
                driver_name = self.driver_name.text() if hasattr(self.driver_name, 'text') else str(self.driver_name)

            print(f"Resolved driver name: {driver_name}")

            # Check if we have all necessary data
            if not driver_name or driver_name == "":
                print("FAILED: No driver name")
                self.show_error("Please set a driver name first!")
                return False
            
            # Skip sending if we don't have a lap time
            if lap_time is None and from_game:
                print("FAILED: No lap time and from_game=True")
                self.show_error("No valid lap time to send!")
                return False
            
            # Additional validation for manual entries
            if lap_time is not None and not validate_lap_time(lap_time, "Manual Entry"):
                print(f"FAILED: Invalid lap time {lap_time}")
                self.show_error(f"Invalid lap time: {lap_time:.3f}s. Must be between 5s and 60 minutes.")
                return False
                
            # Check if email is required but not provided (SKIP FOR MANUAL ENTRIES)
            # Manual entries don't have email associated, so we allow them through
            email_required = self.email_field_enabled.isChecked() if hasattr(self, 'email_field_enabled') else False
            print(f"Email required: {email_required}, Has email: {hasattr(self, 'last_driver_email') and self.last_driver_email}")
            if email_required and from_game and (not hasattr(self, 'last_driver_email') or not self.last_driver_email):
                print("FAILED: Email required but not provided (from_game=True)")
                self.show_error("Email is required but not provided. Please set driver email via the web interface.")
                return False

            # Prepare data with strict type conversion
            data = {
                "simulator_id": int(self.simulator_id.currentText()),  # Get selected simulator ID
                "driver_name": str(driver_name).strip(),  # Use resolved driver name (may be override)
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
            server_url_str = self.current_server_url

            # Fallback to UI value if current_server_url isn't set
            if not server_url_str:
                server_url_str = self.server_url.text().strip()
                print("Using URL from UI as no saved URL exists yet")

            # Support multiple comma-separated URLs
            server_urls = [url.strip() for url in server_url_str.split(',') if url.strip()]

            print(f"\nSending data to server(s): {data}")
            print(f"Server URLs: {server_urls}")

            # Update status to show we're sending
            self.status_label.setText("📤 Sending lap time to server...")
            self.status_label.setStyleSheet("color: #0066cc; background-color: #f0f0f0; padding: 10px; border-radius: 5px; font-size: 12px;")

            # Send to all servers CONCURRENTLY for speed and reliability
            headers = {'Content-Type': 'application/json'}
            success_count = 0
            failed_urls = []

            def send_to_receiver(server_url):
                """Send lap time to a single receiver. Returns (url, success, error_msg)"""
                try:
                    response = requests.post(
                        server_url,
                        json=data,
                        headers=headers,
                        timeout=5
                    )
                    if response.status_code == 200:
                        return (server_url, True, None)
                    else:
                        return (server_url, False, f"HTTP {response.status_code}")
                except requests.exceptions.Timeout:
                    return (server_url, False, "timeout")
                except requests.exceptions.ConnectionError:
                    return (server_url, False, "connection error")
                except Exception as e:
                    return (server_url, False, str(e))

            # Use ThreadPoolExecutor to send to all receivers in parallel
            # Max 50 workers to avoid overwhelming the system even with 200+ receivers
            max_workers = min(50, len(server_urls))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(send_to_receiver, url): url for url in server_urls}
                for future in as_completed(futures, timeout=10):
                    try:
                        url, success, error_msg = future.result()
                        if success:
                            success_count += 1
                            print(f"✓ Sent to {url}")
                        else:
                            failed_urls.append(url)
                            print(f"✗ Failed to send to {url}: {error_msg}")
                    except Exception as e:
                        url = futures[future]
                        failed_urls.append(url)
                        print(f"✗ Exception sending to {url}: {e}")

            # Report results
            total_urls = len(server_urls)
            if success_count == total_urls:
                self.show_success(f"✅ Lap time {lap_time:.3f}s sent successfully!")
            elif success_count > 0:
                self.show_success(f"✅ Lap time {lap_time:.3f}s sent to {success_count}/{total_urls} receivers")
            else:
                # All failed - add to retry queue
                if data not in self.failed_lap_times:
                    self.failed_lap_times.append(data)
                self.show_error(f"❌ Failed to send to all {total_urls} receiver(s). Will retry automatically.")
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
                        color: #4ec9b0;
                        font-size: 24px;
                        font-weight: 700;
                        padding: 8px 16px;
                        background-color: #2d2d30;
                        border-radius: 6px;
                        border: 1px solid #4ec9b0;
                        font-family: 'Consolas', 'Monaco', monospace;
                    }
                """)
            except:
                self.status_label.setText(message)
                self.status_label.setStyleSheet("""
                    QLabel {
                        color: #4ec9b0;
                        background-color: #2d2d30;
                        padding: 10px;
                        border-radius: 6px;
                        border: 1px solid #4ec9b0;
                        font-size: 13px;
                        font-family: 'Segoe UI', 'Arial', sans-serif;
                    }
                """)
        else:
            self.status_label.setText(message)
            self.status_label.setStyleSheet("""
                QLabel {
                    color: #4ec9b0;
                    background-color: #2d2d30;
                    padding: 10px;
                    border-radius: 6px;
                    border: 1px solid #4ec9b0;
                    font-size: 13px;
                    font-family: 'Segoe UI', 'Arial', sans-serif;
                }
            """)
            
    def show_error(self, message):
        self.status_label.setText(message)
        self.status_label.setStyleSheet("""
            QLabel {
                color: #f48771;
                background-color: #2d2d30;
                padding: 10px;
                border-radius: 6px;
                border: 1px solid #f48771;
                font-size: 13px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                font-weight: 600;
            }
        """)
        
    def update_game_status(self, message, color):
        # Add emoji indicators based on status with professional dark styling
        if "Connected" in message:
            message = "🟢 " + message
            self.game_status.setStyleSheet("""
                QLabel {
                    color: #4ec9b0;
                    font-size: 13px;
                    padding: 6px 12px;
                    background-color: #2d2d30;
                    border-radius: 4px;
                    border: 1px solid #4ec9b0;
                    font-family: 'Segoe UI', 'Arial', sans-serif;
                    font-weight: 600;
                }
            """)
        elif "Attempting" in message or "Lost connection" in message or "Waiting" in message:
            message = "🟡 " + message
            self.game_status.setStyleSheet("""
                QLabel {
                    color: #ce9178;
                    font-size: 13px;
                    padding: 6px 12px;
                    background-color: #2d2d30;
                    border-radius: 4px;
                    border: 1px solid #ce9178;
                    font-family: 'Segoe UI', 'Arial', sans-serif;
                    font-weight: 600;
                }
            """)
        elif "Error" in message:
            message = "🔴 " + message
            self.game_status.setStyleSheet("""
                QLabel {
                    color: #f48771;
                    font-size: 13px;
                    padding: 6px 12px;
                    background-color: #2d2d30;
                    border-radius: 4px;
                    border: 1px solid #f48771;
                    font-family: 'Segoe UI', 'Arial', sans-serif;
                    font-weight: 600;
                }
            """)
        else:
            # Default styling for other messages
            self.game_status.setStyleSheet("""
                QLabel {
                    color: #cccccc;
                    font-size: 13px;
                    padding: 6px 12px;
                    background-color: #2d2d30;
                    border-radius: 4px;
                    border: 1px solid #3e3e42;
                    font-family: 'Segoe UI', 'Arial', sans-serif;
                }
            """)
            
        self.game_status.setText(message)

    def _start_mdns_service(self):
        """Start or restart the mDNS service with current simulator ID."""
        try:
            sim_id = int(self.simulator_id.currentText()) if hasattr(self, 'simulator_id') else 1
            self.mdns_hostname = mdns_service.start(sim_id, 5001)
            if self.mdns_hostname:
                self._update_mobile_url_display()
        except Exception as e:
            print(f"[mDNS] Error starting service: {e}")

    def _check_network_change(self):
        """Check if the network IP has changed and update accordingly."""
        new_ip = get_local_ip()
        if new_ip != self.local_ip:
            print(f"[Network] IP changed from {self.local_ip} to {new_ip}")
            self.local_ip = new_ip

            # Restart mDNS with new IP
            self._start_mdns_service()

            # Update the URL display
            self._update_mobile_url_display()

            # Update QR code
            self._update_qr_code()

    def _update_mobile_url_display(self):
        """Update the mobile interface URL display with current IP and mDNS name."""
        if hasattr(self, 'url_info_label'):
            ip_url = f"http://{self.local_ip}:5001"
            if self.mdns_hostname:
                # Show both mDNS and IP address
                self.url_info_label.setText(
                    f"📱 Mobile Interface:\n"
                    f"   {self.mdns_hostname}:5001  (easy)\n"
                    f"   {self.local_ip}:5001  (IP)"
                )
            else:
                self.url_info_label.setText(f"📱 Mobile Interface: {ip_url}")

    def _update_qr_code(self):
        """Update the QR code with current IP."""
        if hasattr(self, 'qr_label'):
            qr_code = self.generate_qr_code(f"http://{self.local_ip}:5001")
            if qr_code:
                self.qr_label.setPixmap(qr_code)

    def _on_simulator_id_changed(self, new_id):
        """Called when the simulator ID dropdown changes."""
        try:
            sim_id = int(new_id)
            # Update mDNS service with new simulator ID
            self.mdns_hostname = mdns_service.update_simulator_id(sim_id, 5001)
            self._update_mobile_url_display()
        except Exception as e:
            print(f"[mDNS] Error updating simulator ID: {e}")

    def closeEvent(self, event):
        if self.current_monitor:
            self.current_monitor.stop()
        if self.retry_timer:
            self.retry_timer.cancel()
        # Stop mDNS service
        mdns_service.stop()
        event.accept()

    def show_web_interface_message(self):
        pass  # Remove the popup message
    
    def _flush_pending_beamng_laps(self, driver_name):
        """Send any pending BeamNG laps before driver changes
        
        This ensures laps completed by the previous driver get attributed correctly.
        """
        if not self.current_monitor:
            return
            
        # Check if this is a BeamNG monitor with pending laps
        if hasattr(self.current_monitor, 'pending_laps') and self.current_monitor.pending_laps:
            pending_count = len(self.current_monitor.pending_laps)
            print(f"\n=== Flushing {pending_count} pending BeamNG lap(s) for driver: {driver_name} ===")
            
            # Send each pending lap with the old driver's name
            while self.current_monitor.pending_laps:
                lap = self.current_monitor.pending_laps.pop(0)
                lap_time = lap.get('lap_time', 0)
                stored_driver = lap.get('driver', driver_name)
                
                # Use the stored driver if available, otherwise use the passed driver_name
                actual_driver = stored_driver if stored_driver else driver_name
                
                print(f"  Sending queued lap: {lap_time:.3f}s for {actual_driver}")
                self.send_lap_time(lap_time, from_game=True, override_driver=actual_driver)
            
            print("=== Pending laps flushed ===\n")
    
    def update_driver_name(self, name):
        # Flush any pending laps from BeamNG before changing driver
        # This ensures laps are sent with the correct driver name
        old_driver = self.driver_name.text() if hasattr(self.driver_name, 'text') else ""
        if old_driver and old_driver != name:
            self._flush_pending_beamng_laps(old_driver)
        
        self.driver_name.setText(name)
        
        # Clear any expired timer message from previous session
        self.status_label.setText("Ready")
        self.status_label.setStyleSheet("color: #9d9d9d; padding: 8px; background-color: #1e1e1e; border-radius: 4px;")
        
        # Reset session state for new driver BEFORE starting video/timer
        self.timer_started = False
        if hasattr(self, 'rotation_mode_selector') and self.rotation_mode_selector.currentText() == "Laps":
            self.lap_rotation_count = 0
            self.lap_count_started = False
        
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

    def on_manual_lap_submitted(self, lap_time, driver_name):
        """Handle manual lap time submission from mobile interface (thread-safe via signal)"""
        print(f"\n=== Processing Manual Lap Submission ===")
        print(f"Driver: {driver_name}, Lap Time: {lap_time:.3f}s")
        self.send_lap_time(lap_time, from_game=False, override_driver=driver_name)

    def on_receivers_updated(self, receiver_urls):
        """Called when automatic discovery updates the list of leaderboard receivers.

        Args:
            receiver_urls: List of receiver URLs (e.g., ["http://192.168.1.100:5000", "http://192.168.1.101:5000"])
        """
        if not receiver_urls:
            return

        new_urls_str = ", ".join(receiver_urls)
        print(f"[Auto-Discovery] Active receivers: {new_urls_str}")

        # Get current URL(s) from the text field
        current_url = self.server_url.text().strip()
        default_url = f"http://{self.local_ip}:5000"

        # Parse current URLs into a set for comparison
        current_urls = set()
        if current_url:
            current_urls = {u.strip() for u in current_url.split(',') if u.strip()}

        new_urls = set(receiver_urls)

        # Auto-fill if: empty, only contains default self-reference, or matches discovered receivers
        is_default_only = (not current_urls or
                          current_urls == {default_url} or
                          current_urls == new_urls)

        # Also auto-fill if current URLs are a subset of discovered (user hasn't added custom ones)
        is_auto_managed = current_urls.issubset(new_urls) or new_urls.issuperset(current_urls - {default_url})

        if is_default_only or is_auto_managed:
            self.server_url.setText(new_urls_str)
            self.current_server_url = new_urls_str

            # Show appropriate message
            if len(receiver_urls) == 1:
                self.show_success(f"Connected to receiver: {receiver_urls[0]}")
            else:
                self.show_success(f"Connected to {len(receiver_urls)} receivers")

            # Automatically test the connection(s)
            QTimer.singleShot(500, self.test_and_save_connection)
        else:
            # User has custom URL(s) not in discovered list, don't override
            print(f"[Auto-Discovery] Not overriding user's custom URLs: {current_url}")

    def on_simulator_discovered(self, simulator_info):
        """Called immediately when UDP broadcast discovers a new/updated simulator"""
        # Immediately refresh the UI with all known simulators from UDP listener
        self._refresh_from_udp_discovery()
    
    def _refresh_from_udp_discovery(self):
        """Refresh simulator list using UDP-discovered simulators (instant, no network scan)"""
        if not hasattr(self, 'discovery_listener'):
            return
            
        local_ip = get_local_ip()
        other_simulators = []
        
        for sim in self.discovery_listener.get_known_simulators():
            ip = sim.get('ip')
            if ip and ip != local_ip:
                other_simulators.append({
                    'name': sim.get('simulator_name', f'Sim ({ip})'),
                    'ip': ip,
                    'game': sim.get('game'),
                    'driver': sim.get('current_driver'),
                })
        
        self._update_network_simulators_ui(other_simulators)

    def refresh_network_simulators_ui(self):
        """Refresh the list of auto-discovered network simulators
        
        Uses both UDP broadcast discovery (instant) and HTTP probe (fallback).
        UDP-discovered simulators take priority as they're faster and more reliable.
        """
        def discover_and_update():
            try:
                local_ip = get_local_ip()
                simulators_by_ip = {}
                
                # First, add UDP-discovered simulators (instant, high priority)
                if hasattr(self, 'discovery_listener'):
                    for sim in self.discovery_listener.get_known_simulators():
                        ip = sim.get('ip')
                        if ip and ip != local_ip:
                            simulators_by_ip[ip] = {
                                'name': sim.get('simulator_name', f'Sim ({ip})'),
                                'ip': ip,
                                'game': sim.get('game'),
                                'driver': sim.get('current_driver'),
                            }
                
                # Then, add HTTP-probed simulators (slower, fallback for non-UDP apps)
                discovered = SimulatorDiscovery.discover()
                for sim in discovered:
                    ip = sim.get('ip')
                    if ip and ip != local_ip and sim.get('reachable'):
                        if ip not in simulators_by_ip:  # Don't override UDP discoveries
                            simulators_by_ip[ip] = {
                                'name': sim.get('simulator_name', f'Sim ({ip})'),
                                'ip': ip,
                                'game': sim.get('game'),
                                'driver': sim.get('current_driver'),
                            }
                
                other_simulators = list(simulators_by_ip.values())
                
                # Update UI on main thread
                QTimer.singleShot(0, lambda: self._update_network_simulators_ui(other_simulators))
            except Exception as e:
                print(f"Error discovering simulators: {e}")
        
        threading.Thread(target=discover_and_update, daemon=True).start()
    
    def _update_network_simulators_ui(self, simulators):
        """Update the network simulator list UI with discovered simulators"""
        # Clear existing items
        while self.simulators_list.count():
            item = self.simulators_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        if not simulators:
            no_sims_label = QLabel("No other simulators found on network")
            no_sims_label.setStyleSheet("color: #9d9d9d; font-size: 11px; padding: 4px 0;")
            self.simulators_list.addWidget(no_sims_label)
            return
        
        for sim in simulators:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 2, 0, 2)
            row_layout.setSpacing(8)
            
            # Status indicator (online)
            status_label = QLabel("🟢")
            status_label.setFixedWidth(20)
            status_label.setStyleSheet("font-size: 10px;")
            row_layout.addWidget(status_label)
            
            # Simulator name
            name = sim.get('name', 'Unknown')
            name_label = QLabel(name)
            name_label.setStyleSheet("color: #ffffff; font-size: 12px; font-weight: 500;")
            row_layout.addWidget(name_label)
            
            # Game info if available
            game = sim.get('game')
            if game:
                game_label = QLabel(f"({game})")
                game_label.setStyleSheet("color: #9d9d9d; font-size: 11px;")
                row_layout.addWidget(game_label)
            
            # Driver info if available
            driver = sim.get('driver')
            if driver:
                driver_label = QLabel(f"[{driver}]")
                driver_label.setStyleSheet("color: #4fc3f7; font-size: 11px;")
                row_layout.addWidget(driver_label)
            
            # IP address (subtle)
            ip_label = QLabel(sim.get('ip', ''))
            ip_label.setStyleSheet("color: #6d6d6d; font-size: 10px;")
            row_layout.addWidget(ip_label)
            
            row_layout.addStretch()
            self.simulators_list.addWidget(row)

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
        print("[END SESSION] end_session() called - showing session complete screen")

        # Notify receiver that session has ended (for queue wait time tracking)
        self._notify_session_ended()

        self.session_active = False
        self.timer_started = False  # Reset timer state
        self.timer_end_timestamp = 0  # Prevent stale timestamp race condition
        self.session_start_timestamp = 0  # Reset session start timestamp

        # Clear driver name so network tabs show "No driver"
        self.driver_name.clear()
        print("[END SESSION] Driver name cleared")
        
        # Show a brief "session complete" interstitial, then return to waiting
        try:
            self.waiting_overlay.show_session_complete()
            self.waiting_overlay.show()
            print("[OK] Session complete screen should now be visible")
        except Exception as e:
            print(f"[ERROR] Error showing session complete screen: {e}")
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
        # Record session start time for queue wait time estimates
        self.session_start_timestamp = int(time.time())

        # Notify receiver that session has started (for queue system)
        self._notify_session_started()

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

    def _notify_session_started(self):
        """Notify the receiver that a session has started (for queue wait time tracking)"""
        try:
            if hasattr(self, 'current_server_url') and self.current_server_url:
                driver_name = self.driver_name.text() if hasattr(self, 'driver_name') else ""
                queue_id = getattr(self, 'assigned_queue_id', None)
                data = {
                    'simulator_ip': get_local_ip(),
                    'driver_name': driver_name,
                    'queue_id': queue_id or ''
                }
                response = requests.post(
                    f"{self.current_server_url}/api/session/started",
                    json=data,
                    timeout=2.0
                )
                if response.ok:
                    print(f"[QUEUE] Session start reported to receiver")
                    # Clear assigned queue ID after session starts
                    self.assigned_queue_id = None
        except Exception as e:
            print(f"[QUEUE] Could not notify receiver of session start: {e}")

    def _notify_session_ended(self):
        """Notify the receiver that a session has ended (for queue wait time tracking)"""
        try:
            if hasattr(self, 'current_server_url') and self.current_server_url:
                # Calculate session duration
                duration = 0
                if hasattr(self, 'session_start_timestamp') and self.session_start_timestamp > 0:
                    duration = int(time.time()) - self.session_start_timestamp

                data = {
                    'simulator_ip': get_local_ip(),
                    'duration_seconds': duration
                }
                response = requests.post(
                    f"{self.current_server_url}/api/session/ended",
                    json=data,
                    timeout=2.0
                )
                if response.ok:
                    print(f"[QUEUE] Session end reported to receiver (duration: {duration}s)")
        except Exception as e:
            print(f"[QUEUE] Could not notify receiver of session end: {e}")

    def check_session_timeout(self):
        """Periodically check if the session has timed out (even without lap completion)"""
        if not self.session_active:
            return
        if not self.timer_started:
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

        # Basic URL validation - support multiple comma-separated URLs
        urls = [u.strip() for u in url.split(',') if u.strip()]
        is_valid = all(u.startswith(('http://', 'https://')) and ':' in u.split('/')[-1] for u in urls) if urls else False

        if is_valid:
            # Clear any previous error styling - dark theme
            self.server_url.setStyleSheet("""
                QLineEdit {
                    background-color: #1e1e1e;
                    border: 1px solid #3e3e42;
                    color: #cccccc;
                    font-family: 'Consolas', 'Monaco', monospace;
                    font-size: 11px;
                    padding: 4px 6px;
                }
                QLineEdit:focus {
                    background-color: #1e1e1e;
                    border: 1px solid #007acc;
                    color: #ffffff;
                }
            """)
        else:
            # Add error styling - dark theme with red border
            self.server_url.setStyleSheet("""
                QLineEdit {
                    background-color: #1e1e1e;
                    border: 1px solid #f48771;
                    color: #cccccc;
                    font-family: 'Consolas', 'Monaco', monospace;
                    font-size: 11px;
                    padding: 4px 6px;
                }
                QLineEdit:focus {
                    background-color: #1e1e1e;
                    border: 1px solid #f48771;
                    color: #ffffff;
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
        """Test connection and save URL if successful (supports multiple comma-separated URLs)"""
        url_str = self.server_url.text().strip()
        test_button = self.findChild(QPushButton, "test-connection")

        # Support multiple comma-separated URLs
        urls = [url.strip() for url in url_str.split(',') if url.strip()]

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

        # Test each URL
        success_count = 0
        failed_urls = []

        for url in urls:
            try:
                # Test connection with a 5-second timeout
                response = requests.get(url, timeout=5)
                # Accept any response as valid - the server is responding
                print(f"Server {url} responded with status code: {response.status_code}")
                success_count += 1
            except requests.exceptions.Timeout:
                failed_urls.append((url, "timeout"))
                print(f"Timeout connecting to {url}")
            except requests.exceptions.ConnectionError as e:
                failed_urls.append((url, "connection error"))
                print(f"Connection error to {url}: {str(e)}")
            except Exception as e:
                failed_urls.append((url, str(e)))
                print(f"Error testing {url}: {str(e)}")

        # Always save the URL(s) - user may want to use them anyway
        self.current_server_url = url_str

        total_urls = len(urls)

        if success_count == total_urls:
            # All URLs connected successfully
            if total_urls == 1:
                test_button.setText("Connected!")
            else:
                test_button.setText(f"All {total_urls} Connected!")
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
            test_button.setToolTip("")
        elif success_count > 0:
            # Some URLs connected
            test_button.setText(f"{success_count}/{total_urls} Connected")
            test_button.setStyleSheet("""
                QPushButton#test-connection {
                    background-color: rgb(234, 179, 8);
                    color: white;
                    border: none;
                    padding: 4px 8px;
                    border-radius: 6px;
                    font-weight: bold;
                    font-size: 13px;
                }
            """)
            failed_info = "\n".join([f"• {url}: {reason}" for url, reason in failed_urls])
            test_button.setToolTip(f"Failed connections:\n{failed_info}")
        else:
            # All failed
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
            failed_info = "\n".join([f"• {url}: {reason}" for url, reason in failed_urls])
            test_button.setToolTip(f"All connections failed:\n{failed_info}")

        print(f"URL(s) saved for future attempts: {url_str}")
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
        """Generate QR code for the web interface URL - optimized for camera scanning"""
        # Create QR code instance with optimal settings for scanning
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,  # Highest error correction for better scanning
            box_size=10,  # Good box size for clarity
            border=2,     # Smaller border to maximize QR code area
        )
        
        # Add data
        qr.add_data(url)
        qr.make(fit=True)
        
        # Create image with high contrast
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Convert PIL image to QPixmap
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        
        # Create QImage from buffer
        qimage = QImage.fromData(buffer.getvalue())
        
        # Scale to display size using FAST transformation (preserves sharp edges)
        # FastTransformation keeps QR code crisp, SmoothTransformation blurs it
        return QPixmap.fromImage(qimage).scaled(
            160, 160,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation
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
                    border: 1px solid #f48771;
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
                    border: 1px solid #f48771;
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

