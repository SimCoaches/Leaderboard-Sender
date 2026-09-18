"""Receiver timeouts must not block the real Qt driver-registration signal."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ReceiverConnectionUITests(unittest.TestCase):
    def test_slow_receivers_keep_registration_responsive_and_ignore_stale_results(self):
        script = r'''
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch, Mock
from contextlib import ExitStack
import lap_time_sender as s

app = s.QApplication([])
with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
    stack.enter_context(patch.object(s, 'get_settings_path', return_value=Path(directory) / 'settings.json'))
    stack.enter_context(patch.object(s, 'get_local_ip', return_value='192.168.0.20'))
    stack.enter_context(patch.object(s, 'select_web_interface_port', return_value=5001))
    for name in ['configure_firewall', 'mdns_service']:
        stack.enter_context(patch.object(s, name))
    for name in ['show_waiting_screen', 'start_monitor', '_notify_session_started']:
        stack.enter_context(patch.object(s.LapTimeSenderWindow, name))
    with patch.object(s.threading.Thread, 'start'), patch.object(s.QThread, 'start'), \
            patch.object(s.QTimer, 'start'), patch.object(s.QTimer, 'singleShot'):
        window = s.LapTimeSenderWindow()
    window.waiting_overlay = Mock()
    button = window.findChild(s.QPushButton, 'test-connection')
    old_urls = ['http://old-one:5000', 'http://old-two:5000']
    new_url = 'http://new:5000'
    failed_url = 'http://unreachable:5000'
    urls = old_urls + [new_url, failed_url]
    started = {url: threading.Event() for url in urls}
    release = {url: threading.Event() for url in urls}
    worker_threads = []
    gui_thread = threading.get_ident()

    def probe(url, timeout):
        worker_threads.append(threading.get_ident())
        started[url].set()
        assert release[url].wait(5), 'Test did not release the simulated HTTP request'
        if url == failed_url:
            raise s.requests.exceptions.Timeout()
        return Mock(status_code=200)

    def spin_until(predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            assert time.monotonic() < deadline, 'Qt did not deliver the expected update'
            app.processEvents()
            time.sleep(0.005)

    with patch.object(s.requests, 'get', side_effect=probe) as get, \
            patch.object(s.QTimer, 'singleShot') as timers:
        try:
            window.current_server_url = 'http://previously-saved:5000'
            window.server_url.setText(', '.join(old_urls))
            start = time.monotonic()
            window.test_and_save_connection()
            assert time.monotonic() - start < 1, 'Receiver testing blocked the GUI thread'
            assert all(started[url].wait(1) for url in old_urls), 'Receivers should be probed concurrently'
            assert not button.isEnabled() and button.text() == 'Testing...'
            window.test_and_save_connection()  # Duplicate discovery event is coalesced.
            assert get.call_count == 2

            # A tablet-style signal can still update the driver while both
            # receiver HTTP requests are stuck waiting for the network.
            registration = threading.Thread(target=lambda: s.ui_bridge.driver_registered.emit('Quick Driver'))
            registration.start()
            registration.join()
            spin_until(lambda: window.driver_name.text() == 'Quick Driver')
            assert not any(event.is_set() for event in release.values())

            window.server_url.setText(new_url)
            window.test_and_save_connection()
            assert not started[new_url].is_set(), 'Changed URLs should share the in-flight guard'
            for url in old_urls:
                release[url].set()
            spin_until(lambda: started[new_url].is_set())
            assert window.current_server_url == 'http://previously-saved:5000', 'Stale probe overwrote saved URL'
            assert button.text() == 'Testing...' and not button.isEnabled()
            release[new_url].set()
            spin_until(lambda: button.text() == 'Connected!')
            assert window.current_server_url == new_url
            assert all(worker != gui_thread for worker in worker_threads)
            previous_reset = [call.args[1] for call in timers.call_args_list if call.args[0] == 2000][-1]

            window.server_url.setText(failed_url)
            window.test_and_save_connection()
            assert started[failed_url].wait(1)
            previous_reset()
            assert button.text() == 'Testing...' and not button.isEnabled(), 'Old reset interrupted a new test'
            release[failed_url].set()
            spin_until(lambda: button.text() == 'No Connection!')
            assert 'timeout' in button.toolTip()
            assert window.current_server_url == failed_url

            window.server_url.setText('')
            window.test_and_save_connection()
            spin_until(lambda: window._receiver_test_url is None)
            assert button.text() == 'No Connection!'
            assert 'Enter a leaderboard receiver URL' in button.toolTip()
        finally:
            for event in release.values():
                event.set()
            window.close()
print('Receiver timeouts, Qt registration, coalesced updates and stale-result guards passed')
'''
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, '-'], input=script, encoding='utf-8',
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
