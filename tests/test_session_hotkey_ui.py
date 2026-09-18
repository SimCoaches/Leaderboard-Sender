"""Real Qt session lifecycle, with keyboard delivery and network I/O mocked."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class SessionEndUITests(unittest.TestCase):
    def test_settings_and_all_session_end_paths(self):
        script = r'''
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, Mock
from contextlib import ExitStack
import lap_time_sender as s

app = s.QApplication([])
if os.environ.get('HOTKEY_PREVIEW'):
    from PyQt6.QtGui import QFontDatabase, QFont
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/seguisb.ttf')
    app.setFont(QFont('Segoe UI', 10))
with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
    stack.enter_context(patch.object(s, 'get_settings_path', return_value=Path(directory) / 'settings.json'))
    s.save_app_settings({'simulator_id': 2})
    for target in [s.threading.Thread, s.QThread, s.QTimer]:
        stack.enter_context(patch.object(target, 'start'))
    timers = stack.enter_context(patch.object(s.QTimer, 'singleShot'))
    for name in ['configure_firewall', 'mdns_service']:
        stack.enter_context(patch.object(s, name))
    stack.enter_context(patch.object(s, 'select_web_interface_port', return_value=5001))
    stack.enter_context(patch.object(s, 'get_local_ip', return_value='192.168.0.20'))
    for name in ['show_waiting_screen', 'start_monitor', '_notify_session_ended',
                 '_notify_session_started', 'send_lap_time', '_log_sector_run']:
        stack.enter_context(patch.object(s.LapTimeSenderWindow, name))
    send = stack.enter_context(patch.object(s, 'send_game_shortcut', return_value=(True, 'Sent Ctrl+R')))
    window = s.LapTimeSenderWindow()
    window.waiting_overlay = Mock()
    assert not window.session_end_hotkey_enabled
    assert window.session_end_shortcut == 'Ctrl+R'
    assert window.session_end_delay_ms == 1500
    assert window.settings_tabs.count() == 4
    assert window.settings_tabs.tabText(3) == 'Session End'
    assert window.session_end_game_tabs.tabText(0) == 'Assetto Corsa'
    assert window.session_end_game_tabs.tabText(1) == 'iRacing'

    def driver(name):
        window.update_driver_name(name)
        assert window.session_active

    def shortcut_callback():
        return [call.args[1] for call in timers.call_args_list
                if call.args[0] == window.session_end_delay_ms][-1]

    def deliver_shortcut():
        callback = shortcut_callback()
        before = send.call_count
        callback()
        assert send.call_count == before + 1
        callback()
        assert send.call_count == before + 1  # Even a repeated callback cannot resend.

    # Default off, ordinary end behavior preserved.
    driver('Disabled')
    window.end_session()
    send.assert_not_called()
    assert not window.session_active and not window.driver_name.text()
    window.waiting_overlay.show_session_complete.assert_called()

    window.session_end_hotkey_checkbox.setChecked(True)
    window.session_end_shortcut_input.setText('Ctrl+R')
    window._save_session_end_hotkey()
    assert window.session_end_hotkey_enabled
    settings = s.load_app_settings()
    assert settings['session_end_hotkey_enabled'] and settings['session_end_shortcut'] == 'Ctrl+R'
    assert settings['session_end_delay_ms'] == 1500
    assert settings['simulator_id'] == 2
    # Restore settings using the real Qt controls.
    window.session_end_hotkey_enabled = False
    window.session_end_shortcut = 'F7'
    window._load_saved_settings()
    assert window.session_end_hotkey_enabled and window.session_end_shortcut == 'Ctrl+R'
    # Invalid edits and failed writes leave the last saved action intact.
    window.session_end_shortcut_input.setText('Alt+F4')
    window._save_session_end_hotkey()
    assert 'Not saved' in window.session_end_hotkey_status.text()
    assert window.session_end_shortcut == 'Ctrl+R'
    window.session_end_shortcut_input.setText('F7')
    with patch.object(s, 'save_app_settings', return_value=False):
        window._save_session_end_hotkey()
    assert window.session_end_shortcut == 'Ctrl+R'
    window.session_end_shortcut_input.setText('Ctrl+R')

    # Real admin signal, duplicate request, delay, and overlay ordering.
    driver('Admin')
    window.waiting_overlay.reset_mock()
    def check_order(*args):
        window.waiting_overlay.show_session_complete.assert_called()
        assert not window.session_active and not window.driver_name.text()
        return True, 'Sent Ctrl+R'
    send.side_effect = check_order
    s.ui_bridge.force_end_session.emit()
    send.assert_not_called()
    s.ui_bridge.force_end_session.emit()
    deliver_shortcut()
    send.assert_called_once_with('Assetto Corsa', 'Ctrl+R')
    send.side_effect = None
    assert send.call_count == 1
    assert not window.session_active and not window.driver_name.text()

    # Expired timer sends once without requiring a lap completion.
    driver('Timer')
    window.rotation_mode_selector.setCurrentText('Time')
    window.timer_started = True
    window.timer_end_timestamp = 1
    window.check_session_timeout()
    window.check_session_timeout()
    assert send.call_count == 1
    deliver_shortcut()
    assert send.call_count == 2

    # Lap-limit path.
    driver('Laps')
    window.lap_rotation_active = True
    window.lap_count_started = True
    window.lap_rotation_count = 0
    window.lap_rotation_target = 1
    window.on_lap_completed(65.3, 'Laps')
    assert send.call_count == 2
    deliver_shortcut()
    assert send.call_count == 3 and not window.session_active
    window.lap_rotation_active = False

    # Sector end/reset callbacks share a captured session identity.
    for outcome in ('failed', 'completed'):
        driver(outcome)
        timers.reset_mock()
        if outcome == 'failed':
            window.on_challenge_failed(0, 20, 30)
        else:
            window.on_challenge_completed(65)
        callbacks = {call.args[0]: call.args[1] for call in timers.call_args_list}
        callbacks[400]()
        deliver_shortcut()
        with patch.object(s, 'ac_app_alive') as alive, patch.object(window, 'restart_ac_session') as restart:
            callbacks[1400]()
            alive.assert_not_called()
            restart.assert_not_called()
    assert send.call_count == 5

    # A blocked shortcut does not prevent driver clearing/availability.
    driver('Blocked')
    send.return_value = (False, 'Windows blocked input')
    window.end_session()
    deliver_shortcut()
    assert not window.session_active and not window.driver_name.text()
    assert 'blocked' in window.status_label.text()
    assert send.call_count == 6
    send.return_value = (True, 'Sent Ctrl+R')

    # An old delayed result cannot end or reset the next driver.
    driver('Old sector')
    timers.reset_mock()
    window.on_challenge_completed(65)
    callbacks = {call.args[0]: call.args[1] for call in timers.call_args_list}
    driver('Next driver')
    callbacks[400]()
    with patch.object(window, 'restart_ac_session') as restart:
        callbacks[1400]()
        restart.assert_not_called()
    assert window.session_active and window.driver_name.text() == 'Next driver'
    assert send.call_count == 6
    window._test_session_end_hotkey()
    assert send.call_count == 6  # Testing cannot interrupt an active session.

    # Old interstitial completion cannot cover a new driver's game.
    timers.reset_mock()
    window.end_session()
    waiting_callback = [c.args[1] for c in timers.call_args_list if c.args[0] == 4000][-1]
    old_shortcut_callback = shortcut_callback()
    driver('After interstitial')
    window.waiting_overlay.reset_mock()
    waiting_callback()
    old_shortcut_callback()
    window.waiting_overlay.show.assert_not_called()
    assert send.call_count == 6
    window.end_session()
    deliver_shortcut()
    before_test = send.call_count
    window._test_session_end_hotkey()
    window._test_session_end_hotkey()
    assert send.call_count == before_test
    deliver_shortcut()
    assert send.call_count == before_test + 1

    # Consecutive Test button presses each use the same saved chord and timing.
    for _ in range(10):
        window._test_session_end_hotkey()
        deliver_shortcut()
    assert send.call_count == before_test + 11

    # Changed games and saved settings invalidate a queued shortcut.
    window._test_session_end_hotkey()
    old_callback = shortcut_callback()
    window.game_selector.blockSignals(True)  # No game-install dialogs in this lifecycle test.
    window.game_selector.setCurrentText('BeamNG.drive')
    old_callback()
    assert send.call_count == before_test + 11
    window.game_selector.setCurrentText('Assetto Corsa')
    window.game_selector.blockSignals(False)
    window._test_session_end_hotkey()
    old_callback = shortcut_callback()
    window.session_end_delay_selector.setCurrentIndex(window.session_end_delay_selector.findData(2000))
    window._save_session_end_hotkey()
    old_callback()
    assert send.call_count == before_test + 11
    window._load_saved_settings()
    assert window.session_end_delay_ms == 2000
    window._test_session_end_hotkey()
    assert timers.call_args.args[0] == 2000
    deliver_shortcut()

    # Disabled sector behavior still invokes the original reset.
    window.session_end_hotkey_checkbox.setChecked(False)
    window._save_session_end_hotkey()
    driver('Original reset')
    window.end_session()
    with patch.object(s, 'ac_app_alive', return_value=False), patch.object(window, 'restart_ac_session') as restart:
        window._reset_car_for_next_driver(window.current_session_id)
        restart.assert_called_once()

    if os.environ.get('HOTKEY_PREVIEW'):
        window.settings_tabs.setCurrentIndex(3)
        window.settings_tabs.resize(1050, 330)
        window.settings_tabs.grab().save(os.environ['HOTKEY_PREVIEW'])
    window.close()
print('Session hotkey settings, admin/timer/lap/sector ends and stale-session guards passed')
'''
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, '-'], input=script, encoding='utf-8',
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
