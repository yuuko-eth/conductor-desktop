"""Parser + config + registration-gating checks for the optional OmniParser
enumeration backend.

No real OmniParser server is contacted — we exercise `_parse_content` against
canned `parsed_content` payloads, the config plumbing, and the env-gated tool
registration (the tool must be ABSENT unless CONDUCTOR_DESKTOP_OMNIPARSER_URL is
set).
"""

from __future__ import annotations

import asyncio
import os
from unittest import mock

import httpx

from conductor_desktop.enumerate import EnumeratedElement, Enumerator, _parse_content

# --- _parse_content -------------------------------------------------------

def test_parse_content_maps_fields():
    sample = [
        {"type": "text", "bbox": [10, 20, 110, 40],
         "interactivity": False, "content": "File"},
        {"type": "icon", "bbox": [200, 300, 232, 332],
         "interactivity": True, "content": "settings gear"},
    ]
    out = _parse_content(sample)
    assert out == [
        EnumeratedElement(bbox=(10, 20, 110, 40), kind="text",
                          interactive=False, content="File"),
        EnumeratedElement(bbox=(200, 300, 232, 332), kind="icon",
                          interactive=True, content="settings gear"),
    ]


def test_parse_content_drops_degenerate_and_malformed():
    sample = [
        {"type": "icon", "bbox": [0, 0, 0, 0], "interactivity": True,
         "content": None},          # degenerate -> dropped
        {"type": "icon", "bbox": [5, 5, 3, 9], "interactivity": True},  # inverted x
        {"bbox": [1, 2, 3]},        # wrong length -> dropped
        "garbage",                  # not a dict -> dropped
        {"type": "text", "bbox": [1, 2, 3, 4], "interactivity": 1,
         "content": "  keep me  "},
    ]
    out = _parse_content(sample)
    assert len(out) == 1
    assert out[0].bbox == (1, 2, 3, 4)
    assert out[0].interactive is True       # truthy int coerced to bool
    assert out[0].content == "keep me"      # stripped


def test_parse_content_float_bbox_rounds():
    out = _parse_content([{"type": "icon", "bbox": [10.4, 20.6, 30.2, 40.9],
                           "interactivity": True, "content": ""}])
    assert out[0].bbox == (10, 21, 30, 41)


def test_parse_content_scales_normalized_ratio_to_pixels():
    """Some OmniParser deploys emit bbox as 0..1 ratio; with image dims
    known, those scale back to pixels. (Pixel-unit deploys have coords > 1 and
    pass through unscaled.)"""
    sample = [{"type": "icon", "bbox": [0.1, 0.2, 0.5, 0.6],
               "interactivity": True, "content": "x"}]
    out = _parse_content(sample, 1280, 800)
    assert out[0].bbox == (128, 160, 640, 480)


def test_parse_content_ratio_unscaled_without_dims():
    """No dims → can't scale; a sub-1 box collapses to degenerate and is
    dropped rather than silently mis-placed at the origin."""
    sample = [{"type": "icon", "bbox": [0.1, 0.2, 0.5, 0.6],
               "interactivity": True, "content": "x"}]
    assert _parse_content(sample) == []


def test_parse_content_drops_inverted_ratio_box():
    """An inverted ratio box (y2 < y1, as OmniParser occasionally emits) is
    dropped after scaling — same degeneracy guard as the pixel path."""
    sample = [{"type": "text", "bbox": [0.37, 0.107, 0.62, 0.097],
               "interactivity": False, "content": "noise"}]
    assert _parse_content(sample, 1280, 800) == []


def test_parse_content_non_list_is_empty():
    assert _parse_content(None) == []
    assert _parse_content({"detected": []}) == []


# --- config plumbing ------------------------------------------------------

def test_config_omniparser_disabled_by_default():
    from conductor_desktop.config import MCPConfig
    env = {"CONDUCTOR_DESKTOP_GROUNDER_URL": "http://g:8000"}
    with mock.patch.dict(os.environ, env, clear=True):
        c = MCPConfig.from_env()
    assert c.omniparser_url == ""
    assert c.omniparser_enabled is False


