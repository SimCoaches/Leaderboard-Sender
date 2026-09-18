"""Optional game keyboard shortcuts. No hooks, background listeners or shell commands."""
import ctypes
from ctypes import wintypes
from fnmatch import fnmatchcase
import ntpath
import sys
import time


GAME_PROCESSES = {
    'Assetto Corsa': ('acs.exe', 'acs_x86.exe'),
    'iRacing': ('iracingsim64dx11.exe',),
    'BeamNG.drive': ('beamng.drive.x64.exe', 'beamng.drive.x64.vulkan.exe'),
    'EA Sports F1': ('f1_??.exe', 'f1_20??.exe'),
}
MODIFIERS = {'ctrl': 0x11, 'alt': 0x12, 'shift': 0x10}
KEYS = {**{chr(k): k for k in range(0x41, 0x5B)},
        **{str(k): 0x30 + k for k in range(10)},
        **{f'F{k}': 0x6F + k for k in range(1, 25)},
        'SPACE': 0x20, 'ENTER': 0x0D, 'RETURN': 0x0D,
        'TAB': 0x09, 'ESC': 0x1B, 'ESCAPE': 0x1B,
        'BACKSPACE': 0x08, 'DELETE': 0x2E, 'INSERT': 0x2D,
        'HOME': 0x24, 'END': 0x23, 'PGUP': 0x21, 'PGDOWN': 0x22,
        'UP': 0x26, 'DOWN': 0x28, 'LEFT': 0x25, 'RIGHT': 0x27}


def parse_shortcut(value):
    """One chord only; reject unsupported and Windows/system shortcuts."""
    parts = [p.strip() for p in str(value).split('+')]
    if not parts or not parts[-1] or any(p.lower() not in MODIFIERS for p in parts[:-1]):
        raise ValueError('Use one key with optional Ctrl, Alt or Shift, for example Ctrl+R.')
    modifiers = [p.lower() for p in parts[:-1]]
    if len(set(modifiers)) != len(modifiers) or parts[-1].upper() not in KEYS:
        raise ValueError('Choose A–Z, 0–9, F1–F24, or a navigation key, with optional Ctrl/Alt/Shift.')
    key = KEYS[parts[-1].upper()]
    if ('alt' in modifiers and key in (0x09, 0x1B, 0x73, 0x20)) or (
            'ctrl' in modifiers and key == 0x1B) or (
            'ctrl' in modifiers and 'alt' in modifiers and key == 0x2E):
        raise ValueError('Choose a game shortcut instead of a Windows or close-window shortcut.')
    return tuple(MODIFIERS[m] for m in ('ctrl', 'alt', 'shift') if m in modifiers) + (key,)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class INPUTUNION(ctypes.Union):
    # The mouse member is required for the native INPUT union's correct size.
    _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('type', wintypes.DWORD), ('data', INPUTUNION)]


