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

    def create_session(self, name: str, cols: int, rows: int) -> SessionData:
        with self.lock:
            sid = str(uuid.uuid4())[:8]
            pane_id = str(uuid.uuid4())[:8]
            root = LayoutNode(is_leaf=True, pane_id=pane_id, parent=None)
            sess = SessionData(id=sid, name=name, root=root)

            fd = self.pty_mgr.create_pty(cols, rows, DEFAULT_SHELL)
            buf = ScreenBuffer(cols, rows)
            pane = PaneData(id=pane_id, session_id=sid, pty_fd=fd,
                            buffer=buf, x=0, y=0, width=cols, height=rows)
            sess.add_pane(pane)
            self._fd_to_pane[fd] = pane
            self.sessions[sid] = sess
            return sess

    def kill_session(self, sid: str) -> None:
        with self.lock:
            if sid not in self.sessions:
                return
            sess = self.sessions[sid]
            for p in list(sess.panes.values()):
                if p.pty_fd is not None:
                    try:
                        del self._fd_to_pane[p.pty_fd]
                    except Exception:
                        pass
                    self.pty_mgr.destroy_pty(p.pty_fd)
            del self.sessions[sid]

    def attach(self, sid: str, client_id: str) -> Optional[SessionData]:
        with self.lock:
            if sid not in self.sessions:
                return None
            s = self.sessions[sid]
            if s.attached and s.attached_client:
                s.attached = False
                s.attached_client = None
            s.attached = True
            s.attached_client = client_id
            s.touch()
            return s

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

    def _relayout(self, s: SessionData, total_w: int, total_h: int) -> None:
        pty_sizes = {}
        _apply_layout(s.root, 0, 0, total_w, total_h, s.panes, pty_sizes)
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

            if direction == 'horizontal':
                h1 = max(MIN_PANE_H, int(cur.height * ratio))
                h2 = max(MIN_PANE_H, cur.height - h1)
                total = h1 + h2
                if total != cur.height:
                    h1 = cur.height - h2
                fd = self.pty_mgr.create_pty(cur.width, h1, DEFAULT_SHELL)
                buf = ScreenBuffer(cur.width, h1)
                new_pane = PaneData(id=pane_id, session_id=sid, pty_fd=fd,
                                    buffer=buf, x=cur.x, y=cur.y,
                                    width=cur.width, height=h1)
            else:
                w1 = max(MIN_PANE_W, int(cur.width * ratio))
                w2 = max(MIN_PANE_W, cur.width - w1)
                total = w1 + w2
                if total != cur.width:
                    w1 = cur.width - w2
                fd = self.pty_mgr.create_pty(w1, cur.height, DEFAULT_SHELL)
                buf = ScreenBuffer(w1, cur.height)
                new_pane = PaneData(id=pane_id, session_id=sid, pty_fd=fd,
                                    buffer=buf, x=cur.x, y=cur.y,
                                    width=w1, height=cur.height)

            s.add_pane(new_pane)
            self._fd_to_pane[fd] = new_pane

            if new_root is not None:
                s.root = new_root

            total_w = max(p.width + p.x for p in s.panes.values())
            total_h = max(p.height + p.y for p in s.panes.values())
            self._relayout(s, total_w, total_h)

            s.focus_pane(pane_id)
            s.touch()
            return new_pane

    def close_focused(self, sid: str) -> bool:
        with self.lock:
            s = self.sessions.get(sid)
            if not s:
                return False
            cur = s.focused()
            if not cur:
                return False
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
                return True

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
            elif s.root.is_leaf and len(s.panes) <= 1:
                pass

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

            total_w = max(p.width + p.x for p in s.panes.values()) if s.panes else 80
            total_h = max(p.height + p.y for p in s.panes.values()) if s.panes else 24
            self._relayout(s, total_w, total_h)

            if focus_target and focus_target in s.panes:
                s.focus_pane(focus_target)
            elif s.list_pane_ids():
                s.focus_pane(s.list_pane_ids()[0])

            s.touch()
            return False

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

    def resize_session(self, sid: str, cols: int, rows: int) -> None:
        with self.lock:
            s = self.sessions.get(sid)
            if not s:
                return
            cols = max(MIN_PANE_W, cols)
            rows = max(MIN_PANE_H, rows)
            self._relayout(s, cols, rows)
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
                    sess = SERVER.create_session(name, cols, rows)
                    clients[sess.id] = conn
                    SERVER.attach(sess.id, client_id)
                    send_msg(conn, {"type": "ok", "session_id": sess.id})

                elif cmd == "attach":
                    sid = msg.get("session_id", "")
                    sess = SERVER.attach(sid, client_id)
                    if sess:
                        clients[sid] = conn
                        cols = msg.get("cols", 80)
                        rows = msg.get("rows", 24)
                        SERVER.resize_session(sid, cols, rows)
                        send_msg(conn, {"type": "ok", "session_id": sid})
                    else:
                        send_msg(conn, {"type": "error", "msg": "session not found"})

                elif cmd == "detach":
                    sid = msg.get("session_id", "")
                    SERVER.detach(sid)
                    if sid in clients:
                        del clients[sid]
                    send_msg(conn, {"type": "ok"})

                elif cmd == "list":
                    send_msg(conn, {"type": "ok", "sessions": SERVER.list_sessions()})

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
                    SERVER.close_focused(sid)
                    if sid not in SERVER.sessions:
                        try:
                            send_msg(conn, {"type": "session_closed"})
                        except Exception:
                            pass
                        if sid in clients:
                            del clients[sid]

                elif cmd == "focus":
                    sid = msg.get("session_id", "")
                    direction = msg.get("direction", "")
                    SERVER.focus_neighbor(sid, direction)

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
                    send_msg(conn, {"type": "pong"})

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
        return max(24, rows), max(80, cols)
    except Exception:
        return 24, 80


