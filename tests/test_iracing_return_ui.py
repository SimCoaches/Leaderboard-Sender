"""Real Qt lifecycle with brake keys, keyboard sequence and network mocked."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


FIXTURE = r'''
from contextlib import ExitStack, contextmanager
from pathlib import Path
import tempfile
import time
from unittest.mock import Mock, patch
import lap_time_sender as s

app = s.QApplication([])

@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        stack.enter_context(patch.object(s, 'get_settings_path', return_value=Path(directory) / 'settings.json'))
        s.save_app_settings({'simulator_id': 2, 'lead_gen_enabled': False})
        for target in (s.threading.Thread, s.QThread):
            stack.enter_context(patch.object(target, 'start'))
        timers = stack.enter_context(patch.object(s.QTimer, 'singleShot'))
        for name in ('configure_firewall', 'mdns_service'):
            stack.enter_context(patch.object(s, name))
        stack.enter_context(patch.object(s, 'select_web_interface_port', return_value=5001))
        stack.enter_context(patch.object(s, 'get_local_ip', return_value='192.168.0.20'))
        for name in ('show_waiting_screen', 'start_monitor', '_notify_session_ended',
                     '_notify_session_started', 'send_lap_time', '_log_sector_run'):
            stack.enter_context(patch.object(s.LapTimeSenderWindow, name))
        shortcut = stack.enter_context(patch.object(s, 'send_game_shortcut', return_value=(True, 'Sent Ctrl+R')))
        brake = Mock()
        brake_ctor = stack.enter_context(patch.object(s, 'KeyboardBrake', return_value=brake))
        made = []
        def sequence_factory(*args, **kwargs):
            sequence = Mock(done=False, success=False, cleanup_pending=False,
                            message='Braking in iRacing; waiting for the car to stop.')
            def cancel(reason):
                sequence.done = True
                sequence.message = reason
            sequence.cancel.side_effect = cancel
            made.append(sequence)
            return sequence
        sequence_ctor = stack.enter_context(patch.object(s, 'IracingReturnSequence', side_effect=sequence_factory))
        with patch.object(s.QTimer, 'start'):
            window = s.LapTimeSenderWindow()
        window.waiting_overlay = Mock()
        snapshot = dict(connected=True, in_car=True, sampled_at=time.monotonic(),
                        speed_mps=20.0, brake=0.0,
                        in_pit_stall=False, tow_time=0.0, session_num=0)
        telemetry = stack.enter_context(patch.object(window, '_iracing_snapshot', return_value=snapshot))
        def callback():
            return [call.args[1] for call in timers.call_args_list
                    if call.args[0] == (0
                       if window.game_selector.currentText() == 'iRacing'
                       else window.session_end_delay_ms)][-1]
        def start():
            window.session_active = False
            window.driver_name.clear()
            window.game_selector.setCurrentText('iRacing')
            snapshot['sampled_at'] = time.monotonic()
            window._test_iracing_return()
            callback()()
            assert window._iracing_return is made[-1]
            assert window.iracing_return_timer.isActive()
            return made[-1]
        try:
            yield window, timers, callback, start, shortcut, brake_ctor, sequence_ctor, telemetry, made
        finally:
            window.close()
'''


class IRacingReturnUITests(unittest.TestCase):
    def run_scenario(self, scenario):
        script = FIXTURE + ('\nwith fixture() as (window, timers, callback, start, shortcut, '
                            'brake_ctor, sequence_ctor, telemetry, made):\n') + textwrap.indent(
                                textwrap.dedent(scenario), '    ')
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, '-'], input=script, encoding='utf-8',
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_game_controls_are_grouped_under_session_end(self):
        self.run_scenario(r'''
            assert window.settings_tabs.count() == 4
            assert window.settings_tabs.tabText(3) == 'Session End'
            games = window.session_end_game_tabs
            assert games.count() == 2
            assert [games.tabText(i) for i in range(2)] == ['Assetto Corsa', 'iRacing']
            assert window.settings_tabs.widget(3).isAncestorOf(games)
            assert games.widget(0).isAncestorOf(window.session_end_shortcut_input)
            assert games.widget(0).isAncestorOf(window.session_end_delay_selector)
            assert games.widget(1).isAncestorOf(window.iracing_brake_shortcut_input)
            assert not hasattr(window, 'iracing_clutch_checkbox')
            assert not hasattr(window, 'iracing_clutch_shortcut_input')
            assert games.widget(1).isAncestorOf(window.iracing_reset_input)
            assert games.widget(1).isAncestorOf(window.iracing_end_delay_input)
            assert not hasattr(window, 'iracing_brake_device_selector')
            assert not hasattr(window, 'iracing_brake_axis_selector')
            brake_ctor.assert_not_called()
            sequence_ctor.assert_not_called()
        ''')

    def test_saved_settings_round_trip_and_invalid_hold_preserves_saved_values(self):
        self.run_scenario(r'''
            assert not window.iracing_auto_pit_enabled
            assert window.iracing_reset_shortcut == 'F9'
            assert window.iracing_reset_hold == 3.5
            assert window.iracing_brake_shortcut == 'F8'
            assert window.iracing_end_delay_ms == 1500
            window.iracing_auto_pit_checkbox.setChecked(True)
            window.iracing_reset_input.setText('F10')
            window.iracing_brake_shortcut_input.setText('F11')
            window.iracing_end_delay_input.setValue(2)
            window.iracing_hold_input.setText('5.5')
            window._save_iracing_return_settings()
            saved = s.load_app_settings()
            assert saved['simulator_id'] == 2
            assert saved['iracing_auto_pit_enabled'] is True
            assert saved['iracing_reset_shortcut'] == 'F10'
            assert saved['iracing_brake_shortcut'] == 'F11'
            assert saved['iracing_end_delay_ms'] == 2000
            assert saved['iracing_reset_hold'] == 5.5
            window._load_saved_settings()
            assert window.iracing_auto_pit_checkbox.isChecked()
            assert window.iracing_reset_input.text() == 'F10'
            assert window.iracing_brake_shortcut_input.text() == 'F11'
            assert window.iracing_end_delay_input.value() == 2
            assert window.session_end_delay_ms == 1500
            assert window.iracing_hold_input.text() == '5.5'
            for invalid in ('0', '0.99', '10.01', '-1', 'nan', 'inf', 'not a number'):
                window.iracing_hold_input.setText(invalid)
                window._save_iracing_return_settings()
                assert 'Not saved' in window.iracing_return_status.text(), invalid
                assert s.load_app_settings() == saved
                assert window.iracing_reset_hold == 5.5
            window.iracing_hold_input.setText('3')
            window.iracing_reset_input.setText('Alt+F4')
            window._save_iracing_return_settings()
            assert 'Not saved' in window.iracing_return_status.text()
            assert s.load_app_settings() == saved
            window.iracing_reset_input.setText('F7')
            with patch.object(s, 'save_app_settings', return_value=False):
                window._save_iracing_return_settings()
            assert 'Could not save' in window.iracing_return_status.text()
            assert window.iracing_reset_shortcut == 'F10'
            assert window.iracing_reset_hold == 5.5
            for boundary in ('1', '10'):
                window.iracing_hold_input.setText(boundary)
                window._save_iracing_return_settings()
                assert window.iracing_reset_hold == float(boundary)
            brake_ctor.assert_not_called()
            sequence_ctor.assert_not_called()
        ''')

    def test_invalid_stored_settings_disable_automatic_control(self):
        self.run_scenario(r'''
            invalid_settings = (
                {'iracing_brake_shortcut': ''}, {'iracing_brake_shortcut': 'Alt+F4'},
                {'iracing_brake_shortcut': None},
                {'iracing_brake_shortcut': 'F9'},  # Reset action is held while braking.
                {'iracing_reset_hold': float('nan')}, {'iracing_reset_hold': 0.5},
                {'iracing_reset_hold': 11}, {'iracing_reset_shortcut': 'Alt+F4'},
                {'iracing_end_delay_ms': float('nan')}, {'iracing_end_delay_ms': 999},
                {'iracing_end_delay_ms': 2001},
            )
            for invalid in invalid_settings:
                settings = dict(iracing_auto_pit_enabled=True, iracing_brake_shortcut='F8',
                                iracing_reset_shortcut='F9')
                settings.update(invalid)
                with patch.object(s, 'load_app_settings', return_value=settings):
                    window._load_saved_settings()
                assert not window.iracing_auto_pit_enabled, invalid
                assert not window.iracing_auto_pit_checkbox.isChecked()
                assert window.iracing_reset_shortcut == 'F9'
                assert window.iracing_reset_hold == 3.5
                assert window.iracing_brake_shortcut == 'F8'
                assert window.iracing_end_delay_ms == 1500
            brake_ctor.assert_not_called()
        ''')

    def test_legacy_virtual_brake_settings_require_saving_keyboard_mapping(self):
        self.run_scenario(r'''
            settings = dict(simulator_id=2, iracing_auto_pit_enabled=True,
                            iracing_reset_shortcut='F12', iracing_reset_hold=4.0,
                            iracing_brake_device=3, iracing_brake_axis='RY',
                            session_end_delay_ms=2000)
            s.save_app_settings(settings)
            window._load_saved_settings()
            assert not window.iracing_auto_pit_enabled
            assert not window.iracing_auto_pit_checkbox.isChecked()
            assert window.iracing_brake_shortcut == 'F8'
            assert window.iracing_reset_shortcut == 'F12'
            assert window.iracing_reset_hold == 4.0
            assert window.iracing_end_delay_ms == 2000
            assert window.iracing_end_delay_input.value() == 2
            assert window.session_end_delay_ms == 2000
            brake_ctor.assert_not_called()
            window.iracing_auto_pit_checkbox.setChecked(True)
            window.iracing_brake_shortcut_input.setText('F10')
            window.iracing_end_delay_input.setValue(1)
            window._save_iracing_return_settings()
            saved = s.load_app_settings()
            assert saved['iracing_auto_pit_enabled'] is True
            assert saved['iracing_brake_shortcut'] == 'F10'
            assert saved['iracing_end_delay_ms'] == 1000
            assert saved['session_end_delay_ms'] == 2000
            assert 'iracing_brake_device' not in saved
            assert 'iracing_brake_axis' not in saved
            window._load_saved_settings()
            assert window.iracing_auto_pit_enabled
            assert window.iracing_brake_shortcut == 'F10'
            assert window.iracing_end_delay_ms == 1000
        ''')

    def test_old_modifier_reset_requires_rebinding_and_preserves_other_preferences(self):
        self.run_scenario(r'''
            settings = dict(simulator_id=2, iracing_auto_pit_enabled=True,
                            iracing_reset_shortcut='Shift+R', iracing_brake_shortcut='F10',
                            iracing_reset_hold=4.0, iracing_end_delay_ms=2000)
            s.save_app_settings(settings)
            window._load_saved_settings()
            assert not window.iracing_auto_pit_enabled
            assert not window.iracing_auto_pit_checkbox.isChecked()
            assert window.iracing_reset_shortcut == 'F9'
            assert window.iracing_reset_input.text() == 'F9'
            assert window.iracing_brake_shortcut == 'F10'
            assert window.iracing_reset_hold == 4.0
            assert window.iracing_end_delay_ms == 2000
            assert 'setup' in window.iracing_return_status.text().lower()
            assert 'Shift' in window.iracing_return_status.text()
            brake_ctor.assert_not_called()
            sequence_ctor.assert_not_called()
            window.iracing_auto_pit_checkbox.setChecked(True)
            window._save_iracing_return_settings()
            saved = s.load_app_settings()
            assert saved['iracing_reset_shortcut'] == 'F9'
            assert saved['iracing_brake_shortcut'] == 'F10'
            window._load_saved_settings()
            assert window.iracing_auto_pit_enabled
        ''')

    def test_existing_brake_and_reset_preferences_remain_enabled_without_clutch_configuration(self):
        self.run_scenario(r'''
            for old_reset in ('F12', 'F7'):
                settings = dict(iracing_auto_pit_enabled=True, iracing_brake_shortcut='F8',
                                iracing_reset_shortcut=old_reset, iracing_reset_hold=3.5,
                                iracing_end_delay_ms=1500)
                with patch.object(s, 'load_app_settings', return_value=settings):
                    window._load_saved_settings()
                assert window.iracing_auto_pit_enabled
                assert window.iracing_auto_pit_checkbox.isChecked()
                assert window.iracing_reset_shortcut == old_reset
                assert window.iracing_brake_shortcut == 'F8'
                assert window.iracing_reset_hold == 3.5
                assert window.iracing_end_delay_ms == 1500
            brake_ctor.assert_not_called()
            sequence_ctor.assert_not_called()
        ''')

    def test_brake_and_reset_conflicts_do_not_replace_saved_mapping(self):
        self.run_scenario(r'''
            window._save_iracing_return_settings()
            saved = s.load_app_settings()
            for brake_key, reset_key in (
                    ('F8', 'F8'), ('F8', 'Shift+R'), ('F8', 'Ctrl+F9'),
                    ('Shift+F8', 'F9'), ('Alt+F8', 'F9')):
                window.iracing_brake_shortcut_input.setText(brake_key)
                window.iracing_reset_input.setText(reset_key)
                window._save_iracing_return_settings()
                assert 'Not saved' in window.iracing_return_status.text(), (brake_key, reset_key)
                assert s.load_app_settings() == saved
                assert window.iracing_brake_shortcut == 'F8'
                assert window.iracing_reset_shortcut == 'F9'
            brake_ctor.assert_not_called()
        ''')

    def test_saved_delays_and_test_buttons_route_only_to_their_game(self):
        self.run_scenario(r'''
            window.session_end_delay_selector.setCurrentIndex(
                window.session_end_delay_selector.findData(1000))
            window._save_session_end_hotkey()
            window.iracing_end_delay_input.setValue(2)
            window._save_iracing_return_settings()
            assert window.session_end_delay_ms == 1000
            assert window.iracing_end_delay_ms == 2000
            window.game_selector.setCurrentText('iRacing')
            timers.reset_mock()
            window._test_session_end_hotkey()
            assert 'Assetto Corsa' in window.session_end_hotkey_status.text()
            timers.assert_not_called()
            shortcut.assert_not_called()
            sequence_ctor.assert_not_called()
            window._test_iracing_return()
            assert timers.call_args.args[0] == 0  # Brake starts immediately; delay is before Reset.
            callback()()
            sequence_ctor.assert_called_once_with(brake_ctor.return_value, 'F9', 3.5,
                                                  reset_delay_seconds=2.0)
            brake_ctor.assert_called_once_with('F8')
            window._cancel_iracing_return_clicked()
            window.game_selector.setCurrentText('Assetto Corsa')
            timers.reset_mock()
            window._test_iracing_return()
            timers.assert_not_called()
            window._test_session_end_hotkey()
            assert timers.call_args.args[0] == 1000
            callback()()
            shortcut.assert_called_once_with('Assetto Corsa', 'Ctrl+R')
            assert sequence_ctor.call_count == 1
        ''')

    def test_assetto_automatic_shortcut_never_runs_for_iracing_when_return_disabled(self):
        self.run_scenario(r'''
            window.session_end_hotkey_checkbox.setChecked(True)
            window._save_session_end_hotkey()
            assert not window.iracing_auto_pit_enabled
            window.game_selector.setCurrentText('iRacing')
            window.update_driver_name('iRacing Driver')
            timers.reset_mock()
            window.end_session()
            assert window._pending_session_shortcut is None
            for call in timers.call_args_list:
                if call.args[0] == window.session_end_delay_ms:
                    call.args[1]()
            shortcut.assert_not_called()
            brake_ctor.assert_not_called()
            sequence_ctor.assert_not_called()
        ''')

    def test_iracing_session_end_starts_one_sequence_and_assetto_keeps_delayed_shortcut(self):
        self.run_scenario(r'''
            window.iracing_auto_pit_checkbox.setChecked(True)
            window._save_iracing_return_settings()
            assert not window.session_end_hotkey_enabled
            window.game_selector.setCurrentText('iRacing')
            window.update_driver_name('iRacing Driver')
            window.end_session()
            pending = callback()
            assert not window.session_active and not window.driver_name.text()
            assert any(call.args[0] == 0 for call in timers.call_args_list)
            brake_ctor.assert_not_called()
            sequence_ctor.assert_not_called()
            window.end_session()  # Repeated admin/session-end callback must not duplicate control.
            pending()
            sequence_ctor.assert_called_once_with(brake_ctor.return_value, 'F9', 3.5,
                                                  reset_delay_seconds=1.5)
            brake_ctor.assert_called_once_with('F8')
            assert window.iracing_return_timer.isActive()
            pending()
            window._test_iracing_return()
            window._test_session_end_hotkey()
            assert sequence_ctor.call_count == 1
            assert 'already running' in window.iracing_return_status.text()
            shortcut.assert_not_called()
            window._cancel_iracing_return_clicked()
            window.game_selector.setCurrentText('Assetto Corsa')
            window.session_end_hotkey_checkbox.setChecked(True)
            window._save_session_end_hotkey()
            window.update_driver_name('Assetto Driver')
            window.end_session()
            shortcut.assert_not_called()
            callback()()
            shortcut.assert_called_once_with('Assetto Corsa', 'Ctrl+R')
            assert sequence_ctor.call_count == 1
        ''')

    def test_manual_test_requires_iracing_and_no_registered_or_active_driver(self):
        self.run_scenario(r'''
            timers.reset_mock()
            window._test_iracing_return()
            assert 'Select iRacing' in window.iracing_return_status.text()
            timers.assert_not_called()
            window.game_selector.setCurrentText('iRacing')
            for active, driver in ((True, ''), (False, 'Registered Driver')):
                window.session_active = active
                window.driver_name.setText(driver)
                window._test_iracing_return()
                assert 'End the registered driver' in window.iracing_return_status.text()
                timers.assert_not_called()
            sequence = start()  # Manual testing is supported with automatic mode off.
            assert not window.iracing_auto_pit_enabled
            sequence.tick.assert_called_once_with(telemetry.return_value)
            shortcut.assert_not_called()
        ''')

    def test_pending_callback_is_invalidated_on_operator_cancel_and_driver_change(self):
        self.run_scenario(r'''
            window.game_selector.setCurrentText('iRacing')
            for action in ('operator', 'driver', 'game', 'iracing settings', 'shortcut settings', 'close'):
                window.session_active = False
                window.driver_name.clear()
                window.game_selector.setCurrentText('iRacing')
                window._test_iracing_return()
                pending = callback()
                if action == 'operator':
                    window._cancel_iracing_return_clicked()
                elif action == 'driver':
                    window.update_driver_name('Next Driver')
                elif action == 'game':
                    window.game_selector.setCurrentText('Assetto Corsa')
                elif action == 'iracing settings':
                    window._save_iracing_return_settings()
                elif action == 'shortcut settings':
                    window._save_session_end_hotkey()
                else:
                    window.close()
                pending()
                assert window._pending_session_shortcut is None, action
                assert not window.iracing_return_timer.isActive(), action
            brake_ctor.assert_not_called()
            sequence_ctor.assert_not_called()
            shortcut.assert_not_called()
        ''')

    def test_running_sequence_releases_controls_on_all_lifecycle_changes(self):
        self.run_scenario(r'''
            for action in ('operator', 'driver', 'game', 'iracing settings', 'shortcut settings', 'close'):
                sequence = start()
                if action == 'operator':
                    window._cancel_iracing_return_clicked()
                elif action == 'driver':
                    window.update_driver_name('Next Driver')
                elif action == 'game':
                    window.game_selector.setCurrentText('Assetto Corsa')
                elif action == 'iracing settings':
                    window._save_iracing_return_settings()
                elif action == 'shortcut settings':
                    window._save_session_end_hotkey()
                else:
                    window.close()
                sequence.cancel.assert_called_once()
                assert window._iracing_return is None, action
                assert not window.iracing_return_timer.isActive(), action
            shortcut.assert_not_called()
        ''')

    def test_failed_release_retains_sequence_until_operator_retry_succeeds(self):
        self.run_scenario(r'''
            sequence = start()
            warning = 'Windows blocked brake-key release. Release the brake key manually.'
            sequence.done, sequence.cleanup_pending, sequence.message = True, True, warning
            window._tick_iracing_return()
            assert window._iracing_return is sequence
            assert not window.iracing_return_timer.isActive()
            assert warning in window.iracing_return_status.text()
            timers.reset_mock()
            window._test_iracing_return()
            timers.assert_not_called()
            assert sequence_ctor.call_count == 1
            assert window._iracing_return is sequence
            def still_blocked(reason):
                sequence.message = warning
            sequence.cancel.side_effect = still_blocked
            window._cancel_iracing_return_clicked()
            sequence.cancel.assert_called_once()
            assert window._iracing_return is sequence
            assert warning in window.iracing_return_status.text()
            def release_succeeds(reason):
                sequence.cleanup_pending = False
                sequence.message = 'Controls released.'
            sequence.cancel.side_effect = release_succeeds
            window._cancel_iracing_return_clicked()
            assert sequence.cancel.call_count == 2
            assert window._iracing_return is None
            assert window.iracing_return_status.text() == 'Controls released.'
            assert not window.iracing_return_timer.isActive()
            shortcut.assert_not_called()
        ''')

    def test_settings_save_keeps_failed_release_warning_and_owned_sequence(self):
        self.run_scenario(r'''
            for save_name, status_name in (
                    ('_save_iracing_return_settings', 'iracing_return_status'),
                    ('_save_session_end_hotkey', 'session_end_hotkey_status')):
                sequence = start()
                warning = 'Windows blocked brake-key release. Release the brake key manually.'
                sequence.done, sequence.cleanup_pending, sequence.message = True, True, warning
                def still_blocked(reason):
                    sequence.message = warning
                sequence.cancel.side_effect = still_blocked
                window._tick_iracing_return()
                getattr(window, save_name)()
                sequence.cancel.assert_called_once()
                assert window._iracing_return is sequence, save_name
                assert not window.iracing_return_timer.isActive()
                assert warning in window.iracing_return_status.text(), save_name
                assert warning in getattr(window, status_name).text(), save_name
                def release_succeeds(reason):
                    sequence.cleanup_pending = False
                    sequence.message = 'Controls released.'
                sequence.cancel.side_effect = release_succeeds
                window._cancel_iracing_return_clicked()
                assert window._iracing_return is None
            shortcut.assert_not_called()
        ''')

    def test_unready_telemetry_does_not_create_brake_input(self):
        self.run_scenario(r'''
            window.game_selector.setCurrentText('iRacing')
            for snapshot in (None, {}, {'connected': False, 'in_car': True},
                             {'connected': True, 'in_car': False}):
                telemetry.return_value = snapshot
                window._test_iracing_return()
                callback()()
                assert 'No live iRacing' in window.iracing_return_status.text()
                assert not window.iracing_return_timer.isActive()
                assert window._iracing_return is None
            brake_ctor.assert_not_called()
            sequence_ctor.assert_not_called()
            shortcut.assert_not_called()
        ''')

    def test_stale_or_malformed_telemetry_does_not_create_brake_input(self):
        self.run_scenario(r'''
            window.game_selector.setCurrentText('iRacing')
            valid = dict(telemetry.return_value)
            for invalid in ({'sampled_at': time.monotonic() - 2},
                            {'sampled_at': time.monotonic() + 20},
                            {'sampled_at': float('nan')}, {'speed_mps': None},
                            {'speed_mps': float('nan')}, {'brake': True},
                            {'tow_time': '0'}, {'in_pit_stall': None}):
                telemetry.return_value = dict(valid, **invalid)
                window._test_iracing_return()
                callback()()
                assert window._iracing_return is None, invalid
                assert not window.iracing_return_timer.isActive(), invalid
            brake_ctor.assert_not_called()
            sequence_ctor.assert_not_called()
            shortcut.assert_not_called()
        ''')

    def test_legacy_clutch_settings_are_ignored_and_removed_without_changing_working_controls(self):
        self.run_scenario(r'''
            for enabled, key in ((True, 'F7'), (True, 'F10'), (False, ''), ('invalid', None)):
                settings = dict(simulator_id=2, iracing_auto_pit_enabled=True,
                                iracing_reset_shortcut='F7', iracing_brake_shortcut='F10',
                                iracing_reset_hold=4.5, iracing_end_delay_ms=2000,
                                iracing_clutch_enabled=enabled, iracing_clutch_shortcut=key)
                s.save_app_settings(settings)
                window._load_saved_settings()
                assert window.iracing_auto_pit_enabled
                assert window.iracing_auto_pit_checkbox.isChecked()
                assert window.iracing_brake_shortcut == 'F10'
                assert window.iracing_reset_shortcut == 'F7'
                assert window.iracing_reset_hold == 4.5
                assert window.iracing_end_delay_ms == 2000
                window._save_iracing_return_settings()
                saved = s.load_app_settings()
                assert 'iracing_clutch_enabled' not in saved
                assert 'iracing_clutch_shortcut' not in saved
                assert saved['iracing_auto_pit_enabled'] is True
                assert saved['iracing_brake_shortcut'] == 'F10'
                assert saved['iracing_reset_shortcut'] == 'F7'
                assert saved['iracing_reset_hold'] == 4.5
                assert saved['iracing_end_delay_ms'] == 2000
            assert 'clutch_disengaged' not in telemetry.return_value
            sequence = start()
            brake_ctor.assert_called_once_with('F10')
            sequence_ctor.assert_called_once_with(brake_ctor.return_value, 'F7', 4.5,
                                                  reset_delay_seconds=2.0)
            sequence.tick.assert_called_once_with(telemetry.return_value)
            shortcut.assert_not_called()
        ''')

    def test_brake_and_reset_start_without_any_clutch_settings_or_telemetry(self):
        self.run_scenario(r'''
            window._save_iracing_return_settings()
            saved = s.load_app_settings()
            assert 'iracing_clutch_enabled' not in saved
            assert 'iracing_clutch_shortcut' not in saved
            window._load_saved_settings()
            assert not hasattr(window, 'iracing_clutch_enabled')
            assert not hasattr(window, 'iracing_clutch_shortcut')
            window.game_selector.setCurrentText('iRacing')
            assert 'clutch_disengaged' not in telemetry.return_value
            window._test_iracing_return()
            callback()()
            brake_ctor.assert_called_once_with('F8')
            sequence_ctor.assert_called_once_with(brake_ctor.return_value, 'F9', 3.5,
                                                  reset_delay_seconds=1.5)
            assert window._iracing_return is made[-1]
            assert window.iracing_return_timer.isActive()
            shortcut.assert_not_called()
        ''')

    def test_completion_failure_and_startup_errors_are_reported_and_stop_timer(self):
        self.run_scenario(r'''
            for success, message in ((True, 'iRacing confirmed the car is in its pit stall.'),
                                     (False, 'iRacing telemetry disconnected; return to pits was cancelled.')):
                sequence = start()
                sequence.done, sequence.success, sequence.message = True, success, message
                window._tick_iracing_return()
                assert window.iracing_return_status.text() == message
                assert not window.iracing_return_timer.isActive()
                assert window._iracing_return is None
            sequence = start()
            sequence.tick.side_effect = RuntimeError('unexpected SDK failure')
            window._tick_iracing_return()
            sequence.cancel.assert_called_once()
            assert 'unexpected SDK failure' in window.iracing_return_status.text()
            assert not window.iracing_return_timer.isActive()
            assert window._iracing_return is None
            sequence_ctor.side_effect = RuntimeError('cannot create sequence')
            window._test_iracing_return()
            callback()()
            brake_ctor.return_value.close.assert_called_once()
            assert 'could not start' in window.iracing_return_status.text()
            assert 'cannot create sequence' in window.iracing_return_status.text()
            assert window._iracing_return is None
            assert not window.iracing_return_timer.isActive()
            shortcut.assert_not_called()
        ''')


if __name__ == '__main__':
    unittest.main()
