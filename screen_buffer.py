import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CellAttr:
    fg: int = 37
    bg: int = 40
    bold: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False
    blink: bool = False
    invisible: bool = False

    def reset(self) -> None:
        self.fg = 37
        self.bg = 40
        self.bold = False
        self.italic = False
        self.underline = False
        self.reverse = False
        self.blink = False
        self.invisible = False

    def clone(self) -> "CellAttr":
        return CellAttr(
            fg=self.fg, bg=self.bg, bold=self.bold, italic=self.italic,
            underline=self.underline, reverse=self.reverse,
            blink=self.blink, invisible=self.invisible
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


@dataclass
class CursorState:
    x: int = 0
    y: int = 0
    saved_x: int = 0
    saved_y: int = 0
    visible: bool = True
    blinking: bool = True


class ScreenBuffer:
    CSI_PATTERN = re.compile(r'\x1b\[([0-9;]*)([@-~])')
    OSC_PATTERN = re.compile(r'\x1b\]([0-9]*);([^\x07]*)\x07')

    def __init__(self, width: int = 80, height: int = 24, 
                 scrollback_size: int = 1000):
        self.width = width
        self.height = height
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
        
        self.main_grid: list[list[Cell]] = []
        self.main_scrollback: list[list[Cell]] = []
        
        self.top_margin = 0
        self.bottom_margin = height - 1
        self.left_margin = 0
        self.right_margin = width - 1
        
        self._pending_escape = bytearray()
        self.dirty = True

    def _init_grid(self) -> None:
        self.grid = [
            [Cell() for _ in range(self.width)]
            for _ in range(self.height)
        ]

    def resize(self, new_width: int, new_height: int) -> None:
        if new_width == self.width and new_height == self.height:
            return
        
        old_width, old_height = self.width, self.height
        self.width = new_width
        self.height = new_height
        
        new_grid = [
            [Cell() for _ in range(new_width)]
            for _ in range(new_height)
        ]
        
        copy_rows = min(old_height, new_height)
        copy_cols = min(old_width, new_width)
        
        for y in range(copy_rows):
            for x in range(copy_cols):
                new_grid[y][x] = self.grid[y][x]
        
        self.grid = new_grid
        self.bottom_margin = new_height - 1
        self.right_margin = new_width - 1
        
        if self.cursor.x >= new_width:
            self.cursor.x = new_width - 1
        if self.cursor.y >= new_height:
            self.cursor.y = new_height - 1
        
        self.dirty = True

    def feed(self, data: bytes) -> None:
        if self._pending_escape:
            data = bytes(self._pending_escape) + data
            self._pending_escape = bytearray()
        
        i = 0
        while i < len(data):
            if data[i] == 0x1b:
                consumed = self._handle_escape(data, i)
                if consumed == 0:
                    self._pending_escape = bytearray(data[i:])
                    break
                i += consumed
            else:
                self._handle_char(data[i:i+1])
                i += 1
        
        self.dirty = True

    def _handle_escape(self, data: bytes, start: int) -> int:
        if len(data) <= start:
            return 0
        
        seq_start = start + 1
        
        if seq_start >= len(data):
            return 0
        
        if data[seq_start:seq_start+1] == b'[':
            match = self.CSI_PATTERN.match(data[start:].decode('latin-1', errors='replace'))
            if match:
                params_str, cmd = match.groups()
                params = self._parse_csi_params(params_str)
                self._handle_csi(cmd, params)
                return len(match.group(0))
            return 0
        
        elif data[seq_start:seq_start+1] == b']':
            match = self.OSC_PATTERN.match(data[start:].decode('latin-1', errors='replace'))
            if match:
                num, content = match.groups()
                self._handle_osc(int(num) if num else 0, content)
                return len(match.group(0))
            return 0
        
        else:
            seq_char = data[seq_start:seq_start+1].decode('latin-1', errors='replace')
            self._handle_simple_escape(seq_char)
            return 2

    def _parse_csi_params(self, params_str: str) -> list[int]:
        if not params_str:
            return [0]
        parts = params_str.split(';')
        params = []
        for p in parts:
            try:
                params.append(int(p))
            except ValueError:
                params.append(0)
        return params

    def _handle_csi(self, cmd: str, params: list[int]) -> None:
        p = params if params else [0]
        
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
            'c': self._csi_reset,
            'h': self._csi_set_mode,
            'l': self._csi_reset_mode,
            'm': self._csi_sgr,
            'r': self._csi_set_margins,
            's': self._csi_save_cursor,
            'u': self._csi_restore_cursor,
        }
        
        handler = handlers.get(cmd)
        if handler:
            handler(p)

    def _handle_simple_escape(self, char: str) -> None:
        handlers = {
            'c': self._reset,
            'D': self._index,
            'E': self._next_line,
            'H': self._tab_set,
            'M': self._reverse_index,
            '7': self._save_cursor,
            '8': self._restore_cursor,
        }
        handler = handlers.get(char)
        if handler:
            handler()

    def _handle_osc(self, num: int, content: str) -> None:
        pass

    def _handle_char(self, byte_data: bytes) -> None:
        try:
            char = byte_data.decode('utf-8', errors='replace')
        except UnicodeDecodeError:
            char = '?'
        
        for c in char:
            code = ord(c)
            
            if code == 0x00:
                continue
            elif code == 0x07:
                continue
            elif code == 0x08:
                self._backspace()
            elif code == 0x09:
                self._tab()
            elif code == 0x0a:
                self._newline()
            elif code == 0x0b:
                self._newline()
            elif code == 0x0c:
                self._newline()
            elif code == 0x0d:
                self._carriage_return()
            elif code < 0x20 or code == 0x7f:
                continue
            else:
                self._write_char(c)

    def _write_char(self, c: str) -> None:
        x, y = self.cursor.x, self.cursor.y
        
        if y >= self.height:
            self._scroll_up()
            y = self.height - 1
            self.cursor.y = y
        
        if x >= self.width:
            if self.mode_auto_wrap:
                self._carriage_return()
                self._newline()
                x, y = self.cursor.x, self.cursor.y
            else:
                x = self.width - 1
        
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            return
        
        if self.mode_insert:
            for i in range(self.width - 1, x, -1):
                if i - 1 >= 0:
                    self.grid[y][i] = self.grid[y][i - 1].clone() if hasattr(self.grid[y][i - 1], 'clone') else Cell()
                else:
                    self.grid[y][i] = Cell()
        
        cell = self.grid[y][x]
        cell.char = c
        cell.attr = self.current_attr.clone()
        
        self.cursor.x += 1

    def _scroll_up(self, n: int = 1) -> None:
        for _ in range(n):
            if self.top_margin < self.bottom_margin:
                scrolled_line = self.grid[self.top_margin]
                if len(self.scrollback) < self.scrollback_size:
                    self.scrollback.append([c.clone() for c in scrolled_line])
                
                for y in range(self.top_margin, self.bottom_margin):
                    self.grid[y] = self.grid[y + 1]
                
                self.grid[self.bottom_margin] = [Cell() for _ in range(self.width)]

    def _scroll_down(self, n: int = 1) -> None:
        for _ in range(n):
            if self.top_margin < self.bottom_margin:
                for y in range(self.bottom_margin, self.top_margin, -1):
                    self.grid[y] = self.grid[y - 1]
                
                self.grid[self.top_margin] = [Cell() for _ in range(self.width)]

    def _newline(self) -> None:
        if self.cursor.y >= self.bottom_margin:
            self._scroll_up()
        else:
            self.cursor.y += 1

    def _carriage_return(self) -> None:
        self.cursor.x = 0

    def _backspace(self) -> None:
        if self.cursor.x > 0:
            self.cursor.x -= 1

    def _tab(self) -> None:
        next_tab = ((self.cursor.x // 8) + 1) * 8
        if next_tab >= self.width:
            next_tab = self.width - 1
        self.cursor.x = next_tab

    def _index(self) -> None:
        if self.cursor.y == self.bottom_margin:
            self._scroll_up()
        else:
            self.cursor.y += 1

    def _reverse_index(self) -> None:
        if self.cursor.y == self.top_margin:
            self._scroll_down()
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
        self.left_margin = 0
        self.right_margin = self.width - 1
        self.mode_alternate = False
        self.mode_insert = False
        self.mode_auto_wrap = True

    def _save_cursor(self) -> None:
        self.cursor.saved_x = self.cursor.x
        self.cursor.saved_y = self.cursor.y

    def _restore_cursor(self) -> None:
        self.cursor.x = min(self.cursor.saved_x, self.width - 1)
        self.cursor.y = min(self.cursor.saved_y, self.height - 1)

    def _tab_set(self) -> None:
        pass

    def _csi_cursor_up(self, params: list[int]) -> None:
        n = params[0] if params and params[0] > 0 else 1
        self.cursor.y = max(0, self.cursor.y - n)

    def _csi_cursor_down(self, params: list[int]) -> None:
        n = params[0] if params and params[0] > 0 else 1
        self.cursor.y = min(self.height - 1, self.cursor.y + n)

    def _csi_cursor_right(self, params: list[int]) -> None:
        n = params[0] if params and params[0] > 0 else 1
        self.cursor.x = min(self.width - 1, self.cursor.x + n)

    def _csi_cursor_left(self, params: list[int]) -> None:
        n = params[0] if params and params[0] > 0 else 1
        self.cursor.x = max(0, self.cursor.x - n)

    def _csi_cursor_next_line(self, params: list[int]) -> None:
        n = params[0] if params and params[0] > 0 else 1
        self.cursor.y = min(self.height - 1, self.cursor.y + n)
        self.cursor.x = 0

    def _csi_cursor_prev_line(self, params: list[int]) -> None:
        n = params[0] if params and params[0] > 0 else 1
        self.cursor.y = max(0, self.cursor.y - n)
        self.cursor.x = 0

    def _csi_cursor_column(self, params: list[int]) -> None:
        n = params[0] if params else 1
        self.cursor.x = max(0, min(self.width - 1, n - 1))

    def _csi_cursor_position(self, params: list[int]) -> None:
        row = params[0] if len(params) > 0 else 1
        col = params[1] if len(params) > 1 else 1
        self.cursor.y = max(0, min(self.height - 1, row - 1))
        self.cursor.x = max(0, min(self.width - 1, col - 1))

    def _csi_erase_display(self, params: list[int]) -> None:
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
            self.cursor.x = 0
            self.cursor.y = 0
        elif n == 3:
            self.scrollback.clear()

    def _csi_erase_line(self, params: list[int]) -> None:
        n = params[0] if params else 0
        y = self.cursor.y
        
        if n == 0:
            for x in range(self.cursor.x, self.width):
                self.grid[y][x].reset()
        elif n == 1:
            for x in range(self.cursor.x + 1):
                self.grid[y][x].reset()
        elif n == 2:
            for x in range(self.width):
                self.grid[y][x].reset()

    def _csi_insert_lines(self, params: list[int]) -> None:
        n = params[0] if params and params[0] > 0 else 1
        for _ in range(n):
            if self.top_margin <= self.cursor.y <= self.bottom_margin:
                for y in range(self.bottom_margin, self.cursor.y, -1):
                    self.grid[y] = self.grid[y - 1]
                self.grid[self.cursor.y] = [Cell() for _ in range(self.width)]

    def _csi_delete_lines(self, params: list[int]) -> None:
        n = params[0] if params and params[0] > 0 else 1
        for _ in range(n):
            if self.top_margin <= self.cursor.y <= self.bottom_margin:
                for y in range(self.cursor.y, self.bottom_margin):
                    self.grid[y] = self.grid[y + 1]
                self.grid[self.bottom_margin] = [Cell() for _ in range(self.width)]

    def _csi_insert_chars(self, params: list[int]) -> None:
        n = params[0] if params and params[0] > 0 else 1
        y = self.cursor.y
        for _ in range(n):
            for x in range(self.width - 1, self.cursor.x, -1):
                self.grid[y][x] = self.grid[y][x - 1].clone() if hasattr(self.grid[y][x - 1], 'clone') else Cell()
            self.grid[y][self.cursor.x] = Cell()

    def _csi_delete_chars(self, params: list[int]) -> None:
        n = params[0] if params and params[0] > 0 else 1
        y = self.cursor.y
        for _ in range(n):
            for x in range(self.cursor.x, self.width - 1):
                self.grid[y][x] = self.grid[y][x + 1].clone() if hasattr(self.grid[y][x + 1], 'clone') else Cell()
            self.grid[y][self.width - 1] = Cell()

    def _csi_erase_chars(self, params: list[int]) -> None:
        n = params[0] if params and params[0] > 0 else 1
        y = self.cursor.y
        end_x = min(self.cursor.x + n, self.width)
        for x in range(self.cursor.x, end_x):
            self.grid[y][x].reset()

    def _csi_sgr(self, params: list[int]) -> None:
        if not params or params == [0]:
            self.current_attr.reset()
            return
        
        i = 0
        while i < len(params):
            p = params[i]
            
            if p == 0:
                self.current_attr.reset()
            elif p == 1:
                self.current_attr.bold = True
            elif p == 3:
                self.current_attr.italic = True
            elif p == 4:
                self.current_attr.underline = True
            elif p == 5:
                self.current_attr.blink = True
            elif p == 7:
                self.current_attr.reverse = True
            elif p == 8:
                self.current_attr.invisible = True
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
            elif 30 <= p <= 37:
                self.current_attr.fg = p
            elif p == 38 and i + 2 < len(params) and params[i + 1] == 5:
                self.current_attr.fg = 38
                i += 2
            elif p == 38 and i + 4 < len(params) and params[i + 1] == 2:
                self.current_attr.fg = 38
                i += 4
            elif p == 39:
                self.current_attr.fg = 37
            elif 40 <= p <= 47:
                self.current_attr.bg = p
            elif p == 48 and i + 2 < len(params) and params[i + 1] == 5:
                self.current_attr.bg = 48
                i += 2
            elif p == 48 and i + 4 < len(params) and params[i + 1] == 2:
                self.current_attr.bg = 48
                i += 4
            elif p == 49:
                self.current_attr.bg = 40
            elif 90 <= p <= 97:
                self.current_attr.fg = p
            elif 100 <= p <= 107:
                self.current_attr.bg = p
            
            i += 1

    def _csi_set_mode(self, params: list[int]) -> None:
        for p in params:
            if p == 1:
                self.mode_cursor_rel = True
            elif p == 4:
                self.mode_insert = True
            elif p == 7:
                self.mode_auto_wrap = True
            elif p == 25:
                self.cursor.visible = True
            elif p == 47 or p == 1047:
                self._enter_alternate_buffer()
            elif p == 1049:
                self._enter_alternate_buffer()
                self.cursor.x = 0
                self.cursor.y = 0

    def _csi_reset_mode(self, params: list[int]) -> None:
        for p in params:
            if p == 1:
                self.mode_cursor_rel = False
            elif p == 4:
                self.mode_insert = False
            elif p == 7:
                self.mode_auto_wrap = False
            elif p == 25:
                self.cursor.visible = False
            elif p == 47 or p == 1047:
                self._exit_alternate_buffer()
            elif p == 1049:
                self._exit_alternate_buffer()

    def _csi_set_margins(self, params: list[int]) -> None:
        top = params[0] if len(params) > 0 and params[0] > 0 else 1
        bottom = params[1] if len(params) > 1 and params[1] > 0 else self.height
        
        self.top_margin = max(0, top - 1)
        self.bottom_margin = min(self.height - 1, bottom - 1)
        self.cursor.x = 0
        self.cursor.y = 0

    def _csi_save_cursor(self, params: list[int]) -> None:
        self._save_cursor()

    def _csi_restore_cursor(self, params: list[int]) -> None:
        self._restore_cursor()

    def _csi_reset(self, params: list[int]) -> None:
        self._reset()

    def _enter_alternate_buffer(self) -> None:
        if not self.mode_alternate:
            self.main_grid = [[c.clone() for c in row] for row in self.grid]
            self.main_scrollback = [[c.clone() for c in row] for row in self.scrollback]
            self._init_grid()
            self.scrollback.clear()
            self.mode_alternate = True

    def _exit_alternate_buffer(self) -> None:
        if self.mode_alternate:
            self.grid = self.main_grid
            self.scrollback = self.main_scrollback
            self.mode_alternate = False

    def get_visible_lines(self) -> list[str]:
        lines = []
        for row in self.grid:
            line = ''.join(cell.char for cell in row)
            lines.append(line.rstrip())
        return lines

    def get_full_screen(self) -> str:
        lines = self.get_visible_lines()
        return '\n'.join(lines)

    def get_render_sequence(self) -> bytes:
        result = bytearray()
        result.extend(b'\x1b[?25l')
        result.extend(b'\x1b[H')
        
        last_attr = None
        for y, row in enumerate(self.grid):
            if y > 0:
                result.extend(b'\r\n')
            
            for x, cell in enumerate(row):
                if last_attr is None or cell.attr != last_attr:
                    result.extend(self._attr_to_sequence(cell.attr))
                    last_attr = cell.attr
                
                result.append(ord(cell.char))
        
        if self.cursor.visible:
            result.extend(b'\x1b[%d;%dH' % (self.cursor.y + 1, self.cursor.x + 1))
            result.extend(b'\x1b[?25h')
        
        return bytes(result)

    def _attr_to_sequence(self, attr: CellAttr) -> bytes:
        parts = []
        
        if not attr.bold and not attr.italic and not attr.underline:
            parts.append(0)
        if attr.bold:
            parts.append(1)
        if attr.italic:
            parts.append(3)
        if attr.underline:
            parts.append(4)
        parts.append(attr.fg)
        parts.append(attr.bg)
        
        return b'\x1b[' + b';'.join(str(p).encode() for p in parts) + b'm'

    def get_cursor(self) -> tuple[int, int]:
        return self.cursor.x, self.cursor.y

    def get_scrollback(self, n: int = 100) -> list[str]:
        lines = []
        start = max(0, len(self.scrollback) - n)
        for row in self.scrollback[start:]:
            line = ''.join(cell.char for cell in row)
            lines.append(line.rstrip())
        return lines
