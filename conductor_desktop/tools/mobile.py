"""Mobile tools — Android via adb, iOS via WDA HTTP.

Action verbs are prefixed with ``mobile_`` so the model commits to "act
on a device" before reading args (auto-regressive safety property).
Perception tools (``capture_screen``, ``locate``, ``crop``, ``read_screen``,
``list_windows``) are NOT prefixed — they take an optional ``device_id``.
Omit it for the host.

``device_id`` is required on every action call by design. No "active
device" session state — it would invite cross-call confusion when
multiple devices are attached. The brain inspects ``mobile_list_devices``
first and threads the chosen serial through every subsequent call.

Mobile tools bypass the desktop input lock — a phone tap and a desktop
click can safely run concurrently from different MCP server instances.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from ..runtime.capture import frame_to_native, mobile_executor
from ..runtime.replies import action_reply, output_of
from ..state import current
from ..tooling import Content, ToolError, tool

# The mobile_* tools are on only when a phone transport is enabled
# (CONDUCTOR_DESKTOP_MOBILE_ANDROID / _IOS). They act on a device, not the host
# desktop, so they skip the desktop input lock: a spectator may drive a phone.
# mobile_shell is also an arbitrary shell, so CONDUCTOR_DESKTOP_SHELL=false drops it.
_mobile_tool = tool(kind="acting", lock=False, gate="mobile_enabled")


DeviceId = Annotated[str, Field(description="A device_id from mobile_list_devices.")]
ImageCoords = Annotated[bool, Field(description=(
    "true: the coordinates are pixels in the last screenshot of this device "
    "you were shown, and the server scales them to device pixels."))]


@tool(kind="observing", gate="mobile_enabled")
async def mobile_list_devices() -> str:
    """List connected phones and tablets as JSON, with the device_id every
    mobile_* call needs (there is no "current device"). Each entry has
    transport (adb or wda), name, model, os_version, screen size in device
    pixels, battery_pct, state ("device" when usable) and locked: don't tap
    a locked device. Call it again after plugging one in; no restart needed.

    `locked_detail` says why a device counts as locked. `state` is
    "unauthorized" until the phone accepts this computer's USB debugging
    prompt, and "offline" or "probe_failed" when it can't be reached.
    """
    reg = current().mobile
    if reg is None:
        raise ToolError(
            "mobile surface disabled — set CONDUCTOR_DESKTOP_MOBILE_ANDROID=true "
            "(or _IOS) and restart the MCP server."
        )
    devices = await reg.list_devices()
    return json.dumps([d.to_dict() for d in devices], ensure_ascii=False, default=str)


@_mobile_tool
async def mobile_tap(device_id: DeviceId,
                     x: Annotated[int, Field(description="Device pixels from the left.")],
                     y: Annotated[int, Field(description="Device pixels from the top.")],
                     image_coords: ImageCoords = False,
                     ) -> list[Content]:
    """Tap a point on a phone or tablet. Coordinates are device pixels, which
    is what locate(target, device_id=...) returns. A screenshot of the
    device may be smaller than the device (its reply says so): multiply, or
    pass image_coords=true to give pixels from the image. The reply carries
    a fresh screenshot when auto-screenshots are on."""
    if image_coords:
        [(x, y)], _ = frame_to_native(device_id, None, [(x, y)])
    ex = mobile_executor(device_id)
    result = await ex.tap(x, y)
    return await action_reply(f"mobile_tap[{device_id}]({x},{y})", result, device_id=device_id)


@_mobile_tool
async def mobile_swipe(device_id: DeviceId,
                       x1: Annotated[int, Field(description="Start, device pixels from the left.")],
                       y1: Annotated[int, Field(description="Start, device pixels from the top.")],
                       x2: Annotated[int, Field(description="End, device pixels from the left.")],
                       y2: Annotated[int, Field(description="End, device pixels from the top.")],
                       duration_ms: Annotated[int, Field(description=(
                           "Shorter flings (inertial scroll); 300 is a typical "
                           "scroll; 800 or more drags (select text, rearrange "
                           "icons); 150-250 usually dismisses a notification."))] = 300,
                       image_coords: ImageCoords = False,
                       ) -> list[Content]:
    """Swipe from (x1,y1) to (x2,y2) on a phone or tablet: scroll a list,
    change pages, dismiss a notification, or drag when slow enough (see
    duration_ms). Coordinates are device pixels, or screenshot pixels with
    image_coords=true. To scroll content up, swipe upward (y2 smaller than
    y1)."""
    if image_coords:
        [(x1, y1), (x2, y2)], _ = frame_to_native(device_id, None, [(x1, y1), (x2, y2)])
    ex = mobile_executor(device_id)
    result = await ex.swipe(x1, y1, x2, y2, duration_ms=duration_ms)
    return await action_reply(f"mobile_swipe[{device_id}]({x1},{y1})→({x2},{y2}) {duration_ms}ms",
                              result, device_id=device_id)


@_mobile_tool
async def mobile_type(device_id: DeviceId,
                      text: Annotated[str, Field(description="The text to type.")],
                      ) -> list[Content]:
    """Type text into the focused field on a phone or tablet; tap the field
    first. On Android this goes through `adb shell input text`, which drops
    non-ASCII characters: for other scripts, tap the on-screen keyboard, or
    drive a keyboard helper app through mobile_shell."""
    ex = mobile_executor(device_id)
    result = await ex.type_text(text)
    return await action_reply(f"mobile_type[{device_id}]({len(text)} chars)", result, observe=False)


@_mobile_tool
async def mobile_key(device_id: DeviceId,
                     key: Annotated[str, Field(description=(
                         'A key name such as "back", "home", "enter", or any '
                         'Android KEYCODE_* name.'))],
                     ) -> list[Content]:
    """Press a system or hardware key on a phone or tablet: home, back, enter,
    tab, escape, menu, power, volume_up, volume_down, recent_apps, or
    up/down/left/right (D-pad). Any Android KEYCODE_* name passes through as
    is, and a single letter or digit becomes its KEYCODE."""
    ex = mobile_executor(device_id)
    result = await ex.key_press(key)
    return await action_reply(f"mobile_key[{device_id}]({key})", result, observe=False)


@_mobile_tool
async def mobile_app_launch(device_id: DeviceId,
                            package_or_intent: Annotated[str, Field(description=(
                                'Android: a package ("com.android.settings") or a '
                                'component ("com.foo/.MainActivity"). iOS: a bundle ID '
                                '("com.apple.Preferences").'))],
                            ) -> list[Content]:
    """Launch an app on a phone or tablet. Android: by package name
    (com.android.settings), which opens its launcher activity, or by
    component (com.foo/.MainActivity) to open an activity the launcher
    doesn't list. iOS: by bundle ID (com.apple.Preferences). Quicker and
    surer than finding the app's icon on the home screen."""
    ex = mobile_executor(device_id)
    result = await ex.app_launch(package_or_intent)
    return await action_reply(f"mobile_app_launch[{device_id}]({package_or_intent})",
                              result, device_id=device_id)


@tool(kind="acting", lock=False, gate="mobile_shell_enabled")
async def mobile_shell(device_id: DeviceId,
                       command: Annotated[str, Field(description="Run as `adb shell <command>`.")],
                       timeout: Annotated[float, Field(description="Seconds before giving up.")] = 30.0,
                       ) -> str:
    """Run a shell command on an Android device through adb and return its
    output, e.g. `pm list packages` or `dumpsys battery`. Android only:
    other transports fail saying shell isn't supported. A command that
    fails is reported as an error."""
    ex = mobile_executor(device_id)
    if not hasattr(ex, "shell"):
        raise ToolError(f"shell not supported on this device's transport (device {device_id})")
    result = await ex.shell(command, timeout=timeout)
    return output_of(result, empty="ok", error="shell command failed (rc!=0)")
