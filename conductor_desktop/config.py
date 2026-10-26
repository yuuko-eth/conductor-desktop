"""MCP server config — env-driven, minimal, independent of the standalone Settings."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .grounder import (
    CoordSpace,
    family_can_describe,
    family_can_ground,
    family_is_point_only,
)
from .http_auth import MIN_TOKEN_LEN, is_loopback
from .paths import state_dir


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    return _env_number(key, default, int)


def _env_float(key: str, default: float) -> float:
    return _env_number(key, default, float)


def _env_number(key: str, default, cast):
    v = os.environ.get(key)
    if v is None or not v.strip():
        return default
    try:
        return cast(v)
    except ValueError as e:
        raise RuntimeError(
            f"{key}={v!r} is not a {cast.__name__}; unset it to use {default}."
        ) from e


def _env_choice(key: str, default: str, allowed: set[str], *, extra: str = "") -> str:
    """A lower-cased env value restricted to ``allowed``; anything else fails
    at startup naming the variable and the choices."""
    v = os.environ.get(key, default).lower()
    if v not in allowed:
        raise RuntimeError(f"{key}={v!r} invalid. Choose one of: {sorted(allowed)}.{extra}")
    return v


def _auth_token_from_env() -> str:
    """The bearer token from CONDUCTOR_DESKTOP_AUTH_TOKEN or the file named by
    CONDUCTOR_DESKTOP_AUTH_TOKEN_FILE. Both are popped from the environment so
    exec_shell and anything else this process starts can't read them."""
    inline = os.environ.pop("CONDUCTOR_DESKTOP_AUTH_TOKEN", "").strip()
    path = os.environ.pop("CONDUCTOR_DESKTOP_AUTH_TOKEN_FILE", "").strip()
    if inline and path:
        raise RuntimeError("Set CONDUCTOR_DESKTOP_AUTH_TOKEN or CONDUCTOR_DESKTOP_AUTH_TOKEN_FILE, not both.")
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                inline = fh.read().strip()
        except OSError as e:
            raise RuntimeError(f"CONDUCTOR_DESKTOP_AUTH_TOKEN_FILE={path!r}: {e}") from e
    if inline and len(inline) < MIN_TOKEN_LEN:
        raise RuntimeError(
            f"The auth token is {len(inline)} characters; use at least {MIN_TOKEN_LEN} "
            "(python -c 'import secrets; print(secrets.token_urlsafe(32))').")
    return inline


_VALID_TRANSPORTS = {"stdio", "sse", "http"}
# Only "resident" is implemented; the others name what create_executor
# explains (phones are the mobile_* tools, the rest were never built).
_VALID_BACKENDS = {"resident", "adb", "wda", "vnc", "rdp", "vbox"}
_VALID_SCREENSHOT_MODES = {"inline", "file", "both"}
# How much of each tool call's arguments the audit log keeps.
_VALID_AUDIT_ARGS = {"none", "redacted", "full"}
_VALID_GROUNDER_FAMILIES = {"qwen", "gemma4", "locateanything", "holo",
                            "venus", "guiowl", "maiui", "evocua", "uitars",
                            # describe-only families (no grounding dialect):
                            "minicpm"}


