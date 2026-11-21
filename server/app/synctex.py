"""Read SyncTeX files (.synctex or .synctex.gz) without the synctex program,
which the desktop builds do not ship.

The file lists the inputs (`Input:<tag>:<name>`) and, per page (`{n` ... `}n`),
nested boxes and points that each carry the input tag and source line:
`[` / `]` vbox, `(` / `)` hbox, `v` / `h` void boxes (`x,y:W,H,D`), and the
points `k` (kern, `x,y:W`), `g` (glue), `$` (math), `x` (current), `x,y` only.
Positions are in scaled points from TeX's origin, one inch from the top left;
`X Offset` / `Y Offset` hold that inch, `Unit` and `Magnification` scale it.

Coordinates in and out of this module are PDF points from the page's top left.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from pathlib import Path

_SP_PER_BP = 65781.76  # 1 bp = 72.27 / 72 pt, 1 pt = 65536 sp
_BOXES = "[(vh"
_POINTS = "kg$x"


@dataclass
class Node:
    kind: str  # one of [ ( v h k g $ x
    tag: int
    line: int
    x: float
    y: float  # baseline
    w: float = 0.0
    h: float = 0.0
    d: float = 0.0
    children: list[Node] = field(default_factory=list)

    @property
    def box(self) -> bool:
        return self.kind in _BOXES

    def span(self) -> tuple[float, float]:
        """Left and right: a box runs right from x; a kern is recorded where it
        ends, so it runs from x - w (either way, as w may be negative)."""
        ends = (self.x - self.w, self.x) if self.kind == "k" else (self.x, self.x + self.w)
        return min(ends), max(ends)

    def contains(self, x: float, y: float) -> bool:
        left, right = self.span()
        return left <= x <= right and self.y - self.h <= y <= self.y + self.d


@dataclass
class SyncTeX:
    inputs: dict[int, str]
    pages: dict[int, list[Node]]  # top-level nodes per page

    # ---- reading ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> SyncTeX:
        raw = path.read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return cls.parse(raw.decode("utf-8", errors="replace"))

    @classmethod
    def parse(cls, text: str) -> SyncTeX:
        inputs: dict[int, str] = {}
        pages: dict[int, list[Node]] = {}
        unit, mag, x_off, y_off = 1.0, 1000.0, 0.0, 0.0
        page: int | None = None
        stack: list[Node] = []
        in_content = False

        def scale(value: float) -> float:
            return value * unit * mag / 1000.0 / _SP_PER_BP

        for line in text.splitlines():
            if not line:
                continue
            if line.startswith("Input:"):
                tag, _, name = line[6:].partition(":")
                if tag.isdigit():
                    inputs[int(tag)] = name
                continue
            if not in_content:
                key, _, value = line.partition(":")
                try:
                    if key == "Unit":
                        unit = float(value)
                    elif key == "Magnification":
                        mag = float(value) or 1000.0
                    elif key == "X Offset":
                        x_off = float(value)
                    elif key == "Y Offset":
                        y_off = float(value)
                    elif key == "Content":
                        in_content = True
                except ValueError:
                    pass
                continue
            head = line[0]
            if head == "{":
                try:
                    page = int(line[1:])
                except ValueError:
                    page = None
                stack = []
                if page is not None:
                    pages.setdefault(page, [])
                continue
            if head == "}":
                page, stack = None, []
                continue
            if page is None:
                continue
            if head in "])":
                if stack:
                    stack.pop()
                continue
            if head not in _BOXES and head not in _POINTS:
                continue
            node = _record(head, line[1:], scale, x_off, y_off)
            if node is None:
                continue
            (stack[-1].children if stack else pages[page]).append(node)
            if head in "[(":
                stack.append(node)
        return cls(inputs, pages)

    # ---- queries ---------------------------------------------------------

    def tags_for(self, matches) -> set[int]:  # noqa: ANN001 - a predicate on input names
        return {tag for tag, name in self.inputs.items() if matches(name)}

    def forward(self, tags: set[int], line: int) -> tuple[int, float, float] | None:
        """(page, x, y) where `line` of the given inputs starts on the page.

        Records of the line are grouped by the hbox (a typeset line) holding
        them, and the box with most of them wins, first one on ties: TeX also
        files stray records under a line (a kern ending the previous line, an
        indent box), and these sit alone. The answer is the first record of the
        line in that box; a kern records where it ends, so its start is x - w.
        A line with no records (preamble, comment) falls to the next line that
        has some, else the previous one."""
        found: dict[int, list[tuple[int, Node, Node | None]]] = {}
        for page in sorted(self.pages):
            for node, parent in _walk_parents(self.pages[page], None):
                if node.tag in tags and node.line > 0 and node.kind != "[":
                    found.setdefault(node.line, []).append((page, node, parent))
        if not found:
            return None
        later = [n for n in found if n >= line]
        hits = found[min(later)] if later else found[max(found)]
        groups: dict[int, list[tuple[int, Node, Node | None]]] = {}
        for hit in hits:
            groups.setdefault(id(hit[2]) if hit[2] is not None else id(hit[1]), []).append(hit)
        best = max(groups.values(), key=len)  # max keeps the first of equals
        page, node, _ = best[0]
        return page, node.span()[0] if node.kind == "k" else node.x, node.y

    def inverse(self, page: int, x: float, y: float) -> tuple[str, int] | None:
        """(input name, line) for a point: the innermost hbox around it, then the
        record in that box nearest the point along the line."""
        nodes = self.pages.get(page)
        if not nodes:
            return None
        box = _innermost(nodes, x, y)
        if box is not None:
            pool = [c for c in box.children if c.line > 0] or [box]
            pick = min(pool, key=lambda n: (_dist(n, x, y), 0 if n.kind in _POINTS else 1))
        else:
            pool = [n for n in _walk(nodes) if n.line > 0 and n.kind != "["]
            if not pool:
                return None
            pick = min(pool, key=lambda n: _dist(n, x, y))
        name = self.inputs.get(pick.tag)
        return (name, pick.line) if name is not None else None


def _record(kind: str, body: str, scale, x_off: float, y_off: float) -> Node | None:  # noqa: ANN001
    try:
        where, _, rest = body.partition(":")
        tag, line = (int(v) for v in where.split(","))
        parts = rest.split(":")
        x, y = (float(v) for v in parts[0].split(","))
        size = [float(v) for v in parts[1].split(",")] if len(parts) > 1 and parts[1] else []
    except ValueError:
        return None
    node = Node(kind, tag, line, scale(x + x_off), scale(y + y_off))
    if kind == "k" and size:
        node.w = scale(size[0])
    elif kind in _BOXES and len(size) >= 3:
        node.w, node.h, node.d = scale(size[0]), scale(size[1]), scale(size[2])
    return node


def _walk(nodes: list[Node]):  # noqa: ANN202
    for node in nodes:
        yield node
        if node.children:
            yield from _walk(node.children)


def _walk_parents(nodes: list[Node], parent: Node | None):  # noqa: ANN202
    for node in nodes:
        yield node, parent
        if node.children:
            yield from _walk_parents(node.children, node)


def _innermost(nodes: list[Node], x: float, y: float) -> Node | None:
    """The deepest hbox containing the point, searching through vboxes."""
    found: Node | None = None
    for node in nodes:
        if node.kind in "[(" and node.contains(x, y):
            inner = _innermost(node.children, x, y)
            if inner is not None:
                found = inner
            elif node.kind == "(":
                found = node
    return found


def _dist(node: Node, x: float, y: float) -> float:
    """Distance from the point, mostly along the line: a vertical step costs more
    than a horizontal one, so a click picks a record on its own line."""
    if node.box and node.contains(x, y):
        return 0.0
    if node.box or node.kind == "k":
        # For a kern, x..x+w: what matches the synctex program's answers on real
        # builds best (612 of 616 test points, against 608 for x-w..x).
        left, right = node.span() if node.box else sorted((node.x, node.x + node.w))
        dx = max(left - x, 0.0, x - right)
        dy = max(node.y - node.h - y, 0.0, y - node.y - node.d)
    else:
        dx, dy = abs(node.x - x), abs(node.y - y)
    return dx + 4 * dy
