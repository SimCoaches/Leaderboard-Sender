"""Read current LAN interfaces and Wi-Fi names without changing networking."""
import ipaddress
import os
import socket
import subprocess
from PyQt6.QtNetwork import QNetworkInterface, QAbstractSocket


def active_interfaces():
    result = []
    for interface in QNetworkInterface.allInterfaces():
        flags = interface.flags()
        if not (flags & QNetworkInterface.InterfaceFlag.IsUp and
                flags & QNetworkInterface.InterfaceFlag.IsRunning):
            continue
        if flags & QNetworkInterface.InterfaceFlag.IsLoopBack:
            continue
        kind = interface.type()
        for entry in interface.addressEntries():
            if entry.ip().protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                continue
            address = entry.ip().toString()
            ip = ipaddress.ip_address(address)
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                continue
            result.append(dict(ip=address, name=interface.humanReadableName(),
                               broadcast=entry.broadcast().toString(),
                               kind='Wi-Fi' if kind == QNetworkInterface.InterfaceType.Wifi else
                                    'Ethernet' if kind == QNetworkInterface.InterfaceType.Ethernet else 'Adapter'))
    return result


def route_ip():
    try:
        # UDP connect chooses a route; it sends no packet and needs no Internet response.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('8.8.8.8', 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def select_interface(interfaces, routed_ip):
    # Physical LAN takes priority over a VPN's default route, which an iPad
    # on the venue Wi-Fi cannot necessarily reach.
    physical = [i for i in interfaces if i['kind'] in ('Wi-Fi', 'Ethernet')]
    candidates = physical or interfaces
    return next((i for i in candidates if i['ip'] == routed_ip),
                candidates[0] if candidates else None)


def current_lan_ip():
    interface = select_interface(active_interfaces(), route_ip())
    return interface['ip'] if interface else '127.0.0.1'


def parse_wifi_names(output):
    """Parse interface/SSID pairs; never confuse a BSSID or saved profile with SSID."""
    names = {}
    current = None
    for line in output.splitlines():
        key, separator, value = line.strip().partition(':')
        if not separator:
            continue
        if key.strip() == 'Name':
            current = value.strip()
        elif key.strip() == 'SSID' and current:
            names[current] = value.strip()
    return names


def wifi_names():
    if os.name != 'nt':
        return {}
    try:
        result = subprocess.run(['netsh', 'wlan', 'show', 'interfaces'],
                                capture_output=True, text=True, errors='replace',
                                timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
        return parse_wifi_names(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired):
        return {}


def network_snapshot():
    interfaces = active_interfaces()
    names = wifi_names() if any(i['kind'] == 'Wi-Fi' for i in interfaces) else {}
    for interface in interfaces:
        interface['ssid'] = names.get(interface['name']) if interface['kind'] == 'Wi-Fi' else None
    selected = select_interface(interfaces, route_ip())
    return dict(interfaces=interfaces, selected=selected,
                ip=selected['ip'] if selected else '127.0.0.1')


def network_signature(snapshot, previous=None):
    """Keep the last known SSID when Windows temporarily cannot read it.

    The visible snapshot still reports an unavailable name. Only change
    detection retains it; a failed netsh call is not a network disconnection.
    """
    previous_names = {(name, ip): ssid for name, ip, ssid in previous[1]} if previous else {}
    return (snapshot['ip'], tuple(sorted(
        (i['name'], i['ip'], i.get('ssid') or previous_names.get((i['name'], i['ip']), ''))
        for i in snapshot['interfaces'])))


def network_changed(previous, current):
    if previous is None or previous[0] != current[0]:
        return True
    before = {(name, ip): ssid for name, ip, ssid in previous[1]}
    after = {(name, ip): ssid for name, ip, ssid in current[1]}
    if before.keys() != after.keys():
        return True
    # Learning a previously unavailable name doesn't mean the PC moved.
    return any(before[key] and after[key] and before[key] != after[key] for key in before)
