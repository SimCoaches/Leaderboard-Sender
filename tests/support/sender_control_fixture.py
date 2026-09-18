"""Local integration fixture: real Sender routes, Qt window and signal handling.

No game monitoring, LAN scans, receiver delivery, firewall changes or key input.
Only the loopback web servers owned by the parent test are reachable candidates.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import lap_time_sender as s
from werkzeug.serving import make_server


def main():
    number = int(sys.argv[1])
    directory = Path(sys.argv[2])
    address = f'127.0.0.{number}'
    port = int(sys.argv[3])
    peers = json.loads(sys.argv[4])
    mutations, laps = [], []
    app = s.QApplication([])
    with ExitStack() as stack:
        stack.enter_context(patch.object(s, 'get_settings_path', return_value=directory / f'settings-{number}.json'))
        stack.enter_context(patch.object(s, 'get_config_path', return_value=directory / f'peers-{number}.json'))
        stack.enter_context(patch.object(s, 'get_local_ip', return_value=address))
        stack.enter_context(patch.object(s, 'local_network_interfaces', return_value=[{'ip': address}]))
        stack.enter_context(patch.object(s, 'device_identity', return_value=f'test-pc-{number}'))
        stack.enter_context(patch.object(s, 'load_known_simulators', return_value=peers))
        stack.enter_context(patch.object(s, 'get_simulator_probe_ports', return_value=[]))
        stack.enter_context(patch.object(s, 'select_web_interface_port', return_value=port))
        stack.enter_context(patch.object(s.SimulatorDiscovery, 'ensure_scan'))
        for name in ['configure_firewall', 'mdns_service']:
            stack.enter_context(patch.object(s, name))
        for name in ['show_waiting_screen', 'start_monitor', '_notify_session_ended', '_notify_session_started']:
            stack.enter_context(patch.object(s.LapTimeSenderWindow, name))
        stack.enter_context(patch.object(s, 'send_game_shortcut', side_effect=AssertionError('No game input in tests')))
        s.save_app_settings(dict(simulator_id=number, automatic_simulator_id=False,
                                 lead_gen_enabled=False, rotation_mode='Time', rotation_time='5'))
        # Suppress constructor background work, then restore real Python threads
        # for HTTP/discovery. Qt signal delivery uses the real main event loop.
        with patch.object(s.threading.Thread, 'start'), patch.object(s.QThread, 'start'), \
                patch.object(s.QTimer, 'start'), patch.object(s.QTimer, 'singleShot'):
            window = s.LapTimeSenderWindow()
        window.waiting_overlay = Mock()
        window.send_lap_time = lambda lap, **kwargs: laps.append(dict(lap_time=lap, **kwargs))
        window.lead_questions = {'questions': []}

        @s.app.before_request
        def record_action():
            if s.request.method == 'POST' and s.request.path in s.SIM_PROXY_ALLOWED_PATHS:
                mutations.append(dict(path=s.request.path, body=s.request.get_json()))

        @s.app.get('/fixture/state')
        def state():
            return s.jsonify(dict(number=number, driver=window.driver_name.text(),
                                 active=window.session_active, deadline=window.timer_end_timestamp,
                                 mutations=mutations, laps=laps))

        server = make_server(address, port, s.app, threaded=True)
        window.web_port = server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        ready = directory / f'ready-{number}.json'
        ready.write_text(json.dumps(dict(ip=address, port=server.server_port,
                                        url=f'http://{address}:{server.server_port}')), encoding='utf-8')
        app.exec()


if __name__ == '__main__':
    main()
