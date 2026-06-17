import sys
import os
import errno
import termios
import tty
import select
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Union
from enum import Enum


class KeyType(Enum):
    CHAR = "char"
    CONTROL = "control"
    ALT = "alt"
    FUNCTION = "function"
    ARROW = "arrow"
    SPECIAL = "special"


@dataclass
class KeyEvent:
    type: KeyType
    key: str
    raw: bytes
    ctrl: bool = False
    alt: bool = False
    shift: bool = False

    def is_control(self, char: str) -> bool:
        return (self.type == KeyType.CONTROL and
                self.key.lower() == char.lower())

    def is_char(self, char: Optional[str] = None) -> bool:
        if char is None:
            return self.type == KeyType.CHAR
        return self.type == KeyType.CHAR and self.key == char

    def is_escape(self) -> bool:
        return self.raw == b'\x1b'


class InputMode(Enum):
    NORMAL = "normal"
    PREFIX = "prefix"
    COMMAND = "command"


@dataclass
class KeyBinding:
    key: Union[str, bytes]
    description: str
    handler: Callable[[], None]
    require_prefix: bool = True


class InputRouter:
    PREFIX_KEY = b'\x02'
    ARROW_KEYS = {
        b'\x1b[A': ('up', False),
        b'\x1b[B': ('down', False),
        b'\x1b[C': ('right', False),
        b'\x1b[D': ('left', False),
        b'\x1b[1;2A': ('up', True),
        b'\x1b[1;2B': ('down', True),
        b'\x1b[1;2C': ('right', True),
        b'\x1b[1;2D': ('left', True),
        b'\x1b[1;3A': ('up', False),
        b'\x1b[1;3B': ('down', False),
        b'\x1b[1;3C': ('right', False),
        b'\x1b[1;3D': ('left', False),
        b'\x1b[1;4A': ('up', True),
        b'\x1b[1;4B': ('down', True),
        b'\x1b[1;4C': ('right', True),
        b'\x1b[1;4D': ('left', True),
        b'\x1b[1;5A': ('up', False),
        b'\x1b[1;5B': ('down', False),
        b'\x1b[1;5C': ('right', False),
        b'\x1b[1;5D': ('left', False),
        b'\x1b[1;6A': ('up', True),
        b'\x1b[1;6B': ('down', True),
        b'\x1b[1;6C': ('right', True),
        b'\x1b[1;6D': ('left', True),
        b'\x1bOA': ('up', False),
        b'\x1bOB': ('down', False),
        b'\x1bOC': ('right', False),
        b'\x1bOD': ('left', False),
    }

    FUNCTION_KEYS = {
        b'\x1b[11~': 'F1',
        b'\x1b[12~': 'F2',
        b'\x1b[13~': 'F3',
        b'\x1b[14~': 'F4',
        b'\x1b[15~': 'F5',
        b'\x1b[17~': 'F6',
        b'\x1b[18~': 'F7',
        b'\x1b[19~': 'F8',
        b'\x1b[20~': 'F9',
        b'\x1b[21~': 'F10',
        b'\x1b[23~': 'F11',
        b'\x1b[24~': 'F12',
        b'\x1b[25~': 'F13',
        b'\x1b[26~': 'F14',
        b'\x1b[28~': 'F15',
        b'\x1b[29~': 'F16',
        b'\x1b[31~': 'F17',
        b'\x1b[32~': 'F18',
        b'\x1b[33~': 'F19',
        b'\x1b[34~': 'F20',
        b'\x1bOP': 'F1',
        b'\x1bOQ': 'F2',
        b'\x1bOR': 'F3',
        b'\x1bOS': 'F4',
        b'\x1b[1;2P': 'F13',
        b'\x1b[1;2Q': 'F14',
        b'\x1b[1;2R': 'F15',
        b'\x1b[1;2S': 'F16',
    }

    SPECIAL_KEYS = {
        b'\x7f': 'backspace',
        b'\x08': 'backspace',
        b'\x0d': 'enter',
        b'\x0a': 'enter',
        b'\x09': 'tab',
        b'\x1b[Z': 'shift_tab',
        b'\x1b[3~': 'delete',
        b'\x1b[3;2~': 'delete',
        b'\x1b[3;5~': 'delete',
        b'\x1b[H': 'home',
        b'\x1b[F': 'end',
        b'\x1b[1~': 'home',
        b'\x1b[4~': 'end',
        b'\x1b[7~': 'home',
        b'\x1b[8~': 'end',
        b'\x1bOH': 'home',
        b'\x1bOF': 'end',
        b'\x1b[1;2H': 'home',
        b'\x1b[1;2F': 'end',
        b'\x1b[1;5H': 'home',
        b'\x1b[1;5F': 'end',
        b'\x1b[5~': 'pageup',
        b'\x1b[6~': 'pagedown',
        b'\x1b[5;2~': 'pageup',
        b'\x1b[6;2~': 'pagedown',
        b'\x1b[5;5~': 'pageup',
        b'\x1b[6;5~': 'pagedown',
        b'\x1b[2~': 'insert',
        b'\x1b[2;2~': 'insert',
        b'\x1b[2;5~': 'insert',
    }

    CONTROL_CHARS = {
        b'\x01': 'a', b'\x02': 'b', b'\x03': 'c', b'\x04': 'd',
        b'\x05': 'e', b'\x06': 'f', b'\x07': 'g', b'\x08': 'h',
        b'\x09': 'i', b'\x0a': 'j', b'\x0b': 'k', b'\x0c': 'l',
        b'\x0d': 'm', b'\x0e': 'n', b'\x0f': 'o', b'\x10': 'p',
        b'\x11': 'q', b'\x12': 'r', b'\x13': 's', b'\x14': 't',
        b'\x15': 'u', b'\x16': 'v', b'\x17': 'w', b'\x18': 'x',
        b'\x19': 'y', b'\x1a': 'z',
        b'\x1b': 'escape',
        b'\x1c': '\\', b'\x1d': ']', b'\x1e': '^', b'\x1f': '_',
        b'\x7f': 'backspace',
    }

    CSI_ESCAPE_PREFIXES = (b'[', b'O')

    def __init__(self):
        self.mode = InputMode.NORMAL
        self.prefix_timeout = 0.5
        self.bindings: dict[bytes, KeyBinding] = {}
        self.on_input: Optional[Callable[[bytes], None]] = None
        self.on_key: Optional[Callable[[KeyEvent], None]] = None
        self.pending_data = bytearray()
        self.original_termios: Optional[list] = None
        self.input_fd = sys.stdin.fileno()
        self.running = False
        self.prefix_start_time: Optional[float] = None
        self.escape_timeout = 0.02
        self.last_escape_time: Optional[float] = None

    def register_binding(self, key: Union[str, bytes], handler: Callable[[], None],
                        description: str = "", require_prefix: bool = True) -> None:
        if isinstance(key, str):
            key_bytes = self._key_string_to_bytes(key)
        else:
            key_bytes = key

        self.bindings[key_bytes] = KeyBinding(
            key=key,
            description=description,
            handler=handler,
            require_prefix=require_prefix
        )

    def _key_string_to_bytes(self, key_str: str) -> bytes:
        if not key_str:
            return b''

        key_upper = key_str.upper()
        modifiers = []
        rest = key_str

        while True:
            if rest.startswith('C-') or rest.startswith('c-'):
                modifiers.append('ctrl')
                rest = rest[2:]
            elif rest.startswith('M-') or rest.startswith('m-'):
                modifiers.append('alt')
                rest = rest[2:]
            elif rest.startswith('S-') or rest.startswith('s-'):
                modifiers.append('shift')
                rest = rest[2:]
            else:
                break

        if rest == 'Space':
            result = b' '
        elif rest == 'Enter':
            result = b'\x0d'
        elif rest == 'Return':
            result = b'\x0d'
        elif rest == 'Tab':
            result = b'\x09'
        elif rest == 'BS' or rest == 'Backspace':
            result = b'\x7f'
        elif rest == 'ESC' or rest == 'Escape':
            result = b'\x1b'
        elif rest == 'Del' or rest == 'Delete':
            result = b'\x1b[3~'
        elif rest == 'Ins' or rest == 'Insert':
            result = b'\x1b[2~'
        elif rest == 'Home':
            result = b'\x1b[H'
        elif rest == 'End':
            result = b'\x1b[F'
        elif rest == 'PgUp' or rest == 'PageUp':
            result = b'\x1b[5~'
        elif rest == 'PgDn' or rest == 'PageDown':
            result = b'\x1b[6~'
        elif rest == 'Up':
            if 'ctrl' in modifiers and 'shift' in modifiers:
                result = b'\x1b[1;6A'
            elif 'ctrl' in modifiers:
                result = b'\x1b[1;5A'
            elif 'shift' in modifiers:
                result = b'\x1b[1;2A'
            elif 'alt' in modifiers:
                result = b'\x1b[1;3A'
            else:
                result = b'\x1b[A'
            modifiers.clear()
        elif rest == 'Down':
            if 'ctrl' in modifiers and 'shift' in modifiers:
                result = b'\x1b[1;6B'
            elif 'ctrl' in modifiers:
                result = b'\x1b[1;5B'
            elif 'shift' in modifiers:
                result = b'\x1b[1;2B'
            elif 'alt' in modifiers:
                result = b'\x1b[1;3B'
            else:
                result = b'\x1b[B'
            modifiers.clear()
        elif rest == 'Right':
            if 'ctrl' in modifiers and 'shift' in modifiers:
                result = b'\x1b[1;6C'
            elif 'ctrl' in modifiers:
                result = b'\x1b[1;5C'
            elif 'shift' in modifiers:
                result = b'\x1b[1;2C'
            elif 'alt' in modifiers:
                result = b'\x1b[1;3C'
            else:
                result = b'\x1b[C'
            modifiers.clear()
        elif rest == 'Left':
            if 'ctrl' in modifiers and 'shift' in modifiers:
                result = b'\x1b[1;6D'
            elif 'ctrl' in modifiers:
                result = b'\x1b[1;5D'
            elif 'shift' in modifiers:
                result = b'\x1b[1;2D'
            elif 'alt' in modifiers:
                result = b'\x1b[1;3D'
            else:
                result = b'\x1b[D'
            modifiers.clear()
        elif len(rest) == 1 and rest.isalpha():
            char = rest.lower()
            if 'ctrl' in modifiers:
                result = bytes([ord(char) - ord('a') + 1])
            else:
                result = rest.encode()
        elif len(rest) == 1:
            result = rest.encode()
        else:
            result = rest.encode()

        if 'alt' in modifiers:
            result = b'\x1b' + result

        return result

    def set_mode(self, mode: InputMode) -> None:
        self.mode = mode
        if mode == InputMode.PREFIX:
            self.prefix_start_time = time.monotonic()

    def _check_prefix_timeout(self) -> None:
        if self.mode == InputMode.PREFIX and self.prefix_start_time is not None:
            elapsed = time.monotonic() - self.prefix_start_time
            if elapsed >= self.prefix_timeout:
                self.mode = InputMode.NORMAL
                prefix_key = self.PREFIX_KEY
                self.prefix_start_time = None
                if self.on_input:
                    self.on_input(prefix_key)

    def send_prefix(self) -> None:
        self.set_mode(InputMode.PREFIX)

    def send_raw(self, data: bytes) -> None:
        if not data:
            return
        self.process_input(data)

    def process_input(self, data: bytes) -> None:
        if not data:
            return

        self.pending_data.extend(data)

        self._check_prefix_timeout()

        while self.pending_data:
            consumed = self._parse_next_key()
            if consumed == 0:
                break

    def _parse_next_key(self) -> int:
        data = bytes(self.pending_data)

        self._check_prefix_timeout()

        if data[0:1] == self.PREFIX_KEY and self.mode == InputMode.NORMAL:
            self.set_mode(InputMode.PREFIX)
            del self.pending_data[:1]
            return 1

        self._check_prefix_timeout()
        if self.mode == InputMode.PREFIX and self.prefix_start_time is None:
            pass

        event = self._parse_key(data)
        if event is None:
            return 0

        consumed = len(event.raw)
        if consumed > len(self.pending_data):
            return 0
        del self.pending_data[:consumed]

        if self.mode == InputMode.PREFIX:
            self.prefix_start_time = None
            if self._handle_prefix_key(event):
                self.mode = InputMode.NORMAL
                return consumed
            self.mode = InputMode.NORMAL

        if self._handle_binding(event):
            return consumed

        if self.mode == InputMode.NORMAL:
            if self.on_input:
                self.on_input(event.raw)
            if self.on_key:
                self.on_key(event)

        return consumed

    def _parse_key(self, data: bytes) -> Optional[KeyEvent]:
        if not data:
            return None

        data_len = len(data)
        first_byte = data[0:1]

        if first_byte == b'\x1b':
            if data_len == 1:
                now = time.monotonic()
                if self.last_escape_time is None:
                    self.last_escape_time = now
                    return None
                elapsed = now - self.last_escape_time
                if elapsed < self.escape_timeout:
                    return None
                self.last_escape_time = None
                return KeyEvent(
                    type=KeyType.SPECIAL,
                    key='escape',
                    raw=b'\x1b'
                )
            self.last_escape_time = None

            for pattern, (name, shift) in sorted(self.ARROW_KEYS.items(),
                                                  key=lambda x: len(x[0]), reverse=True):
                if data.startswith(pattern):
                    return KeyEvent(
                        type=KeyType.ARROW,
                        key=name,
                        raw=pattern,
                        shift=shift,
                        ctrl=self._is_ctrl_modifier(pattern),
                        alt=self._is_alt_modifier(pattern)
                    )

            for pattern, name in sorted(self.FUNCTION_KEYS.items(),
                                         key=lambda x: len(x[0]), reverse=True):
                if data.startswith(pattern):
                    return KeyEvent(
                        type=KeyType.FUNCTION,
                        key=name,
                        raw=pattern,
                        shift=self._is_shift_modifier(pattern),
                        ctrl=self._is_ctrl_modifier(pattern),
                        alt=self._is_alt_modifier(pattern)
                    )

            for pattern, name in sorted(self.SPECIAL_KEYS.items(),
                                         key=lambda x: len(x[0]), reverse=True):
                if data.startswith(pattern):
                    return KeyEvent(
                        type=KeyType.SPECIAL,
                        key=name,
                        raw=pattern,
                        shift=self._is_shift_modifier(pattern),
                        ctrl=self._is_ctrl_modifier(pattern),
                        alt=self._is_alt_modifier(pattern)
                    )

            if data_len >= 3 and data[1:2] in self.CSI_ESCAPE_PREFIXES:
                csi_data = data[2:]
                param_bytes = bytearray()
                intermediate_bytes = bytearray()
                final_byte = None
                idx = 0

                while idx < len(csi_data):
                    b = csi_data[idx:idx + 1]
                    if 0x30 <= b[0] <= 0x3f:
                        param_bytes.extend(b)
                        idx += 1
                    elif 0x20 <= b[0] <= 0x2f:
                        intermediate_bytes.extend(b)
                        idx += 1
                    elif 0x40 <= b[0] <= 0x7e:
                        final_byte = b
                        break
                    else:
                        break

                if final_byte is not None:
                    total_len = 2 + idx + 1
                    raw = data[:total_len]
                    return KeyEvent(
                        type=KeyType.SPECIAL,
                        key=f'csi_{final_byte.decode("latin-1")}',
                        raw=raw
                    )
                else:
                    return None

            if data_len >= 2 and data[1:2] not in self.CSI_ESCAPE_PREFIXES:
                second = data[1:2]
                if data_len >= 3:
                    try:
                        utf8_char = data[1:].decode('utf-8')
                        raw = b'\x1b' + utf8_char.encode('utf-8')
                        return KeyEvent(
                            type=KeyType.ALT,
                            key=utf8_char,
                            raw=raw,
                            alt=True
                        )
                    except UnicodeDecodeError:
                        pass
                return KeyEvent(
                    type=KeyType.ALT,
                    key=second.decode('latin-1', errors='replace'),
                    raw=data[0:2],
                    alt=True
                )

            return None

        for pattern, name in sorted(self.SPECIAL_KEYS.items(),
                                     key=lambda x: len(x[0]), reverse=True):
            if pattern != b'\x1b' and data.startswith(pattern):
                return KeyEvent(
                    type=KeyType.SPECIAL,
                    key=name,
                    raw=pattern
                )

        if first_byte in self.CONTROL_CHARS:
            char = self.CONTROL_CHARS[first_byte]
            if char == 'escape':
                return KeyEvent(
                    type=KeyType.SPECIAL,
                    key='escape',
                    raw=first_byte
                )
            if char == 'backspace':
                return KeyEvent(
                    type=KeyType.SPECIAL,
                    key='backspace',
                    raw=first_byte
                )
            return KeyEvent(
                type=KeyType.CONTROL,
                key=char,
                raw=first_byte,
                ctrl=True
            )

        if data[0] >= 0x20 and data[0] < 0x7f:
            return KeyEvent(
                type=KeyType.CHAR,
                key=chr(data[0]),
                raw=data[0:1]
            )

        utf8_len = self._utf8_char_length(data[0])
        if utf8_len > 1 and data_len >= utf8_len:
            try:
                char = data[:utf8_len].decode('utf-8')
                return KeyEvent(
                    type=KeyType.CHAR,
                    key=char,
                    raw=data[:utf8_len]
                )
            except UnicodeDecodeError:
                pass

        if utf8_len > 1:
            return None

        return KeyEvent(
            type=KeyType.CHAR,
            key='?',
            raw=data[0:1]
        )

    @staticmethod
    def _utf8_char_length(first_byte: int) -> int:
        if first_byte < 0x80:
            return 1
        elif (first_byte & 0xe0) == 0xc0:
            return 2
        elif (first_byte & 0xf0) == 0xe0:
            return 3
        elif (first_byte & 0xf8) == 0xf0:
            return 4
        return 1

    @staticmethod
    def _is_shift_modifier(pattern: bytes) -> bool:
        pattern_str = pattern.decode('latin-1', errors='replace')
        if ';2' in pattern_str:
            return True
        if pattern in (b'\x1b[Z',):
            return True
        return False

    @staticmethod
    def _is_ctrl_modifier(pattern: bytes) -> bool:
        pattern_str = pattern.decode('latin-1', errors='replace')
        if ';5' in pattern_str or ';6' in pattern_str:
            return True
        return False

    @staticmethod
    def _is_alt_modifier(pattern: bytes) -> bool:
        pattern_str = pattern.decode('latin-1', errors='replace')
        if ';3' in pattern_str or ';4' in pattern_str:
            return True
        return False

    def _handle_prefix_key(self, event: KeyEvent) -> bool:
        if event.raw in self.bindings:
            binding = self.bindings[event.raw]
            if binding.require_prefix:
                binding.handler()
                return True

        if event.is_control('d') or event.is_char('d') or event.key == 'd':
            for b in self.bindings.values():
                if b.description == 'detach':
                    b.handler()
                    return True

        if event.is_char('"') or event.key == '"':
            for b in self.bindings.values():
                if b.description == 'split_horizontal':
                    b.handler()
                    return True

        if event.is_char('%') or event.key == '%':
            for b in self.bindings.values():
                if b.description == 'split_vertical':
                    b.handler()
                    return True

        if event.key == 'up':
            for b in self.bindings.values():
                if b.description == 'focus_up':
                    b.handler()
                    return True

        if event.key == 'down':
            for b in self.bindings.values():
                if b.description == 'focus_down':
                    b.handler()
                    return True

        if event.key == 'left':
            for b in self.bindings.values():
                if b.description == 'focus_left':
                    b.handler()
                    return True

        if event.key == 'right':
            for b in self.bindings.values():
                if b.description == 'focus_right':
                    b.handler()
                    return True

        if event.is_char('x') or event.key == 'x':
            for b in self.bindings.values():
                if b.description == 'close_pane':
                    b.handler()
                    return True

        if event.is_char('?') or event.key == '?':
            for b in self.bindings.values():
                if b.description == 'help':
                    b.handler()
                    return True

        if event.is_char('s') or event.key == 's':
            for b in self.bindings.values():
                if b.description == 'list_sessions':
                    b.handler()
                    return True

        return False

    def _handle_binding(self, event: KeyEvent) -> bool:
        if event.raw in self.bindings:
            binding = self.bindings[event.raw]
            if not binding.require_prefix:
                binding.handler()
                return True
        return False

    def enter_raw_mode(self) -> None:
        if not sys.stdin.isatty():
            return

        try:
            self.original_termios = termios.tcgetattr(self.input_fd)
        except termios.error:
            self.original_termios = None
            return

        try:
            new_attrs = termios.tcgetattr(self.input_fd)

            new_attrs[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
                              termios.ISTRIP | termios.INLCR | termios.IGNCR |
                              termios.ICRNL | termios.IXON | termios.IXOFF |
                              termios.IXANY)
            new_attrs[0] |= termios.IGNPAR

            new_attrs[1] &= ~termios.OPOST
            new_attrs[1] &= ~termios.ONLCR

            new_attrs[2] &= ~(termios.CSIZE | termios.PARENB)
            new_attrs[2] |= termios.CS8
            new_attrs[2] &= ~termios.PARODD

            new_attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
                              termios.ISIG | termios.IEXTEN | termios.ECHOE |
                              termios.ECHOK | termios.ECHOKE | termios.ECHOPRT)

            new_attrs[6][termios.VMIN] = 0
            new_attrs[6][termios.VTIME] = 0

            termios.tcsetattr(self.input_fd, termios.TCSAFLUSH, new_attrs)

        except termios.error:
            if self.original_termios is not None:
                try:
                    termios.tcsetattr(self.input_fd, termios.TCSANOW, self.original_termios)
                except termios.error:
                    pass
                self.original_termios = None

    def exit_raw_mode(self) -> None:
        if self.original_termios is not None and sys.stdin.isatty():
            try:
                termios.tcsetattr(self.input_fd, termios.TCSAFLUSH, self.original_termios)
            except termios.error:
                try:
                    termios.tcsetattr(self.input_fd, termios.TCSANOW, self.original_termios)
                except termios.error:
                    pass
            finally:
                self.original_termios = None

    def read_available(self, timeout: float = 0.01) -> bytes:
        if not sys.stdin.isatty():
            return b''

        self._check_prefix_timeout()

        if self.pending_data:
            pass

        try:
            r, _, _ = select.select([self.input_fd], [], [], timeout)
        except (OSError, select.error, ValueError) as e:
            if isinstance(e, ValueError) and str(e) == 'file descriptor cannot be a negative integer (-1)':
                pass
            return b''
        except Exception:
            return b''

        if not r:
            return b''

        try:
            data = os.read(self.input_fd, 8192)
            if data is None:
                return b''
            return data
        except OSError as e:
            eagain = getattr(errno, 'EAGAIN', None)
            ewouldblock = getattr(errno, 'EWOULDBLOCK', None)
            if eagain is not None and e.errno == eagain:
                return b''
            if ewouldblock is not None and e.errno == ewouldblock:
                return b''
            if e.errno == 9:
                return b''
            return b''
        except Exception:
            return b''

    def start_listening(self, on_input: Callable[[bytes], None]) -> None:
        self.on_input = on_input
        self.running = True
        self.enter_raw_mode()

    def stop_listening(self) -> None:
        self.running = False
        self.exit_raw_mode()

    def loop_once(self) -> None:
        if not self.running:
            return

        self._check_prefix_timeout()

        data = self.read_available()
        if data:
            self.process_input(data)


