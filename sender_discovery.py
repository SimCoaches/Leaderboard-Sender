"""
Sender Auto-Discovery Module

Provides automatic discovery and connection management for the Leaderboard Receiver.
Uses UDP broadcast on port 5001 to find the receiver on the local network.

Protocol:
1. Sender broadcasts: "LEADERBOARD_DISCOVER" to UDP port 5001
2. Receiver responds: "LEADERBOARD_SERVER:192.168.0.248:5000"
3. Sender extracts IP:port and connects via HTTP POST
"""

import socket
import time
import threading
import requests


# Discovery constants - must match the Receiver
DISCOVERY_PORT = 5001
DISCOVERY_REQUEST = b"LEADERBOARD_DISCOVER"
DISCOVERY_RESPONSE_PREFIX = "LEADERBOARD_SERVER:"
DISCOVERY_TIMEOUT = 3  # seconds to wait for response


def discover_leaderboard_server():
    """
    Broadcast on the local network to find the Leaderboard Receiver.

    Returns:
        str: Server URL (e.g., "http://192.168.0.248:5000") if found
        None: If no receiver found on network
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(DISCOVERY_TIMEOUT)

    try:
        # Send broadcast to all devices on network
        sock.sendto(DISCOVERY_REQUEST, ('<broadcast>', DISCOVERY_PORT))

        # Wait for receiver to respond
        data, addr = sock.recvfrom(1024)
        response = data.decode()

        # Parse response: "LEADERBOARD_SERVER:192.168.0.248:5000"
        if response.startswith(DISCOVERY_RESPONSE_PREFIX):
            server_info = response.replace(DISCOVERY_RESPONSE_PREFIX, "")
            ip, port = server_info.rsplit(":", 1)
            return f"http://{ip}:{port}"

    except socket.timeout:
        return None  # No receiver responded
    except Exception as e:
        print(f"[DISCOVERY] Error: {e}")
        return None
    finally:
        sock.close()

    return None


class LeaderboardConnection:
    """
    Manages connection to Leaderboard Receiver with:
    - Automatic discovery (no IP entry needed)
    - Auto-reconnect if connection lost
    - Manual fallback (just in case)
    """

    def __init__(self):
        self.server_url = None           # Current active URL
        self.manual_url = None           # Manual backup URL (user-entered)
        self.last_discovery_time = 0     # Track when we last discovered
        self.connected = False
        self._lock = threading.Lock()

        # Callbacks for UI updates (optional)
        self.on_connected = None         # Called when connected: on_connected(url)
        self.on_disconnected = None      # Called when disconnected: on_disconnected()
        self.on_searching = None         # Called when searching: on_searching()

    def set_manual_backup(self, url):
        """
        Set a manual backup URL (the old way, just in case).

        Args:
            url: Full URL, e.g., "http://192.168.0.248:5000"
                 Or just IP:port, e.g., "192.168.0.248:5000"
        """
        if url and not url.startswith("http"):
            url = f"http://{url}"
        self.manual_url = url

    def discover(self):
        """
        Try to find the Leaderboard Receiver on the network.

        Returns:
            bool: True if found, False otherwise
        """
        with self._lock:
            if self.on_searching:
                self.on_searching()

            discovered_url = discover_leaderboard_server()

            if discovered_url:
                self.server_url = discovered_url
                self.connected = True
                self.last_discovery_time = time.time()
                print(f"[DISCOVERY] Found leaderboard at: {discovered_url}")

                if self.on_connected:
                    self.on_connected(discovered_url)
                return True
            else:
                print("[DISCOVERY] No leaderboard found on network")
                return False

    def ensure_connected(self):
        """
        Make sure we have a valid server URL.
        Tries auto-discovery first, then falls back to manual.

        Returns:
            bool: True if we have a URL to try, False otherwise
        """
        # If we have a working connection, use it
        if self.server_url and self.connected:
            return True

        # Try auto-discovery
        if self.discover():
            return True

        # Fall back to manual URL if set
        if self.manual_url:
            print(f"[FALLBACK] Using manual URL: {self.manual_url}")
            self.server_url = self.manual_url
            return True

        return False

    def send_lap_time(self, simulator_id, driver_name, lap_time, email="", formatted_time=None):
        """
        Send a lap time to the Leaderboard Receiver.
        Automatically reconnects if connection is lost.

        Args:
            simulator_id: Unique ID for this simulator (e.g., 1)
            driver_name: Driver's name
            lap_time: Lap time in seconds (float)
            email: Optional email address
            formatted_time: Optional pre-formatted time string

        Returns:
            tuple: (success: bool, error_msg: str or None)
        """
        data = {
            "simulator_id": int(simulator_id),
            "driver_name": str(driver_name).strip(),
            "lap_time": float(round(lap_time, 3)),
        }

        if formatted_time:
            data["formatted_time"] = formatted_time

        if email:
            data["email"] = email

        # Try up to 3 times with auto-reconnect
        for attempt in range(3):
            # Make sure we have a connection
            if not self.ensure_connected():
                print(f"[SEND] Attempt {attempt + 1}: No server available")
                time.sleep(2)
                continue

            try:
                response = requests.post(
                    self.server_url,
                    json=data,
                    headers={'Content-Type': 'application/json'},
                    timeout=5
                )

                if response.status_code == 200:
                    self.connected = True
                    print(f"[SEND] Lap time sent successfully: {driver_name} - {lap_time}s")
                    return (True, None)
                else:
                    error_msg = f"Server returned status {response.status_code}"
                    print(f"[SEND] {error_msg}")
                    return (False, error_msg)

            except requests.exceptions.ConnectionError:
                print(f"[SEND] Connection failed, will retry...")
                self._mark_disconnected()

            except requests.exceptions.Timeout:
                print(f"[SEND] Request timed out, will retry...")
                self._mark_disconnected()

            except Exception as e:
                print(f"[SEND] Error: {e}")
                self._mark_disconnected()

            # Wait before retry
            time.sleep(1)

        return (False, "Failed after 3 attempts")

    def _mark_disconnected(self):
        """Mark as disconnected and trigger callback."""
        self.connected = False
        self.server_url = None  # Force re-discovery
        if self.on_disconnected:
            self.on_disconnected()

    def test_connection(self):
        """
        Test if we can reach the server (sends a GET ping).

        Returns:
            bool: True if server responds, False otherwise
        """
        if not self.ensure_connected():
            return False

        try:
            response = requests.get(self.server_url, timeout=3)
            # Accept any response as valid - the server is responding
            self.connected = True
            if self.on_connected:
                self.on_connected(self.server_url)
            return True
        except Exception as e:
            print(f"[TEST] Connection test failed: {e}")
            self._mark_disconnected()

        return False

    def get_status(self):
        """
        Get current connection status as a string for display.

        Returns:
            str: Status message
        """
        if self.connected and self.server_url:
            return f"Connected: {self.server_url}"
        elif self.server_url:
            return f"Connecting to: {self.server_url}"
        else:
            return "Searching for leaderboard..."

    def get_current_url(self):
        """
        Get the current server URL.

        Returns:
            str: Current server URL or None
        """
        return self.server_url


class BackgroundDiscovery(threading.Thread):
    """
    Runs discovery in background, keeps connection alive.
    Automatically re-discovers if connection is lost.
    """

    def __init__(self, connection: LeaderboardConnection):
        super().__init__(daemon=True)
        self.connection = connection
        self.running = True
        self.check_interval = 10  # Check every 10 seconds

    def run(self):
        # Try to discover immediately on start
        self.connection.discover()

        while self.running:
            time.sleep(self.check_interval)

            # If not connected, try to discover
            if not self.connection.connected:
                self.connection.discover()

    def stop(self):
        """Stop the background discovery thread."""
        self.running = False
