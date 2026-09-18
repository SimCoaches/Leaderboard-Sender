import unittest
from unittest.mock import Mock, patch

from iracing_brake import KeyboardBrake
from iracing_return import IracingReturnSequence


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class IracingReturnTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.down = set()
        self.events = []
        self.keyboard = Mock()
        self.keyboard.find_game.return_value = 123
        self.keyboard.foreground.return_value = 123
        self.keyboard.held.side_effect = lambda keys: bool(self.down.intersection(keys))
        self.keyboard.send.side_effect = self.send
        self.brake = Mock()
        self.brake.press.return_value = None
        self.brake.release.return_value = None
        self.brake.close.return_value = None
        self.sequence = self.make_sequence()

    def make_sequence(self, **kwargs):
        return IracingReturnSequence(self.brake, keyboard=self.keyboard, clock=self.clock,
                                     hold_seconds=kwargs.pop('hold_seconds', 1),
                                     reset_delay_seconds=kwargs.pop('reset_delay_seconds', 0), **kwargs)

    def send(self, events):
        self.events.extend((self.clock(), key, released) for key, released in events)
        for key, released in events:
            if released:
                self.down.discard(key)
            else:
                self.down.add(key)
        return len(events)

    def tick(self, elapsed=0.1, **changes):
        self.clock.now += elapsed
        snapshot = dict(connected=True, in_car=True, speed_mps=20.0, brake=1.0,
                        sampled_at=self.clock(), in_pit_stall=False, tow_time=0)
        snapshot.update(changes)
        self.sequence.tick(snapshot)
        return snapshot

    def start_braking(self):
        self.tick(0)
        self.tick(0.11)
        self.assertEqual('braking', self.sequence.state)

    def start_reset(self):
        self.start_braking()
        self.tick(0.1, speed_mps=0)
        self.tick(0.51, speed_mps=0)
        self.assertEqual('resetting', self.sequence.state)

    def assert_clean(self):
        self.assertTrue(self.sequence.done)
        self.assertEqual(set(), self.down)
        self.brake.release.assert_called_once()
        self.brake.close.assert_called_once()

    def test_brakes_then_holds_single_key_without_blocking_and_reports_unconfirmed(self):
        with patch('time.sleep', side_effect=AssertionError('must not sleep')):
            self.start_reset()
            self.brake.press.assert_called_once()
            self.assertEqual({0x78}, self.down)
            self.tick(0.99, speed_mps=0)
            self.assertEqual({0x78}, self.down)
            self.tick(0.02, speed_mps=0)
            self.assertEqual(set(), self.down)
            self.brake.release.assert_not_called()
            self.tick(1, speed_mps=0)
            self.tick(1.01, speed_mps=0)
        self.assert_clean()
        self.assertFalse(self.sequence.success)
        self.assertIn('did not confirm', self.sequence.message)
        self.assertEqual([(0x78, False), (0x78, True)],
                         [(key, up) for _, key, up in self.events])
        self.assertGreaterEqual(self.events[1][0] - self.events[0][0], 1)

    def test_waits_for_slow_stop_and_restarts_stationary_dwell_after_motion(self):
        self.start_braking()
        for speed in (30, 20, 10, 1, 0):
            self.tick(0.5, speed_mps=speed)
        self.tick(0.2, speed_mps=0)
        self.tick(0.1, speed_mps=0.2)
        self.tick(0.1, speed_mps=0)
        self.tick(0.2, speed_mps=0)
        self.keyboard.send.assert_not_called()
        self.tick(0.11, speed_mps=0)
        self.assertEqual({0x78}, self.down)

    def test_duplicate_samples_do_not_prove_stationary_dwell_or_brake(self):
        self.start_braking()
        sample = self.tick(0.1, speed_mps=0)
        self.tick(0.6, sampled_at=sample['sampled_at'], speed_mps=0)
        self.keyboard.send.assert_not_called()
        self.tick(0.01, speed_mps=0)
        self.assertEqual({0x78}, self.down)

    def test_missing_brake_mapping_times_out_without_reset(self):
        self.start_braking()
        for _ in range(5):
            self.tick(0.5, speed_mps=0, brake=0)
        self.assert_clean()
        self.keyboard.send.assert_not_called()
        self.assertIn('Assign the saved brake key to Second Brake', self.sequence.message)

    def test_stale_stopped_sample_releases_brake_and_does_not_reset(self):
        self.start_braking()
        sample = self.tick(0.1, speed_mps=0)
        self.tick(1.01, sampled_at=sample['sampled_at'], speed_mps=0)
        self.assert_clean()
        self.keyboard.send.assert_not_called()
        self.assertIn('stale', self.sequence.message)

    def test_stop_timeout_never_sends_reset(self):
        self.start_braking()
        for _ in range(61):
            self.tick(0.5, speed_mps=10)
        self.assert_clean()
        self.keyboard.send.assert_not_called()
        self.assertIn('30 seconds', self.sequence.message)

    def test_focus_is_acquired_before_brake_and_settles_across_ticks(self):
        self.keyboard.foreground.return_value = 999
        self.tick(0)
        self.keyboard.focus.assert_called_once_with(123)
        self.brake.press.assert_not_called()
        self.keyboard.foreground.return_value = 123
        self.tick(0.1)
        self.brake.press.assert_not_called()
        self.tick(0.11)
        self.brake.press.assert_called_once()

    def test_focus_timeout_and_missing_game_never_apply_brake(self):
        for mode in ('missing', 'blocked'):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == 'missing':
                    self.keyboard.find_game.return_value = None
                else:
                    self.keyboard.foreground.return_value = 999
                for _ in range(12):
                    self.tick(0.1)
                self.assert_clean()
                self.brake.press.assert_not_called()
                self.keyboard.send.assert_not_called()

    def test_focus_loss_in_each_input_phase_cleans_up(self):
        for state in ('braking', 'resetting'):
            with self.subTest(state=state):
                self.setUp()
                self.start_braking()
                if state != 'braking':
                    self.tick(0.1, speed_mps=0)
                    self.tick(0.51, speed_mps=0)
                self.keyboard.foreground.return_value = 999
                self.tick(speed_mps=0)
                self.assert_clean()
                self.assertIn('lost focus', self.sequence.message)

    def test_cancel_is_idempotent_and_releases_all_owned_inputs(self):
        self.start_reset()
        self.sequence.cancel('New driver registered.')
        self.sequence.cancel('Repeated cancel.')
        self.tick()
        self.assert_clean()
        self.assertFalse(self.sequence.success)
        self.assertEqual('New driver registered.', self.sequence.message)

    def test_invalid_and_missing_telemetry_never_applies_brake(self):
        cases = [dict(connected=False), dict(in_car=False), dict(in_car=None),
                 dict(speed_mps=float('nan')), dict(speed_mps=None),
                 dict(speed_mps=float('inf')), dict(brake=-0.1), dict(brake=1.1),
                 dict(brake=None), dict(in_pit_stall=None), dict(tow_time=-1),
                 dict(tow_time=float('nan')), dict(sampled_at=0),
                 dict(sampled_at=float('nan')), dict(sampled_at=101)]
        for changes in cases:
            with self.subTest(changes=changes):
                self.setUp()
                self.tick(0, **changes)
                self.assert_clean()
                self.brake.press.assert_not_called()
                self.keyboard.send.assert_not_called()

    def test_invalid_telemetry_during_reset_releases_keys(self):
        for changes in (dict(connected=False), dict(brake=None), dict(speed_mps=float('nan'))):
            with self.subTest(changes=changes):
                self.setUp()
                self.start_reset()
                self.tick(**changes)
                self.assert_clean()
                self.assertFalse(self.sequence.success)

    def test_reverse_motion_is_not_stationary(self):
        self.start_braking()
        for _ in range(5):
            self.tick(0.3, speed_mps=-1)
        self.keyboard.send.assert_not_called()

    def test_moving_during_reset_aborts(self):
        self.start_reset()
        self.tick(speed_mps=0.06)
        self.assert_clean()
        self.assertIn('moved', self.sequence.message)

    def test_brake_signal_loss_aborts(self):
        self.start_reset()
        self.tick(speed_mps=0, brake=0.1)
        self.assert_clean()
        self.assertIn('stopped reporting', self.sequence.message)

    def test_confirmed_exit_pit_or_tow_succeeds_and_cleans_up(self):
        for changes in (dict(in_car=False), dict(in_pit_stall=True), dict(tow_time=20)):
            with self.subTest(changes=changes):
                self.setUp()
                self.start_reset()
                self.tick(speed_mps=0, **changes)
                self.assert_clean()
                self.assertTrue(self.sequence.success)
                self.assertIn('confirmed', self.sequence.message)

    def test_exit_before_reset_does_not_claim_success(self):
        self.start_braking()
        self.tick(in_car=False)
        self.assert_clean()
        self.assertFalse(self.sequence.success)
        self.keyboard.send.assert_not_called()

    def test_already_in_pit_or_towing_does_not_claim_success(self):
        self.tick(0, in_pit_stall=True)
        self.tick(0.11, in_pit_stall=True)
        self.tick(0.1, speed_mps=0, in_pit_stall=True)
        self.tick(0.51, speed_mps=0, in_pit_stall=True)
        self.tick(0.09, speed_mps=0, in_pit_stall=True)
        self.tick(0.1, speed_mps=0, in_pit_stall=True)
        self.assertFalse(self.sequence.done)
        self.sequence.cancel()
        self.setUp()
        self.tick(0, tow_time=10)
        self.tick(0.11, tow_time=9)
        self.assert_clean()
        self.brake.press.assert_not_called()

    def test_operator_key_is_never_released_by_sequence(self):
        self.down.add(0x10)
        self.tick(0)
        self.tick(0.11)
        self.assertTrue(self.sequence.done)
        self.brake.press.assert_not_called()
        self.keyboard.send.assert_not_called()
        self.assertEqual({0x10}, self.down)

    def test_rejects_reset_modifiers_before_any_input(self):
        for shortcut in ('Shift+R', 'Ctrl+R', 'Alt+F9', 'Ctrl+Shift+R', 'Win+R'):
            with self.subTest(shortcut=shortcut), self.assertRaises(ValueError):
                self.make_sequence(reset_shortcut=shortcut)
        self.keyboard.send.assert_not_called()
        self.brake.press.assert_not_called()

    def test_action_press_failure_never_retries_action(self):
        self.start_braking()
        self.tick(0.1, speed_mps=0)
        def reject_action(events):
            return 0 if events == [(0x78, False)] else self.send(events)
        self.keyboard.send.side_effect = reject_action
        self.tick(0.51, speed_mps=0)
        self.assert_clean()
        attempts = [c.args[0] for c in self.keyboard.send.call_args_list if c.args[0] == [(0x78, False)]]
        self.assertEqual(1, len(attempts))

    def test_backend_exception_during_press_attempts_key_release(self):
        self.start_braking()
        self.tick(0.1, speed_mps=0)
        def uncertain_press(events):
            self.send(events)
            if events == [(0x78, False)]:
                raise RuntimeError('driver failure after insertion')
            return len(events)
        self.keyboard.send.side_effect = uncertain_press
        self.tick(0.51, speed_mps=0)
        self.assert_clean()
        self.assertFalse(self.sequence.success)

    def test_cleanup_attempts_other_keys_and_brake_even_when_release_fails(self):
        self.start_reset()
        def fail_action_release(events):
            return 0 if events == [(0x78, True)] else self.send(events)
        self.keyboard.send.side_effect = fail_action_release
        self.sequence.cancel()
        self.assertTrue(self.sequence.done)
        self.assertFalse(self.sequence.success)
        self.assertEqual({0x78}, self.down)
        self.brake.release.assert_called_once()
        self.brake.close.assert_called_once()
        self.assertIn('release the shortcut keys manually', self.sequence.message)

    def test_cleanup_failure_overrides_confirmed_success_and_still_closes(self):
        self.start_reset()
        self.brake.release.side_effect = RuntimeError('brake release blocked')
        self.tick(speed_mps=0, tow_time=20)
        self.assert_clean()
        self.assertFalse(self.sequence.success)
        self.assertIn('cleanup failed', self.sequence.message)

    def test_single_key_and_configurable_hold(self):
        self.sequence = self.make_sequence(reset_shortcut='R', hold_seconds=3.5)
        self.start_reset()
        self.assertEqual({0x52}, self.down)
        for _ in range(3):
            self.tick(1, speed_mps=0)
        self.assertEqual({0x52}, self.down)
        self.tick(0.51, speed_mps=0)
        self.assertEqual(set(), self.down)
        self.sequence.cancel()
        self.assert_clean()

    def test_reject_invalid_hold_durations(self):
        for seconds in (0, 0.99, 10.01, float('nan'), float('inf')):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.make_sequence(hold_seconds=seconds)

    def test_brake_is_applied_immediately_but_reset_respects_saved_delay(self):
        self.sequence = self.make_sequence(reset_delay_seconds=1.5)
        self.start_braking()
        self.brake.press.assert_called_once()
        self.tick(0.1, speed_mps=0)
        self.tick(0.31, speed_mps=0)
        self.keyboard.send.assert_not_called()
        self.tick(0.97, speed_mps=0)
        self.keyboard.send.assert_not_called()
        self.tick(0.02, speed_mps=0)
        self.assertEqual('resetting', self.sequence.state)
        self.assertEqual({0x78}, self.down)

    def test_reject_invalid_reset_delays(self):
        for seconds in (-0.01, 2.01, float('nan'), float('inf')):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.make_sequence(reset_delay_seconds=seconds)

    def test_legacy_clutch_state_is_irrelevant_to_brake_and_reset(self):
        for legacy_fields in ({}, {'clutch_disengaged': False},
                              {'clutch_disengaged': None},
                              {'clutch_disengaged': 0},
                              {'clutch_disengaged': 'invalid'}):
            with self.subTest(fields=legacy_fields):
                self.setUp()
                self.tick(0, **legacy_fields)
                self.tick(0.11, **legacy_fields)
                self.tick(0.1, speed_mps=0, **legacy_fields)
                self.tick(0.31, speed_mps=0, **legacy_fields)
                self.assertEqual('resetting', self.sequence.state)
                self.assertEqual({0x78}, self.down)
                self.tick(0.1, speed_mps=0, tow_time=20, **legacy_fields)
                self.assert_clean()
                self.assertTrue(self.sequence.success)
                self.brake.press.assert_called_once()

    def test_session_or_telemetry_generation_change_cancels_before_confirmation(self):
        for field in ('session_num', 'telemetry_generation'):
            with self.subTest(field=field):
                self.setUp()
                self.tick(0, **{field: 1})
                self.tick(0.11, **{field: 1})
                self.tick(0.1, speed_mps=0, **{field: 1})
                self.tick(0.51, speed_mps=0, **{field: 1})
                self.tick(0.09, speed_mps=0, **{field: 1})
                self.tick(speed_mps=0, in_pit_stall=True, **{field: 2})
                self.assert_clean()
                self.assertFalse(self.sequence.success)
                self.assertIn('session or telemetry connection changed', self.sequence.message)


