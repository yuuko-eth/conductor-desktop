"""Mobile (Android via adb, iOS via WDA) backend for the MCP server.

Mobile is an *additive* surface — it sits alongside the host backend rather
than replacing it. The MCP server can hold a `MobileRegistry` at boot when
gated by CONDUCTOR_DESKTOP_MOBILE_ANDROID / CONDUCTOR_DESKTOP_MOBILE_IOS, and routes
mobile_* action tools + perception tools (with device_id set) through it.

Devices are addressed by their detected ID (adb serial, WDA UDID). Action
tool calls require `device_id`; perception tools default to the host when
device_id is omitted. See project_mobile_executor memory for the design.

Each device is wrapped in its own per-device executor (AndroidExecutor,
IOSExecutor) so adb's `-s SERIAL` flag is set once at construction. The
registry caches executors by device_id but always re-probes the device list
on `list_devices()` calls — adb-server picks up hot-plugged devices, and
callers should not assume the first list is final.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shlex
import shutil
from dataclasses import dataclass

from ..executor.base import ActionResult, _exc_str

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Device metadata
# ─────────────────────────────────────────────────────────────────────


@dataclass
class MobileDevice:
    """Connected mobile device + cached metadata for the brain.

    Lock + accessibility state are checked at probe time, NOT cached
    indefinitely — re-probed on each list_devices() call so the brain
    sees current state before deciding to tap.
    """
    device_id: str
    transport: str  # "adb" | "wda"
    name: str = ""
    model: str = ""
    os_version: str = ""
    screen_w: int = 0
    screen_h: int = 0
    locked: bool = False
    locked_detail: str = ""
    battery_pct: int = -1
    state: str = "unknown"  # "device" (ok) | "unauthorized" | "offline" | "unknown"

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "transport": self.transport,
            "name": self.name,
            "model": self.model,
            "os_version": self.os_version,
            "screen": {"w": self.screen_w, "h": self.screen_h},
            "locked": self.locked,
            "locked_detail": self.locked_detail,
            "battery_pct": self.battery_pct,
            "state": self.state,
        }


# ─────────────────────────────────────────────────────────────────────
# Android (adb)
# ─────────────────────────────────────────────────────────────────────


class AndroidExecutor:
    """Per-device adb wrapper — one instance per connected Android device.

    Stateless w.r.t. the device (each method spawns its own adb subprocess).
    Cached `_screen_w` / `_screen_h` are populated on first dimension
    request and refreshed if a probe sees a different size.
    """

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self._screen_w: int = 0
        self._screen_h: int = 0

    # Patterns that indicate a device-side failure even though `adb shell`
    # exited 0. The `input` binary normally produces no stderr on success;
    # when ART throws (SecurityException for blocked INJECT_EVENTS, RuntimeException
    # for malformed args) the trace lands on stderr while adb's transport
    # still returns rc=0. monkey emits `Error type 3` for missing components.
    # `Killed` indicates OOM-killer reaped the device-side process.
    _SHELL_FAILURE_PATTERNS = ("Exception", "Error type ", "Killed")

    async def _adb(self, *args: str, timeout: float = 15) -> tuple[bytes, bytes, int]:
        cmd = ["adb", "-s", self.serial, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(  # noqa: B904 — message is complete; context is noise
                f"adb -s {self.serial} {' '.join(args)}: timed out after {timeout}s"
            )
        return stdout, stderr, proc.returncode or 0

    async def _adb_text(self, *args: str, timeout: float = 15) -> str:
        """Run an adb command and return stdout. Raises on failure.

        Two layers of failure to surface:
          1. Outer adb rc != 0 — device offline / unauthorized / not found,
             daemon dead, or `adb` not on PATH. Stderr is the diagnostic.
          2. `adb shell <cmd>` returns rc=0 even when the device-side
             command failed (legacy shell protocol, no exit-code passthrough
             on older devices). Match known device-side error patterns in
             stderr so a `tap` blocked by SELinux doesn't report success.
        """
        stdout, stderr, rc = await self._adb(*args, timeout=timeout)
        err = stderr.decode("utf-8", errors="replace").strip()
        if rc != 0:
            raise RuntimeError(
                f"adb -s {self.serial} {' '.join(args)} → rc={rc}: "
                f"{err or '(no stderr)'}"
            )
        if err and any(p in err for p in self._SHELL_FAILURE_PATTERNS):
            raise RuntimeError(
                f"adb -s {self.serial} {' '.join(args)}: device-side error: {err}"
            )
        return stdout.decode("utf-8", errors="replace").strip()

    # ---- Probing / metadata ----

    async def probe(self) -> MobileDevice:
        """Build a MobileDevice snapshot — runs several adb queries in parallel."""
        # All independent — gather in parallel for snappy device listing.
        size_t, model_t, name_t, os_t, lock_t, batt_t = await asyncio.gather(
            self._adb_text("shell", "wm", "size"),
            self._adb_text("shell", "getprop", "ro.product.model"),
            self._adb_text("shell", "settings", "get", "global", "device_name"),
            self._adb_text("shell", "getprop", "ro.build.version.release"),
            self._adb_text("shell", "dumpsys", "window"),
            self._adb_text("shell", "dumpsys", "battery"),
            return_exceptions=True,
        )
        # Each may have failed individually — coerce to strings.
        def _s(x):
            return x if isinstance(x, str) else ""
        size_out = _s(size_t)
        w, h = self._parse_size(size_out)
        if w:
            self._screen_w, self._screen_h = w, h

        locked, lock_detail = self._parse_lock(_s(lock_t))
        return MobileDevice(
            device_id=self.serial,
            transport="adb",
            name=_s(name_t).strip() or _s(model_t).strip() or self.serial,
            model=_s(model_t).strip(),
            os_version=f"Android {_s(os_t).strip()}" if _s(os_t).strip() else "Android",
            screen_w=w or self._screen_w,
            screen_h=h or self._screen_h,
            locked=locked,
            locked_detail=lock_detail,
            battery_pct=self._parse_battery(_s(batt_t)),
            state="device",
        )

    @staticmethod
    def _parse_size(out: str) -> tuple[int, int]:
        # `wm size` prints "Physical size: 1080x2400" and may also print
        # an "Override size:" line — Override wins when present.
        physical = (0, 0)
        override = (0, 0)
        for line in out.splitlines():
            m = re.search(r"size:\s*(\d+)x(\d+)", line, re.IGNORECASE)
            if not m:
                continue
            pair = (int(m.group(1)), int(m.group(2)))
            if "override" in line.lower():
                override = pair
            else:
                physical = pair
        return override if override[0] else physical

    @staticmethod
    def _parse_lock(dumpsys_window: str) -> tuple[bool, str]:
        """Best-effort lock-screen detection from `dumpsys window`.

        mShowingLockscreen=true / KeyguardController showing=true are the
        common indicators across Android versions. False negatives are OK
        (brain just tries to tap and finds out); false positives would be
        worse — they'd block legitimate workflows. So default to "unlocked"
        on ambiguity.
        """
        if not dumpsys_window:
            return False, "unknown (dumpsys empty)"
        # Look for a few common signals.
        text = dumpsys_window.lower()
        if "mshowinglockscreen=true" in text:
            return True, "mShowingLockscreen=true"
        if re.search(r"keyguardcontroller.*showing=true", text):
            return True, "KeyguardController showing=true"
        if "mdreamingsleeptoken" in text and "mawake=false" in text:
            return True, "screen off"
        return False, "screen on, no lockscreen"

    @staticmethod
    def _parse_battery(out: str) -> int:
        m = re.search(r"^\s*level:\s*(\d+)", out, re.MULTILINE)
        return int(m.group(1)) if m else -1

    # ---- Capture ----

    async def screenshot(self) -> str:
        """Return base64-encoded PNG. Empty string on failure.

        `screencap -p` writes warnings to *stdout* (not stderr) on
        multi-display devices like the Galaxy Z Fold series — the
        binary PNG ends up prefixed by a "[Warning] Multiple displays
        were found ..." block, which makes PIL choke. We locate the
        PNG magic in the captured bytes and discard any preamble; this
        also guards against future stdout contamination from screencap
        without forcing a slower file-pull roundtrip.
        """
        stdout, stderr, rc = await self._adb("exec-out", "screencap", "-p", timeout=15)
        if rc != 0 or not stdout:
            log.warning("adb screencap failed (%s): %s",
                        self.serial, stderr.decode(errors="replace")[:200])
            return ""
        png_magic = b"\x89PNG\r\n\x1a\n"
        idx = stdout.find(png_magic)
        if idx < 0:
            log.warning("adb screencap (%s) returned no PNG magic; first 80 bytes: %r",
                        self.serial, stdout[:80])
            return ""
        if idx > 0:
            log.debug("adb screencap (%s) stripped %d-byte stdout preamble",
                      self.serial, idx)
            stdout = stdout[idx:]
        return base64.b64encode(stdout).decode("ascii")

    async def get_dimensions(self) -> tuple[int, int]:
        # Called on the grounded-action path (runtime.grounding.resolve_one); a
        # transient adb hiccup here would crash the whole tool call. The
        # 1080×1920 fallback is only off for non-portrait or high-DPI
        # devices, and the brain re-grounds from the screenshot anyway —
        # so a wrong-by-aspect-ratio dimension is recoverable, an exception
        # isn't.
        if self._screen_w and self._screen_h:
            return self._screen_w, self._screen_h
        try:
            out = await self._adb_text("shell", "wm", "size")
        except Exception as e:
            log.warning("get_dimensions(%s): %s — falling back to 1080×1920",
                        self.serial, e)
            log.debug("mobile failure traceback", exc_info=True)  # the warning stays one line
            return 1080, 1920
        w, h = self._parse_size(out)
        if w:
            self._screen_w, self._screen_h = w, h
        return self._screen_w or 1080, self._screen_h or 1920

    # ---- Touch input ----

    async def tap(self, x: int, y: int) -> ActionResult:
        try:
            await self._adb_text("shell", "input", "tap", str(x), str(y))
            return ActionResult(success=True, output=f"Tapped ({x},{y}) on {self.serial}")
        except Exception as e:
            log.debug("android tap failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))

    async def swipe(self, x1: int, y1: int, x2: int, y2: int,
                    duration_ms: int = 300) -> ActionResult:
        try:
            await self._adb_text(
                "shell", "input", "swipe",
                str(x1), str(y1), str(x2), str(y2), str(duration_ms),
            )
            return ActionResult(
                success=True,
                output=f"Swiped ({x1},{y1})→({x2},{y2}) {duration_ms}ms on {self.serial}",
            )
        except Exception as e:
            log.debug("android swipe failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))

    async def long_press(self, x: int, y: int, duration_ms: int = 800) -> ActionResult:
        # Long press = swipe to same point with a long duration.
        return await self.swipe(x, y, x, y, duration_ms=duration_ms)

    # ---- Keyboard ----

    async def type_text(self, text: str) -> ActionResult:
        """Type text via `adb shell input text`. Escapes shell metachars.

        Note: input text loses non-ASCII reliably. For Unicode payloads,
        consider clipboard-paste via clipper or kbd helper apps.

        `input text` sends key events, which the active keyboard sees: a
        composing one (Gboard Zhuyin, Pinyin, kana) turns them into its own
        pending text. The result says so, since adb reports success either way.
        """
        try:
            # `input text` reads %s as a space; the rest is passed through.
            payload = text.replace(" ", "%s")
            await self._adb_text("shell", *_sh_args("input", "text", payload))
        except Exception as e:
            log.debug("android type_text failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))
        output = f"typed {len(text)} chars on {self.serial}"
        notes = []
        if not text.isascii():
            notes.append("adb's `input text` drops non-ASCII characters, so those "
                         "were likely lost; tap them on the on-screen keyboard instead")
        if note := await self._ime_note():
            notes.append(note)
        if notes:
            output += "; warning: " + "; ".join(notes)
        return ActionResult(success=True, output=output)

    async def _ime_note(self) -> str:
        """A warning when the active keyboard composes key events, else "".
        Advice only: a failed read never fails the typing."""
        try:
            out = await self._adb_text(
                "shell", "settings get secure default_input_method; "
                "getprop persist.sys.locale; dumpsys input_method | grep -i subtype",
                timeout=5)
        except Exception:
            log.debug("android IME read failed", exc_info=True)
            return ""
        return _composing_ime_note(out)

    async def key_press(self, key: str) -> ActionResult:
        keycode = _android_keycode(key)
        try:
            await self._adb_text("shell", *_sh_args("input", "keyevent", keycode))
            return ActionResult(success=True, output=f"keyevent {keycode} on {self.serial}")
        except Exception as e:
            log.debug("android key_press failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))

    # ---- Apps + shell + windows ----

    async def app_launch(self, package_or_intent: str) -> ActionResult:
        """Launch an app by package (e.g. com.android.settings) or fully
        qualified intent (com.foo/.MainActivity).

        Pure package names go through monkey, which finds the launchable
        activity automatically. Slash-form goes through `am start -n`.
        """
        try:
            if "/" in package_or_intent:
                stdout, stderr, rc = await self._adb(
                    "shell", *_sh_args("am", "start", "-n", package_or_intent), timeout=15,
                )
            else:
                stdout, stderr, rc = await self._adb(
                    "shell", *_sh_args("monkey", "-p", package_or_intent, "-c",
                                      "android.intent.category.LAUNCHER", "1"),
                    timeout=15,
                )
            err = stderr.decode(errors="replace").strip()
            if rc != 0:
                return ActionResult(success=False, error=err or f"rc={rc}")
            return ActionResult(
                success=True,
                output=f"launched {package_or_intent} on {self.serial}",
            )
        except Exception as e:
            log.debug("android app_launch failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))

    async def shell(self, command: str, timeout: float = 30) -> ActionResult:
        try:
            stdout, stderr, rc = await self._adb("shell", command, timeout=timeout)
            return ActionResult(
                success=(rc == 0),
                output=stdout.decode("utf-8", errors="replace"),
                error=stderr.decode("utf-8", errors="replace"),
            )
        except TimeoutError:
            return ActionResult(success=False, error=f"timeout after {timeout}s")
        except Exception as e:
            log.debug("android shell failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))

    async def list_activities(self) -> ActionResult:
        """The resumed activity and the activity stack, as JSON, parsed from
        `dumpsys activity activities`."""
        try:
            out = await self._adb_text("shell", "dumpsys", "activity", "activities", timeout=10)
            return ActionResult(success=True, output=json.dumps(_summarize_activities(out)))
        except Exception as e:
            log.debug("android list_activities failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))


# `ActivityRecord{f4f3f52 u0 com.android.settings/.SubSettings t568}`
_ACTIVITY_RECORD = r"ActivityRecord\{\w+ u\d+ (\S+) t(-?\d+)"
_RESUMED_RE = re.compile(r"\b(?:Resumed|ResumedActivity|mResumedActivity):\s*" + _ACTIVITY_RECORD)
# Stack entries: "* Hist  #0: ..." (Android 10+) or "Run #0: ..." (older).
_STACK_RE = re.compile(r"(?:\bHist|\bRun)\s+#\d+:\s*" + _ACTIVITY_RECORD)


def _summarize_activities(dumpsys: str) -> dict:
    """The resumed activity and the stack, top first, from `dumpsys activity
    activities`. The raw dump is never passed on: it runs to tens of KB and
    carries app metadata, keys included."""
    resumed = _RESUMED_RE.search(dumpsys)
    stack, seen = [], set()
    for component, task in _STACK_RE.findall(dumpsys):
        if (component, task) not in seen:
            seen.add((component, task))
            stack.append({"activity": component, "task": int(task)})
    return {"resumed": resumed.group(1) if resumed else None, "activities": stack[:20]}


# Keyboards whose every layout composes, so key events never arrive as text.
_COMPOSING_IME_PACKAGES = (
    "com.google.android.inputmethod.pinyin",
    "com.google.android.inputmethod.japanese",
    "com.google.android.inputmethod.korean",
    "com.google.android.apps.inputmethod.zhuyin",
    "com.google.android.apps.inputmethod.cantonese",
    "com.android.inputmethod.pinyin",
    "com.sohu.inputmethod.sogou",
    "com.baidu.input",
    "com.iflytek.inputmethod",
    "jp.co.omronsoft.openwnn",
)
# Languages whose layouts on a multi-language keyboard (Gboard, SwiftKey,
# Samsung) compose: Zhuyin/Pinyin, kana, Hangul, Telex.
_COMPOSING_LANGUAGES = ("zh", "ja", "ko", "vi")
_SUBTYPE_LOCALE_RE = re.compile(
    r"current\w*subtype\S*.*?\b(?:languageTag|locale)=([A-Za-z]{2,3})", re.IGNORECASE)


def _composing_ime_note(out: str) -> str:
    """Read the reply to ``settings get secure default_input_method;
    getprop persist.sys.locale; dumpsys input_method | grep -i subtype``.

    The current subtype's locale settles it when dumpsys prints one (the
    format varies by Android release); otherwise the device locale stands in,
    which only says a composing layout is likely to be among the enabled ones.
    """
    lines = out.splitlines()
    ime = lines[0].strip() if lines else ""
    if not ime or ime == "null":
        return ""
    package = ime.split("/", 1)[0]
    if package in _COMPOSING_IME_PACKAGES:
        return (f"the active keyboard ({package}) composes key events, so the text "
                f"may be sitting in its candidate bar instead of the field")
    if m := _SUBTYPE_LOCALE_RE.search(out):
        lang = m.group(1).lower()
        if lang in _COMPOSING_LANGUAGES:
            return (f"the active keyboard ({package}) is on a {lang} layout, which "
                    f"composes key events, so the text may be sitting in its candidate "
                    f"bar; switch it to an English layout first")
        return ""
    device_locale = lines[1].strip() if len(lines) > 1 else ""
    lang = re.split(r"[-_]", device_locale, maxsplit=1)[0].lower()
    if lang in _COMPOSING_LANGUAGES:
        return (f"the device locale is {device_locale}; if {package} is on a composing "
                f"layout (Zhuyin, Pinyin, kana, Hangul), the text went to its "
                f"candidate bar, not the field; check, and switch to an English layout")
    return ""


def _sh_args(*args: str) -> list[str]:
    """Quote each argument for the device's ``sh``.

    ``adb shell`` joins its argv with spaces and hands the result to a shell on
    the device, so an unquoted caller-supplied argument is code: ``$(reboot)``
    typed as text would run. Quote everything that isn't a literal.
    """
    return [shlex.quote(a) for a in args]


def _android_keycode(key: str) -> str:
    """Map common key names to Android KEYCODE_* constants."""
    k = key.lower().strip()
    if k.startswith("keycode_"):
        return k.upper()
    _MAP = {
        "home": "KEYCODE_HOME", "back": "KEYCODE_BACK",
        "enter": "KEYCODE_ENTER", "return": "KEYCODE_ENTER",
        "tab": "KEYCODE_TAB", "space": "KEYCODE_SPACE",
        "delete": "KEYCODE_DEL", "backspace": "KEYCODE_DEL",
        "escape": "KEYCODE_ESCAPE", "menu": "KEYCODE_MENU",
        "power": "KEYCODE_POWER", "volume_up": "KEYCODE_VOLUME_UP",
        "volume_down": "KEYCODE_VOLUME_DOWN", "mute": "KEYCODE_VOLUME_MUTE",
        "camera": "KEYCODE_CAMERA", "search": "KEYCODE_SEARCH",
        "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN",
        "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT",
        "recent_apps": "KEYCODE_APP_SWITCH", "app_switch": "KEYCODE_APP_SWITCH",
    }
    # Anything else is assumed to be a KEYCODE_* suffix: "a" -> KEYCODE_A,
    # "f5" -> KEYCODE_F5, "page_down" -> KEYCODE_PAGE_DOWN.
    return _MAP.get(k, f"KEYCODE_{k.upper()}")


# ─────────────────────────────────────────────────────────────────────
# iOS (pymobiledevice3 / WDA HTTP)
# ─────────────────────────────────────────────────────────────────────
#
# Two-tier capability model:
#
#  Tier 1 — lockdown only (works as soon as device is paired + trusted):
#    probe()  — DeviceName, ProductType, ProductVersion, battery via
#               pymobiledevice3 lockdown info / diagnostics battery.
#    Used by `mobile_list_devices` so the brain can see the phone exists
#    even before WDA is wired.
#
#  Tier 2 — needs WDA HTTP server reachable (Wi-Fi or USB-mux forward):
#    screenshot()        — GET /screenshot (base64 PNG, native pixels).
#    get_dimensions()    — GET /window/size + /wda/screen (logical points
#                          × scale → pixel resolution matching screenshot).
#    tap, swipe, type_text, key_press, app_launch
#                        — POST /session/{sid}/wda/<verb>.
#
# Note: we deliberately do NOT route through `pymobiledevice3 developer
# wda` (which would require a sudo'd tunneld + DDI mount). WDA's own HTTP
# server is reachable via Wi-Fi (default) or USB-mux forwarded port and
# gives us every primitive we need without spawning a tunneld daemon.
# This matches what every other WDA client (Appium, Facebook tooling) does.
#
# Coordinate system note: WDA tap/drag take POINTS (logical). Screenshots
# come back in PIXELS. We expose pixels at the executor boundary (matching
# Android, matching the grounder's coord-from-screenshot mental model) and
# divide by the cached scale factor before each WDA call.


# Conservative default if WDA window-size hasn't been queried yet — at
# locate() time, dividing by zero would explode. Modern iPhone Pro device-
# pixel size; the actual value is overridden by Tier-3 get_dimensions()
# once WDA is wired. Brain can detect "screen size from default" by seeing
# screen_w == _IOS_DEFAULT_SCREEN[0] in MobileDevice.
_IOS_DEFAULT_SCREEN = (1170, 2532)


class _WDAClient:
    """W3C-style HTTP client for WebDriverAgent.

    Holds a single lazy session bound to SpringBoard (empty capabilities).
    Sessions persist across calls and auto-refresh on 404/410 (the codes
    WDA returns when the device-side runtime has been restarted).

    All URLs are constructed from ``base_url`` (e.g. ``http://127.0.0.1:8100``)
    — the caller picks Wi-Fi vs USB-mux forwarded; we don't care.
    """

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        import httpx  # local import: pulled in transitively, avoid module-load cost
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)
        self._session_id: str | None = None
        self._session_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _value(payload: dict):
        # WDA wraps results in {"value": ...} per W3C; tolerate flat too.
        return payload.get("value", payload) if isinstance(payload, dict) else payload

    async def status(self) -> dict:
        r = await self._client.get(f"{self.base_url}/status")
        r.raise_for_status()
        return self._value(r.json())

    async def _ensure_session(self) -> str:
        if self._session_id is not None:
            return self._session_id
        async with self._session_lock:
            if self._session_id is not None:
                return self._session_id
            # Empty capabilities → session bound to whatever's foreground.
            # WDA accepts both legacy (`desiredCapabilities`) and W3C
            # (`capabilities.alwaysMatch`) shapes; we send both for max
            # compatibility across forks.
            r = await self._client.post(
                f"{self.base_url}/session",
                json={
                    "capabilities": {"alwaysMatch": {}},
                    "desiredCapabilities": {},
                },
            )
            r.raise_for_status()
            data = r.json()
            sid = (
                (data.get("value") or {}).get("sessionId")
                or data.get("sessionId")
            )
            if not sid:
                raise RuntimeError(f"WDA session create returned no sessionId: {data!r}")
            self._session_id = sid
            return sid

    async def _post_session(self, path: str, body: dict,
                            timeout: float | None = None) -> dict:
        sid = await self._ensure_session()
        url = f"{self.base_url}/session/{sid}{path}"
        kwargs: dict = {"json": body}
        if timeout is not None:
            kwargs["timeout"] = timeout
        r = await self._client.post(url, **kwargs)
        if r.status_code in (404, 410):
            # Session went stale (Runner restarted, etc.) — refresh + retry once.
            self._session_id = None
            sid = await self._ensure_session()
            r = await self._client.post(f"{self.base_url}/session/{sid}{path}", **kwargs)
        r.raise_for_status()
        return r.json()

    async def _get_session(self, path: str) -> dict:
        sid = await self._ensure_session()
        r = await self._client.get(f"{self.base_url}/session/{sid}{path}")
        r.raise_for_status()
        return r.json()

    # ---- Capture / screen info ----

    async def screenshot_b64(self) -> str:
        """Return base64-encoded PNG (native device pixels)."""
        # /screenshot is session-less in modern WDA. Older forks require
        # a session-scoped URL; fall back if the top-level returns 404.
        r = await self._client.get(f"{self.base_url}/screenshot")
        if r.status_code == 404:
            data = await self._get_session("/screenshot")
            return self._value(data)
        r.raise_for_status()
        return self._value(r.json())

    async def screen_info(self) -> tuple[int, int, float]:
        """Return (pixel_w, pixel_h, scale).

        ``window/size`` reports logical points; ``/wda/screen`` reports the
        scale factor (typically 2.0 or 3.0 on Retina). Pixels = points × scale.
        """
        size = self._value(await self._get_session("/window/size"))
        screen = self._value(await self._get_session("/wda/screen"))
        w_pts = int(size["width"])
        h_pts = int(size["height"])
        scale = float(screen.get("scale", 1.0))
        return int(round(w_pts * scale)), int(round(h_pts * scale)), scale

    # ---- Input verbs (coordinates in POINTS) ----

    async def tap_points(self, x_pts: float, y_pts: float) -> None:
        # Modern WDA forks (Facebook v12+) use /wda/tap; older legacy
        # MJSONWP variant was /wda/tap/0 (the "/0" was the element id, but
        # element-less tap dropped it). We target the modern form.
        await self._post_session("/wda/tap", {"x": x_pts, "y": y_pts})

    async def drag_points(self, fx: float, fy: float, tx: float, ty: float,
                          duration_s: float) -> None:
        await self._post_session(
            "/wda/dragfromtoforduration",
            {"fromX": fx, "fromY": fy, "toX": tx, "toY": ty,
             "duration": duration_s},
            # Allow swipes longer than the default 15s timeout for slow
            # or long-distance drags (e.g. text selection).
            timeout=max(20.0, duration_s + 10.0),
        )

    async def type_keys(self, text: str) -> None:
        # Splitting into single-char list makes WDA emit each as a discrete
        # keystroke — matches user-typing rhythm and lets the IME track input.
        await self._post_session("/wda/keys", {"value": list(text)})

    async def press_button(self, name: str) -> None:
        # name ∈ {"home", "lock", "volumeup", "volumedown"} (case-insensitive)
        await self._post_session("/wda/pressButton", {"name": name})

    async def app_launch(self, bundle_id: str) -> None:
        await self._post_session("/wda/apps/launch", {"bundleId": bundle_id})


# Hardware-name → WDA pressButton name. WDA exposes only physical buttons;
# anything else (enter, tab, escape...) goes through type_keys with the
# matching unicode (these are recognized by iOS as physical-keyboard input).
_WDA_BUTTONS = {"home", "lock", "volumeup", "volumedown"}
_WDA_BUTTON_ALIASES = {
    "power": "lock", "sleep": "lock", "side": "lock",
    "volume_up": "volumeup", "vol_up": "volumeup", "volup": "volumeup",
    "volume_down": "volumedown", "vol_down": "volumedown", "voldown": "volumedown",
}
# Special-key → unicode for type_keys fallback. iOS recognizes these as
# their physical-keyboard counterparts.
_WDA_KEY_UNICODES = {
    "enter": "\n", "return": "\n", "newline": "\n",
    "tab": "\t", "space": " ",
    "backspace": "\b", "delete": "\b",  # WDA backspace is \b
    "escape": "\x1b", "esc": "\x1b",
}


class IOSExecutor:
    """iOS device via pymobiledevice3 (Tier 1 lockdown) + WDA HTTP (Tier 2).

    Mirrors `AndroidExecutor`'s primitive surface so the registry can
    dispatch through a uniform interface. When ``wda_url`` is None, action
    methods return clear ``ActionResult(success=False, error=...)`` so the
    brain reads the gap and the operator gets the unblock recipe.

    No ``shell`` method by design — iOS has no ``adb shell`` analogue.
    The MCP server's ``mobile_shell`` tool checks ``hasattr(ex, 'shell')``
    and returns a clean error when absent.
    """

    def __init__(self, udid: str, wda_url: str | None = None) -> None:
        self.udid = udid
        self.wda_url = wda_url
        self._screen_w: int = 0
        self._screen_h: int = 0
        # Cached after first WDA screen-info query. WDA tap/drag take points;
        # our public surface takes pixels, so we divide by this on each call.
        self._scale: float = 0.0
        self._wda: _WDAClient | None = None
        self._wda_lock = asyncio.Lock()

    async def _ensure_wda(self) -> _WDAClient | None:
        """Lazy-construct the WDA client. None if no URL is configured."""
        if self.wda_url is None:
            return None
        if self._wda is not None:
            return self._wda
        async with self._wda_lock:
            if self._wda is None:
                self._wda = _WDAClient(self.wda_url)
        return self._wda

    async def _ensure_scale(self) -> float:
        """Populate scale + cached dims from WDA. 1.0 fallback on failure."""
        if self._scale:
            return self._scale
        wda = await self._ensure_wda()
        if wda is None:
            return 1.0
        try:
            w_px, h_px, scale = await wda.screen_info()
            self._screen_w, self._screen_h = w_px, h_px
            self._scale = scale or 1.0
        except Exception as e:
            log.warning("WDA screen_info failed for %s: %s", self.udid, _exc_str(e))
            log.debug("mobile failure traceback", exc_info=True)  # the warning stays one line
            self._scale = 1.0
        return self._scale

    # ---- pymobiledevice3 shell-out helpers ----

    async def _pmd3(self, *args: str, timeout: float = 30,
                    json_out: bool = False) -> tuple[bytes, bytes, int]:
        """Run ``pymobiledevice3 <args>`` against this device.

        Always appends ``--udid <udid>`` so multi-device hosts can't
        accidentally talk to the wrong phone. ``json_out=True`` adds the
        ``--json`` flag — most pmd3 subcommands respect it and emit
        machine-readable output suitable for ``json.loads``.
        """
        cmd = ["pymobiledevice3", *args, "--udid", self.udid]
        if json_out:
            cmd.append("--json")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout, stderr, proc.returncode or 0

    async def _pmd3_json(self, *args: str, timeout: float = 30) -> dict | list | None:
        """Run pmd3 with ``--json`` and parse stdout. Returns None on failure."""
        stdout, stderr, rc = await self._pmd3(*args, timeout=timeout, json_out=True)
        if rc != 0 or not stdout:
            log.debug("pmd3 %s rc=%s stderr=%s", args, rc,
                      stderr.decode(errors="replace")[:200])
            return None
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            log.warning("pmd3 %s returned non-JSON (%s); first 120 bytes: %r",
                        args, e, stdout[:120])
            return None

    # ---- Probing / metadata (Tier 1 — no tunneld required) ----

    async def probe(self) -> MobileDevice:
        """Lockdown-only metadata snapshot. Safe to call without tunneld."""
        info_t, batt_t = await asyncio.gather(
            self._pmd3_json("lockdown", "info"),
            self._pmd3_json("diagnostics", "battery"),
            return_exceptions=True,
        )
        info = info_t if isinstance(info_t, dict) else {}
        batt = batt_t if isinstance(batt_t, dict) else {}

        device_name = (info.get("DeviceName") or "").strip()
        product_type = (info.get("ProductType") or "").strip()
        product_ver = (info.get("ProductVersion") or "").strip()

        return MobileDevice(
            device_id=self.udid,
            transport="wda",
            name=device_name or product_type or self.udid,
            model=product_type,
            os_version=f"iOS {product_ver}" if product_ver else "iOS",
            # Screen size known only via WDA (Tier 3). Defaults are overridden
            # once get_dimensions() lands; brain compares against
            # _IOS_DEFAULT_SCREEN to know whether the size is real.
            screen_w=self._screen_w or _IOS_DEFAULT_SCREEN[0],
            screen_h=self._screen_h or _IOS_DEFAULT_SCREEN[1],
            # iOS lock state requires WDA to query reliably (no lockdown
            # primitive). Default to "unknown — recommend no-passcode
            # device" so a brain seeing this hint can act appropriately.
            locked=False,
            locked_detail="iOS lock state requires WDA; configure device with no passcode",
            battery_pct=int(batt.get("CurrentCapacity", -1)) if isinstance(batt, dict) else -1,
            state="device",
        )

    # ---- Capture / dimensions (Tier 2 — WDA HTTP) ----

    async def screenshot(self) -> str:
        """Return base64-encoded PNG. Empty string on failure."""
        wda = await self._ensure_wda()
        if wda is None:
            log.warning("iOS screenshot %s: no WDA URL configured", self.udid)
            return ""
        try:
            return await wda.screenshot_b64()
        except Exception as e:
            log.warning("WDA screenshot failed for %s: %s", self.udid, _exc_str(e))
            log.debug("mobile failure traceback", exc_info=True)  # the warning stays one line
            return ""

    async def get_dimensions(self) -> tuple[int, int]:
        """Native screen size in device pixels.

        Queries WDA on first call and caches the result. Falls back to a
        modern-iPhone default when WDA isn't reachable so locate()'s
        norm→pixel conversion doesn't divide by zero. Brain can detect the
        fallback by comparing against ``_IOS_DEFAULT_SCREEN``.
        """
        if self._screen_w and self._screen_h:
            return self._screen_w, self._screen_h
        wda = await self._ensure_wda()
        if wda is None:
            return _IOS_DEFAULT_SCREEN
        await self._ensure_scale()  # populates _screen_w/_h as a side effect
        if self._screen_w and self._screen_h:
            return self._screen_w, self._screen_h
        return _IOS_DEFAULT_SCREEN

    # ---- Touch input (Tier 2 — WDA HTTP, coords in pixels) ----

    async def tap(self, x: int, y: int) -> ActionResult:
        wda = await self._ensure_wda()
        if wda is None:
            return self._wda_pending("tap")
        try:
            scale = await self._ensure_scale()
            await wda.tap_points(x / scale, y / scale)
            return ActionResult(success=True,
                                output=f"Tapped ({x},{y}) on iOS {self.udid}")
        except Exception as e:
            log.debug("iOS tap failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))

    async def swipe(self, x1: int, y1: int, x2: int, y2: int,
                    duration_ms: int = 300) -> ActionResult:
        wda = await self._ensure_wda()
        if wda is None:
            return self._wda_pending("swipe")
        try:
            scale = await self._ensure_scale()
            await wda.drag_points(
                x1 / scale, y1 / scale, x2 / scale, y2 / scale,
                duration_s=duration_ms / 1000.0,
            )
            return ActionResult(
                success=True,
                output=f"Swiped ({x1},{y1})→({x2},{y2}) {duration_ms}ms on iOS {self.udid}",
            )
        except Exception as e:
            log.debug("iOS swipe failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))

    async def type_text(self, text: str) -> ActionResult:
        wda = await self._ensure_wda()
        if wda is None:
            return self._wda_pending("type")
        try:
            await wda.type_keys(text)
            return ActionResult(success=True,
                                output=f"Typed {len(text)} chars on iOS {self.udid}")
        except Exception as e:
            log.debug("iOS type_text failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))

    async def key_press(self, key: str) -> ActionResult:
        """Press a hardware button or send a special key.

        WDA's ``pressButton`` only knows physical buttons (home, lock,
        volumeup, volumedown). For typewriter keys (enter, tab, escape,
        backspace) we route through ``type_keys`` with the matching
        unicode — iOS recognises those as physical-keyboard input.
        """
        wda = await self._ensure_wda()
        if wda is None:
            return self._wda_pending(f"key_press({key})")
        k = key.lower().strip()
        button = _WDA_BUTTON_ALIASES.get(k, k)
        try:
            if button in _WDA_BUTTONS:
                await wda.press_button(button)
                return ActionResult(success=True,
                                    output=f"Pressed {button} on iOS {self.udid}")
            unicode_ch = _WDA_KEY_UNICODES.get(k)
            if unicode_ch is not None:
                await wda.type_keys(unicode_ch)
                return ActionResult(success=True,
                                    output=f"Sent {k} ({unicode_ch!r}) on iOS {self.udid}")
            return ActionResult(
                success=False,
                error=(
                    f"iOS key {key!r} not supported. Hardware buttons: "
                    f"{sorted(_WDA_BUTTONS)}. Typewriter keys: "
                    f"{sorted(_WDA_KEY_UNICODES)}. For other keys, use type_text."
                ),
            )
        except Exception as e:
            log.debug("iOS key_press failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))

    # ---- App management (Tier 2 — WDA HTTP) ----

    async def app_launch(self, package_or_intent: str) -> ActionResult:
        """Launch an iOS app by bundle ID (e.g. com.apple.Preferences)."""
        if "/" in package_or_intent:
            return ActionResult(
                success=False,
                error=(
                    "iOS expects a bundle ID (com.apple.X), not an Android-"
                    f"style component reference: {package_or_intent!r}"
                ),
            )
        wda = await self._ensure_wda()
        if wda is None:
            return self._wda_pending(f"app_launch({package_or_intent})")
        try:
            await wda.app_launch(package_or_intent)
            return ActionResult(success=True,
                                output=f"launched {package_or_intent} on iOS {self.udid}")
        except Exception as e:
            log.debug("iOS app_launch failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))

    async def list_activities(self) -> ActionResult:
        """iOS has no Android-style activity stack. Returns the foreground
        app's bundleId from WDA when available."""
        wda = await self._ensure_wda()
        if wda is None:
            return self._wda_pending("list_activities")
        try:
            data = await wda._get_session("/wda/activeAppInfo")
            return ActionResult(success=True,
                                output=json.dumps(wda._value(data), default=str))
        except Exception as e:
            log.debug("iOS list_activities failed", exc_info=True)
            return ActionResult(success=False, error=_exc_str(e))

    # ---- Pending-state helper ----

    @staticmethod
    def _wda_pending(action: str) -> ActionResult:
        return ActionResult(
            success=False,
            error=(
                f"iOS {action}: WDA URL not configured. Set "
                f"CONDUCTOR_DESKTOP_MOBILE_IOS_WDA_URL=http://<host>:<port> "
                f"(typically http://127.0.0.1:8100 with `pymobiledevice3 "
                f"usbmux forward 8100 8100 --udid <UDID>` running, OR "
                f"the iPhone's Wi-Fi IP if WDA listens on Wi-Fi)."
            ),
        )


