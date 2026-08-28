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
import uuid
from pathlib import Path
from datetime import datetime
from abc import ABC, abstractmethod
from flask import Flask, request, jsonify, send_from_directory, g
from flask_cors import CORS
from zeroconf import ServiceInfo, Zeroconf
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                            QHBoxLayout, QLabel, QLineEdit, QPushButton,
                            QMessageBox, QFormLayout, QComboBox, QGridLayout, QFrame,
                            QCheckBox, QTableWidget, QTableWidgetItem, QDialog,
                            QAbstractItemView, QHeaderView)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QObject, QUrl
from PyQt6.QtGui import QImage, QPixmap, QIcon, QFont, QColor
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget
import qrcode
from io import BytesIO
import os
import csv

def get_runtime_base_path():
    """Return the bundled app base path when frozen, otherwise the source folder."""
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
    return Path(__file__).resolve().parent

def get_app_install_path():
    """Return the executable/source folder used for writable adjacent resources."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def get_resource_directories():
    """Return resource directories in lookup order for web assets."""
    candidates = [
        get_app_install_path() / 'resources',
        Path.cwd() / 'resources',
        get_runtime_base_path() / 'resources',
    ]
    unique = []
    seen = set()
    for directory in candidates:
        resolved = directory.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique

def find_resource_file(filename):
    """Find a resource file in external or bundled resource folders."""
    safe_name = os.path.basename(filename)
    for directory in get_resource_directories():
        candidate = directory / safe_name
        if candidate.is_file():
            return directory, safe_name
    return None, safe_name

def get_default_web_background():
    """Return the default mobile page background image.

    Empty means the mobile registration pages use the plain black CSS backdrop.
    The Sim Coaches logo is shown inside the registration panel instead.
    """
    return ''

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

        # For EA Sports F1
        elif context == "EA Sports F1" and graphics_data:
            # Reject if car is in pit lane or pit box
            if hasattr(graphics_data, 'm_pitStatus') and graphics_data.m_pitStatus != 0:
                print(f"WARNING: Skipping lap - car is in pit (pitStatus: {graphics_data.m_pitStatus})")
                return False

            # Reject if lap was invalidated (corner cut, etc.)
            if hasattr(graphics_data, 'm_currentLapInvalid') and graphics_data.m_currentLapInvalid != 0:
                print(f"WARNING: Skipping lap - lap invalidated (corner cut)")
                return False

            # Only accept flying lap (1) or on-track (4) driver status
            if hasattr(graphics_data, 'm_driverStatus'):
                valid_statuses = (1, 4)  # 1=flying lap, 4=on track
                if graphics_data.m_driverStatus not in valid_statuses:
                    print(f"WARNING: Skipping lap - invalid driver status ({graphics_data.m_driverStatus})")
                    return False

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

PREFERRED_WEB_INTERFACE_PORT = 5001
WEB_INTERFACE_FALLBACK_PORTS = [5101, 5102, 5103, 5104, 5105]
WEB_INTERFACE_PORT = PREFERRED_WEB_INTERFACE_PORT

def is_tcp_port_available(port):
    """Return True when the web interface can bind the requested TCP port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('0.0.0.0', int(port)))
            return True
    except OSError:
        return False

def select_web_interface_port():
    """Pick the mobile web port, falling back when another app owns 5001."""
    global WEB_INTERFACE_PORT

    for port in [PREFERRED_WEB_INTERFACE_PORT] + WEB_INTERFACE_FALLBACK_PORTS:
        if is_tcp_port_available(port):
            WEB_INTERFACE_PORT = port
            if port != PREFERRED_WEB_INTERFACE_PORT:
                print(
                    f"[Web] Port {PREFERRED_WEB_INTERFACE_PORT} is already in use; "
                    f"using {port} for the Sender mobile page"
                )
            return port

    WEB_INTERFACE_PORT = PREFERRED_WEB_INTERFACE_PORT
    print(f"[Web] WARNING: No fallback web ports are available; trying {WEB_INTERFACE_PORT}")
    return WEB_INTERFACE_PORT

def get_web_interface_port():
    return WEB_INTERFACE_PORT

def get_simulator_probe_ports():
    return [PREFERRED_WEB_INTERFACE_PORT] + WEB_INTERFACE_FALLBACK_PORTS

def configure_firewall(web_port=None):
    """Add Windows Firewall rules to allow inbound connections.
    - Web TCP port: Web interface for iPads and other simulators
    - Port 5003 UDP: Simulator discovery broadcast
    """
    import subprocess
    web_port = int(web_port or get_web_interface_port())
    web_rule_name = (
        "Lap Time Sender - Web Interface"
        if web_port == PREFERRED_WEB_INTERFACE_PORT
        else f"Lap Time Sender - Web Interface {web_port}"
    )
    
    rules = [
        (web_rule_name, "TCP", str(web_port)),
        ("Lap Time Sender - Discovery", "UDP", "5003"),
        ("Lap Time Sender - F1 Telemetry", "UDP", "20777"),
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

def get_resources_dir():
    """Get the absolute path to the bundled 'resources' directory.

    When frozen by PyInstaller the resources are extracted into sys._MEIPASS
    (the app's _internal folder). When running from source they live next to
    this script. Relative paths fail on installed PCs because the working
    directory is not the app folder, which is why the mobile logo only showed
    up when running from source.
    """
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, 'resources')

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

def get_settings_path():
    """Get the path to the application settings file."""
    if getattr(sys, 'frozen', False):
        # Running as compiled executable
        base_path = Path(sys.executable).parent
    else:
        # Running from script
        base_path = Path(__file__).parent
    return base_path / 'settings.json'

def load_app_settings():
    """Load application settings from file."""
    settings_path = get_settings_path()
    if settings_path.exists():
        try:
            with open(settings_path, 'r') as f:
                settings = json.load(f)
                print(f"[Settings] Loaded from {settings_path}")
                return settings
        except Exception as e:
            print(f"WARNING: Could not load settings: {e}")
    return {}

def save_app_settings(settings):
    """Save application settings to file."""
    settings_path = get_settings_path()
    try:
        with open(settings_path, 'w') as f:
            json.dump(settings, f, indent=2)
        print(f"[Settings] Saved to {settings_path}")
        return True
    except Exception as e:
        print(f"WARNING: Could not save settings: {e}")
        return False

def get_sector_config_path():
    """Get the path to the custom sector challenge config file."""
    if getattr(sys, 'frozen', False):
        base_path = Path(sys.executable).parent
    else:
        base_path = Path(__file__).parent
    return base_path / 'sector_config.json'

def normalize_sector_config(config):
    """Validate and normalize a sector challenge config.

    Sectors are kept sorted by start_pct; the first sector always starts at 0.
    Returns a clean config dict, or None if the sector list is unusable.
    """
    try:
        sectors = []
        for s in config.get('sectors', []):
            start_pct = float(s['start_pct'])
            target_time = float(s['target_time'])
            if not (0 <= start_pct < 100) or target_time <= 0:
                return None
            sectors.append({'start_pct': start_pct, 'target_time': target_time})
        if not sectors:
            return None
        sectors.sort(key=lambda s: s['start_pct'])
        # Boundaries must be distinct or sector spans collapse to zero length
        starts = [s['start_pct'] for s in sectors]
        if len(set(starts)) != len(starts):
            return None
        sectors[0]['start_pct'] = 0.0
        # Spawn set the CSP companion app teleports to after a failed run.
        # HOTLAP_START puts the car just before the start/finish line.
        spawn = str(config.get('teleport_spawn', 'HOTLAP_START')).upper()
        if spawn not in ('HOTLAP_START', 'START', 'PIT', 'TIME_ATTACK'):
            spawn = 'HOTLAP_START'
        return {'enabled': bool(config.get('enabled', False)), 'sectors': sectors,
                'teleport_spawn': spawn}
    except (KeyError, TypeError, ValueError):
        return None

def load_sector_config():
    """Load the custom sector challenge config from file."""
    config_path = get_sector_config_path()
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = normalize_sector_config(json.load(f))
                if config:
                    print(f"[Sectors] Loaded {len(config['sectors'])} sectors from {config_path}")
                    return config
        except Exception as e:
            print(f"WARNING: Could not load sector config: {e}")
    return {'enabled': False, 'sectors': [{'start_pct': 0.0, 'target_time': 60.0}],
            'teleport_spawn': 'HOTLAP_START'}

def save_sector_config(config):
    """Save the custom sector challenge config to file."""
    config_path = get_sector_config_path()
    try:
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
        print(f"[Sectors] Saved to {config_path}")
        return True
    except Exception as e:
        print(f"WARNING: Could not save sector config: {e}")
        return False

def parse_time_to_seconds(text):
    """Parse a sector target time like '75', '1:15' or '1:15.500' into seconds.

    Returns None if the text is not a valid positive time.
    """
    try:
        text = str(text).strip()
        if ':' in text:
            minutes, seconds = text.split(':', 1)
            value = int(minutes) * 60 + float(seconds)
        else:
            value = float(text)
        return value if value > 0 else None
    except (ValueError, TypeError):
        return None

def get_lead_questions_path():
    """Get the path to the lead-gen survey questions config file."""
    if getattr(sys, 'frozen', False):
        base_path = Path(sys.executable).parent
    else:
        base_path = Path(__file__).parent
    return base_path / 'lead_questions.json'

def normalize_lead_questions(config):
    """Keep only well-formed questions: a prompt plus at least 2 options."""
    questions = []
    for q in (config or {}).get('questions', []):
        try:
            prompt = str(q.get('prompt', '')).strip()
            options = [str(o).strip() for o in q.get('options', []) if str(o).strip()]
        except AttributeError:
            continue
        if prompt and len(options) >= 2:
            questions.append({'prompt': prompt, 'options': options})
    return {'questions': questions}

def load_lead_questions():
    """Load the lead-gen survey questions from file."""
    config_path = get_lead_questions_path()
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = normalize_lead_questions(json.load(f))
                print(f"[Leads] Loaded {len(config['questions'])} survey questions")
                return config
        except Exception as e:
            print(f"WARNING: Could not load lead questions: {e}")
    return {'questions': []}

def save_lead_questions(config):
    """Save the lead-gen survey questions to file."""
    config_path = get_lead_questions_path()
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        print(f"[Leads] Saved survey questions to {config_path}")
        return True
    except Exception as e:
        print(f"WARNING: Could not save lead questions: {e}")
        return False

def get_leads_csv_path():
    """Get the path to the captured leads CSV file."""
    if getattr(sys, 'frozen', False):
        base_path = Path(sys.executable).parent
    else:
        base_path = Path(__file__).parent
    return base_path / 'leads.csv'

# The mobile page retries timed-out registration POSTs, so remember the last
# appended lead to avoid duplicate rows from the same submission.
_last_lead_row = {'key': None, 'time': 0.0}