def test_config_omniparser_from_env():
    from conductor_desktop.config import MCPConfig
    env = {
        "CONDUCTOR_DESKTOP_GROUNDER_URL": "http://g:8000",
        "CONDUCTOR_DESKTOP_OMNIPARSER_URL": "http://p:8918/",
        "CONDUCTOR_DESKTOP_OMNIPARSER_TIMEOUT": "45",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        c = MCPConfig.from_env()
    assert c.omniparser_url == "http://p:8918"   # trailing slash stripped
    assert c.omniparser_enabled is True
    assert c.omniparser_timeout == 45.0


# --- os/theme param forwarding (icon labeling) ----------------------------

def _enumerator_capturing(seen: dict) -> Enumerator:
    """An Enumerator whose POST is intercepted; records query params in `seen`."""
    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"parsed_content": []})
    en = Enumerator("http://p:8918")
    en._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return en


def test_parse_forwards_os_and_theme():
    seen: dict = {}
    en = _enumerator_capturing(seen)
    asyncio.run(en.parse("", os_id="linux-gnome", theme="yaru-blue"))
    asyncio.run(en.close())
    assert seen["params"]["os"] == "linux-gnome"
    assert seen["params"]["theme"] == "yaru-blue"


def test_parse_omits_os_theme_when_none():
    seen: dict = {}
    en = _enumerator_capturing(seen)
    asyncio.run(en.parse(""))                 # no os/theme → labeling stays off
    asyncio.run(en.close())
    assert "os" not in seen["params"]
    assert "theme" not in seen["params"]


# --- detect_icon_env (DE + icon theme, not display server) ----------------

def test_detect_icon_env_macos():
    from conductor_desktop.core import platform_detect as pd
    pd.detect_icon_env.cache_clear()
    with mock.patch.object(pd.platform, "system", return_value="Darwin"), \
         mock.patch.object(pd, "_macos_appearance", return_value="dark"):
        assert pd.detect_icon_env() == ("macos", "dark")     # app icons agnostic,
                                                             #   SF symbols gated
    pd.detect_icon_env.cache_clear()


def test_detect_icon_env_gnome_on_x11_still_gnome():
    """GNOME-on-X11 must map to linux-gnome (icons are Yaru regardless of x11)."""
    from conductor_desktop.core import platform_detect as pd
    pd.detect_icon_env.cache_clear()
    with mock.patch.object(pd.platform, "system", return_value="Linux"), \
         mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
                                      "XDG_SESSION_TYPE": "x11"}, clear=True), \
         mock.patch.object(pd, "_gnome_icon_theme", return_value="yaru-blue"):
        assert pd.detect_icon_env() == ("linux-gnome", "yaru-blue")
    pd.detect_icon_env.cache_clear()


def test_detect_icon_env_windows_version():
    """Windows theme = OS version (win10/win11), the icon-style axis."""
    from conductor_desktop.core import platform_detect as pd
    for ver, want in [("10.0.19045", "win10"), ("10.0.22631", "win11")]:
        pd.detect_icon_env.cache_clear()
        with mock.patch.object(pd.platform, "system", return_value="Windows"), \
             mock.patch.object(pd.platform, "version", return_value=ver):
            assert pd.detect_icon_env() == ("windows", want)
    pd.detect_icon_env.cache_clear()


def test_detect_icon_env_kde_not_gnome():
    """KDE must NOT borrow GNOME's Yaru refs (the two detectors share one reading of the desktop name)."""
    from conductor_desktop.core import platform_detect as pd
    pd.detect_icon_env.cache_clear()
    with mock.patch.object(pd.platform, "system", return_value="Linux"), \
         mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}, clear=True), \
         mock.patch.object(pd, "_kde_icon_theme", return_value="breeze-dark"):
        assert pd.detect_icon_env() == ("linux-kde", "breeze-dark")
    pd.detect_icon_env.cache_clear()


# --- registration gating --------------------------------------------------

def test_tool_absent_without_a_parser(surface):
    assert "enumerate_elements" not in surface()


def test_tool_present_in_managed_mode_without_omniparser_url(surface):
    # Managed mode has no CONDUCTOR_DESKTOP_OMNIPARSER_URL — the gateway owns the
    # parser and serves /v1/enumerate — but the tool must still register.
    assert "enumerate_elements" in surface(grounder_url="https://gw.test",
                                           grounder_family="managed")


def test_tool_classified_observing(kind):
    assert kind("enumerate_elements") == "observing"
