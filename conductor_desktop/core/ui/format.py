"""Outline and JSON renderings of a ``UIRead``."""

from __future__ import annotations

import json

from .model import UINode, UIRead

OUTLINE_VALUE_MAX = 80
OUTLINE_NAME_MAX = 120
JSON_VALUE_MAX = 500


def _quote(text: str, limit: int) -> str:
    if len(text) > limit:
        text = text[:limit] + "…"
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "") + '"'


def target_label(read: UIRead) -> str:
    if read.window is None:
        target = "no window"
    elif read.window_title:
        target = f'window "{read.window_title}"'
    else:
        # Untitled windows such as a Save sheet: fall back to the root's own label.
        root = read.window
        target = f'{root.role} "{root.name}"' if root.name else f"untitled {root.role}"
    return f"{read.app} (pid {read.pid}) — {target}"


def unresponsive_note(read: UIRead) -> str:
    """Apps left out of this read because they stopped answering."""
    if not read.unresponsive_apps:
        return ""
    return (f" — {len(read.unresponsive_apps)} unresponsive app(s) skipped: "
            f"{', '.join(read.unresponsive_apps)}")


def _header(read: UIRead) -> str:
    line = f"{target_label(read)} — {read.nodes} nodes in {read.elapsed:.2f}s"
    if read.budget_hit:
        line += f" — walk budget hit after {read.timeout:g}s, partial tree"
    return line + unresponsive_note(read)


def node_line(node: UINode) -> str:
    """One element as an outline line, without indentation."""
    parts = [node.role]
    if node.title:
        parts.append(_quote(node.title, OUTLINE_NAME_MAX))
    if node.ref:
        parts.append(f"[{node.ref}]")
    parts.extend(v for v in node.verbs if v != "scroll_to")
    parts.extend(node.states)
    if node.bounds and node.actionable:
        parts.append("@" + ",".join(str(n) for n in node.bounds))
    if node.value and node.value != node.title:
        parts.append(_quote(node.value, OUTLINE_VALUE_MAX))
    return " ".join(parts)


def _silent(node: UINode) -> bool:
    """Nothing to say on its own line: no name, value or verbs."""
    return not (node.title or node.value or node.actionable)


def _outline(node: UINode, indent: int, out: list[str], is_root: bool = False) -> None:
    if node.is_label and not node.children and not node.truncated:
        return  # already shown as its control's label
    if not is_root and _silent(node) and not node.truncated:
        if not node.children:
            return  # empty wrapper
        if len(node.children) == 1:
            _outline(node.children[0], indent, out)  # fold the wrapper away
            return
    out.append("  " * indent + node_line(node))
    pad = "  " * (indent + 1)
    for child in node.children:
        _outline(child, indent + 1, out)
    if node.budget_skipped:
        out.append(f"{pad}…{node.budget_skipped} more children not read (walk budget)")
    if node.omitted_children:
        out.append(f"{pad}…{node.omitted_children} more children (max_children)")
    if node.unwalked_children:
        out.append(f"{pad}…{node.unwalked_children} children below the depth limit (depth)")
    if node.menubar_skipped:
        out.append(f"{pad}…{node.menubar_skipped} menu bar items not read (include_menubar=true)")


def format_outline(read: UIRead) -> str:
    out = [_header(read)]
    if read.window is not None:
        _outline(read.window, 0, out, is_root=True)
    if read.menubar is not None:
        out.append("menu bar:")
        _outline(read.menubar, 0, out, is_root=True)
    return "\n".join(out)


def node_to_dict(node: UINode) -> dict:
    d: dict = {"role": node.role, "native_role": node.native_role}
    if node.name:
        d["name"] = node.name
    if node.label:
        d["label"] = node.label
    if node.value:
        d["value"] = node.value[:JSON_VALUE_MAX]
    if node.id:
        d["id"] = node.id
    if node.ref:
        d["ref"] = node.ref
    if node.verbs:
        d["verbs"] = node.verbs
    if node.states:
        d["states"] = node.states
    if node.bounds:
        d["bounds"] = list(node.bounds)
    if node.row_range is not None:
        d["row_range"] = list(node.row_range)
    if node.col_range is not None:
        d["col_range"] = list(node.col_range)
    d["path"] = list(node.path)
    for key in ("omitted_children", "unwalked_children", "budget_skipped", "menubar_skipped"):
        if getattr(node, key):
            d[key] = getattr(node, key)
    if node.children:
        d["children"] = [node_to_dict(c) for c in node.children]
    return d


def format_json(read: UIRead) -> str:
    d: dict = {
        "app": read.app,
        "pid": read.pid,
        "window_id": read.window_id,
        "window_title": read.window_title,
        "nodes": read.nodes,
        "elapsed": round(read.elapsed, 3),
        "budget_hit": read.budget_hit,
        "window": node_to_dict(read.window) if read.window is not None else None,
    }
    if read.menubar is not None:
        d["menubar"] = node_to_dict(read.menubar)
    return json.dumps(d, ensure_ascii=False)