class KeyboardBrakeSequenceTests(unittest.TestCase):
    """Exercise real brake ownership together with the reset state machine."""

    def setUp(self):
        self.clock = Clock()
        self.down = set()
        self.events = []
        self.keyboard = Mock()
        self.keyboard.find_game.return_value = 123
        self.keyboard.foreground.return_value = 123
        self.keyboard.held.side_effect = lambda keys: bool(self.down.intersection(keys))
        self.keyboard.send.side_effect = self.send
        self.brake = KeyboardBrake('F8', reset_shortcut='F9', keyboard=self.keyboard)
        self.sequence = IracingReturnSequence(self.brake, keyboard=self.keyboard,
                                              clock=self.clock, hold_seconds=1,
                                              reset_delay_seconds=0)

    def send(self, events):
        self.events.extend(events)
        for key, released in events:
            if released:
                self.down.discard(key)
            else:
                self.down.add(key)
        return len(events)

    def tick(self, elapsed=0.1, **changes):
        self.clock.now += elapsed
        snapshot = dict(connected=True, in_car=True, speed_mps=0, brake=1.0,
                        sampled_at=self.clock(), in_pit_stall=False, tow_time=0,
                        throttle=1.0)
        snapshot.update(changes)
        self.sequence.tick(snapshot)

    def stop_car(self):
        self.tick(0, speed_mps=20, brake=0)
        self.tick(0.11, speed_mps=20, brake=0)
        self.assertEqual('braking', self.sequence.state)
        self.assertEqual({0x77}, self.down)
        self.tick(0.1)
        self.tick(0.51)
        self.assertEqual('resetting', self.sequence.state)

    def test_full_throttle_keeps_only_brake_down_through_confirmed_reset(self):
        self.stop_car()
        self.assertEqual('resetting', self.sequence.state)
        self.assertEqual({0x77, 0x78}, self.down)
        self.tick(0.5)
        self.assertEqual({0x77, 0x78}, self.down)
        self.tick(0.1, tow_time=20)
        self.assertTrue(self.sequence.success)
        self.assertEqual(set(), self.down)
        self.assertEqual([(0x77, False), (0x78, False),
                          (0x78, True), (0x77, True)], self.events)

    def test_legacy_clutch_telemetry_does_not_block_braking_or_reset(self):
        self.tick(0, speed_mps=20, brake=0, clutch_disengaged=False)
        self.tick(0.11, speed_mps=20, brake=0, clutch_disengaged=False)
        self.tick(0.1, speed_mps=10)
        self.tick(0.1, speed_mps=5, clutch_disengaged=False)
        self.assertFalse(self.sequence.done)
        self.assertEqual({0x77}, self.down)
        self.assertNotIn((0x78, False), self.events)
        self.tick(0.1, clutch_disengaged=False)
        self.tick(0.31, clutch_disengaged=False)
        self.assertEqual('resetting', self.sequence.state)
        self.tick(0.1, tow_time=20, clutch_disengaged=False)
        self.assertTrue(self.sequence.success)
        self.assertEqual(set(), self.down)
        self.assertEqual(1, self.events.count((0x77, False)))
        self.assertEqual({0x77, 0x78}, {key for key, _ in self.events})

    def test_brake_loss_during_reset_releases_both_controls_without_retry(self):
        self.stop_car()
        self.tick(0.09)
        self.tick(0.1, brake=0.1)
        self.assertTrue(self.sequence.done)
        self.assertFalse(self.sequence.success)
        self.assertEqual(set(), self.down)
        self.assertEqual(1, self.events.count((0x78, False)))
        self.assertEqual(1, self.events.count((0x77, False)))

    def test_legacy_clutch_loss_during_reset_is_ignored(self):
        self.stop_car()
        self.tick(0.1, clutch_disengaged=False)
        self.assertFalse(self.sequence.done)
        self.assertEqual({0x77, 0x78}, self.down)
        self.tick(0.1, tow_time=20, clutch_disengaged=None)
        self.assertTrue(self.sequence.success)
        self.assertEqual(set(), self.down)
        for key in (0x77, 0x78):
            self.assertEqual(1, self.events.count((key, False)))
            self.assertEqual(1, self.events.count((key, True)))
        self.assertEqual({0x77, 0x78}, {key for key, _ in self.events})

    def test_focus_loss_cleans_up_all_keys(self):
        self.stop_car()
        self.keyboard.foreground.return_value = 999
        self.tick(0.09)
        self.assertTrue(self.sequence.done)
        self.assertIn('lost focus', self.sequence.message)
        self.assertEqual(set(), self.down)
        self.assertEqual(1, self.events.count((0x78, False)))

    def test_physical_brake_key_is_left_held_without_reset_or_own_keyup(self):
        self.down.add(0x77)
        self.tick(0)
        self.tick(0.11)
        self.assertTrue(self.sequence.done)
        self.assertFalse(self.sequence.success)
        self.assertEqual({0x77}, self.down)
        self.assertEqual([], self.events)

    def test_cancel_retries_blocked_cleanup_without_repeating_any_action(self):
        self.assertFalse(self.sequence.cleanup_pending)
        self.stop_car()
        self.tick(0.09)
        self.assertEqual({0x77, 0x78}, self.down)
        self.keyboard.send.side_effect = lambda events: 0
        self.sequence.cancel('Operator stopped return to pits.')
        self.assertTrue(self.sequence.done)
        self.assertFalse(self.sequence.success)
        self.assertTrue(self.sequence.cleanup_pending)
        self.assertEqual({0x77, 0x78}, self.down)
        self.assertIn('Input cleanup failed', self.sequence.message)

        # A timer tick must not resume the action or pretend cleanup succeeded.
        attempts = self.keyboard.send.call_count
        self.tick()
        self.assertEqual(attempts, self.keyboard.send.call_count)
        self.assertTrue(self.sequence.cleanup_pending)

        self.keyboard.send.side_effect = self.send
        self.sequence.cancel('Released controls after retry.')
        self.assertTrue(self.sequence.done)
        self.assertFalse(self.sequence.success)
        self.assertFalse(self.sequence.cleanup_pending)
        self.assertEqual(set(), self.down)
        self.assertEqual('Released controls after retry.', self.sequence.message)
        self.assertEqual([(0x77, False), (0x78, False),
                          (0x78, True), (0x77, True)], self.events)
        attempts = self.keyboard.send.call_count
        self.sequence.cancel()
        self.assertEqual(attempts, self.keyboard.send.call_count)


if __name__ == '__main__':
    unittest.main()
