"""A status response sampled before registration must not replace its acknowledgement."""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import lap_time_sender as sender


class SimulatorSnapshotTests(unittest.TestCase):
    def check_registration_during_snapshot(self, endpoint):
        clock = {'now': 100.0}
        state = {'driver': 'Old driver', 'applied_at': None}

        def read_driver():
            value = state['driver']
            if value == 'Old driver':
                # This HTTP worker has read the old name. Before it builds the
                # rest of its response, Qt applies and acknowledges a new driver.
                state['driver'] = 'New driver'
                clock['now'] = 101.0
                state['applied_at'] = sender.time.time()
                clock['now'] = 102.0
            return value

        window = SimpleNamespace(
            simulator_id=SimpleNamespace(currentText=lambda: '1'),
            game_selector=SimpleNamespace(currentText=lambda: 'Assetto Corsa'),
            rotation_mode_selector=SimpleNamespace(currentText=lambda: 'Laps'),
            driver_name=SimpleNamespace(text=read_driver),
            get_sim_id_conflicts=lambda: [],
            session_active=True,
        )

        def current_status(response):
            self.assertEqual(200, response.status_code)
            payload = response.get_json()
            return payload['simulators'][0] if endpoint.endswith('discover_simulators') else payload

        with ExitStack() as stack:
            stack.enter_context(patch.object(sender, 'main_window', window))
            stack.enter_context(patch.object(sender.time, 'time', side_effect=lambda: clock['now']))
            stack.enter_context(patch.object(sender, 'get_local_ip', return_value='192.168.1.20'))
            stack.enter_context(patch.object(sender, 'get_web_interface_port', return_value=5001))
            stack.enter_context(patch.object(sender.SimulatorDiscovery, 'cached', return_value=[]))
            stack.enter_context(patch.object(sender.SimulatorDiscovery, 'scan_in_progress', return_value=False))
            with sender.app.test_client() as client:
                stale = current_status(client.get(endpoint))
                self.assertEqual('Old driver', stale['current_driver'])
                self.assertEqual(100.0, stale['state_timestamp'])
                self.assertLess(stale['state_timestamp'], state['applied_at'])

                # A subsequent sample can supersede the registration overlay:
                # both its name and sampling timestamp are genuinely newer.
                fresh = current_status(client.get(endpoint))
                self.assertEqual('New driver', fresh['current_driver'])
                self.assertEqual(102.0, fresh['state_timestamp'])
                self.assertGreater(fresh['state_timestamp'], state['applied_at'])

    def test_peer_info_keeps_timestamp_before_interleaved_registration(self):
        self.check_registration_during_snapshot('/api/simulator_info')

    def test_local_discovery_keeps_timestamp_before_interleaved_registration(self):
        self.check_registration_during_snapshot('/api/discover_simulators')


if __name__ == '__main__':
    unittest.main()
