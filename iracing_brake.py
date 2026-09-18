"""Hold a dedicated key assigned to iRacing's Second Brake.

Importing this module and constructing KeyboardBrake only validate settings.
Native input is loaded lazily by press(), after the return sequence has checked
telemetry and focused iRacing. No virtual controller or helper app is needed.
"""
import threading

from session_hotkey import WindowsKeyboard, parse_shortcut


class KeyboardBrakeError(RuntimeError):
    """The configured brake key could not be applied or released safely."""


def _single_key(shortcut, role, example):
    keys = parse_shortcut(shortcut)
    if len(keys) != 1:
        raise ValueError(f'Choose one iRacing {role} key without Ctrl, Alt or Shift, for example {example}.')
    return keys


def validate_return_shortcuts(brake_shortcut, reset_shortcut):
    """Return distinct, unmodified brake and reset key tuples.

    iRacing can stop recognizing a plain brake binding when a reset
    modifier goes down, so every key in the sequence must be unmodified.
    """
    brake_keys = _single_key(brake_shortcut, 'brake', 'F8')
    reset_keys = _single_key(reset_shortcut, 'reset', 'F9') if reset_shortcut is not None else ()
    all_keys = brake_keys + reset_keys
    if len(set(all_keys)) != len(all_keys):
        raise ValueError('The iRacing brake and reset controls must use different keys.')
    return brake_keys, reset_keys


def validate_brake_shortcut(shortcut, reset_shortcut=None):
    """Return the brake tuple while validating its reset pairing, if supplied."""
    return validate_return_shortcuts(shortcut, reset_shortcut)[0]


class KeyboardBrake:
    """Press once, hold until released, and clean up only our attempted input.

The caller owns the stop/reset timing and must call close() when finished.
Only key-up events are retried. If Windows rejects every release attempt,
close()/release() can be called again to retry cleanup even after closing.
"""

    def __init__(self, shortcut='F8', *, reset_shortcut=None, keyboard=None):
        self.brake_keys, _ = validate_return_shortcuts(shortcut, reset_shortcut)
        self.keys = self.brake_keys
        self.shortcut = str(shortcut).strip()
        self._keyboard = keyboard
        self._pending_release = set()
        self._closed = False
        self._lock = threading.RLock()

    def press(self):
        with self._lock:
            if self._closed:
                raise KeyboardBrakeError('The iRacing keyboard brake is closed.')
            if self._pending_release:
                return
            try:
                if self._keyboard is None:
                    self._keyboard = WindowsKeyboard()
                keyboard = self._keyboard
                window = keyboard.find_game('iRacing')
                if not window or keyboard.foreground() != window:
                    raise KeyboardBrakeError('iRacing must be in the foreground before applying the brake key.')
                if keyboard.held(set(self.keys) | {0x10, 0x11, 0x12, 0x5B, 0x5C}):
                    raise KeyboardBrakeError('The brake key or a modifier is already held; release the keyboard keys and try again.')
                if keyboard.foreground() != window:
                    raise KeyboardBrakeError('iRacing lost focus before the brake key was pressed.')
                # Even an exception can occur after Windows accepted input.
                # Mark every attempted key before insertion so cleanup covers
                # partially accepted batches as well as unknown native errors.
                self._pending_release.update(self.keys)
                count = keyboard.send([(key, False) for key in self.keys])
                if type(count) is not int or count != len(self.keys):
                    raise KeyboardBrakeError('Windows blocked the iRacing brake key. Check that Sender and iRacing have the same administrator permissions.')
            except Exception as exc:
                self._closed = True
                try:
                    self.release()
                except Exception as cleanup:
                    raise KeyboardBrakeError(f'{exc} Brake cleanup also failed: {cleanup}') from exc
                if isinstance(exc, KeyboardBrakeError):
                    raise
                raise KeyboardBrakeError(f'The iRacing brake key could not be pressed: {exc}') from exc

    def release(self):
        with self._lock:
            if not self._pending_release:
                return
            # Release each key independently so one failed release cannot stop
            # the others. Retain only failed keys for another cleanup attempt.
            for _ in range(2):
                for key in reversed(self.keys):
                    if key not in self._pending_release:
                        continue
                    try:
                        count = self._keyboard.send([(key, True)])
                        if type(count) is int and count == 1:
                            self._pending_release.discard(key)
                    except Exception:
                        pass
                if not self._pending_release:
                    return
            raise KeyboardBrakeError('Windows blocked brake key release. Release the configured key manually.')

    def close(self):
        with self._lock:
            self._closed = True
            self.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False
