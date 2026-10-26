"""Input backends: mouse and keyboard on the resident host, one per platform."""

from __future__ import annotations

from .base import LocalInputBackend
from .quartz import QuartzInput
from .win32 import Win32Input
from .xdotool import XdotoolInput
from .ydotool import KwinYdotoolInput, WlrootsYdotoolInput, YdotoolInput

__all__ = ["KwinYdotoolInput", "LocalInputBackend", "QuartzInput",
           "Win32Input", "WlrootsYdotoolInput", "XdotoolInput", "YdotoolInput"]

