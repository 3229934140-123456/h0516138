from dataclasses import dataclass, field
from typing import Optional, Union
from abc import ABC, abstractmethod
from enum import Enum


class LayoutType(Enum):
    SINGLE = "single"
    HORIZONTAL_SPLIT = "horizontal_split"
    VERTICAL_SPLIT = "vertical_split"
    TILED = "tiled"
    MAIN_HORIZONTAL = "main_horizontal"
    MAIN_VERTICAL = "main_vertical"


@dataclass
class Rect:
    x: int
    y: int
    width: int
    height: int

    def contains_point(self, px: int, py: int) -> bool:
        return (self.x <= px < self.x + self.width and 
                self.y <= py < self.y + self.height)

    def intersects(self, other: "Rect") -> bool:
        return not (self.x + self.width <= other.x or
                    other.x + other.width <= self.x or
                    self.y + self.height <= other.y or
                    other.y + other.height <= self.y)


@dataclass
class LayoutNode(ABC):
    rect: Rect
    parent: Optional["ContainerNode"] = None

    @abstractmethod
    def get_all_panes(self) -> list["PaneNode"]:
        pass

    @abstractmethod
    def find_pane_at(self, x: int, y: int) -> Optional["PaneNode"]:
        pass

    @abstractmethod
    def resize(self, width: int, height: int) -> None:
        pass


@dataclass
class PaneNode(LayoutNode):
    pane_id: str = ""

    def get_all_panes(self) -> list["PaneNode"]:
        return [self]

    def find_pane_at(self, x: int, y: int) -> Optional["PaneNode"]:
        if self.rect.contains_point(x, y):
            return self
        return None

    def resize(self, width: int, height: int) -> None:
        self.rect.width = width
        self.rect.height = height


@dataclass
class ContainerNode(LayoutNode):
    layout_type: LayoutType = LayoutType.HORIZONTAL_SPLIT
    children: list[LayoutNode] = field(default_factory=list)
    ratios: list[float] = field(default_factory=list)

    def add_child(self, child: LayoutNode, ratio: float) -> None:
        child.parent = self
        self.children.append(child)
        self.ratios.append(ratio)
        self._normalize_ratios()
        self._layout_children()

    def remove_child(self, child: LayoutNode) -> None:
        if child in self.children:
            idx = self.children.index(child)
            self.children.pop(idx)
            self.ratios.pop(idx)
            child.parent = None
            self._normalize_ratios()
            self._layout_children()

    def replace_child(self, old_child: LayoutNode, new_child: LayoutNode) -> None:
        if old_child in self.children:
            idx = self.children.index(old_child)
            old_child.parent = None
            self.children[idx] = new_child
            new_child.parent = self
            self._layout_children()

    def _normalize_ratios(self) -> None:
        if not self.ratios:
            return
        total = sum(self.ratios)
        if total > 0:
            self.ratios = [r / total for r in self.ratios]

    def _layout_children(self) -> None:
        if not self.children:
            return

        x, y = self.rect.x, self.rect.y
        width, height = self.rect.width, self.rect.height

        if len(self.children) == 1:
            self.children[0].resize(width, height)
            self.children[0].rect.x = x
            self.children[0].rect.y = y
            return

        if self.layout_type == LayoutType.HORIZONTAL_SPLIT:
            current_y = y
            for i, child in enumerate(self.children):
                child_height = int(height * self.ratios[i])
                if i == len(self.children) - 1:
                    child_height = height - (current_y - y)
                child.resize(width, child_height)
                child.rect.x = x
                child.rect.y = current_y
                current_y += child_height

        elif self.layout_type == LayoutType.VERTICAL_SPLIT:
            current_x = x
            for i, child in enumerate(self.children):
                child_width = int(width * self.ratios[i])
                if i == len(self.children) - 1:
                    child_width = width - (current_x - x)
                child.resize(child_width, height)
                child.rect.x = current_x
                child.rect.y = y
                current_x += child_width

    def get_all_panes(self) -> list[PaneNode]:
        panes = []
        for child in self.children:
            panes.extend(child.get_all_panes())
        return panes

    def find_pane_at(self, x: int, y: int) -> Optional[PaneNode]:
        if not self.rect.contains_point(x, y):
            return None
        for child in self.children:
            pane = child.find_pane_at(x, y)
            if pane:
                return pane
        return None

    def resize(self, width: int, height: int) -> None:
        self.rect.width = width
        self.rect.height = height
        self._layout_children()


