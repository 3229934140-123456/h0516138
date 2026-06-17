import os
import time
import uuid
from typing import Optional
from dataclasses import dataclass, field

from pty_manager import PTYManager
from screen_buffer import ScreenBuffer


@dataclass
class Pane:
    id: str
    session_id: str
    x: int = 0
    y: int = 0
    width: int = 80
    height: int = 24
    pty_fd: Optional[int] = None
    buffer: ScreenBuffer = None
    focus: bool = False
    title: str = ""

    def __post_init__(self):
        if self.buffer is None:
            self.buffer = ScreenBuffer(self.width, self.height)

    def resize(self, width: int, height: int) -> None:
        if width == self.width and height == self.height:
            return
        self.width = width
        self.height = height
        self.buffer.resize(width, height)

    def write(self, data: bytes) -> None:
        self.buffer.feed(data)

    def get_render_lines(self) -> list[str]:
        return self.buffer.get_visible_lines()

    def get_cursor(self) -> tuple[int, int]:
        return self.buffer.cursor_x, self.buffer.cursor_y


@dataclass
class Session:
    id: str
    name: str
    attached: bool = False
    attached_client_id: Optional[str] = None
    panes: dict[str, Pane] = field(default_factory=dict)
    layout: dict = field(default_factory=dict)
    focused_pane_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    def add_pane(self, pane: Pane) -> None:
        self.panes[pane.id] = pane
        if self.focused_pane_id is None:
            self.focused_pane_id = pane.id
            pane.focus = True

    def remove_pane(self, pane_id: str) -> None:
        if pane_id in self.panes:
            del self.panes[pane_id]
            if self.focused_pane_id == pane_id:
                remaining = list(self.panes.keys())
                if remaining:
                    self.focus_pane(remaining[0])

    def focus_pane(self, pane_id: str) -> None:
        for pane in self.panes.values():
            pane.focus = False
        if pane_id in self.panes:
            self.panes[pane_id].focus = True
            self.focused_pane_id = pane_id

    def get_focused_pane(self) -> Optional[Pane]:
        if self.focused_pane_id:
            return self.panes.get(self.focused_pane_id)
        return None

    def list_panes(self) -> list[Pane]:
        return list(self.panes.values())

    def touch(self) -> None:
        self.last_activity = time.time()


class SessionManager:
    def __init__(self, pty_manager: PTYManager):
        self.pty_manager = pty_manager
        self.sessions: dict[str, Session] = {}
        self.clients: dict[str, str] = {}

    def create_session(self, name: str, cols: int = 80, rows: int = 24) -> Session:
        session_id = str(uuid.uuid4())[:8]
        session = Session(id=session_id, name=name)
        
        pane_id = str(uuid.uuid4())[:8]
        pty_fd = self.pty_manager.create_pty(cols, rows)
        
        pane = Pane(
            id=pane_id,
            session_id=session_id,
            x=0,
            y=0,
            width=cols,
            height=rows,
            pty_fd=pty_fd
        )
        
        session.add_pane(pane)
        session.layout = {
            "type": "single",
            "panes": [pane_id],
            "geometry": {"x": 0, "y": 0, "width": cols, "height": rows}
        }
        
        self.sessions[session_id] = session
        return session

    def attach_session(self, session_id: str, client_id: str) -> Optional[Session]:
        if session_id not in self.sessions:
            return None
        
        session = self.sessions[session_id]
        if session.attached and session.attached_client_id:
            self.detach_session(session.attached_client_id)
        
        session.attached = True
        session.attached_client_id = client_id
        self.clients[client_id] = session_id
        session.touch()
        return session

    def detach_session(self, client_id: str) -> Optional[Session]:
        if client_id not in self.clients:
            return None
        
        session_id = self.clients[client_id]
        if session_id not in self.sessions:
            return None
        
        session = self.sessions[session_id]
        session.attached = False
        session.attached_client_id = None
        del self.clients[client_id]
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        return self.sessions.get(session_id)

    def get_client_session(self, client_id: str) -> Optional[Session]:
        session_id = self.clients.get(client_id)
        if session_id:
            return self.sessions.get(session_id)
        return None

    def kill_session(self, session_id: str) -> None:
        if session_id not in self.sessions:
            return
        
        session = self.sessions[session_id]
        for pane in session.panes.values():
            if pane.pty_fd is not None:
                self.pty_manager.destroy_pty(pane.pty_fd)
        
        if session.attached_client_id and session.attached_client_id in self.clients:
            del self.clients[session.attached_client_id]
        
        del self.sessions[session_id]

    def list_sessions(self) -> list[Session]:
        return sorted(self.sessions.values(), key=lambda s: s.created_at)

    def split_pane(self, session_id: str, direction: str = "horizontal", 
                    ratio: float = 0.5) -> Optional[Pane]:
        session = self.sessions.get(session_id)
        if not session or not session.focused_pane_id:
            return None
        
        current_pane = session.panes[session.focused_pane_id]
        pane_id = str(uuid.uuid4())[:8]
        
        if direction == "horizontal":
            new_height = int(current_pane.height * ratio)
            remaining_height = current_pane.height - new_height
            
            pty_fd = self.pty_manager.create_pty(current_pane.width, new_height)
            new_pane = Pane(
                id=pane_id,
                session_id=session_id,
                x=current_pane.x,
                y=current_pane.y,
                width=current_pane.width,
                height=new_height,
                pty_fd=pty_fd
            )
            
            current_pane.y += new_height
            current_pane.height = remaining_height
            self.pty_manager.resize_pty(current_pane.pty_fd, 
                                        current_pane.width, remaining_height)
            
        else:
            new_width = int(current_pane.width * ratio)
            remaining_width = current_pane.width - new_width
            
            pty_fd = self.pty_manager.create_pty(new_width, current_pane.height)
            new_pane = Pane(
                id=pane_id,
                session_id=session_id,
                x=current_pane.x,
                y=current_pane.y,
                width=new_width,
                height=current_pane.height,
                pty_fd=pty_fd
            )
            
            current_pane.x += new_width
            current_pane.width = remaining_width
            self.pty_manager.resize_pty(current_pane.pty_fd, 
                                        remaining_width, current_pane.height)
        
        session.add_pane(new_pane)
        session.focus_pane(pane_id)
        self._update_layout(session)
        
        return new_pane

    def _update_layout(self, session: Session) -> None:
        panes = session.list_panes()
        session.layout = {
            "type": "tiled",
            "panes": [p.id for p in panes],
            "geometry": {
                p.id: {"x": p.x, "y": p.y, "width": p.width, "height": p.height}
                for p in panes
            }
        }

    def write_to_focused_pane(self, client_id: str, data: bytes) -> None:
        session = self.get_client_session(client_id)
        if not session:
            return
        
        pane = session.get_focused_pane()
        if pane and pane.pty_fd is not None:
            self.pty_manager.write_to_pty(pane.pty_fd, data)
            session.touch()

    def process_pty_output(self) -> None:
        output = self.pty_manager.poll(timeout=0.01)
        
        fd_to_pane = {}
        for session in self.sessions.values():
            for pane in session.panes.values():
                if pane.pty_fd is not None:
                    fd_to_pane[pane.pty_fd] = pane
        
        for fd, data in output.items():
            pane = fd_to_pane.get(fd)
            if pane:
                pane.write(data)
                if pane.session_id and pane.session_id in self.sessions:
                    self.sessions[pane.session_id].touch()
