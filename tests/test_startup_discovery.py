"""Exercise real Qt widgets in a separate process, without touching the LAN."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class StartupTests(unittest.TestCase):
    def test_automatic_id_widgets_restore_resolve_and_persist(self):
        script = r'''
import tempfile
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
import lap_time_sender as s

app = s.QApplication([])
with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
    stack.enter_context(patch.object(s, 'get_settings_path', return_value=Path(directory) / 'settings.json'))
    s.save_app_settings({'simulator_id': 2})
    for target in [s.threading.Thread, s.QThread, s.QTimer]:
        stack.enter_context(patch.object(target, 'start'))
    stack.enter_context(patch.object(s.QTimer, 'singleShot'))
    stack.enter_context(patch.object(s, 'configure_firewall'))
    stack.enter_context(patch.object(s, 'select_web_interface_port', return_value=5001))
    stack.enter_context(patch.object(s, 'get_local_ip', return_value='192.168.0.20'))
    stack.enter_context(patch.object(s, 'mdns_service'))
    stack.enter_context(patch.object(s.LapTimeSenderWindow, 'show_waiting_screen'))
    stack.enter_context(patch.object(s.LapTimeSenderWindow, 'start_monitor'))
    window = s.LapTimeSenderWindow()
    assert window.simulator_id.currentText() == '2'
    assert window.automatic_id_checkbox.isChecked()
    assert not window.simulator_id.isEnabled()
    window._on_network_simulators_updated([dict(ip='192.168.0.21', device_id='legacy',
        simulator_id=2, simulator_name='Sim 2', reachable=True, id_automatic=False)])
    assert window.simulator_id.currentText() == '1'
    assert s.load_app_settings()['simulator_id'] == 1
    window.session_active = True
    window._on_network_simulators_updated([dict(ip='192.168.0.21', device_id='legacy',
        simulator_id=1, simulator_name='Sim 1', reachable=True, id_automatic=False)])
    assert window.simulator_id.currentText() == '1'
    assert window.get_sim_id_conflicts() == ['192.168.0.21']
    window.session_active = False
    window.automatic_id_checkbox.setChecked(False)
    assert window.simulator_id.isEnabled()
    assert s.load_app_settings()['automatic_simulator_id'] is False
    def snapshot(ip, ssid):
        interface = dict(ip=ip, ssid=ssid, name='Wi-Fi', kind='Wi-Fi')
        return dict(ip=ip, selected=interface, interfaces=[interface])
    with patch.object(window, '_request_mdns_refresh') as mdns, \
            patch.object(window, '_update_qr_code') as qr, \
            patch.object(s.SimulatorDiscovery, 'reset_network') as reset:
        window._apply_network_status(snapshot('192.168.0.19', 'Venue A'))
        assert 'Venue A' in window.network_info_label.text()
        assert '192.168.0.19:5001' in window.url_info_label.text()
        window._apply_network_status(snapshot('192.168.5.20', 'Venue B'))
        assert 'Venue B' in window.network_info_label.text()
        assert '192.168.5.20:5001' in window.url_info_label.text()
        assert window.receiver_discovery.local_ip == '192.168.5.20'
        assert window.discovery_listener.local_ip == '192.168.5.20'
        # Different Wi-Fi can give the PC the same private IP.
        window._apply_network_status(snapshot('192.168.5.20', 'Venue C'))
        assert mdns.call_count == qr.call_count == reset.call_count == 3
        window._apply_network_status(snapshot('192.168.5.20', 'Venue C'))
        assert reset.call_count == 3
        for ssid in [None, 'Venue C', '', 'Venue C']:
            window._apply_network_status(snapshot('192.168.5.20', ssid))
            assert reset.call_count == 3, 'An unavailable Wi-Fi name must not clear discovered simulators'
    window._apply_network_status(dict(ip='127.0.0.1', selected=None, interfaces=[]))
    assert 'No connected' in window.network_info_label.text()
    assert 'unavailable' in window.url_info_label.text()
    assert window.qr_label.pixmap().isNull()
    window.close()
print('Real Qt startup and ID resolution passed')
'''
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, '-'], input=script, encoding='utf-8',
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