class LayoutManager:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.root: LayoutNode = PaneNode(Rect(0, 0, width, height), pane_id="")
        self.pane_nodes: dict[str, PaneNode] = {}

    def set_initial_pane(self, pane_id: str) -> None:
        self.root = PaneNode(Rect(0, 0, self.width, self.height), pane_id=pane_id)
        self.pane_nodes = {pane_id: self.root}

    def split_horizontal(self, pane_id: str, new_pane_id: str, 
                         ratio: float = 0.5) -> Rect:
        return self._split(pane_id, new_pane_id, 
                          LayoutType.HORIZONTAL_SPLIT, ratio)

    def split_vertical(self, pane_id: str, new_pane_id: str, 
                       ratio: float = 0.5) -> Rect:
        return self._split(pane_id, new_pane_id, 
                          LayoutType.VERTICAL_SPLIT, ratio)

    def _split(self, pane_id: str, new_pane_id: str, 
               layout_type: LayoutType, ratio: float) -> Rect:
        if pane_id not in self.pane_nodes:
            raise ValueError(f"Pane {pane_id} not found")

        pane_node = self.pane_nodes[pane_id]
        parent = pane_node.parent

        pane_rect = pane_node.rect
        other_ratio = 1.0 - ratio

        if layout_type == LayoutType.HORIZONTAL_SPLIT:
            new_height = int(pane_rect.height * ratio)
            remaining_height = pane_rect.height - new_height
            
            new_rect = Rect(pane_rect.x, pane_rect.y, pane_rect.width, new_height)
            pane_rect = Rect(pane_rect.x, pane_rect.y + new_height, 
                            pane_rect.width, remaining_height)
        else:
            new_width = int(pane_rect.width * ratio)
            remaining_width = pane_rect.width - new_width
            
            new_rect = Rect(pane_rect.x, pane_rect.y, new_width, pane_rect.height)
            pane_rect = Rect(pane_rect.x + new_width, pane_rect.y, 
                            remaining_width, pane_rect.height)

        container = ContainerNode(
            rect=pane_node.rect,
            layout_type=layout_type,
            ratios=[ratio, other_ratio]
        )

        new_pane = PaneNode(new_rect, pane_id=new_pane_id)
        pane_node.rect = pane_rect

        container.children = [new_pane, pane_node]
        new_pane.parent = container
        pane_node.parent = container

        if parent:
            parent.replace_child(pane_node, container)
        else:
            self.root = container

        self.pane_nodes[new_pane_id] = new_pane
        container._layout_children()

        return new_rect

    def remove_pane(self, pane_id: str) -> Optional[str]:
        if pane_id not in self.pane_nodes:
            return None

        pane_node = self.pane_nodes[pane_id]
        parent = pane_node.parent

        del self.pane_nodes[pane_id]

        if parent is None:
            if len(self.pane_nodes) > 0:
                remaining_id = next(iter(self.pane_nodes.keys()))
                remaining_node = self.pane_nodes[remaining_id]
                remaining_node.rect = Rect(0, 0, self.width, self.height)
                self.root = remaining_node
                return remaining_id
            return None

        parent.remove_child(pane_node)

        if len(parent.children) == 1:
            remaining_child = parent.children[0]
            remaining_child.rect = parent.rect
            
            grandparent = parent.parent
            if grandparent:
                grandparent.replace_child(parent, remaining_child)
            else:
                self.root = remaining_child
                remaining_child.parent = None

        if self.pane_nodes:
            return next(iter(self.pane_nodes.keys()))
        return None

    def get_pane_rect(self, pane_id: str) -> Optional[Rect]:
        if pane_id in self.pane_nodes:
            return self.pane_nodes[pane_id].rect
        return None

    def find_pane_at(self, x: int, y: int) -> Optional[str]:
        pane = self.root.find_pane_at(x, y)
        return pane.pane_id if pane else None

    def get_all_pane_rects(self) -> dict[str, Rect]:
        return {pid: pnode.rect for pid, pnode in self.pane_nodes.items()}

    def resize_screen(self, new_width: int, new_height: int) -> dict[str, Rect]:
        self.width = new_width
        self.height = new_height
        self.root.resize(new_width, new_height)
        return self.get_all_pane_rects()

    def get_pane_geometry(self, pane_id: str) -> Optional[dict]:
        rect = self.get_pane_rect(pane_id)
        if rect:
            return {
                "x": rect.x,
                "y": rect.y,
                "width": rect.width,
                "height": rect.height,
                "cols": rect.width,
                "rows": rect.height
            }
        return None

    def iterate_layout(self) -> list[tuple[str, Rect, bool]]:
        result = []
        panes = self.root.get_all_panes()
        for pane in panes:
            is_focused = False
            result.append((pane.pane_id, pane.rect, is_focused))
        return result
