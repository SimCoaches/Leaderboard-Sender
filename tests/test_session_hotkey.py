import ctypes
import unittest
from unittest.mock import Mock

from session_hotkey import INPUT, WindowsKeyboard, parse_shortcut, send_game_shortcut


class ShortcutTests(unittest.TestCase):
    def backend(self):
        keyboard = Mock()
        keyboard.find_game.return_value = 123
        keyboard.foreground.return_value = 123
        keyboard.held.return_value = False
        keyboard.send.side_effect = lambda events: len(events)
        return keyboard

    def test_parse_chords_and_function_keys(self):
        self.assertEqual((0x11, 0x52), parse_shortcut(' Ctrl + r '))
        self.assertEqual((0x11, 0x12, 0x10, 0x76), parse_shortcut('Shift+Alt+Ctrl+F7'))
        self.assertEqual((0x20,), parse_shortcut('Space'))

    def test_reject_invalid_and_system_chords(self):
        for value in ('', 'Ctrl', 'Ctrl+', 'Ctrl+Ctrl+R', 'Ctrl+R, Ctrl+S',
                      'Win+R', 'Alt+F4', 'Ctrl+Alt+Delete', 'Alt+Tab', 'Ctrl+Esc', 'F25'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_shortcut(value)

    def test_native_input_layout_and_function_signatures(self):
        self.assertEqual(40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28, ctypes.sizeof(INPUT))
        backend = WindowsKeyboard()  # Loads native ABI, does not send input.
        self.assertEqual(ctypes.c_void_p, backend.user.GetForegroundWindow.restype)
        backend.user = Mock()
        backend.user.MapVirtualKeyW.return_value = 0x13
        seen = []
        def capture(count, batch, size):
            seen.extend((e.type, e.ki.wVk, e.ki.wScan, e.ki.dwFlags) for e in batch)
            self.assertEqual(ctypes.sizeof(INPUT), size)
            return count
        backend.user.SendInput.side_effect = capture
        self.assertEqual(2, backend.send([(0x52, False), (0x52, True)]))
        self.assertEqual([(1, 0, 0x13, 8), (1, 0, 0x13, 10)], seen)

    def test_send_presses_holds_and_releases_in_reverse_order(self):
        keyboard = self.backend()
        sleep = Mock()
        ok, _ = send_game_shortcut('Assetto Corsa', 'Ctrl+R', keyboard, sleep)
        self.assertTrue(ok)
        keyboard.find_game.assert_called_once_with('Assetto Corsa')
        self.assertEqual([[(0x11, False), (0x52, False)], [(0x52, True), (0x11, True)]],
                         [call.args[0] for call in keyboard.send.call_args_list])
        sleep.assert_called_once_with(0.10)

    def test_game_detection_uses_executable_not_browser_title(self):
        keyboard = WindowsKeyboard()
        keyboard.user = Mock()
        keyboard.kernel = Mock()
        keyboard.user.IsWindowVisible.return_value = True
        keyboard.user.GetForegroundWindow.return_value = 10
        def enumerate_windows(callback, _):
            callback(10, 0)
            callback(20, 0)
            return True
        def pid_for_window(hwnd, pid):
            pid._obj.value = hwnd
            return 1
        def executable(process, flags, path, size):
            path.value = 'C:/Games/acs.exe' if process == 20 else 'C:/Apps/chrome.exe'
            return True
        keyboard.user.EnumWindows.side_effect = enumerate_windows
        keyboard.user.GetWindowThreadProcessId.side_effect = pid_for_window
        keyboard.kernel.OpenProcess.side_effect = lambda access, inherit, pid: pid
        keyboard.kernel.QueryFullProcessImageNameW.side_effect = executable
        self.assertEqual(20, keyboard.find_game('Assetto Corsa'))
        self.assertIsNone(keyboard.find_game('iRacing'))
        self.assertEqual(4, keyboard.kernel.CloseHandle.call_count)

    def test_focus_game_before_sending(self):
        keyboard = self.backend()
        keyboard.foreground.side_effect = [999, 123, 123]
        self.assertTrue(send_game_shortcut('Assetto Corsa', 'Ctrl+R', keyboard, Mock())[0])
        keyboard.focus.assert_called_once_with(123)

    def test_no_input_without_game_focus_or_with_held_keys(self):
        for reason in ('missing', 'focus', 'focus_lost', 'held'):
            keyboard = self.backend()
            if reason == 'missing':
                keyboard.find_game.return_value = None
            elif reason == 'focus':
                keyboard.foreground.return_value = 999
            elif reason == 'focus_lost':
                keyboard.foreground.side_effect = [123, 123, 999]
            else:
                keyboard.held.return_value = True
            with self.subTest(reason=reason):
                self.assertFalse(send_game_shortcut('Assetto Corsa', 'Ctrl+R', keyboard, Mock())[0])
                keyboard.send.assert_not_called()

    def test_partial_press_releases_only_inserted_keys_without_retrying_action(self):
        keyboard = self.backend()
        keyboard.send.side_effect = [1, 1]
        self.assertFalse(send_game_shortcut('Assetto Corsa', 'Ctrl+R', keyboard, Mock())[0])
        self.assertEqual([(0x11, True)], keyboard.send.call_args_list[-1].args[0])
        self.assertEqual(2, keyboard.send.call_count)

    def test_blocked_input_reported_without_retry(self):
        keyboard = self.backend()
        keyboard.send.side_effect = [0]
        ok, message = send_game_shortcut('Assetto Corsa', 'Ctrl+R', keyboard, Mock())
        self.assertFalse(ok)
        self.assertIn('blocked', message)
        keyboard.send.assert_called_once()

    def test_release_even_if_hold_fails(self):
        keyboard = self.backend()
        self.assertFalse(send_game_shortcut('Assetto Corsa', 'Ctrl+R', keyboard,
                                           Mock(side_effect=RuntimeError('hold failed')))[0])
        self.assertEqual([(0x52, True), (0x11, True)], keyboard.send.call_args_list[-1].args[0])

    def test_retry_releases_only_and_report_unreleased_keys(self):
        keyboard = self.backend()
        keyboard.send.side_effect = [2, 0, 0]
        ok, message = send_game_shortcut('Assetto Corsa', 'Ctrl+R', keyboard, Mock())
        self.assertFalse(ok)
        self.assertIn('Release the shortcut keys manually', message)
        self.assertEqual(keyboard.send.call_args_list[1], keyboard.send.call_args_list[2])


if __name__ == '__main__':
    unittest.main()
