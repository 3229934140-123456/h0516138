import os
import pty
import tty
import termios
import struct
import fcntl
import select
import subprocess
from typing import Callable, Optional


class PTY:
    def __init__(self, cols: int, rows: int, shell: str = "/bin/bash"):
        self.cols = cols
        self.rows = rows
        self.shell = shell
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self.on_output: Optional[Callable[[bytes], None]] = None

    def spawn(self) -> int:
        self.pid, self.master_fd = pty.fork()
        
        if self.pid == 0:
            self._setup_slave()
            os.execvp(self.shell, [self.shell])
        else:
            self._setup_master()
            self.set_size(self.cols, self.rows)
            return self.master_fd

    def _setup_slave(self) -> None:
        for fd in [0, 1, 2]:
            os.dup2(self.slave_fd, fd)
        
        if os.isatty(0):
            old = termios.tcgetattr(0)
            old[3] &= ~(termios.ECHO | termios.ICANON)
            termios.tcsetattr(0, termios.TCSANOW, old)

    def _setup_master(self) -> None:
        flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL, 0)
        fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def set_size(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        if self.master_fd is None:
            return
        
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        
        if self.pid:
            try:
                os.kill(self.pid, 28)
            except OSError:
                pass

    def write(self, data: bytes) -> None:
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    def read(self, size: int = 4096) -> bytes:
        if self.master_fd is None:
            return b""
        try:
            return os.read(self.master_fd, size)
        except OSError:
            return b""

    def close(self) -> None:
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        
        if self.pid:
            try:
                os.waitpid(self.pid, 0)
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
        if master_fd in self.ptys:
            self.ptys[master_fd].close()
            del self.ptys[master_fd]

    def write_to_pty(self, master_fd: int, data: bytes) -> None:
        if master_fd in self.ptys:
            self.ptys[master_fd].write(data)

    def resize_pty(self, master_fd: int, cols: int, rows: int) -> None:
        if master_fd in self.ptys:
            self.ptys[master_fd].set_size(cols, rows)

    def poll(self, timeout: float = 0.1) -> dict[int, bytes]:
        if not self.ptys:
            return {}
        
        read_fds = list(self.ptys.keys())
        readable, _, _ = select.select(read_fds, [], [], timeout)
        
        result = {}
        for fd in readable:
            data = self.ptys[fd].read()
            if data:
                result[fd] = data
            else:
                self.destroy_pty(fd)
        
        return result

    def close_all(self) -> None:
        for fd in list(self.ptys.keys()):
            self.destroy_pty(fd)