class ClientApp:
    def __init__(self):
        self.client_id = str(uuid.uuid4())[:12]
        self.sock: Optional[socket.socket] = None
        self.reader = KeyboardInputReader()
        self.session_id: Optional[str] = None
        self.rows, self.cols = 24, 80
        self.last_render_text = ""
        self.detached = False
        self.running = False
        self.session_closed = False

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
            print("Failed to create session")
            sys.exit(1)
        self.session_id = resp["session_id"]
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
            print("Session not found")
            sys.exit(1)
        self.session_id = sid
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
            print("-" * 60)
            for s in sess:
                attached = "(attached)" if s["attached"] else "  (detached)"
                activity = time.ctime(s["last_activity"])
                print(f"  {s['id']}: {s['name']} [{s['num_panes']} pane(s)] "
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
            time.sleep(0.1)
            send_msg(self.sock, {"cmd": "list", "client_id": self.client_id})
            resp = recv_msg(self.sock, 0.5)
            if resp and resp.get("type") == "ok":
                ids = [s["id"] for s in resp["sessions"]]
                if self.session_id not in ids:
                    self.session_closed = True
                    self.running = False

        def do_help():
            self._show_overlay(
                "Terminal Multiplexer - Help\n"
                "==========================\n"
                "Prefix key: Ctrl+B\n\n"
                "  Ctrl+B \"    Split horizontally (上下)\n"
                "  Ctrl+B %    Split vertically (左右)\n"
                "  Ctrl+B x    Close current pane\n"
                "  Ctrl+B d    Detach from session (普通 d)\n"
                "  Ctrl+B C-d  Detach from session (Ctrl+d)\n"
                "  Ctrl+B s    List sessions\n"
                "  Ctrl+B ?    This help\n"
                "  Ctrl+B <arrow>  Move focus\n\n"
                "  Ctrl+C       Sent to focused pane\n"
                "  Resize terminal = panes auto-adjust\n\n"
                "Press any key to continue..."
            )

        def do_list():
            send_msg(self.sock, {"cmd": "list", "client_id": self.client_id})
            resp = recv_msg(self.sock, 1.0)
            if resp and resp.get("type") == "ok":
                lines = ["Active sessions:", "-" * 50]
                for s in resp["sessions"]:
                    cur = " <-- current" if s["id"] == self.session_id else ""
                    att = "(attached)" if s["attached"] else ""
                    lines.append(f"  {s['id']}: {s['name']} [{s['num_panes']} panes] {att}{cur}")
                lines.append("")
                lines.append("Press any key to continue...")
                self._show_overlay("\n".join(lines))

        r.register_handler('detach', do_detach)
        r.register_handler('split', do_split)
        r.register_handler('focus', do_focus)
        r.register_handler('close_pane', do_close)
        r.register_handler('help', do_help)
        r.register_handler('list_sessions', do_list)

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
        while self.running and not self.detached and not self.session_closed:
            try:
                self.reader.poll()
            except Exception:
                pass

            msg = recv_msg(self.sock, 0.01)
            if msg:
                mtype = msg.get("type")
                if mtype == "render":
                    self._apply_render(msg["data"])
                elif mtype == "error":
                    break
                elif mtype == "session_closed":
                    self.session_closed = True
                    break

    def _apply_render(self, snap: dict):
        try:
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
            if focus_pane:
                cx = focus_pane['x'] + min(focus_pane['cursor_x'], focus_pane['width'] - 1)
                cy = focus_pane['y'] + min(focus_pane['cursor_y'], focus_pane['height'] - 1)
                if focus_pane.get("cursor_vis", True):
                    parts.extend(f'\x1b[{cy + 1};{cx + 1}H'.encode())
                    parts.extend(b'\x1b[?25h')
                else:
                    parts.extend(b'\x1b[?25l')

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
        sys.stdout.write("\x1b[H\x1b[2J")
        sys.stdout.flush()
        self._request_render()

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
        print("  tmux_like.py new [session_name]   - Create new session")
        print("  tmux_like.py attach <session_id> - Attach to existing session")
        print("  tmux_like.py list                 - List active sessions")
        sys.exit(0)

    cmd = sys.argv[1]
    app = ClientApp()

    if cmd == "new":
        name = sys.argv[2] if len(sys.argv) > 2 else "default"
        app.cmd_new(name)
    elif cmd == "attach":
        if len(sys.argv) < 3:
            print("Error: session_id required")
            sys.exit(1)
        app.cmd_attach(sys.argv[2])
    elif cmd == "list":
        app.cmd_list()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