def append_lead_to_csv(driver_name, email, phone, answers_by_prompt):
    """Append a lead-gen registration to leads.csv.

    Columns are timestamp, driver_name, email, phone, then one column per
    configured survey question. If the questions have changed since the file
    was created, the old file is rotated to a dated backup so columns and
    answers always line up.
    """
    global _last_lead_row
    try:
        if main_window is not None and hasattr(main_window, 'lead_questions'):
            questions = main_window.lead_questions.get('questions', [])
        else:
            questions = load_lead_questions().get('questions', [])
        prompts = [q['prompt'] for q in questions]
        fieldnames = ['timestamp', 'driver_name', 'email', 'phone'] + prompts

        row_key = (driver_name, email, phone, tuple(sorted(answers_by_prompt.items())))
        now = time.time()
        if _last_lead_row['key'] == row_key and now - _last_lead_row['time'] < 120:
            print(f"[Leads] Skipping duplicate lead row for {driver_name}")
            return True

        path = get_leads_csv_path()
        if path.exists():
            with open(path, 'r', newline='', encoding='utf-8') as f:
                header = next(csv.reader(f), None)
            if header != fieldnames:
                backup = path.with_name(f"leads_backup_{time.strftime('%Y%m%d_%H%M%S')}.csv")
                path.rename(backup)
                print(f"[Leads] Question columns changed - previous leads moved to {backup.name}")

        write_header = not path.exists()
        row = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'driver_name': driver_name,
            'email': email,
            'phone': phone,
        }
        for prompt in prompts:
            row[prompt] = answers_by_prompt.get(prompt, '')

        with open(path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

        _last_lead_row = {'key': row_key, 'time': now}
        print(f"[Leads] Saved lead for {driver_name} to {path.name}")
        return True
    except Exception as e:
        print(f"WARNING: Could not save lead to CSV: {e}")
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

# ============================================
# SMS NOTIFICATION FUNCTIONS
# ============================================

SMS_CONFIG_FILE = "sms_config.json"
sms_config = {
    "enabled": False,
    "provider": "textbelt",
    "textbelt_key": "",
    "textbelt_sender": "",
    "twilio_account_sid": "",
    "twilio_auth_token": "",
    "twilio_from_number": "",
    "message_template": "Hi {name}! It's your turn to race at {simulator}. Head over now!"
}

def get_sms_config_path():
    """Get the path to the SMS config file."""
    if getattr(sys, 'frozen', False):
        base_path = Path(sys.executable).parent
    else:
        base_path = Path(__file__).parent
    return base_path / SMS_CONFIG_FILE

def load_sms_config():
    """Load SMS configuration from file"""
    global sms_config
    config_path = get_sms_config_path()
    print(f"[SMS] === Loading SMS Configuration ===")
    print(f"[SMS] Config path: {config_path}")

    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                loaded = json.load(f)
                sms_config.update(loaded)
                print(f"[SMS] Loaded config: provider={loaded.get('provider')}, enabled={loaded.get('enabled')}")

                # Check if properly configured
                if sms_config.get("provider") == "textbelt" and sms_config.get("textbelt_key"):
                    sms_config["enabled"] = True
                    key = sms_config.get("textbelt_key", "")
                    print(f"[SMS] Textbelt notifications ENABLED")
                    print(f"[SMS] API Key: {key[:8]}...{key[-4:] if len(key) > 12 else ''}")
                elif sms_config.get("provider") == "twilio" and sms_config.get("twilio_account_sid"):
                    sms_config["enabled"] = True
                    print("[SMS] Twilio notifications ENABLED")
                else:
                    sms_config["enabled"] = False
                    print("[SMS] SMS DISABLED - missing credentials")
                    print(f"[SMS]   provider={sms_config.get('provider')}")
                    print(f"[SMS]   textbelt_key present: {bool(sms_config.get('textbelt_key'))}")
        except Exception as e:
            print(f"[SMS] ERROR loading config: {e}")
            import traceback
            traceback.print_exc()
    else:
        # Create default config file
        save_sms_config()
        print(f"[SMS] Created new config file: {config_path}")
        print("[SMS] Edit this file with your Textbelt API key to enable SMS")

def save_sms_config():
    """Save SMS configuration to file"""
    try:
        config_path = get_sms_config_path()
        with open(config_path, 'w') as f:
            json.dump(sms_config, f, indent=2)
    except Exception as e:
        print(f"[SMS] Error saving config: {e}")

def format_phone_e164(phone_number):
    """Format phone number to E.164 format (+1XXXXXXXXXX) for SMS APIs."""
    import re
    # Remove everything except digits and +
    clean = re.sub(r'[^\d+]', '', phone_number or '')

    if not clean:
        return None

    # Already has + prefix - validate and return
    if clean.startswith('+'):
        return clean if len(clean) >= 11 else None

    # Has country code (11 digits starting with 1)
    if clean.startswith('1') and len(clean) == 11:
        return '+' + clean

    # 10-digit US number - add +1
    if len(clean) == 10:
        return '+1' + clean

    # For any other case, try adding +1 if reasonable length
    if 10 <= len(clean) <= 11:
        if not clean.startswith('1'):
            return '+1' + clean
        return '+' + clean

    return None  # Invalid

def is_valid_email_address(email):
    """Basic email shape check for required lead-gen fields."""
    email = str(email or '').strip()
    if not email or ' ' in email or '@' not in email:
        return False
    local, _, domain = email.rpartition('@')
    return bool(local and '.' in domain and not domain.startswith('.') and not domain.endswith('.'))

def is_lead_gen_enabled():
    """Return whether the driver sign-in flow should require contact info."""
    window = globals().get('main_window')
    return bool(
        window
        and hasattr(window, 'email_field_enabled')
        and window.email_field_enabled.isChecked()
    )

def send_sms_notification(phone_number, driver_name, simulator_name="your simulator"):
    """Send SMS notification via configured provider"""
    global sms_config

    print(f"[SMS] === SMS Notification Request ===")
    print(f"[SMS] To: {phone_number}, Driver: {driver_name}, Simulator: {simulator_name}")
    print(f"[SMS] Config enabled: {sms_config.get('enabled')}, Provider: {sms_config.get('provider')}")

    if not sms_config.get("enabled"):
        print(f"[SMS] SKIPPED: SMS is disabled in config")
        return False

    # Clean up phone number using unified formatter
    clean_phone = format_phone_e164(phone_number)
    if not clean_phone:
        print(f"[SMS] SKIPPED: Could not format phone '{phone_number}'")
        return False
    print(f"[SMS] Formatted phone: {clean_phone}")

    # Format message
    message = sms_config.get("message_template", "It's your turn to race!").format(
        name=driver_name,
        simulator=simulator_name
    )
    print(f"[SMS] Message: {message}")

    provider = sms_config.get("provider", "textbelt")
    print(f"[SMS] Sending via {provider}...")

    if provider == "textbelt":
        return send_sms_textbelt(clean_phone, message)
    elif provider == "twilio":
        return send_sms_twilio(clean_phone, message)
    else:
        print(f"[SMS] ERROR: Unknown provider '{provider}'")
        return False

def send_sms_textbelt(phone_number, message):
    """Send SMS via Textbelt API"""
    try:
        api_key = sms_config.get("textbelt_key", "")
        print(f"[SMS] Textbelt API key: {api_key[:8]}...{api_key[-4:] if len(api_key) > 12 else '(short)'}")

        data = {
            "phone": phone_number,
            "message": message,
            "key": api_key
        }

        # Add sender name for regulatory compliance
        sender = sms_config.get("textbelt_sender", "")
        if sender:
            data["sender"] = sender
            print(f"[SMS] Sender name: {sender}")

        print(f"[SMS] Posting to textbelt.com/text...")
        response = requests.post(
            "https://textbelt.com/text",
            data=data,
            timeout=10
        )

        print(f"[SMS] Response status: {response.status_code}")
        result = response.json()
        print(f"[SMS] Response body: {result}")

        if result.get("success"):
            print(f"[SMS] SUCCESS! Sent to {phone_number}")
            print(f"[SMS] Quota remaining: {result.get('quotaRemaining', 'unknown')}")
            return True
        else:
            print(f"[SMS] FAILED: {result.get('error', 'Unknown error')}")
            return False

    except Exception as e:
        print(f"[SMS] Textbelt ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

def send_sms_twilio(phone_number, message):
    """Send SMS via Twilio API"""
    try:
        # Ensure phone has + prefix for Twilio
        if not phone_number.startswith("+"):
            phone_number = "+" + phone_number

        account_sid = sms_config["twilio_account_sid"]
        auth_token = sms_config["twilio_auth_token"]
        from_number = sms_config["twilio_from_number"]

        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

        response = requests.post(
            url,
            auth=(account_sid, auth_token),
            data={
                "To": phone_number,
                "From": from_number,
                "Body": message
            },
            timeout=10
        )

        if response.status_code in [200, 201]:
            print(f"[SMS] Twilio sent to {phone_number}: {message}")
            return True
        else:
            print(f"[SMS] Twilio failed: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        print(f"[SMS] Twilio error: {e}")
        return False

# ============================================
# PROACTIVE SMS NOTIFICATION SYSTEM
# Sends "heads up" SMS when estimated wait < 10 minutes
# ============================================

notified_queue_ids = set()  # Track who's been texted

# ============================================
# ASYNC SMS QUEUE SYSTEM
# Sends SMS in background to avoid blocking request handlers
# ============================================
import queue as queue_module

sms_send_queue = queue_module.Queue()
_sms_worker_started = False

def sms_background_worker():
    """Background worker that processes SMS queue - never blocks request handlers"""
    print("[SMS] Background worker started")
    while True:
        try:
            task = sms_send_queue.get()
            if task is None:
                break  # Shutdown signal
            phone, message, task_type = task
            try:
                if sms_config.get("provider") == "twilio":
                    send_sms_twilio(phone, message)
                else:
                    send_sms_textbelt(phone, message)
                print(f"[SMS Background] {task_type} sent to {phone}")
            except Exception as e:
                print(f"[SMS Background] Error sending {task_type}: {e}")
            sms_send_queue.task_done()
        except Exception as e:
            print(f"[SMS Background] Worker error: {e}")

def start_sms_worker():
    """Start the SMS background worker thread (idempotent)"""
    global _sms_worker_started
    if not _sms_worker_started:
        _sms_worker_started = True
        sms_thread = threading.Thread(target=sms_background_worker, daemon=True)
        sms_thread.start()
        print("[SMS] Background worker thread started")

def queue_sms_async(phone, message, task_type="notification"):
    """Queue an SMS for background sending - returns immediately"""
    start_sms_worker()  # Ensure worker is running
    sms_send_queue.put((phone, message, task_type))
    print(f"[SMS] Queued {task_type} for {phone} (queue size: {sms_send_queue.qsize()})")

def send_headsup_notification(phone_number, name, wait_minutes):
    """Send proactive heads-up SMS when guest's turn is approaching"""
    if not sms_config.get("enabled"):
        print(f"[SMS] Heads-up SMS disabled - SMS not enabled")
        return False

    # Get template or use default
    template = sms_config.get("headsup_message_template",
        "Hey {name}! It's almost your turn to race! Head back to the simulators now - you're up in about {wait} minutes!")

    # Format message
    message = template.format(name=name, wait=int(wait_minutes))

    # Clean phone number using unified formatter
    clean_phone = format_phone_e164(phone_number)
    if not clean_phone:
        print(f"[SMS] Heads-up skipped: Invalid phone '{phone_number}'")
        return False

    print(f"[SMS] Sending heads-up to {name}: {message}")

    provider = sms_config.get("provider", "textbelt")
    if provider == "textbelt":
        return send_sms_textbelt(clean_phone, message)
    elif provider == "twilio":
        return send_sms_twilio(clean_phone, message)

    return False

def queue_notification_checker():
    """Background thread that checks queue and sends proactive SMS"""
    global notified_queue_ids
    import time as time_module

    print("[SMS] Queue notification checker started")

    while True:
        time_module.sleep(30)  # Check every 30 seconds

        if not sms_config.get("enabled"):
            continue

        try:
            # Get receiver URL
            receiver_url = None
            if main_window and hasattr(main_window, 'current_server_url'):
                url = main_window.current_server_url
                if url:
                    receiver_url = url.split(',')[0].strip()

            if not receiver_url:
                continue

            # Fetch queue from receiver
            import requests
            response = requests.get(f"{receiver_url}/api/queue", timeout=5)
            if not response.ok:
                continue

            queue_data = response.json()
            if not queue_data.get('success'):
                continue

            queue = queue_data.get('queue', [])
            if not queue:
                notified_queue_ids.clear()
                continue

            # Get stats for average session time
            try:
                stats_response = requests.get(f"{receiver_url}/api/queue/stats", timeout=5)
                stats = stats_response.json() if stats_response.ok else {}
                avg_session_minutes = stats.get('avg_session_minutes', 8)
            except:
                avg_session_minutes = 8

            # Count available simulators (rough estimate based on discovered sims)
            num_sims = max(1, len(getattr(main_window, 'discovered_simulators', [])) or 1)

            for guest in queue:
                guest_id = guest.get('id')
                phone = guest.get('phone')
                name = guest.get('name', 'Guest')
                position = guest.get('position', 1)

                # Skip if no phone or already notified
                if not phone or guest_id in notified_queue_ids:
                    continue

                # Calculate estimated wait (position / num_sims) * avg_session_time
                estimated_wait = (position / num_sims) * avg_session_minutes

                if estimated_wait < 10:  # Under 10 minutes threshold
                    success = send_headsup_notification(phone, name, estimated_wait)
                    if success:
                        notified_queue_ids.add(guest_id)
                        print(f"[SMS] Sent heads-up to {name} (wait: {estimated_wait:.0f} min)")
                    else:
                        print(f"[SMS] Failed to send heads-up to {name}")

            # Clean up: remove IDs no longer in queue
            current_ids = {g.get('id') for g in queue}
            notified_queue_ids.intersection_update(current_ids)

        except Exception as e:
            print(f"[SMS] Queue check error: {e}")

def start_queue_notification_thread():
    """Start the background queue notification checker"""
    import threading
    thread = threading.Thread(target=queue_notification_checker, daemon=True)
    thread.start()
    return thread

def calculate_wait_display(position):
    """Calculate wait time display based on queue position and available sims."""
    try:
        # Get simulators via internal discovery
        current_ip = get_local_ip()
        discovered = SimulatorDiscovery.discover()

        # Build list with current sim
        all_sims = []
        if main_window:
            all_sims.append({
                'current_driver': main_window.driver_name.text() or None,
                'reachable': True
            })
        for sim in discovered:
            if sim.get('ip') != current_ip:
                all_sims.append(sim)

        # Count available (reachable + no current driver)
        available = sum(1 for s in all_sims
                       if s.get('reachable', True) != False
                       and not (s.get('current_driver') or '').strip())
        total_sims = max(1, len(all_sims))
    except Exception as e:
        print(f"[Queue] Error getting simulators: {e}")
        available = 0
        total_sims = 1

    # Special cases
    if position == 1 and available > 0:
        return "You're Next!"
    if position <= available and available > 0:
        return "Immediately"

    # Calculate wait: (position / num_sims) * 6 minutes
    AVG_SESSION_MINUTES = 6
    estimated_wait = (position / total_sims) * AVG_SESSION_MINUTES

    if estimated_wait < 1:
        return "< 1 minute"
    elif estimated_wait < 2:
        return "~1 minute"
    else:
        return f"~{int(round(estimated_wait))} minutes"

# Create Flask app with no output
app = Flask(__name__)
app.logger.disabled = True
import logging
log = logging.getLogger('werkzeug')
log.disabled = True
CORS(app)  # Enable CORS for all routes

# ============================================
# ENTERPRISE-GRADE INSTRUMENTATION
# Correlation IDs and timing for request tracing
# ============================================
import uuid
from functools import wraps

# Slow request threshold (milliseconds)
SLOW_REQUEST_THRESHOLD_MS = 2000

# Track slow requests for visibility
slow_request_count = 0
last_slow_request_time = None
last_slow_request_path = None

@app.before_request
def before_request_timing():
    """Add correlation ID and start timing for every request"""
    from flask import g
    g.request_start_time = time.time()
    g.correlation_id = request.headers.get('X-Correlation-ID') or str(uuid.uuid4())[:8]

@app.after_request
def after_request_timing(response):
    """Log timing and detect slow requests"""
    global slow_request_count, last_slow_request_time, last_slow_request_path
    from flask import g

    if hasattr(g, 'request_start_time'):
        duration_ms = (time.time() - g.request_start_time) * 1000
        correlation_id = getattr(g, 'correlation_id', 'unknown')

        # Add correlation ID to response headers
        response.headers['X-Correlation-ID'] = correlation_id
        response.headers['X-Response-Time-Ms'] = str(int(duration_ms))

        # Log slow requests prominently
        if duration_ms > SLOW_REQUEST_THRESHOLD_MS:
            slow_request_count += 1
            last_slow_request_time = datetime.now()
            last_slow_request_path = request.path

            print(f"\n{'='*60}")
            print(f"⚠️  SLOW REQUEST DETECTED [{correlation_id}]")
            print(f"Time: {datetime.now().strftime('%H:%M:%S')}")
            print(f"Path: {request.path}")
            print(f"Method: {request.method}")
            print(f"Duration: {duration_ms:.0f}ms (threshold: {SLOW_REQUEST_THRESHOLD_MS}ms)")
            print(f"Client: {request.remote_addr}")
            print(f"Total slow requests this session: {slow_request_count}")
            print(f"{'='*60}\n")

    return response

# ============================================
# CONNECTION RELIABILITY
# Connection pooling, heartbeat, and circuit breaker
# ============================================
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Create a session with connection pooling and auto-retry
receiver_session = requests.Session()
retry_strategy = Retry(
    total=2,  # Max 2 retries
    backoff_factor=0.5,  # Wait 0.5s, then 1s between retries
    status_forcelist=[500, 502, 503, 504],  # Retry on server errors
)
adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_connections=10,
    pool_maxsize=10
)
receiver_session.mount('http://', adapter)
receiver_session.mount('https://', adapter)

class CircuitBreaker:
    """
    Circuit breaker pattern for automatic failure recovery.

    When the receiver fails multiple times in a row, stop trying for a while
    to avoid overwhelming it and to allow recovery.

    States:
    - CLOSED: Normal operation, requests go through
    - OPEN: Too many failures, requests are blocked
    - HALF_OPEN: Testing if service recovered
    """

    def __init__(self, failure_threshold=3, recovery_time=30):
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.failures = 0
        self.last_failure_time = 0
        self.state = 'closed'
        self._lock = threading.Lock()

    def is_open(self):
        """Check if circuit is open (blocking requests)"""
        with self._lock:
            if self.state == 'closed':
                return False

            # Check if recovery time has passed
            if time.time() - self.last_failure_time > self.recovery_time:
                self.state = 'half_open'
                return False

            return True

    def record_failure(self):
        """Record a failure"""
        with self._lock:
            self.failures += 1
            self.last_failure_time = time.time()

            if self.failures >= self.failure_threshold:
                self.state = 'open'
                print(f"\n{'='*60}")
                print(f"⚠️  CIRCUIT BREAKER OPEN")
                print(f"Receiver failed {self.failures} times in a row")
                print(f"Blocking requests for {self.recovery_time}s")
                print(f"{'='*60}\n")

    def record_success(self):
        """Record a success"""
        with self._lock:
            if self.state == 'half_open':
                print("[Circuit Breaker] Service recovered, closing circuit")
            self.failures = 0
            self.state = 'closed'

    def get_status(self):
        """Get current status for diagnostics"""
        return {
            'state': self.state,
            'failures': self.failures,
            'last_failure': self.last_failure_time,
            'recovery_time': self.recovery_time
        }

# Global circuit breaker instance
receiver_circuit = CircuitBreaker(failure_threshold=3, recovery_time=30)

# Heartbeat tracking
_heartbeat_latency = None
_heartbeat_last_success = None
_heartbeat_thread_started = False

def heartbeat_thread():
    """Background thread that monitors receiver connectivity"""
    global _heartbeat_latency, _heartbeat_last_success

    print("[Heartbeat] Background monitor started")

    while True:
        try:
            receiver_url = get_receiver_base_url()
            if receiver_url:
                start = time.time()
                try:
                    resp = receiver_session.get(f"{receiver_url}/health", timeout=2)
                    latency = (time.time() - start) * 1000

                    if resp.status_code == 200:
                        _heartbeat_latency = latency
                        _heartbeat_last_success = time.time()
                        receiver_circuit.record_success()

                        if latency > 500:
                            print(f"[Heartbeat] ⚠️ High latency: {latency:.0f}ms")
                    else:
                        print(f"[Heartbeat] ❌ Receiver returned {resp.status_code}")
                        receiver_circuit.record_failure()
                except requests.Timeout:
                    print(f"[Heartbeat] ❌ Timeout after 2s")
                    receiver_circuit.record_failure()
                except requests.ConnectionError as e:
                    print(f"[Heartbeat] ❌ Connection error: {e}")
                    receiver_circuit.record_failure()
        except Exception as e:
            print(f"[Heartbeat] Error: {e}")

        time.sleep(10)  # Check every 10 seconds

def start_heartbeat_thread():
    """Start the heartbeat thread if not already running"""
    global _heartbeat_thread_started
    if not _heartbeat_thread_started:
        _heartbeat_thread_started = True
        thread = threading.Thread(target=heartbeat_thread, daemon=True)
        thread.start()

def forward_to_receiver(path, data=None, method='POST', timeout=3):
    """
    Forward a request to the receiver with circuit breaker protection.

    Features:
    - Uses connection pooling (keep-alive)
    - Automatic retry on transient failures
    - Circuit breaker to prevent cascading failures
    - Reduced timeout (3s instead of 5s)
    """
    receiver_url = get_receiver_base_url()
    if not receiver_url:
        return None, "Receiver not connected"

    # Check circuit breaker
    if receiver_circuit.is_open():
        return None, "Circuit breaker open - receiver unavailable"

    url = f"{receiver_url}{path}"

    try:
        if method == 'POST':
            response = receiver_session.post(
                url,
                json=data,
                headers={'Content-Type': 'application/json'},
                timeout=timeout
            )
        else:
            response = receiver_session.get(url, timeout=timeout)

        receiver_circuit.record_success()
        return response, None

    except requests.Timeout:
        receiver_circuit.record_failure()
        return None, f"Timeout after {timeout}s"
    except requests.ConnectionError as e:
        receiver_circuit.record_failure()
        return None, f"Connection error: {e}"
    except Exception as e:
        receiver_circuit.record_failure()
        return None, f"Error: {e}"

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
            background-color: #000000;
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
        input[type="text"], input[type="email"], input[type="tel"] {
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
        /* Connection status indicator */
        #connectionStatus {
            position: fixed; top: 10px; right: 10px; padding: 6px 12px;
            border-radius: 20px; font-size: 12px; font-weight: bold;
            background-color: #2d2d30; border: 1px solid #3e3e42;
            z-index: 1000; transition: all 0.3s ease;
        }
        #connectionStatus.connected { color: #4ec9b0; border-color: #4ec9b0; }
        #connectionStatus.connecting { color: #ce9178; border-color: #ce9178; animation: pulse 1.5s infinite; }
        #connectionStatus.disconnected { color: #f48771; border-color: #f48771; }
        button:disabled { background-color: #555; cursor: not-allowed; opacity: 0.7; }
        /* Lead-gen survey wizard (one question per page) */
        .survey-question { margin-bottom: 18px; }
        .survey-question > label { font-size: 18px; color: #cccccc; margin-bottom: 12px; }
        .survey-option {
            display: block; background-color: #2d2d30; border: 1px solid #3e3e42;
            border-radius: 4px; padding: 13px 12px; margin-bottom: 8px;
            color: #cccccc; font-size: 16px; cursor: pointer;
        }
        .survey-option input[type="radio"] { margin-right: 10px; transform: scale(1.3); accent-color: #007acc; }
        .survey-option:hover { border-color: #007acc; }
        .survey-option:has(input:checked) { border-color: #007acc; background-color: #1e1e1e; }
        .wizard-step { display: none; }
        .wizard-step.active { display: block; animation: stepSlideIn 0.25s ease; }
        @keyframes stepSlideIn { from { opacity: 0; transform: translateX(45px); } to { opacity: 1; transform: none; } }
        .wizard-progress { text-align: center; color: #9d9d9d; font-size: 13px; margin-bottom: 12px; }
        .wizard-nav { display: flex; gap: 10px; }
        #backBtn { flex: 0 0 auto; width: 70px; background-color: #3e3e42; }
        #backBtn:hover { background-color: #4e4e52; }
        #nextBtn { flex: 1; }
    </style>
</head>
<body>
    <div id="connectionStatus" class="connecting">🔄 Connecting...</div>
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

        <div class="wizard-step active" id="stepContact">
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
            <div class="input-group">
                <label for="driverPhone">📱 Phone Number:</label>
                <input type="tel" id="driverPhone" placeholder="Enter phone number (optional)">
                <small id="driverPhoneHelp" style="color: #6e6e6e; font-style: italic;">Optional for partner race result integrations</small>
            </div>
        </div>
        <div id="surveySteps"></div>
        <div class="wizard-progress" id="wizardProgress" style="display: none;"></div>
        <div class="wizard-nav">
            <button type="button" id="backBtn" onclick="wizardBack()" style="display: none;">←</button>
            <button type="button" id="nextBtn" onclick="wizardNext()">🏁 Register as Active Driver</button>
        </div>
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
        // Connection state management
        let isConnected = false;
        let connectionAttempts = 0;
        let pollInterval = 1000;
        const maxPollInterval = 10000;
        let pendingSubmission = null;

        // Lead-gen survey wizard: contact info first, then one question per
        // page. Questions cannot be skipped - Next requires an answer.
        let surveyQuestions = [];
        let lastSurveyJson = '';
        let surveyActive = false;
        let currentStep = 0;   // 0 = contact info, 1..N = survey questions

        function escapeHtml(s) {
            return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
        }

        function totalSteps() { return surveyActive ? 1 + surveyQuestions.length : 1; }

        function renderSurvey() {
            const host = document.getElementById('surveySteps');
            let html = '';
            for (const q of surveyQuestions) {
                html += '<div class="wizard-step survey-step" id="stepQ' + q.id + '">' +
                        '<div class="survey-question"><label>' + escapeHtml(q.prompt) + '</label>';
                for (const opt of q.options) {
                    html += '<label class="survey-option"><input type="radio" name="survey_q' + q.id +
                            '" value="' + escapeHtml(opt) + '">' + escapeHtml(opt) + '</label>';
                }
                html += '</div></div>';
            }
            host.innerHTML = html;
            currentStep = 0;
            updateWizard();
        }

        function updateWizard() {
            document.getElementById('stepContact').classList.toggle('active', currentStep === 0);
            surveyQuestions.forEach((q, i) => {
                const el = document.getElementById('stepQ' + q.id);
                if (el) el.classList.toggle('active', surveyActive && currentStep === i + 1);
            });
            const progress = document.getElementById('wizardProgress');
            if (surveyActive && currentStep > 0) {
                progress.textContent = 'Question ' + currentStep + ' of ' + surveyQuestions.length;
                progress.style.display = 'block';
            } else {
                progress.style.display = 'none';
            }
            document.getElementById('backBtn').style.display = currentStep > 0 ? 'block' : 'none';
            if (isConnected) {
                document.getElementById('nextBtn').textContent =
                    (currentStep === totalSteps() - 1) ? '🏁 Register as Active Driver' : 'Next →';
            }
        }

        function hideStatus() {
            document.getElementById('status').style.display = 'none';
        }

        function showStepError(message) {
            const statusDiv = document.getElementById('status');
            statusDiv.textContent = message;
            statusDiv.className = 'status error';
            statusDiv.style.display = 'block';
        }

        function validateContactStep() {
            const driverName = document.getElementById('driverName').value.trim();
            const driverEmail = document.getElementById('driverEmail')?.value?.trim() || '';
            const driverPhone = document.getElementById('driverPhone')?.value?.trim() || '';
            const emailInput = document.getElementById('driverEmail');
            const leadGenEnabled = document.getElementById('emailGroup').style.display !== 'none';
            if (!driverName) return 'Please enter your name';
            if (leadGenEnabled && !driverEmail) return 'Please enter your email address';
            if (leadGenEnabled && emailInput && !emailInput.checkValidity()) return 'Please enter a valid email address';
            if (leadGenEnabled && !driverPhone) return 'Please enter your phone number';
            return null;
        }

        function wizardBack() {
            if (currentStep > 0) {
                currentStep--;
                updateWizard();
                hideStatus();
            }
        }

        async function wizardNext() {
            if (currentStep === 0) {
                const err = validateContactStep();
                if (err) { showStepError(err); return; }
            } else {
                const q = surveyQuestions[currentStep - 1];
                if (!q || !document.querySelector('input[name="survey_q' + q.id + '"]:checked')) {
                    showStepError('Please select an answer to continue');
                    return;
                }
            }
            hideStatus();
            if (currentStep === totalSteps() - 1) {
                await updateDriver();
            } else {
                currentStep++;
                updateWizard();
            }
        }

        function collectSurveyAnswers() {
            // Returns {answers} or {missing: prompt} for the first unanswered question
            const answers = {};
            if (!surveyActive) return { answers };
            for (const q of surveyQuestions) {
                const sel = document.querySelector('input[name="survey_q' + q.id + '"]:checked');
                if (!sel) return { missing: q.prompt };
                answers[q.id] = sel.value;
            }
            return { answers };
        }

        // Enterprise-grade submission with timeout and retry
        const REQUEST_TIMEOUT_MS = 5000;  // 5 second timeout per attempt
        const MAX_RETRIES = 3;

        async function submitWithRetry(url, options, maxRetries = MAX_RETRIES) {
            for (let attempt = 1; attempt <= maxRetries; attempt++) {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

                try {
                    const startTime = Date.now();
                    const response = await fetch(url, {
                        ...options,
                        signal: controller.signal
                    });
                    clearTimeout(timeoutId);

                    const duration = Date.now() - startTime;
                    if (duration > 3000) {
                        console.warn('⚠️ Slow response: ' + duration + 'ms');
                    }

                    return await response.json();
                } catch (e) {
                    clearTimeout(timeoutId);

                    if (e.name === 'AbortError') {
                        console.log('Request timeout on attempt ' + attempt + '/' + maxRetries);
                    } else {
                        console.log('Request failed on attempt ' + attempt + '/' + maxRetries + ': ' + e.message);
                    }

                    if (attempt === maxRetries) {
                        throw new Error('Request failed after ' + maxRetries + ' attempts');
                    }

                    // Wait before retry (exponential backoff)
                    await new Promise(r => setTimeout(r, 1000 * attempt));
                }
            }
        }

        function updateConnectionStatus(connected, message) {
            const indicator = document.getElementById('connectionStatus');
            const nextBtn = document.getElementById('nextBtn');
            isConnected = connected;

            if (connected) {
                indicator.textContent = '🟢 Connected';
                indicator.className = 'connected';
                nextBtn.disabled = false;
                updateWizard();  // restores the Next/Register label for the current step
            } else {
                indicator.textContent = message || '🔴 Reconnecting...';
                indicator.className = message ? 'connecting' : 'disconnected';
                nextBtn.disabled = true;
                nextBtn.textContent = 'Waiting for server...';
            }
        }

        async function waitForServer() {
            const maxAttempts = 60;  // Up to 2 minutes of trying
            updateConnectionStatus(false, '🔄 Connecting...');

            while (connectionAttempts < maxAttempts) {
                connectionAttempts++;
                try {
                    const response = await fetch('/health', {
                        method: 'GET',
                        signal: AbortSignal.timeout ? AbortSignal.timeout(3000) : undefined
                    });
                    if (response.ok) {
                        updateConnectionStatus(true);
                        connectionAttempts = 0;
                        pollInterval = 1000;
                        return true;
                    }
                } catch (e) {
                    console.log('Connection attempt ' + connectionAttempts + ' failed, retrying...');
                }
                updateConnectionStatus(false, '🔄 Connecting... (' + connectionAttempts + ')');
                await new Promise(r => setTimeout(r, 2000));
            }
            updateConnectionStatus(false, '🔴 Server unavailable');
            return false;
        }

        async function updateDriver() {
            const driverName = document.getElementById('driverName').value.trim();
            const driverEmail = document.getElementById('driverEmail')?.value?.trim() || '';
            const driverPhone = document.getElementById('driverPhone')?.value?.trim() || '';
            const statusDiv = document.getElementById('status');
            const emailGroup = document.getElementById('emailGroup');
            const leadGenEnabled = emailGroup.style.display !== 'none';
            const driverEmailInput = document.getElementById('driverEmail');
            const submitEmail = leadGenEnabled ? driverEmail : '';
            const submitPhone = leadGenEnabled ? driverPhone : '';

            if (!driverName) {
                statusDiv.textContent = 'Please enter your name';
                statusDiv.className = 'status error';
                statusDiv.style.display = 'block';
                return;
            }
            if (leadGenEnabled && !driverEmail) {
                statusDiv.textContent = 'Please enter your email address';
                statusDiv.className = 'status error';
                statusDiv.style.display = 'block';
                return;
            }
            if (leadGenEnabled && driverEmailInput && !driverEmailInput.checkValidity()) {
                statusDiv.textContent = 'Please enter a valid email address';
                statusDiv.className = 'status error';
                statusDiv.style.display = 'block';
                return;
            }
            if (leadGenEnabled && !driverPhone) {
                statusDiv.textContent = 'Please enter your phone number';
                statusDiv.className = 'status error';
                statusDiv.style.display = 'block';
                return;
            }

            const survey = collectSurveyAnswers();
            if (survey.missing) {
                statusDiv.textContent = 'Please answer: ' + survey.missing;
                statusDiv.className = 'status error';
                statusDiv.style.display = 'block';
                return;
            }
            const surveyAnswers = survey.answers;

            // Check connection first
            if (!isConnected) {
                statusDiv.textContent = '⏳ Waiting for server connection...';
                statusDiv.className = 'status error';
                statusDiv.style.display = 'block';
                pendingSubmission = { driver_name: driverName, driver_email: submitEmail, driver_phone: submitPhone, survey_answers: surveyAnswers };
                return;
            }

            try {
                // Submit with timeout and retry for reliability
                const data = await submitWithRetry('/set_driver', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ driver_name: driverName, driver_email: submitEmail, driver_phone: submitPhone, survey_answers: surveyAnswers })
                }, 3);

                if (data.success) {
                    statusDiv.textContent = '✅ Registered as ' + driverName + '!';
                    statusDiv.className = 'status success';
                    document.getElementById('currentDriverName').textContent = driverName;
                    document.getElementById('currentDriverBox').style.display = 'flex';
                    pendingSubmission = null;
                    // Reset the wizard for the next customer
                    document.getElementById('driverName').value = '';
                    if (document.getElementById('driverEmail')) document.getElementById('driverEmail').value = '';
                    if (document.getElementById('driverPhone')) document.getElementById('driverPhone').value = '';
                    document.querySelectorAll('#surveySteps input[type="radio"]').forEach(r => { r.checked = false; });
                    currentStep = 0;
                    updateWizard();
                } else {
                    statusDiv.textContent = data.error ? '❌ ' + data.error : '❌ Failed to register';
                    statusDiv.className = 'status error';
                }
                statusDiv.style.display = 'block';
            } catch (error) {
                statusDiv.textContent = '⏳ Queued - will retry when connected...';
                statusDiv.className = 'status error';
                statusDiv.style.display = 'block';
                pendingSubmission = { driver_name: driverName, driver_email: submitEmail, driver_phone: submitPhone, survey_answers: surveyAnswers };
                updateConnectionStatus(false);
            }
        }

        // Auto-retry pending submissions when connection restored
        async function retryPendingSubmission() {
            if (pendingSubmission && isConnected) {
                console.log('Retrying pending submission...');
                document.getElementById('driverName').value = pendingSubmission.driver_name;
                if (pendingSubmission.driver_email) {
                    document.getElementById('driverEmail').value = pendingSubmission.driver_email;
                }
                if (pendingSubmission.driver_phone) {
                    document.getElementById('driverPhone').value = pendingSubmission.driver_phone;
                }
                if (pendingSubmission.survey_answers) {
                    for (const [qid, val] of Object.entries(pendingSubmission.survey_answers)) {
                        document.querySelectorAll('input[name="survey_q' + qid + '"]').forEach(r => {
                            if (r.value === val) r.checked = true;
                        });
                    }
                }
                await updateDriver();
            }
        }

        function clearTimerClasses() {
            const el = document.getElementById('timerStatus');
            el.classList.remove('timer-active', 'timer-warning', 'timer-expired', 'laps-active', 'laps-outlap');
        }

        let consecutiveErrors = 0;

        function checkTimerAndSettings() {
            fetch('/get_settings')
                .then(response => {
                    if (!response.ok) throw new Error('Server error');
                    return response.json();
                })
                .then(data => {
                    // Connection restored - reset backoff
                    consecutiveErrors = 0;
                    pollInterval = 1000;
                    if (!isConnected) {
                        updateConnectionStatus(true);
                        retryPendingSubmission();
                    }

                    // Lead Gen contact fields
                    const leadGenEnabled = Boolean(data.lead_gen_enabled || data.require_email);
                    const emailGroup = document.getElementById('emailGroup');
                    const phoneGroup = document.getElementById('phoneGroup');
                    const emailInput = document.getElementById('driverEmail');
                    if (emailGroup) emailGroup.style.display = leadGenEnabled ? 'block' : 'none';
                    if (phoneGroup) phoneGroup.style.display = leadGenEnabled ? 'block' : 'none';
                    const phoneInput = document.getElementById('driverPhone');
                    const phoneLabel = document.querySelector('label[for="driverPhone"]');
                    if (phoneLabel) {
                        phoneLabel.innerHTML = 'Phone Number: <span style="color: #f48771;">*</span>';
                    }
                    if (phoneInput) {
                        phoneInput.required = leadGenEnabled;
                        phoneInput.placeholder = 'Enter phone number (required)';
                    }
                    const phoneHelp = document.getElementById('driverPhoneHelp');
                    if (phoneHelp) {
                        phoneHelp.textContent = 'Required for race result delivery';
                    }
                    if (!leadGenEnabled) {
                        if (emailInput) emailInput.value = '';
                        if (phoneInput) phoneInput.value = '';
                    }
                    // Survey questions: only re-render when the config changes so
                    // polling never wipes a half-answered wizard
                    const sq = Array.isArray(data.survey_questions) ? data.survey_questions : [];
                    const sqJson = JSON.stringify(sq);
                    const nowActive = leadGenEnabled && sq.length > 0;
                    if (sqJson !== lastSurveyJson) {
                        lastSurveyJson = sqJson;
                        surveyQuestions = sq;
                        surveyActive = nowActive;
                        renderSurvey();
                    } else if (nowActive !== surveyActive) {
                        surveyActive = nowActive;
                        currentStep = 0;
                        updateWizard();
                    }
                    // Background
                    if (data.background_image) {
                        document.body.style.backgroundImage = 'url("/backgrounds/' + data.background_image + '")';
                    } else {
                        document.body.style.backgroundImage = 'none';
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
                .catch(error => {
                    consecutiveErrors++;
                    // Exponential backoff: 1s, 2s, 4s, 8s, max 10s
                    pollInterval = Math.min(1000 * Math.pow(2, consecutiveErrors), maxPollInterval);
                    console.log('Poll error, backing off to ' + pollInterval + 'ms');
                    if (consecutiveErrors >= 3) {
                        updateConnectionStatus(false);
                    }
                })
                .finally(() => {
                    setTimeout(checkTimerAndSettings, pollInterval);
                });
        }

        // Initialize on page load
        async function initializePage() {
            // Wait for server to be ready (handles cold start)
            const connected = await waitForServer();

            if (connected) {
                // Load current driver
                try {
                    const response = await fetch('/get_driver');
                    const data = await response.json();
                    if (data.driver_name) {
                        document.getElementById('driverName').value = data.driver_name;
                        document.getElementById('currentDriverName').textContent = data.driver_name;
                        document.getElementById('currentDriverBox').style.display = 'flex';
                    }
                } catch (error) {
                    console.error('Error loading driver:', error);
                }
            }

            // Start polling (uses setTimeout with exponential backoff internally)
            checkTimerAndSettings();
        }

        // Start initialization
        initializePage();
    </script>
</body>
</html>
"""

# Full admin HTML page - Restaurant-style host stand UI
ADMIN_HTML_CONTENT = r"""
<!DOCTYPE html>
<html>
<head>
    <title>Sim Racing - Host Stand</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body {
            width: 100%;
            min-height: 100vh;
            overflow-x: hidden;
            background-color: #1e1e1e;
        }
        body {
            font-family: 'Segoe UI', Arial, sans-serif;
            display: flex;
            flex-direction: column;
            padding: 15px;
            padding-bottom: 100px;
        }

        /* Stats Bar */
        .stats-bar {
            display: flex;
            justify-content: center;
            gap: 15px;
            padding: 12px 15px;
            background: #252526;
            border-radius: 8px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .stat-item {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 16px;
            background: #2d2d30;
            border-radius: 6px;
            color: #cccccc;
            font-size: 14px;
        }
        .stat-item .stat-value {
            font-weight: bold;
            font-size: 18px;
            color: #007acc;
        }
        .stat-item.warning .stat-value { color: #ce9178; }
        .stat-item.success .stat-value { color: #4ec9b0; }

        /* Simulator Cards Grid */
        .sims-section {
            margin-bottom: 20px;
        }
        .sims-section h2 {
            color: #9d9d9d;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .sims-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 12px;
        }
        .sim-card {
            background: #252526;
            border: 2px solid #3e3e42;
            border-radius: 12px;
            padding: 16px;
            display: flex;
            flex-direction: column;
            min-height: 140px;
            transition: all 0.2s ease;
            cursor: pointer;
        }
        .sim-card:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.3); }

        /* Sim card states */
        .sim-card.available { border-color: #4ec9b0; }
        .sim-card.occupied { border-color: #007acc; }
        .sim-card.expiring { border-color: #ce9178; animation: pulse-border 1.5s infinite; }
        .sim-card.expired { border-color: #f48771; animation: pulse-border-red 1s infinite; }
        .sim-card.offline { border-color: #f48771; opacity: 0.6; }

        @keyframes pulse-border {
            0%, 100% { box-shadow: 0 0 0 0 rgba(206, 145, 120, 0.4); }
            50% { box-shadow: 0 0 0 4px rgba(206, 145, 120, 0.2); }
        }
        @keyframes pulse-border-red {
            0%, 100% { box-shadow: 0 0 0 0 rgba(244, 135, 113, 0.5); }
            50% { box-shadow: 0 0 0 6px rgba(244, 135, 113, 0.3); }
        }

        .sim-card-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 8px;
        }
        .sim-card-name {
            font-weight: 600;
            font-size: 16px;
            color: #ffffff;
        }
        .sim-card-status {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #4ec9b0;
        }
        .sim-card-status.offline { background: #f48771; }

        .sim-card-driver {
            font-size: 18px;
            font-weight: bold;
            color: #cccccc;
            margin-bottom: 4px;
            flex: 1;
        }
        .sim-card-driver.available-text {
            color: #4ec9b0;
            font-size: 16px;
        }

        .sim-card-timer {
            font-size: 24px;
            font-weight: bold;
            color: #007acc;
            margin-bottom: 8px;
            font-family: 'Consolas', monospace;
        }
        .sim-card.expiring .sim-card-timer { color: #ce9178; }
        .sim-card.expired .sim-card-timer { color: #f48771; }

        .sim-card-laps {
            font-size: 14px;
            color: #9d9d9d;
            margin-bottom: 8px;
        }

        .sim-card-action {
            margin-top: auto;
            padding: 12px;
            border: none;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            min-height: 44px;
        }
        .sim-card-action.seat-next {
            background: #4ec9b0;
            color: #1e1e1e;
        }
        .sim-card-action.seat-next:hover { background: #5fd9c0; }
        .sim-card-action.seat-next:disabled {
            background: #3e3e42;
            color: #6e6e6e;
            cursor: not-allowed;
        }
        .sim-card-action.end-session {
            background: #f48771;
            color: #1e1e1e;
        }
        .sim-card-action.end-session:hover { background: #ff9785; }

        /* Queue Section */
        .queue-section {
            background: #252526;
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 20px;
        }
        .queue-section h2 {
            color: #cccccc;
            font-size: 16px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .queue-list {
            max-height: 300px;
            overflow-y: auto;
        }
        .queue-item {
            display: flex;
            align-items: center;
            padding: 12px;
            background: #2d2d30;
            border-radius: 8px;
            margin-bottom: 8px;
            gap: 12px;
        }
        .queue-item .position {
            font-size: 20px;
            font-weight: bold;
            color: #007acc;
            min-width: 36px;
            text-align: center;
        }
        .queue-item .guest-info {
            flex: 1;
        }
        .queue-item .guest-name {
            font-weight: 600;
            font-size: 15px;
            color: #ffffff;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .queue-item .notified-badge {
            font-size: 14px;
            color: #4ec9b0;
            title: 'SMS sent';
        }
        .queue-item .guest-wait {
            font-size: 12px;
            color: #9d9d9d;
            margin-top: 2px;
        }
        .queue-item .remove-btn {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            border: 1px solid #f48771;
            background: transparent;
            color: #f48771;
            font-size: 16px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .queue-item .remove-btn:hover {
            background: rgba(244, 135, 113, 0.15);
        }
        .queue-empty {
            text-align: center;
            color: #6e6e6e;
            padding: 40px 20px;
            font-style: italic;
        }

        /* Bottom Action Bar */
        .action-bar {
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            background: #252526;
            border-top: 1px solid #3e3e42;
            padding: 12px 15px;
            display: flex;
            gap: 10px;
            z-index: 100;
        }
        .action-bar button {
            flex: 1;
            padding: 14px;
            border: none;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            min-height: 48px;
        }
        .action-bar .register-btn {
            background: #007acc;
            color: white;
        }
        .action-bar .register-btn:hover { background: #0098ff; }
        .action-bar .manual-btn {
            background: #3e3e42;
            color: #cccccc;
        }
        .action-bar .manual-btn:hover { background: #454545; }
        .action-bar .settings-btn {
            background: #3e3e42;
            color: #cccccc;
            flex: 0 0 48px;
        }

        /* Modal Styles */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.85);
            z-index: 2000;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .modal-overlay.active { display: flex; }
        .modal {
            background: #252526;
            border-radius: 12px;
            border: 1px solid #3e3e42;
            width: 100%;
            max-width: 400px;
            max-height: 90vh;
            overflow-y: auto;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 16px 20px;
            border-bottom: 1px solid #3e3e42;
        }
        .modal-header h3 {
            color: #ffffff;
            font-size: 18px;
            margin: 0;
        }
        .modal-close {
            background: none;
            border: none;
            color: #9d9d9d;
            font-size: 24px;
            cursor: pointer;
            padding: 0;
            width: 32px;
            height: 32px;
        }
        .modal-close:hover { color: #ffffff; }
        .modal-body {
            padding: 20px;
        }
        .input-group {
            margin-bottom: 16px;
        }
        .input-group label {
            display: block;
            margin-bottom: 6px;
            color: #9d9d9d;
            font-size: 14px;
            font-weight: 500;
        }
        .input-group input {
            width: 100%;
            padding: 12px;
            font-size: 16px;
            border: 1px solid #3e3e42;
            border-radius: 8px;
            background: #2d2d30;
            color: #ffffff;
        }
        .input-group input:focus {
            border-color: #007acc;
            outline: none;
        }
        .input-group small {
            display: block;
            margin-top: 4px;
            color: #6e6e6e;
            font-size: 12px;
        }
        .modal-body button.primary {
            width: 100%;
            padding: 14px;
            background: #007acc;
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            margin-top: 10px;
        }
        .modal-body button.primary:hover { background: #0098ff; }

        .status {
            margin-top: 16px;
            padding: 12px;
            border-radius: 8px;
            text-align: center;
            display: none;
        }
        .status.success {
            display: block;
            background: rgba(78, 201, 176, 0.2);
            color: #4ec9b0;
            border: 1px solid #4ec9b0;
        }
        .status.error {
            display: block;
            background: rgba(244, 135, 113, 0.2);
            color: #f48771;
            border: 1px solid #f48771;
        }

        /* Settings Modal specific */
        .sim-config-list {
            margin-bottom: 16px;
        }
        .sim-config-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 12px;
            background: #2d2d30;
            border-radius: 6px;
            margin-bottom: 8px;
        }
        .sim-config-item .info {
            color: #cccccc;
        }
        .sim-config-item .info small {
            display: block;
            color: #6e6e6e;
            font-size: 11px;
        }
        .sim-config-item .remove-btn {
            background: #f48771;
            color: #1e1e1e;
            border: none;
            border-radius: 4px;
            padding: 4px 10px;
            font-size: 12px;
            cursor: pointer;
        }
        .add-sim-form input {
            width: 100%;
            padding: 10px;
            margin-bottom: 8px;
            border: 1px solid #3e3e42;
            border-radius: 6px;
            background: #2d2d30;
            color: #cccccc;
            font-size: 14px;
        }

        /* Responsive */
        @media (max-width: 400px) {
            .stats-bar { flex-direction: column; align-items: stretch; }
            .stat-item { justify-content: space-between; }
            .sims-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <!-- Stats Bar -->
    <div class="stats-bar">
        <div class="stat-item">
            <span id="waitingCount" class="stat-value">0</span>
            <span>waiting</span>
        </div>
        <div class="stat-item success">
            <span id="availableCount" class="stat-value">0</span>
            <span>available</span>
        </div>
        <div class="stat-item">
            <span>Avg:</span>
            <span id="avgWaitTime" class="stat-value">--</span>
        </div>
    </div>

    <!-- Simulators Grid -->
    <div class="sims-section">
        <h2>Simulators <button onclick="openSettingsModal()" style="background: none; border: none; color: #6e6e6e; cursor: pointer; font-size: 16px;">+</button></h2>
        <div class="sims-grid" id="simsGrid">
            <!-- Simulator cards populated dynamically -->
        </div>
    </div>

    <!-- Queue Section -->
    <div class="queue-section">
        <h2>Queue</h2>
        <div class="queue-list" id="queueList">
            <div class="queue-empty">Queue is empty</div>
        </div>
    </div>

    <!-- Bottom Action Bar -->
    <div class="action-bar">
        <button class="register-btn" onclick="openRegisterModal()">+ Register Driver</button>
        <button class="manual-btn" onclick="openManualLapModal()">Manual Lap</button>
        <button class="settings-btn" onclick="openSettingsModal()">+</button>
    </div>

    <!-- Register Driver Modal -->
    <div class="modal-overlay" id="registerModal">
        <div class="modal">
            <div class="modal-header">
                <h3>Register Driver</h3>
                <button class="modal-close" onclick="closeRegisterModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="input-group">
                    <label for="driverName">Driver Name</label>
                    <input type="text" id="driverName" placeholder="Enter driver name">
                    <small>This will appear on the leaderboard</small>
                </div>
                <div class="input-group" id="emailGroup" style="display: none;">
                    <label for="driverEmail">Email Address <span style="color: #f48771;">*</span></label>
                    <input type="email" id="driverEmail" placeholder="Enter email address (required)">
                </div>
                <div class="input-group" id="phoneGroup" style="display: none;">
                    <label for="driverPhone">Phone Number</label>
                    <input type="text" id="driverPhone" placeholder="Enter phone number (required)">
                    <small id="driverPhoneHelp">Required for race result delivery</small>
                </div>
                <button class="primary" onclick="updateDriver()">Register</button>
                <div id="registerStatus" class="status"></div>
            </div>
        </div>
    </div>

    <!-- Manual Lap Entry Modal -->
    <div class="modal-overlay" id="manualLapModal">
        <div class="modal">
            <div class="modal-header">
                <h3>Manual Lap Time</h3>
                <button class="modal-close" onclick="closeManualLapModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="input-group">
                    <label for="manualDriverName">Driver Name</label>
                    <input type="text" id="manualDriverName" placeholder="Enter driver name">
                </div>
                <div class="input-group">
                    <label for="manualLapTime">Lap Time</label>
                    <input type="text" id="manualLapTime" placeholder="e.g., 1:23.456 or 83.456">
                    <small>Format: mm:ss.xxx or total seconds</small>
                </div>
                <button class="primary" onclick="submitManualLapTime()">Submit Lap</button>
                <div id="manualStatus" class="status"></div>
            </div>
        </div>
    </div>

    <!-- Settings Modal -->
    <div class="modal-overlay" id="settingsModal">
        <div class="modal">
            <div class="modal-header">
                <h3>Simulator Settings</h3>
                <button class="modal-close" onclick="closeSettingsModal()">&times;</button>
            </div>
            <div class="modal-body">
                <p style="color: #9d9d9d; font-size: 13px; margin-bottom: 12px;" id="discoveryStatus">Searching for simulators...</p>
                <div class="sim-config-list" id="manualSimList">
                    <!-- Manual entries appear here -->
                </div>
                <div class="add-sim-form">
                    <input type="text" id="manualSimIp" placeholder="IP Address (e.g., 192.168.1.10)">
                    <input type="text" id="manualSimName" placeholder="Name (e.g., Sim 3)">
                    <button class="primary" onclick="addManualSimulator()">Add Simulator</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Edit Simulator Modal -->
    <div class="modal-overlay" id="editSimModal">
        <div class="modal">
            <div class="modal-header">
                <h3>Edit Simulator</h3>
                <button class="modal-close" onclick="closeEditModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="input-group">
                    <label>IP Address</label>
                    <input type="text" id="editSimIp" placeholder="IP Address">
                </div>
                <div class="input-group">
                    <label>Name</label>
                    <input type="text" id="editSimName" placeholder="Simulator Name">
                </div>
                <button class="primary" onclick="saveSimulatorEdit()">Save</button>
            </div>
        </div>
    </div>

    <script>
        // ============================================
        // HOST STAND ADMIN - Simulator Cards UI
        // ============================================

        const STORAGE_KEY = 'lapTimeSender_manualSimulators';
        let discoveredSimulators = [];
        let currentSimulatorIp = null;
        let queueData = { queue: [], active_sessions: {} };
        let editingSimulator = null;

        // ============================================
        // SIMULATOR CARDS
        // ============================================

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

        async function refreshSimulatorInfo(ip, port = 5001) {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 1500);
            try {
                const response = await fetch(`http://${ip}:${port}/api/simulator_info`, {
                    mode: 'cors', signal: controller.signal
                });
                clearTimeout(timeoutId);
                if (response.ok) {
                    const data = await response.json();
                    data.ip = ip;
                    data.port = port;
                    data.reachable = true;
                    return data;
                }
            } catch (e) { clearTimeout(timeoutId); }
            return null;
        }

        async function refreshSimulators() {
            if (discoveredSimulators.length === 0) return;

            const refreshPromises = discoveredSimulators.map(async (sim) => {
                if (sim.is_current) {
                    try {
                        const response = await fetch('/api/simulator_info');
                        if (response.ok) {
                            const data = await response.json();
                            return { ...sim, ...data, is_current: true, reachable: true };
                        }
                    } catch (e) {}
                    return sim;
                } else {
                    const freshData = await refreshSimulatorInfo(sim.ip, sim.port || 5001);
                    if (freshData) {
                        return { ...sim, ...freshData, reachable: true };
                    }
                    return { ...sim, reachable: false };
                }
            });

            discoveredSimulators = await Promise.all(refreshPromises);
            renderSimulatorCards();
            updateStats();
        }

        function renderSimulatorCards() {
            const grid = document.getElementById('simsGrid');
            if (!grid) return;

            const manualSims = getManualSimulators();
            const allSimulators = [...discoveredSimulators];

            manualSims.forEach(manual => {
                if (!allSimulators.some(s => s.ip === manual.ip)) {
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

            allSimulators.sort((a, b) => (a.simulator_id || 0) - (b.simulator_id || 0));

            if (allSimulators.length === 0) {
                grid.innerHTML = '<div style="color: #6e6e6e; text-align: center; padding: 20px;">No simulators found</div>';
                return;
            }

            grid.innerHTML = allSimulators.map(sim => {
                const hasDriver = sim.current_driver && sim.current_driver.trim();
                const isReachable = sim.reachable !== false;

                // Determine card state
                let cardClass = 'available';
                if (!isReachable) cardClass = 'offline';
                else if (hasDriver) {
                    cardClass = 'occupied';
                    // Check for expiring/expired
                    if (sim.timer_active && sim.timer_seconds_left !== undefined) {
                        if (sim.timer_seconds_left === 0) cardClass = 'expired';
                        else if (sim.timer_seconds_left <= 120) cardClass = 'expiring';
                    }
                    if (sim.laps_active && sim.laps_progress) {
                        if (sim.laps_progress.count >= sim.laps_progress.target && sim.laps_progress.target > 0) {
                            cardClass = 'expired';
                        }
                    }
                }

                // Format timer display
                let timerDisplay = '';
                if (hasDriver && sim.timer_active && sim.timer_seconds_left !== undefined) {
                    const mins = Math.floor(sim.timer_seconds_left / 60);
                    const secs = sim.timer_seconds_left % 60;
                    timerDisplay = `${mins}:${secs.toString().padStart(2, '0')}`;
                }

                // Laps display
                let lapsDisplay = '';
                if (hasDriver && sim.laps_active && sim.laps_progress) {
                    lapsDisplay = `${sim.laps_progress.count}/${sim.laps_progress.target} laps`;
                }

                // Check if queue has someone waiting
                const hasQueueWaiting = queueData.queue && queueData.queue.length > 0 && !queueData.queue[0].assigned_to;

                return `
                    <div class="sim-card ${cardClass}" onclick="onSimCardClick('${sim.ip}', ${sim.port || 5001}, ${hasDriver}, ${isReachable})">
                        <div class="sim-card-header">
                            <span class="sim-card-name">${escapeHtml(sim.simulator_name)}</span>
                            <span class="sim-card-status ${isReachable ? '' : 'offline'}"></span>
                        </div>
                        <div class="sim-card-driver ${hasDriver ? '' : 'available-text'}">
                            ${hasDriver ? escapeHtml(sim.current_driver) : 'AVAILABLE'}
                        </div>
                        ${timerDisplay ? `<div class="sim-card-timer">${timerDisplay}</div>` : ''}
                        ${lapsDisplay ? `<div class="sim-card-laps">${lapsDisplay}</div>` : ''}
                        ${isReachable ? (
                            hasDriver
                                ? `<button class="sim-card-action end-session" onclick="event.stopPropagation(); endSessionOnSim('${sim.ip}')">END</button>`
                                : `<button class="sim-card-action seat-next" onclick="event.stopPropagation(); seatNextGuest('${sim.ip}')" ${hasQueueWaiting ? '' : 'disabled'}>SEAT NEXT</button>`
                        ) : '<button class="sim-card-action" disabled style="background:#3e3e42;color:#6e6e6e;">OFFLINE</button>'}
                    </div>
                `;
            }).join('');
        }

        function onSimCardClick(ip, port, hasDriver, isReachable) {
            if (!isReachable) {
                alert(`Cannot connect to simulator at ${ip}`);
                return;
            }
            // Long press could open edit menu - for now just navigate
            if (ip !== currentSimulatorIp) {
                window.location.href = `http://${ip}:${port}/admin`;
            }
        }

        async function seatNextGuest(simIp) {
            const queue = queueData.queue || [];
            const nextGuest = queue.find(g => !g.assigned_to);
            if (!nextGuest) {
                alert('No guests waiting in queue');
                return;
            }

            const sim = discoveredSimulators.find(s => s.ip === simIp);
            const simName = sim ? sim.simulator_name : `Sim at ${simIp}`;
            const simPort = sim ? (sim.port || 5001) : 5001;

            try {
                // Step 1: Assign guest to simulator
                const assignResponse = await fetch('/api/queue/assign', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        queue_id: nextGuest.id,
                        simulator_ip: simIp,
                        simulator_name: simName
                    })
                });
                const assignData = await assignResponse.json();

                if (!assignData.success) {
                    alert('Failed to assign guest: ' + (assignData.error || 'Unknown error'));
                    return;
                }

                // Step 2: Start session on that simulator
                const startResponse = await fetch(`http://${simIp}:${simPort}/api/session/start_remote`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        driver_name: nextGuest.name,
                        queue_id: nextGuest.id,
                        driver_email: nextGuest.email || assignData.email || '',
                        driver_phone: nextGuest.phone || assignData.phone || ''
                    })
                });
                const startData = await startResponse.json();

                if (startData.success) {
                    // Remove from queue after successful start
                    await fetch('/api/queue/remove', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: nextGuest.id })
                    });
                    refreshQueue();
                    refreshSimulators();
                } else {
                    alert('Session start failed: ' + (startData.error || 'Unknown error'));
                }
            } catch (e) {
                console.error('Error seating guest:', e);
                alert('Failed to connect to simulator');
            }
        }

        async function endSessionOnSim(simIp) {
            try {
                const sim = discoveredSimulators.find(s => s.ip === simIp);
                const simPort = sim ? (sim.port || 5001) : 5001;
                const response = await fetch(`http://${simIp}:${simPort}/api/session/force_end`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' }
                });
                const data = await response.json();
                if (data.success) {
                    refreshSimulators();
                    refreshQueue();
                }
            } catch (e) {
                console.error('Error ending session:', e);
                alert('Failed to end session');
            }
        }

        // ============================================
        // QUEUE MANAGEMENT
        // ============================================

        async function refreshQueue() {
            try {
                const response = await fetch('/api/queue');
                const data = await response.json();
                if (data.success) {
                    queueData = data;
                    renderQueue(data.queue);
                    updateStats();
                }
            } catch (e) {
                console.error('Error fetching queue:', e);
            }
        }

        function renderQueue(queue) {
            const list = document.getElementById('queueList');
            if (!queue || queue.length === 0) {
                list.innerHTML = '<div class="queue-empty">Queue is empty - guests can join at /queue</div>';
                return;
            }

            list.innerHTML = queue.map(entry => {
                const notifiedBadge = entry.notified ? '<span class="notified-badge" title="SMS sent">&#128276;</span>' : '';
                return `
                    <div class="queue-item" data-id="${entry.id}">
                        <div class="position">#${entry.position}</div>
                        <div class="guest-info">
                            <div class="guest-name">${escapeHtml(entry.name)} ${notifiedBadge}</div>
                            <div class="guest-wait">${entry.wait_display || 'Just joined'}</div>
                        </div>
                        <button class="remove-btn" onclick="removeFromQueue('${entry.id}')">&times;</button>
                    </div>
                `;
            }).join('');
        }

        async function removeFromQueue(queueId) {
            try {
                await fetch('/api/queue/remove', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: queueId })
                });
                refreshQueue();
            } catch (e) {
                console.error('Error removing from queue:', e);
            }
        }

        function updateStats() {
            // Update waiting count
            const waitingCount = queueData.total_in_queue || 0;
            document.getElementById('waitingCount').textContent = waitingCount;

            // Update available count
            const availableCount = discoveredSimulators.filter(s =>
                s.reachable !== false && (!s.current_driver || !s.current_driver.trim())
            ).length;
            document.getElementById('availableCount').textContent = availableCount;

            // Update average wait time
            fetch('/api/queue/stats')
                .then(r => r.json())
                .then(stats => {
                    if (stats.avg_session_time_display) {
                        document.getElementById('avgWaitTime').textContent = stats.avg_session_time_display;
                    }
                })
                .catch(() => {});
        }

        // ============================================
        // MODAL FUNCTIONS
        // ============================================

        function openRegisterModal() {
            document.getElementById('registerModal').classList.add('active');
            document.getElementById('driverName').focus();
            loadSettings(); // Check if email is required
        }

        function closeRegisterModal() {
            document.getElementById('registerModal').classList.remove('active');
        }

        function openManualLapModal() {
            document.getElementById('manualLapModal').classList.add('active');
            document.getElementById('manualDriverName').focus();
        }

        function closeManualLapModal() {
            document.getElementById('manualLapModal').classList.remove('active');
        }

        function openSettingsModal() {
            document.getElementById('settingsModal').classList.add('active');
            renderManualSimulatorList();
        }

        function closeSettingsModal() {
            document.getElementById('settingsModal').classList.remove('active');
        }

        function openEditModal(sim) {
            editingSimulator = sim;
            document.getElementById('editSimIp').value = sim.ip || '';
            document.getElementById('editSimName').value = sim.simulator_name || sim.name || '';
            document.getElementById('editSimModal').classList.add('active');
        }

        function closeEditModal() {
            document.getElementById('editSimModal').classList.remove('active');
            editingSimulator = null;
        }

        // ============================================
        // DRIVER REGISTRATION
        // ============================================

        function updateDriver() {
            const driverName = document.getElementById('driverName').value.trim();
            const driverEmailInput = document.getElementById('driverEmail');
            const driverEmail = driverEmailInput?.value?.trim() || '';
            const driverPhone = document.getElementById('driverPhone')?.value?.trim() || '';
            const statusEl = document.getElementById('registerStatus');

            if (!driverName) {
                statusEl.textContent = 'Please enter a driver name';
                statusEl.className = 'status error';
                return;
            }

            // Check if email required
            const emailGroup = document.getElementById('emailGroup');
            const leadGenEnabled = emailGroup.style.display !== 'none';
            let payload = {
                driver_name: driverName,
                driver_email: leadGenEnabled ? driverEmail : '',
                driver_phone: leadGenEnabled ? driverPhone : ''
            };
            if (leadGenEnabled) {
                if (!driverEmail) {
                    statusEl.textContent = 'Email address is required';
                    statusEl.className = 'status error';
                    return;
                }
                if (driverEmailInput && !driverEmailInput.checkValidity()) {
                    statusEl.textContent = 'Please enter a valid email address';
                    statusEl.className = 'status error';
                    return;
                }
                if (!driverPhone) {
                    statusEl.textContent = 'Phone number is required';
                    statusEl.className = 'status error';
                    return;
                }
            }

            fetch('/set_driver', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    statusEl.textContent = 'Driver registered successfully!';
                    statusEl.className = 'status success';
                    document.getElementById('driverName').value = '';
                    document.getElementById('driverPhone').value = '';
                    document.getElementById('driverEmail').value = '';
                    setTimeout(() => {
                        closeRegisterModal();
                        statusEl.className = 'status';
                        refreshSimulators();
                    }, 1500);
                } else {
                    statusEl.textContent = 'Failed to register driver';
                    statusEl.className = 'status error';
                }
            })
            .catch(e => {
                statusEl.textContent = 'Error connecting to server';
                statusEl.className = 'status error';
            });
        }

        // ============================================
        // MANUAL LAP ENTRY
        // ============================================

        function parseLapTime(input) {
            input = input.trim();
            if (input.includes(':')) {
                const parts = input.split(':');
                if (parts.length !== 2) return null;
                const minutes = parseInt(parts[0]);
                const seconds = parseFloat(parts[1]);
                if (isNaN(minutes) || isNaN(seconds)) return null;
                return minutes * 60 + seconds;
            } else {
                const seconds = parseFloat(input);
                return isNaN(seconds) ? null : seconds;
            }
        }

        function submitManualLapTime() {
            const driverName = document.getElementById('manualDriverName').value.trim();
            const lapTimeInput = document.getElementById('manualLapTime').value.trim();
            const statusEl = document.getElementById('manualStatus');

            if (!driverName) {
                statusEl.textContent = 'Please enter a driver name';
                statusEl.className = 'status error';
                return;
            }

            if (!lapTimeInput) {
                statusEl.textContent = 'Please enter a lap time';
                statusEl.className = 'status error';
                return;
            }

            const lapTimeSeconds = parseLapTime(lapTimeInput);
            if (lapTimeSeconds === null || lapTimeSeconds < 5 || lapTimeSeconds > 3600) {
                statusEl.textContent = 'Invalid lap time format';
                statusEl.className = 'status error';
                return;
            }

            fetch('/api/manual_lap', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ driver_name: driverName, lap_time: lapTimeSeconds })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    statusEl.textContent = `Lap time ${data.formatted_time} sent!`;
                    statusEl.className = 'status success';
                    document.getElementById('manualLapTime').value = '';
                    setTimeout(() => {
                        closeManualLapModal();
                        statusEl.className = 'status';
                    }, 1500);
                } else {
                    statusEl.textContent = data.message || 'Failed to send lap time';
                    statusEl.className = 'status error';
                }
            })
            .catch(e => {
                statusEl.textContent = 'Error connecting to server';
                statusEl.className = 'status error';
            });
        }

        // ============================================
        // SIMULATOR SETTINGS
        // ============================================

        function getManualSimulators() {
            try {
                const stored = localStorage.getItem(STORAGE_KEY);
                return stored ? JSON.parse(stored) : [];
            } catch (e) { return []; }
        }

        function saveManualSimulators(simulators) {
            try {
                localStorage.setItem(STORAGE_KEY, JSON.stringify(simulators));
            } catch (e) {}
        }

        function renderManualSimulatorList() {
            const list = document.getElementById('manualSimList');
            const manualSims = getManualSimulators();

            if (manualSims.length === 0) {
                list.innerHTML = '<p style="color: #6e6e6e; text-align: center; font-size: 13px;">No manual simulators added</p>';
                return;
            }

            list.innerHTML = manualSims.map((sim, idx) => `
                <div class="sim-config-item">
                    <div class="info">
                        <strong>${escapeHtml(sim.name)}</strong>
                        <small>${sim.ip}:${sim.port || 5001}</small>
                    </div>
                    <button class="remove-btn" onclick="removeManualSimulator(${idx})">Remove</button>
                </div>
            `).join('');
        }

        async function addManualSimulator() {
            const ip = document.getElementById('manualSimIp').value.trim();
            const name = document.getElementById('manualSimName').value.trim() || `Sim (${ip})`;

            if (!ip || !/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(ip)) {
                alert('Please enter a valid IP address');
                return;
            }

            const manualSims = getManualSimulators();
            if (manualSims.some(s => s.ip === ip)) {
                alert('This simulator is already added');
                return;
            }

            manualSims.push({ ip, name, port: 5001 });
            saveManualSimulators(manualSims);

            try {
                await fetch('/api/known_simulators', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ip, name })
                });
            } catch (e) {}

            document.getElementById('manualSimIp').value = '';
            document.getElementById('manualSimName').value = '';
            renderManualSimulatorList();
            initializeSimulators();
        }

        async function removeManualSimulator(index) {
            const manualSims = getManualSimulators();
            const removed = manualSims[index];
            manualSims.splice(index, 1);
            saveManualSimulators(manualSims);

            if (removed && removed.ip) {
                try {
                    await fetch(`/api/known_simulators/${removed.ip}`, { method: 'DELETE' });
                } catch (e) {}
            }

            renderManualSimulatorList();
            initializeSimulators();
        }

        async function saveSimulatorEdit() {
            const oldIp = editingSimulator?.ip;
            const newIp = document.getElementById('editSimIp').value.trim();
            const newName = document.getElementById('editSimName').value.trim();

            if (!newIp || !/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(newIp)) {
                alert('Please enter a valid IP address');
                return;
            }

            const manualSims = getManualSimulators();
            const existingIndex = manualSims.findIndex(s => s.ip === oldIp);

            if (existingIndex >= 0) {
                manualSims[existingIndex] = { ip: newIp, name: newName || `Sim (${newIp})`, port: 5001 };
            } else {
                manualSims.push({ ip: newIp, name: newName || `Sim (${newIp})`, port: 5001 });
            }

            if (oldIp && oldIp !== newIp) {
                try { await fetch(`/api/known_simulators/${oldIp}`, { method: 'DELETE' }); } catch (e) {}
            }

            try {
                await fetch('/api/known_simulators', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ip: newIp, name: newName })
                });
            } catch (e) {}

            saveManualSimulators(manualSims);
            closeEditModal();
            initializeSimulators();
        }

        // ============================================
        // UTILITIES
        // ============================================

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text || '';
            return div.innerHTML;
        }

        function loadSettings() {
            fetch('/get_settings')
                .then(r => r.json())
                .then(data => {
                    const emailGroup = document.getElementById('emailGroup');
                    const leadGenEnabled = Boolean(data.lead_gen_enabled || data.require_email);
                    if (emailGroup) {
                        emailGroup.style.display = leadGenEnabled ? 'block' : 'none';
                    }
                    const phoneGroup = document.getElementById('phoneGroup');
                    if (phoneGroup) {
                        phoneGroup.style.display = leadGenEnabled ? 'block' : 'none';
                    }
                    const driverEmailInput = document.getElementById('driverEmail');
                    const phoneInput = document.getElementById('driverPhone');
                    const phoneLabel = document.querySelector('label[for="driverPhone"]');
                    if (phoneLabel) {
                        phoneLabel.innerHTML = 'Phone Number <span style="color: #f48771;">*</span>';
                    }
                    if (phoneInput) {
                        phoneInput.required = leadGenEnabled;
                        phoneInput.placeholder = 'Enter phone number (required)';
                    }
                    const phoneHelp = document.getElementById('driverPhoneHelp');
                    if (phoneHelp) {
                        phoneHelp.textContent = 'Required for race result delivery';
                    }
                    if (!leadGenEnabled) {
                        if (driverEmailInput) driverEmailInput.value = '';
                        if (phoneInput) phoneInput.value = '';
                    }
                })
                .catch(() => {});
        }

        // ============================================
        // INITIALIZATION
        // ============================================

        async function initializeSimulators() {
            const statusEl = document.getElementById('discoveryStatus');
            if (statusEl) statusEl.textContent = 'Searching for simulators...';

            const simulators = await discoverSimulators();

            if (statusEl) {
                const total = simulators.length;
                const online = simulators.filter(s => s.reachable !== false).length;
                if (total <= 1) {
                    statusEl.textContent = 'Only this simulator found. Add others below.';
                } else {
                    statusEl.textContent = `Found ${total} simulators (${online} online)`;
                }
            }

            renderSimulatorCards();
            updateStats();
        }

        // Start polling
        document.addEventListener('DOMContentLoaded', () => {
            initializeSimulators();
            refreshQueue();

            // Refresh simulators every 2 seconds
            setInterval(refreshSimulators, 2000);

            // Refresh queue every 5 seconds
            setInterval(refreshQueue, 5000);

            // Full discovery every 30 seconds
            setInterval(initializeSimulators, 30000);
        });

        // Close modals on overlay click
        document.querySelectorAll('.modal-overlay').forEach(overlay => {
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    overlay.classList.remove('active');
                }
            });
        });
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
            background-color: #000000;
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
        input[type="text"], input[type="email"], input[type="tel"] {
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
            <div class="input-group" id="guestEmailGroup" style="display: none;">
                <label for="guestEmail">Email <span style="color: #f48771;">*</span></label>
                <input type="email" id="guestEmail" placeholder="Enter email address" autocomplete="off">
            </div>
            <div class="input-group" id="guestPhoneGroup" style="display: none;">
                <label for="guestPhone">Phone <span id="guestPhoneRequired" style="color: #f48771;">*</span></label>
                <input type="tel" id="guestPhone" placeholder="Phone number required" autocomplete="off">
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
        // Update queue count display (uses local proxy endpoint)
        async function updateQueueCount() {
            try {
                const response = await fetch('/api/queue');
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
            const emailInput = document.getElementById('guestEmail');
            const phoneInput = document.getElementById('guestPhone');
            const name = nameInput.value.trim();
            const email = emailInput?.value?.trim() || '';
            const phone = phoneInput.value.trim();
            const errorMsg = document.getElementById('errorMsg');
            const joinBtn = document.getElementById('joinBtn');
            const leadGenEnabled = document.body.dataset.leadGen === 'true';

            if (!name) {
                errorMsg.textContent = 'Please enter your name';
                errorMsg.style.display = 'block';
                nameInput.focus();
                return;
            }
            if (leadGenEnabled && !email) {
                errorMsg.textContent = 'Please enter your email address';
                errorMsg.style.display = 'block';
                emailInput.focus();
                return;
            }
            if (leadGenEnabled && emailInput && !emailInput.checkValidity()) {
                errorMsg.textContent = 'Please enter a valid email address';
                errorMsg.style.display = 'block';
                emailInput.focus();
                return;
            }
            if (leadGenEnabled && !phone) {
                errorMsg.textContent = 'Please enter your phone number';
                errorMsg.style.display = 'block';
                phoneInput.focus();
                return;
            }

            joinBtn.disabled = true;
            joinBtn.textContent = 'Joining...';
            errorMsg.style.display = 'none';

            try {
                const response = await fetch('/api/queue/join', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: name,
                        email: leadGenEnabled ? email : '',
                        phone: leadGenEnabled ? phone : ''
                    })
                });
                const data = await response.json();

                if (data.success) {
                    // Show success overlay
                    showSuccess(name, data.position, data.wait_display);

                    // Clear form for next person
                    nameInput.value = '';
                    if (emailInput) emailInput.value = '';
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
                    const leadGenEnabled = Boolean(data.lead_gen_enabled || data.require_email);
                    document.body.dataset.leadGen = leadGenEnabled ? 'true' : 'false';
                    const emailGroup = document.getElementById('guestEmailGroup');
                    const emailInput = document.getElementById('guestEmail');
                    const phoneInput = document.getElementById('guestPhone');
                    const phoneGroup = document.getElementById('guestPhoneGroup');
                    if (emailGroup) emailGroup.style.display = leadGenEnabled ? 'block' : 'none';
                    if (phoneGroup) phoneGroup.style.display = leadGenEnabled ? 'block' : 'none';
                    if (emailInput) emailInput.required = leadGenEnabled;
                    if (phoneInput) {
                        phoneInput.required = leadGenEnabled;
                        phoneInput.placeholder = 'Phone number required';
                    }
                    if (!leadGenEnabled) {
                        if (emailInput) emailInput.value = '';
                        if (phoneInput) phoneInput.value = '';
                    }
                    if (data.background_image) {
                        document.body.style.backgroundImage = 'url("/backgrounds/' + data.background_image + '")';
                    } else {
                        document.body.style.backgroundImage = 'none';
                    }
                })
                .catch(e => console.error('Error loading settings:', e));
        }

        // Initialize
        loadSettings();
        updateQueueCount();

        // Update queue count every 10 seconds
        setInterval(updateQueueCount, 10000);

        // Focus on name input on load
        document.getElementById('guestName').focus();
    </script>
