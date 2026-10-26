"""Command-line helpers for the Linux / FreeBSD backends: running a tool, and
the clipboard through xclip (X11) or wl-clipboard (Wayland)."""

from __future__ import annotations

import asyncio

from ..errors import BackendError, reason


async def run(cmd: list[str], timeout: float = 5) -> str:
    """``cmd``'s stdout. Raises BackendError when it's missing, times out or
    exits non-zero (with its stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise BackendError(f"{cmd[0]} is not installed (not on PATH)") from None
    except OSError as e:
        raise BackendError(f"could not run {cmd[0]}: {reason(e)}") from e
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise BackendError(f"{cmd[0]} timed out after {timeout:g}s") from None
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise BackendError(f"{cmd[0]} failed (rc={proc.returncode}): {err}")
    return stdout.decode("utf-8", errors="replace")


async def run_ok(cmd: list[str], timeout: float = 5) -> bool:
    """Whether ``cmd`` exits 0, for tools whose non-zero exit means "no match"
    (``wmctrl -a``). A missing tool or a timeout still raises."""
    try:
        await run(cmd, timeout)
    except BackendError as e:
        if "failed (rc=" in str(e):
            return False
        raise
    return True


# What xclip and wl-paste print when the clipboard simply holds no text.
_EMPTY_CLIPBOARD = ("target STRING not available", "Nothing is copied", "No selection")


async def _read_clipboard(cmd: list[str]) -> str:
    try:
        return await run(cmd, timeout=3)
    except BackendError as e:
        if any(marker in str(e) for marker in _EMPTY_CLIPBOARD):
            return ""
        raise


async def _write_clipboard(cmd: list[str], text: str) -> None:
    """Hand ``text`` to xclip / wl-copy. Both fork a child that keeps serving
    the selection and inherits stdout/stderr, so those must not be pipes:
    reading them would wait for the child, i.e. until the timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(text.encode("utf-8")), timeout=3)
    except FileNotFoundError:
        raise BackendError(f"{cmd[0]} is not installed (not on PATH)") from None
    except (OSError, TimeoutError) as e:
        raise BackendError(f"{cmd[0]} failed: {reason(e)}") from e
    if proc.returncode != 0:
        raise BackendError(f"{cmd[0]} failed (rc={proc.returncode})")


async def xclip_get() -> str:
    return await _read_clipboard(["xclip", "-selection", "clipboard", "-o"])


async def xclip_set(text: str) -> None:
    await _write_clipboard(["xclip", "-selection", "clipboard"], text)


async def wl_get() -> str:
    return await _read_clipboard(["wl-paste", "--no-newline"])


async def wl_set(text: str) -> None:
    await _write_clipboard(["wl-copy"], text)


async def wl_or_x_get() -> str:
    """wl-clipboard, falling back to xclip on XWayland."""
    try:
        return await wl_get()
    except BackendError as wl_err:
        try:
            return await xclip_get()
        except BackendError as x_err:
            raise BackendError(f"{wl_err}; {x_err}") from None


async def wl_or_x_set(text: str) -> None:
    try:
        await wl_set(text)
    except BackendError as wl_err:
        try:
            await xclip_set(text)
        except BackendError as x_err:
            raise BackendError(f"{wl_err}; {x_err}") from None
