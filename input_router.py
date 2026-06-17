import sys
import termios
import tty
import select
import signal
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
        b'\x1b[1;5A': ('up', False),
        b'\x1b[1;5B': ('down', False),
        b'\x1b[1;5C': ('right', False),
        b'\x1b[1;5D': ('left', False),
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
        b'\x1bOP': 'F1',
        b'\x1bOQ': 'F2',
        b'\x1bOR': 'F3',
        b'\x1bOS': 'F4',
    }

    SPECIAL_KEYS = {
        b'\x7f': 'backspace',
        b'\x08': 'backspace',
        b'\x0d': 'enter',
        b'\x0a': 'enter',
        b'\x09': 'tab',
        b'\x1b[Z': 'shift_tab',
        b'\x1b[3~': 'delete',
        b'\x1b[H': 'home',
        b'\x1b[F': 'end',
        b'\x1b[5~': 'pageup',
        b'\x1b[6~': 'pagedown',
        b'\x1b[2~': 'insert',
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
    }

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
        if key_str.startswith('C-') and len(key_str) > 2:
            char = key_str[2].lower()
            return bytes([ord(char) - ord('a') + 1])
        elif key_str.startswith('M-') and len(key_str) > 2:
            return b'\x1b' + key_str[2:].encode()
        elif key_str == 'Space':
            return b' '
        elif key_str == 'Enter':
            return b'\x0d'
        elif key_str == 'Tab':
            return b'\x09'
        elif key_str == 'BS':
            return b'\x7f'
        elif key_str == 'ESC':
            return b'\x1b'
        else:
            return key_str.encode()

    def set_mode(self, mode: InputMode) -> None:
        self.mode = mode
        if mode == InputMode.PREFIX:
            self._start_prefix_timeout()

    def _start_prefix_timeout(self) -> None:
        signal.signal(signal.SIGALRM, self._prefix_timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, self.prefix_timeout, 0)

    def _prefix_timeout_handler(self, signum, frame):
        if self.mode == InputMode.PREFIX:
            self.mode = InputMode.NORMAL
            if self.on_input:
                self.on_input(self.PREFIX_KEY)

    def process_input(self, data: bytes) -> None:
        if not data:
            return
        
        self.pending_data.extend(data)
        
        while self.pending_data:
            consumed = self._parse_next_key()
            if consumed == 0:
                break

    def _parse_next_key(self) -> int:
        data = bytes(self.pending_data)
        
        if data[0:1] == self.PREFIX_KEY and self.mode == InputMode.NORMAL:
            self.mode = InputMode.PREFIX
            self._start_prefix_timeout()
            del self.pending_data[:1]
            return 1
        
        event = self._parse_key(data)
        if event is None:
            return 0
        
        consumed = len(event.raw)
        del self.pending_data[:consumed]
        
        if self.mode == InputMode.PREFIX:
            signal.setitimer(signal.ITIMER_REAL, 0, 0)
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
        
        for pattern, (name, shift) in self.ARROW_KEYS.items():
            if data.startswith(pattern):
                return KeyEvent(
                    type=KeyType.ARROW,
                    key=name,
                    raw=pattern,
                    shift=shift
                )
        
        for pattern, name in self.FUNCTION_KEYS.items():
            if data.startswith(pattern):
                return KeyEvent(
                    type=KeyType.FUNCTION,
                    key=name,
                    raw=pattern
                )
        
        for pattern, name in self.SPECIAL_KEYS.items():
            if data.startswith(pattern):
                return KeyEvent(
                    type=KeyType.SPECIAL,
                    key=name,
                    raw=pattern
                )
        
        if data[0:1] in self.CONTROL_CHARS:
            char = self.CONTROL_CHARS[data[0:1]]
            if char == 'escape' and len(data) == 1:
                return KeyEvent(
                    type=KeyType.SPECIAL,
                    key='escape',
                    raw=b'\x1b'
                )
            return KeyEvent(
                type=KeyType.CONTROL,
                key=char,
                raw=data[0:1],
                ctrl=True
            )
        
        if data[0:1] == b'\x1b' and len(data) > 1:
            next_byte = data[1:2]
            if next_byte not in b'[O]':
                return KeyEvent(
                    type=KeyType.ALT,
                    key=next_byte.decode('latin-1', errors='replace'),
                    raw=data[0:2],
                    alt=True
                )
        
        if data[0] >= 0x20 and data[0] < 0x7f:
            return KeyEvent(
                type=KeyType.CHAR,
                key=chr(data[0]),
                raw=data[0:1]
            )
        
        try:
            char = data[0:1].decode('utf-8')
            return KeyEvent(
                type=KeyType.CHAR,
                key=char,
                raw=data[0:1]
            )
        except UnicodeDecodeError:
            return KeyEvent(
                type=KeyType.CHAR,
                key='?',
                raw=data[0:1]
            )

    def _handle_prefix_key(self, event: KeyEvent) -> bool:
        if event.raw in self.bindings:
            binding = self.bindings[event.raw]
            if binding.require_prefix:
                binding.handler()
                return True
        
        if event.is_control('d'):
            if 'detach' in [b.description for b in self.bindings.values()]:
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
        
        self.original_termios = termios.tcgetattr(self.input_fd)
        tty.setraw(self.input_fd)
        
        new_attrs = termios.tcgetattr(self.input_fd)
        new_attrs[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
                          termios.ISTRIP | termios.INLCR | termios.IGNCR |
                          termios.ICRNL | termios.IXON)
        new_attrs[1] &= ~termios.OPOST
        new_attrs[2] &= ~(termios.CSIZE | termios.PARENB)
        new_attrs[2] |= termios.CS8
        new_attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
                          termios.ISIG | termios.IEXTEN)
        termios.tcsetattr(self.input_fd, termios.TCSANOW, new_attrs)

    def exit_raw_mode(self) -> None:
        if self.original_termios is not None and sys.stdin.isatty():
            termios.tcsetattr(self.input_fd, termios.TCSANOW, self.original_termios)
            self.original_termios = None

    def read_available(self, timeout: float = 0.01) -> bytes:
        if not sys.stdin.isatty():
            return b''
        
        try:
            r, _, _ = select.select([self.input_fd], [], [], timeout)
            if r:
                return os.read(self.input_fd, 4096)
        except (OSError, select.error):
            pass
        
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


import os