</body>
</html>
"""

# Queue display page for TV screens (no internet required)
QUEUE_DISPLAY_HTML_CONTENT = r"""
<!DOCTYPE html>
<html>
<head>
    <title>Queue Display</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="30">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body {
            width: 100%; height: 100vh; overflow: hidden;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            font-family: 'Segoe UI', Arial, sans-serif;
            color: #ffffff;
        }
        .container {
            display: flex;
            flex-direction: column;
            height: 100vh;
            padding: 30px;
        }

        /* Header with title */
        .header {
            text-align: center;
            padding: 20px;
            margin-bottom: 20px;
        }
        .header h1 {
            font-size: 48px;
            font-weight: 300;
            color: #4ec9b0;
            text-transform: uppercase;
            letter-spacing: 4px;
        }

        /* Now Serving Banner */
        .now-serving {
            background: linear-gradient(135deg, #007acc 0%, #0056b3 100%);
            border-radius: 16px;
            padding: 30px;
            margin-bottom: 30px;
            text-align: center;
            box-shadow: 0 8px 32px rgba(0, 122, 204, 0.4);
        }
        .now-serving-label {
            font-size: 24px;
            text-transform: uppercase;
            letter-spacing: 3px;
            opacity: 0.9;
            margin-bottom: 10px;
        }
        .now-serving-name {
            font-size: 64px;
            font-weight: bold;
            animation: pulse-glow 2s ease-in-out infinite;
        }
        @keyframes pulse-glow {
            0%, 100% { text-shadow: 0 0 20px rgba(255,255,255,0.5); }
            50% { text-shadow: 0 0 40px rgba(255,255,255,0.8); }
        }
        .now-serving.empty {
            background: linear-gradient(135deg, #3e3e42 0%, #2d2d30 100%);
            box-shadow: none;
        }
        .now-serving.empty .now-serving-name {
            font-size: 48px;
            opacity: 0.7;
            animation: none;
        }

        /* Queue List */
        .queue-section {
            flex: 1;
            display: flex;
            flex-direction: column;
            min-height: 0;
        }
        .queue-header {
            font-size: 28px;
            font-weight: 300;
            margin-bottom: 20px;
            color: #9d9d9d;
            text-transform: uppercase;
            letter-spacing: 2px;
        }
        .queue-list {
            flex: 1;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .queue-item {
            display: flex;
            align-items: center;
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 20px 30px;
            border-left: 4px solid #007acc;
        }
        .queue-item .position {
            font-size: 36px;
            font-weight: bold;
            color: #007acc;
            min-width: 80px;
        }
        .queue-item .name {
            flex: 1;
            font-size: 32px;
            font-weight: 500;
        }
        .queue-item .wait {
            font-size: 24px;
            color: #9d9d9d;
        }
        .queue-item .notified {
            margin-left: 20px;
            font-size: 28px;
            color: #4ec9b0;
        }

        .queue-empty {
            text-align: center;
            padding: 60px;
            font-size: 32px;
            color: #6e6e6e;
            font-style: italic;
        }

        /* Footer */
        .footer {
            text-align: center;
            padding: 20px;
            color: #6e6e6e;
            font-size: 18px;
        }

        /* Connection status indicator */
        .connection-status {
            position: fixed;
            top: 10px;
            right: 10px;
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 14px;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 8px;
            z-index: 1000;
        }
        .connection-status.connected {
            background: rgba(78, 201, 176, 0.2);
            color: #4ec9b0;
        }
        .connection-status.slow {
            background: rgba(255, 193, 7, 0.2);
            color: #ffc107;
        }
        .connection-status.disconnected {
            background: rgba(220, 53, 69, 0.2);
            color: #dc3545;
            animation: pulse-warning 1s ease-in-out infinite;
        }
        @keyframes pulse-warning {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: currentColor;
        }
    </style>
</head>
<body>
    <!-- Connection status indicator -->
    <div class="connection-status connected" id="connectionStatus">
        <span class="status-dot"></span>
        <span id="statusText">Connected</span>
    </div>

    <div class="container">
        <div class="header">
            <h1>Sim Racing Queue</h1>
        </div>

        <div class="now-serving empty" id="nowServing">
            <div class="now-serving-label">Now Serving</div>
            <div class="now-serving-name" id="nowServingName">Waiting for first guest...</div>
        </div>

        <div class="queue-section">
            <div class="queue-header">Up Next</div>
            <div class="queue-list" id="queueList">
                <div class="queue-empty">Queue is empty</div>
            </div>
        </div>

        <div class="footer">
            Scan the QR code at the kiosk to join the queue
        </div>
    </div>

    <script>
        let currentlyServing = null;
        let lastResponseTime = 0;
        let connectionLost = false;
        let slowRequestCount = 0;
        let eventSource = null;

        // Connection status thresholds
        const SLOW_THRESHOLD_MS = 3000;
        const TIMEOUT_MS = 10000;

        function updateConnectionStatus(status, message) {
            const statusEl = document.getElementById('connectionStatus');
            const textEl = document.getElementById('statusText');

            statusEl.className = 'connection-status ' + status;
            textEl.textContent = message;
        }

        async function timedFetch(url, options = {}) {
            const startTime = Date.now();
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), TIMEOUT_MS);

            try {
                const response = await fetch(url, { ...options, signal: controller.signal });
                clearTimeout(timeoutId);

                const duration = Date.now() - startTime;
                lastResponseTime = duration;

                // Log response time from server header if available
                const serverTime = response.headers.get('X-Response-Time-Ms');
                const correlationId = response.headers.get('X-Correlation-ID');

                if (duration > SLOW_THRESHOLD_MS) {
                    slowRequestCount++;
                    console.warn(`⚠️ Slow request [${correlationId}]: ${url} took ${duration}ms (server: ${serverTime}ms)`);
                    updateConnectionStatus('slow', `Slow (${duration}ms)`);
                } else if (connectionLost) {
                    connectionLost = false;
                    console.log('Connection restored');
                    updateConnectionStatus('connected', 'Connected');
                } else {
                    updateConnectionStatus('connected', 'Connected');
                }

                return response;
            } catch (e) {
                clearTimeout(timeoutId);
                if (e.name === 'AbortError') {
                    connectionLost = true;
                    updateConnectionStatus('disconnected', 'Timeout - Retrying...');
                    throw new Error('Request timed out');
                }
                throw e;
            }
        }

        async function refreshDisplay() {
            try {
                // Fetch queue with timing
                const response = await timedFetch('/api/queue');
                const data = await response.json();

                if (!data.success) {
                    console.error('Queue fetch failed');
                    return;
                }

                const queue = data.queue || [];
                const nowServingEl = document.getElementById('nowServing');
                const nowServingName = document.getElementById('nowServingName');
                const queueList = document.getElementById('queueList');

                // Fetch simulator info to find who's being served
                const simsResponse = await timedFetch('/api/discover_simulators');
                const simsData = await simsResponse.json();
                const simulators = simsData.simulators || [];

                // Find current drivers (active sessions)
                const activeDrivers = simulators
                    .filter(s => s.current_driver && s.current_driver.trim())
                    .map(s => s.current_driver);

                // Show now serving
                if (activeDrivers.length > 0) {
                    nowServingEl.classList.remove('empty');
                    nowServingName.textContent = activeDrivers[0];
                    currentlyServing = activeDrivers[0];
                } else {
                    nowServingEl.classList.add('empty');
                    nowServingName.textContent = queue.length > 0 ? 'Ready for next guest' : 'Waiting for first guest...';
                    currentlyServing = null;
                }

                // Show queue
                if (queue.length === 0) {
                    queueList.innerHTML = '<div class="queue-empty">Queue is empty - join at the kiosk!</div>';
                } else {
                    queueList.innerHTML = queue.map(entry => {
                        const notifiedBadge = entry.notified ? '<span class="notified" title="Notified">🔔</span>' : '';
                        return `
                            <div class="queue-item">
                                <span class="position">#${entry.position}</span>
                                <span class="name">${escapeHtml(entry.name)}</span>
                                <span class="wait">${entry.wait_display || ''}</span>
                                ${notifiedBadge}
                            </div>
                        `;
                    }).join('');
                }

            } catch (e) {
                console.error('Error refreshing display:', e);
                connectionLost = true;
                updateConnectionStatus('disconnected', 'Connection Lost');
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text || '';
            return div.innerHTML;
        }

        // Try to connect via SSE for real-time updates
        function connectSSE() {
            if (eventSource) {
                eventSource.close();
            }

            try {
                eventSource = new EventSource('/api/queue/stream');

                eventSource.onopen = () => {
                    console.log('SSE connected - real-time updates active');
                    updateConnectionStatus('connected', 'Real-time');
                };

                eventSource.onmessage = (event) => {
                    try {
                        const data = JSON.parse(event.data);
                        if (data.queue !== undefined) {
                            // Direct queue update from SSE
                            updateQueueDisplay(data.queue);
                        }
                    } catch (e) {
                        console.error('SSE parse error:', e);
                    }
                };

                eventSource.onerror = () => {
                    console.log('SSE error - falling back to polling');
                    eventSource.close();
                    eventSource = null;
                    // Fall back to polling
                };
            } catch (e) {
                console.log('SSE not supported - using polling');
            }
        }

        function updateQueueDisplay(queue) {
            const queueList = document.getElementById('queueList');
            if (queue.length === 0) {
                queueList.innerHTML = '<div class="queue-empty">Queue is empty - join at the kiosk!</div>';
            } else {
                queueList.innerHTML = queue.map(entry => {
                    const notifiedBadge = entry.notified ? '<span class="notified" title="Notified">🔔</span>' : '';
                    return `
                        <div class="queue-item">
                            <span class="position">#${entry.position}</span>
                            <span class="name">${escapeHtml(entry.name)}</span>
                            <span class="wait">${entry.wait_display || ''}</span>
                            ${notifiedBadge}
                        </div>
                    `;
                }).join('');
            }
        }

        // Keep-alive ping to maintain connection
        setInterval(() => {
            fetch('/health').catch(() => {
                connectionLost = true;
                updateConnectionStatus('disconnected', 'Connection Lost');
            });
        }, 30000);

        // Initial load
        refreshDisplay();

        // Try SSE first, fall back to polling
        connectSSE();

        // Polling as fallback (or primary if SSE fails)
        setInterval(() => {
            if (!eventSource || eventSource.readyState !== EventSource.OPEN) {
                refreshDisplay();
            }
        }, 5000);
    </script>
</body>
</html>
"""

# Store the main window instance and UI bridge globally
main_window = None
ui_bridge = None

# Receiver URL cache
receiver_url_cache = None

# --- CSP companion app command channel ---------------------------------------
# The LeaderboardSector CSP Lua app (ac_app/) polls /api/ac/command a few times
# a second while Assetto Corsa is running. Commands are one-shot: the Qt side
# issues one, the route serves it until the app acks it (or it expires).
ac_app_lock = threading.Lock()
ac_app_state = {
    'last_poll': 0.0,   # time.time() of the app's most recent poll
    'command': None,    # {'id', 'action', 'spawn', 'issued'}
    'next_id': 1,
    'acked_id': 0,
    'ack_ok': False,
}

def ac_app_alive():
    """True if the CSP companion app has polled within the last 3 seconds."""
    with ac_app_lock:
        return (time.time() - ac_app_state['last_poll']) < 3.0

def ac_app_issue_teleport(spawn):
    """Queue a teleport command for the CSP app; returns the command id."""
    with ac_app_lock:
        cmd_id = ac_app_state['next_id']
        ac_app_state['next_id'] += 1
        ac_app_state['command'] = {'id': cmd_id, 'action': 'teleport',
                                   'spawn': spawn, 'issued': time.time()}
        return cmd_id

def ac_app_command_done(cmd_id):
    """True once the CSP app acked the command as executed successfully."""
    with ac_app_lock:
        return ac_app_state['acked_id'] >= cmd_id and ac_app_state['ack_ok']

def ac_app_clear_command(cmd_id):
    """Withdraw a still-pending command (used before falling back to Ctrl+O)."""
    with ac_app_lock:
        cmd = ac_app_state['command']
        if cmd and cmd['id'] == cmd_id:
            ac_app_state['command'] = None

@app.route('/')
def index():
    return HTML_CONTENT

@app.route('/admin')
def admin():
    return ADMIN_HTML_CONTENT

@app.route('/queue')
def queue_page():
    return QUEUE_HTML_CONTENT

@app.route('/queue-display')
def queue_display_page():
    return QUEUE_DISPLAY_HTML_CONTENT

@app.route('/health', methods=['GET'])
def health_check():
    """Fast health check endpoint - no blocking operations"""
    return jsonify({
        'status': 'ok',
        'timestamp': int(time.time()),
        'simulator_id': main_window.simulator_id.currentText() if main_window else None,
        'server_ready': main_window is not None and ui_bridge is not None
    })

# ============================================
# SERVER-SENT EVENTS (SSE) FOR REAL-TIME UPDATES
# Pushes queue updates instantly instead of polling
# ============================================
from flask import Response, stream_with_context

# Track last known queue state for change detection
_sse_last_queue_hash = None
_sse_clients_connected = 0

def get_queue_data_for_sse():
    """Fetch queue data from receiver for SSE broadcast using pooled connection"""
    receiver_url = get_receiver_base_url()
    if not receiver_url:
        return None

    # Check circuit breaker first
    if receiver_circuit.is_open():
        return None

    try:
        # Use pooled session for connection reuse
        response = receiver_session.get(f"{receiver_url}/api/queue", timeout=2)
        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                queue = data.get('queue', [])
                # Add notification status
                for entry in queue:
                    entry['notified'] = entry.get('id') in notified_queue_ids
                    entry['wait_display'] = calculate_wait_display(entry.get('position', 1))
                receiver_circuit.record_success()
                return queue
    except Exception as e:
        receiver_circuit.record_failure()
        print(f"[SSE] Error fetching queue: {e}")
    return None

@app.route('/api/queue/stream')
def queue_stream():
    """Server-Sent Events endpoint for real-time queue updates"""
    global _sse_clients_connected

    def generate():
        global _sse_last_queue_hash, _sse_clients_connected
        _sse_clients_connected += 1
        print(f"[SSE] Client connected (total: {_sse_clients_connected})")

        last_hash = None
        try:
            while True:
                # Fetch current queue
                queue = get_queue_data_for_sse()
                if queue is not None:
                    # Simple hash to detect changes
                    current_hash = hash(json.dumps(queue, sort_keys=True))

                    if current_hash != last_hash:
                        last_hash = current_hash
                        _sse_last_queue_hash = current_hash

                        # Send update to client
                        data = json.dumps({'queue': queue, 'timestamp': int(time.time())})
                        yield f"data: {data}\n\n"

                # Check every 500ms
                time.sleep(0.5)
        except GeneratorExit:
            pass
        finally:
            _sse_clients_connected -= 1
            print(f"[SSE] Client disconnected (total: {_sse_clients_connected})")

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'  # Disable nginx buffering
        }
    )

@app.route('/api/diagnostics', methods=['GET'])
def diagnostics():
    """Return comprehensive system diagnostics for troubleshooting"""
    receiver_url = get_receiver_base_url()
    receiver_ok = False
    receiver_latency = None

    if receiver_url:
        try:
            start = time.time()
            # Use the pooled session for connection reuse
            resp = receiver_session.get(f"{receiver_url}/health", timeout=2)
            receiver_latency = int((time.time() - start) * 1000)
            receiver_ok = resp.status_code == 200
        except Exception:
            pass

    # Circuit breaker status
    circuit_status = receiver_circuit.get_status()

    return jsonify({
        'timestamp': int(time.time()),
        'local_ip': get_local_ip(),
        'mdns_hostname': f"sim{mdns_service.current_sim_id}.local" if mdns_service.current_sim_id else None,
        'mdns_healthy': mdns_service.is_healthy() if hasattr(mdns_service, 'is_healthy') else None,
        'receiver_url': receiver_url,
        'receiver_connected': receiver_ok,
        'receiver_latency_ms': receiver_latency,
        'flask_server': 'waitress',
        'sms_enabled': sms_config.get('enabled', False),
        'sms_provider': sms_config.get('provider'),
        'sms_queue_size': sms_send_queue.qsize() if 'sms_send_queue' in dir() else 0,
        'simulator_id': main_window.simulator_id.currentText() if main_window else None,
        'current_driver': main_window.driver_name.text() if main_window else None,
        # Instrumentation data
        'slow_request_count': slow_request_count,
        'last_slow_request_time': last_slow_request_time.isoformat() if last_slow_request_time else None,
        'last_slow_request_path': last_slow_request_path,
        'slow_request_threshold_ms': SLOW_REQUEST_THRESHOLD_MS,
        # Connection reliability
        'circuit_breaker': circuit_status,
        'heartbeat_latency_ms': int(_heartbeat_latency) if _heartbeat_latency else None,
        'heartbeat_last_success': _heartbeat_last_success,
        'sse_clients_connected': _sse_clients_connected
    })

@app.route('/get_driver', methods=['GET'])
def get_driver():
    if main_window:
        return jsonify({'driver_name': main_window.driver_name.text()})
    return jsonify({'driver_name': ''})

@app.route('/set_driver', methods=['POST'])
def set_driver():
    import datetime as dt
    start_time = time.time()
    print(f"\n[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] === SET_DRIVER REQUEST RECEIVED ===")
    try:
        data = request.get_json() or {}
        driver_name = str(data.get('driver_name') or '').strip()
        driver_email = str(data.get('driver_email') or '').strip()
        driver_phone = str(data.get('driver_phone') or '').strip()
        print(f"[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Driver: {driver_name}")

        if not driver_name:
            return jsonify({'success': False, 'error': 'driver_name is required'}), 400

        if is_lead_gen_enabled():
            if not driver_email:
                return jsonify({'success': False, 'error': 'email is required when Lead Gen is enabled'}), 400
            if not is_valid_email_address(driver_email):
                return jsonify({'success': False, 'error': 'valid email is required when Lead Gen is enabled'}), 400

            formatted_phone = format_phone_e164(driver_phone)
            if not formatted_phone:
                return jsonify({'success': False, 'error': 'valid phone is required when Lead Gen is enabled'}), 400
            driver_phone = formatted_phone
        else:
            driver_email = ""
            driver_phone = ""

        # Lead-gen survey: every configured question must be answered with one
        # of its own options (answers keyed by question index from /get_settings)
        survey_answers_raw = data.get('survey_answers') or {}
        answers_by_prompt = {}
        questions = []
        if main_window is not None and hasattr(main_window, 'lead_questions'):
            questions = main_window.lead_questions.get('questions', [])
        if is_lead_gen_enabled() and questions:
            for idx, q in enumerate(questions):
                answer = str(survey_answers_raw.get(str(idx), survey_answers_raw.get(idx, '')) or '').strip()
                if answer not in q['options']:
                    return jsonify({'success': False, 'error': f"Please answer: {q['prompt']}"}), 400
                answers_by_prompt[q['prompt']] = answer

        if main_window:
            # Store contact fields before changing the driver so session-start
            # notifications and partner events carry the right data.
            main_window.last_driver_email = driver_email or ""
            main_window.last_driver_phone = driver_phone or ""
            main_window.last_survey_answers = answers_by_prompt

            # Persist the lead (name/email/phone + survey answers) to leads.csv
            if is_lead_gen_enabled():
                append_lead_to_csv(driver_name, driver_email, driver_phone, answers_by_prompt)

            # Emit signal to UI thread to update driver and start video
            try:
                if ui_bridge:
                    print(f"[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Emitting signal to UI...")
                    ui_bridge.driver_registered.emit(driver_name)
                    print(f"[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Signal emitted")
                else:
                    # Fallback (not ideal): direct call
                    main_window.update_driver_name(driver_name)
            except Exception:
                main_window.update_driver_name(driver_name)
            elapsed = (time.time() - start_time) * 1000
            print(f"[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] === SET_DRIVER COMPLETE ({elapsed:.0f}ms) ===\n")
            return jsonify({'success': True, 'driver_name': driver_name})
    except Exception as e:
        print(f"Error setting driver: {str(e)}")
    return jsonify({'success': False})

@app.route('/get_settings', methods=['GET'])
def get_settings():
    if not main_window:
        return jsonify({
            'lead_gen_enabled': False,
            'require_email': False,
            'require_phone': False,
            'background_image': get_default_web_background(),
            'timer_active': False,
            'timer_seconds_left': 0,
            'rotation_mode': 'Time',
            'laps_active': False,
            'laps_progress': {
                'started': False,
                'count': 0,
                'target': 0
            },
            'survey_questions': []
        })
    
    # Lead Gen requires both email and phone before a driver can continue.
    lead_gen_enabled = is_lead_gen_enabled()
    
    # Get selected background image; otherwise the mobile pages use a plain black backdrop.
    background_image = os.path.basename(main_window.background_image_path.text()) if main_window.background_image_path.text() else get_default_web_background()
    
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
    
    # Survey questions for the registration page (id = index, used as the
    # answer key in /set_driver)
    survey_questions = [
        {'id': idx, 'prompt': q['prompt'], 'options': q['options']}
        for idx, q in enumerate(getattr(main_window, 'lead_questions', {}).get('questions', []))
    ]

    return jsonify({
        'lead_gen_enabled': lead_gen_enabled,
        'require_email': lead_gen_enabled,
        'require_phone': lead_gen_enabled,
        'background_image': background_image,
        'timer_active': timer_active,
        'timer_seconds_left': timer_seconds_left,
        'rotation_mode': rotation_mode,
        'laps_active': laps_active,
        'laps_progress': laps_progress,
        'survey_questions': survey_questions
    })

@app.route('/backgrounds/<filename>')
def background_file(filename):
    directory, safe_name = find_resource_file(filename)
    if not directory:
        return ("Background not found", 404)
    return send_from_directory(str(directory), safe_name)

@app.route('/logo')
def serve_logo():
    directory, safe_name = find_resource_file('logo.png')
    if not directory:
        return ("Logo not found", 404)
    return send_from_directory(str(directory), safe_name)

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

# ============================================
# QUEUE PROXY ENDPOINTS
# These proxy queue API calls to the Receiver while handling SMS locally
# ============================================

def get_receiver_base_url():
    """Get receiver base URL for proxying requests"""
    if main_window and hasattr(main_window, 'current_server_url'):
        url = main_window.current_server_url
        if url:
            return url.split(',')[0].strip()
    return None

@app.route('/api/queue', methods=['GET'])
def proxy_queue_list():
    """Proxy queue list from Receiver with circuit breaker protection"""
    response, error = forward_to_receiver('/api/queue', method='GET', timeout=3)

    if error:
        print(f"[Queue Proxy] Error fetching queue: {error}")
        return jsonify({'success': False, 'error': error}), 503

    try:
        data = response.json()

        # Add notification status and wait_display to each queue entry
        if data.get('success') and data.get('queue'):
            for entry in data['queue']:
                entry['notified'] = entry.get('id') in notified_queue_ids
                # Add wait_display for each position
                entry['wait_display'] = calculate_wait_display(entry.get('position', 1))

        return jsonify(data), response.status_code
    except Exception as e:
        print(f"[Queue Proxy] Error parsing response: {e}")
        return jsonify({'success': False, 'error': str(e)}), 503

@app.route('/api/queue/join', methods=['POST'])
def proxy_queue_join():
    """Proxy join request to Receiver with circuit breaker, then send confirmation SMS"""
    import datetime as dt
    start_time = time.time()
    correlation_id = getattr(g, 'correlation_id', 'unknown')
    print(f"\n[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] [{correlation_id}] === QUEUE JOIN REQUEST ===")

    data = request.get_json() or {}
    name = str(data.get('name') or '').strip()
    email = str(data.get('email') or '').strip()
    phone = str(data.get('phone') or '').strip()
    print(f"[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] [{correlation_id}] Processing: name='{name}', email='{email or 'none'}', phone='{phone or 'none'}'")

    if not name:
        return jsonify({'success': False, 'error': 'Name is required'}), 400

    if is_lead_gen_enabled():
        if not email:
            return jsonify({'success': False, 'error': 'Email is required'}), 400
        if not is_valid_email_address(email):
            return jsonify({'success': False, 'error': 'Valid email is required'}), 400
        formatted_phone = format_phone_e164(phone)
        if not formatted_phone:
            return jsonify({'success': False, 'error': 'Valid phone is required'}), 400
        phone = formatted_phone
    else:
        email = ""
        phone = ""

    data['name'] = name
    data['email'] = email
    data['phone'] = phone

    # Forward with circuit breaker protection
    print(f"[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] [{correlation_id}] Forwarding to Receiver...")
    forward_start = time.time()
    response, error = forward_to_receiver('/api/queue/join', data=data, timeout=3)
    forward_elapsed = (time.time() - forward_start) * 1000
    print(f"[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] [{correlation_id}] Receiver responded in {forward_elapsed:.0f}ms")

    if error:
        print(f"[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] [{correlation_id}] ERROR: {error}")
        return jsonify({'success': False, 'error': error}), 503

    try:
        result = response.json()
        print(f"[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] [{correlation_id}] Result: {result}")

        # Calculate wait_display locally
        if result.get('success'):
            position = result.get('position', 1)
            result['wait_display'] = calculate_wait_display(position)
            print(f"[Queue] [{correlation_id}] Calculated wait: {result['wait_display']} (pos={position})")

        # Send confirmation SMS if phone provided and join was successful
        print(f"[SMS] [{correlation_id}] Checking SMS: success={result.get('success')}, phone='{bool(phone)}'")

        if result.get('success') and phone:
            position = result.get('position', '?')
            wait_display = result.get('wait_display', 'a few minutes')
            confirmation_msg = f"Hi {name}! You're #{position} in the sim racing queue. Estimated wait: {wait_display}. We'll text you when it's almost your turn!"

            # Use async SMS - queue for background sending (non-blocking!)
            clean_phone = format_phone_e164(phone)
            if clean_phone:
                queue_sms_async(clean_phone, confirmation_msg, "queue_confirmation")
                result['confirmation_sms_queued'] = True
                print(f"[SMS] [{correlation_id}] Confirmation queued for {name}")
            else:
                result['confirmation_sms_queued'] = False

        total_elapsed = (time.time() - start_time) * 1000
        print(f"[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] [{correlation_id}] === COMPLETE ({total_elapsed:.0f}ms) ===\n")
        return jsonify(result), response.status_code
    except Exception as e:
        print(f"[Queue] [{correlation_id}] ERROR parsing response: {e}")
        return jsonify({'success': False, 'error': str(e)}), 503

@app.route('/api/queue/remove', methods=['POST'])
def proxy_queue_remove():
    """Proxy remove request to Receiver with circuit breaker"""
    data = request.get_json() or {}
    response, error = forward_to_receiver('/api/queue/remove', data=data, timeout=3)

    if error:
        print(f"[Queue Proxy] Error removing from queue: {error}")
        return jsonify({'success': False, 'error': error}), 503

    try:
        return jsonify(response.json()), response.status_code
    except Exception as e:
        print(f"[Queue Proxy] Error parsing response: {e}")
        return jsonify({'success': False, 'error': str(e)}), 503

@app.route('/api/sms/test', methods=['POST'])
def test_sms():
    """Test SMS sending - for debugging"""
    data = request.get_json() or {}
    phone = data.get('phone', '')
    message = data.get('message', 'Test message from Sim Racing Queue!')

    clean_phone = format_phone_e164(phone)
    if not clean_phone:
        return jsonify({'success': False, 'error': f'Invalid phone format: {phone}'}), 400

    print(f"[SMS Test] Sending test to {clean_phone}")
    success = send_sms_textbelt(clean_phone, message)

    return jsonify({
        'success': success,
        'phone_input': phone,
        'phone_formatted': clean_phone,
        'sms_enabled': sms_config.get('enabled'),
        'provider': sms_config.get('provider')
    })

@app.route('/api/queue/assign', methods=['POST'])
def proxy_queue_assign():
    """Proxy assign request to Receiver with circuit breaker, then send SMS locally"""
    correlation_id = getattr(g, 'correlation_id', 'unknown')
    data = request.get_json() or {}
    print(f"[Queue] [{correlation_id}] Assign request: {data}")

    # Forward with circuit breaker protection
    response, error = forward_to_receiver('/api/queue/assign', data=data, timeout=3)

    if error:
        print(f"[Queue] [{correlation_id}] ERROR: {error}")
        return jsonify({'success': False, 'error': error}), 503

    try:
        result = response.json()
        print(f"[Queue] [{correlation_id}] Assign result: {result}")

        # If Receiver succeeded and returned phone, send SMS from Sender (async!)
        if result.get('success'):
            phone = result.get('phone')
            name = result.get('name', 'Guest')
            simulator_name = data.get('simulator_name', 'your simulator')

            if phone:
                # Format the notification message
                message = sms_config.get("message_template", "It's your turn to race!").format(
                    name=name,
                    simulator=simulator_name
                )
                clean_phone = format_phone_e164(phone)
                if clean_phone:
                    # Use async SMS - queue for background sending (non-blocking!)
                    queue_sms_async(clean_phone, message, "turn_notification")
                    result['sms_queued'] = True
                    print(f"[SMS] [{correlation_id}] Turn notification queued for {name}")
                else:
                    result['sms_queued'] = False
            else:
                result['sms_queued'] = False

        return jsonify(result), response.status_code
    except Exception as e:
        print(f"[Queue] [{correlation_id}] ERROR parsing response: {e}")
        return jsonify({'success': False, 'error': str(e)}), 503

@app.route('/api/queue/stats', methods=['GET'])
def proxy_queue_stats():
    """Proxy stats from Receiver with circuit breaker"""
    response, error = forward_to_receiver('/api/queue/stats', method='GET', timeout=3)

    if error:
        print(f"[Queue Proxy] Error fetching stats: {error}")
        return jsonify({'success': False, 'error': error}), 503

    try:
        return jsonify(response.json()), response.status_code
    except Exception as e:
        print(f"[Queue Proxy] Error parsing response: {e}")
        return jsonify({'success': False, 'error': str(e)}), 503

@app.route('/api/session/start_remote', methods=['POST'])
def start_remote_session():
    """Start a session for an assigned driver (called by admin)"""
    global main_window, ui_bridge
    try:
        data = request.get_json() or {}
        driver_name = str(data.get('driver_name') or '').strip()
        queue_id = data.get('queue_id', '')
        driver_email = str(data.get('driver_email') or '').strip()
        driver_phone = str(data.get('driver_phone') or '').strip()

        if not driver_name:
            return jsonify({'success': False, 'error': 'driver_name is required'}), 400

        if is_lead_gen_enabled():
            if not driver_email:
                return jsonify({'success': False, 'error': 'driver_email is required when Lead Gen is enabled'}), 400
            if not is_valid_email_address(driver_email):
                return jsonify({'success': False, 'error': 'valid driver_email is required when Lead Gen is enabled'}), 400
            formatted_phone = format_phone_e164(driver_phone)
            if not formatted_phone:
                return jsonify({'success': False, 'error': 'valid driver_phone is required when Lead Gen is enabled'}), 400
            driver_phone = formatted_phone
        else:
            driver_email = ""
            driver_phone = ""

        if main_window:
            # Store queue ID for session tracking
            main_window.assigned_queue_id = queue_id
            main_window.last_driver_email = driver_email or ""
            main_window.last_driver_phone = driver_phone or ""

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
            'port': get_web_interface_port(),
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
        'port': get_web_interface_port(),
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
        'port': get_web_interface_port(),
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
            'port': get_web_interface_port(),
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
    for port in get_simulator_probe_ports():
        try:
            response = requests.get(
                f'http://{ip}:{port}/api/simulator_info',
                timeout=2.0
            )
            if response.ok:
                data = response.json()
                data['port'] = data.get('port', port)
                return jsonify({
                    'reachable': True,
                    'simulator_info': data
                })
        except Exception:
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

@app.route('/api/ac/command', methods=['GET'])
def ac_app_poll_command():
    """Polled by the LeaderboardSector CSP Lua app. Plain-text protocol so the
    Lua side needs no JSON parsing: 'none' or 'teleport|<id>|<spawn_set>'."""
    with ac_app_lock:
        ac_app_state['last_poll'] = time.time()
        cmd = ac_app_state['command']
        if cmd and (time.time() - cmd['issued']) > 10.0:
            # Nobody executed it in time; drop it so a late-starting app
            # doesn't teleport the next driver out of nowhere
            ac_app_state['command'] = None
            cmd = None
        if cmd:
            return (f"{cmd['action']}|{cmd['id']}|{cmd['spawn']}", 200,
                    {'Content-Type': 'text/plain'})
    return 'none', 200, {'Content-Type': 'text/plain'}

@app.route('/api/ac/ack', methods=['GET'])
def ac_app_ack_command():
    """Ack from the CSP app after it executed (or failed to execute) a command."""
    cmd_id = request.args.get('id', type=int) or 0
    status = request.args.get('status', 'ok')
    with ac_app_lock:
        cmd = ac_app_state['command']
        if cmd and cmd['id'] == cmd_id:
            ac_app_state['command'] = None
        ac_app_state['acked_id'] = max(ac_app_state['acked_id'], cmd_id)
        ac_app_state['ack_ok'] = (status == 'ok')
    print(f"[Sectors] CSP app ack for command {cmd_id}: {status}")
    return 'ok', 200, {'Content-Type': 'text/plain'}

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
    """Run Flask with Waitress production WSGI server for multi-threaded handling"""
    web_port = get_web_interface_port()

    # Start heartbeat thread for connection monitoring
    start_heartbeat_thread()

    try:
        from waitress import serve
    except ImportError:
        print(f"[Flask] Waitress not available; starting Flask threaded server on port {web_port}")
        app.run(host='0.0.0.0', port=web_port, threaded=True, use_reloader=False)
        return

    print(f"[Flask] Starting production server with Waitress on port {web_port} (8 threads)...")
    print("[Flask] Enterprise features enabled:")
    print("[Flask]   - Correlation IDs for request tracing")
    print("[Flask]   - Slow request detection (>2s)")
    print("[Flask]   - Connection pooling with auto-retry")
    print("[Flask]   - Circuit breaker for failure recovery")
    print("[Flask]   - Heartbeat monitoring (10s interval)")
    print("[Flask]   - SSE for real-time updates")

    serve(app, host='0.0.0.0', port=web_port, threads=8,
          connection_limit=100, channel_timeout=30)

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
        self.poll_interval_ms = 100  # Subclasses may tighten this (sector challenge needs finer sampling)
    
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
                    # Only attempt to connect if not already connected
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

                        self.is_connected = True
                        self.connection_status.emit(f"Connected to {self.game_name}! ✅", "green")

                    # Read lap time - returns (is_valid_lap, lap_time)
                    # A special return of (False, -1) indicates disconnection
                    is_valid_lap, lap_time = self.read_lap_time(None)

                    # Check for disconnection signal
                    if lap_time == -1:
                        raise ConnectionError("Game disconnected")

                    if is_valid_lap and lap_time > 0 and lap_time != self.last_lap_time:
                        self.last_lap_time = lap_time
                        driver = self.get_current_driver()
                        self.lap_completed.emit(lap_time, driver)

                    self.msleep(self.poll_interval_ms)

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
        for port in get_simulator_probe_ports():
            try:
                response = requests.get(
                    f'http://{ip}:{port}/api/simulator_info',
                    timeout=timeout
                )
                if response.ok:
                    data = response.json()
                    data['ip'] = ip
                    data['port'] = data.get('port', port)
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

    Enhanced with:
    - Health monitoring to detect service failures
    - Auto-recovery when mDNS service becomes unhealthy
    - Aggressive re-announcement during cold start (first 2 minutes)
    """

    def __init__(self):
        self.zeroconf = None
        self.service_info = None
        self.current_sim_id = None
        self.current_port = 5001
        self.healthy = False
        self._monitor_thread = None
        self._announce_thread = None
        self._stop_monitoring = False

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
            self.current_port = port
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

            self.healthy = True
            self._stop_monitoring = False
            print(f"[mDNS] Service registered: http://{service_name}.local:{port}")

            # Start health monitoring
            self._start_health_monitor()

            # Start aggressive re-announcement for cold start
            self._start_cold_start_announcer()

            return f"{service_name}.local"

        except Exception as e:
            print(f"[mDNS] Failed to start service: {e}")
            self.healthy = False
            return None

    def _start_health_monitor(self):
        """Start background thread that monitors mDNS health"""
        def monitor():
            check_interval = 30  # Check every 30 seconds
            while not self._stop_monitoring and self.healthy:
                time.sleep(check_interval)
                if self._stop_monitoring:
                    break
                if not self._verify_registration():
                    print("[mDNS] Service unhealthy, attempting re-registration...")
                    self._reregister()

        self._monitor_thread = threading.Thread(target=monitor, daemon=True)
        self._monitor_thread.start()
        print("[mDNS] Health monitor started")

    def _start_cold_start_announcer(self):
        """Aggressively re-announce for first 2 minutes to help cold start"""
        def announce_loop():
            # Re-announce every 5 seconds for 2 minutes (24 times)
            for i in range(24):
                if self._stop_monitoring:
                    break
                time.sleep(5)
                if self._stop_monitoring or not self.zeroconf or not self.service_info:
                    break
                try:
                    # Force re-announcement by unregister/register
                    self.zeroconf.unregister_service(self.service_info)
                    time.sleep(0.3)
                    self.zeroconf.register_service(self.service_info)
                    if i < 3:  # Only log first few
                        print(f"[mDNS] Cold start re-announcement {i+1}/24")
                except Exception as e:
                    print(f"[mDNS] Re-announcement error: {e}")
            print("[mDNS] Cold start announcement period complete")

        self._announce_thread = threading.Thread(target=announce_loop, daemon=True)
        self._announce_thread.start()
        print("[mDNS] Cold start announcer started (2 minute aggressive mode)")

    def _verify_registration(self):
        """Check if our mDNS service is still working"""
        if not self.current_sim_id or not self.zeroconf:
            return False
        try:
            # Try to resolve our own hostname
            hostname = f"sim{self.current_sim_id}.local"
            socket.gethostbyname(hostname)
            return True
        except socket.gaierror:
            return False
        except Exception:
            return False

    def _reregister(self):
        """Re-register the mDNS service after a failure"""
        if self.current_sim_id:
            try:
                # Try to cleanly stop first
                if self.zeroconf and self.service_info:
                    try:
                        self.zeroconf.unregister_service(self.service_info)
                    except:
                        pass
                    try:
                        self.zeroconf.close()
                    except:
                        pass

                # Re-create and register
                local_ip = get_local_ip()
                service_name = f"sim{self.current_sim_id}"

                self.service_info = ServiceInfo(
                    "_http._tcp.local.",
                    f"{service_name}._http._tcp.local.",
                    addresses=[socket.inet_aton(local_ip)],
                    port=self.current_port,
                    properties={
                        'path': '/',
                        'simulator_id': str(self.current_sim_id),
                        'app': 'Lap Time Sender'
                    },
                    server=f"{service_name}.local."
                )

                self.zeroconf = Zeroconf()
                self.zeroconf.register_service(self.service_info)
                self.healthy = True
                print(f"[mDNS] Service re-registered successfully: {service_name}.local")
            except Exception as e:
                print(f"[mDNS] Re-registration failed: {e}")
                self.healthy = False

    def stop(self):
        """Stop the mDNS service."""
        self._stop_monitoring = True
        self.healthy = False
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

    def is_healthy(self):
        """Check if mDNS service is currently healthy"""
        return self.healthy and self.zeroconf is not None


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
            'port': get_web_interface_port()
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

    # Custom sector challenge signals
    sector_result = pyqtSignal(int, float, float, bool)  # sector_index, sector_time_s, target_s, passed
    challenge_failed = pyqtSignal(int, float, float)     # sector_index, distance_pct, elapsed_s

    def __init__(self):
        super().__init__()
        self.last_completed_laps = 0
        self.physics_handle = None
        self.graphics_handle = None
        self.challenge_config = None
        self._chal_reset()

    def set_challenge_config(self, config):
        """Apply a sector challenge config (dict with 'enabled' and 'sectors').

        Called from the UI thread; the reference swap is atomic so the monitor
        thread always sees a complete config.
        """
        self.challenge_config = normalize_sector_config(config) if config else None
        enabled = bool(self.challenge_config and self.challenge_config.get('enabled'))
        # Sector boundary/expiry detection needs finer sampling than lap detection
        self.poll_interval_ms = 20 if enabled else 100
        self._chal_reset()
        if enabled:
            print(f"[Sectors] Challenge active with {len(self.challenge_config['sectors'])} sectors")

    def _challenge_enabled(self):
        cfg = self.challenge_config
        return bool(cfg and cfg.get('enabled') and cfg.get('sectors'))

    def _chal_reset(self):
        self._chal_state = 'idle'      # 'idle' | 'running' | 'failed'
        self._chal_sector = 0
        self._chal_sector_anchor_ms = 0
        self._chal_run_anchor_ms = 0
        self._chal_prev_pos = None
        self._chal_prev_ms = None
        self._chal_lap_ok_latch = False
        self._chal_reset_requested = False

    def request_challenge_reset(self):
        """Thread-safe request (e.g. after a CSP teleport) to re-arm the
        challenge state machine; consumed by the monitor's poll loop."""
        self._chal_reset_requested = True

    def _chal_consume_lap_ok(self):
        """Read-and-clear the 'run passed all sectors' verdict.

        Latched because completedLaps can tick one poll after the spline
        position wraps, so the verdict and AC's lap event may not land on the
        same 20ms sample.
        """
        ok = self._chal_lap_ok_latch
        self._chal_lap_ok_latch = False
        return ok

    def _chal_begin_run(self, anchor_ms):
        self._chal_state = 'running'
        self._chal_sector = 0
        self._chal_sector_anchor_ms = anchor_ms
        self._chal_run_anchor_ms = anchor_ms

    def _chal_fail(self, sector_index, pos, lap_ms):
        self._chal_state = 'failed'
        distance_pct = max(0.0, min(pos, 1.0)) * 100.0
        elapsed_s = max(0, lap_ms - self._chal_run_anchor_ms) / 1000.0
        print(f"[Sectors] FAILED sector {sector_index + 1} at {distance_pct:.2f}% (elapsed {elapsed_s:.3f}s)")
        self.challenge_failed.emit(sector_index, distance_pct, elapsed_s)

    def _challenge_update(self, graphics):
        """Judge the current run against the configured sector targets.

        Returns True only on the poll where the car crosses the finish line
        having met every sector target (that lap may go to the leaderboard).
        """
        if self._chal_reset_requested:
            # A teleport back to start/finish just happened: drop any stale
            # run state so the next driver's run arms fresh at the line
            self._chal_reset()

        sectors = self.challenge_config['sectors']
        pos = float(graphics.normalizedCarPosition)
        lap_ms = int(graphics.iCurrentTime)
        prev_pos = self._chal_prev_pos
        prev_ms = self._chal_prev_ms
        self._chal_prev_pos = pos
        self._chal_prev_ms = lap_ms

        if prev_pos is None:
            return False

        # iCurrentTime is INT32_MAX while AC has no live lap running
        timer_valid = 0 <= lap_ms < 86400000
        prev_timer_valid = prev_ms is not None and 0 <= prev_ms < 86400000
        line_crossed = prev_pos > 0.9 and pos < 0.1
        timer_reset = timer_valid and prev_timer_valid and lap_ms + 500 < prev_ms
        teleported = (prev_pos - pos) > 0.05 and not line_crossed

        if self._chal_state == 'running':
            if line_crossed:
                lap_ok = False
                if self._chal_sector == len(sectors) - 1:
                    # Judge the final sector using the last time before the timer reset
                    final_target = sectors[self._chal_sector]['target_time']
                    elapsed_s = max(0, prev_ms - self._chal_sector_anchor_ms) / 1000.0 if prev_timer_valid else 0.0
                    passed = elapsed_s <= final_target
                    self.sector_result.emit(self._chal_sector, elapsed_s, final_target, passed)
                    if passed:
                        print(f"[Sectors] Run COMPLETE - all {len(sectors)} sectors passed")
                        lap_ok = True
                    else:
                        self._chal_fail(self._chal_sector, prev_pos, prev_ms)
                else:
                    print(f"[Sectors] Line crossed in sector {self._chal_sector + 1} - run not scored")
                self._chal_lap_ok_latch = lap_ok
                if self._chal_state != 'failed':
                    # The next run starts immediately at the line (lap timer restarts at ~0)
                    self._chal_begin_run(0)
                return lap_ok

            if timer_reset or teleported or not timer_valid:
                # Session restarted or car reset mid-run - abandon without scoring
                print("[Sectors] Run abandoned (session restart or car reset)")
                self._chal_state = 'idle'
                return False

            i = self._chal_sector
            target_ms = sectors[i]['target_time'] * 1000.0
            elapsed_ms = lap_ms - self._chal_sector_anchor_ms
            if i < len(sectors) - 1 and pos >= sectors[i + 1]['start_pct'] / 100.0:
                passed = elapsed_ms <= target_ms
                self.sector_result.emit(i, elapsed_ms / 1000.0, sectors[i]['target_time'], passed)
                if passed:
                    print(f"[Sectors] Sector {i + 1} passed: {elapsed_ms / 1000.0:.3f}s (target {sectors[i]['target_time']:.3f}s)")
                    self._chal_sector = i + 1
                    self._chal_sector_anchor_ms = lap_ms
                else:
                    self._chal_fail(i, pos, lap_ms)
            elif elapsed_ms > target_ms:
                # Time expired mid-sector: instant fail, don't wait for the boundary
                self.sector_result.emit(i, elapsed_ms / 1000.0, sectors[i]['target_time'], False)
                self._chal_fail(i, pos, lap_ms)
            return False

        if self._chal_state == 'failed':
            # Hold until the session restarts or the car is back at the line.
            # A line crossing also resets the lap timer, so check it first.
            if line_crossed:
                self._chal_begin_run(0)
            elif timer_reset or teleported:
                self._chal_state = 'idle'
            return False

        # idle: arm on a line crossing, or on a lap timer freshly counting up
        # (covers session restarts where the car rolls from the pit/start box)
        if line_crossed:
            self._chal_begin_run(0)
        elif timer_valid and prev_timer_valid and 0 < lap_ms < 3000 and prev_ms < lap_ms:
            self._chal_begin_run(lap_ms)
        return False
    
    def connect_to_game(self):
        """Attempt to connect to Assetto Corsa via shared memory"""
        try:
            print("\n=== Attempting AC Connection ===")

            # Clean up any existing handles first
            self.cleanup_handles()

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
                self.cleanup_handles()
                return False, None

            # Read initial data to verify connection
            graphics = AssettoCorsaGraphicsData()
            raw_data = self.graphics_handle.read(ctypes.sizeof(AssettoCorsaGraphicsData))
            self.graphics_handle.seek(0)
            ctypes.memmove(ctypes.byref(graphics), raw_data, ctypes.sizeof(AssettoCorsaGraphicsData))

            # Verify we're getting valid data by checking status
            # Status: 0=OFF, 1=REPLAY, 2=LIVE, 3=PAUSE
            if graphics.status == 0:  # AC is not running
                print("AC is not running (status = 0)")
                self.cleanup_handles()
                return False, None

            print(f"Successfully connected to Assetto Corsa (status: {graphics.status})")
            self.last_completed_laps = graphics.completedLaps  # Sync lap count to avoid false triggers
            return True, None

        except Exception as e:
            print(f"\n=== Connection Error ===")
            print(f"Failed to connect to AC: {str(e)}")
            print("=====================")
            self.cleanup_handles()
            return False, None
    
    def read_lap_time(self, _):
        """Read lap time from Assetto Corsa shared memory

        Returns:
            (is_valid_lap, lap_time) - Normal lap data
            (False, -1) - Signals disconnection (game closed or went to menu)
        """
        try:
            # Check if handles are valid
            if not self.graphics_handle or not self.physics_handle:
                print("AC handles are not initialized")
                return False, -1  # Signal disconnection

            # Read graphics data for lap times
            graphics = AssettoCorsaGraphicsData()
            raw_data = self.graphics_handle.read(ctypes.sizeof(AssettoCorsaGraphicsData))
            self.graphics_handle.seek(0)
            ctypes.memmove(ctypes.byref(graphics), raw_data, ctypes.sizeof(AssettoCorsaGraphicsData))

            # Check if AC is still running (status 0 = OFF/closed)
            if graphics.status == 0:
                print("AC status is 0 (game closed or in menu)")
                return False, -1  # Signal disconnection

            # Read physics data for additional validation
            physics = AssettoCorsaData()
            raw_data = self.physics_handle.read(ctypes.sizeof(AssettoCorsaData))
            self.physics_handle.seek(0)
            ctypes.memmove(ctypes.byref(physics), raw_data, ctypes.sizeof(AssettoCorsaData))

            # Judge custom sectors while a challenge is configured (only on live laps)
            challenge_on = self._challenge_enabled()
            if challenge_on and graphics.status == 2:
                self._challenge_update(graphics)

            # Debug info only when lap completed (reduce spam)
            if graphics.completedLaps > self.last_completed_laps:
                print(f"\n=== Lap Completed ===")
                print(f"Completed Laps: {graphics.completedLaps}")
                print(f"Last Time: {graphics.lastTime}")
                print(f"Speed: {physics.speedKmh:.1f} km/h")
                print(f"Status: {graphics.status}")

            # Check if we've completed a new lap
            if graphics.completedLaps > self.last_completed_laps:
                self.last_completed_laps = graphics.completedLaps

                # In challenge mode only laps that beat every sector target count
                if challenge_on and not self._chal_consume_lap_ok():
                    print("[Sectors] Lap not scored (sector targets not all met)")
                    return False, 0

                # Validate racing context first
                if not is_valid_racing_context(graphics, physics, "Assetto Corsa"):
                    return False, 0

                # Convert last time from milliseconds to seconds
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

        except (ValueError, OSError) as e:
            # Handle closed/invalid mmap handles
            print(f"AC shared memory error (likely game closed): {e}")
            return False, -1  # Signal disconnection

        except Exception as e:
            print(f"Error reading lap time: {str(e)}")
            return False, -1  # Signal disconnection on unexpected errors
            
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

class F1PacketHeader(ctypes.Structure):
    """EA Sports F1 UDP telemetry packet header (2023/2024/2025 format)"""
    _pack_ = 1
    _fields_ = [
        ('m_packetFormat', ctypes.c_uint16),       # Packet format (year e.g. 2023, 2024)
        ('m_gameMajorVersion', ctypes.c_uint8),     # Game major version
        ('m_gameMinorVersion', ctypes.c_uint8),     # Game minor version
        ('m_packetVersion', ctypes.c_uint8),        # Packet version
        ('m_packetId', ctypes.c_uint8),             # Packet type identifier
        ('m_sessionUID', ctypes.c_uint64),          # Unique session identifier
        ('m_sessionTime', ctypes.c_float),          # Session timestamp
        ('m_frameIdentifier', ctypes.c_uint32),     # Frame identifier
        ('m_playerCarIndex', ctypes.c_uint8),       # Index of player's car
        ('m_secondaryPlayerCarIndex', ctypes.c_uint8),  # Index of secondary player's car (splitscreen)
    ]

class F1LapData(ctypes.Structure):
    """Lap data for a single car in EA Sports F1 telemetry (Packet ID 2)"""
    _pack_ = 1
    _fields_ = [
        ('m_lastLapTimeInMS', ctypes.c_uint32),     # Last lap time in milliseconds
        ('m_currentLapTimeInMS', ctypes.c_uint32),   # Current lap time in milliseconds
        ('m_sector1TimeInMS', ctypes.c_uint16),      # Sector 1 time in milliseconds
        ('m_sector2TimeInMS', ctypes.c_uint16),      # Sector 2 time in milliseconds
        ('m_lapDistance', ctypes.c_float),            # Distance around lap in metres
        ('m_totalDistance', ctypes.c_float),          # Total distance in metres
        ('m_safetyCarDelta', ctypes.c_float),        # Safety car delta in seconds
        ('m_carPosition', ctypes.c_uint8),           # Car race position
        ('m_currentLapNum', ctypes.c_uint8),         # Current lap number
        ('m_pitStatus', ctypes.c_uint8),             # 0=none, 1=pitting, 2=in pit area
        ('m_numPitStops', ctypes.c_uint8),           # Number of pit stops taken
        ('m_sector', ctypes.c_uint8),                # 0=sector1, 1=sector2, 2=sector3
        ('m_currentLapInvalid', ctypes.c_uint8),     # Current lap invalid - 0=valid, 1=invalid
        ('m_penalties', ctypes.c_uint8),             # Accumulated time penalties in seconds
        ('m_warnings', ctypes.c_uint8),              # Accumulated number of warnings
        ('m_numUnservedDriveThroughPens', ctypes.c_uint8),
        ('m_numUnservedStopGoPens', ctypes.c_uint8),
        ('m_gridPosition', ctypes.c_uint8),          # Grid position
        ('m_driverStatus', ctypes.c_uint8),          # 0=garage, 1=flying lap, 2=in lap, 3=out lap, 4=on track
        ('m_resultStatus', ctypes.c_uint8),          # Result status
        ('m_pitLaneTimerActive', ctypes.c_uint8),    # Pit lane timer active
        ('m_pitLaneTimeInLaneInMS', ctypes.c_uint16),
        ('m_pitStopTimerInMS', ctypes.c_uint16),
        ('m_pitStopShouldServePen', ctypes.c_uint8),
    ]

class F1Monitor(GameMonitor):
    """Monitor for EA Sports F1 games (F1 23/24/25) via UDP telemetry on port 20777"""
    game_name = "EA Sports F1"

    def __init__(self):
        super().__init__()
        self.udp_socket = None
        self.last_lap_num = 0
        self.last_session_uid = 0
        self.last_packet_time = 0

    def connect_to_game(self):
        """Bind UDP socket on port 20777 and wait for first valid packet"""
        try:
            if self.udp_socket:
                self.cleanup_socket()

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('', 20777))
            sock.settimeout(5.0)

            print("\n=== EA Sports F1: Listening on UDP port 20777 ===")
            print("Waiting for telemetry data (enable UDP telemetry in F1 game settings)...")

            # Wait for first valid packet to confirm game is running
            try:
                data, addr = sock.recvfrom(4096)
                if len(data) >= ctypes.sizeof(F1PacketHeader):
                    header = F1PacketHeader()
                    ctypes.memmove(ctypes.byref(header), data, ctypes.sizeof(F1PacketHeader))
                    print(f"Connected! Receiving telemetry from {addr[0]} (format: {header.m_packetFormat})")
                    self.udp_socket = sock
                    self.last_packet_time = time.time()
                    self.last_session_uid = header.m_sessionUID
                    self.last_lap_num = 0  # Will be set on first lap data packet
                    return True, None
                else:
                    print(f"Received invalid packet (size: {len(data)})")
                    sock.close()
                    return False, None
            except socket.timeout:
                sock.close()
                return False, None

        except OSError as e:
            print(f"Failed to bind UDP port 20777: {e}")
            if self.udp_socket:
                self.cleanup_socket()
            return False, None

    def read_lap_time(self, _):
        """Parse F1 UDP telemetry for lap completions.

        Returns:
            (True, lap_time_seconds) - Valid lap completed
            (False, 0) - No new lap
            (False, -1) - Disconnection detected
        """
        try:
            if not self.udp_socket:
                return False, -1

            self.udp_socket.settimeout(0.1)

            try:
                data, addr = self.udp_socket.recvfrom(4096)
            except socket.timeout:
                # Check for disconnect (no packets for 5 seconds)
                if time.time() - self.last_packet_time > 5.0:
                    print("EA Sports F1: No telemetry received for 5 seconds - game disconnected?")
                    return False, -1
                return False, 0

            self.last_packet_time = time.time()

            if len(data) < ctypes.sizeof(F1PacketHeader):
                return False, 0

            # Parse header
            header = F1PacketHeader()
            ctypes.memmove(ctypes.byref(header), data, ctypes.sizeof(F1PacketHeader))

            # Detect new session (reset lap counter)
            if header.m_sessionUID != self.last_session_uid:
                print(f"EA Sports F1: New session detected (UID: {header.m_sessionUID})")
                self.last_session_uid = header.m_sessionUID
                self.last_lap_num = 0

            # Only process Lap Data packets (Packet ID 2)
            if header.m_packetId != 2:
                return False, 0

            # Calculate offset to player's car lap data
            header_size = ctypes.sizeof(F1PacketHeader)
            lap_data_size = ctypes.sizeof(F1LapData)
            player_offset = header_size + (header.m_playerCarIndex * lap_data_size)

            if len(data) < player_offset + lap_data_size:
                return False, 0

            # Parse player's lap data
            lap_data = F1LapData()
            ctypes.memmove(
                ctypes.byref(lap_data),
                data[player_offset:player_offset + lap_data_size],
                lap_data_size
            )

            current_lap = lap_data.m_currentLapNum

            # Detect lap completion: lap number increased
            if self.last_lap_num > 0 and current_lap > self.last_lap_num:
                self.last_lap_num = current_lap

                last_time_ms = lap_data.m_lastLapTimeInMS
                if last_time_ms == 0:
                    return False, 0

                last_time_sec = last_time_ms / 1000.0

                print(f"\n=== EA Sports F1 Lap Completed ===")
                print(f"Lap: {current_lap - 1}")
                print(f"Time: {format_time_ms(last_time_ms)} ({last_time_sec:.3f}s)")
                print(f"Position: P{lap_data.m_carPosition}")
                print(f"Pit Status: {lap_data.m_pitStatus}")
                print(f"Valid: {'Yes' if lap_data.m_currentLapInvalid == 0 else 'No'}")
                print(f"Driver Status: {lap_data.m_driverStatus}")

                # Validate racing context
                if not is_valid_racing_context(lap_data, None, "EA Sports F1"):
                    return False, 0

                # Validate lap time range
                if validate_lap_time(last_time_sec, "EA Sports F1"):
                    print(f"[OK] Valid lap time recorded: {last_time_sec:.3f}s")
                    return True, last_time_sec
                else:
                    print(f"Invalid lap time rejected: {last_time_sec:.3f}s")
                    return False, 0
            else:
                # Update lap number tracking (first packet or no change)
                if self.last_lap_num == 0 and current_lap > 0:
                    self.last_lap_num = current_lap
                    print(f"EA Sports F1: Tracking from lap {current_lap}")
                elif current_lap > self.last_lap_num:
                    self.last_lap_num = current_lap

            return False, 0

        except (OSError, ConnectionError) as e:
            print(f"EA Sports F1 socket error: {e}")
            return False, -1
        except Exception as e:
            print(f"EA Sports F1 error reading telemetry: {e}")
            return False, 0

    def cleanup_socket(self):
        """Safely close the UDP socket"""
        try:
            if hasattr(self, 'udp_socket') and self.udp_socket:
                try:
                    self.udp_socket.close()
                    print("[OK] F1 UDP socket closed")
                except (OSError, Exception) as e:
                    print(f"F1 UDP socket already closed: {e}")
                finally:
                    self.udp_socket = None
        except Exception as e:
            print(f"Error during F1 cleanup: {e}")

    def stop(self):
        """Clean up EA Sports F1 UDP socket"""
        print(f"\n=== Stopping {self.game_name} Monitor ===")
        self.running = False
        self.cleanup_socket()
        self.wait()

class State:
    ir_connected = False

class SurveyQuestionsDialog(QDialog):
    """Editor for the lead-gen survey questions shown on the registration page."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lead Gen Survey Questions")
        self.resize(760, 480)
        self.result_config = None
        self.setStyleSheet("""
            QDialog { background-color: #252526; }
            QLabel { color: #9d9d9d; font-size: 12px; }
            QPushButton {
                background-color: #3e3e42; color: #e0e0e0; border: none;
                border-radius: 4px; padding: 8px 14px; font-size: 12px;
            }
            QPushButton:hover { background-color: #4e4e52; }
            QPushButton#saveBtn {
                background-color: #007acc; color: white; font-weight: bold;
            }
            QPushButton#saveBtn:hover { background-color: #0098ff; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("📋 Survey Questions")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #e0e0e0;")
        layout.addWidget(title)

        help_label = QLabel(
            "Drivers answer these one at a time (as multiple-choice pages) when they sign up on the tablet. "
            "They can't skip a question, and every answer is saved to leads.csv for export.\n"
            "Click a cell to type. Separate answer options with commas — at least 2 per question."
        )
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        example_label = QLabel(
            "Example  —  Question: How much money does your company manage?    "
            "Answer options: 1-5M, 10-25M, 50-100M, 250M+"
        )
        example_label.setWordWrap(True)
        example_label.setStyleSheet("color: #6e6e6e; font-style: italic; font-size: 11px;")
        layout.addWidget(example_label)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Question", "Answer options (comma-separated)"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #1e1e1e; color: #e0e0e0;
                border: 1px solid #3e3e42; font-size: 12px;
            }
            QTableWidget::item { padding: 4px; }
            QTableWidget::item:selected { background-color: #094771; }
            QHeaderView::section {
                background-color: #2d2d30; color: #9d9d9d;
                border: none; padding: 6px; font-size: 11px; font-weight: bold;
            }
        """)
        layout.addWidget(self.table)
        for q in config.get('questions', []):
            self._add_row(q['prompt'], ", ".join(q['options']))

        saved_note = QLabel(
            "💾 Saved questions are remembered permanently — they load automatically at your next event."
        )
        saved_note.setStyleSheet("color: #4ec9b0; font-size: 11px;")
        layout.addWidget(saved_note)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("➕ Add Question")
        add_btn.clicked.connect(self.on_add_row)
        remove_btn = QPushButton("➖ Remove Selected")
        remove_btn.clicked.connect(self.on_remove_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("💾 Save Questions")
        save_btn.setObjectName("saveBtn")
        save_btn.clicked.connect(self.on_save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def on_add_row(self):
        self._add_row("", "")
        # Put the cursor straight into the new question cell so typing can start
        row = self.table.rowCount() - 1
        self.table.setCurrentCell(row, 0)
        self.table.editItem(self.table.item(row, 0))

    def _add_row(self, prompt, options_text):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(prompt))
        self.table.setItem(row, 1, QTableWidgetItem(options_text))

    def on_remove_row(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def on_save(self):
        questions = []
        for row in range(self.table.rowCount()):
            prompt_item = self.table.item(row, 0)
            options_item = self.table.item(row, 1)
            prompt = prompt_item.text().strip() if prompt_item else ''
            options_text = options_item.text().strip() if options_item else ''
            if not prompt and not options_text:
                continue
            options = [o.strip() for o in options_text.split(',') if o.strip()]
            if not prompt or len(options) < 2:
                QMessageBox.warning(self, "Invalid Question",
                                    f"Row {row + 1}: needs a question and at least 2 comma-separated answer options.")
                return
            questions.append({'prompt': prompt, 'options': options})
        self.result_config = {'questions': questions}
        self.accept()

class SectorFlashOverlay(QWidget):
    """Brief pass/fail banner shown at the top of the screen when a custom
    sector is judged. Never takes focus so game input is not interrupted."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def show_result(self, sector_number, sector_time, target_time, passed):
        delta = sector_time - target_time
        time_text = format_time_ms(int(sector_time * 1000))
        if passed:
            text = f"SECTOR {sector_number}  ✔  {time_text}  ({delta:+.3f}s)"
            color = "#16a34a"
        else:
            text = f"SECTOR {sector_number}  ✘  {time_text}  ({delta:+.3f}s)"
            color = "#dc2626"
        self.label.setText(text)
        self.label.setStyleSheet(f"""
            QLabel {{
                background-color: {color};
                color: white;
                font-size: 42px;
                font-weight: bold;
                padding: 18px 40px;
                border-radius: 12px;
            }}
        """)
        self.adjustSize()
        screen = QApplication.primaryScreen().geometry()
        self.move(screen.x() + (screen.width() - self.width()) // 2, screen.y() + 60)
        self.show()
        self._hide_timer.start(2500)


try:
    import websocket as obs_websocket_lib  # websocket-client, for OBS control
    OBS_WEBSOCKET_AVAILABLE = True
except ImportError:
    OBS_WEBSOCKET_AVAILABLE = False


class BroadcastDirector(threading.Thread):
    """Keeps the 4th-screen OBS broadcast in sync with the simulator state.

    Consumes cues from the Qt side through a queue and drives OBS Studio's
    built-in WebSocket server (v5 protocol) on localhost:

      'live'    -> TV camera scene (driver is in the cockpit)
      'waiting' -> in 'waiting' mode: branded Waiting scene
                   in 'replay' mode: save OBS's replay buffer and loop the
                   clip on the Replay scene until the next 'live' cue

    Connection details are read from OBS's own obs-websocket config file
    (provisioned by broadcast/setup_broadcast.ps1). Everything fails soft:
    no OBS, no plugin, no config -> cues are dropped with a log line and
    the simulator is never blocked or slowed.
    """

    SCENE_LIVE = 'AC Broadcast'
    SCENE_WAITING = 'Waiting'
    SCENE_REPLAY = 'Replay'
    REPLAY_INPUT = 'Replay Media'

    def __init__(self, mode_getter):
        super().__init__(daemon=True)
        self._cues = queue_module.Queue()
        self._mode_getter = mode_getter  # returns 'off' | 'waiting' | 'replay'
        self._ws = None
        self._req_id = 0
        self._last_cue = None
        self._last_replay_path = None
        self._warned_no_obs = False

    def cue(self, name):
        """Queue a scene cue; call from any thread. No-op when mode is off."""
        try:
            if self._mode_getter() == 'off' or not OBS_WEBSOCKET_AVAILABLE:
                return
            if name == self._last_cue:
                return
            self._last_cue = name
            self._cues.put(name)
        except Exception:
            pass

    def run(self):
        while True:
            cue = self._cues.get()
            try:
                self._handle(cue)
                self._warned_no_obs = False
            except Exception as e:
                self._disconnect()
                # Allow the same cue to be retried once OBS comes back
                self._last_cue = None
                if not self._warned_no_obs:
                    print(f"[Broadcast] OBS not reachable ({e}) - 4th screen cue '{cue}' dropped")
                    self._warned_no_obs = True

    # --- OBS WebSocket v5 plumbing -------------------------------------------

    def _config(self):
        cfg_path = Path(os.environ.get('APPDATA', '')) / 'obs-studio' / 'plugin_config' / 'obs-websocket' / 'config.json'
        with open(cfg_path, 'r') as f:
            cfg = json.load(f)
        return int(cfg.get('server_port', 4455)), cfg.get('server_password', '')

    def _disconnect(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _connect(self):
        if self._ws is not None:
            return
        port, password = self._config()
        ws = obs_websocket_lib.create_connection(f"ws://127.0.0.1:{port}", timeout=4)
        hello = json.loads(ws.recv())
        identify = {'op': 1, 'd': {'rpcVersion': 1, 'eventSubscriptions': 0}}
        auth = hello.get('d', {}).get('authentication')
        if auth:
            import base64, hashlib
            secret = base64.b64encode(hashlib.sha256((password + auth['salt']).encode()).digest())
            token = base64.b64encode(hashlib.sha256(secret + auth['challenge'].encode()).digest()).decode()
            identify['d']['authentication'] = token
        ws.send(json.dumps(identify))
        identified = json.loads(ws.recv())
        if identified.get('op') != 2:
            ws.close()
            raise RuntimeError(f"OBS identify failed: {identified}")
        self._ws = ws

    def _request(self, request_type, request_data=None):
        """Send one request; returns responseData dict on success, None on
        a request-level failure (e.g. replay buffer not active)."""
        self._connect()
        self._req_id += 1
        req_id = f"lts-{self._req_id}"
        self._ws.send(json.dumps({'op': 6, 'd': {
            'requestType': request_type,
            'requestId': req_id,
            'requestData': request_data or {},
        }}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get('op') == 7 and msg['d'].get('requestId') == req_id:
                if msg['d'].get('requestStatus', {}).get('result'):
                    return msg['d'].get('responseData', {})
                return None

    # --- Cue handling ---------------------------------------------------------

    def _switch(self, scene):
        self._request('SetCurrentProgramScene', {'sceneName': scene})
        print(f"[Broadcast] 4th screen -> {scene}")

    def _handle(self, cue):
        mode = self._mode_getter()
        if mode == 'off':
            return
        if cue == 'live':
            self._switch(self.SCENE_LIVE)
            if mode == 'replay':
                # Keep the buffer rolling so the fail moment is captured
                self._request('StartReplayBuffer')
            return
        if cue == 'waiting':
            if mode == 'replay' and self._cue_replay():
                return
            self._switch(self.SCENE_WAITING)

    def _cue_replay(self):
        """Save the replay buffer and put it on the Replay scene, looping.
        Returns False to fall back to the Waiting scene."""
        # Save while the program scene is still the live camera
        if self._request('SaveReplayBuffer') is None:
            self._request('StartReplayBuffer')  # arm it for the next run
            return False
        path = None
        for _ in range(12):
            time.sleep(0.4)
            resp = self._request('GetLastReplayBufferReplay')
            candidate = (resp or {}).get('savedReplayPath')
            if candidate and candidate != self._last_replay_path:
                path = candidate
                break
        if not path:
            return False
        self._last_replay_path = path
        if self._request('SetInputSettings', {
            'inputName': self.REPLAY_INPUT,
            'inputSettings': {'local_file': path, 'looping': True},
            'overlay': True,
        }) is None:
            return False
        self._switch(self.SCENE_REPLAY)
        return True


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
        "EA Sports F1": F1Monitor,
    }
    
    def __init__(self):
        super().__init__()
        self.current_monitor = None
        self.local_ip = get_local_ip()
        self.web_port = select_web_interface_port()
        self.failed_lap_times = []  # Store failed lap times for retry
        self.retry_timer = None
        self.current_server_url = f"http://{self.local_ip}:5000"  # Store current working URL
        self.timer_end_timestamp = 0  # Initialize driver swap timer end timestamp
        # Lap-based rotation state
        self.lap_rotation_active = False
        self.lap_rotation_target = 0
        self.lap_rotation_count = 0
        self.lap_count_started = False
        self.sector_config = load_sector_config()
        self.lead_questions = load_lead_questions()
        self.last_survey_answers = {}

        # 4th-screen broadcast director (OBS scene switching): 'off',
        # 'waiting' (waiting scene between drivers) or 'replay' (loop the
        # last run's replay until the next driver is in the cockpit)
        self.broadcast_mode = 'waiting'
        self.broadcast_director = BroadcastDirector(lambda: getattr(self, 'broadcast_mode', 'off'))
        self.broadcast_director.start()

        self.setup_ui()
        
        # Start with no monitor active
        self.current_monitor = None
        
        # Configure Windows Firewall to allow connections on the selected web port
        configure_firewall(self.web_port)

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

        # Load saved settings (video paths, background, etc.)
        self._load_saved_settings()

        # Show waiting overlay on startup
        self.show_waiting_screen()

    def _load_saved_settings(self):
        """Load and apply saved application settings"""
        settings = load_app_settings()

        # Restore pre-session video path
        if settings.get('pre_session_video_path'):
            path = settings['pre_session_video_path']
            if os.path.exists(path):
                self.video_path_input.setText(path)
                self.pre_session_video_path = path

        # Restore waiting screen media path
        if settings.get('waiting_media_path'):
            path = settings['waiting_media_path']
            if os.path.exists(path):
                self.waiting_media_path.setText(path)

        # Restore 4th-screen broadcast mode
        mode = settings.get('broadcast_mode', 'waiting')
        if mode not in ('off', 'waiting', 'replay'):
            mode = 'waiting'
        self.broadcast_mode = mode
        if hasattr(self, 'broadcast_mode_combo'):
            self.broadcast_mode_combo.setCurrentIndex(['off', 'waiting', 'replay'].index(mode))

        # Restore background image path
        if settings.get('background_image_path'):
            path = settings['background_image_path']
            if os.path.basename(path) == 'iPad Events Background (Sim Coaches Branded).png':
                self.background_image_path.clear()
            elif os.path.exists(path):
                self.background_image_path.setText(path)

        # Restore pre-session video toggle state
        if 'pre_session_video_required' in settings:
            self.video_required_toggle.setChecked(settings['pre_session_video_required'])
            self.pre_session_video_required = settings['pre_session_video_required']

        # Restore Lead Gen contact requirement
        if 'lead_gen_enabled' in settings and hasattr(self, 'email_field_enabled'):
            self.email_field_enabled.setChecked(bool(settings['lead_gen_enabled']))

        # Restore rotation mode
        if settings.get('rotation_mode'):
            index = self.rotation_mode_selector.findText(settings['rotation_mode'])
            if index >= 0:
                self.rotation_mode_selector.setCurrentIndex(index)

        # Restore rotation time
        if settings.get('rotation_time') and hasattr(self, 'time_target_input'):
            self.time_target_input.setText(str(settings['rotation_time']))

        # Restore rotation laps
        if settings.get('rotation_laps') and hasattr(self, 'lap_target_input'):
            self.lap_target_input.setText(str(settings['rotation_laps']))

        # Restore Lead Gen toggle
        if 'lead_gen_enabled' in settings:
            self.email_field_enabled.setChecked(bool(settings['lead_gen_enabled']))

    def setup_ui(self):
        # Main window settings
        self.setWindowTitle("Lap Time Sender - Racing Leaderboard Interface")
        self.setMinimumSize(940, 720)  # Wider but shorter - compact multi-column layout
        self.resize(980, 760)  # Optimized for modern displays
        self.setWindowIcon(QIcon(os.path.join(get_resources_dir(), 'icon.png')))
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
        self.url_info_label = QLabel(f"📱 Mobile Interface: http://{get_local_ip()}:{self.web_port}")
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
        qr_code = self.generate_qr_code(f"http://{get_local_ip()}:{self.web_port}")
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

        # Custom Sectors section (Assetto Corsa sector challenge)
        sector_section = QWidget()
        sector_section.setObjectName("sectorSection")
        sector_section.setStyleSheet("""
            #sectorSection {
                background-color: #2d2d30;
                border-radius: 6px;
                border: 1px solid #3e3e42;
            }
        """)
        sector_layout = QVBoxLayout(sector_section)
        sector_layout.setContentsMargins(10, 6, 10, 6)
        sector_layout.setSpacing(4)

        sector_header_row = QHBoxLayout()
        sector_title = QLabel("🏁 Custom Sectors (Assetto Corsa)")
        sector_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #e0e0e0;")
        sector_header_row.addWidget(sector_title)
        sector_header_row.addStretch()
        self.sector_challenge_enabled = QCheckBox("Enable Sector Challenge")
        self.sector_challenge_enabled.setStyleSheet("font-size: 11px; color: #e0e0e0;")
        self.sector_challenge_enabled.setChecked(bool(self.sector_config.get('enabled')))
        self.sector_challenge_enabled.toggled.connect(self.on_sector_challenge_toggled)
        sector_header_row.addWidget(self.sector_challenge_enabled)
        sector_layout.addLayout(sector_header_row)

        sector_help = QLabel(
            "Each row is one sector: where it STARTS (% of the track) and how long the driver has to "
            "get through it. A sector runs until the next sector's start; the last sector always ends at "
            "the finish line, so no 100% row is needed. Target times are plain seconds (30, 90, 145) or "
            "M:SS.mmm (1:30 or 1:30.500) - both mean the same thing. Miss a target and the run ends: the "
            "car teleports back to start/finish (LeaderboardSector CSP app; Ctrl+O fallback without it) "
            "and the waiting screen comes up for the next driver."
        )
        sector_help.setWordWrap(True)
        sector_help.setStyleSheet("font-size: 10px; color: #9d9d9d;")
        sector_layout.addWidget(sector_help)

        self.sector_table = QTableWidget(0, 3)
        self.sector_table.setHorizontalHeaderLabels(["Sector Start %", "Target Time", "Covers"])
        self.sector_table.horizontalHeader().setStretchLastSection(True)
        self.sector_table.verticalHeader().setDefaultSectionSize(24)
        self.sector_table.setMaximumHeight(200)
        self._sector_table_updating = False
        self.sector_table.itemChanged.connect(self._on_sector_cell_changed)
        self.sector_table.setStyleSheet("""
            QTableWidget {
                background-color: #1e1e1e;
                color: #e0e0e0;
                border: 1px solid #3e3e42;
                font-size: 11px;
            }
            QHeaderView::section {
                background-color: #2d2d30;
                color: #9d9d9d;
                border: none;
                padding: 3px;
                font-size: 10px;
            }
        """)
        sector_layout.addWidget(self.sector_table)

        sector_btn_row = QHBoxLayout()
        add_sector_btn = QPushButton("➕ Add Sector")
        add_sector_btn.clicked.connect(self.on_add_sector_row)
        remove_sector_btn = QPushButton("➖ Remove Selected")
        remove_sector_btn.clicked.connect(self.on_remove_sector_row)
        save_sectors_btn = QPushButton("💾 Save Sectors")
        save_sectors_btn.clicked.connect(self.on_save_sectors)
        for btn in (add_sector_btn, remove_sector_btn, save_sectors_btn):
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #3e3e42;
                    color: #e0e0e0;
                    border: none;
                    border-radius: 4px;
                    padding: 4px 10px;
                    font-size: 11px;
                }
                QPushButton:hover { background-color: #4e4e52; }
            """)
            sector_btn_row.addWidget(btn)
        sector_btn_row.addStretch()
        sector_layout.addLayout(sector_btn_row)
        # Pin content to the top: any extra height the card gives this section
        # collects below the buttons instead of spreading gaps between rows
        sector_layout.addStretch()

        card_layout.addWidget(sector_section)
        self._populate_sector_table()

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
        
        # Row 1: Lead Gen toggle (compact)
        email_label = QLabel("Lead Gen:")
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
        email_label.setToolTip("Require drivers to enter email and phone before racing")
        grid_layout.addWidget(email_label, 1, 0)
        
        self.email_field_enabled = QPushButton("Off")
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
        self.email_field_enabled.toggled.connect(self._on_lead_gen_toggled)
        self.email_field_enabled.setText("Off")

        # Lead Gen toggle + survey questions editor share the grid cell
        self.survey_questions_btn = QPushButton("📋 Questions")
        self.survey_questions_btn.setToolTip("Edit the survey questions shown on the registration page (saved to leads.csv)")
        self.survey_questions_btn.setStyleSheet("""
            QPushButton {
                background-color: #37373d;
                color: #cccccc;
                border: 1px solid #3e3e42;
                padding: 4px 6px;
                border-radius: 4px;
                font-weight: 600;
                font-size: 10px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
            QPushButton:hover {
                border: 1px solid #007acc;
            }
        """)
        self.survey_questions_btn.clicked.connect(self.on_edit_survey_questions)
        lead_gen_cell = QWidget()
        lead_gen_cell_layout = QHBoxLayout(lead_gen_cell)
        lead_gen_cell_layout.setContentsMargins(0, 0, 0, 0)
        lead_gen_cell_layout.setSpacing(4)
        lead_gen_cell_layout.addWidget(self.email_field_enabled)
        lead_gen_cell_layout.addWidget(self.survey_questions_btn)
        grid_layout.addWidget(lead_gen_cell, 1, 1)
        
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

        # Row 6: 4th-screen broadcast mode (Advanced) - drives OBS scenes
        broadcast_mode_label = QLabel("4th Screen:")
        broadcast_mode_label.setMinimumWidth(80)
        broadcast_mode_label.setMaximumWidth(100)
        broadcast_mode_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        broadcast_mode_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                color: #cccccc;
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        broadcast_mode_label.setToolTip("What the OBS broadcast screen shows between drivers")
        grid_layout.addWidget(broadcast_mode_label, 6, 0)

        self.broadcast_mode_combo = QComboBox()
        self.broadcast_mode_combo.addItems([
            "Off (always live camera)",
            "Waiting screen between drivers",
            "Replay of last run between drivers",
        ])
        self.broadcast_mode_combo.setStyleSheet("""
            QComboBox {
                background-color: #1e1e1e;
                border: 1px solid #3e3e42;
                color: #cccccc;
                font-size: 10px;
                font-family: 'Segoe UI', 'Arial', sans-serif;
                padding: 4px 8px;
            }
        """)
        self.broadcast_mode_combo.setToolTip(
            "Requires the OBS broadcast screen setup (see broadcast folder). "
            "While driving, the 4th screen always shows the live TV cameras."
        )
        self.broadcast_mode_combo.setCurrentIndex(1)
        self.broadcast_mode_combo.currentIndexChanged.connect(self._on_broadcast_mode_changed)
        grid_layout.addWidget(self.broadcast_mode_combo, 6, 1, 1, 2)

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
        self.last_driver_phone = ""
        self.current_session_id = ""
        # Video playback state
        self.pre_session_video_required = False
        self.pre_session_video_path = ""
        self.media_player = None
        self.video_widget = None
        
        # Overlay system
        self.waiting_overlay = WaitingOverlay()
        self.sector_flash = SectorFlashOverlay()
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
            (broadcast_mode_label, self.broadcast_mode_combo),
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

                    # Wire up the custom sector challenge (Assetto Corsa only)
                    if hasattr(self.current_monitor, 'set_challenge_config'):
                        self.current_monitor.sector_result.connect(self.on_sector_result)
                        self.current_monitor.challenge_failed.connect(self.on_challenge_failed)
                        self.current_monitor.set_challenge_config(self.sector_config)

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
        # Automatically send the lap time with the driver who completed it.
        # In sector challenge mode a completed lap means 100% distance.
        challenge_on = bool(self.sector_config.get('enabled')) and \
            self.current_monitor is not None and hasattr(self.current_monitor, 'set_challenge_config')
        self.send_lap_time(lap_time, from_game=True, override_driver=driver_name,
                           distance_pct=100.0 if challenge_on else None)
        
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
            
            # Retry against every configured receiver (comma-separated), same
            # as send_lap_time; success on any receiver clears the lap.
            server_url_str = self.current_server_url or self.server_url.text().strip()
            server_urls = [u.strip() for u in str(server_url_str).split(',') if u.strip()]

            successful_sends = []
            for lap_data in self.failed_lap_times:
                sent = False
                for server_url in server_urls:
                    try:
                        headers = {'Content-Type': 'application/json'}
                        response = requests.post(
                            server_url,
                            json=lap_data,
                            headers=headers,
                            timeout=5
                        )
                        if response.status_code == 200:
                            sent = True
                    except:
                        pass
                if sent:
                    successful_sends.append(lap_data)
                    print(f"Successfully resent lap time: {lap_data['lap_time']:.3f}s")
                else:
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

    def send_lap_time(self, lap_time=None, from_game=True, override_driver="",
                      distance_pct=None, challenge_fail=False):
        """Send the lap time to the server

        Args:
            lap_time: The lap time in seconds
            from_game: Whether this came from game telemetry
            override_driver: If provided, use this driver name instead of current
            distance_pct: Distance traveled (0-100) for sector challenge runs
            challenge_fail: True for a failed challenge run (elapsed time, not a
                full lap, so the normal lap time range check doesn't apply)
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
            
            # Additional validation for manual entries (failed challenge runs
            # carry an elapsed time that can legitimately fall outside lap range)
            if lap_time is not None and not challenge_fail and not validate_lap_time(lap_time, "Manual Entry"):
                print(f"FAILED: Invalid lap time {lap_time}")
                self.show_error(f"Invalid lap time: {lap_time:.3f}s. Must be between 5s and 60 minutes.")
                return False
                
            # Lead Gen contact data is required for telemetry laps. Manual entries
            # can still be used by staff for corrections without a sign-in record.
            lead_gen_enabled = self.email_field_enabled.isChecked() if hasattr(self, 'email_field_enabled') else False
            has_email = bool(getattr(self, 'last_driver_email', ''))
            has_phone = bool(getattr(self, 'last_driver_phone', ''))
            print(f"Lead Gen required: {lead_gen_enabled}, Has email: {has_email}, Has phone: {has_phone}")
            if lead_gen_enabled and from_game and (not has_email or not has_phone):
                print("FAILED: Lead Gen contact info required but not provided (from_game=True)")
                self.show_error("Lead Gen requires email and phone. Please register via the web interface.")
                return False

            # Prepare data with strict type conversion
            data = {
                "simulator_id": int(self.simulator_id.currentText()),  # Get selected simulator ID
                "driver_name": str(driver_name).strip(),  # Use resolved driver name (may be override)
                "lap_time": float(round(lap_time, 3)),  # Round to 3 decimal places
            }

            # Include session ID for partner integrations. Older receivers ignore it.
            if not getattr(self, 'current_session_id', ''):
                self.current_session_id = str(uuid.uuid4())
            data["session_id"] = self.current_session_id
            
            # Include formatted time if available
            if formatted_time:
                data["formatted_time"] = formatted_time
                
            # Include driver email if available
            if hasattr(self, 'last_driver_email') and self.last_driver_email:
                data["email"] = self.last_driver_email  # Changed from "driver_email" to "email"
                print(f"Including driver email: {self.last_driver_email}")

            # Include phone if a future registration/queue flow provides it.
            if hasattr(self, 'last_driver_phone') and self.last_driver_phone:
                data["phone"] = self.last_driver_phone

            # Sector challenge: distance traveled ranks ahead of time on the
            # receiver when its distance ranking mode is enabled.
            if distance_pct is not None:
                data["distance_pct"] = float(round(max(0.0, min(distance_pct, 100.0)), 2))

            # Lead-gen survey answers (question prompt -> chosen option) so
            # partner receivers can collect the full registration record.
            if getattr(self, 'last_survey_answers', None):
                data["survey_answers"] = dict(self.last_survey_answers)
                
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
            self.mdns_hostname = mdns_service.start(sim_id, self.web_port)
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
            ip_url = f"http://{self.local_ip}:{self.web_port}"
            if self.mdns_hostname:
                # Show both mDNS and IP address
                self.url_info_label.setText(
                    f"📱 Mobile Interface:\n"
                    f"   {self.mdns_hostname}:{self.web_port}  (easy)\n"
                    f"   {self.local_ip}:{self.web_port}  (IP)"
                )
            else:
                self.url_info_label.setText(f"📱 Mobile Interface: {ip_url}")

    def _update_qr_code(self):
        """Update the QR code with current IP."""
        if hasattr(self, 'qr_label'):
            qr_code = self.generate_qr_code(f"http://{self.local_ip}:{self.web_port}")
            if qr_code:
                self.qr_label.setPixmap(qr_code)

    def _on_simulator_id_changed(self, new_id):
        """Called when the simulator ID dropdown changes."""
        try:
            sim_id = int(new_id)
            # Update mDNS service with new simulator ID
            self.mdns_hostname = mdns_service.update_simulator_id(sim_id, self.web_port)
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
        self.current_session_id = str(uuid.uuid4())
        
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
        self.broadcast_director.cue('waiting')
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
        self.broadcast_director.cue('live')
        self.waiting_overlay.hide()
        self.session_active = True
        # Update button text
        if hasattr(self, 'toggle_waiting_screen_btn'):
            self.toggle_waiting_screen_btn.setText("Show Waiting Screen")
        
    def end_session(self):
        """End the current session and return to waiting screen"""
        print("[END SESSION] end_session() called - showing session complete screen")

        # 4th screen: waiting scene, or save + loop the run's replay.
        # Cued here (not only in show_waiting_screen) because the replay
        # buffer must be saved close to the moment the run ended.
        self.broadcast_director.cue('waiting')

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

    # --- Custom sector challenge (Assetto Corsa) ---

    def _populate_sector_table(self):
        sectors = self.sector_config.get('sectors', [])
        self._sector_table_updating = True
        try:
            self.sector_table.setRowCount(len(sectors))
            for row, s in enumerate(sectors):
                self.sector_table.setItem(row, 0, QTableWidgetItem(f"{s['start_pct']:g}"))
                self.sector_table.setItem(row, 1, QTableWidgetItem(format_time_ms(int(s['target_time'] * 1000))))
        finally:
            self._sector_table_updating = False
        self._refresh_sector_ranges()

    def _refresh_sector_ranges(self):
        """Fill the read-only 'Covers' column: each sector runs from its own
        start % to the next sector's start %, the last one to the finish."""
        starts = []
        for row in range(self.sector_table.rowCount()):
            item = self.sector_table.item(row, 0)
            try:
                starts.append(float(item.text().replace('%', '').strip()) if item else None)
            except ValueError:
                starts.append(None)
        self._sector_table_updating = True
        try:
            for row, start in enumerate(starts):
                if start is None:
                    text = ""
                else:
                    later = [s for s in starts if s is not None and s > start]
                    text = (f"{start:g}% → {min(later):g}%" if later
                            else f"{start:g}% → Finish")
                cell = QTableWidgetItem(text)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled)  # read-only
                cell.setForeground(QColor('#6e9e6e'))
                self.sector_table.setItem(row, 2, cell)
        finally:
            self._sector_table_updating = False

    def _on_sector_cell_changed(self, item):
        if self._sector_table_updating:
            return
        if item.column() == 0:
            self._refresh_sector_ranges()
        if item.column() in (0, 1):
            self._autosave_sectors()

    def _autosave_sectors(self):
        """Persist the sector table whenever an edit leaves it fully valid, so
        percentages and targets survive a shutdown even if nobody clicks Save.

        Silent by design: an incomplete or invalid table simply isn't saved
        yet (the previous good config stays on disk), and the Save button
        still gives explicit validation feedback on demand. The table is not
        repopulated here, so typing is never disturbed."""
        sectors = []
        for row in range(self.sector_table.rowCount()):
            pct_item = self.sector_table.item(row, 0)
            time_item = self.sector_table.item(row, 1)
            pct_text = pct_item.text().strip() if pct_item else ''
            time_text = time_item.text().strip() if time_item else ''
            if not pct_text and not time_text:
                continue
            try:
                start_pct = float(pct_text.replace('%', ''))
            except ValueError:
                return
            target_time = parse_time_to_seconds(time_text)
            if target_time is None:
                return
            sectors.append({'start_pct': start_pct, 'target_time': target_time})
        config = normalize_sector_config({
            'enabled': self.sector_challenge_enabled.isChecked(),
            'sectors': sectors,
            'teleport_spawn': self.sector_config.get('teleport_spawn', 'HOTLAP_START'),
        })
        if not config:
            return
        self.sector_config = config
        save_sector_config(config)
        self._apply_sector_config_to_monitor()

    def on_add_sector_row(self):
        row = self.sector_table.rowCount()
        self.sector_table.insertRow(row)
        self._sector_table_updating = True
        try:
            self.sector_table.setItem(row, 0, QTableWidgetItem(""))
            self.sector_table.setItem(row, 1, QTableWidgetItem(""))
        finally:
            self._sector_table_updating = False
        self._refresh_sector_ranges()

    def on_remove_sector_row(self):
        rows = sorted({index.row() for index in self.sector_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.sector_table.removeRow(row)
        self._refresh_sector_ranges()
        self._autosave_sectors()

    def on_sector_challenge_toggled(self, checked):
        self.sector_config['enabled'] = bool(checked)
        save_sector_config(self.sector_config)
        self._apply_sector_config_to_monitor()

    def on_save_sectors(self):
        sectors = []
        for row in range(self.sector_table.rowCount()):
            pct_item = self.sector_table.item(row, 0)
            time_item = self.sector_table.item(row, 1)
            pct_text = pct_item.text().strip() if pct_item else ''
            time_text = time_item.text().strip() if time_item else ''
            if not pct_text and not time_text:
                continue
            try:
                start_pct = float(pct_text.replace('%', ''))
            except ValueError:
                self.show_error(f"Sector row {row + 1}: invalid start % '{pct_text}'")
                return
            if start_pct >= 100:
                self.show_error(
                    f"Sector row {row + 1}: start % must be below 100. The last sector "
                    "automatically runs to the finish line - no 100% row is needed."
                )
                return
            target_time = parse_time_to_seconds(time_text)
            if target_time is None:
                self.show_error(f"Sector row {row + 1}: invalid target time '{time_text}' (use M:SS.mmm or seconds)")
                return
            sectors.append({'start_pct': start_pct, 'target_time': target_time})

        config = normalize_sector_config({
            'enabled': self.sector_challenge_enabled.isChecked(),
            'sectors': sectors,
            'teleport_spawn': self.sector_config.get('teleport_spawn', 'HOTLAP_START'),
        })
        if not config:
            self.show_error("Invalid sectors: need at least one, start % between 0 and 99.9, no duplicate start %, times above 0.")
            return

        self.sector_config = config
        save_sector_config(config)
        self._populate_sector_table()
        self._apply_sector_config_to_monitor()
        self.show_success(f"✅ Saved {len(config['sectors'])} custom sectors")

    def _apply_sector_config_to_monitor(self):
        if self.current_monitor and hasattr(self.current_monitor, 'set_challenge_config'):
            self.current_monitor.set_challenge_config(self.sector_config)

    def on_sector_result(self, sector_index, sector_time, target_time, passed):
        self.sector_flash.show_result(sector_index + 1, sector_time, target_time, passed)

    def on_challenge_failed(self, sector_index, distance_pct, elapsed_s):
        """A driver missed a sector target: score the partial run, put the car
        back at start/finish, and bring up the waiting screen for the next
        driver. Prefers the CSP companion app (clean in-game teleport, no
        keyboard focus games); falls back to AC's Ctrl+O session restart."""
        print(f"[Sectors] Run over in sector {sector_index + 1}: {distance_pct:.2f}% of track in {elapsed_s:.3f}s")

        driver = self.driver_name.text().strip() if hasattr(self.driver_name, 'text') else ''
        if driver:
            self.send_lap_time(elapsed_s, from_game=True, override_driver=driver,
                               distance_pct=distance_pct, challenge_fail=True)

        if ac_app_alive():
            spawn = self.sector_config.get('teleport_spawn', 'HOTLAP_START')
            cmd_id = ac_app_issue_teleport(spawn)
            print(f"[Sectors] Teleport command {cmd_id} sent to CSP app (spawn set {spawn})")
            QTimer.singleShot(2500, lambda: self._verify_ac_teleport(cmd_id))
        else:
            # No CSP app polling: restart AC via hotkey (needs keyboard focus)
            QTimer.singleShot(600, self.restart_ac_session)
        QTimer.singleShot(1500, self.end_session)

    def _verify_ac_teleport(self, cmd_id):
        """Confirm the CSP app executed the teleport; fall back to Ctrl+O if not."""
        if ac_app_command_done(cmd_id):
            print("[Sectors] CSP teleport confirmed")
            if self.current_monitor and hasattr(self.current_monitor, 'request_challenge_reset'):
                self.current_monitor.request_challenge_reset()
        else:
            print("[Sectors] No CSP teleport ack - falling back to Ctrl+O restart")
            ac_app_clear_command(cmd_id)
            self.restart_ac_session()

    def restart_ac_session(self):
        """Send Assetto Corsa its default 'Restart session' hotkey (Ctrl+O) so
        the car teleports back to the start for the next driver."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "Assetto Corsa")
            if not hwnd:
                matches = []

                @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                def enum_proc(handle, _):
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(handle, buf, 256)
                    if "assetto corsa" in buf.value.lower():
                        matches.append(handle)
                    return True

                user32.EnumWindows(enum_proc, None)
                hwnd = matches[0] if matches else None

            if not hwnd:
                print("[Sectors] Assetto Corsa window not found - cannot auto-restart")
                return

            if user32.GetForegroundWindow() != hwnd:
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.15)

            VK_CONTROL, VK_O = 0x11, 0x4F
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            user32.keybd_event(VK_O, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_O, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
            print("[Sectors] Sent Ctrl+O restart to Assetto Corsa")
        except Exception as e:
            print(f"[Sectors] Could not restart AC session: {e}")

    def _on_broadcast_mode_changed(self, index):
        """Persist the 4th-screen broadcast mode immediately (like Lead Gen)."""
        mode = ['off', 'waiting', 'replay'][index] if 0 <= index <= 2 else 'waiting'
        self.broadcast_mode = mode
        settings = load_app_settings()
        settings['broadcast_mode'] = mode
        save_app_settings(settings)

    def _on_lead_gen_toggled(self, checked):
        """Persist the Lead Gen toggle immediately so the next event starts
        with the same setup (no Apply Settings click needed)."""
        self.email_field_enabled.setText("On" if checked else "Off")
        settings = load_app_settings()
        settings['lead_gen_enabled'] = bool(checked)
        save_app_settings(settings)

    def on_edit_survey_questions(self):
        """Open the lead-gen survey questions editor."""
        dialog = SurveyQuestionsDialog(self.lead_questions, self)
        if dialog.exec() and dialog.result_config is not None:
            self.lead_questions = dialog.result_config
            save_lead_questions(self.lead_questions)
            count = len(self.lead_questions['questions'])
            self.show_success(f"✅ Saved {count} survey question{'s' if count != 1 else ''}")

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
                    'queue_id': queue_id or '',
                    'session_id': getattr(self, 'current_session_id', ''),
                    'email': getattr(self, 'last_driver_email', ''),
                    'phone': getattr(self, 'last_driver_phone', ''),
                    'survey_answers': dict(getattr(self, 'last_survey_answers', {}) or {})
                }
                # current_server_url may hold several comma-separated receivers
                any_ok = False
                for server_url in [u.strip() for u in str(self.current_server_url).split(',') if u.strip()]:
                    try:
                        response = requests.post(
                            f"{server_url}/api/session/started",
                            json=data,
                            timeout=2.0
                        )
                        if response.ok:
                            any_ok = True
                    except Exception as e:
                        print(f"[QUEUE] Could not notify {server_url} of session start: {e}")
                if any_ok:
                    print(f"[QUEUE] Session start reported to receiver(s)")
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
                # current_server_url may hold several comma-separated receivers
                for server_url in [u.strip() for u in str(self.current_server_url).split(',') if u.strip()]:
                    try:
                        response = requests.post(
                            f"{server_url}/api/session/ended",
                            json=data,
                            timeout=2.0
                        )
                        if response.ok:
                            print(f"[QUEUE] Session end reported to {server_url} (duration: {duration}s)")
                    except Exception as e:
                        print(f"[QUEUE] Could not notify {server_url} of session end: {e}")
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
        """Update driver-related settings (timer, lead-gen fields, background)"""
        # Process Lead Gen setting
        lead_gen_enabled = self.email_field_enabled.isChecked()
        print(f"Lead Gen updated: {'Enabled' if lead_gen_enabled else 'Disabled'}")
        
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
        if lead_gen_enabled:
            details.append("Lead Gen required")
        if timer_enabled:
            details.append(f"Timer: {timer_minutes}min")
        if self.background_image_path.text():
            details.append("Custom background")
        
        if self.pre_session_video_required:
            details.append("Pre-session video ON")
        if details:
            message += f" ({', '.join(details)})"

        # Save all settings to persist across restarts
        settings = {
            'pre_session_video_path': self.video_path_input.text(),
            'waiting_media_path': self.waiting_media_path.text(),
            'background_image_path': self.background_image_path.text(),
            'pre_session_video_required': self.video_required_toggle.isChecked(),
            'lead_gen_enabled': lead_gen_enabled,
            'rotation_mode': self.rotation_mode_selector.currentText(),
            'rotation_time': self.time_target_input.text() if hasattr(self, 'time_target_input') else '5',
            'rotation_laps': self.lap_target_input.text() if hasattr(self, 'lap_target_input') else '10',
            'lead_gen_enabled': self.email_field_enabled.isChecked()
        }
        save_app_settings(settings)

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

    # Load SMS configuration for queue notifications
    load_sms_config()

    # Start proactive SMS notification background thread
    start_queue_notification_thread()

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
