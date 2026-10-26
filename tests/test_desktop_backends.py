"""Desktop backends declare what they support, and the platform table routes
every platform. A backend whose ``caps`` and methods disagree would either
hide a working tool or offer one that always fails."""

from __future__ import annotations

import dataclasses
import importlib

import pytest

from conductor_desktop.core.backends.desktop import CAPABILITY_METHODS, Capabilities, DesktopBackend
from conductor_desktop.core.executor.platforms import PLATFORMS
from conductor_desktop.core.platform_detect import Detection, Platform

BACKENDS = sorted({b.desktop for b in PLATFORMS.values() if b.desktop})


def _load(spec: str) -> type[DesktopBackend]:
    module, cls = spec.split(":")
    try:
        mod = importlib.import_module(f"conductor_desktop.core.backends.{module}")
    except ImportError as e:
        pytest.skip(f"{module} doesn't import here: {e}")
    return getattr(mod, cls)


def test_every_capability_names_its_methods():
    assert set(CAPABILITY_METHODS) == {f.name for f in dataclasses.fields(Capabilities)}


@pytest.mark.parametrize("spec", BACKENDS)
def test_caps_match_the_implemented_methods(spec):
    backend = _load(spec)
    for cap, methods in CAPABILITY_METHODS.items():
        for method in methods:
            implemented = getattr(backend, method) is not getattr(DesktopBackend, method)
            assert implemented == getattr(backend.caps, cap), (
                f"{backend.__name__}: caps.{cap}={getattr(backend.caps, cap)} "
                f"but {method} is {'' if implemented else 'not '}implemented")


@pytest.mark.parametrize("spec", ["linux_gnome:GnomeDesktop", "linux_kde:KdeDesktop",
                                  "linux_wlroots:WlrootsDesktop"])
def test_wayland_backends_keep_the_atspi_tools(spec):
    # A lint autofix once deleted these re-exports as unused imports, and the
    # ui_* tools went quiet on GNOME Wayland and wlroots.
    backend = _load(spec)
    assert backend.caps.ui
    assert backend.ui_walk is not DesktopBackend.ui_walk
    assert backend.ui_act is not DesktopBackend.ui_act


def test_every_platform_is_routed():
    assert set(PLATFORMS) == set(Platform)


async def test_an_unloaded_backend_says_so():
    from conductor_desktop.core.errors import BackendError

    with pytest.raises(BackendError, match="not available on this host"):
        await DesktopBackend().list_windows()


async def test_connect_on_an_unknown_platform_reports_no_input(monkeypatch):
    from conductor_desktop.core.executor import ResidentExecutor, resident
    from conductor_desktop.core.models.host_node import HostNode

    async def screenshot_ok(platform):
        return True, "OK"

    monkeypatch.setattr(resident, "detect", lambda _: Detection(Platform.UNKNOWN, "test"))
    monkeypatch.setattr(resident, "_test_screenshot_permissions", screenshot_ok)
    ex = ResidentExecutor(HostNode(uuid="h", label="t", host_type="resident"))
    await ex.connect()
    assert not ex.permissions.input_works
    assert ex.permissions.input_detail == "no input backend for unknown"
    assert not ex.permissions.semantic_works
    assert not (await ex.click(1, 1)).success


async def test_connect_honors_a_requested_platform_and_records_the_route(monkeypatch):
    from conductor_desktop.core.executor import ResidentExecutor, resident
    from conductor_desktop.core.models.host_node import HostNode

    async def screenshot_ok(platform):
        return True, "OK"

    monkeypatch.setattr(resident, "_test_screenshot_permissions", screenshot_ok)
    host = HostNode(uuid="h", label="t", host_type="resident", platform="unknown")
    await ResidentExecutor(host).connect()
    assert host.platform == "unknown"
    assert host.platform_reason == "CONDUCTOR_DESKTOP_PLATFORM=unknown"


@pytest.mark.parametrize("spec", sorted({b.input for b in PLATFORMS.values() if b.input}))
def test_every_input_spec_names_an_input_backend(spec):
    from conductor_desktop.core.executor.input import LocalInputBackend
    from conductor_desktop.core.executor.platforms import _load

    try:
        backend = _load(".input", spec)
    except ModuleNotFoundError as e:  # left out of this build (Haiku's in the public one)
        pytest.skip(str(e))
    assert issubclass(backend, LocalInputBackend)


async def test_connect_without_the_platform_modules_turns_the_tools_off(monkeypatch):
    # The public release leaves Haiku's modules out; a Haiku host must still
    # connect, with the pointer, keyboard and window tools off.
    from conductor_desktop.core.executor import ResidentExecutor, resident
    from conductor_desktop.core.executor.platforms import PlatformBackends
    from conductor_desktop.core.models.host_node import HostNode

    async def screenshot_ok(platform):
        return True, "OK"

    monkeypatch.setattr(resident, "detect", lambda _: Detection(Platform.HAIKU, "test"))
    monkeypatch.setitem(resident.PLATFORMS, Platform.HAIKU,
                        PlatformBackends("not_shipped:Desktop", "not_shipped:Input"))
    monkeypatch.setattr(resident, "_test_screenshot_permissions", screenshot_ok)
    ex = ResidentExecutor(HostNode(uuid="h", label="t", host_type="resident"))
    await ex.connect()
    assert ex.permissions.input_detail == "no input backend for haiku"
    assert not ex.permissions.semantic_works
    assert not (await ex.click(1, 1)).success