@dataclass
class MCPConfig:
    grounder_url: str
    # Which executor drives the host. "resident" (this machine) is the only
    # one implemented; the other names exist so a config that asks for them
    # gets create_executor's explanation instead of "unknown".
    backend: str = "resident"
    connection_uri: str = ""
    platform: str = ""
    # Off by default: an acting tool replies with text (plus change notices when
    # they're on), and the agent looks when it decides to. On, every action and
    # every failed action attaches a screenshot of the result.
    auto_screenshot: bool = False
    # Text-only mode — post-action observations are VLM-generated descriptions
    # instead of raw screenshots. RECOMMENDED for medium/high-difficulty
    # navigation tasks: describe text (~200-500 tokens/screen) preserves
    # recall budget for on-page content vs raw images (~1500-3000 tokens),
    # and text-only context appends cleanly turn after turn so the harness's
    # prefix cache survives — empirically ~3x faster per turn with a local
    # Qwen3.5-27B on a single-slot llama-server. Also the right choice
    # for drivers that cannot ingest images. Costs one extra VLM round-trip
    # per observed action. It implies the post-action observation whatever
    # `auto_screenshot` says: turn text_only off for fully silent actions with
    # `read_screen()` called explicitly.
    text_only: bool = False
    screenshot_max_width: int = 0
    # Cap on the LONG edge (px) of an inline image returned to the MCP
    # caller, aspect-preserving. Separate from screenshot_max_width, which
    # governs grounder input. 1568 is about where the Claude API starts
    # shrinking images itself, so a laptop screen goes out unscaled and the
    # image is the click coordinate space. When an image is shrunk, the reply
    # says by how much (see runtime/capture.py). 0 disables the cap.
    payload_max_edge: int = 1568
    # Overrides the qwen/gemma4 dialect's coordinate space for the model at
    # grounder_url: image_pixels for one that answers in pixels (Qwen2.5-VL).
    # None = the dialect's own; Qwen3-VL answers in norm1000 even though its
    # native format is often described as pixels.
    coord_system: CoordSpace | None = None
    # Which model-family dialect the grounder speaks. Drives the system
    # prompt, bbox JSON key, axis order, and max_tokens budget the grounder
    # client sends. See conductor_desktop.grounder.DIALECTS for the full table.
    #   qwen    = Qwen3-VL-Instruct family (default; tightest fit)
    #   gemma4  = Gemma 4 (E2B/E4B) — native [y1,x1,y2,x2] / 0..1000 / box_2d,
    #             with a larger max_tokens budget for the model's thinking
    #             channel.
    #   locateanything = NVIDIA LocateAnything-3B, a purpose-built grounder on
    #             the llama.cpp fork. Emits native control tokens (no JSON, no
    #             thinking tax) normalized 0..1000 xyxy. Grounding-only —
    #             describe is NOT supported on this family, so you MUST also set
    #             describe_url/describe_family at a Qwen3-VL server (see below).
    #             Requires the server launched with `--special`.
    #   holo    = HCompany Holo-3.1 (0.8B/3B/7B) — single-point GUI grounder.
    #             Returns one click point normalized 0..1000 (no multi-match).
    #             Grounding-only like locateanything → set describe_url/
    #             describe_family at a Qwen3-VL server. Tiny (0.8B ≈ 1.5GB VRAM).
    #   uitars  = ByteDance UI-TARS-1.5-7B — point grounder answering in
    #             resized-image pixels; the client pre-resizes and converts to
    #             norm1000 itself.
    #             Grounding-only → set describe_url/describe_family.
    grounder_family: str = "qwen"
    # Split backend (since the LocateAnything leg): ground/locate route to
    # grounder_url+grounder_family above; describe/describe_image route here.
    # Both default empty → describe reuses the ground backend (one server does
    # both, the historical single-VLM setup). Set these when the ground family
    # cannot describe (LocateAnything) or when you simply want describe served
    # by a different/larger VLM. describe_family must be a describe-capable
    # family (qwen|gemma4 — NOT locateanything). describe_url alone (family
    # empty) overrides only the URL, keeping grounder_family's describe dialect;
    # describe_family alone (url empty) overrides only the dialect on the same
    # URL (e.g. a multi-model server).
    describe_url: str = ""
    describe_family: str = ""
    # Managed (SaaS) mode. When grounder_family == "managed", grounder_url is the
    # conductor-grounder gateway base URL and api_key is a cgr_live_… seat token.
    # The gateway owns capability routing, dialects, the license gate, and
    # metering — so describe_url/describe_family/coord branching are all unused
    # (the gateway is one endpoint pair speaking norm1000). See managed_grounder.
    api_key: str = ""
    grounder_timeout: float = 30.0
    wait_for_poll_interval: float = 0.5
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765
    # Pre-shared bearer token for every HTTP route (MCP transport and
    # dashboard); see conductor_desktop/http_auth.py. Empty = no auth, which is
    # only allowed on a loopback bind.
    auth_token: str = ""
    screenshot_mode: str = "inline"
    screenshot_dir: str = ""
    # When enabled, every MCP tool result gets a trailing "[NNms]" line
    # reporting how long the handler took. Off by default — useful while
    # profiling or when a tool seems to be running unexpectedly slowly.
    tool_timing: bool = False
    # Operator dashboard at /dashboard. Off by default: under stdio it means
    # binding a second HTTP port nobody asked for. Under http/sse it shares
    # the MCP port; under stdio it runs a sidecar on host:port.
    dashboard: bool = False
    # Desktop input lock file. Empty = ~/.conductor-desktop/desktop.lock. Servers only
    # exclude each other when they share the file, so leave it alone in
    # normal use; a test run (scripts/smoke_matrix.py) points at its own file
    # to act alongside an idle holder.
    lock_path: str = ""
    # Audit log — one JSON line per tool call, one file per day, under
    # `audit_dir` (default ~/.conductor-desktop/logs). ON by default: this
    # server is a remote-control surface for a real machine, and the
    # operator should get a record of what drove it without having to opt
    # in. `audit_args` decides how much of each call's arguments is kept:
    #   none     — tool names and outcomes only
    #   redacted — (default) argument names, with the values most likely to
    #              carry a secret (text/value/js/command/...) replaced by
    #              "<redacted:N chars>"
    #   full     — argument values up to a cap. Captures typed passwords;
    #              for debugging, not for a shared machine.
    # `audit_retention_days` prunes older files at startup; 0 keeps forever.
    # See https://docs.conductor-mcp.io/security/audit-log/.
    audit: bool = True
    audit_dir: str = ""
    audit_args: str = "redacted"
    audit_retention_days: int = 14
    # CDP (Chrome DevTools Protocol) — drives web_eval / web_crop tools.
    # Requires Chrome launched with --remote-debugging-port=<cdp_port>
    # AND --user-data-dir=<path> (required on macOS).
    cdp_host: str = "127.0.0.1"
    cdp_port: int = 9222
    # Default substring (URL or title, case-insensitive) used to pick a tab
    # when the tool caller omits `tab`. Empty string = first page target.
    cdp_default_tab_filter: str = ""
    # Browser registry for web_* tools: comma-separated slug=scheme://host:port,
    # scheme cdp (Chromium) or bidi (Firefox WebDriver BiDi), e.g.
    # "chrome=cdp://127.0.0.1:9222,firefox=bidi://127.0.0.1:9223". Empty =
    # one implicit chrome=cdp://<cdp_host>:<cdp_port>.
    browsers: str = ""
    # Slug web_* tools use when a call names no browser and several are configured.
    default_browser: str = ""
    # Perception delta channel — AT-SPI / AX / WinEvent / CDP observers that
    # ride along on every tool result. Off by default; flip on with
    # CONDUCTOR_DESKTOP_DELTAS=true when you want cheap structural cues for
    # action-confirmation. Leaving off keeps envelopes deterministic (good
    # for golden-file tests and image-only drivers) and avoids noisy hosts
    # (e.g. macOS Terminal AXTextArea storms) drowning the brain.
    deltas_enabled: bool = False
    # Mobile (Android via adb, iOS via WDA). Additive surface — the host
    # backend still owns the desktop; mobile_* tools sit alongside, with
    # device_id-routed perception tools sharing the screenshot/locate/
    # describe surface. Each transport has its own gate because adb is a
    # single binary while WDA needs xcodebuild + per-device pairing — users
    # commonly have one but not the other. Off by default.
    mobile_android_enabled: bool = False
    mobile_ios_enabled: bool = False
    # Optional WDA HTTP base URL for the iOS surface. Typical values:
    # `http://127.0.0.1:8100` (with `pymobiledevice3 usbmux forward 8100 8100
    # --udid <UDID>` running, OR `iproxy 8100 8100` from libimobiledevice) —
    # OR `http://<iphone-wifi-ip>:8100` if the WDA build's Wi-Fi listener is
    # enabled. Empty = lockdown-only mode: mobile_list_devices works but
    # action verbs return a clear "WDA URL not configured" error.
    mobile_ios_wda_url: str = ""
    # Optional OmniParser enumeration backend. When set, the server stands up
    # an Enumerator client and registers the `enumerate_elements` tool (a flat
    # list of every detected icon/text box with pixel bboxes). Empty = feature
    # off, no tool registered — the model is an extra deployment not every host
    # runs. URL points at the OmniParser FastAPI `/parse` server (e.g.
    # http://parser.internal:8918). See conductor_desktop.enumerate.
    omniparser_url: str = ""
    omniparser_timeout: float = 30.0
    # Optional tool families. Each is a tooling.tool(gate=...) name: off means
    # the tools are absent from the surface, not present-but-erroring.
    # web_* drive a browser over CDP / BiDi; hosts that never open one can
    # drop the family.
    web_enabled: bool = True
    # exec_shell (and mobile_shell) hand the agent an arbitrary shell.
    # Locked-down deployments turn them off.
    shell_enabled: bool = True
    # describe_image reads an image file from disk as prose — for a
    # text-only agent with a webcam. Most agents read pixels themselves.
    describe_image_enabled: bool = False
    # Diagnostic loop for the delta channel: one line every few seconds with
    # RSS, loop depth and per-observer buffer sizes, for investigating memory
    # growth. Off by default; its log goes to `delta_diag_file` because an MCP
    # host stops capturing stderr once the connection is up.
    delta_diag: bool = False
    delta_diag_file: str = ""

    @property
    def mobile_enabled(self) -> bool:
        """True when either phone transport is on — gates the mobile_* tools."""
        return self.mobile_android_enabled or self.mobile_ios_enabled

    @property
    def mobile_shell_enabled(self) -> bool:
        return self.mobile_enabled and self.shell_enabled

    @property
    def enumerate_enabled(self) -> bool:
        """True when `enumerate_elements` has a backend: a self-hosted
        OmniParser, or managed mode, where the gateway runs the parser."""
        return self.omniparser_enabled or self.managed

    @property
    def omniparser_enabled(self) -> bool:
        """True when an OmniParser URL is configured — gates the Enumerator
        client and the optional `enumerate_elements` tool."""
        return bool(self.omniparser_url)

    @property
    def grounder_configured(self) -> bool:
        """True when a grounder URL is set. Without one the server still starts:
        tools that find things by description (locate, click, crop, wait_for)
        answer with an error that names what's missing and what works instead."""
        return bool(self.grounder_url)

    @property
    def describe_configured(self) -> bool:
        """True when read_screen / describe_image have a backend."""
        return bool(self.effective_describe_url)

    @property
    def region_configured(self) -> bool:
        """True when crop / locate(multiple) have a bbox-capable backend."""
        return bool(self.effective_region_url)

    @property
    def managed(self) -> bool:
        """True in SaaS mode — grounder_url is the gateway, not a raw VLM. The
        server builds a ManagedGrounder and skips all ensemble/role wiring."""
        return self.grounder_family == "managed"

    @property
    def effective_describe_url(self) -> str:
        """URL the describe backend talks to: describe_url if set, else the
        ground backend's URL (single-server / back-compat path)."""
        return self.describe_url or self.grounder_url

    @property
    def effective_describe_family(self) -> str:
        """Family the describe backend speaks: describe_family if set, else
        the ground family (single-server / back-compat path)."""
        return self.describe_family or self.grounder_family

    @property
    def describe_backend_is_separate(self) -> bool:
        """True when describe routes to a different (url, family) than ground,
        so the server builds a second Grounder instead of reusing one."""
        return (self.effective_describe_url != self.grounder_url
                or self.effective_describe_family != self.grounder_family)

    @property
    def effective_region_url(self) -> str:
        """URL the region backend (crop, locate.bbox, N-match) talks to.

        Region ops need a real bbox extent. When the click grounder is
        point-only (Holo) it can't produce one, so regions route to the
        describe backend — which in every supported point-only ensemble is a
        mandatory, bbox-capable Qwen3-VL/Gemma server. Otherwise the ground
        backend does boxes itself (qwen/gemma/LocateAnything) and serves
        regions directly. Mirrors server.py's `region_grounder` resolution.

        With no grounder, regions go to the describe backend when its family
        can ground (qwen, gemma4), else nowhere."""
        if not self.grounder_url:
            can_box = family_can_ground(self.effective_describe_family) and \
                not family_is_point_only(self.effective_describe_family)
            return self.effective_describe_url if can_box else ""
        if family_is_point_only(self.grounder_family):
            return self.effective_describe_url
        return self.grounder_url

    @property
    def effective_region_family(self) -> str:
        """Family the region backend speaks — see `effective_region_url`."""
        if not self.grounder_url or family_is_point_only(self.grounder_family):
            return self.effective_describe_family
        return self.grounder_family

    @classmethod
    def from_env(cls) -> MCPConfig:
        # Optional: without a grounder the server still starts, and the tools
        # that need one say so when called.
        grounder = os.environ.get("CONDUCTOR_DESKTOP_GROUNDER_URL", "").rstrip("/")
        transport = _env_choice("CONDUCTOR_DESKTOP_TRANSPORT", "stdio", _VALID_TRANSPORTS)
        host = os.environ.get("CONDUCTOR_DESKTOP_HOST", "127.0.0.1")
        auth_token = _auth_token_from_env()
        serves_http = transport != "stdio" or _env_bool("CONDUCTOR_DESKTOP_DASHBOARD", False)
        if serves_http and not auth_token and not is_loopback(host):
            raise RuntimeError(
                f"CONDUCTOR_DESKTOP_HOST={host!r} serves this desktop to the network, so "
                "it needs a token: set CONDUCTOR_DESKTOP_AUTH_TOKEN_FILE (or "
                "CONDUCTOR_DESKTOP_AUTH_TOKEN) and give clients 'Authorization: "
                "Bearer <token>'. Or bind 127.0.0.1 and tunnel with ssh -L.")
        screenshot_mode = _env_choice("CONDUCTOR_DESKTOP_SCREENSHOT_MODE", "inline",
                                      _VALID_SCREENSHOT_MODES)
        audit_args = _env_choice("CONDUCTOR_DESKTOP_AUDIT_ARGS", "redacted", _VALID_AUDIT_ARGS)
        backend = _env_choice("CONDUCTOR_DESKTOP_BACKEND", "resident", _VALID_BACKENDS)
        grounder_family = _env_choice(
            "CONDUCTOR_DESKTOP_GROUNDER_FAMILY", "qwen",
            _VALID_GROUNDER_FAMILIES | {"managed"},
            extra=" 'managed' is the conductor-grounder SaaS gateway.")
        # The point/click backend must be able to ground. A describe-only family
        # (minicpm) is a valid family name but has no grounding dialect, so
        # setting it as GROUNDER_FAMILY is a config error — catch it here, not at
        # the agent's first click. (It belongs on DESCRIBE_FAMILY instead.)
        if grounder and grounder_family != "managed" and not family_can_ground(grounder_family):
            raise RuntimeError(
                f"CONDUCTOR_DESKTOP_GROUNDER_FAMILY={grounder_family!r} is a "
                f"describe-only family and cannot serve ground/locate/click. "
                f"Use it as CONDUCTOR_DESKTOP_DESCRIBE_FAMILY with a grounding "
                f"family (qwen, holo, venus, ...) as the point backend."
            )
        # Managed (SaaS) mode short-circuits the whole ensemble story: the
        # gateway owns family routing, the describe/region split, and the
        # license gate, so none of the per-family validation below applies.
        # We only need the seat token and the norm1000 invariant.
        coord_env = os.environ.get("CONDUCTOR_DESKTOP_COORD_SYSTEM", "").strip().lower()
        if coord_env and coord_env not in (CoordSpace.NORM1000, CoordSpace.IMAGE_PIXELS):
            raise RuntimeError(
                f"CONDUCTOR_DESKTOP_COORD_SYSTEM={coord_env!r} is not a coordinate space; "
                f"use 'norm1000' or 'image_pixels', or leave it unset.")
        coord_system = CoordSpace(coord_env) if coord_env else None
        api_key = ""
        if grounder_family == "managed":
            if not grounder:
                raise RuntimeError(
                    "managed mode (CONDUCTOR_DESKTOP_GROUNDER_FAMILY=managed) needs "
                    "CONDUCTOR_DESKTOP_GROUNDER_URL — the conductor-grounder gateway URL."
                )
            api_key = os.environ.get("CONDUCTOR_DESKTOP_API_KEY", "")
            if not api_key:
                raise RuntimeError(
                    "managed mode (CONDUCTOR_DESKTOP_GROUNDER_FAMILY=managed) needs "
                    "CONDUCTOR_DESKTOP_API_KEY — your cgr_live_… seat token from the "
                    "conductor-grounder gateway."
                )
            if coord_system not in (None, CoordSpace.NORM1000):
                raise RuntimeError(
                    f"managed mode serves norm1000 coordinates; "
                    f"CONDUCTOR_DESKTOP_COORD_SYSTEM={coord_system} would mis-project "
                    f"clicks. Unset it or set it to 'norm1000'."
                )
        describe_url = os.environ.get("CONDUCTOR_DESKTOP_DESCRIBE_URL", "").rstrip("/")
        describe_family = os.environ.get("CONDUCTOR_DESKTOP_DESCRIBE_FAMILY", "").lower()
        if describe_family and describe_family not in _VALID_GROUNDER_FAMILIES:
            raise RuntimeError(
                f"CONDUCTOR_DESKTOP_DESCRIBE_FAMILY={describe_family!r} invalid. "
                f"Choose one of: {sorted(_VALID_GROUNDER_FAMILIES)}"
            )
        # The describe backend must speak a describe-capable family. This
        # catches the headline case: GROUNDER_FAMILY=locateanything with no
        # separate describe backend — fail at startup with the fix, not at the
        # agent's first read_screen() call.
        eff_describe_family = describe_family or grounder_family
        describe_backend = describe_url or grounder
        if describe_backend and grounder_family != "managed" and not family_can_describe(eff_describe_family):
            if describe_family:
                raise RuntimeError(
                    f"CONDUCTOR_DESKTOP_DESCRIBE_FAMILY={describe_family!r} is a "
                    f"grounding-only family and cannot serve describe. "
                    f"Choose a describe-capable family (e.g. qwen)."
                )
            raise RuntimeError(
                f"CONDUCTOR_DESKTOP_GROUNDER_FAMILY={grounder_family!r} is a "
                f"grounding-only model and cannot serve describe. Point "
                f"describe at a Qwen3-VL server: set CONDUCTOR_DESKTOP_DESCRIBE_URL "
                f"(e.g. http://qwen.internal:8000) and "
                f"CONDUCTOR_DESKTOP_DESCRIBE_FAMILY=qwen."
            )
        # A point-only click grounder (Holo) needs a bbox-capable backend for
        # region ops (crop, locate.bbox, N-match) — it routes those to the
        # describe backend. Today this is auto-satisfied (describe must be
        # qwen/gemma, both do boxes), but assert it explicitly so a future
        # point-only describe family can't silently break crop. eff_region_family
        # mirrors MCPConfig.effective_region_family.
        if grounder and grounder_family != "managed" and family_is_point_only(grounder_family):
            eff_region_family = eff_describe_family
            if family_is_point_only(eff_region_family):
                raise RuntimeError(
                    f"CONDUCTOR_DESKTOP_GROUNDER_FAMILY={grounder_family!r} is a "
                    f"point-only grounder (click point, no bbox extent), so "
                    f"region ops (crop, locate.bbox) route to the describe "
                    f"backend — but that backend ({eff_region_family!r}) is also "
                    f"point-only. Point describe/region at a bbox-capable "
                    f"Qwen3-VL or Gemma server (qwen|gemma4)."
                )
        if os.environ.get("CONDUCTOR_DESKTOP_PAYLOAD_MAX_WIDTH", "").strip():
            # It capped the short edge at 540 to dodge an old Claude Code
            # payload limit; the shrink it caused is what made images and
            # click coordinates disagree. Fail rather than guess a mapping.
            raise RuntimeError(
                "CONDUCTOR_DESKTOP_PAYLOAD_MAX_WIDTH is gone. Use "
                "CONDUCTOR_DESKTOP_PAYLOAD_MAX_EDGE, which caps the long edge "
                "(default 1568), or unset it."
            )
        default_dir = str(state_dir() / "screenshots")
        screenshot_dir = os.environ.get("CONDUCTOR_DESKTOP_SCREENSHOT_DIR", default_dir)
        return cls(
            grounder_url=grounder,
            backend=backend,
            connection_uri=os.environ.get("CONDUCTOR_DESKTOP_CONNECTION_URI", ""),
            platform=os.environ.get("CONDUCTOR_DESKTOP_PLATFORM", ""),
            auto_screenshot=_env_bool("CONDUCTOR_DESKTOP_AUTO_SCREENSHOT", False),
            text_only=_env_bool("CONDUCTOR_DESKTOP_TEXT_ONLY", False),
            screenshot_max_width=_env_int("CONDUCTOR_DESKTOP_SCREENSHOT_MAX_WIDTH", 0),
            payload_max_edge=_env_int("CONDUCTOR_DESKTOP_PAYLOAD_MAX_EDGE", 1568),
            coord_system=coord_system,
            grounder_family=grounder_family,
            describe_url=describe_url,
            describe_family=describe_family,
            api_key=api_key,
            grounder_timeout=_env_float("CONDUCTOR_DESKTOP_GROUNDER_TIMEOUT", 30.0),
            transport=transport,
            host=host,
            port=_env_int("CONDUCTOR_DESKTOP_PORT", 8765),
            auth_token=auth_token,
            screenshot_mode=screenshot_mode,
            screenshot_dir=screenshot_dir,
            cdp_host=os.environ.get("CONDUCTOR_DESKTOP_CDP_HOST", "127.0.0.1"),
            cdp_port=_env_int("CONDUCTOR_DESKTOP_CDP_PORT", 9222),
            cdp_default_tab_filter=os.environ.get("CONDUCTOR_DESKTOP_CDP_TAB_FILTER", ""),
            browsers=os.environ.get("CONDUCTOR_DESKTOP_BROWSERS", ""),
            default_browser=os.environ.get("CONDUCTOR_DESKTOP_DEFAULT_BROWSER", ""),
            tool_timing=_env_bool("CONDUCTOR_DESKTOP_TOOL_TIMING", False),
            dashboard=_env_bool("CONDUCTOR_DESKTOP_DASHBOARD", False),
            lock_path=os.environ.get("CONDUCTOR_DESKTOP_LOCK_PATH", ""),
            audit=_env_bool("CONDUCTOR_DESKTOP_AUDIT", True),
            audit_dir=os.environ.get("CONDUCTOR_DESKTOP_AUDIT_DIR", ""),
            audit_args=audit_args,
            audit_retention_days=_env_int("CONDUCTOR_DESKTOP_AUDIT_RETENTION_DAYS", 14),
            deltas_enabled=_env_bool("CONDUCTOR_DESKTOP_DELTAS", False),
            mobile_android_enabled=_env_bool("CONDUCTOR_DESKTOP_MOBILE_ANDROID", False),
            mobile_ios_enabled=_env_bool("CONDUCTOR_DESKTOP_MOBILE_IOS", False),
            mobile_ios_wda_url=os.environ.get("CONDUCTOR_DESKTOP_MOBILE_IOS_WDA_URL", "").rstrip("/"),
            omniparser_url=os.environ.get("CONDUCTOR_DESKTOP_OMNIPARSER_URL", "").rstrip("/"),
            omniparser_timeout=_env_float("CONDUCTOR_DESKTOP_OMNIPARSER_TIMEOUT", 30.0),
            web_enabled=_env_bool("CONDUCTOR_DESKTOP_WEB", True),
            shell_enabled=_env_bool("CONDUCTOR_DESKTOP_SHELL", True),
            describe_image_enabled=_env_bool("CONDUCTOR_DESKTOP_DESCRIBE_IMAGE", False),
            delta_diag=_env_bool("CONDUCTOR_DESKTOP_DELTA_DIAG", False),
            delta_diag_file=os.environ.get("CONDUCTOR_DESKTOP_DELTA_DIAG_FILE",
                                           "/tmp/conductor-desktop-delta-diag.log"),
        )
