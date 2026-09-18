import unittest
from unittest.mock import Mock, patch

from iracing_brake import (KeyboardBrake, KeyboardBrakeError,
                          validate_brake_shortcut, validate_return_shortcuts)


def fake_keyboard():
    """Never load user32 or send a real key event."""
    keyboard = Mock()
    keyboard.find_game.return_value = 123
    keyboard.foreground.return_value = 123
    keyboard.held.return_value = False
    keyboard.send.side_effect = lambda events: len(events)
    return keyboard


class KeyboardBrakeTests(unittest.TestCase):
    def test_constructor_release_and_close_have_no_native_side_effects(self):
        with patch('iracing_brake.WindowsKeyboard') as native:
            brake = KeyboardBrake()
            brake.release()
            brake.close()
            brake.close()
            native.assert_not_called()

    def test_initializes_native_input_lazily_once(self):
        keyboard = fake_keyboard()
        with patch('iracing_brake.WindowsKeyboard', return_value=keyboard) as native:
            brake = KeyboardBrake()
            native.assert_not_called()
            brake.press()
            brake.release()
            brake.press()
            brake.close()
            native.assert_called_once_with()
        self.assertEqual([[(0x77, False)], [(0x77, True)],
                          [(0x77, False)], [(0x77, True)]],
                         [call.args[0] for call in keyboard.send.call_args_list])

    def test_press_and_release_are_idempotent_without_repeated_key_down(self):
        keyboard = fake_keyboard()
        brake = KeyboardBrake('F9', keyboard=keyboard)
        brake.press()
        brake.press()
        brake.release()
        brake.release()
        brake.close()
        brake.close()
        self.assertEqual([[(0x78, False)], [(0x78, True)]],
                         [call.args[0] for call in keyboard.send.call_args_list])
        self.assertEqual((0x78,), brake.keys)
        self.assertEqual('F9', brake.shortcut)

    def test_rejects_modifiers_unsupported_keys_and_conflicting_reset(self):
        with patch('iracing_brake.WindowsKeyboard') as native:
            for invalid in ('Ctrl+F8', 'Shift+F8', 'Alt+F8', '', 'Wheel', None, '+F8'):
                with self.subTest(key=invalid), self.assertRaises(ValueError):
                    KeyboardBrake(invalid)
            for brake_key, reset_key in (('r', 'R'), ('F8', 'F8'), ('Return', 'Enter')):
                with self.subTest(key=brake_key), self.assertRaisesRegex(ValueError, 'different keys'):
                    KeyboardBrake(brake_key, reset_shortcut=reset_key)
            for reset_key in ('Shift+R', 'Ctrl+R', 'Alt+R'):
                with self.subTest(reset=reset_key), self.assertRaisesRegex(ValueError, 'reset key without'):
                    KeyboardBrake('F8', reset_shortcut=reset_key)
            native.assert_not_called()
        self.assertEqual((0x77,), validate_brake_shortcut('f8', 'F9'))

    def test_all_return_controls_must_be_distinct_unmodified_keys(self):
        self.assertEqual(((0x77,), (0x78,)), validate_return_shortcuts('F8', 'F9'))
        self.assertEqual(((0x77,), ()), validate_return_shortcuts('F8', None))
        for args in (('F8', 'F8'), ('F9', 'F9'), ('r', 'R')):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, 'different keys'):
                validate_return_shortcuts(*args)
        for args in (('Ctrl+F8', 'F9'), ('F8', 'Shift+R'), ('Alt+F7', 'F9')):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, 'without Ctrl, Alt or Shift'):
                validate_return_shortcuts(*args)

    def test_only_configured_brake_is_pressed_and_released(self):
        keyboard = fake_keyboard()
        brake = KeyboardBrake('F8', reset_shortcut='F9', keyboard=keyboard)
        self.assertEqual((0x77,), brake.brake_keys)
        self.assertEqual((0x77,), brake.keys)
        brake.press()
        brake.press()
        brake.close()
        brake.close()
        self.assertEqual([[(0x77, False)], [(0x77, True)]],
                         [call.args[0] for call in keyboard.send.call_args_list])

    def test_unrelated_key_is_not_pressed_or_released(self):
        keyboard = fake_keyboard()
        keyboard.held.side_effect = lambda keys: 0x76 in keys
        brake = KeyboardBrake('F8', keyboard=keyboard)
        brake.press()
        brake.close()
        self.assertEqual([[(0x77, False)], [(0x77, True)]],
                         [call.args[0] for call in keyboard.send.call_args_list])

    def test_game_missing_or_not_foreground_sends_nothing(self):
        for mode in ('missing', 'background', 'lost_focus'):
            keyboard = fake_keyboard()
            if mode == 'missing':
                keyboard.find_game.return_value = None
            elif mode == 'background':
                keyboard.foreground.return_value = 999
            else:
                keyboard.foreground.side_effect = [123, 999]
            brake = KeyboardBrake(keyboard=keyboard)
            with self.subTest(mode=mode), self.assertRaisesRegex(KeyboardBrakeError, 'foreground|lost focus'):
                brake.press()
            brake.close()
            keyboard.send.assert_not_called()
            keyboard.focus.assert_not_called()

    def test_preexisting_physical_key_or_modifier_is_never_released(self):
        keyboard = fake_keyboard()
        keyboard.held.return_value = True
        brake = KeyboardBrake(keyboard=keyboard)
        with self.assertRaisesRegex(KeyboardBrakeError, 'already held'):
            brake.press()
        brake.release()
        brake.close()
        keyboard.held.assert_called_once_with({0x77, 0x10, 0x11, 0x12, 0x5B, 0x5C})
        keyboard.send.assert_not_called()

    def test_failed_or_unknown_press_always_attempts_release_and_never_represses(self):
        for outcome in (0, None, True, -1, 2, RuntimeError('native failure')):
            keyboard = fake_keyboard()
            keyboard.send.side_effect = [outcome, 1]
            brake = KeyboardBrake(keyboard=keyboard)
            with self.subTest(outcome=outcome), self.assertRaises(KeyboardBrakeError):
                brake.press()
            brake.close()
            with self.assertRaisesRegex(KeyboardBrakeError, 'closed'):
                brake.press()
            self.assertEqual([[(0x77, False)], [(0x77, True)]],
                             [call.args[0] for call in keyboard.send.call_args_list])

    def test_release_retries_key_up_only_after_block_or_exception(self):
        for failure in (0, None, True, RuntimeError('native failure')):
            keyboard = fake_keyboard()
            keyboard.send.side_effect = [1, failure, 1]
            brake = KeyboardBrake(keyboard=keyboard)
            brake.press()
            brake.close()
            brake.release()
            self.assertEqual([[(0x77, False)], [(0x77, True)], [(0x77, True)]],
                             [call.args[0] for call in keyboard.send.call_args_list])

    def test_failed_release_remains_owned_for_another_close_attempt(self):
        keyboard = fake_keyboard()
        keyboard.send.side_effect = [1, 0, 0, 1]
        brake = KeyboardBrake(keyboard=keyboard)
        brake.press()
        with self.assertRaisesRegex(KeyboardBrakeError, 'release'):
            brake.close()
        with self.assertRaisesRegex(KeyboardBrakeError, 'closed'):
            brake.press()
        brake.close()
        brake.close()
        self.assertEqual([[(0x77, False)], [(0x77, True)], [(0x77, True)], [(0x77, True)]],
                         [call.args[0] for call in keyboard.send.call_args_list])

    def test_press_and_cleanup_failures_both_reported_without_losing_cleanup_ownership(self):
        keyboard = fake_keyboard()
        keyboard.send.side_effect = [RuntimeError('unknown insertion'), 0, 0, 1]
        brake = KeyboardBrake(keyboard=keyboard)
        with self.assertRaisesRegex(KeyboardBrakeError, 'unknown insertion.*cleanup also failed'):
            brake.press()
        brake.close()
        self.assertEqual(4, keyboard.send.call_count)

    def test_context_exception_releases_pressed_key(self):
        keyboard = fake_keyboard()
        with self.assertRaisesRegex(RuntimeError, 'sequence stopped'):
            with KeyboardBrake(keyboard=keyboard) as brake:
                brake.press()
                raise RuntimeError('sequence stopped')
        self.assertEqual([[(0x77, False)], [(0x77, True)]],
                         [call.args[0] for call in keyboard.send.call_args_list])

    def test_native_initialization_failure_does_not_send_a_release(self):
        with patch('iracing_brake.WindowsKeyboard', side_effect=RuntimeError('Windows unavailable')):
            brake = KeyboardBrake()
            with self.assertRaisesRegex(KeyboardBrakeError, 'Windows unavailable'):
                brake.press()
            brake.close()


if __name__ == '__main__':
    unittest.main()
