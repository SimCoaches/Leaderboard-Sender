import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lap_time_sender as sender


class Checkbox:
    def __init__(self, checked):
        self.checked = checked

    def isChecked(self):
        return self.checked


class TextField:
    def __init__(self, value=''):
        self.value = value

    def text(self):
        return self.value

    def setText(self, value):
        self.value = value

    def setStyleSheet(self, _value):
        pass


class ComboField:
    def __init__(self, value):
        self.value = value

    def currentText(self):
        return self.value


class RegistrationWindow:
    def __init__(self):
        self.email_field_enabled = Checkbox(True)
        self.collect_email_checkbox = Checkbox(True)
        self.collect_phone_checkbox = Checkbox(True)
        self.lead_questions = {
            'questions': [{
                'prompt': 'Favorite manufacturer',
                'options': ['Porsche', 'BMW'],
            }]
        }
        self.last_driver_email = ''
        self.last_driver_phone = ''
        self.last_survey_answers = {}
        self.driver_name = TextField('')
        self.session_active = False
        self.background_image_path = TextField('')
        self.logo_image_path = TextField('')
        self.rotation_mode_selector = ComboField('Time')
        self.timer_started = False
        self.lap_rotation_active = False
        self.lap_count_started = False
        self.lap_rotation_count = 0
        self.lap_rotation_target = 0

    def update_driver_name(self, name):
        self.driver_name.setText(name)


class SignalCapture:
    def __init__(self):
        self.values = []

    def emit(self, value):
        self.values.append(value)


