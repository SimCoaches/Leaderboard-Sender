"""Nonblocking, telemetry-guarded iRacing brake and return-to-pits sequence.

Call ``tick`` from the GUI timer with the latest telemetry snapshot. No method
sleeps or retries an action key; only harmless key-up events may be retried.
``success`` means telemetry confirmed a return, not merely that input was sent.
"""
import math
import time

from session_hotkey import WindowsKeyboard, parse_shortcut


class IracingReturnSequence:
    MAX_SAMPLE_AGE = 1.0
    FOCUS_TIMEOUT = 1.0
    FOCUS_SETTLE = 0.10
    BRAKE_TIMEOUT = 2.0
    STOP_TIMEOUT = 30.0
    STOP_SPEED = 0.05
    STOP_DWELL = 0.30
    CONFIRM_TIMEOUT = 2.0

    def __init__(self, brake, reset_shortcut='F9', hold_seconds=3.5,
                 keyboard=None, clock=time.monotonic, reset_delay_seconds=1.5):
        self.keys = parse_shortcut(reset_shortcut)
        if len(self.keys) != 1 or self.keys[0] in {0x10, 0x11, 0x12, 0x5B, 0x5C}:
            raise ValueError('Use a single reset key without Shift, Ctrl, Alt, or Windows, such as F9. Modifiers can release iRacing keyboard braking.')
        self.hold_seconds = float(hold_seconds)
        if not math.isfinite(self.hold_seconds) or not 1 <= self.hold_seconds <= 10:
            raise ValueError('iRacing reset hold must be between 1 and 10 seconds.')
        self.reset_delay_seconds = float(reset_delay_seconds)
        if not math.isfinite(self.reset_delay_seconds) or not 0 <= self.reset_delay_seconds <= 2:
            raise ValueError('iRacing reset delay must be between 0 and 2 seconds.')
        self.brake = brake
        self.keyboard = keyboard if keyboard is not None else WindowsKeyboard()
        self.clock = clock
        self.done = False
        self.success = False
        self.cleanup_pending = False
        self.message = 'Waiting for fresh iRacing telemetry and game focus.'
        self.state = 'focus'
        self._started = clock()
        self._last_now = self._started
        self._last_sample = None
        self._last_focus_attempt = None
        self._window = None
        self._phase_started = self._started
        self._brake_started = None
        self._brake_sample = None
        self._brake_verified = False
        self._stopped_since = None
        self._pressed = []
        self._action_sent = False
        self._initial_pit = None
        self._initial_tow = None
        self._confirmation = None
        self._telemetry_identity = {}

    @staticmethod
    def _number(snapshot, name):
        value = snapshot.get(name)
        if isinstance(value, (bool, str, bytes)) or value is None:
            raise ValueError(f'iRacing {name} telemetry is missing or invalid.')
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f'iRacing {name} telemetry is missing or invalid.')
        return value

    def _snapshot(self, snapshot, now):
        if not isinstance(snapshot, dict) or snapshot.get('connected') is not True:
            raise ValueError('iRacing telemetry disconnected; return to pits was cancelled.')
        for field in ('in_car', 'in_pit_stall'):
            if not isinstance(snapshot.get(field), bool):
                raise ValueError(f'iRacing {field} telemetry is missing or invalid.')
        sampled_at = self._number(snapshot, 'sampled_at')
        if now - sampled_at > self.MAX_SAMPLE_AGE or sampled_at > now + 0.05:
            raise ValueError('iRacing telemetry is stale; return to pits was cancelled.')
        if self._last_sample is not None and sampled_at < self._last_sample:
            raise ValueError('iRacing telemetry time went backwards; return to pits was cancelled.')
        for field in ('session_num', 'telemetry_generation'):
            value = snapshot.get(field)
            if value is not None:
                if field in self._telemetry_identity and value != self._telemetry_identity[field]:
                    raise ValueError('The iRacing session or telemetry connection changed; return to pits was cancelled.')
                self._telemetry_identity[field] = value
        speed = self._number(snapshot, 'speed_mps')
        brake = self._number(snapshot, 'brake')
        tow = self._number(snapshot, 'tow_time')
        if not 0 <= brake <= 1 or tow < 0:
            raise ValueError('iRacing brake or tow telemetry is invalid.')
        fresh = self._last_sample is None or sampled_at > self._last_sample
        self._last_sample = sampled_at
        return sampled_at, fresh, abs(speed), brake, tow

    def _press(self, keys):
        # A backend exception can leave the insertion count unknown. Include all
        # attempted keys in cleanup until a returned count proves otherwise.
        self._pressed.extend(keys)
        count = self.keyboard.send([(key, False) for key in keys])
        if not isinstance(count, int) or not 0 <= count <= len(keys):
            raise RuntimeError('Windows returned an invalid keyboard input result.')
        if count != len(keys):
            for key in keys[count:]:
                self._pressed.remove(key)
            raise RuntimeError('Windows blocked keyboard input. Check that Sender and iRacing have the same administrator permissions.')

    def _release(self, keys):
        errors = []
        # Release one key at a time: a failed action-key release must not prevent
        # releasing a modifier. Duplicate key-up events never repeat the action.
        for key in keys:
            released = False
            for _ in range(2):
                try:
                    if self.keyboard.send([(key, True)]) == 1:
                        released = True
                        break
                except Exception:
                    pass
            if released:
                self._pressed.remove(key)
            else:
                errors.append('Windows blocked key release; release the shortcut keys manually.')
        if errors:
            raise RuntimeError(errors[0])

    def _finish(self, success, message):
        if self.done and not self.cleanup_pending:
            return
        # A finished sequence can retain unreleased inputs if Windows blocked
        # key-up. Cancel retries only cleanup, never the brake or reset action.
        if self.done:
            success = False
        errors = []
        try:
            self._release(tuple(reversed(self._pressed)))
        except Exception as exc:
            errors.append(str(exc))
        # Always attempt both operations even when key release or brake release
        # fails. close() gives the backend a final chance to release its input.
        for operation in (self.brake.release, self.brake.close):
            try:
                if operation() is False:
                    raise RuntimeError('The iRacing brake input could not be released.')
            except Exception as exc:
                errors.append(str(exc))
        self.done = True
        self.cleanup_pending = bool(errors)
        self.success = bool(success and not errors)
        self.state = 'done'
        self.message = message
        if errors:
            self.message += ' Input cleanup failed: ' + '; '.join(errors)

    def cancel(self, reason='Return to pits cancelled.'):
        """Release owned inputs; retry blocked cleanup on subsequent calls."""
        self._finish(False, str(reason))

    def tick(self, snapshot):
        if self.done:
            return
        try:
            self._tick(snapshot)
        except Exception as exc:
            self._finish(False, f'iRacing return to pits stopped: {exc}')

    def _tick(self, snapshot):
        now = self.clock()
        if not math.isfinite(now) or now < self._last_now:
            raise ValueError('The sequence clock is invalid.')
        self._last_now = now
        sampled_at, fresh, speed, reported_brake, tow = self._snapshot(snapshot, now)

        if self._window is None:
            self._window = self.keyboard.find_game('iRacing')
            if not self._window:
                raise RuntimeError('The iRacing game window was not found.')
        foreground = self.keyboard.foreground() == self._window
        if self.state == 'focus':
            if not snapshot['in_car']:
                raise ValueError('Enter the iRacing car before using return to pits.')
            if not foreground:
                if now - self._started >= self.FOCUS_TIMEOUT:
                    raise RuntimeError('Windows did not let Sender focus iRacing; no input was sent.')
                if self._last_focus_attempt is None or now - self._last_focus_attempt >= 0.15:
                    self.keyboard.focus(self._window)
                    self._last_focus_attempt = now
                return
            self.state = 'focus_settle'
            self._phase_started = now
            return
        if not foreground:
            raise RuntimeError('iRacing lost focus; return to pits was cancelled.')

        if self._action_sent and fresh:
            if not snapshot['in_car']:
                self._confirmation = 'iRacing confirmed that the driver exited the car.'
            elif snapshot['in_pit_stall'] and not self._initial_pit:
                self._confirmation = 'iRacing confirmed the car is in its pit stall.'
            elif tow > 0 and self._initial_tow == 0:
                self._confirmation = 'iRacing confirmed that towing to the pits has started.'
            if self._confirmation:
                self._finish(True, self._confirmation)
                return
        if not snapshot['in_car']:
            raise ValueError('The driver is no longer in the iRacing car; return to pits was cancelled.')

        if self.state == 'focus_settle':
            if now - self._phase_started < self.FOCUS_SETTLE:
                return
            if tow > 0:
                raise ValueError('iRacing is already towing the car; no input was sent.')
            self._check_held_keys()
            self._initial_pit, self._initial_tow = snapshot['in_pit_stall'], tow
            self._brake_started, self._brake_sample = now, sampled_at
            if self.brake.press() is False:
                raise RuntimeError('The iRacing brake input could not be applied.')
            self.state = 'braking'
            self.message = 'Braking in iRacing; waiting for the car to stop.'
            return

        if self._brake_verified and reported_brake < 0.5:
            raise RuntimeError('iRacing stopped reporting the brake input; return to pits was cancelled.')
        if self.state == 'braking':
            if fresh and sampled_at > self._brake_sample and reported_brake >= 0.5:
                self._brake_verified = True
            if not self._brake_verified and now - self._brake_started >= self.BRAKE_TIMEOUT:
                raise RuntimeError('iRacing did not report brake input. Assign the saved brake key to Second Brake in iRacing and test again.')
            if now - self._brake_started >= self.STOP_TIMEOUT:
                raise RuntimeError('The iRacing car did not stop within 30 seconds; reset was not sent.')
            if not fresh:
                return
            if not self._brake_verified or speed > self.STOP_SPEED:
                self._stopped_since = None
                return
            if self._stopped_since is None:
                self._stopped_since = sampled_at
                return
            if sampled_at - self._stopped_since < self.STOP_DWELL:
                return
            if now - self._started < self.reset_delay_seconds:
                return
            self._check_held_keys()
            self._start_reset(now)
            return

        if speed > self.STOP_SPEED:
            raise RuntimeError('The iRacing car moved during reset; the reset key and brake were released.')
        if self.state == 'resetting':
            if now - self._phase_started >= self.hold_seconds:
                self._release((self.keys[-1],))
                self.state = 'confirming'
                self._phase_started = now
                self.message = 'Reset shortcut sent; waiting for iRacing to confirm return to pits.'
        elif self.state == 'confirming' and now - self._phase_started >= self.CONFIRM_TIMEOUT:
            self._finish(False, 'Reset shortcut was held, but iRacing did not confirm a return to pits. Check the reset binding and hold duration in iRacing.')

    def _check_held_keys(self):
        keys = (set(self.keys) | {0x10, 0x11, 0x12, 0x5B, 0x5C}) - set(self._pressed)
        if self.keyboard.held(keys):
            raise RuntimeError('A shortcut key or modifier is already held; release the keyboard keys and try again.')

    def _start_reset(self, now):
        self._press((self.keys[-1],))
        self._action_sent = True
        self.state = 'resetting'
        self._phase_started = now
        self.message = 'Car stopped; holding the iRacing reset shortcut.'
