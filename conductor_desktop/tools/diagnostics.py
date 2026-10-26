"""Diagnostics tool — surface executor / backend / grounder / mobile / browser state.

One tool: ``diagnose``. Used when something fails unexpectedly or
preemptively to verify the server can actually see the screen and inject
input. The probe is read-only and never raises — every check that fails
shows up as a labelled FAIL line instead.
"""

from __future__ import annotations

import logging

from ..audit import summarize
from ..core import input_method
from ..grounder import family_is_point_only
from ..state import current
from ..tooling import tool

log = logging.getLogger(__name__)


async def _browser_status() -> str:
    """Probe each configured browser through the shared web registry, so a
    BiDi probe reuses the browser's single session instead of competing
    with the web_* tools for it."""
    import asyncio

    from ..browser import BROWSER_ERRORS
    from .web import _registry

    try:
        endpoints = _registry().endpoints()
    except BROWSER_ERRORS as e:
        return f"config error — {e}"
    probes = await asyncio.gather(*(ep.probe() for ep in endpoints),
                                  return_exceptions=True)
    parts = []
    for ep, res in zip(endpoints, probes):
        state = (f"not reachable at {ep.address}" if isinstance(res, BaseException)
                 else f"OK ({res})")
        parts.append(f"{ep.slug}={ep.kind} {state}")
    return "; ".join(parts)


# Per-family license. Three distinct buckets — do NOT collapse "Apache-2.0"
# into "commercial-safe": Gemma is commercially usable but ships under Google's
# own *Gemma Terms of Use*, NOT Apache-2.0, so an all-Apache-2.0 mandate
# excludes it even though `diagnose` still rates it commercial-safe.
#   Apache-2.0          → qwen (Qwen3-VL), holo (HCompany Holo-3.1)
#   Gemma Terms of Use  → gemma4 (commercial-OK, NOT Apache-2.0)
#   non-commercial      → locateanything (NVIDIA LocateAnything-3B)
_FAMILY_LICENSE = {
    "qwen": "Apache-2.0",
    "holo": "Apache-2.0",
    "gemma4": "Gemma Terms of Use",
    "locateanything": "non-commercial",
    # Managed mode routes to the gateway fleet; the actual model — and hence its
    # license — is server-side and NOT client-verifiable. Give it its own bucket
    # so it doesn't fall through to the non-commercial fail-safe default and
    # mislabel a managed seat as "non-commercial".
    "managed": "gateway-defined",
}

# Licenses that forbid commercial use (flip the `diagnose` safety verdict).
_NON_COMMERCIAL_LICENSES = {"non-commercial"}


def _family_license(family: str) -> str:
    """The accurate license string for a grounder family. Unknown families are
    treated as non-commercial — fail safe rather than wave through an unvetted
    model as Apache-2.0."""
    return _FAMILY_LICENSE.get(family, "non-commercial")


def _grounding_role_lines(cfg) -> list[str]:
    """The three-role grounding report: point / describe / region backends,
    each with its (url, family) and license, plus the detected ensemble and
    a commercial-safety verdict.

    point = the click/locate grounder; describe = prose backend; region = the
    bbox-extent backend (crop, locate.bbox, N-match). region aliases describe
    when the click grounder is point-only (Holo), else aliases point."""
    point_only = family_is_point_only(cfg.grounder_family)
    roles = [
        ("point", cfg.grounder_url, cfg.grounder_family, ""),
        ("describe", cfg.effective_describe_url, cfg.effective_describe_family,
         "" if cfg.describe_backend_is_separate else "shared with point"),
        ("region", cfg.effective_region_url, cfg.effective_region_family,
         "shared with describe" if point_only else "shared with point"),
    ]
    lines = []
    for label, url, family, shared in roles:
        if not url:
            lines.append(f"{label + ':':<10}not configured  [tools that need it say so when called]")
            continue
        tail = f"[{shared}]" if shared else f"[{_family_license(family)}]"
        lines.append(f"{label + ':':<10}{url} ({family})  {tail}")
    # Ensemble label: distinct families across the three roles, describe-first
    # (matches the §3 ensemble-matrix naming, e.g. "qwen+holo", "qwen+locateanything").
    # Only roles with a backend count: an unset URL's default family isn't a model.
    families: list[str] = []
    for f, url in ((cfg.effective_describe_family, cfg.effective_describe_url),
                   (cfg.grounder_family, cfg.grounder_url),
                   (cfg.effective_region_family, cfg.effective_region_url)):
        if url and f not in families:
            families.append(f)
    if not families:
        lines.append("ensemble: none (no grounding or describe backend; ui_*, web_* and "
                     "coordinate tools don't need one)")
        return lines
    ensemble = "+".join(families)
    licenses = {f: _family_license(f) for f in families}
    non_commercial = [f for f, lic in licenses.items()
                      if lic in _NON_COMMERCIAL_LICENSES]
    non_apache = [f for f, lic in licenses.items() if lic != "Apache-2.0"]
    if cfg.managed:
        # The gateway fleet owns model selection; we can't assert any license
        # (commercial-safe or otherwise) from the client side.
        verdict = "license determined by the managed gateway fleet (not client-verifiable)"
    elif non_commercial:
        verdict = (f"non-commercial ({', '.join(sorted(set(non_commercial)))} "
                   f"is non-commercial)")
    elif non_apache:
        # Commercially usable, but not the strict all-Apache-2.0 bar — name the
        # offenders and their licenses so an Apache-only deployment can exclude
        # them (gemma4 → Gemma Terms of Use).
        detail = ", ".join(f"{f} is {licenses[f]}" for f in sorted(set(non_apache)))
        verdict = f"commercial-safe, but not all Apache-2.0 ({detail})"
    else:
        verdict = "commercial-safe — all Apache-2.0"
    lines.append(f"ensemble: {ensemble}  → {verdict}")
    return lines