class SenderReleaseReadinessTests(unittest.TestCase):
    def setUp(self):
        self.original_main_window = sender.main_window
        self.original_ui_bridge = sender.ui_bridge

    def tearDown(self):
        sender.main_window = self.original_main_window
        sender.ui_bridge = self.original_ui_bridge

    def test_sector_config_sorts_boundaries_and_rejects_duplicates(self):
        config = sender.normalize_sector_config({
            'enabled': True,
            'sectors': [
                {'start_pct': 60, 'target_time': 40},
                {'start_pct': 10, 'target_time': 30},
                {'start_pct': 35, 'target_time': 25},
            ],
        })
        self.assertEqual([0.0, 35.0, 60.0], [s['start_pct'] for s in config['sectors']])
        self.assertTrue(config['enabled'])
        self.assertIsNone(sender.normalize_sector_config({
            'sectors': [
                {'start_pct': 0, 'target_time': 30},
                {'start_pct': 0, 'target_time': 40},
            ]
        }))

    def test_registration_contract_validates_and_preserves_guest_fields(self):
        window = RegistrationWindow()
        sender.main_window = window
        sender.ui_bridge = None
        saved_leads = []

        with mock.patch.object(
                sender, 'append_lead_to_csv',
                side_effect=lambda *args: saved_leads.append(args)):
            client = sender.app.test_client()
            settings = client.get('/get_settings')
            invalid = client.post('/set_driver', json={
                'driver_name': 'Jane Driver',
                'driver_email': 'jane@example.com',
                'driver_phone': '208-555-1234',
                'survey_answers': {'0': 'Mercedes'},
            })
            valid = client.post('/set_driver', json={
                'driver_name': 'Jane Driver',
                'driver_email': 'jane@example.com',
                'driver_phone': '208-555-1234',
                'survey_answers': {'0': 'Porsche'},
            })

        self.assertEqual(200, settings.status_code)
        self.assertEqual([{
            'id': 0,
            'prompt': 'Favorite manufacturer',
            'options': ['Porsche', 'BMW'],
        }], settings.get_json()['survey_questions'])
        self.assertEqual(400, invalid.status_code)
        self.assertEqual(200, valid.status_code)
        self.assertTrue(valid.get_json()['success'])
        self.assertEqual('Jane Driver', window.driver_name.text())
        self.assertEqual('jane@example.com', window.last_driver_email)
        self.assertEqual('+12085551234', window.last_driver_phone)
        self.assertEqual({'Favorite manufacturer': 'Porsche'}, window.last_survey_answers)
        self.assertEqual(1, len(saved_leads))

    def test_customer_ipad_logo_is_managed_served_and_cache_busted(self):
        window = RegistrationWindow()
        sender.main_window = window

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'customer-source.png'
            image = sender.QImage(4, 2, sender.QImage.Format.Format_ARGB32)
            image.fill(0xFF1BAA66)
            self.assertTrue(image.save(str(source)))

            install_root = root / 'installed'
            with mock.patch.object(sender, 'get_app_install_path', return_value=install_root):
                installed = sender.install_custom_ipad_logo(source)

            window.logo_image_path.setText(str(installed))
            response = sender.app.test_client().get('/logo')
            settings = sender.app.test_client().get('/get_settings').get_json()
            try:
                self.assertEqual(200, response.status_code)
                self.assertEqual(installed.read_bytes(), response.data)
                self.assertIn('no-store', response.headers.get('Cache-Control', ''))
                self.assertTrue(settings['logo_revision'].startswith('ipad-customer-logo.png-'))
            finally:
                response.close()

    def test_distance_payload_allows_email_only_and_contains_session_data(self):
        window = RegistrationWindow()
        window.collect_phone_checkbox = Checkbox(False)
        window.last_driver_email = 'jane@example.com'
        window.driver_name = TextField('Jane Driver')
        window.simulator_id = ComboField('4')
        window.current_session_id = 'session-456'
        window.last_survey_answers = {'Favorite manufacturer': 'Porsche'}
        window.current_server_url = 'http://127.0.0.1:5000'
        window.status_label = TextField()
        window.errors = []
        window.show_error = window.errors.append
        window._send_lap_to_receivers = lambda *_args: None
        captured = {}

        class DeferredThread:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def start(self):
                pass

        sender.main_window = window
        with mock.patch.object(sender.threading, 'Thread', DeferredThread):
            sender.LapTimeSenderWindow.send_lap_time(
                window,
                lap_time=103.4567,
                from_game=True,
                distance_pct=87.654321,
                challenge_fail=True,
            )

        self.assertEqual([], window.errors)
        payload = captured['args'][1]
        self.assertEqual(87.654321, payload['distance_pct'])
        self.assertEqual(103.457, payload['lap_time'])
        self.assertEqual('session-456', payload['session_id'])
        self.assertEqual('jane@example.com', payload['email'])
        self.assertNotIn('phone', payload)
        self.assertEqual({'Favorite manufacturer': 'Porsche'}, payload['survey_answers'])

    def test_sector_timeout_reports_live_track_distance(self):
        monitor = sender.AssettoCorsaMonitor()
        failures = []
        monitor.challenge_failed.connect(
            lambda sector, distance, elapsed: failures.append(
                (sector, distance, elapsed)
            )
        )
        monitor.set_challenge_config({
            'enabled': True,
            'ignore_out_lap': True,
            'sectors': [
                {'start_pct': 0, 'target_time': 1.0},
                {'start_pct': 50, 'target_time': 1.0},
            ],
        })

        # Cross start/finish to arm, then time out at 30.123456% of the lap.
        monitor._challenge_update(types.SimpleNamespace(
            normalizedCarPosition=0.99, iCurrentTime=990,
        ))
        monitor._challenge_update(types.SimpleNamespace(
            normalizedCarPosition=0.01, iCurrentTime=10,
        ))
        for position, lap_ms in ((0.05, 110), (0.10, 210), (0.15, 310),
                                 (0.20, 510), (0.25, 710), (0.30, 910)):
            monitor._challenge_update(types.SimpleNamespace(
                normalizedCarPosition=position, iCurrentTime=lap_ms,
            ))
        monitor._challenge_update(types.SimpleNamespace(
            normalizedCarPosition=0.30123456, iCurrentTime=1111,
        ))

        self.assertEqual(1, len(failures))
        self.assertEqual(0, failures[0][0])
        self.assertAlmostEqual(30.123456, failures[0][1], places=6)
        self.assertAlmostEqual(1.101, failures[0][2], places=3)

    def test_startup_near_finish_cannot_skip_thirteen_sectors(self):
        monitor = sender.AssettoCorsaMonitor()
        completions = []
        monitor.challenge_completed.connect(completions.append)
        monitor.set_challenge_config({
            'enabled': True,
            'ignore_out_lap': False,
            'sectors': [
                {'start_pct': index * (100.0 / 13.0), 'target_time': 60.0}
                for index in range(13)
            ],
        })

        # AC's first HOTLAP_START sample can be near 100%. It may arm a run,
        # but sitting there must not satisfy the twelve earlier boundaries.
        monitor._challenge_update(types.SimpleNamespace(
            normalizedCarPosition=0.990, iCurrentTime=10,
        ))
        monitor._challenge_update(types.SimpleNamespace(
            normalizedCarPosition=0.991, iCurrentTime=30,
        ))
        for index in range(15):
            monitor._challenge_update(types.SimpleNamespace(
                normalizedCarPosition=0.992 + (index * 0.0001),
                iCurrentTime=50 + (index * 20),
            ))
        monitor._challenge_update(types.SimpleNamespace(
            normalizedCarPosition=0.010, iCurrentTime=10,
        ))

        self.assertEqual([], completions)
        self.assertEqual('running', monitor._chal_state)
        self.assertEqual(0, monitor._chal_sector)

    def test_valid_sector_boundaries_still_complete_in_sequence(self):
        monitor = sender.AssettoCorsaMonitor()
        completions = []
        monitor.challenge_completed.connect(completions.append)
        monitor.set_challenge_config({
            'enabled': True,
            'ignore_out_lap': True,
            'sectors': [
                {'start_pct': 0.0, 'target_time': 20.0},
                {'start_pct': 50.0, 'target_time': 20.0},
            ],
        })

        samples = [(0.99, 990), (0.01, 10)]  # arm at start/finish
        lap_ms = 10
        position = 0.01
        while position < 0.99:
            position = min(0.99, position + 0.04)
            lap_ms += 500
            samples.append((position, lap_ms))
        samples.append((0.01, 10))  # finish after both sectors
        for position, lap_ms in samples:
            monitor._challenge_update(types.SimpleNamespace(
                normalizedCarPosition=position, iCurrentTime=lap_ms,
            ))

        self.assertEqual(1, len(completions))
        self.assertGreater(completions[0], 0.0)

    def test_csp_reset_point_can_be_saved_and_is_not_silently_overwritten(self):
        companion = (ROOT / 'ac_app' / 'LeaderboardSector' / 'LeaderboardSector.lua').read_text(
            encoding='utf-8')
        self.assertIn("if action == 'capture' then", companion)
        self.assertIn('Save current position as start point', companion)
        self.assertIn('if savedThisSession then return end', companion)
        self.assertIn('physics.setCarPosition(0, gridPos, -gridFwd)', companion)

    def test_partial_delivery_queues_only_failed_destination_and_merges(self):
        worker = types.SimpleNamespace(
            failed_lap_times=[],
            failed_lap_times_lock=threading.Lock(),
            successes=[],
            errors=[],
            status_label=TextField(),
        )
        worker._delivery_identity = sender.LapTimeSenderWindow._delivery_identity
        worker._queue_failed_delivery = types.MethodType(
            sender.LapTimeSenderWindow._queue_failed_delivery, worker
        )
        worker.show_success = worker.successes.append
        worker.show_error = worker.errors.append
        payload = {'simulator_id': 1, 'session_id': 'abc', 'lap_time': 88.123}

        sender.LapTimeSenderWindow._on_lap_send_finished(worker, {
            'data': payload,
            'lap_time': 88.123,
            'success_count': 1,
            'total': 2,
            'failed_urls': ['http://customer-device:5000'],
        })
        worker._queue_failed_delivery(payload, ['http://backup-device:5000'])

        self.assertEqual([], worker.errors)
        self.assertEqual(1, len(worker.failed_lap_times))
        self.assertEqual(
            ['http://customer-device:5000', 'http://backup-device:5000'],
            worker.failed_lap_times[0]['urls'],
        )

    def test_retry_keeps_only_still_failing_destination(self):
        worker = types.SimpleNamespace(
            failed_lap_times=[{
                'data': {'simulator_id': 1, 'session_id': 'abc', 'lap_time': 88.123},
                'urls': ['http://now-good:5000', 'http://still-bad:5000'],
            }],
            failed_lap_times_lock=threading.Lock(),
            retry_delivery_finished=SignalCapture(),
            start_retry_timer=lambda: None,
        )
        worker._delivery_identity = sender.LapTimeSenderWindow._delivery_identity
        worker._queue_failed_delivery = types.MethodType(
            sender.LapTimeSenderWindow._queue_failed_delivery, worker
        )

        def post(url, **_kwargs):
            if 'still-bad' in url:
                raise sender.requests.ConnectionError('offline')
            return types.SimpleNamespace(status_code=200)

        with mock.patch.object(sender.requests, 'post', side_effect=post):
            sender.LapTimeSenderWindow.retry_failed_laps(worker)

        self.assertEqual(1, len(worker.failed_lap_times))
        self.assertEqual(['http://still-bad:5000'], worker.failed_lap_times[0]['urls'])
        self.assertEqual(1, worker.retry_delivery_finished.values[0]['delivered_endpoints'])
        self.assertEqual(1, worker.retry_delivery_finished.values[0]['remaining_jobs'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
