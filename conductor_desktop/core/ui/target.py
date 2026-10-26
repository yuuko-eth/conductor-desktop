"""Choosing the window a ``title`` argument names, shared by the backends."""

from __future__ import annotations


def _norm(text: str) -> str:
    """Case-folded, with runs of whitespace collapsed to one space.

    The whitespace part matters because the title a caller passes usually
    came from ``list_windows``, and the two sources don't always agree
    character for character: on KDE, kwin collapses a window caption that
    AT-SPI reports verbatim, so Kate's window is ``"notes.txt — Kate"`` to
    one and ``"notes.txt  — Kate"`` to the other. A person retyping a title
    wouldn't reproduce the double space either.
    """
    return " ".join(text.split()).casefold()


def pick_window(windows: list[tuple[str, str, bool]], title: str | None) -> int | None:
    """Index into ``(window_title, app_name, main)`` for ``title``, front to back.

    An exact (case-insensitive) window title matches any window, so a named
    dialog can be targeted. App-name and substring matches prefer main windows
    (not dialogs, popups or tool windows): with Notepad's File menu open, its
    popup ("PopupHost") is the frontmost Notepad window and would win otherwise.
    """
    if not title:
        return None
    needle = _norm(title)
    for test in (
        lambda t, a, main: _norm(t) == needle,
        lambda t, a, main: main and bool(a) and needle in _norm(a),
        lambda t, a, main: main and needle in _norm(t),
        lambda t, a, main: bool(a) and needle in _norm(a),
        lambda t, a, main: needle in _norm(t),
    ):
        for i, (t, a, main) in enumerate(windows):
            if test(t, a, main):
                return i
    return None
