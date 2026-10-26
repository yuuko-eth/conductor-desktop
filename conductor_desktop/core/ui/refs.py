"""Model-facing refs (``e14``) mapped to server-side locators.

The model only ever sees the short id. The locator holds what resolution
needs: pid, a stable native window handle, the native child-index path and
the element's identity. The window title rides along as a label and is not
part of the key, because titles change (Terminal follows the cwd, editors add
"— Edited").
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from .model import UINode, iter_nodes


@dataclass(frozen=True)
class Locator:
    pid: int
    window_id: int | str | None  # CGWindowID / HWND / AT-SPI bus name + object path
    root: str                  # "window" | "menubar"
    path: tuple[int, ...]
    native_role: str
    name: str
    id: str
    window_title: str = field(default="", compare=False)


class RefTable:
    """Refs for the last ``reads_per_window`` reads of up to ``max_windows`` windows.

    Re-reading the same element yields the same ref, so an agent's refs stay
    valid across reads until the element moves or is evicted.
    """

    def __init__(self, reads_per_window: int = 5, max_windows: int = 32) -> None:
        self.reads_per_window = reads_per_window
        self.max_windows = max_windows
        self._next = 0
        self._locators: dict[str, Locator] = {}
        self._by_locator: dict[Locator, str] = {}
        self._uses: dict[str, int] = {}
        self._reads: OrderedDict[tuple, deque[set[str]]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._locators)

    def get(self, ref: str) -> Locator | None:
        return self._locators.get(ref)

    def assign(
        self,
        roots: list[tuple[str, UINode]],
        *,
        pid: int,
        window_id: int | str | None,
        window_title: str,
        also: Iterable[UINode] = (),
    ) -> int:
        """Give a ref to every actionable or named node, plus the ``also``
        nodes (search results the caller will show); returns how many."""
        extra = {id(node) for node in also}
        read: set[str] = set()
        for kind, root in roots:
            for node in iter_nodes(root):
                if not (node.actionable or node.name or id(node) in extra):
                    continue
                loc = Locator(pid, window_id, kind, node.path, node.native_role,
                              node.name, node.id, window_title)
                ref = self._by_locator.get(loc)
                if ref is None:
                    self._next += 1
                    ref = f"e{self._next}"
                    self._by_locator[loc] = ref
                    self._locators[ref] = loc
                node.ref = ref
                read.add(ref)

        key = (pid, window_id if window_id is not None else window_title)
        reads = self._reads.pop(key, None) or deque()
        self._reads[key] = reads
        reads.append(read)
        for ref in read:
            self._uses[ref] = self._uses.get(ref, 0) + 1
        while len(reads) > self.reads_per_window:
            self._release(reads.popleft())
        while len(self._reads) > self.max_windows:
            _, old = self._reads.popitem(last=False)
            for r in old:
                self._release(r)
        return len(read)

    def relocate(self, ref: str, path: tuple[int, ...]) -> None:
        """Point ``ref`` at the element's new path after resolution found it moved."""
        loc = self._locators.get(ref)
        if loc is None or loc.path == path:
            return
        if self._by_locator.get(loc) == ref:
            del self._by_locator[loc]
        new = replace(loc, path=path)
        self._locators[ref] = new
        self._by_locator[new] = ref

    def forget_window(self, pid: int, window_id: int | str | None, window_title: str) -> None:
        """Drop every ref of a window that has closed."""
        reads = self._reads.pop((pid, window_id if window_id is not None else window_title), None)
        for read in reads or ():
            self._release(read)

    def _release(self, refs: set[str]) -> None:
        for ref in refs:
            self._uses[ref] -= 1
            if self._uses[ref] == 0:
                del self._uses[ref]
                loc = self._locators.pop(ref)
                if self._by_locator.get(loc) == ref:
                    del self._by_locator[loc]
