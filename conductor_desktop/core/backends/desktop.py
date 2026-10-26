"""The desktop backend interface: windows, screens, clipboard and accessibility.

Each platform module (``linux_x11``, ``macos``, ...) keeps its functions at
module level and ends with one ``DesktopBackend`` subclass that names them and
declares what it supports in ``caps``. The executor asks ``caps`` instead of
probing the module, so a function that goes missing (an import a linter
removed) fails a test instead of quietly turning a tool off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from ..errors import BackendError

if TYPE_CHECKING:
    from ..protocols import TableExtract


@dataclass(frozen=True)
class Capabilities:
    """What a desktop backend supports. Each field covers the methods listed
    for it in ``CAPABILITY_METHODS``."""
    windows: bool = False
    screens: bool = False
    clipboard: bool = False
    ui: bool = False
    table: bool = False


CAPABILITY_METHODS: dict[str, tuple[str, ...]] = {
    "windows": ("list_windows", "focus_window"),
    "screens": ("list_screens",),
    "clipboard": ("get_clipboard", "set_clipboard"),
    "ui": ("ui_walk", "ui_act"),
    "table": ("extract_table",),
}


class DesktopBackend:
    """One platform's windows, screens, clipboard and accessibility tree.

    A subclass overrides exactly the methods its ``caps`` name. The defaults
    raise, so a capability the executor doesn't check still fails with a
    reason. Instantiated as is, this is the backend of a platform whose
    module didn't load: nothing is available.
    """

    name: ClassVar[str] = "none"
    caps: ClassVar[Capabilities] = Capabilities()

    def _unavailable(self, what: str) -> BackendError:
        return BackendError(f"{what} is not available on this host ({self.name} backend)")

    async def list_windows(self) -> list[dict]:
        raise self._unavailable("window listing")

    async def focus_window(self, title: str | None = None, pid: int | None = None) -> bool:
        """Raise the matching window. False means nothing matched."""
        raise self._unavailable("window focus")

    async def list_screens(self) -> list[dict]:
        raise self._unavailable("screen listing")

    async def get_clipboard(self) -> str:
        raise self._unavailable("the clipboard")

    async def set_clipboard(self, text: str) -> None:
        raise self._unavailable("the clipboard")

    async def ui_walk(self, title: str | None = None, depth: int = 8, max_children: int = 50,
                      include_menubar: bool = False, timeout: float = 10.0,
                      web_search: dict | None = None) -> Any:
        """Walk the target window into a ``ui.model.UIRead`` (None: no target)."""
        raise self._unavailable("the ui_* tools")

    async def ui_act(self, locator: Any, verb: str, params: dict, timeout: float = 5.0) -> Any:
        """Run ``verb`` on the element ``locator`` names; a ``ui.act`` outcome."""
        raise self._unavailable("the ui_* tools")

    async def extract_table(self, title: str | None = None, max_rows: int = 100) -> TableExtract:
        raise self._unavailable("table extraction")