def _audit_line(st) -> str:
    """One line on the audit log, for the operator reading `diagnose`.

    Says where the file is and how much of each call's arguments it keeps,
    because "is this machine recording what the agent types?" is a question
    that deserves an answer without reading the config.
    """
    audit = getattr(st, "audit", None)
    if audit is None or not getattr(audit, "enabled", False):
        return "audit log: off (CONDUCTOR_DESKTOP_AUDIT=true to enable)"
    retention = (f"{audit.retention_days}d" if audit.retention_days else "kept forever")
    return (f"audit log: on → {audit.directory} "
            f"(args={audit.policy}, retention={retention}) — {summarize(audit.directory)}")


async def _input_method_line() -> str:
    im = await input_method.current()
    if im is None:
        return "input method: none detected"
    how = ("composes: bare letter and digit keys go to it; type_text gets past it"
           if im.composing else "plain keyboard")
    return f"input method: {im.describe()} — {how}"


@tool(kind="observing")
async def diagnose() -> str:
    """Report what this server can do right now: screen capture and input
    permissions (OK or FAIL with the reason), which grounding and describe
    models are configured, the screenshot mode, the audit log, browser
    connections for the web_* tools, phones, and which optional tool
    families are on. Call it when a tool fails unexpectedly, or first thing
    on a new machine. It only reads; a failed check is a FAIL line, never an
    error.

    On macOS, Screen Recording and Accessibility are granted in System
    Settings; after granting, fully quit and relaunch whatever launched this
    server so the grants apply.
    """
    st = current()
    perms = st.executor.permissions
    if not st.rolling_describe_hints:
        rolling = "(none — using generic prompt)"
    else:
        parts = []
        for key, val in st.rolling_describe_hints.items():
            label = "host" if key == "" else f"device:{key}"
            parts.append(f"{label}={val!r}")
        rolling = ", ".join(parts)
    browser_status = await _browser_status()
    lines = [
        f"backend: {st.config.backend}",
        f"platform: {st.executor.host.platform} — {st.executor.host.platform_reason}",
        *_grounding_role_lines(st.config),
        f"text_only: {st.config.text_only}",
        f"auto_screenshot: {st.config.auto_screenshot}",
        f"screenshot_mode: {st.config.screenshot_mode}",
        f"tool_timing: {'on' if st.config.tool_timing else 'off'}",
        _audit_line(st),
        f"rolling describe hint: {rolling}",
        (f"screenshot: {'OK' if perms.screenshot_works else 'FAIL'}"
         f" — {perms.screenshot_detail}"),
        (f"input: {'OK' if perms.input_works else 'FAIL'}"
         f" — {perms.input_detail}"),
        await _input_method_line(),
        (f"semantic backend: {'OK' if perms.semantic_works else 'n/a'}"
         f" — {perms.semantic_detail}"),
        f"browsers (web_* tools): {browser_status}",
    ]
    if st.mobile is not None:
        lines.extend(st.mobile.status_lines())
        try:
            devs = await st.mobile.list_devices()
            if devs:
                summary = ", ".join(
                    f"{d.device_id}({d.transport},{d.state}"
                    f"{',locked' if d.locked else ''})"
                    for d in devs
                )
                lines.append(f"mobile devices: {summary}")
            else:
                lines.append("mobile devices: (none connected)")
        except Exception as e:
            log.debug("mobile device probe failed", exc_info=True)
            lines.append(f"mobile devices: probe error — {type(e).__name__}: {e}")
    else:
        lines.append("mobile: disabled (set CONDUCTOR_DESKTOP_MOBILE_ANDROID/_IOS to enable)")
    cfg = st.config

    def on(flag: bool) -> str:
        return "on" if flag else "off"

    lines.append(
        "optional tool families: "
        f"web={on(cfg.web_enabled)}, exec_shell={on(cfg.shell_enabled)}, "
        f"describe_image={on(cfg.describe_image_enabled)}, "
        f"enumerate_elements={on(cfg.enumerate_enabled)}, "
        f"mobile={on(st.mobile is not None)}"
    )
    if not perms.ok:
        lines.append("")
        lines.append(
            "→ Fix permissions, then fully quit and relaunch the terminal "
            "(or whatever launched this MCP server) so the new grants apply."
        )
    return "\n".join(lines)
