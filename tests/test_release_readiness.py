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

    def test_visible_app_version_matches_installer_version(self):
        setup_script = (ROOT / 'setup.iss').read_text(encoding='utf-8')
        self.assertIn(f'#define MyAppVersion "{sender.APP_VERSION}"', setup_script)

    def test_staff_ipad_contains_manual_top10_scoring_console(self):
        html = sender.ADMIN_HTML_CONTENT
        self.assertIn('id="manualBoardModal"', html)
        self.assertIn('name="manualRankBy" value="cars_passed"', html)
        self.assertIn('name="manualRankBy" value="finishing_position"', html)
        self.assertIn('name="manualRankBy" value="lap_time"', html)
        self.assertIn('id="manualShowCars"', html)
        self.assertIn('id="manualShowFinish"', html)
        self.assertIn('id="manualShowLap"', html)
        self.assertIn("fetchWithTimeout('/api/manual_leaderboard/result'", html)
        self.assertIn('entries.slice(0, 10)', html)
        self.assertIn('const NASCAR_STARTING_POSITION = 40', html)
        self.assertIn("syncManualNascarPosition('cars_passed')", html)
        self.assertIn("syncManualNascarPosition('finishing_position')", html)
        self.assertIn('12 passed = 28th', html)

    def test_staff_ipad_proxies_manual_settings_results_and_removal(self):
        calls = []

        def response(payload, status=200):
            return types.SimpleNamespace(status_code=status, json=lambda: payload)

        def forward(path, data=None, method='POST', timeout=3):
            calls.append((path, data, method, timeout))
            if path.startswith('/api/leaderboard/display-config'):
                return response({'success': True, 'config': {
                    'ranking_mode': 'cars_passed',
                    'manual_rank_by': 'finishing_position',
                    'manual_show_cars_passed': True,
                    'manual_show_finishing_position': True,
                    'manual_show_lap_time': True,
                }}), None
            if path.startswith('/api/leaderboard?'):
                return response({'success': True, 'entries': [{
                    'driver_name': 'Jamie Smith',
                    'cars_passed': 12,
                    'finishing_position': 3,
                    'lap_time': 101.5,
                }]}), None
            if path == '/api/manual-results':
                return response({'success': True, 'entries': []}), None
            if path == '/api/manual-results/remove':
                return response({'success': True, 'entries': []}), None
            raise AssertionError(path)

        client = sender.app.test_client()
        with mock.patch.object(sender, 'forward_to_receiver', side_effect=forward):
            state = client.get('/api/manual_leaderboard').get_json()
            settings = client.post('/api/manual_leaderboard/settings', json={
                'manual_rank_by': 'finishing_position',
                'manual_show_cars_passed': True,
                'manual_show_finishing_position': True,
                'manual_show_lap_time': False,
            }).get_json()
            result = client.post('/api/manual_leaderboard/result', json={
                'driver_name': 'Jamie Smith',
                'cars_passed': 12,
                'finishing_position': 3,
                'lap_time': 101.5,
            }).get_json()
            removed = client.post('/api/manual_leaderboard/remove', json={
                'driver_name': 'Jamie Smith',
            }).get_json()

        self.assertEqual('Jamie Smith', state['entries'][0]['driver_name'])
        self.assertTrue(settings['success'])
        self.assertTrue(result['success'])
        self.assertTrue(removed['success'])
        settings_call = next(call for call in calls if call[0] == '/api/leaderboard/display-config')
        self.assertEqual('cars_passed', settings_call[1]['ranking_mode'])
        self.assertEqual('finishing_position', settings_call[1]['manual_rank_by'])
        self.assertFalse(settings_call[1]['manual_show_lap_time'])
        self.assertIn(('/api/manual-results/remove', {'driver_name': 'Jamie Smith'}, 'POST', 5), calls)

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

    def test_waiting_background_cover_crop_stays_centered_without_distortion(self):
        # Exact 3440x1440 event artwork must remain pixel-for-pixel centered
        # on the matching ultrawide monitors shown in the field video.
        self.assertEqual(
            (0, 0, 3440, 1440),
            sender.centered_cover_crop(3440, 1440, 3440, 1440),
        )
        # A wider source is cropped equally from the left and right rather
        # than being stretched by QLabel.setScaledContents().
        self.assertEqual(
            (440, 0, 2560, 1440),
            sender.centered_cover_crop(3440, 1440, 1920, 1080),
        )
        # A taller source is cropped equally from the top and bottom.
        self.assertEqual(
            (0, 420, 2160, 2160),
            sender.centered_cover_crop(2160, 3000, 1920, 1920),
        )

    def test_waiting_overlay_propagates_every_state_to_covered_screens(self):
        calls = []

        class FakeBlanker:
            def set_media(self, path):
                calls.append(('media', path))

            def show_default_content(self):
                calls.append(('default',))

            def show_session_complete(self, headline, detail, accent):
                calls.append(('message', headline, detail, accent))

        overlay = types.SimpleNamespace(_blanker_state=('default',))
        sender.WaitingOverlay._apply_blanker_state(overlay, FakeBlanker())
        overlay._blanker_state = ('media', 'event-background.png')
        sender.WaitingOverlay._apply_blanker_state(overlay, FakeBlanker())
        overlay._blanker_state = ('message', 'Out of Time', 'Next driver', '#f48771')
        sender.WaitingOverlay._apply_blanker_state(overlay, FakeBlanker())

        self.assertEqual([
            ('default',),
            ('media', 'event-background.png'),
            ('message', 'Out of Time', 'Next driver', '#f48771'),
        ], calls)

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
        self.assertEqual(sender.APP_VERSION, settings.get_json()['app_version'])
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