# ─────────────────────────────────────────────────────────────────────
# Registry — singleton held by the MCP server
# ─────────────────────────────────────────────────────────────────────


class MobileRegistry:
    """Singleton coordinating per-device executors.

    Holds gate flags + a cache of per-device executors. `list_devices`
    re-probes adb each time so hot-plugged devices show up without an
    MCP-server restart. `get_executor(device_id)` returns a stable
    AndroidExecutor for the brain to bounce calls through.
    """

    def __init__(self, *, android_enabled: bool, ios_enabled: bool,
                 ios_wda_url: str | None = None) -> None:
        self.android_enabled = android_enabled
        self.ios_enabled = ios_enabled
        # Optional WDA HTTP base URL; passed to every IOSExecutor we mint.
        # ``http://127.0.0.1:8100`` (USB-mux forwarded) is the typical value;
        # an iPhone Wi-Fi IP works too if WDA's Wi-Fi listener is enabled.
        self.ios_wda_url: str | None = ios_wda_url
        self.android_available: bool = False  # adb on PATH + start-server ok
        self.android_detail: str = "disabled"
        self.ios_available: bool = False  # pymobiledevice3 on PATH
        self.ios_detail: str = "disabled"
        self.ios_wda_reachable: bool = False  # advisory; reported in diagnose
        self._android_cache: dict[str, AndroidExecutor] = {}
        self._ios_cache: dict[str, IOSExecutor] = {}
        # Populated by list_devices(); read by get_executor() to dispatch
        # to the right class. {device_id: "adb" | "wda"}.
        self._device_transport: dict[str, str] = {}

    async def boot_probe(self) -> None:
        """Run once at MCP startup. Failures here log + disable the gate
        for that transport rather than crashing the server."""
        if self.android_enabled:
            await self._probe_android()
        if self.ios_enabled:
            await self._probe_ios()

    async def _probe_android(self) -> None:
        if shutil.which("adb") is None:
            self.android_detail = "adb not on PATH (install platform-tools)"
            log.warning("mobile: %s", self.android_detail)
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "start-server",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                self.android_detail = f"adb start-server rc={proc.returncode}: {stderr.decode(errors='replace')[:120]}"
                log.warning("mobile: %s", self.android_detail)
                return
        except TimeoutError:
            self.android_detail = "adb start-server timeout"
            log.warning("mobile: %s", self.android_detail)
            return
        except Exception as e:
            self.android_detail = f"adb start-server failed: {_exc_str(e)}"
            log.warning("mobile: %s", self.android_detail)
            log.debug("mobile failure traceback", exc_info=True)  # the warning stays one line
            return
        self.android_available = True
        self.android_detail = "adb ready"
        log.info("mobile: %s", self.android_detail)

    async def _probe_ios(self) -> None:
        """Verify pymobiledevice3 is on PATH (lockdown queries) and probe
        the WDA HTTP endpoint when one is configured (action surface)."""
        if shutil.which("pymobiledevice3") is None:
            self.ios_detail = (
                "pymobiledevice3 not on PATH — install via "
                "`uv tool install pymobiledevice3` or `pipx install pymobiledevice3`"
            )
            log.warning("mobile: %s", self.ios_detail)
            return
        self.ios_available = True

        # WDA reachability — only meaningful when a URL is configured.
        # GET /status is the canonical health check (no session needed).
        if self.ios_wda_url:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=3.0) as c:
                    r = await c.get(f"{self.ios_wda_url.rstrip('/')}/status")
                    self.ios_wda_reachable = r.status_code == 200
            except Exception as e:
                log.debug("mobile: WDA probe failed: %s", _exc_str(e), exc_info=True)
                self.ios_wda_reachable = False

        if not self.ios_wda_url:
            self.ios_detail = (
                "pymobiledevice3 ready; WDA URL NOT configured "
                "(set CONDUCTOR_DESKTOP_MOBILE_IOS_WDA_URL for action support). "
                "list_devices works via lockdown."
            )
        elif self.ios_wda_reachable:
            self.ios_detail = f"pymobiledevice3 ready, WDA reachable at {self.ios_wda_url}"
        else:
            self.ios_detail = (
                f"pymobiledevice3 ready; WDA at {self.ios_wda_url} NOT reachable — "
                f"check `xcodebuild test-without-building` is running on the host "
                f"that owns the iPhone's USB cable, and that the port forward / "
                f"Wi-Fi listener is up. list_devices still works."
            )
        log.info("mobile: %s", self.ios_detail)

    # ---- Listing ----

    async def list_devices(self) -> list[MobileDevice]:
        """Re-probe and return all connected mobile devices.

        Repopulates the transport map so subsequent ``get_executor`` calls
        can dispatch to the right class without ambiguity when both
        transports are enabled.
        """
        out: list[MobileDevice] = []
        if self.android_available:
            out.extend(await self._list_android())
        if self.ios_available:
            out.extend(await self._list_ios())
        # Refresh transport map from the fresh listing.
        for d in out:
            self._device_transport[d.device_id] = d.transport
        return out

    async def _list_android(self) -> list[MobileDevice]:
        proc = await asyncio.create_subprocess_exec(
            "adb", "devices", "-l",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        text = stdout.decode("utf-8", errors="replace")
        # Output format:
        #   List of devices attached
        #   SERIAL  device|unauthorized|offline  product:... model:... ...
        serials: list[tuple[str, str]] = []  # (serial, state)
        for line in text.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serials.append((parts[0], parts[1]))

        results: list[MobileDevice] = []
        # Probe healthy devices in parallel; partial-state devices get a
        # placeholder so the brain knows they exist but can't be acted on.
        healthy = [s for s, st in serials if st == "device"]
        unhealthy = [(s, st) for s, st in serials if st != "device"]

        if healthy:
            execs = [self._get_android(s) for s in healthy]
            probes = await asyncio.gather(
                *(e.probe() for e in execs), return_exceptions=True,
            )
            for serial, _exec, probe in zip(healthy, execs, probes):
                if isinstance(probe, MobileDevice):
                    results.append(probe)
                else:
                    log.warning("mobile: probe %s failed: %s", serial, probe)
                    results.append(MobileDevice(
                        device_id=serial, transport="adb",
                        name=serial, state="probe_failed",
                        locked_detail=str(probe)[:120],
                    ))
        for serial, state in unhealthy:
            results.append(MobileDevice(
                device_id=serial, transport="adb",
                name=serial, state=state,
                locked_detail=f"adb state={state} — try `adb kill-server` or check USB-debug auth on the device",
            ))
        return results

    async def _list_ios(self) -> list[MobileDevice]:
        """Enumerate iOS devices.

        Two discovery paths:
          1. ``pymobiledevice3 usbmux list`` — only sees devices physically
             USB-attached to THIS Mac. Rich metadata (true UDID, real
             DeviceName, battery) via lockdown probes.
          2. WDA-only fallback — when usbmux returns empty AND a WDA URL is
             reachable, synthesize a single device from WDA's own endpoints.
             Useful when the iPhone's USB cable is on a *different* host
             (e.g. a build Mac running ``xcodebuild test-without-building``)
             but WDA is reachable over Wi-Fi from this host.

        WDA-only mode loses lockdown metadata (real UDID, battery, true
        device name) but gains action capability — which is what actually
        matters for the brain.
        """
        usb_results = await self._list_ios_usbmux()
        if usb_results:
            return usb_results
        if self.ios_wda_url and self.ios_wda_reachable:
            wda_result = await self._list_ios_wda_only()
            if wda_result is not None:
                return [wda_result]
        return []

    async def _list_ios_usbmux(self) -> list[MobileDevice]:
        """USB-connected iOS via lockdown. Empty list if no USB-attached devices."""
        proc = await asyncio.create_subprocess_exec(
            "pymobiledevice3", "usbmux", "list", "--no-color", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except TimeoutError:
            proc.kill()
            log.warning("mobile: pymobiledevice3 usbmux list timed out")
            return []
        if proc.returncode != 0 or not stdout:
            log.debug("mobile: usbmux list rc=%s stderr=%s",
                      proc.returncode, stderr.decode(errors="replace")[:200])
            return []
        try:
            entries = json.loads(stdout)
        except json.JSONDecodeError as e:
            log.warning("mobile: usbmux list non-JSON (%s); first 120 bytes: %r",
                        e, stdout[:120])
            return []
        if not isinstance(entries, list):
            return []

        # Different pmd3 versions have used "Identifier" vs "SerialNumber"
        # for the UDID field; check both.
        udids: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            udid = entry.get("Identifier") or entry.get("SerialNumber") or ""
            if udid:
                udids.append(udid)

        if not udids:
            return []

        execs = [self._get_ios(u) for u in udids]
        probes = await asyncio.gather(
            *(e.probe() for e in execs), return_exceptions=True,
        )
        results: list[MobileDevice] = []
        for udid, probe in zip(udids, probes):
            if isinstance(probe, MobileDevice):
                results.append(probe)
            else:
                log.warning("mobile: iOS probe %s failed: %s", udid, probe)
                results.append(MobileDevice(
                    device_id=udid, transport="wda",
                    name=udid, state="probe_failed",
                    locked_detail=str(probe)[:120],
                ))
        return results

    async def _list_ios_wda_only(self) -> MobileDevice | None:
        """Synthesize a single MobileDevice purely from WDA endpoints.

        Used when the iPhone isn't USB-attached to this host (so lockdown
        is unavailable) but WDA is reachable over Wi-Fi. The device_id is
        WDA's ``identifierForVendor`` (NOT the iOS UDID) — stable across
        WDA Runner sessions on the same install. Battery and true device
        name aren't reachable from WDA; surfaces as -1 / "iPhone".
        """
        try:
            wda = _WDAClient(self.ios_wda_url)
        except Exception as e:
            log.warning("mobile: WDA client init failed: %s", _exc_str(e))
            log.debug("mobile failure traceback", exc_info=True)  # the warning stays one line
            return None
        try:
            status = await wda.status()
            sid = await wda._ensure_session()
            r = await wda._client.get(f"{wda.base_url}/session/{sid}/wda/device/info")
            r.raise_for_status()
            info = wda._value(r.json())
            w_px, h_px, _ = await wda.screen_info()
        except Exception as e:
            log.warning("mobile: WDA-only device probe failed: %s", _exc_str(e))
            log.debug("mobile failure traceback", exc_info=True)  # the warning stays one line
            return None
        finally:
            await wda.aclose()

        ifv = info.get("uuid") or "wda-device"
        device_id = f"ios-{ifv}"
        os_ver = (status.get("os") or {}).get("version", "")
        # Construct + cache the executor under the synthesized device_id so
        # subsequent action calls don't re-probe.
        ex = self._ios_cache.get(device_id)
        if ex is None:
            ex = IOSExecutor(device_id, wda_url=self.ios_wda_url)
            ex._screen_w, ex._screen_h = w_px, h_px
            self._ios_cache[device_id] = ex
        return MobileDevice(
            device_id=device_id,
            transport="wda",
            name=info.get("name") or "iPhone",
            model=info.get("model") or "iPhone",
            os_version=f"iOS {os_ver}" if os_ver else "iOS",
            screen_w=w_px, screen_h=h_px,
            locked=False,
            locked_detail=(
                "WDA-only mode (no USB lockdown access from this host); "
                "lock state not detectable — assume unlocked"
            ),
            battery_pct=-1,
            state="device",
        )

    # ---- Executor lookup ----

    def _get_android(self, serial: str) -> AndroidExecutor:
        ex = self._android_cache.get(serial)
        if ex is None:
            ex = AndroidExecutor(serial)
            self._android_cache[serial] = ex
        return ex

    def _get_ios(self, udid: str) -> IOSExecutor:
        ex = self._ios_cache.get(udid)
        if ex is None:
            ex = IOSExecutor(udid, wda_url=self.ios_wda_url)
            self._ios_cache[udid] = ex
        return ex

    def get_executor(self, device_id: str):
        """Return the per-device executor, dispatching by transport.

        Resolution order:
          1. Transport map populated by list_devices() — authoritative.
          2. If only one transport is enabled, use it.
          3. Otherwise raise — caller should run mobile_list_devices first
             so the registry knows whether device_id is Android or iOS.
        """
        transport = self._device_transport.get(device_id)
        if transport == "adb":
            if not self.android_available:
                raise RuntimeError(
                    f"mobile (android) not available: {self.android_detail}. "
                    f"Set CONDUCTOR_DESKTOP_MOBILE_ANDROID=true and ensure `adb` is on PATH."
                )
            return self._get_android(device_id)
        if transport == "wda":
            if not self.ios_available:
                raise RuntimeError(
                    f"mobile (ios) not available: {self.ios_detail}. "
                    f"Set CONDUCTOR_DESKTOP_MOBILE_IOS=true and install pymobiledevice3."
                )
            return self._get_ios(device_id)

        # Transport unknown — pick the only available one or fail loud.
        if self.android_available and not self.ios_available:
            return self._get_android(device_id)
        if self.ios_available and not self.android_available:
            return self._get_ios(device_id)
        if self.android_available and self.ios_available:
            raise RuntimeError(
                f"mobile: device_id {device_id!r} has unknown transport "
                f"and both Android+iOS are enabled. Call mobile_list_devices "
                f"first so the registry can resolve it."
            )
        raise RuntimeError(
            f"mobile: no transport available "
            f"(android: {self.android_detail}; ios: {self.ios_detail})."
        )

    # ---- Status (for diagnose) ----

    def status_lines(self) -> list[str]:
        ios_line = (
            f"mobile.ios:     {'enabled' if self.ios_enabled else 'disabled'}"
            f" ({'OK' if self.ios_available else 'unavailable'} — {self.ios_detail})"
        )
        if self.ios_available and self.ios_wda_url:
            ios_line += f" [WDA: {'up' if self.ios_wda_reachable else 'DOWN'}]"
        return [
            (f"mobile.android: {'enabled' if self.android_enabled else 'disabled'}"
             f" ({'OK' if self.android_available else 'unavailable'} — {self.android_detail})"),
            ios_line,
        ]
