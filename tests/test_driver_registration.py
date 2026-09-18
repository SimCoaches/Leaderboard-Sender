"""HTTP registrations acknowledge the actual Qt change, including queued retries."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


FIXTURE = r'''
from contextlib import ExitStack, contextmanager
from pathlib import Path
import queue
import tempfile
import threading
import time
from unittest.mock import Mock, patch
import lap_time_sender as s

app = s.QApplication([])

@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        stack.enter_context(patch.object(s, 'get_settings_path', return_value=Path(directory) / 'settings.json'))
        stack.enter_context(patch.object(s, 'get_local_ip', return_value='192.168.0.20'))
        stack.enter_context(patch.object(s, 'select_web_interface_port', return_value=5001))
        s.save_app_settings({'lead_gen_enabled': False, 'rotation_mode': 'Time', 'rotation_time': '5'})
        for name in ['configure_firewall', 'mdns_service']:
            stack.enter_context(patch.object(s, name))
        for name in ['show_waiting_screen', 'start_monitor', '_notify_session_started']:
            stack.enter_context(patch.object(s.LapTimeSenderWindow, name))
        stack.enter_context(patch.object(s, 'send_game_shortcut', side_effect=AssertionError('No game input in tests')))
        stack.enter_context(patch.object(s.QTimer, 'singleShot'))
        with patch.object(s.threading.Thread, 'start'), patch.object(s.QThread, 'start'), \
                patch.object(s.QTimer, 'start'):
            window = s.LapTimeSenderWindow()
        window.waiting_overlay = Mock()
        window.lead_questions = {'questions': []}
        registrations = queue.Queue()
        # Observe dispatch synchronously on the HTTP thread, without draining
        # Qt events; the window still receives its normal queued connection.
        s.ui_bridge.driver_registration_requested.connect(
            registrations.put, type=s.Qt.ConnectionType.DirectConnection)
        try:
            yield window, registrations
        finally:
            window.close()

def begin_request(path, payload):
    done, replies = threading.Event(), []
    def request():
        try:
            with s.app.test_client() as client:
                response = client.post(path, json=payload)
                replies.append((response.get_json(), response.status_code))
        except Exception as exc:
            replies.append(exc)
        finally:
            done.set()
    thread = threading.Thread(target=request, daemon=True)
    thread.start()
    return done, replies

def spin_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, 'Qt did not deliver the expected registration'
        app.processEvents()
        time.sleep(0.005)

def checked_reply(replies, status):
    assert len(replies) == 1 and isinstance(replies[0], tuple), replies
    result, actual_status = replies[0]
    assert actual_status == status, replies
    return result
'''


class DriverRegistrationTests(unittest.TestCase):
    def run_scenario(self, scenario):
        script = FIXTURE + '\nwith fixture() as (window, registrations):\n' + textwrap.indent(
            textwrap.dedent(scenario), '    ')
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, '-'], input=script, encoding='utf-8',
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_both_endpoints_acknowledge_only_after_the_gui_applies(self):
        self.run_scenario(r'''
            gui_thread = threading.get_ident()
            update_threads = []
            original_update = window.update_driver_name
            def update(name):
                update_threads.append(threading.get_ident())
                original_update(name)
            with patch.object(window, 'update_driver_name', side_effect=update):
                for endpoint, name in [('/set_driver', 'Walk-in'), ('/api/session/start_remote', 'Queued Driver')]:
                    old_driver = window.driver_name.text()
                    old_session = window.current_session_id
                    done, replies = begin_request(endpoint, {'driver_name': name, 'queue_id': 'queue-1'})
                    registration = registrations.get(timeout=1)
                    assert registration.state == 'queued'
                    assert not done.wait(0.05), 'HTTP acknowledged before Qt applied the driver'
                    assert window.driver_name.text() == old_driver
                    spin_until(done.is_set)
                    result = checked_reply(replies, 200)
                    assert result['success'] and result['applied'] and not result['already_active']
                    assert isinstance(result['applied_at'], (int, float)) and result['applied_at'] > 0
                    assert result['driver_name'] == window.driver_name.text() == name
                    assert result['session_id'] == window.current_session_id != old_session
                    assert registration.state == 'done' and registration.done.is_set()
                    assert window.session_active
            assert update_threads == [gui_thread, gui_thread]
            assert window.assigned_queue_id == 'queue-1'
        ''')

    def test_busy_gui_cancels_queued_requests_before_late_dispatch(self):
        self.run_scenario(r'''
            apply = s.apply_driver_registration
            with patch.object(s, 'apply_driver_registration', side_effect=lambda **data: apply(timeout=0.05, **data)):
                old_session = window.current_session_id
                for endpoint in ['/set_driver', '/api/session/start_remote']:
                    done, replies = begin_request(endpoint, {'driver_name': 'Must Not Appear'})
                    registration = registrations.get(timeout=1)
                    assert done.wait(1), 'Busy interface did not return a bounded failure'
                    result = checked_reply(replies, 503)
                    assert not result['success'] and 'busy' in result['error']
                    assert 'driver was not changed' in result['error']
                    assert registration.state == 'cancelled'
                    app.processEvents()  # Deliver the previously stalled signal.
                    assert not window.driver_name.text()
                    assert window.current_session_id == old_session
                    assert not window.session_active
        ''')

    def test_missing_interface_reports_unavailable_from_both_endpoints(self):
        self.run_scenario(r'''
            for missing in ['ui_bridge', 'main_window']:
                with patch.object(s, missing, None), s.app.test_client() as client:
                    for endpoint in ['/set_driver', '/api/session/start_remote']:
                        response = client.post(endpoint, json={'driver_name': 'Not Ready'})
                        assert response.status_code == 503, response.get_json()
                        result = response.get_json()
                        assert not result['success'] and 'not ready' in result['error']
            assert registrations.empty() and not window.driver_name.text()
        ''')

    def test_concurrent_same_driver_requests_keep_one_session_and_original_metadata(self):
        self.run_scenario(r'''
            window.lead_questions = {'questions': [{'prompt': 'Experience', 'options': ['New', 'Returning']}]}
            first = dict(driver_name='Alice', driver_email='first@example.com',
                         driver_phone='+14155550123', survey_answers={'0': 'New'})
            duplicate = dict(driver_name=' aLiCe ', driver_email='retry@example.com',
                             driver_phone='+14155550987', queue_id='retry-queue')
            with patch.object(s, 'is_lead_gen_enabled', return_value=True), \
                    patch.object(s, 'is_email_collection_enabled', return_value=True), \
                    patch.object(s, 'is_phone_collection_enabled', return_value=True), \
                    patch.object(s, 'append_lead_to_csv') as append, \
                    patch.object(window, 'update_driver_name', wraps=window.update_driver_name) as update:
                done_first, reply_first = begin_request('/set_driver', first)
                first_registration = registrations.get(timeout=1)
                done_again, reply_again = begin_request('/api/session/start_remote', duplicate)
                second_registration = registrations.get(timeout=1)
                assert first_registration.state == second_registration.state == 'queued'
                assert not done_first.is_set() and not done_again.is_set()
                spin_until(lambda: done_first.is_set() and done_again.is_set())
                original = checked_reply(reply_first, 200)
                repeated = checked_reply(reply_again, 200)
                assert original['applied'] and not original['already_active']
                assert repeated['applied'] and repeated['already_active']
                assert original['session_id'] == repeated['session_id'] == window.current_session_id
                assert original['driver_name'] == repeated['driver_name'] == 'Alice'
                update.assert_called_once_with('Alice')
                assert window.last_driver_email == 'first@example.com'
                assert window.last_driver_phone == '+14155550123'
                assert window.last_survey_answers == {'Experience': 'New'}
                assert window.assigned_queue_id is None
                append.assert_called_once_with('Alice', 'first@example.com', '+14155550123', {'Experience': 'New'})
        ''')

    def test_applying_timeout_reports_uncertain_state_then_acknowledges_completion(self):
        self.run_scenario(r'''
            apply = s.apply_driver_registration
            original_update = window.update_driver_name
            done = None
            def slow_update(name):
                assert done.wait(1), 'The HTTP worker did not time out while Qt applied the request'
                original_update(name)
            with patch.object(s, 'apply_driver_registration', side_effect=lambda **data: apply(timeout=0.05, **data)), \
                    patch.object(window, 'update_driver_name', side_effect=slow_update):
                done, replies = begin_request('/set_driver', {'driver_name': 'Slow Apply'})
                registration = registrations.get(timeout=1)
                app.processEvents()
                result = checked_reply(replies, 503)
                assert not result['success'] and 'still processing' in result['error']
                assert 'Check the simulator' in result['error']
                assert registration.done.is_set() and registration.state == 'done'
                assert registration.result[0]['applied']
                assert window.driver_name.text() == 'Slow Apply'
        ''')

    def test_retry_during_intro_video_does_not_replay_video_or_replace_queue_assignment(self):
        self.run_scenario(r'''
            intro = s.get_settings_path().parent / 'intro.mp4'
            intro.write_bytes(b'placeholder; media playback is mocked')
            window.video_required_toggle.setChecked(True)
            window.video_path_input.setText(str(intro))
            with patch.object(window, 'play_pre_session_video') as play:
                done, replies = begin_request('/api/session/start_remote',
                    {'driver_name': 'Intro Driver', 'queue_id': 'original-queue'})
                registrations.get(timeout=1)
                spin_until(done.is_set)
                original = checked_reply(replies, 200)
                assert original['applied'] and not original['already_active']
                assert not window.session_active and not window.timer_started

                done, replies = begin_request('/set_driver', {'driver_name': ' intro driver '})
                registrations.get(timeout=1)
                spin_until(done.is_set)
                repeated = checked_reply(replies, 200)
                assert repeated['already_active'] and repeated['applied']
                assert repeated['session_id'] == original['session_id'] == window.current_session_id
                assert window.driver_name.text() == 'Intro Driver'
                assert window.assigned_queue_id == 'original-queue'
                assert not window.session_active and not window.timer_started
                play.assert_called_once_with(str(intro))
        ''')