class WindowsKeyboard:
    def __init__(self):
        if sys.platform != 'win32':
            raise RuntimeError('Game shortcuts require Windows.')
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        signatures = [
            (self.user.EnumWindows, [self.callback_type, wintypes.LPARAM], wintypes.BOOL),
            (self.user.IsWindowVisible, [wintypes.HWND], wintypes.BOOL),
            (self.user.IsIconic, [wintypes.HWND], wintypes.BOOL),
            (self.user.ShowWindow, [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            (self.user.GetForegroundWindow, [], wintypes.HWND),
            (self.user.SetForegroundWindow, [wintypes.HWND], wintypes.BOOL),
            (self.user.GetWindowThreadProcessId, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (self.user.GetAsyncKeyState, [ctypes.c_int], ctypes.c_short),
            (self.user.MapVirtualKeyW, [wintypes.UINT, wintypes.UINT], wintypes.UINT),
            (self.user.SendInput, [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int], wintypes.UINT),
            (self.kernel.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (self.kernel.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel.QueryFullProcessImageNameW,
             [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        ]
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result

    def find_game(self, game):
        matches = []
        patterns = GAME_PROCESSES.get(game, ())

        @self.callback_type
        def visit(hwnd, _):
            if not self.user.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = self.kernel.OpenProcess(0x1000, False, pid.value)
            if process:
                try:
                    size = wintypes.DWORD(32768)
                    path = ctypes.create_unicode_buffer(size.value)
                    if self.kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                        name = ntpath.basename(path.value).lower()
                        if any(fnmatchcase(name, pattern) for pattern in patterns):
                            matches.append(hwnd)
                finally:
                    self.kernel.CloseHandle(process)
            return True

        self.user.EnumWindows(visit, 0)
        foreground = self.foreground()
        return foreground if foreground in matches else (matches[0] if matches else None)

    def foreground(self):
        return self.user.GetForegroundWindow()

    def focus(self, hwnd):
        if self.user.IsIconic(hwnd):
            self.user.ShowWindow(hwnd, 9)  # SW_RESTORE
        self.user.SetForegroundWindow(hwnd)

    def held(self, keys):
        # Avoid combining our chord with keys the operator is holding.
        return any(self.user.GetAsyncKeyState(key) & 0x8000 for key in keys)

    def send(self, events):
        inputs = []
        for key, released in events:
            scan = self.user.MapVirtualKeyW(key, 0)
            flags = 0x0008 | (0x0002 if released else 0)
            if key in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E):
                flags |= 0x0001
            inputs.append(INPUT(type=1, ki=KEYBDINPUT(0, scan, flags, 0, 0)))
        batch = (INPUT * len(inputs))(*inputs)
        return self.user.SendInput(len(batch), batch, ctypes.sizeof(INPUT))


def send_game_shortcut(game, shortcut, backend=None, sleep=time.sleep):
    """Return (sent, message). Retry focus/releases, never the game action.

    Polling games need to observe the modifier before the action key and its
    release before the modifier is released. Sending both edges in the same
    batch makes those intermediate states invisible to some input handlers.
    """
    try:
        keys = parse_shortcut(shortcut)
        keyboard = backend if backend is not None else WindowsKeyboard()
        hwnd = keyboard.find_game(game)
        if not hwnd:
            return False, f'{game} game window not found. Start the game and check the selected game in Sender.'
        # Restoring a fullscreen game can take more than one frame. Only retry
        # acquiring focus: repeating an action key could toggle the action off.
        for _ in range(4):
            if keyboard.foreground() == hwnd:
                break
            keyboard.focus(hwnd)
            sleep(0.15)
        if keyboard.foreground() != hwnd:
            return False, 'Windows did not let Sender focus the game; no shortcut was sent.'
        sleep(0.10)  # Let the game resume input polling after gaining focus.
        if keyboard.held(set(keys) | {0x10, 0x11, 0x12, 0x5B, 0x5C}):
            return False, 'A shortcut key or modifier is already held; release the keyboard keys and test again.'
        pressed = []
        def press(batch):
            count = keyboard.send([(key, False) for key in batch])
            pressed.extend(batch[:count])
            if count != len(batch):
                raise RuntimeError('Windows blocked keyboard input. Check that Sender and the game have the same administrator permissions.')

        def release(batch):
            events = [(key, True) for key in batch]
            if keyboard.send(events) != len(events):
                # A duplicate key-up is harmless. Never repeat a key-down.
                if keyboard.send(events) != len(events):
                    raise RuntimeError('Windows blocked key release. Release the shortcut keys manually.')
            for key in batch:
                pressed.remove(key)

        try:
            # Recheck just before input. Never fall back to typing in Sender/browser.
            if keyboard.foreground() != hwnd:
                return False, 'The game lost focus; no shortcut was sent.'
            modifiers, action_key = keys[:-1], keys[-1]
            if modifiers:
                press(modifiers)
                sleep(0.08)
                if keyboard.foreground() != hwnd:
                    return False, 'The game lost focus; the action key was not sent.'
            press((action_key,))
            sleep(0.15)  # Hold across game frames so polling games see the chord.
            release((action_key,))
            if modifiers:
                sleep(0.08)  # Let the game see key-up while Ctrl/Alt/Shift remain held.
        finally:
            if pressed:
                release(tuple(reversed(pressed)))
        return True, f'Sent {shortcut} to {game}. Confirm the assigned action happened in the game.'
    except Exception as exc:
        return False, f'Shortcut could not be sent: {exc}'
