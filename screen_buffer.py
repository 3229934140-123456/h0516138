import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional


@dataclass(eq=True)
class CellAttr:
    fg: int = 37
    bg: int = 40
    fg_rgb: tuple = None
    bg_rgb: tuple = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False
    blink: bool = False
    invisible: bool = False
    strikethrough: bool = False

    def reset(self) -> None:
        self.fg = 37
        self.bg = 40
        self.fg_rgb = None
        self.bg_rgb = None
        self.bold = False
        self.italic = False
        self.underline = False
        self.reverse = False
        self.blink = False
        self.invisible = False
        self.strikethrough = False

    def clone(self) -> "CellAttr":
        return CellAttr(
            fg=self.fg, bg=self.bg,
            fg_rgb=self.fg_rgb, bg_rgb=self.bg_rgb,
            bold=self.bold, italic=self.italic,
            underline=self.underline, reverse=self.reverse,
            blink=self.blink, invisible=self.invisible,
            strikethrough=self.strikethrough
        )


@dataclass
class Cell:
    char: str = " "
    attr: CellAttr = field(default_factory=CellAttr)
    wide: bool = False
    wide_next: bool = False

    def reset(self) -> None:
        self.char = " "
        self.attr.reset()
        self.wide = False
        self.wide_next = False

    def clone(self) -> "Cell":
        return Cell(
            char=self.char,
            attr=self.attr.clone(),
            wide=self.wide,
            wide_next=self.wide_next
        )

    def __eq__(self, other) -> bool:
        if not isinstance(other, Cell):
            return False
        return (self.char == other.char and
                self.attr == other.attr and
                self.wide == other.wide and
                self.wide_next == other.wide_next)


@dataclass
class CursorState:
    x: int = 0
    y: int = 0
    saved_x: int = 0
    saved_y: int = 0
    saved_attr: CellAttr = field(default_factory=CellAttr)
    visible: bool = True
    blinking: bool = True


def is_wide_char(c: str) -> bool:
    if not c:
        return False
    try:
        w = unicodedata.east_asian_width(c)
        return w in ('W', 'F')
    except Exception:
        return False


