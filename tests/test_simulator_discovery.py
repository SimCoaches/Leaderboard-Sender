import json
import subprocess
import sys
import threading
import time
import types
import tempfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lap_time_sender as sender
from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal, pyqtSlot


class DiscoveryView(QObject):
    network_simulators_updated = pyqtSignal(object)
    refresh_network_simulators_ui = sender.LapTimeSenderWindow.refresh_network_simulators_ui

    def __init__(self):
        super().__init__()
        self._resolve_automatic_id = mock.Mock()
        self.rows = None
        self.updated_on = None
        self.network_simulators_updated.connect(self._on_network_simulators_updated)
        self._network_discovery_in_progress = False

    _on_network_simulators_updated = sender.LapTimeSenderWindow._on_network_simulators_updated

    @pyqtSlot(object)
    def _update_network_simulators_ui(self, rows):
        self.rows = rows
        self.updated_on = threading.get_ident()
        self._network_discovery_in_progress = False


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_listener_recovers_when_busy_discovery_port_becomes_available(self):
        busy, working = mock.MagicMock(), mock.MagicMock()
        busy.bind.side_effect = OSError(10048, 'Address already in use')
        with mock.patch.object(sender, 'get_local_ip', return_value='192.168.60.134'):
            listener = sender.SimulatorBroadcastListener()
        def receive(*args):
            listener.stop()
            raise sender.socket.timeout()
        working.recvfrom.side_effect = receive
        with mock.patch.object(sender.socket, 'socket', side_effect=[busy, working]), \
                mock.patch.object(listener, 'msleep'):
            listener.run()
        self.assertTrue(listener.bind_ok)
        self.assertIsNone(listener.bind_error)
        self.assertEqual(15003, listener.bound_port)
        busy.close.assert_called_once()
        working.close.assert_called_once()

    def test_listener_stops_during_bind_retry_and_keeps_failure_reason(self):
        busy = mock.MagicMock()
        busy.bind.side_effect = OSError(10013, 'Permission denied')
        with mock.patch.object(sender, 'get_local_ip', return_value='192.168.60.134'):
            listener = sender.SimulatorBroadcastListener()
        with mock.patch.object(sender.socket, 'socket', return_value=busy) as create, \
                mock.patch.object(listener, 'msleep', side_effect=lambda _: listener.stop()):
            listener.run()
        self.assertFalse(listener.bind_ok)
        self.assertIn('10013', listener.bind_error)
        self.assertEqual(len(sender.DISCOVERY_PORTS), create.call_count)
        self.assertEqual(len(sender.DISCOVERY_PORTS), busy.close.call_count)

    def test_listener_retries_after_all_discovery_ports_fail(self):
        failed = [mock.MagicMock() for _ in sender.DISCOVERY_PORTS]
        working = mock.MagicMock()
        for sock in failed:
            sock.bind.side_effect = OSError(10048, 'Address already in use')
        with mock.patch.object(sender, 'get_local_ip', return_value='192.168.60.134'):
            listener = sender.SimulatorBroadcastListener()
        def receive(*args):
            listener.stop()
            raise sender.socket.timeout()
        working.recvfrom.side_effect = receive
        with mock.patch.object(sender.socket, 'socket', side_effect=failed + [working]), \
                mock.patch.object(listener, 'msleep') as sleep:
            listener.run()
        self.assertTrue(listener.bind_ok)
        self.assertEqual(5003, listener.bound_port)
        self.assertIsNone(listener.bind_error)
        self.assertEqual(20, sleep.call_count)
        for sock in failed + [working]:
            sock.close.assert_called_once()

    def test_real_udp_fallback_receives_two_distinct_senders(self):
        # Only loopback and ephemeral ports: no venue or developer LAN changes.
        with sender.socket.socket(sender.socket.AF_INET, sender.socket.SOCK_DGRAM) as occupied:
            if hasattr(sender.socket, 'SO_EXCLUSIVEADDRUSE'):
                occupied.setsockopt(sender.socket.SOL_SOCKET, sender.socket.SO_EXCLUSIVEADDRUSE, 1)
            occupied.bind(('0.0.0.0', 0))
            busy_port = occupied.getsockname()[1]
            with sender.socket.socket(sender.socket.AF_INET, sender.socket.SOCK_DGRAM) as reserve:
                reserve.bind(('127.0.0.1', 0))
                fallback_port = reserve.getsockname()[1]
            with mock.patch.object(sender, 'DISCOVERY_PORTS', (busy_port, fallback_port)), \
                    mock.patch.object(sender, 'get_local_ip', return_value='127.0.0.1'), \
                    mock.patch.object(sender, 'local_network_interfaces', return_value=[]):
                listener = sender.SimulatorBroadcastListener()
                listener.start()
                try:
                    deadline = time.monotonic() + 3
                    while listener.bind_ok is not True and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(listener.bind_ok)
                    self.assertEqual(fallback_port, listener.bound_port)
                    for number in (2, 3):
                        with sender.socket.socket(sender.socket.AF_INET, sender.socket.SOCK_DGRAM) as peer:
                            peer.bind((f'127.0.0.{number}', 0))
                            packet = dict(simulator_id=number, simulator_name=f'Sim {number}',
                                          device_id=f'pc-{number}', current_driver=None, port=5001)
                            peer.sendto(sender.DISCOVERY_MAGIC + json.dumps(packet).encode(),
                                        ('127.0.0.1', fallback_port))
                    deadline = time.monotonic() + 2
                    while len(listener.get_known_simulators()) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual({2, 3}, {p['simulator_id'] for p in listener.get_known_simulators()})
                finally:
                    listener.stop()
                    self.assertTrue(listener.wait(2000))

    def test_shared_address_hotspot_and_private_ethernet_both_participate(self):
        interfaces = [
            dict(ip='100.64.12.10', broadcast='100.64.12.255', kind='Wi-Fi'),
            dict(ip='192.168.8.20', broadcast='192.168.8.255', kind='Ethernet'),
        ]
        with mock.patch.object(sender, 'active_interfaces', return_value=interfaces):
            self.assertEqual(interfaces, sender.local_network_interfaces())

    def test_live_peer_reaches_ipad_while_an_old_saved_peer_is_still_pending(self):
        discovery = sender.SimulatorDiscovery
        release, live_answered = threading.Event(), threading.Event()
        saved = [{'ip': '192.168.8.90'}, {'ip': '192.168.8.21'}]
        def probe(ip, *args):
            if ip == saved[0]['ip']:
                release.wait(3)
                return None
            live_answered.set()
            return dict(ip=ip, device_id='wifi-pc', simulator_id=2,
                        simulator_name='Sim 2', current_driver=None, reachable=True)
        with mock.patch.object(sender, 'main_window', None), \
                mock.patch.object(sender, 'get_local_ip', return_value='192.168.8.20'), \
                mock.patch.object(sender, 'local_network_interfaces', return_value=[{'ip': '192.168.8.20'}]), \
                mock.patch.object(sender, 'load_known_simulators', return_value=saved), \
                mock.patch.object(discovery, 'ensure_scan'), \
                mock.patch.object(discovery, '_cache', {}), \
                mock.patch.object(discovery, '_scan_results', {}), \
                mock.patch.object(discovery, '_refresh_in_progress', True), \
                mock.patch.object(discovery, 'probe_host', side_effect=probe):
            worker = threading.Thread(target=discovery.discover)
            worker.start()
            try:
                self.assertTrue(live_answered.wait(1))
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    peers = sender.app.test_client().get('/api/discover_simulators').json['simulators']
                    if any(p.get('device_id') == 'wifi-pc' for p in peers):
                        break
                    time.sleep(0.01)
                self.assertTrue(worker.is_alive(), 'Old saved peer should still be pending')
                self.assertIn('wifi-pc', [p.get('device_id') for p in peers])
            finally:
                release.set()
                worker.join(3)

    def test_http_discovery_reaches_desktop_on_gui_thread(self):
        view = DiscoveryView()
        peer = dict(ip='192.168.0.22', simulator_name='Sim 3', reachable=True)
        with mock.patch.object(sender, 'get_local_ip', return_value='192.168.0.20'), \
                mock.patch.object(sender.SimulatorDiscovery, 'discover', return_value=[peer]):
            view.refresh_network_simulators_ui()
            deadline = time.monotonic() + 0.5
            while view.rows is None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
        self.assertIsNotNone(view.rows, 'Background discovery never updated the desktop')
        self.assertEqual(['192.168.0.22'], [row['ip'] for row in view.rows])
        self.assertEqual(threading.get_ident(), view.updated_on)

    def test_udp_change_requests_merged_refresh_instead_of_erasing_http_peers(self):
        view = types.SimpleNamespace(
            refresh_network_simulators_ui=mock.Mock(),
            _refresh_from_udp_discovery=mock.Mock(),
            _update_sim_id_conflict_ui=mock.Mock(),
        )
        sender.LapTimeSenderWindow.on_simulator_discovered(view, {'ip': '192.168.0.21'})
        view.refresh_network_simulators_ui.assert_called_once_with()
        view._refresh_from_udp_discovery.assert_not_called()

    def test_unreachable_peer_stays_in_desktop_list(self):
        view = DiscoveryView()
        peer = dict(ip='192.168.0.22', simulator_name='Sim 3', reachable=False,
                    connection_state='reconnecting')
        with mock.patch.object(sender, 'get_local_ip', return_value='192.168.0.20'), \
                mock.patch.object(sender.SimulatorDiscovery, 'discover', return_value=[peer]):
            view.refresh_network_simulators_ui()
            deadline = time.monotonic() + 0.5
            while view.rows is None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
        self.assertEqual(['192.168.0.22'], [row['ip'] for row in view.rows])
        self.assertFalse(view.rows[0]['reachable'])

    def test_actual_network_change_retains_known_cards_but_disables_actions(self):
        discovery = sender.SimulatorDiscovery
        peer = dict(ip='192.168.0.22', device_id='peer', simulator_name='Sim 3', reachable=True)
        with mock.patch.object(discovery, '_cache', {peer['ip']: peer}), \
                mock.patch.object(discovery, '_scan_results', {}), \
                mock.patch.object(sender, 'main_window', None):
            discovery.reset_network()
            peers = discovery._snapshot()
            self.assertEqual(1, len(peers))
            self.assertEqual('Sim 3', peers[0]['simulator_name'])
            self.assertFalse(peers[0]['reachable'])
            self.assertEqual('reconnecting', peers[0]['connection_state'])

    def test_transient_http_failure_retries_before_publishing_offline(self):
        discovery = sender.SimulatorDiscovery
        peer = dict(ip='192.168.0.22', port=5001, device_id='peer', simulator_id=3,
                    simulator_name='Sim 3', reachable=True, current_driver='Josh')
        replies = [None, dict(peer, current_driver=None)]
        with mock.patch.object(sender, 'main_window', None), \
                mock.patch.object(sender, 'get_local_ip', return_value='192.168.0.20'), \
                mock.patch.object(sender, 'local_network_interfaces', return_value=[{'ip': '192.168.0.20'}]), \
                mock.patch.object(sender, 'load_known_simulators', return_value=[]), \
                mock.patch.object(discovery, '_cache', {peer['ip']: peer}), \
                mock.patch.object(discovery, '_scan_results', {}), \
                mock.patch.object(discovery, 'ensure_scan'), \
                mock.patch.object(discovery, 'probe_host', side_effect=replies) as probe:
            peers = discovery.discover()
            self.assertEqual(2, probe.call_count)
            self.assertEqual([5001], probe.call_args.args[2])
            self.assertTrue(peers[0]['reachable'])
            self.assertIsNone(peers[0]['current_driver'])
            self.assertEqual(0, peers[0]['missed_status_checks'])

    def test_repeated_missing_replies_keep_card_disable_actions_and_recover(self):
        discovery = sender.SimulatorDiscovery
        peer = dict(ip='192.168.0.22', port=5001, device_id='peer', simulator_id=3,
                    simulator_name='Sim 3', reachable=True, current_driver='Josh')
        announcement = dict(peer)  # UDP can still arrive while the web endpoint is blocked.
        with mock.patch.object(sender, 'main_window', None), \
                mock.patch.object(sender, 'get_local_ip', return_value='192.168.0.20'), \
                mock.patch.object(sender, 'local_network_interfaces', return_value=[{'ip': '192.168.0.20'}]), \
                mock.patch.object(sender, 'load_known_simulators', return_value=[]), \
                mock.patch.object(discovery, '_cache', {peer['ip']: peer}), \
                mock.patch.object(discovery, '_scan_results', {peer['ip']: dict(peer)}), \
                mock.patch.object(discovery, '_udp_simulators', return_value=[announcement]), \
                mock.patch.object(discovery, 'ensure_scan'), \
                mock.patch.object(discovery, 'probe_host', return_value=None) as probe:
            for missed, state in [(1, 'reconnecting'), (2, 'reconnecting'), (3, 'offline'), (4, 'offline')]:
                peers = discovery.discover()
                self.assertEqual(1, len(peers))
                self.assertFalse(peers[0]['reachable'])
                self.assertEqual(state, peers[0]['connection_state'])
                self.assertEqual(missed, peers[0]['missed_status_checks'])
                cards = self.render_cards(peers)
                self.assertEqual(1, cards.count('class="sim-card '))
                self.assertIn('disabled', cards)
                self.assertNotIn('>REGISTER<', cards)
                self.assertNotIn('>AVAILABLE<', cards)
            probe.return_value = dict(peer, current_driver=None)
            peers = discovery.discover()
            self.assertTrue(peers[0]['reachable'])
            self.assertEqual('online', peers[0]['connection_state'])
            self.assertEqual(0, peers[0]['missed_status_checks'])
            self.assertIn('>REGISTER<', self.render_cards(peers))

    def test_ipad_background_scan_runs_even_with_a_partial_peer_list(self):
        combo = types.SimpleNamespace(currentText=lambda: '1')
        window = types.SimpleNamespace(
            simulator_id=combo, game_selector=combo,
            driver_name=types.SimpleNamespace(text=lambda: ''),
            get_sim_id_conflicts=lambda: [],
            discovery_listener=types.SimpleNamespace(bind_ok=True),
        )
        cases = [
            ('no announcements', [], 0, False, True),
            ('saved peer offline', [{'ip': '192.168.0.21', 'simulator_id': 99,
                                     'reachable': False}], 0, False, True),
            ('peer found', [{'ip': '192.168.0.21', 'simulator_id': 2,
                            'reachable': True}], 0, False, True),
            ('scan active', [], 0, True, True),
            ('recent scan', [], 490, False, False),
        ]
        for label, peers, scan_time, active, should_scan in cases:
            with self.subTest(label=label), \
                    mock.patch.object(sender, 'main_window', window), \
                    mock.patch.object(sender, 'get_local_ip', return_value='192.168.0.20'), \
                    mock.patch.object(sender.time, 'time', return_value=500), \
                    mock.patch.object(sender.SimulatorDiscovery, '_cache', {p['ip']: p for p in peers}), \
                    mock.patch.object(sender.SimulatorDiscovery, '_last_refresh', 500), \
                    mock.patch.object(sender.SimulatorDiscovery, '_scan_time', scan_time), \
                    mock.patch.object(sender.SimulatorDiscovery, '_scan_attempt_time', 0), \
                    mock.patch.object(sender.SimulatorDiscovery, 'scan_in_progress', return_value=active), \
                    mock.patch.object(sender.SimulatorDiscovery, 'start_full_scan') as scan:
                response = sender.app.test_client().get('/api/discover_simulators')
                self.assertEqual(200, response.status_code)
                self.assertEqual('192.168.0.20', response.json['current_ip'])
                self.assertEqual(should_scan, scan.called)

    def test_scan_results_restore_three_ipad_cards_without_udp(self):
        peers = {ip: dict(ip=ip, simulator_id=sim_id, simulator_name=f'Sim {sim_id}',
                          port=5001, reachable=True)
                 for ip, sim_id in [('192.168.0.21', 2), ('192.168.0.22', 3)]}
        with mock.patch.object(sender, 'main_window', None), \
                mock.patch.object(sender, 'get_local_ip', return_value='192.168.0.20'), \
                mock.patch.object(sender, 'local_network_interfaces', return_value=[{'ip': '192.168.0.20'}]), \
                mock.patch.object(sender.SimulatorDiscovery, '_cache', {}), \
                mock.patch.object(sender.SimulatorDiscovery, '_last_refresh', time.time()), \
                mock.patch.object(sender, 'load_known_simulators', return_value=[]), \
                mock.patch.object(sender, 'add_known_simulator') as remember, \
                mock.patch.object(sender.SimulatorDiscovery, '_scan_results', {}), \
                mock.patch.object(sender.SimulatorDiscovery, '_scan_time', 0), \
                mock.patch.object(sender.SimulatorDiscovery, '_scan_in_progress', True), \
                mock.patch.object(sender.SimulatorDiscovery, 'probe_host', side_effect=lambda ip, _: peers.get(ip)):
            sender.SimulatorDiscovery._run_full_scan()
            response = sender.app.test_client().get('/api/discover_simulators')
            self.assertEqual(200, response.status_code)
            self.assertEqual([1, 2, 3], [sim['simulator_id'] for sim in response.json['simulators']])
            self.assertEqual(2, remember.call_count)

    def test_scan_attempt_timestamp_does_not_extend_cached_results(self):
        with mock.patch.object(sender.SimulatorDiscovery, '_scan_in_progress', False), \
                mock.patch.object(sender.SimulatorDiscovery, '_scan_attempt_time', 0), \
                mock.patch.object(sender.SimulatorDiscovery, '_scan_time', 100), \
                mock.patch.object(sender.time, 'time', return_value=500), \
                mock.patch.object(sender.threading, 'Thread') as thread:
            self.assertTrue(sender.SimulatorDiscovery.start_full_scan())
            self.assertEqual(500, sender.SimulatorDiscovery._scan_attempt_time)
            self.assertEqual(100, sender.SimulatorDiscovery._scan_time)
            self.assertFalse(sender.SimulatorDiscovery.start_full_scan())
            thread.assert_called_once()

    def test_unprobed_saved_addresses_have_no_reported_id(self):
        html = sender.ADMIN_HTML_CONTENT
        code = html[html.index('function allSimulatorsForDisplay()'):html.index('function renderHostBanner()')]
        script = """
const discoveredSimulators = [];
const getManualSimulators = () => [{ip: '192.168.0.21'}, {ip: '192.168.0.22'}];
CODE
process.stdout.write(JSON.stringify(allSimulatorsForDisplay()));
""".replace('CODE', code)
        result = subprocess.run(['node', '-'], input=script, encoding='utf-8', capture_output=True, check=True)
        peers = json.loads(result.stdout)
        self.assertEqual(2, len(peers))
        self.assertTrue(all(peer['simulator_id'] is None and peer['reachable'] is False for peer in peers))

    def test_desktop_does_not_overlap_discovery_jobs(self):
        view = DiscoveryView()
        view._network_discovery_in_progress = True
        with mock.patch.object(sender.SimulatorDiscovery, 'discover') as discover:
            view.refresh_network_simulators_ui()
        discover.assert_not_called()

    def test_failed_desktop_refresh_preserves_rows_and_allows_retry(self):
        view = DiscoveryView()
        view.rows = [{'ip': '192.168.0.22'}]
        with mock.patch.object(sender, 'get_local_ip', return_value='192.168.0.20'), \
                mock.patch.object(sender.SimulatorDiscovery, 'discover', side_effect=RuntimeError('probe failed')):
            view.refresh_network_simulators_ui()
            deadline = time.monotonic() + 0.5
            while view._network_discovery_in_progress and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
        self.assertFalse(view._network_discovery_in_progress)
        self.assertEqual([{'ip': '192.168.0.22'}], view.rows)

    def render_cards(self, peers):
        html = sender.ADMIN_HTML_CONTENT
        code = html[html.index('function renderSimulatorCards()'):html.index('function onSimCardClick(')]
        script = """
const grid = {innerHTML: ''};
const document = {getElementById: () => grid};
const queueData = {queue: []};
const isCurrentSim = () => false;
const simName = s => s.simulator_name || 'Saved simulator';
const escapeHtml = s => String(s);
const allSimulatorsForDisplay = () => PEERS;
CODE
renderSimulatorCards();
process.stdout.write(grid.innerHTML);
""".replace('PEERS', json.dumps(peers)).replace('CODE', code)
        result = subprocess.run(['node', '-'], input=script, encoding='utf-8', capture_output=True, check=True)
        return result.stdout

    def test_offline_placeholder_ids_do_not_report_duplicate(self):
        peers = [
            dict(ip='192.168.0.20', simulator_id=1, reachable=True),
            dict(ip='192.168.0.21', simulator_id=99, reachable=False),
            dict(ip='192.168.0.22', simulator_id=99, reachable=False),
        ]
        cards = self.render_cards(peers)
        self.assertNotIn('Duplicate ID', cards)
        self.assertEqual(3, cards.count('class="sim-card '))

    def test_real_duplicate_ids_are_still_reported(self):
        peers = [dict(ip=ip, simulator_id=99, reachable=True)
                 for ip in ['192.168.0.21', '192.168.0.22']]
        self.assertEqual(2, self.render_cards(peers).count('Duplicate ID'))

    def test_snapshot_deduplicates_interfaces_but_keeps_duplicate_numbers(self):
        peers = [dict(ip=ip, device_id=device, simulator_id=1, reachable=online)
                 for ip, device, online in [('a', 'pc1', False), ('b', 'pc1', True),
                                            ('c', 'pc2', True), ('d', 'self', True)]]
        with mock.patch.object(sender, 'main_window', types.SimpleNamespace(device_id='self')), \
                mock.patch.object(sender.SimulatorDiscovery, '_cache', {p['ip']: p for p in peers}):
            self.assertEqual({'b', 'c'}, {p['ip'] for p in sender.SimulatorDiscovery._snapshot()})

    def test_slow_peer_does_not_block_ipad_snapshot(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def slow_probe():
            started.set()
            release.wait(2)
            finished.set()
        with mock.patch.object(sender, 'main_window', None), \
                mock.patch.object(sender, 'get_local_ip', return_value='192.168.0.20'), \
                mock.patch.object(sender.SimulatorDiscovery, 'ensure_scan'), \
                mock.patch.object(sender.SimulatorDiscovery, 'discover', side_effect=slow_probe) as probe, \
                mock.patch.object(sender.SimulatorDiscovery, '_cache', {}), \
                mock.patch.object(sender.SimulatorDiscovery, '_last_refresh', 0), \
                mock.patch.object(sender.SimulatorDiscovery, '_refresh_in_progress', False):
            try:
                begin = time.monotonic()
                result = sender.app.test_client().get('/api/discover_simulators')
                self.assertLess(time.monotonic() - begin, 0.5)
                self.assertEqual(200, result.status_code)
                self.assertTrue(started.wait(0.5))
                sender.app.test_client().get('/api/discover_simulators')
                self.assertEqual(1, probe.call_count)
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
                deadline = time.monotonic() + 1
                while sender.SimulatorDiscovery._refresh_in_progress and time.monotonic() < deadline:
                    time.sleep(0.005)

    def test_real_http_peers_and_registration_through_one_host(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                data = dict(simulator_id=self.server.number, simulator_name=f'Sim {self.server.number}',
                            device_id=f'pc{self.server.number}', port=5001)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            def do_POST(self):
                received.append((self.server.number, self.path,
                                 json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"success": true}')
        servers = []
        try:
            for number in [2, 3]:
                server = ThreadingHTTPServer((f'127.0.0.{number}', 0), Handler)
                server.number = number
                servers.append(server)
                threading.Thread(target=server.serve_forever, daemon=True).start()
            saved = [dict(ip=s.server_address[0], port=s.server_address[1]) for s in servers]
            with mock.patch.dict('os.environ', {'NO_PROXY': '*'}), \
                    mock.patch.object(sender, 'main_window', None), \
                    mock.patch.object(sender, 'get_local_ip', return_value='127.0.0.1'), \
                    mock.patch.object(sender, 'local_network_interfaces', return_value=[{'ip': '127.0.0.1'}]), \
                    mock.patch.object(sender, 'load_known_simulators', return_value=saved), \
                    mock.patch.object(sender.SimulatorDiscovery, '_cache', {}), \
                    mock.patch.object(sender.SimulatorDiscovery, '_scan_results', {}), \
                    mock.patch.object(sender.SimulatorDiscovery, 'ensure_scan'):
                peers = sender.SimulatorDiscovery.discover()
                response = sender.app.test_client().get('/api/discover_simulators')
                self.assertEqual([1, 2, 3], [p['simulator_id'] for p in response.json['simulators']])
                self.assertEqual({s.server_address[1] for s in servers}, {p['port'] for p in peers})
                for p in peers:
                    result = sender.app.test_client().post('/api/sim_proxy', json=dict(
                        ip=p['ip'], port=p['port'], path='/set_driver', payload={'driver_name': 'Josh'}))
                    self.assertEqual(200, result.status_code)
                self.assertEqual({2, 3}, {entry[0] for entry in received})
                self.assertEqual(2, len(received))
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()

    def test_real_http_peer_loss_and_recovery_preserve_three_ipad_cards(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.server.unavailable:
                    self.send_response(503)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(dict(
                    simulator_id=self.server.number, simulator_name=f'Sim {self.server.number}',
                    device_id=f'pc{self.server.number}', current_driver=None)).encode())
        servers = []
        try:
            for number in [2, 3]:
                server = ThreadingHTTPServer((f'127.0.0.{number}', 0), Handler)
                server.number, server.unavailable = number, False
                servers.append(server)
                threading.Thread(target=server.serve_forever, daemon=True).start()
            saved = [dict(ip=s.server_address[0], port=s.server_address[1]) for s in servers]
            discovery = sender.SimulatorDiscovery
            with mock.patch.dict('os.environ', {'NO_PROXY': '*'}), \
                    mock.patch.object(sender, 'main_window', None), \
                    mock.patch.object(sender, 'get_local_ip', return_value='127.0.0.1'), \
                    mock.patch.object(sender, 'local_network_interfaces', return_value=[{'ip': '127.0.0.1'}]), \
                    mock.patch.object(sender, 'load_known_simulators', return_value=saved), \
                    mock.patch.object(sender, 'get_simulator_probe_ports', return_value=[]), \
                    mock.patch.object(discovery, '_cache', {}), \
                    mock.patch.object(discovery, '_scan_results', {}), \
                    mock.patch.object(discovery, '_refresh_in_progress', True), \
                    mock.patch.object(discovery, 'ensure_scan'):
                for unavailable in [False, True, True, False, True, False, True, True, True, False]:
                    servers[0].unavailable = unavailable
                    discovery.discover()
                    response = sender.app.test_client().get('/api/discover_simulators')
                    cards = response.json['simulators']
                    self.assertEqual([1, 2, 3], [card['simulator_id'] for card in cards])
                    self.assertEqual(not unavailable, cards[1]['reachable'])
                    self.assertTrue(cards[2]['reachable'])
                    rendered = self.render_cards(cards)
                    self.assertEqual(3, rendered.count('class="sim-card '))
                    self.assertEqual(2 if unavailable else 3, rendered.count('>REGISTER<'))
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()

    def test_concurrent_scan_and_manual_adds_preserve_saved_peers(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(sender, 'get_config_path', return_value=Path(directory) / 'simulators.json'):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(sender.add_known_simulator, [f'192.168.0.{n}' for n in range(1, 10)]))
            self.assertEqual(9, len(sender.load_known_simulators()))

    def test_ipad_remote_action_has_one_same_origin_request(self):
        html = sender.ADMIN_HTML_CONTENT
        code = html[html.index('async function postToSim('):html.index('function showToast(')]
        script = '''
const calls = [];
const isCurrentSim = s => s.is_current;
const fetchWithTimeout = async (url, options) => {
    calls.push({url, body: JSON.parse(options.body)});
    return {json: async () => ({success: true})};
};
CODE
(async () => {
    await postToSim({ip:'192.168.0.22',port:5101}, '/set_driver', {driver_name:'Josh'});
    process.stdout.write(JSON.stringify(calls));
})();
'''.replace('CODE', code)
        result = subprocess.run(['node', '-'], input=script, encoding='utf-8', capture_output=True, check=True)
        calls = json.loads(result.stdout)
        self.assertEqual(1, len(calls))
        self.assertEqual('/api/sim_proxy', calls[0]['url'])
        self.assertEqual('192.168.0.22', calls[0]['body']['ip'])

    def test_ipad_overlapping_refreshes_share_one_request(self):
        html = sender.ADMIN_HTML_CONTENT
        code = html[html.index('let simulatorRefreshPromise'):html.index('async function updateSimulatorSnapshot')]
        script = '''
let calls = 0, release;
const pending = new Promise(r => release = r);
const updateSimulatorSnapshot = () => { calls++; return pending; };
CODE
(async () => {
    const first = initializeSimulators();
    const second = initializeSimulators();
    if(first !== second || calls !== 1) throw Error('overlapping requests');
    release(); await first;
    await initializeSimulators();
    if(calls !== 2) throw Error('refresh cannot resume');
})();
'''.replace('CODE', code)
        subprocess.run(['node', '-'], input=script, encoding='utf-8', capture_output=True, check=True)

    def test_announcements_use_every_current_lan_and_include_identity(self):
        interfaces = [dict(ip='192.168.0.19', broadcast='192.168.0.255'),
                      dict(ip='10.2.3.4', broadcast='10.2.255.255')]
        sockets = [mock.MagicMock(), mock.MagicMock()]
        for sock in sockets:
            sock.__enter__.return_value = sock
        window = types.SimpleNamespace(device_id='pc-a', automatic_id_enabled=True,
                                       simulator_id_assigned=True, session_active=False,
                                       simulator_id=types.SimpleNamespace(currentText=lambda: '3'),
                                       game_selector=types.SimpleNamespace(currentText=lambda: 'Assetto Corsa'),
                                       driver_name=types.SimpleNamespace(text=lambda: ''))
        with mock.patch.object(sender, 'main_window', window), \
                mock.patch.object(sender, 'local_network_interfaces', return_value=interfaces), \
                mock.patch.object(sender, 'get_local_ip', return_value='192.168.0.19'), \
                mock.patch.object(sender.socket, 'socket', side_effect=sockets):
            sender.broadcast_simulator_presence()
        for interface, sock in zip(interfaces, sockets):
            sock.bind.assert_called_once_with((interface['ip'], 0))
            calls = sock.sendto.call_args_list
            self.assertEqual([(interface['broadcast'], port) for port in sender.DISCOVERY_PORTS],
                             [call.args[1] for call in calls])
            packet = calls[0].args[0]
            payload = json.loads(packet[len(sender.DISCOVERY_MAGIC):])
            self.assertEqual('pc-a', payload['device_id'])
            self.assertEqual(3, payload['simulator_id'])
            self.assertTrue(payload['id_automatic'])

    def test_network_switch_discards_inflight_old_peer_results(self):
        discovery = sender.SimulatorDiscovery
        with mock.patch.object(sender, 'main_window', None), \
                mock.patch.object(sender, 'get_local_ip', return_value='192.168.0.20'), \
                mock.patch.object(sender, 'local_network_interfaces', return_value=[]), \
                mock.patch.object(sender, 'load_known_simulators', return_value=[{'ip': '192.168.0.21'}]), \
                mock.patch.object(discovery, 'ensure_scan'), \
                mock.patch.object(discovery, '_cache', {}), \
                mock.patch.object(discovery, '_scan_results', {}), \
                mock.patch.object(discovery, '_network_generation', 1):
            def old_result(*args):
                discovery.reset_network()
                return dict(ip='192.168.0.21', simulator_id=2, reachable=True)
            with mock.patch.object(discovery, 'probe_host', side_effect=old_result):
                self.assertEqual([], discovery.discover())
            self.assertEqual({}, discovery._cache)


if __name__ == '__main__':
    unittest.main()
