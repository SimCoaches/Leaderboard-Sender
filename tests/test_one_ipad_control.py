"""One host controls three real Sender Qt processes through real HTTP routes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import requests


ROOT = Path(__file__).resolve().parents[1]


class OneIPadControlTests(unittest.TestCase):
    def test_one_host_routes_all_controls_and_recovers_a_peer(self):
        session = requests.Session()
        session.trust_env = False
        processes, logs, nodes = {}, [], {}
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)

            def wait_for(get_value, predicate, description, timeout=15):
                deadline = time.monotonic() + timeout
                last = None
                while time.monotonic() < deadline:
                    last = get_value()
                    if predicate(last):
                        return last
                    time.sleep(0.05)
                self.fail(f'{description}: {last}')

            def start(number, port=0, peers=None):
                ready = directory / f'ready-{number}.json'
                ready.unlink(missing_ok=True)
                log = (directory / f'process-{number}-{len(logs)}.log').open('w', encoding='utf-8')
                logs.append(log)
                env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8', NO_PROXY='*')
                processes[number] = subprocess.Popen(
                    [sys.executable, str(ROOT / 'tests/support/sender_control_fixture.py'), str(number),
                     str(directory), str(port), json.dumps(peers or [])], cwd=ROOT, env=env,
                    stdout=log, stderr=log,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                def ready_state():
                    process = processes[number]
                    if process.poll() is not None:
                        log.flush()
                        self.fail(Path(log.name).read_text(encoding='utf-8'))
                    return json.loads(ready.read_text(encoding='utf-8')) if ready.exists() else None
                nodes[number] = wait_for(ready_state, bool, f'Sender {number} startup')

            def state(number):
                return session.get(nodes[number]['url'] + '/fixture/state', timeout=3).json()

            def stop(number):
                process = processes[number]
                if process.poll() is None:
                    # Windows venv Python can use a launcher process. Killing
                    # only that launcher leaves its child holding the log file.
                    # Ask the real Qt process to exit and close its web server.
                    if number in nodes:
                        try:
                            session.post(nodes[number]['url'] + '/fixture/shutdown', timeout=2)
                        except requests.RequestException:
                            pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if os.name == 'nt':
                            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                           capture_output=True, timeout=5)
                        else:
                            process.terminate()
                        process.wait(timeout=5)

            def action(number, path, payload):
                # Every action enters through PC 1, exactly as one iPad does.
                if number == 1:
                    response = session.post(nodes[1]['url'] + path, json=payload, timeout=10)
                else:
                    response = session.post(nodes[1]['url'] + '/api/sim_proxy', timeout=10,
                                            json=dict(ip=nodes[number]['ip'], port=nodes[number]['port'],
                                                      path=path, payload=payload))
                self.assertEqual(200, response.status_code, response.text)
                self.assertTrue(response.json()['success'], response.text)
                if path in ('/set_driver', '/api/session/start_remote'):
                    result = response.json()
                    self.assertTrue(result['applied'], response.text)
                    self.assertTrue(result['session_id'], response.text)
                    self.assertGreater(result['applied_at'], 0)
                    self.assertEqual(payload['driver_name'], result['driver_name'])
                    # Success is returned after the target Qt window applies
                    # the driver, including when PC 1 proxies to a remote PC.
                    self.assertEqual(result['driver_name'], state(number)['driver'])

            def cards():
                response = session.get(nodes[1]['url'] + '/api/discover_simulators', timeout=3)
                self.assertEqual(200, response.status_code, response.text)
                return {card['simulator_id']: card for card in response.json()['simulators']}

            def wait_state(number, predicate, description):
                return wait_for(lambda: state(number), predicate, description)

            try:
                start(2)
                start(3)
                start(1, peers=[nodes[2], nodes[3]])
                wait_for(cards, lambda rows: set(rows) == {1, 2, 3} and
                         all(row.get('reachable') is not False for row in rows.values()), 'All PCs online')
                names = {1: '', 2: '', 3: ''}
                for number in (1, 2, 3):
                    name = f'Driver on PC {number}'
                    action(number, '/set_driver', {'driver_name': name})
                    wait_state(number, lambda row: row['driver'] == name and row['active'], 'Registration applied')
                    names[number] = name
                    self.assertEqual(names, {n: state(n)['driver'] for n in names})

                for number in (1, 2, 3):
                    deadlines = {n: state(n)['deadline'] for n in names}
                    action(number, '/api/session/extend', {'minutes': 5})
                    wait_state(number, lambda row: row['deadline'] == deadlines[number] + 300, 'Extension applied')
                    for other in names:
                        if other != number:
                            self.assertEqual(deadlines[other], state(other)['deadline'])
                    action(number, '/api/manual_lap', {'driver_name': names[number], 'lap_time': 65.321})
                    wait_state(number, lambda row: len(row['laps']) == 1, 'Manual lap applied')

                for number in (1, 2, 3):
                    action(number, '/api/session/force_end', {})
                    wait_state(number, lambda row: not row['active'] and not row['driver'], 'End applied')
                    names[number] = ''
                    self.assertEqual(names, {n: state(n)['driver'] for n in names})
                    self.assertEqual(['/set_driver', '/api/session/extend', '/api/manual_lap', '/api/session/force_end'],
                                     [entry['path'] for entry in state(number)['mutations']])

                # Also exercise the queue-to-PC start endpoint through the host.
                for number in (1, 2, 3):
                    action(number, '/api/session/start_remote', {'driver_name': f'Queue PC {number}', 'queue_id': 'test'})
                    wait_state(number, lambda row: row['active'], 'Remote start applied')
                    action(number, '/api/session/force_end', {})
                    wait_state(number, lambda row: not row['active'], 'Remote session ended')

                # A lost PC disappears; the other connected PCs remain usable.
                peer_port = nodes[2]['port']
                stop(2)
                lost = wait_for(cards, lambda rows: set(rows) == {1, 3},
                                'Disconnected PC hidden')
                self.assertTrue(lost[3]['reachable'])
                action(3, '/set_driver', {'driver_name': 'Still controllable'})
                wait_state(3, lambda row: row['driver'] == 'Still controllable', 'Healthy PC remained controllable')
                start(2, port=peer_port)
                wait_for(cards, lambda rows: rows.get(2, {}).get('reachable') is True, 'Restarted PC recovered')
                action(2, '/set_driver', {'driver_name': 'Recovered PC'})
                wait_state(2, lambda row: row['driver'] == 'Recovered PC', 'Recovered PC accepts control')
                self.assertEqual('Still controllable', state(3)['driver'])
                self.assertFalse(state(1)['active'])
            finally:
                for number in processes:
                    stop(number)
                for log in logs:
                    log.close()
                session.close()


if __name__ == '__main__':
    unittest.main()