class KeyboardInputReader:
    def __init__(self):
        self.input_router = InputRouter()
        self.handlers: dict[str, Callable] = {}
        self._register_default_handlers()

    def _register_default_handlers(self):
        pass

    def register_handler(self, name: str, handler: Callable) -> None:
        self.handlers[name] = handler

    def setup_bindings(self, session_manager, client_id: str) -> None:
        self.input_router.register_binding(
            'C-d', self._make_detach_handler(),
            description='detach', require_prefix=True
        )

        self.input_router.register_binding(
            '"', self._make_split_handler(session_manager, client_id, 'horizontal'),
            description='split_horizontal', require_prefix=True
        )

        self.input_router.register_binding(
            '%', self._make_split_handler(session_manager, client_id, 'vertical'),
            description='split_vertical', require_prefix=True
        )

        self.input_router.register_binding(
            'C-up', self._make_focus_handler(session_manager, client_id, 'up'),
            description='focus_up', require_prefix=True
        )

        self.input_router.register_binding(
            'C-down', self._make_focus_handler(session_manager, client_id, 'down'),
            description='focus_down', require_prefix=True
        )

        self.input_router.register_binding(
            'C-left', self._make_focus_handler(session_manager, client_id, 'left'),
            description='focus_left', require_prefix=True
        )

        self.input_router.register_binding(
            'C-right', self._make_focus_handler(session_manager, client_id, 'right'),
            description='focus_right', require_prefix=True
        )

        self.input_router.register_binding(
            'Up', self._make_focus_handler(session_manager, client_id, 'up'),
            description='focus_up_plain', require_prefix=True
        )

        self.input_router.register_binding(
            'Down', self._make_focus_handler(session_manager, client_id, 'down'),
            description='focus_down_plain', require_prefix=True
        )

        self.input_router.register_binding(
            'Left', self._make_focus_handler(session_manager, client_id, 'left'),
            description='focus_left_plain', require_prefix=True
        )

        self.input_router.register_binding(
            'Right', self._make_focus_handler(session_manager, client_id, 'right'),
            description='focus_right_plain', require_prefix=True
        )

        self.input_router.register_binding(
            'x', self._make_close_pane_handler(session_manager, client_id),
            description='close_pane', require_prefix=True
        )

        self.input_router.register_binding(
            '?', self._make_help_handler(),
            description='help', require_prefix=True
        )

        self.input_router.register_binding(
            's', self._make_list_sessions_handler(session_manager),
            description='list_sessions', require_prefix=True
        )

    def _make_detach_handler(self) -> Callable:
        def handler():
            if 'detach' in self.handlers:
                self.handlers['detach']()
        return handler

    def _make_split_handler(self, session_manager, client_id: str, direction: str) -> Callable:
        def handler():
            if 'split' in self.handlers:
                self.handlers['split'](direction)
        return handler

    def _make_focus_handler(self, session_manager, client_id: str, direction: str) -> Callable:
        def handler():
            if 'focus' in self.handlers:
                self.handlers['focus'](direction)
        return handler

    def _make_close_pane_handler(self, session_manager, client_id: str) -> Callable:
        def handler():
            if 'close_pane' in self.handlers:
                self.handlers['close_pane']()
        return handler

    def _make_help_handler(self) -> Callable:
        def handler():
            if 'help' in self.handlers:
                self.handlers['help']()
        return handler

    def _make_list_sessions_handler(self, session_manager) -> Callable:
        def handler():
            if 'list_sessions' in self.handlers:
                self.handlers['list_sessions']()
        return handler

    def set_input_callback(self, callback: Callable[[bytes], None]) -> None:
        self.input_router.on_input = callback

    def start(self) -> None:
        self.input_router.start_listening(self._on_raw_input)

    def stop(self) -> None:
        self.input_router.stop_listening()

    def _on_raw_input(self, data: bytes) -> None:
        pass

    def poll(self) -> None:
        self.input_router.loop_once()
