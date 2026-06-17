import os
import pty
import tty
import termios
import struct
import fcntl
import select
import signal
import subprocess
from typing import Callable, Optional, Union


class PTY:
    def __init__(self, cols: int, rows: int, shell: str = "/bin/bash"):
        self.cols = cols
        self.rows = rows
        self.shell = shell
        self.master_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self.on_output: Optional[Callable[[bytes], None]] = None
        self._closed = False

    def spawn(self) -> int:
        self.pid, self.master_fd = pty.fork()

        if self.pid == 0:
            self._setup_slave()
            try:
                os.execvp(self.shell, [self.shell])
            except OSError:
                os._exit(1)
        else:
            self._setup_master()
            self.set_size(self.cols, self.rows)
            return self.master_fd

    def _setup_slave(self) -> None:
        if os.isatty(0):
            try:
                old = termios.tcgetattr(0)
                old[3] &= ~(termios.ECHO | termios.ICANON)
                termios.tcsetattr(0, termios.TCSANOW, old)
            except termios.error:
                pass

    def _setup_master(self) -> None:
        try:
            flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL, 0)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except OSError:
            pass

    def set_size(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        if self.master_fd is None or self._closed:
            return

        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

        if self.pid:
            try:
                os.kill(self.pid, signal.SIGWINCH)
            except OSError:
                pass

    def write(self, data: Union[str, bytes]) -> None:
        if self.master_fd is None or self._closed:
            return

        if isinstance(data, str):
            data = data.encode("utf-8")

        try:
            os.write(self.master_fd, data)
        except (OSError, BrokenPipeError):
            pass

    def read(self, size: int = 4096) -> bytes:
        if self.master_fd is None or self._closed:
            return b""
        try:
            return os.read(self.master_fd, size)
        except (OSError, BlockingIOError):
            return b""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

        if self.pid:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except OSError:
                pass
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass


class PTYManager:
    def __init__(self):
        self.ptys: dict[int, PTY] = {}
        self.running = False

    def create_pty(self, cols: int, rows: int, shell: str = "/bin/bash") -> int:
        pty_obj = PTY(cols, rows, shell)
        master_fd = pty_obj.spawn()
        self.ptys[master_fd] = pty_obj
        return master_fd

    def destroy_pty(self, master_fd: int) -> None:
        try:
            if master_fd in self.ptys:
                self.ptys[master_fd].close()
                del self.ptys[master_fd]
        except Exception:
            if master_fd in self.ptys:
                try:
                    del self.ptys[master_fd]
                except Exception:
                    pass

    def write_to_pty(self, master_fd: int, data: Union[str, bytes]) -> None:
        if master_fd in self.ptys:
            self.ptys[master_fd].write(data)

    def resize_pty(self, master_fd: int, cols: int, rows: int) -> None:
        if master_fd in self.ptys:
            self.ptys[master_fd].set_size(cols, rows)

    def poll(self, timeout: float = 0.1) -> dict[int, bytes]:
        if not self.ptys:
            return {}

        result: dict[int, bytes] = {}
        read_fds = list(self.ptys.keys())

        try:
            readable, _, _ = select.select(read_fds, [], [], timeout)
        except (OSError, ValueError):
            return result

        for fd in readable:
            if fd not in self.ptys:
                continue
            try:
                data = self.ptys[fd].read()
                if data:
                    result[fd] = data
                else:
                    self.destroy_pty(fd)
            except Exception:
                try:
                    self.destroy_pty(fd)
                except Exception:
                    pass

        return result

    def close_all(self) -> None:
        for fd in list(self.ptys.keys()):
            self.destroy_pty(fd)
