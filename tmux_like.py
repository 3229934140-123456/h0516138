import os
import sys
import signal
import uuid
import time
from typing import Optional, Dict, List

from pty_manager import PTYManager
from session_pane import SessionManager, Session, Pane
from layout import LayoutManager, Rect
from screen_buffer import ScreenBuffer
from input_router import KeyboardInputReader


class TerminalMultiplexer:
    def __init__(self):
        self.pty_manager = PTYManager()
        self.session_manager = SessionManager(self.pty_manager)
        self.layout_managers: Dict[str, LayoutManager] = {}
        self.input_readers: Dict[str, KeyboardInputReader] = {}
        self.client_id: Optional[str] = None
        self.terminal_width = 80
        self.terminal_height = 24
        self.running = False
        self.detached = False
        self.last_render = ""

    def start(self, session_name: str = "default"):
        self._get_terminal_size()
        self._setup_signal_handlers()
        
        session = self.session_manager.create_session(
            session_name, self.terminal_width, self.terminal_height
        )
        
        self.layout_managers[session.id] = LayoutManager(
            self.terminal_width, self.terminal_height
        )
        
        first_pane = session.get_focused_pane()
        if first_pane:
            self.layout_managers[session.id].set_initial_pane(first_pane.id)
        
        self.client_id = str(uuid.uuid4())[:8]
        self.session_manager.attach_session(session.id, self.client_id)
        
        input_reader = KeyboardInputReader()
        input_reader.setup_bindings(self.session_manager, self.client_id)
        input_reader.set_input_callback(self._on_pane_input)
        input_reader.register_handler('detach', self._detach)
        input_reader.register_handler('split', self._split_pane)
        input_reader.register_handler('focus', self._change_focus)
        input_reader.register_handler('close_pane', self._close_pane)
        input_reader.register_handler('help', self._show_help)
        input_reader.register_handler('list_sessions', self._list_sessions)
        input_reader.start()
        
        self.input_readers[self.client_id] = input_reader
        self.running = True
        
        print("\x1b[?1049h", end="", flush=True)
        print("\x1b[H\x1b[2J", end="", flush=True)
        
        try:
            self._main_loop()
        finally:
            self._cleanup()

    def attach(self, session_id: str):
        self._get_terminal_size()
        self._setup_signal_handlers()
        
        session = self.session_manager.get_session(session_id)
        if not session:
            print(f"Session {session_id} not found")
            return
        
        if session.id not in self.layout_managers:
            self.layout_managers[session.id] = LayoutManager(
                self.terminal_width, self.terminal_height
            )
            for pane in session.list_panes():
                self.layout_managers[session.id].set_initial_pane(pane.id)
                break
        
        self.client_id = str(uuid.uuid4())[:8]
        self.session_manager.attach_session(session_id, self.client_id)
        
        for pane in session.list_panes():
            self.pty_manager.resize_pty(
                pane.pty_fd, pane.width, pane.height
            )
        
        input_reader = KeyboardInputReader()
        input_reader.setup_bindings(self.session_manager, self.client_id)
        input_reader.set_input_callback(self._on_pane_input)
        input_reader.register_handler('detach', self._detach)
        input_reader.register_handler('split', self._split_pane)
        input_reader.register_handler('focus', self._change_focus)
        input_reader.register_handler('close_pane', self._close_pane)
        input_reader.register_handler('help', self._show_help)
        input_reader.register_handler('list_sessions', self._list_sessions)
        input_reader.start()
        
        self.input_readers[self.client_id] = input_reader
        self.running = True
        self.detached = False
        
        print("\x1b[?1049h", end="", flush=True)
        print("\x1b[H\x1b[2J", end="", flush=True)
        
        self._render_full_screen()
        
        try:
            self._main_loop()
        finally:
            self._cleanup()

    def _get_terminal_size(self):
        try:
            import fcntl
            import struct
            import termios
            
            data = fcntl.ioctl(
                sys.stdout.fileno(), termios.TIOCGWINSZ,
                struct.pack('HHHH', 0, 0, 0, 0)
            )
            rows, cols, _, _ = struct.unpack('HHHH', data)
            self.terminal_height = max(rows, 24)
            self.terminal_width = max(cols, 80)
        except Exception:
            self.terminal_height = 24
            self.terminal_width = 80

    def _setup_signal_handlers(self):
        signal.signal(signal.SIGWINCH, self._handle_resize)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_resize(self, signum, frame):
        self._get_terminal_size()
        self._resize_all_panes()

    def _handle_signal(self, signum, frame):
        if signum == signal.SIGINT:
            session = self.session_manager.get_client_session(self.client_id)
            if session:
                pane = session.get_focused_pane()
                if pane and pane.pty_fd:
                    self.pty_manager.write_to_pty(pane.pty_fd, b'\x03')
        else:
            self.running = False

    def _main_loop(self):
        while self.running and not self.detached:
            if self.client_id in self.input_readers:
                self.input_readers[self.client_id].poll()
            
            self.session_manager.process_pty_output()
            
            self._render_screen()
            
            time.sleep(0.01)

    def _on_pane_input(self, data: bytes):
        if self.client_id:
            self.session_manager.write_to_focused_pane(self.client_id, data)

    def _split_pane(self, direction: str):
        session = self.session_manager.get_client_session(self.client_id)
        if not session:
            return
        
        layout = self.layout_managers.get(session.id)
        if not layout:
            return
        
        current_pane = session.get_focused_pane()
        if not current_pane:
            return
        
        new_pane = self.session_manager.split_pane(
            session.id, direction, ratio=0.5
        )
        
        if new_pane:
            if direction == 'horizontal':
                layout.split_horizontal(current_pane.id, new_pane.id, 0.5)
            else:
                layout.split_vertical(current_pane.id, new_pane.id, 0.5)
            
            rect = layout.get_pane_rect(new_pane.id)
            if rect:
                new_pane.x, new_pane.y = rect.x, rect.y
                new_pane.width, new_pane.height = rect.width, rect.height
                self.pty_manager.resize_pty(
                    new_pane.pty_fd, rect.width, rect.height
                )
            
            for pid, prect in layout.get_all_pane_rects().items():
                pane = session.panes.get(pid)
                if pane:
                    pane.x, pane.y = prect.x, prect.y
                    pane.width, pane.height = prect.width, prect.height
            
            self._render_full_screen()

    def _change_focus(self, direction: str):
        session = self.session_manager.get_client_session(self.client_id)
        if not session:
            return
        
        layout = self.layout_managers.get(session.id)
        if not layout:
            return
        
        current_pane = session.get_focused_pane()
        if not current_pane:
            return
        
        cx, cy = current_pane.x + current_pane.width // 2, current_pane.y + current_pane.height // 2
        
        next_pane_id = None
        if direction == 'up':
            next_pane_id = layout.find_pane_at(cx, cy - 1)
        elif direction == 'down':
            next_pane_id = layout.find_pane_at(cx, cy + current_pane.height)
        elif direction == 'left':
            next_pane_id = layout.find_pane_at(cx - 1, cy)
        elif direction == 'right':
            next_pane_id = layout.find_pane_at(cx + current_pane.width, cy)
        
        if next_pane_id and next_pane_id != current_pane.id:
            session.focus_pane(next_pane_id)
            self._render_full_screen()

    def _close_pane(self):
        session = self.session_manager.get_client_session(self.client_id)
        if not session:
            return
        
        layout = self.layout_managers.get(session.id)
        if not layout:
            return
        
        current_pane = session.get_focused_pane()
        if not current_pane:
            return
        
        if len(session.panes) == 1:
            self.session_manager.kill_session(session.id)
            self.running = False
            return
        
        pane_id = current_pane.id
        if current_pane.pty_fd:
            self.pty_manager.destroy_pty(current_pane.pty_fd)
        
        session.remove_pane(pane_id)
        remaining_id = layout.remove_pane(pane_id)
        
        for pid, prect in layout.get_all_pane_rects().items():
            pane = session.panes.get(pid)
            if pane:
                pane.x, pane.y = prect.x, prect.y
                pane.width, pane.height = prect.width, prect.height
                self.pty_manager.resize_pty(
                    pane.pty_fd, prect.width, prect.height
                )
        
        if remaining_id:
            session.focus_pane(remaining_id)
        
        self._render_full_screen()

    def _resize_all_panes(self):
        session = self.session_manager.get_client_session(self.client_id)
        if not session:
            return
        
        layout = self.layout_managers.get(session.id)
        if not layout:
            return
        
        layout.resize_screen(self.terminal_width, self.terminal_height)
        
        for pane_id, rect in layout.get_all_pane_rects().items():
            pane = session.panes.get(pane_id)
            if pane:
                pane.x, pane.y = rect.x, rect.y
                pane.width, pane.height = rect.width, rect.height
                pane.resize(rect.width, rect.height)
                if pane.pty_fd:
                    self.pty_manager.resize_pty(
                        pane.pty_fd, rect.width, rect.height
                    )
        
        self._render_full_screen()

    def _render_screen(self):
        session = self.session_manager.get_client_session(self.client_id)
        if not session:
            return
        
        output = bytearray()
        
        for pane in session.list_panes():
            lines = pane.get_render_lines()
            for i, line in enumerate(lines):
                if i >= pane.height:
                    break
                
                output.extend(f"\x1b[{pane.y + i + 1};{pane.x + 1}H".encode())
                
                padded_line = line.ljust(pane.width)[:pane.width]
                output.extend(padded_line.encode('utf-8', errors='replace'))
            
            if pane.focus:
                cursor_x, cursor_y = pane.get_cursor()
                abs_x = pane.x + min(cursor_x, pane.width - 1)
                abs_y = pane.y + min(cursor_y, pane.height - 1)
                output.extend(f"\x1b[{abs_y + 1};{abs_x + 1}H".encode())
                output.extend(b"\x1b[?25h")
            else:
                output.extend(b"\x1b[?25l")
        
        current_output = output.decode('utf-8', errors='replace')
        if current_output != self.last_render:
            sys.stdout.write(current_output)
            sys.stdout.flush()
            self.last_render = current_output

    def _render_full_screen(self):
        sys.stdout.write("\x1b[H\x1b[2J")
        sys.stdout.flush()
        self.last_render = ""
        self._render_screen()

    def _detach(self):
        if self.client_id:
            self.session_manager.detach_session(self.client_id)
            if self.client_id in self.input_readers:
                self.input_readers[self.client_id].stop()
                del self.input_readers[self.client_id]
        self.detached = True
        print("\x1b[?1049l", end="", flush=True)
        print("\r\n[detached]\r\n", end="", flush=True)

    def _show_help(self):
        help_text = """
Terminal Multiplexer Help
=========================
Prefix: Ctrl+B

Key Bindings:
  "    - Split horizontally
  %    - Split vertically
  x    - Close current pane
  d    - Detach from session
  ?    - Show this help
  s    - List sessions
  Arrow keys - Change focus between panes
  Ctrl+C - Send SIGINT to focused pane

Resize terminal to adjust pane sizes automatically.
"""
        sys.stdout.write("\x1b[H\x1b[2J")
        sys.stdout.write(help_text)
        sys.stdout.write("\r\nPress any key to continue...")
        sys.stdout.flush()
        
        if self.client_id in self.input_readers:
            self.input_readers[self.client_id].stop()
        
        os.read(sys.stdin.fileno(), 1)
        
        if self.client_id in self.input_readers:
            self.input_readers[self.client_id].start()
        
        self._render_full_screen()

    def _list_sessions(self):
        sessions = self.session_manager.list_sessions()
        
        sys.stdout.write("\x1b[H\x1b[2J")
        sys.stdout.write("Available Sessions:\r\n")
        sys.stdout.write("=" * 60 + "\r\n")
        
        for session in sessions:
            attached = "(attached)" if session.attached else ""
            num_panes = len(session.panes)
            created = time.strftime("%Y-%m-%d %H:%M:%S", 
                                   time.localtime(session.created_at))
            sys.stdout.write(
                f"{session.id}: {session.name} "
                f"[{num_panes} pane(s)] {attached}\r\n"
                f"  Created: {created}\r\n"
                f"  Last activity: {time.ctime(session.last_activity)}\r\n"
                f"\r\n"
            )
        
        sys.stdout.write("\r\nPress any key to continue...")
        sys.stdout.flush()
        
        if self.client_id in self.input_readers:
            self.input_readers[self.client_id].stop()
        
        os.read(sys.stdin.fileno(), 1)
        
        if self.client_id in self.input_readers:
            self.input_readers[self.client_id].start()
        
        self._render_full_screen()

    def _cleanup(self):
        if self.client_id and self.client_id in self.input_readers:
            self.input_readers[self.client_id].stop()
        
        print("\x1b[?1049l", end="", flush=True)
        print("\x1b[?25h", end="", flush=True)
        print("\r\n", end="", flush=True)


def list_sessions():
    pty_manager = PTYManager()
    session_manager = SessionManager(pty_manager)
    
    sessions = session_manager.list_sessions()
    if not sessions:
        print("No sessions found.")
        return
    
    print("Available Sessions:")
    print("=" * 60)
    for session in sessions:
        attached = "(attached)" if session.attached else ""
        print(f"  {session.id}: {session.name} "
              f"[{len(session.panes)} pane(s)] {attached}")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  tmux_like.py new [session_name]")
        print("  tmux_like.py attach <session_id>")
        print("  tmux_like.py list")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "new":
        session_name = sys.argv[2] if len(sys.argv) > 2 else "default"
        tmux = TerminalMultiplexer()
        tmux.start(session_name)
    
    elif command == "attach":
        if len(sys.argv) < 3:
            print("Error: session_id required")
            sys.exit(1)
        session_id = sys.argv[2]
        tmux = TerminalMultiplexer()
        tmux.attach(session_id)
    
    elif command == "list":
        list_sessions()
    
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
