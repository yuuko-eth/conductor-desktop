"""The ``instructions`` this server sends in its MCP ``initialize`` reply.

Clients that honour them (Claude Code puts them in the system prompt) show
them on every turn, even while the tools themselves are deferred and only
their names are visible. Clients that drop them (Claude Desktop) still get
each tool's description, so nothing here may be the *only* place a rule
lives: a tool's first paragraph and its errors carry what that tool needs.
This text is the part no single tool owns: which channel to reach for, and
the loop around them.

Keep it under 2048 characters, where Claude Code cuts it off (the tests
cap it there), true on every platform and config, and free of anything
about one host: that belongs in the user's CLAUDE.md / AGENTS.md (see
examples/operator-briefing/).
"""

INSTRUCTIONS = """\
conductor-desktop drives this computer's real desktop: screen, pointer, \
keyboard, windows, native app controls, browser tabs and, when enabled, \
phones, in the user's actual session.

Work in a loop: look, act, look again. A tool returning ok means the input \
was sent, not that it landed where you meant. A result may end in a \
[deltas ...] block of what changed since the last call (navigations, focus, \
dialogs, values), which confirms an action without another screenshot.

Pick the channel by what you're driving:
1. Native app controls: ui_find (or ui_tree) for a ref, then ui_press, \
ui_set_value, ui_toggle, ui_select, ui_expand, ui_focus or ui_scroll_to. No \
pointer, and a stale ref fails instead of acting, so prefer it for anything \
destructive.
2. Web pages in Chrome or Firefox: web_list_tabs, then web_eval or web_crop; \
activate_tab first, as a background tab may render blank.
3. Anything else (canvases, games, remote desktops, empty trees): pixels.

Seeing: read_screen is a cheap text description; capture_screen returns the \
image. locate, click, crop and wait_for find things by description through a \
grounder model. Without models those tools say so: look with capture_screen \
and act with click_at, drag_at, scroll_at or mouse_move_at, passing \
image_coords=true for pixels read off a shrunk screenshot.

Describe grounder targets as "{thing} in {place}", e.g. "Save button in the \
Export dialog". If several match, the call refuses and lists them.

list_windows(match=...) shows what's open, focus_window raises one; menu and \
dialog contents are in ui_tree. list_screens and screen= for monitors. \
Phones: mobile_list_devices, then device_id= on every mobile_* call.

Habits: dismiss pop-ups first; prefer wait_for over fixed waits; in search \
boxes Enter often picks the first suggestion, so click the result; shortcuts \
and exec_shell (when enabled) beat clicking through menus. Calls go to an \
audit log on the user's machine: keep secrets out of arguments.
"""
