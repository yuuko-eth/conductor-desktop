"""HostExecutor — unified interface for observing and acting on any host type.

Each host type implements this protocol; tool handlers call executor
methods without knowing the host type.

Architecture:
  - ResidentExecutor: direct backend calls (screenshot via mss/grim, input via
    xdotool/ydotool/CGEvent/SendInput, semantic ops via host agent backends)
    Includes permission self-test on connect() to detect TCC/uinput issues.
  - Phones (adb / WebDriverAgent) are not HostExecutors: AndroidExecutor and
    IOSExecutor in core/backends/mobile.py serve the mobile_* tools.
  - VNC / RDP / VirtualBox hosts: not implemented
"""

from __future__ import annotations

from .base import ActionResult, HostExecutor, PermissionReport, resize_screenshot_b64
from .input import (
    LocalInputBackend,
    QuartzInput,
    Win32Input,
    XdotoolInput,
    YdotoolInput,
)
from .resident import ResidentExecutor, create_executor

__all__ = [
    "ActionResult", "HostExecutor", "LocalInputBackend", "PermissionReport",
    "QuartzInput", "ResidentExecutor", "Win32Input", "XdotoolInput", "YdotoolInput",
    "create_executor", "resize_screenshot_b64",
]