class ScreenBuffer:
    CSI_PATTERN = re.compile(r'\x1b\[([0-9;?]*)([@-~])')
    OSC_PATTERN = re.compile(r'\x1b\]([0-9]*);([^\x07\x1b]*)(?:\x07|\x1b\\)')

    def __init__(self, width: int = 80, height: int = 24,
                 scrollback_size: int = 10000):
        self.width = max(1, width)
        self.height = max(1, height)
        self.scrollback_size = scrollback_size

        self.cursor = CursorState()
        self.current_attr = CellAttr()

        self.grid: list[list[Cell]] = []
        self.scrollback: list[list[Cell]] = []

        self._init_grid()

        self.mode_alternate = False
        self.mode_insert = False
        self.mode_cursor_rel = False
        self.mode_auto_wrap = True
        self.mode_lnm = True
        self.mode_origin = False

        self.main_grid: list[list[Cell]] = []
        self.main_scrollback: list[list[Cell]] = []
        self.main_cursor: Optional[CursorState] = None
        self.main_margins: tuple = None

        self.top_margin = 0
        self.bottom_margin = max(0, height - 1)

        self._pending_data = bytearray()
        self._utf8_buf = bytearray()
        self.dirty = True

    def _init_grid(self) -> None:
        self.grid = [
            [Cell() for _ in range(self.width)]
            for _ in range(self.height)
        ]

    def _clone_grid(self, grid: list) -> list:
        return [[c.clone() for c in row] for row in grid]

    def resize(self, new_width: int, new_height: int) -> None:
        new_width = max(1, new_width)
        new_height = max(1, new_height)
        if new_width == self.width and new_height == self.height:
            return

        self.width = new_width
        self.height = new_height
        self.bottom_margin = new_height - 1

        new_grid = [
            [Cell() for _ in range(new_width)]
            for _ in range(new_height)
        ]

        copy_rows = min(len(self.grid), new_height)
        for y in range(copy_rows):
            old_row = self.grid[y]
            copy_cols = min(len(old_row), new_width)
            for x in range(copy_cols):
                new_grid[y][x] = old_row[x].clone()

        self.grid = new_grid

        self.cursor.x = min(self.cursor.x, new_width - 1)
        self.cursor.y = min(self.cursor.y, new_height - 1)

        self.dirty = True

    def feed(self, data: bytes) -> None:
        if not data:
            return

        if self._pending_data:
            data = bytes(self._pending_data) + data
            self._pending_data = bytearray()

        i = 0
        n = len(data)
        while i < n:
            b = data[i]

            if b == 0x1b:
                consumed = self._handle_escape(data, i)
                if consumed == 0:
                    self._pending_data = bytearray(data[i:])
                    break
                i += consumed
            elif b == 0x9b:
                consumed = self._handle_8bit_csi(data, i)
                if consumed == 0:
                    self._pending_data = bytearray(data[i:])
                    break
                i += consumed
            else:
                self._handle_byte(b)
                i += 1

        self.dirty = True

    def _handle_byte(self, b: int) -> None:
        if b == 0x00:
            return
        elif b == 0x07:
            return
        elif b == 0x08:
            self._backspace()
        elif b == 0x09:
            self._tab()
        elif b == 0x0a:
            if self.mode_lnm:
                self._carriage_return()
            self._newline()
        elif b == 0x0b:
            self._newline()
        elif b == 0x0c:
            self._newline()
        elif b == 0x0d:
            self._carriage_return()
        elif b == 0x0e:
            pass
        elif b == 0x0f:
            pass
        elif b == 0x7f:
            self._backspace()
        elif 0x20 <= b < 0x7f:
            self._write_char(chr(b))
        elif b >= 0x80:
            self._utf8_buf.append(b)
            try:
                s = bytes(self._utf8_buf).decode('utf-8')
                for c in s:
                    if ord(c) >= 0x20:
                        self._write_char(c)
                self._utf8_buf.clear()
            except UnicodeDecodeError:
                if len(self._utf8_buf) > 4:
                    try:
                        s = bytes(self._utf8_buf).decode('utf-8', errors='replace')
                        for c in s:
                            if ord(c) >= 0x20:
                                self._write_char(c)
                    except Exception:
                        pass
                    self._utf8_buf.clear()

    def _handle_escape(self, data: bytes, start: int) -> int:
        if len(data) - start < 2:
            return 0

        seq_start = start + 1
        next_byte = data[seq_start:seq_start + 1]

        if next_byte == b'[':
            match = self.CSI_PATTERN.match(data[start:].decode('latin-1', errors='replace'))
            if match:
                params_str, cmd = match.groups()
                params, private = self._parse_csi_params(params_str)
                self._handle_csi(cmd, params, private)
                return len(match.group(0))
            return 0

        elif next_byte == b']':
            rest = data[start:]
            match = self.OSC_PATTERN.match(rest.decode('latin-1', errors='replace'))
            if match:
                num_str, content = match.groups()
                try:
                    num = int(num_str) if num_str else 0
                except ValueError:
                    num = 0
                self._handle_osc(num, content)
                return len(match.group(0))
            if len(data) - start < 128:
                return 0
            return len(data) - start

        elif next_byte == b'P':
            end = data.find(b'\x1b\\', start)
            if end < 0:
                return 0
            return (end + 2) - start

        elif next_byte == b'_':
            end = data.find(b'\x1b\\', start)
            if end < 0:
                return 0
            return (end + 2) - start

        elif next_byte == b'^':
            end = data.find(b'\x1b\\', start)
            if end < 0:
                return 0
            return (end + 2) - start

        else:
            try:
                seq_char = next_byte.decode('latin-1', errors='replace')
            except Exception:
                return 2
            self._handle_simple_escape(seq_char)
            return 2

    def _handle_8bit_csi(self, data: bytes, start: int) -> int:
        rest = b'\x1b[' + data[start + 1:]
        match = self.CSI_PATTERN.match(b'\x1b[' + data[start + 1:].decode('latin-1', errors='replace').encode('latin-1'))
        if match:
            full = match.group(0)
            consumed = len(full) - 1
            params_str, cmd = match.groups()
            params, private = self._parse_csi_params(params_str)
            self._handle_csi(cmd, params, private)
            return consumed
        return 0

    def _parse_csi_params(self, params_str: str) -> tuple:
        private = False
        if params_str.startswith('?'):
            private = True
            params_str = params_str[1:]

        if not params_str:
            return [0], private

        parts = re.split(r'[;:]', params_str)
        params = []
        for p in parts:
            try:
                params.append(int(p))
            except ValueError:
                params.append(0)
        if not params:
            params = [0]
        return params, private

    def _handle_csi(self, cmd: str, params: list, private: bool) -> None:
        p = params if params else [0]

        if private:
            if cmd == 'h':
                self._csi_set_private_mode(p)
            elif cmd == 'l':
                self._csi_reset_private_mode(p)
            return

        handlers = {
            '@': self._csi_insert_chars,
            'A': self._csi_cursor_up,
            'B': self._csi_cursor_down,
            'C': self._csi_cursor_right,
            'D': self._csi_cursor_left,
            'E': self._csi_cursor_next_line,
            'F': self._csi_cursor_prev_line,
            'G': self._csi_cursor_column,
            'H': self._csi_cursor_position,
            'J': self._csi_erase_display,
            'K': self._csi_erase_line,
            'L': self._csi_insert_lines,
            'M': self._csi_delete_lines,
            'P': self._csi_delete_chars,
            'X': self._csi_erase_chars,
            'a': self._csi_cursor_right,
            'c': self._csi_reset,
            'd': self._csi_cursor_row,
            'f': self._csi_cursor_position,
            'h': self._csi_set_mode,
            'l': self._csi_reset_mode,
            'm': self._csi_sgr,
            'n': self._csi_status_report,
            'r': self._csi_set_margins,
            's': self._csi_save_cursor,
            't': self._csi_window_manipulation,
            'u': self._csi_restore_cursor,
        }

        handler = handlers.get(cmd)
        if handler:
            try:
                handler(p)
            except Exception:
                pass

    def _handle_simple_escape(self, char: str) -> None:
        handlers = {
            'c': self._reset,
            'D': self._index,
            'E': self._next_line,
            'H': self._tab_set,
            'M': self._reverse_index,
            'N': lambda: None,
            'O': lambda: None,
            'Z': lambda: None,
            '7': self._save_cursor,
            '8': self._restore_cursor,
            '=': lambda: None,
            '>': lambda: None,
            '<': lambda: None,
        }
        handler = handlers.get(char)
        if handler:
            try:
                handler()
            except Exception:
                pass

    def _handle_osc(self, num: int, content: str) -> None:
        pass

    def _write_char(self, c: str) -> None:
        if ord(c) < 0x20:
            return

        x, y = self.cursor.x, self.cursor.y

        wide = is_wide_char(c)

        if y >= self.height:
            self._scroll_up(1)
            y = self.height - 1
            self.cursor.y = y

        if x >= self.width:
            if self.mode_auto_wrap:
                self._carriage_return()
                self._newline()
                x, y = self.cursor.x, self.cursor.y
                if y >= self.height:
                    self._scroll_up(1)
                    y = self.height - 1
                    self.cursor.y = y
            else:
                x = self.width - 1

        if wide and x >= self.width - 1:
            self._carriage_return()
            self._newline()
            x, y = self.cursor.x, self.cursor.y
            if y >= self.height:
                self._scroll_up(1)
                y = self.height - 1
                self.cursor.y = y

        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            return

        if self.mode_insert:
            shift = 2 if wide else 1
            for i in range(self.width - 1, x + shift - 1, -1):
                src = i - shift
                if src >= 0:
                    self.grid[y][i] = self.grid[y][src].clone()
                else:
                    self.grid[y][i] = Cell()

        cell = self.grid[y][x]
        cell.char = c
        cell.attr = self.current_attr.clone()
        cell.wide = wide
        cell.wide_next = False

        if wide and x + 1 < self.width:
            next_cell = self.grid[y][x + 1]
            next_cell.char = " "
            next_cell.wide_next = True

        self.cursor.x += (2 if wide else 1)

    def _scroll_up(self, n: int = 1) -> None:
        if n <= 0:
            return

        for _ in range(n):
            if self.top_margin < self.bottom_margin:
                scrolled_line = self.grid[self.top_margin]
                if (not self.mode_alternate and
                        len(self.scrollback) < self.scrollback_size):
                    self.scrollback.append([c.clone() for c in scrolled_line])
                    if len(self.scrollback) > self.scrollback_size:
                        self.scrollback.pop(0)

                for y in range(self.top_margin, self.bottom_margin):
                    self.grid[y] = self.grid[y + 1]

                self.grid[self.bottom_margin] = [Cell() for _ in range(self.width)]

    def _scroll_down(self, n: int = 1) -> None:
        if n <= 0:
            return

        for _ in range(n):
            if self.top_margin < self.bottom_margin:
                for y in range(self.bottom_margin, self.top_margin, -1):
                    self.grid[y] = self.grid[y - 1]

                self.grid[self.top_margin] = [Cell() for _ in range(self.width)]

    def _newline(self) -> None:
        if self.cursor.y >= self.bottom_margin:
            self._scroll_up(1)
        else:
            self.cursor.y += 1

    def _carriage_return(self) -> None:
        self.cursor.x = 0

    def _backspace(self) -> None:
        if self.cursor.x > 0:
            if self.cursor.x >= 2:
                try:
                    prev = self.grid[self.cursor.y][self.cursor.x - 2]
                    if prev.wide:
                        self.cursor.x -= 2
                        return
                except Exception:
                    pass
            self.cursor.x -= 1

    def _tab(self) -> None:
        next_tab = ((self.cursor.x // 8) + 1) * 8
        if next_tab >= self.width:
            next_tab = self.width - 1
        self.cursor.x = next_tab

    def _index(self) -> None:
        if self.cursor.y == self.bottom_margin:
            self._scroll_up(1)
        else:
            self.cursor.y += 1

    def _reverse_index(self) -> None:
        if self.cursor.y == self.top_margin:
            self._scroll_down(1)
        else:
            self.cursor.y -= 1

    def _next_line(self) -> None:
        self._carriage_return()
        self._newline()

    def _reset(self) -> None:
        self.current_attr.reset()
        self._init_grid()
        self.cursor = CursorState()
        self.top_margin = 0
        self.bottom_margin = self.height - 1
        self.mode_alternate = False
        self.mode_insert = False
        self.mode_auto_wrap = True
        self.mode_lnm = True
        self.mode_origin = False
        self.scrollback.clear()

    def _save_cursor(self) -> None:
        self.cursor.saved_x = self.cursor.x
        self.cursor.saved_y = self.cursor.y
        self.cursor.saved_attr = self.current_attr.clone()

    def _restore_cursor(self) -> None:
        self.cursor.x = max(0, min(self.cursor.saved_x, self.width - 1))
        self.cursor.y = max(0, min(self.cursor.saved_y, self.height - 1))
        self.current_attr = self.cursor.saved_attr.clone()

    def _tab_set(self) -> None:
        pass

    def _csi_cursor_up(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        top = self.top_margin if self.mode_origin else 0
        self.cursor.y = max(top, self.cursor.y - n)

    def _csi_cursor_down(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        bottom = self.bottom_margin if self.mode_origin else (self.height - 1)
        self.cursor.y = min(bottom, self.cursor.y + n)

    def _csi_cursor_right(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        self.cursor.x = min(self.width - 1, self.cursor.x + n)

    def _csi_cursor_left(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        self.cursor.x = max(0, self.cursor.x - n)

    def _csi_cursor_next_line(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        self.cursor.y = min(self.height - 1, self.cursor.y + n)
        self.cursor.x = 0

    def _csi_cursor_prev_line(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        self.cursor.y = max(0, self.cursor.y - n)
        self.cursor.x = 0

    def _csi_cursor_column(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        self.cursor.x = max(0, min(self.width - 1, n - 1))

    def _csi_cursor_row(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        top = self.top_margin if self.mode_origin else 0
        self.cursor.y = max(top, min(self.height - 1, top + n - 1))

    def _csi_cursor_position(self, params: list) -> None:
        row = params[0] if len(params) > 0 and params[0] > 0 else 1
        col = params[1] if len(params) > 1 and params[1] > 0 else 1
        top = self.top_margin if self.mode_origin else 0
        self.cursor.y = max(top, min(self.height - 1, top + row - 1))
        self.cursor.x = max(0, min(self.width - 1, col - 1))

    def _csi_erase_display(self, params: list) -> None:
        n = params[0] if params else 0
        x, y = self.cursor.x, self.cursor.y

        if n == 0:
            for yy in range(y, self.height):
                start_x = x if yy == y else 0
                for xx in range(start_x, self.width):
                    self.grid[yy][xx].reset()
        elif n == 1:
            for yy in range(y + 1):
                end_x = x + 1 if yy == y else self.width
                for xx in range(end_x):
                    self.grid[yy][xx].reset()
        elif n == 2:
            for row in self.grid:
                for cell in row:
                    cell.reset()
        elif n == 3:
            self.scrollback.clear()

    def _csi_erase_line(self, params: list) -> None:
        n = params[0] if params else 0
        y = self.cursor.y
        if y < 0 or y >= self.height:
            return

        if n == 0:
            for x in range(self.cursor.x, self.width):
                self.grid[y][x].reset()
        elif n == 1:
            for x in range(self.cursor.x + 1):
                self.grid[y][x].reset()
        elif n == 2:
            for x in range(self.width):
                self.grid[y][x].reset()

    def _csi_insert_lines(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        if not (self.top_margin <= self.cursor.y <= self.bottom_margin):
            return

        n = min(n, self.bottom_margin - self.cursor.y + 1)
        for _ in range(n):
            for y in range(self.bottom_margin, self.cursor.y, -1):
                self.grid[y] = [c.clone() for c in self.grid[y - 1]]
            self.grid[self.cursor.y] = [Cell() for _ in range(self.width)]

    def _csi_delete_lines(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        if not (self.top_margin <= self.cursor.y <= self.bottom_margin):
            return

        n = min(n, self.bottom_margin - self.cursor.y + 1)
        for _ in range(n):
            for y in range(self.cursor.y, self.bottom_margin):
                self.grid[y] = [c.clone() for c in self.grid[y + 1]]
            self.grid[self.bottom_margin] = [Cell() for _ in range(self.width)]

    def _csi_insert_chars(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        y = self.cursor.y
        if y < 0 or y >= self.height:
            return
        n = min(n, self.width - self.cursor.x)

        for _ in range(n):
            for x in range(self.width - 1, self.cursor.x, -1):
                self.grid[y][x] = self.grid[y][x - 1].clone()
            self.grid[y][self.cursor.x] = Cell()

    def _csi_delete_chars(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        y = self.cursor.y
        if y < 0 or y >= self.height:
            return
        n = min(n, self.width - self.cursor.x)

        for _ in range(n):
            for x in range(self.cursor.x, self.width - 1):
                self.grid[y][x] = self.grid[y][x + 1].clone()
            self.grid[y][self.width - 1] = Cell()

    def _csi_erase_chars(self, params: list) -> None:
        n = params[0] if params and params[0] > 0 else 1
        y = self.cursor.y
        if y < 0 or y >= self.height:
            return
        end_x = min(self.cursor.x + n, self.width)
        for x in range(self.cursor.x, end_x):
            self.grid[y][x].reset()

    def _csi_sgr(self, params: list) -> None:
        if not params or (len(params) == 1 and params[0] == 0):
            self.current_attr.reset()
            return

        i = 0
        while i < len(params):
            p = params[i]

            if p == 0:
                self.current_attr.reset()
            elif p == 1:
                self.current_attr.bold = True
            elif p == 2:
                pass
            elif p == 3:
                self.current_attr.italic = True
            elif p == 4:
                self.current_attr.underline = True
            elif p == 5:
                self.current_attr.blink = True
            elif p == 6:
                self.current_attr.blink = True
            elif p == 7:
                self.current_attr.reverse = True
            elif p == 8:
                self.current_attr.invisible = True
            elif p == 9:
                self.current_attr.strikethrough = True
            elif p == 22:
                self.current_attr.bold = False
            elif p == 23:
                self.current_attr.italic = False
            elif p == 24:
                self.current_attr.underline = False
            elif p == 25:
                self.current_attr.blink = False
            elif p == 27:
                self.current_attr.reverse = False
            elif p == 28:
                self.current_attr.invisible = False
            elif p == 29:
                self.current_attr.strikethrough = False
            elif 30 <= p <= 37:
                self.current_attr.fg = p
                self.current_attr.fg_rgb = None
            elif p == 38:
                if i + 1 < len(params):
                    if params[i + 1] == 5 and i + 2 < len(params):
                        self.current_attr.fg = 38
                        self.current_attr.fg_rgb = (5, params[i + 2])
                        i += 2
                    elif params[i + 1] == 2 and i + 4 < len(params):
                        self.current_attr.fg = 38
                        self.current_attr.fg_rgb = (2, params[i + 2], params[i + 3], params[i + 4])
                        i += 4
            elif p == 39:
                self.current_attr.fg = 37
                self.current_attr.fg_rgb = None
            elif 40 <= p <= 47:
                self.current_attr.bg = p
                self.current_attr.bg_rgb = None
            elif p == 48:
                if i + 1 < len(params):
                    if params[i + 1] == 5 and i + 2 < len(params):
                        self.current_attr.bg = 48
                        self.current_attr.bg_rgb = (5, params[i + 2])
                        i += 2
                    elif params[i + 1] == 2 and i + 4 < len(params):
                        self.current_attr.bg = 48
                        self.current_attr.bg_rgb = (2, params[i + 2], params[i + 3], params[i + 4])
                        i += 4
            elif p == 49:
                self.current_attr.bg = 40
                self.current_attr.bg_rgb = None
            elif 90 <= p <= 97:
                self.current_attr.fg = p
                self.current_attr.fg_rgb = None
            elif 100 <= p <= 107:
                self.current_attr.bg = p
                self.current_attr.bg_rgb = None

            i += 1

    def _csi_set_mode(self, params: list) -> None:
        for p in params:
            if p == 1:
                self.mode_cursor_rel = True
            elif p == 4:
                self.mode_insert = True
            elif p == 5:
                pass
            elif p == 6:
                self.mode_origin = True
            elif p == 7:
                self.mode_auto_wrap = True
            elif p == 20:
                pass

    def _csi_reset_mode(self, params: list) -> None:
        for p in params:
            if p == 1:
                self.mode_cursor_rel = False
            elif p == 4:
                self.mode_insert = False
            elif p == 5:
                pass
            elif p == 6:
                self.mode_origin = False
            elif p == 7:
                self.mode_auto_wrap = False
            elif p == 20:
                pass

    def _csi_set_private_mode(self, params: list) -> None:
        for p in params:
            if p == 1:
                pass
            elif p == 5:
                pass
            elif p == 6:
                pass
            elif p == 7:
                self.mode_auto_wrap = True
            elif p == 25:
                self.cursor.visible = True
            elif p == 1000:
                pass
            elif p == 1002:
                pass
            elif p == 1004:
                pass
            elif p == 1006:
                pass
            elif p == 1047:
                self._enter_alternate_buffer_1047()
            elif p == 1048:
                self._save_cursor()
            elif p == 1049:
                self._save_cursor()
                self._enter_alternate_buffer_1049()

    def _csi_reset_private_mode(self, params: list) -> None:
        for p in params:
            if p == 1:
                pass
            elif p == 5:
                pass
            elif p == 6:
                pass
            elif p == 7:
                pass
            elif p == 25:
                self.cursor.visible = False
            elif p == 1000:
                pass
            elif p == 1002:
                pass
            elif p == 1004:
                pass
            elif p == 1006:
                pass
            elif p == 1047:
                self._exit_alternate_buffer_1047()
            elif p == 1048:
                self._restore_cursor()
            elif p == 1049:
                self._exit_alternate_buffer_1049()
                self._restore_cursor()

    def _csi_set_margins(self, params: list) -> None:
        top = params[0] if len(params) > 0 and params[0] > 0 else 1
        bottom = params[1] if len(params) > 1 and params[1] > 0 else self.height

        self.top_margin = max(0, top - 1)
        self.bottom_margin = min(self.height - 1, bottom - 1)
        if self.top_margin >= self.bottom_margin:
            self.top_margin = 0
            self.bottom_margin = self.height - 1
        self.cursor.x = 0
        self.cursor.y = 0

    def _csi_save_cursor(self, params: list) -> None:
        self._save_cursor()

    def _csi_restore_cursor(self, params: list) -> None:
        self._restore_cursor()

    def _csi_reset(self, params: list) -> None:
        self._reset()

    def _csi_status_report(self, params: list) -> None:
        pass

    def _csi_window_manipulation(self, params: list) -> None:
        pass

    def _enter_alternate_buffer_1047(self) -> None:
        if not self.mode_alternate:
            self.main_grid = self._clone_grid(self.grid)
            self.main_scrollback = self._clone_grid(self.scrollback)
            self._init_grid()
            self.scrollback.clear()
            self.mode_alternate = True

    def _exit_alternate_buffer_1047(self) -> None:
        if self.mode_alternate:
            self.grid = self.main_grid
            self.scrollback = self.main_scrollback
            self.main_grid = []
            self.main_scrollback = []
            self.mode_alternate = False

    def _enter_alternate_buffer_1049(self) -> None:
        if not self.mode_alternate:
            self.main_grid = self._clone_grid(self.grid)
            self.main_scrollback = self._clone_grid(self.scrollback)
            self.main_cursor = CursorState(
                x=self.cursor.x, y=self.cursor.y,
                saved_x=self.cursor.saved_x, saved_y=self.cursor.saved_y,
                visible=self.cursor.visible
            )
            self.main_margins = (self.top_margin, self.bottom_margin)
            self._init_grid()
            self.scrollback.clear()
            self.cursor.x = 0
            self.cursor.y = 0
            self.top_margin = 0
            self.bottom_margin = self.height - 1
            self.mode_alternate = True

    def _exit_alternate_buffer_1049(self) -> None:
        if self.mode_alternate:
            self.grid = self.main_grid
            self.scrollback = self.main_scrollback
            self.main_grid = []
            self.main_scrollback = []
            if self.main_cursor:
                self.cursor = self.main_cursor
                self.main_cursor = None
            if self.main_margins:
                self.top_margin, self.bottom_margin = self.main_margins
                self.main_margins = None
            self.mode_alternate = False

    def _enter_alternate_buffer(self) -> None:
        self._enter_alternate_buffer_1049()

    def _exit_alternate_buffer(self) -> None:
        self._exit_alternate_buffer_1049()

    def get_visible_lines(self) -> list[str]:
        lines = []
        for row in self.grid:
            chars = []
            skip_next = False
            for cell in row:
                if skip_next:
                    skip_next = False
                    continue
                chars.append(cell.char)
                if cell.wide:
                    skip_next = True
            lines.append(''.join(chars))
        return lines

    def get_full_screen(self) -> str:
        return '\n'.join(self.get_visible_lines())

    def get_grid_for_render(self) -> tuple:
        lines = []
        cursor_info = None

        for y, row in enumerate(self.grid):
            line_cells = []
            x = 0
            while x < len(row):
                cell = row[x]
                if cell.wide_next and x > 0:
                    prev = row[x - 1]
                    if prev.wide:
                        x += 1
                        continue
                width = 2 if cell.wide else 1
                line_cells.append((x, cell.char, cell.attr.clone(), width))
                x += width
            lines.append(line_cells)

        if self.cursor.visible:
            cursor_info = (self.cursor.x, self.cursor.y)

        return lines, cursor_info

    def get_render_sequence(self, offset_x: int = 0, offset_y: int = 0) -> bytes:
        result = bytearray()
        result.extend(b'\x1b[?25l')

        last_attr = None
        for y, row in enumerate(self.grid):
            result.extend(f'\x1b[{offset_y + y + 1};{offset_x + 1}H'.encode())

            x = 0
            while x < len(row):
                cell = row[x]

                if cell.wide_next and x > 0:
                    prev = row[x - 1]
                    if prev.wide:
                        x += 1
                        continue

                if last_attr is None or cell.attr != last_attr:
                    result.extend(self._attr_to_sequence(cell.attr))
                    last_attr = cell.attr

                char_bytes = cell.char.encode('utf-8', errors='replace')
                result.extend(char_bytes)
                if cell.wide and x + 1 < len(row):
                    pass

                x += 1

        if self.cursor.visible:
            cx = offset_x + min(self.cursor.x, self.width - 1)
            cy = offset_y + min(self.cursor.y, self.height - 1)
            result.extend(f'\x1b[{cy + 1};{cx + 1}H'.encode())
            result.extend(b'\x1b[?25h')

        return bytes(result)

    def _attr_to_sequence(self, attr: CellAttr) -> bytes:
        parts = []
        parts.append(0)
        if attr.bold:
            parts.append(1)
        if attr.italic:
            parts.append(3)
        if attr.underline:
            parts.append(4)
        if attr.blink:
            parts.append(5)
        if attr.reverse:
            parts.append(7)

        if attr.fg_rgb and len(attr.fg_rgb) >= 2:
            if attr.fg_rgb[0] == 5:
                parts.extend([38, 5, attr.fg_rgb[1]])
            elif attr.fg_rgb[0] == 2 and len(attr.fg_rgb) >= 4:
                parts.extend([38, 2, attr.fg_rgb[1], attr.fg_rgb[2], attr.fg_rgb[3]])
            else:
                parts.append(attr.fg)
        else:
            parts.append(attr.fg)

        if attr.bg_rgb and len(attr.bg_rgb) >= 2:
            if attr.bg_rgb[0] == 5:
                parts.extend([48, 5, attr.bg_rgb[1]])
            elif attr.bg_rgb[0] == 2 and len(attr.bg_rgb) >= 4:
                parts.extend([48, 2, attr.bg_rgb[1], attr.bg_rgb[2], attr.bg_rgb[3]])
            else:
                parts.append(attr.bg)
        else:
            parts.append(attr.bg)

        return b'\x1b[' + b';'.join(str(p).encode() for p in parts) + b'm'

    def get_cursor(self) -> tuple:
        return (self.cursor.x, self.cursor.y, self.cursor.visible)

    def get_scrollback_lines(self, n: int = 100) -> list[str]:
        lines = []
        start = max(0, len(self.scrollback) - n)
        for row in self.scrollback[start:]:
            chars = []
            skip_next = False
            for cell in row:
                if skip_next:
                    skip_next = False
                    continue
                chars.append(cell.char)
                if cell.wide:
                    skip_next = True
            lines.append(''.join(chars))
        return lines

    def clear_dirty(self) -> None:
        self.dirty = False

    def has_damage(self) -> bool:
        return self.dirty
