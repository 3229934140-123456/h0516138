import os
import sys
import io
import time
import uuid
import json
import socket
import struct
import fcntl
import termios
import signal
import threading
import traceback
import select
import shlex
from dataclasses import dataclass, field
from typing import Optional, Callable, Union, List, Tuple

from pty_manager import PTYManager
from screen_buffer import ScreenBuffer, Cell, CellAttr
from input_router import KeyboardInputReader


DEFAULT_TERM = os.environ.get("TERM", "xterm-256color")
DEFAULT_SHELL = os.environ.get("SHELL", "/bin/bash")
SOCKET_PATH = os.path.expanduser("~/.tmux_impl_socket")
SERVER_PID_PATH = os.path.expanduser("~/.tmux_impl_server.pid")


MIN_PANE_W = 5
MIN_PANE_H = 3
STATUS_BAR_H = 1
STATUS_BAR_STYLE = "\x1b[48;5;234m\x1b[38;5;250m"
STATUS_BAR_FOCUS = "\x1b[48;5;33m\x1b[38;5;231m"
STATUS_BAR_ACCENT = "\x1b[48;5;240m\x1b[38;5;255m"
RESET_STYLE = "\x1b[0m"


def send_msg(sock: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode('utf-8')
    length = struct.pack('>I', len(data))
    try:
        sock.sendall(length + data)
    except Exception:
        pass


def recv_msg(sock: socket.socket, timeout: Optional[float] = None) -> Optional[dict]:
    try:
        if timeout is not None:
            sock.settimeout(timeout)
        length_data = b''
        while len(length_data) < 4:
            chunk = sock.recv(4 - len(length_data))
            if not chunk:
                return None
            length_data += chunk
        length = struct.unpack('>I', length_data)[0]

        data = b''
        while len(data) < length:
            chunk = sock.recv(min(8192, length - len(data)))
            if not chunk:
                return None
            data += chunk
        return json.loads(data.decode('utf-8'))
    except socket.timeout:
        return None
    except Exception:
        return None


@dataclass
class LayoutNode:
    is_leaf: bool
    pane_id: Optional[str] = None
    direction: Optional[str] = None
    children: List['LayoutNode'] = field(default_factory=list)
    ratios: List[float] = field(default_factory=list)
    parent: Optional['LayoutNode'] = field(default=None, repr=False)

    def collect_leaf_ids(self) -> List[str]:
        if self.is_leaf:
            return [self.pane_id] if self.pane_id else []
        result = []
        for c in self.children:
            result.extend(c.collect_leaf_ids())
        return result


@dataclass
class PaneData:
    id: str
    session_id: str
    pty_fd: Optional[int]
    buffer: ScreenBuffer
    x: int = 0
    y: int = 0
    width: int = 80
    height: int = 24
    focus: bool = False

    def write(self, data: bytes) -> None:
        self.buffer.feed(data)

    def resize(self, w: int, h: int) -> None:
        self.width = w
        self.height = h
        self.buffer.resize(w, h)

    def get_cursor(self):
        return self.buffer.get_cursor()

    def get_render(self, off_x=0, off_y=0) -> bytes:
        return self.buffer.get_render_sequence(off_x, off_y)


@dataclass
class SessionData:
    id: str
    name: str
    root: LayoutNode
    attached: bool = False
    attached_client: Optional[str] = None
    panes: dict = field(default_factory=dict)
    focused_pane_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    screen_cols: int = 80
    screen_rows: int = 24

    def pane_area_size(self) -> Tuple[int, int]:
        h = max(1, self.screen_rows - STATUS_BAR_H)
        w = max(MIN_PANE_W, self.screen_cols)
        return w, h

    def list_pane_ids(self) -> list:
        return list(self.panes.keys())

    def add_pane(self, pane: PaneData) -> None:
        self.panes[pane.id] = pane
        if self.focused_pane_id is None:
            self.focus_pane(pane.id)

    def remove_pane(self, pid: str) -> None:
        if pid in self.panes:
            del self.panes[pid]

    def focus_pane(self, pid: str) -> None:
        for p in self.panes.values():
            p.focus = False
        if pid in self.panes:
            self.panes[pid].focus = True
            self.focused_pane_id = pid

    def focused(self) -> Optional[PaneData]:
        if self.focused_pane_id:
            return self.panes.get(self.focused_pane_id)
        return None

    def touch(self) -> None:
        self.last_activity = time.time()


def _find_leaf(root: LayoutNode, pane_id: str) -> Optional[LayoutNode]:
    if root.is_leaf:
        return root if root.pane_id == pane_id else None
    for c in root.children:
        r = _find_leaf(c, pane_id)
        if r:
            return r
    return None


def _apply_layout(node: LayoutNode, x: int, y: int, w: int, h: int,
                  panes: dict, pty_set_sizes: dict) -> None:
    if node.is_leaf:
        pid = node.pane_id
        if pid and pid in panes:
            p = panes[pid]
            new_w = max(MIN_PANE_W, w)
            new_h = max(MIN_PANE_H, h)
            if p.x != x or p.y != y or p.width != new_w or p.height != new_h:
                p.x, p.y, p.width, p.height = x, y, new_w, new_h
                p.buffer.resize(new_w, new_h)
                if p.pty_fd is not None:
                    pty_set_sizes[p.pty_fd] = (new_w, new_h)
            else:
                p.x, p.y, p.width, p.height = x, y, new_w, new_h
        return

    direction = node.direction
    children = node.children
    ratios = node.ratios
    n = len(children)
    if n == 0:
        return

    if n == 1:
        _apply_layout(children[0], x, y, w, h, panes, pty_set_sizes)
        return

    if direction == 'vertical':
        avail = max(MIN_PANE_W * n, w)
        sizes = [max(MIN_PANE_W, int(avail * r)) for r in ratios]
        diff = avail - sum(sizes)
        if diff != 0 and sizes:
            sizes[-1] += diff
        cur_x = x
        for i, ch in enumerate(children):
            cw = sizes[i]
            _apply_layout(ch, cur_x, y, cw, h, panes, pty_set_sizes)
            cur_x += cw
    else:
        avail = max(MIN_PANE_H * n, h)
        sizes = [max(MIN_PANE_H, int(avail * r)) for r in ratios]
        diff = avail - sum(sizes)
        if diff != 0 and sizes:
            sizes[-1] += diff
        cur_y = y
        for i, ch in enumerate(children):
            ch_h = sizes[i]
            _apply_layout(ch, x, cur_y, w, ch_h, panes, pty_set_sizes)
            cur_y += ch_h


def _split_leaf(root: LayoutNode, target_id: str, new_id: str,
                direction: str, ratio: float) -> Optional[LayoutNode]:
    leaf = _find_leaf(root, target_id)
    if leaf is None:
        return None

    parent = leaf.parent
    new_leaf_a = LayoutNode(is_leaf=True, pane_id=leaf.pane_id, parent=None)
    new_leaf_b = LayoutNode(is_leaf=True, pane_id=new_id, parent=None)
    r1, r2 = ratio, 1.0 - ratio

    container = LayoutNode(
        is_leaf=False,
        direction=direction,
        children=[new_leaf_a, new_leaf_b],
        ratios=[r1, r2],
        parent=parent,
    )
    new_leaf_a.parent = container
    new_leaf_b.parent = container

    if parent is None:
        return container

    idx = parent.children.index(leaf)
    parent.children[idx] = container
    _renormalize_ratios(parent)
    return None


def _renormalize_ratios(node: LayoutNode) -> None:
    if not node.children:
        return
    total = sum(node.ratios)
    if total <= 0:
        n = len(node.children)
        node.ratios = [1.0 / n for _ in range(n)]
    else:
        node.ratios = [r / total for r in node.ratios]


def _remove_leaf(root: LayoutNode, target_id: str) -> Tuple[Optional[LayoutNode], Optional[str]]:
    leaf = _find_leaf(root, target_id)
    if leaf is None:
        return root, None

    parent = leaf.parent
    if parent is None:
        return None, None

    neighbor_id = None
    try:
        idx = parent.children.index(leaf)
        other_idx = 1 - idx if len(parent.children) == 2 else (idx + 1) % len(parent.children)
        if 0 <= other_idx < len(parent.children):
            neighbor = parent.children[other_idx]
            if neighbor.is_leaf:
                neighbor_id = neighbor.pane_id
            else:
                leaves = neighbor.collect_leaf_ids()
                if leaves:
                    neighbor_id = leaves[0]
    except Exception:
        pass

    del parent.children[idx]
    del parent.ratios[idx]

    _renormalize_ratios(parent)

    if len(parent.children) == 1:
        only_child = parent.children[0]
        grandparent = parent.parent
        only_child.parent = grandparent
        if grandparent is None:
            return only_child, neighbor_id
        gidx = grandparent.children.index(parent)
        grandparent.children[gidx] = only_child
        _renormalize_ratios(grandparent)
        return root, neighbor_id

    return root, neighbor_id


class ServerState:
    def __init__(self):
        self.pty_mgr = PTYManager()
        self.sessions: dict[str, SessionData] = {}
        self._fd_to_pane: dict[int, PaneData] = {}
        self.lock = threading.RLock()

    def _find_session_by_name(self, name: str) -> Optional[SessionData]:
        hits = [s for s in self.sessions.values() if s.name == name]
        return hits[0] if hits else None

    def _unique_name(self, base: str) -> str:
        if not self._find_session_by_name(base):
            return base
        i = 1
        while True:
            cand = f"{base}-{i}"
            if not self._find_session_by_name(cand):
                return cand
            i += 1

    def create_session(self, name: str, cols: int, rows: int,
                       allow_rename: bool = True) -> Tuple[Optional[SessionData], str]:
        with self.lock:
            existing = self._find_session_by_name(name)
            if existing:
                if allow_rename:
                    name = self._unique_name(name)
                    msg = f"Session renamed to '{name}' to avoid conflict"
                else:
                    return None, f"Session with name '{name}' already exists"
            else:
                msg = "ok"

            sid = str(uuid.uuid4())[:8]
            pane_id = str(uuid.uuid4())[:8]
            root = LayoutNode(is_leaf=True, pane_id=pane_id, parent=None)
            sess = SessionData(id=sid, name=name, root=root)
            sess.screen_cols = cols
            sess.screen_rows = rows

            area_w, area_h = sess.pane_area_size()
            fd = self.pty_mgr.create_pty(area_w, area_h, DEFAULT_SHELL)
            buf = ScreenBuffer(area_w, area_h)
            pane = PaneData(id=pane_id, session_id=sid, pty_fd=fd,
                            buffer=buf, x=0, y=0, width=area_w, height=area_h)
            sess.add_pane(pane)
            self._fd_to_pane[fd] = pane
            self.sessions[sid] = sess
            return sess, msg

    def rename_session(self, sid_or_name: str, new_name: str) -> Tuple[bool, str]:
        with self.lock:
            s = self.sessions.get(sid_or_name) or self._find_session_by_name(sid_or_name)
            if not s:
                return False, f"Session '{sid_or_name}' not found"

            existing = self._find_session_by_name(new_name)
            if existing and existing.id != s.id:
                return False, f"Session name '{new_name}' already in use"

            s.name = new_name
            s.touch()
            return True, f"Renamed to '{new_name}'"

    def kill_session(self, sid_or_name: str) -> Tuple[bool, str, Optional[str]]:
        with self.lock:
            s = self.sessions.get(sid_or_name) or self._find_session_by_name(sid_or_name)
            if not s:
                return False, f"Session '{sid_or_name}' not found", None
            sid = s.id
            for p in list(s.panes.values()):
                if p.pty_fd is not None:
                    try:
                        del self._fd_to_pane[p.pty_fd]
                    except Exception:
                        pass
                    self.pty_mgr.destroy_pty(p.pty_fd)
            del self.sessions[sid]
            return True, f"Session '{sid_or_name}' killed", sid

    def attach_by_name_or_id(self, target: str, client_id: str) -> Tuple[Optional[SessionData], str]:
        with self.lock:
            s = self.sessions.get(target) or self._find_session_by_name(target)
            if not s:
                return None, f"Session '{target}' not found"
            if s.attached and s.attached_client:
                s.attached = False
                s.attached_client = None
            s.attached = True
            s.attached_client = client_id
            s.touch()
            return s, f"Attached to '{s.name}'"

    def detach(self, sid: str) -> Optional[SessionData]:
        with self.lock:
            if sid not in self.sessions:
                return None
            s = self.sessions[sid]
            s.attached = False
            s.attached_client = None
            return s

    def list_sessions(self) -> list:
        with self.lock:
            return [
                {"id": s.id, "name": s.name, "attached": s.attached,
                 "num_panes": len(s.panes),
                 "created_at": s.created_at, "last_activity": s.last_activity}
                for s in self.sessions.values()
            ]

    def write_focused(self, sid: str, data: bytes) -> None:
        with self.lock:
            s = self.sessions.get(sid)
            if not s:
                return
            p = s.focused()
            if p and p.pty_fd is not None:
                self.pty_mgr.write_to_pty(p.pty_fd, data)
                s.touch()

    def _relayout(self, s: SessionData) -> None:
        area_w, area_h = s.pane_area_size()
        pty_sizes = {}
        _apply_layout(s.root, 0, 0, area_w, area_h, s.panes, pty_sizes)
        for fd, (w, h) in pty_sizes.items():
            try:
                self.pty_mgr.resize_pty(fd, w, h)
            except Exception:
                pass

    def split(self, sid: str, direction: str, ratio: float = 0.5) -> Optional[PaneData]:
        with self.lock:
            s = self.sessions.get(sid)
            if not s or not s.focused_pane_id:
                return None
            cur = s.focused()
            if not cur:
                return None

            pane_id = str(uuid.uuid4())[:8]
            new_root = _split_leaf(s.root, cur.id, pane_id, direction, ratio)

            area_w, area_h = s.pane_area_size()
            if direction == 'horizontal':
                h1 = max(MIN_PANE_H, int(area_h * ratio))
                h2 = max(MIN_PANE_H, area_h - h1)
                total = h1 + h2
                if total != area_h:
                    h1 = area_h - h2
                fd = self.pty_mgr.create_pty(area_w, h1, DEFAULT_SHELL)
                buf = ScreenBuffer(area_w, h1)
                new_pane = PaneData(id=pane_id, session_id=sid, pty_fd=fd,
                                    buffer=buf, x=cur.x, y=cur.y,
                                    width=area_w, height=h1)
            else:
                w1 = max(MIN_PANE_W, int(area_w * ratio))
                w2 = max(MIN_PANE_W, area_w - w1)
                total = w1 + w2
                if total != area_w:
                    w1 = area_w - w2
                fd = self.pty_mgr.create_pty(w1, area_h, DEFAULT_SHELL)
                buf = ScreenBuffer(w1, area_h)
                new_pane = PaneData(id=pane_id, session_id=sid, pty_fd=fd,
                                    buffer=buf, x=cur.x, y=cur.y,
                                    width=w1, height=area_h)

            s.add_pane(new_pane)
            self._fd_to_pane[fd] = new_pane

            if new_root is not None:
                s.root = new_root

            self._relayout(s)

            s.focus_pane(pane_id)
            s.touch()
            return new_pane

    def close_focused(self, sid: str) -> Tuple[bool, str]:
        with self.lock:
            s = self.sessions.get(sid)
            if not s:
                return False, "Session not found"
            cur = s.focused()
            if not cur:
                return False, "No focused pane"
            pid = cur.id
            cur_fd = cur.pty_fd

            if len(s.panes) == 1:
                if cur_fd is not None:
                    try:
                        del self._fd_to_pane[cur_fd]
                    except Exception:
                        pass
                    self.pty_mgr.destroy_pty(cur_fd)
                s.remove_pane(pid)
                self.kill_session(sid)
                return True, "session_closed"

            cx, cy = cur.x + cur.width // 2, cur.y + cur.height // 2
            remaining_before = [p for p in s.panes.values() if p.id != pid]

            candidates_direct = []
            for p in remaining_before:
                for test_pt in [(p.x + p.width // 2, p.y - 1),
                                 (p.x + p.width // 2, p.y + p.height),
                                 (p.x - 1, p.y + p.height // 2),
                                 (p.x + p.width, p.y + p.height // 2)]:
                    tx, ty = test_pt
                    if (cur.x <= tx < cur.x + cur.width and
                            cur.y <= ty < cur.y + cur.height):
                        candidates_direct.append(p)
                        break

            if candidates_direct:
                def dist(p):
                    px, py = p.x + p.width // 2, p.y + p.height // 2
                    return (px - cx) ** 2 + (py - cy) ** 2
                candidates_direct.sort(key=dist)
                neighbor_fallback = candidates_direct[0].id
            else:
                sorted_p = sorted(remaining_before,
                                  key=lambda p: (p.y, p.x))
                neighbor_fallback = sorted_p[0].id if sorted_p else None

            new_root, tree_neighbor_id = _remove_leaf(s.root, pid)
            if new_root is not None:
                s.root = new_root

            focus_target = tree_neighbor_id or neighbor_fallback
            if focus_target is None and s.list_pane_ids():
                focus_target = s.list_pane_ids()[0]

            if cur_fd is not None:
                try:
                    del self._fd_to_pane[cur_fd]
                except Exception:
                    pass
                self.pty_mgr.destroy_pty(cur_fd)
            s.remove_pane(pid)

            self._relayout(s)

            if focus_target and focus_target in s.panes:
                s.focus_pane(focus_target)
            elif s.list_pane_ids():
                s.focus_pane(s.list_pane_ids()[0])

            s.touch()
            return False, "pane_closed"

    def focus_neighbor(self, sid: str, direction: str) -> bool:
        with self.lock:
            s = self.sessions.get(sid)
            if not s or len(s.panes) <= 1:
                return False
            cur = s.focused()
            if not cur:
                return False

            cx = cur.x + cur.width // 2
            cy = cur.y + cur.height // 2
            tx, ty = cx, cy
            if direction == 'up':
                ty = cur.y - 1
            elif direction == 'down':
                ty = cur.y + cur.height
            elif direction == 'left':
                tx = cur.x - 1
            elif direction == 'right':
                tx = cur.x + cur.width

            found = None
            for p in s.panes.values():
                if p.id == cur.id:
                    continue
                if (p.x <= tx < p.x + p.width and
                        p.y <= ty < p.y + p.height):
                    found = p
                    break

            if found is None:
                def edge_dist(p):
                    px, py = p.x + p.width // 2, p.y + p.height // 2
                    return (px - cx) ** 2 + (py - cy) ** 2
                panes_sorted = sorted(
                    [p for p in s.panes.values() if p.id != cur.id],
                    key=edge_dist
                )
                if panes_sorted:
                    found = panes_sorted[0]

            if found:
                s.focus_pane(found.id)
                s.touch()
                return True
            return False

    def focus_cycle(self, sid: str, delta: int = 1) -> bool:
        with self.lock:
            s = self.sessions.get(sid)
            if not s or len(s.panes) <= 1:
                return False
            leaves = s.root.collect_leaf_ids()
            if not leaves:
                return False
            cur_id = s.focused_pane_id
            try:
                idx = leaves.index(cur_id) if cur_id in leaves else -1
            except ValueError:
                idx = -1
            new_idx = (idx + delta) % len(leaves)
            s.focus_pane(leaves[new_idx])
            s.touch()
            return True

    def resize_session(self, sid: str, cols: int, rows: int) -> None:
        with self.lock:
            s = self.sessions.get(sid)
            if not s:
                return
            s.screen_cols = max(MIN_PANE_W, cols)
            s.screen_rows = max(MIN_PANE_H + STATUS_BAR_H, rows)
            self._relayout(s)
            s.touch()

    def poll_pty(self) -> None:
        with self.lock:
            output = self.pty_mgr.poll(0.005)
            for fd, data in output.items():
                pane = self._fd_to_pane.get(fd)
                if pane and data:
                    try:
                        pane.write(data)
                        sess = self.sessions.get(pane.session_id)
                        if sess:
                            sess.touch()
                    except Exception:
                        pass

    def get_session_snapshot(self, sid: str) -> Optional[dict]:
        with self.lock:
            s = self.sessions.get(sid)
            if not s:
                return None
            panes_info = []
            for p in s.panes.values():
                cursor = p.get_cursor()
                panes_info.append({
                    "id": p.id,
                    "x": p.x, "y": p.y,
                    "width": p.width, "height": p.height,
                    "focus": p.focus,
                    "cursor_x": cursor[0],
                    "cursor_y": cursor[1],
                    "cursor_vis": cursor[2],
                    "render": p.buffer.get_render_sequence(p.x, p.y).decode('latin-1', errors='replace')
                })
            return {
                "session_id": s.id,
                "name": s.name,
                "panes": panes_info,
                "focused_id": s.focused_pane_id,
                "screen_cols": s.screen_cols,
                "screen_rows": s.screen_rows,
                "status_bar_h": STATUS_BAR_H,
                "num_panes": len(s.panes),
            }


SERVER: Optional[ServerState] = None


def server_loop():
    global SERVER
    SERVER = ServerState()

    if os.path.exists(SOCKET_PATH):
        try:
            os.unlink(SOCKET_PATH)
        except Exception:
            pass

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.bind(SOCKET_PATH)
    server_sock.listen(16)
    os.chmod(SOCKET_PATH, 0o600)

    with open(SERVER_PID_PATH, 'w') as f:
        f.write(str(os.getpid()))

    clients = {}

    def _resp(conn: socket.socket, **kw) -> None:
        try:
            send_msg(conn, dict(kw))
        except Exception:
            pass

    def handle_client(conn: socket.socket, addr):
        try:
            while True:
                msg = recv_msg(conn, 0.1)
                if msg is None:
                    SERVER.poll_pty()
                    for c_sid, c in list(clients.items()):
                        if c is conn:
                            sess = SERVER.sessions.get(c_sid)
                            if sess and sess.attached:
                                snap = SERVER.get_session_snapshot(c_sid)
                                if snap:
                                    try:
                                        send_msg(c, {"type": "render", "data": snap})
                                    except Exception:
                                        pass
                    continue

                cmd = msg.get("cmd")
                client_id = msg.get("client_id", "")

                if cmd == "new":
                    cols = msg.get("cols", 80)
                    rows = msg.get("rows", 24)
                    name = msg.get("name", "default")
                    background = msg.get("background", False)
                    sess, info = SERVER.create_session(name, cols, rows, allow_rename=True)
                    if sess:
                        if not background:
                            clients[sess.id] = conn
                            SERVER.attach_by_name_or_id(sess.id, client_id)
                        _resp(conn, type="ok", session_id=sess.id,
                              session_name=sess.name, info=info)
                    else:
                        _resp(conn, type="error", msg=info)

                elif cmd == "attach":
                    target = msg.get("session_id", "")
                    sess, info = SERVER.attach_by_name_or_id(target, client_id)
                    if sess:
                        clients[sess.id] = conn
                        cols = msg.get("cols", 80)
                        rows = msg.get("rows", 24)
                        SERVER.resize_session(sess.id, cols, rows)
                        _resp(conn, type="ok", session_id=sess.id,
                              session_name=sess.name, info=info)
                    else:
                        _resp(conn, type="error", msg=info)

                elif cmd == "detach":
                    sid = msg.get("session_id", "")
                    SERVER.detach(sid)
                    if sid in clients:
                        del clients[sid]
                    _resp(conn, type="ok")

                elif cmd == "list":
                    _resp(conn, type="ok", sessions=SERVER.list_sessions())

                elif cmd == "rename":
                    sid = msg.get("session_id", "")
                    new_name = msg.get("name", "")
                    ok, info = SERVER.rename_session(sid, new_name)
                    _resp(conn, type="ok" if ok else "error", msg=info)

                elif cmd == "kill":
                    target = msg.get("target", "")
                    ok, info, killed_sid = SERVER.kill_session(target)
                    if ok:
                        for c_sid, c in list(clients.items()):
                            if c_sid not in SERVER.sessions and c is conn:
                                del clients[c_sid]
                    _resp(conn, type="ok" if ok else "error", msg=info,
                          killed_sid=killed_sid)

                elif cmd == "input":
                    sid = msg.get("session_id", "")
                    data = bytes(msg.get("data", []))
                    SERVER.write_focused(sid, data)

                elif cmd == "split":
                    sid = msg.get("session_id", "")
                    direction = msg.get("direction", "horizontal")
                    SERVER.split(sid, direction)

                elif cmd == "close":
                    sid = msg.get("session_id", "")
                    ok, info = SERVER.close_focused(sid)
                    if info == "session_closed":
                        if sid not in SERVER.sessions and sid in clients:
                            del clients[sid]
                        _resp(conn, type="session_closed")
                    else:
                        _resp(conn, type="ok", msg=info)

                elif cmd == "focus":
                    sid = msg.get("session_id", "")
                    direction = msg.get("direction", "")
                    SERVER.focus_neighbor(sid, direction)

                elif cmd == "cycle_pane":
                    sid = msg.get("session_id", "")
                    delta = int(msg.get("delta", 1))
                    SERVER.focus_cycle(sid, delta)

                elif cmd == "resize":
                    sid = msg.get("session_id", "")
                    cols = msg.get("cols", 80)
                    rows = msg.get("rows", 24)
                    SERVER.resize_session(sid, cols, rows)

                elif cmd == "render":
                    sid = msg.get("session_id", "")
                    snap = SERVER.get_session_snapshot(sid)
                    if snap:
                        send_msg(conn, {"type": "render", "data": snap})

                elif cmd == "ping":
                    _resp(conn, type="pong")

        except Exception:
            pass
        finally:
            try:
                for sid, c in list(clients.items()):
                    if c is conn:
                        SERVER.detach(sid)
                        del clients[sid]
                conn.close()
            except Exception:
                pass

    try:
        while True:
            SERVER.poll_pty()
            try:
                readable, _, _ = select.select([server_sock], [], [], 0.01)
                if readable:
                    conn, addr = server_sock.accept()
                    t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
                    t.start()
            except Exception:
                pass

            for sid, c in list(clients.items()):
                sess = SERVER.sessions.get(sid)
                if sess and sess.attached:
                    try:
                        snap = SERVER.get_session_snapshot(sid)
                        if snap:
                            send_msg(c, {"type": "render", "data": snap})
                    except Exception:
                        try:
                            SERVER.detach(sid)
                            del clients[sid]
                            c.close()
                        except Exception:
                            pass
    finally:
        try:
            server_sock.close()
        except Exception:
            pass
        try:
            os.unlink(SOCKET_PATH)
        except Exception:
            pass
        try:
            os.unlink(SERVER_PID_PATH)
        except Exception:
            pass


def is_server_running() -> bool:
    if not os.path.exists(SERVER_PID_PATH):
        return False
    try:
        with open(SERVER_PID_PATH) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        try:
            os.unlink(SERVER_PID_PATH)
        except Exception:
            pass
        try:
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
        except Exception:
            pass
        return False


def start_server():
    if is_server_running():
        return
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
        except Exception:
            pass
        try:
            devnull = open(os.devnull, 'r+b', 0)
            os.dup2(devnull.fileno(), 0)
            os.dup2(devnull.fileno(), 1)
            os.dup2(devnull.fileno(), 2)
        except Exception:
            pass
        server_loop()
        os._exit(0)
    else:
        for _ in range(50):
            time.sleep(0.1)
            if os.path.exists(SOCKET_PATH):
                return
        raise RuntimeError("Failed to start server")


def connect_server(retries: int = 20) -> Optional[socket.socket]:
    for i in range(retries):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(SOCKET_PATH)
            return sock
        except Exception:
            time.sleep(0.1)
    return None


def get_terminal_size():
    try:
        data = fcntl.ioctl(
            sys.stdout.fileno(), termios.TIOCGWINSZ,
            struct.pack('HHHH', 0, 0, 0, 0)
        )
        rows, cols, _, _ = struct.unpack('HHHH', data)
        return max(25, rows), max(80, cols)
    except Exception:
        return 25, 80


class ClientApp:
    def __init__(self):
        self.client_id = str(uuid.uuid4())[:12]
        self.sock: Optional[socket.socket] = None
        self.reader = KeyboardInputReader()
        self.session_id: Optional[str] = None
        self.session_name: str = "default"
        self.rows, self.cols = 25, 80
        self.last_render_text = ""
        self.last_status_key = ""
        self.detached = False
        self.running = False
        self.session_closed = False
        self.error_message: Optional[str] = None
        self.error_expire_at: float = 0.0
        self.command_mode = False
        self.command_input = ""
        self.command_cursor = 0

    def show_error(self, msg: str, duration: float = 3.0) -> None:
        self.error_message = msg
        self.error_expire_at = time.time() + duration
        self.last_status_key = ""

    def cmd_new(self, name: str = "default"):
        if not is_server_running():
            start_server()
        self.sock = connect_server()
        if not self.sock:
            print("Cannot connect to server")
            sys.exit(1)
        self.rows, self.cols = get_terminal_size()
        send_msg(self.sock, {
            "cmd": "new", "name": name,
            "cols": self.cols, "rows": self.rows,
            "client_id": self.client_id
        })
        resp = recv_msg(self.sock, 5.0)
        if not resp or resp.get("type") != "ok":
            print(f"Failed to create session: {resp.get('msg') if resp else ''}")
            sys.exit(1)
        self.session_id = resp["session_id"]
        self.session_name = resp.get("session_name", name)
        self._start_ui()

    def cmd_attach(self, sid: str):
        if not is_server_running():
            print("No server running")
            sys.exit(1)
        self.sock = connect_server()
        if not self.sock:
            print("Cannot connect to server")
            sys.exit(1)
        self.rows, self.cols = get_terminal_size()
        send_msg(self.sock, {
            "cmd": "attach", "session_id": sid,
            "cols": self.cols, "rows": self.rows,
            "client_id": self.client_id
        })
        resp = recv_msg(self.sock, 5.0)
        if not resp or resp.get("type") != "ok":
            print(f"Failed: {resp.get('msg') if resp else 'Session not found'}")
            sys.exit(1)
        self.session_id = resp["session_id"]
        self.session_name = resp.get("session_name", sid)
        self._start_ui()

    def cmd_list(self):
        if not is_server_running():
            print("No active sessions")
            return
        self.sock = connect_server()
        if not self.sock:
            print("Cannot connect to server")
            return
        send_msg(self.sock, {"cmd": "list", "client_id": self.client_id})
        resp = recv_msg(self.sock, 3.0)
        if resp and resp.get("type") == "ok":
            sess = resp["sessions"]
            if not sess:
                print("No active sessions")
                return
            print("Active sessions:")
            print("-" * 70)
            for s in sess:
                attached = "(attached)" if s["attached"] else "  (detached)"
                activity = time.ctime(s["last_activity"])
                print(f"  id={s['id']}  name={s['name']!r}  [{s['num_panes']} panes] "
                      f"{attached}  last: {activity}")
        else:
            print("Error listing sessions")

    def _setup_handlers(self):
        r = self.reader
        r.setup_bindings(None, self.client_id)

        def do_detach():
            self._detach()

        def do_split(direction):
            send_msg(self.sock, {
                "cmd": "split", "session_id": self.session_id,
                "direction": direction
            })
            self._request_render()

        def do_focus(direction):
            send_msg(self.sock, {
                "cmd": "focus", "session_id": self.session_id,
                "direction": direction
            })
            self._request_render()

        def do_close():
            send_msg(self.sock, {
                "cmd": "close", "session_id": self.session_id,
            })
            self._request_render()
            time.sleep(0.05)
            send_msg(self.sock, {"cmd": "list", "client_id": self.client_id})
            resp = recv_msg(self.sock, 0.5)
            if resp and resp.get("type") == "ok":
                ids = [s["id"] for s in resp["sessions"]]
                if self.session_id not in ids:
                    self.session_closed = True
                    self.running = False

        def do_cycle():
            send_msg(self.sock, {
                "cmd": "cycle_pane", "session_id": self.session_id, "delta": 1
            })
            self._request_render()

        def do_next():
            send_msg(self.sock, {
                "cmd": "cycle_pane", "session_id": self.session_id, "delta": 1
            })
            self._request_render()

        def do_prev():
            send_msg(self.sock, {
                "cmd": "cycle_pane", "session_id": self.session_id, "delta": -1
            })
            self._request_render()

        def do_help():
            self._show_overlay(
                "Terminal Multiplexer - Help\n"
                "==========================\n"
                "Prefix key: Ctrl+B\n\n"
                "  \"          Split horizontally (上下)\n"
                "  %          Split vertically (左右)\n"
                "  x          Close current pane\n"
                "  d / C-d    Detach from session\n"
                "  o          Cycle panes\n"
                "  n / p      Next / Previous pane\n"
                "  <arrow>    Move focus (directional)\n"
                "  s          List sessions overlay\n"
                "  :          Command mode\n"
                "  ?          This help\n\n"
                "Commands (after Ctrl+B :):\n"
                "  rename <name>          Rename current session\n"
                "  new [name]             Create new session\n"
                "  kill-session [target]  Kill current or target session\n"
                "  attach <id|name>       Switch to another session\n"
                "  list                   List sessions\n"
                "  detach                 Detach\n"
                "  help                   This help\n\n"
                "Press any key to continue..."
            )

        def do_list():
            send_msg(self.sock, {"cmd": "list", "client_id": self.client_id})
            resp = recv_msg(self.sock, 1.0)
            if resp and resp.get("type") == "ok":
                lines = ["Active sessions:", "-" * 60]
                for s in resp["sessions"]:
                    cur = " <-- current" if s["id"] == self.session_id else ""
                    att = "(attached)" if s["attached"] else ""
                    lines.append(f"  id={s['id']}  name={s['name']!r} "
                                 f"[{s['num_panes']} panes] {att}{cur}")
                lines.append("")
                lines.append("Press any key to continue...")
                self._show_overlay("\n".join(lines))

        def do_command():
            self._enter_command_mode()

        r.register_handler('detach', do_detach)
        r.register_handler('split', do_split)
        r.register_handler('focus', do_focus)
        r.register_handler('close_pane', do_close)
        r.register_handler('cycle_pane', do_cycle)
        r.register_handler('next_pane', do_next)
        r.register_handler('prev_pane', do_prev)
        r.register_handler('help', do_help)
        r.register_handler('list_sessions', do_list)
        r.register_handler('command_mode', do_command)

        r.set_input_callback(self._on_input)

    def _on_input(self, data: bytes):
        send_msg(self.sock, {
            "cmd": "input", "session_id": self.session_id,
            "data": list(data)
        })

    def _request_render(self):
        try:
            send_msg(self.sock, {
                "cmd": "render", "session_id": self.session_id
            })
        except Exception:
            pass

    def _detach(self):
        send_msg(self.sock, {
            "cmd": "detach", "session_id": self.session_id
        })
        self.detached = True
        self.running = False

    def _start_ui(self):
        self._setup_handlers()
        self.running = True
        self._handle_resize(None, None)
        signal.signal(signal.SIGWINCH, self._handle_resize)

        sys.stdout.write("\x1b[?1049h")
        sys.stdout.write("\x1b[H\x1b[2J")
        sys.stdout.flush()

        self.reader.start()

        try:
            self._main_loop()
        except Exception as e:
            pass
        finally:
            self._cleanup()

    def _handle_resize(self, signum, frame):
        try:
            self.rows, self.cols = get_terminal_size()
            if self.sock:
                send_msg(self.sock, {
                    "cmd": "resize", "session_id": self.session_id,
                    "cols": self.cols, "rows": self.rows
                })
        except Exception:
            pass

    def _main_loop(self):
        self._last_status_tick = time.time()
        while self.running and not self.detached and not self.session_closed:
            try:
                self.reader.poll()
            except Exception:
                pass

            if self.command_mode:
                self._command_mode_poll()
                continue

            now = time.time()
            if now - self._last_status_tick >= 1.0:
                self._last_status_tick = now
                self.last_status_key = ""
                if self.error_message and now >= self.error_expire_at:
                    self.error_message = None
                    self.error_expire_at = 0.0
                    self.last_render_text = ""
                self._request_render()

            msg = recv_msg(self.sock, 0.01)
            if msg:
                mtype = msg.get("type")
                if mtype == "render":
                    self._apply_render(msg["data"])
                elif mtype == "error":
                    self.show_error(msg.get("msg", "Error"))
                elif mtype == "session_closed":
                    self.session_closed = True
                    break
                elif mtype == "ok" and "msg" in msg:
                    pass

    def _render_status_bar(self, snap: dict) -> str:
        w = snap.get("screen_cols", self.cols)
        time_str = time.strftime("%H:%M:%S")
        sname = snap.get("name", "?")
        sid = snap.get("session_id", "?")
        num = snap.get("num_panes", 0)
        fid = snap.get("focused_id", "?")
        fid_short = fid[:6] if fid else "?"

        left_txt = f" {sname} [{sid}]  panes:{num} "
        focus_txt = f" [focus:{fid_short}] "
        right_txt = f" {time_str} "

        pieces = []
        pieces.append(f"\x1b[{snap['screen_rows']};1H")
        pieces.append(STATUS_BAR_STYLE)

        if self.error_message and time.time() < self.error_expire_at:
            msg = f" [!] {self.error_message} "
        else:
            if self.command_mode:
                prompt = ":" + self.command_input
                cursor_pos = 1 + self.command_cursor
                msg = " " + prompt + " "
            else:
                msg = left_txt

        left_part = msg
        right_part = focus_txt + right_txt
        max_left = max(1, w - len(right_part) - 1)
        left_part = left_part[:max_left]
        pad = max(0, w - len(left_part) - len(right_part))
        line = left_part + (" " * pad) + right_part
        if len(line) > w:
            line = line[:w]

        if self.command_mode:
            prompt_w = 1 + len(self.command_input)
            if prompt_w <= w:
                cx = min(1 + self.command_cursor, w)
                pieces.append(line + f"\x1b[{snap['screen_rows']};{cx}H")
            else:
                pieces.append(line)
        else:
            pieces.append(line)

        pieces.append(RESET_STYLE)
        return "".join(pieces)

    def _apply_render(self, snap: dict):
        try:
            if "name" in snap:
                self.session_name = snap["name"]
            parts = bytearray()
            parts.extend(b'\x1b[?25l')

            for p in snap["panes"]:
                render_bytes = p["render"].encode('latin-1', errors='replace')
                parts.extend(render_bytes)

            focus_pane = None
            for p in snap["panes"]:
                if p["focus"]:
                    focus_pane = p
                    break

            cursor_in_pane = None
            if focus_pane:
                cx = focus_pane['x'] + min(focus_pane['cursor_x'], focus_pane['width'] - 1)
                cy = focus_pane['y'] + min(focus_pane['cursor_y'], focus_pane['height'] - 1)
                cursor_in_pane = (cx, cy)
                error_active = self.error_message and time.time() < self.error_expire_at
                if focus_pane.get("cursor_vis", True) and not self.command_mode and not error_active:
                    parts.extend(f'\x1b[{cy + 1};{cx + 1}H'.encode())
                    parts.extend(b'\x1b[?25h')
            else:
                cursor_in_pane = None

            status_key = (str(snap.get("panes")) + str(time.time())[:2]
                          + str(self.error_message)
                          + ("CMD:" + self.command_input if self.command_mode else ""))
            if status_key != self.last_status_key:
                status_line = self._render_status_bar(snap)
                parts.extend(status_line.encode('latin-1', errors='replace'))
                self.last_status_key = status_key

            if self.command_mode:
                pass
            else:
                error_active = self.error_message and time.time() < self.error_expire_at
                if not error_active and cursor_in_pane:
                    parts.extend(f'\x1b[{cursor_in_pane[1] + 1};{cursor_in_pane[0] + 1}H'.encode())
                    parts.extend(b'\x1b[?25h')

            text = parts.decode('latin-1', errors='replace')
            if text != self.last_render_text:
                sys.stdout.write(text)
                sys.stdout.flush()
                self.last_render_text = text
        except Exception:
            pass

    def _show_overlay(self, text: str):
        self.reader.stop()

        sys.stdout.write("\x1b[H\x1b[2J")
        sys.stdout.write("\x1b[?25h")

        lines = text.split("\n")
        for i, line in enumerate(lines):
            if i > 0:
                sys.stdout.write("\r\n")
            sys.stdout.write(line)
        sys.stdout.flush()

        os.read(sys.stdin.fileno(), 1)

        self.reader.start()
        self.last_render_text = ""
        self.last_status_key = ""
        sys.stdout.write("\x1b[H\x1b[2J")
        sys.stdout.flush()
        self._request_render()

    def _enter_command_mode(self):
        self.command_mode = True
        self.command_input = ""
        self.command_cursor = 0
        self.error_message = None
        self.last_status_key = ""
        try:
            self.reader.stop()
        except Exception:
            pass
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()

    def _exit_command_mode(self, clear_error=True):
        self.command_mode = False
        self.command_input = ""
        self.command_cursor = 0
        if clear_error:
            pass
        try:
            self.reader.start()
        except Exception:
            pass
        self.last_status_key = ""
        self._request_render()

    def _command_mode_poll(self):
        try:
            r, _, _ = select.select([sys.stdin.fileno()], [], [], 0.02)
            if not r:
                snap_req = False
                if time.time() > (getattr(self, '_last_cmd_render', 0) + 0.15):
                    snap_req = True
                    self._last_cmd_render = time.time()
                if snap_req:
                    msg = recv_msg(self.sock, 0.01)
                    if msg and msg.get("type") == "render":
                        self._apply_render(msg["data"])
                return

            raw = os.read(sys.stdin.fileno(), 4096)
            if not raw:
                return

            for b in raw:
                byte_val = bytes([b])

                if byte_val in (b'\x1b',):
                    self._exit_command_mode()
                    return

                if byte_val in (b'\x0d', b'\x0a'):
                    line = self.command_input.strip()
                    self._execute_command(line)
                    return

                if byte_val in (b'\x7f', b'\x08'):
                    if self.command_cursor > 0:
                        self.command_input = (self.command_input[:self.command_cursor - 1]
                                              + self.command_input[self.command_cursor:])
                        self.command_cursor -= 1
                    continue

                if byte_val == b'\x15':
                    self.command_input = ""
                    self.command_cursor = 0
                    continue

                if byte_val == b'\x17':
                    pos = self.command_cursor
                    if pos > 0:
                        while pos > 0 and self.command_input[pos-1] == ' ':
                            pos -= 1
                        while pos > 0 and self.command_input[pos-1] != ' ':
                            pos -= 1
                        self.command_input = (self.command_input[:pos]
                                              + self.command_input[self.command_cursor:])
                        self.command_cursor = pos
                    continue

                if byte_val == b'\x01':
                    self.command_cursor = 0
                    continue

                if byte_val == b'\x05':
                    self.command_cursor = len(self.command_input)
                    continue

                if byte_val in (b'\x02',):
                    if self.command_cursor > 0:
                        self.command_cursor -= 1
                    continue

                if byte_val in (b'\x06',):
                    if self.command_cursor < len(self.command_input):
                        self.command_cursor += 1
                    continue

                if b >= 0x20 and b <= 0x7e:
                    ch = chr(b)
                    self.command_input = (self.command_input[:self.command_cursor] + ch
                                          + self.command_input[self.command_cursor:])
                    self.command_cursor += 1
                    continue

            self._request_render()
        except Exception as e:
            pass

    def _execute_command(self, line: str):
        try:
            parts = shlex.split(line) if line.strip() else []
        except ValueError:
            self._exec_result(False, "Unbalanced quotes")
            return

        if not parts:
            self._exit_command_mode(False)
            return

        cmd = parts[0].lower()
        args = parts[1:]

        if cmd == "rename":
            if not args:
                self._exec_result(False, "rename: requires <name>")
                return
            new_name = args[0]
            send_msg(self.sock, {
                "cmd": "rename", "session_id": self.session_id,
                "name": new_name
            })
            resp = recv_msg(self.sock, 1.0) or {}
            self._exec_result(resp.get("type") == "ok", resp.get("msg", ""))
            return

        elif cmd == "new":
            name = args[0] if args else "new-session"
            send_msg(self.sock, {
                "cmd": "new", "name": name,
                "cols": self.cols, "rows": self.rows,
                "client_id": self.client_id,
                "background": True
            })
            resp = recv_msg(self.sock, 2.0) or {}
            if resp.get("type") == "ok":
                info = resp.get("info", "") or ""
                self._exec_result(True, f"Created: {resp.get('session_name')!r} "
                                       f"(id={resp.get('session_id')}) {info}")
            else:
                self._exec_result(False, resp.get("msg", "new failed"))
            return

        elif cmd == "kill-session":
            target = args[0] if args else self.session_id
            send_msg(self.sock, {
                "cmd": "kill", "target": target,
                "session_id": self.session_id
            })
            resp = recv_msg(self.sock, 1.0) or {}
            ok = resp.get("type") == "ok"
            info = resp.get("msg", "")
            killed_sid = resp.get("killed_sid")
            if ok and killed_sid == self.session_id:
                self.session_closed = True
                self.running = False
                self._exit_command_mode(False)
                return
            self._exec_result(ok, info)
            return

        elif cmd == "attach":
            if not args:
                self._exec_result(False, "attach: requires <id|name>")
                return
            target = args[0]
            send_msg(self.sock, {"cmd": "detach", "session_id": self.session_id})
            time.sleep(0.05)
            send_msg(self.sock, {
                "cmd": "attach", "session_id": target,
                "cols": self.cols, "rows": self.rows,
                "client_id": self.client_id
            })
            resp = recv_msg(self.sock, 2.0) or {}
            if resp.get("type") == "ok":
                self.session_id = resp["session_id"]
                self.session_name = resp.get("session_name", target)
                self._exec_result(True, resp.get("info", f"Attached to {target}"))
                self.last_render_text = ""
                self.last_status_key = ""
            else:
                send_msg(self.sock, {
                    "cmd": "attach", "session_id": self.session_id,
                    "cols": self.cols, "rows": self.rows,
                    "client_id": self.client_id
                })
                self._exec_result(False, resp.get("msg", "attach failed"))
            return

        elif cmd == "list":
            self._exit_command_mode(False)
            time.sleep(0.05)
            send_msg(self.sock, {"cmd": "list", "client_id": self.client_id})
            resp = recv_msg(self.sock, 1.0) or {}
            if resp.get("type") == "ok":
                lines = ["Active sessions:", "-" * 60]
                for s in resp.get("sessions", []):
                    cur = " <-- current" if s["id"] == self.session_id else ""
                    att = "(attached)" if s["attached"] else ""
                    lines.append(f"  id={s['id']}  name={s['name']!r} "
                                 f"[{s['num_panes']} panes] {att}{cur}")
                lines.append("")
                lines.append("Press any key to continue...")
                self._show_overlay("\n".join(lines))
            return

        elif cmd == "detach":
            self._exit_command_mode(False)
            self._detach()
            return

        elif cmd == "help":
            self._exit_command_mode(False)
            self._show_overlay(
                "Terminal Multiplexer - Command Reference\n"
                "=======================================\n\n"
                "rename <name>          Rename current session\n"
                "new [name]             Create new session (stay attached to current)\n"
                "kill-session [tgt]     Kill current/specified session\n"
                "attach <id|name>       Switch client to target session\n"
                "list                   List all sessions\n"
                "detach                 Detach (same as Ctrl+B d)\n"
                "help                   This help\n\n"
                "Press any key to continue..."
            )
            return

        else:
            self._exec_result(False, f"Unknown command: {cmd!r}. Type 'help' for list.")
            return

    def _exec_result(self, ok: bool, msg: str):
        if ok:
            self.show_error(f"OK: {msg}", 3.0) if msg else None
        else:
            self.show_error(f"ERROR: {msg}" if msg else "ERROR", 5.0)
        self._exit_command_mode(clear_error=False)

    def _cleanup(self):
        try:
            self.reader.stop()
        except Exception:
            pass

        sys.stdout.write("\x1b[?1049l")
        sys.stdout.write("\x1b[?25h")
        sys.stdout.write("\r\n")
        sys.stdout.flush()

        if self.detached:
            print("[detached]")
        elif self.session_closed:
            print("[session closed]")

        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  tmux_like.py new [session_name]    Create new session")
        print("  tmux_like.py attach <id|name>     Attach to session (by id or name)")
        print("  tmux_like.py list                  List active sessions")
        sys.exit(0)

    cmd = sys.argv[1]
    app = ClientApp()

    if cmd == "new":
        name = sys.argv[2] if len(sys.argv) > 2 else "default"
        app.cmd_new(name)
    elif cmd == "attach":
        if len(sys.argv) < 3:
            print("Error: session_id or name required")
            sys.exit(1)
        app.cmd_attach(sys.argv[2])
    elif cmd == "list":
        app.cmd_list()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
