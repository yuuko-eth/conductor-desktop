"""Data types for host agent responses."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WindowInfo:
    id: str
    title: str
    process_name: str
    pid: int
    bounds: tuple[int, int, int, int]  # x, y, width, height
    state: str  # normal | maximized | minimized | fullscreen
    desktop: int
    focused: bool


# ── StructuredProvider schema ─────────────────────────────────────────────
#
# Common output of any structured source (native a11y / app scripting). The
# router merges providers by capability: values from one, geometry from another,
# act from a third. See https://docs.conductor-mcp.io/concepts/structured-reads/ §2 (provider model).

@dataclass
class StructuredCapabilities:
    has_structure: bool = False   # rows/cols/cell topology recoverable
    has_values: bool = False      # cell text/value readable
    has_geometry: bool = False    # on-screen click bbox available
    can_act: bool = False         # settable/invokable without ground→click

    def to_dict(self) -> dict:
        return {
            "has_structure": self.has_structure,
            "has_values": self.has_values,
            "has_geometry": self.has_geometry,
            "can_act": self.can_act,
        }


@dataclass
class TableCell:
    r: int
    c: int
    bbox: tuple[int, int, int, int] | None  # None when off-screen geometry unavailable
    text: str
    can_set: bool = False  # this specific cell is writable (UIA Value(set) / editable)

    def to_dict(self) -> dict:
        return {
            "r": self.r,
            "c": self.c,
            "bbox": list(self.bbox) if self.bbox else None,
            "text": self.text,
            "can_set": self.can_set,
        }


# Failure states — the "blind vs absent" distinction (matrix §7). A provider
# returns one of these in `failure` instead of silently yielding 0 cells.
FAILURE_CANVAS_BLACKOUT = "canvas_blackout"        # custom-drawn grid, no a11y (Numbers)
FAILURE_A11Y_COLD = "a11y_cold"                    # tree not materialized until AT attaches (Electron)
FAILURE_ELEVATION_REQUIRED = "elevation_required"  # higher-IL window invisible un-elevated (Win Task Mgr)
FAILURE_NOT_IMPLEMENTED = "not_implemented"        # this platform's backend has no provider yet
# CV playbook ran but its structural assumption failed (decline → fall back)
FAILURE_LAYOUT_MISMATCH = "layout_mismatch"


@dataclass
class TableExtract:
    rows: int
    cols: int
    cells: list[TableCell]
    source: str                          # "ax" | "applescript" | "uia" | "atspi" | "none"
    capabilities: StructuredCapabilities
    failure: str | None = None           # one of FAILURE_* when blind, else None

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "cols": self.cols,
            "cells": [c.to_dict() for c in self.cells],
            "source": self.source,
            "capabilities": self.capabilities.to_dict(),
            "failure": self.failure,
        }
