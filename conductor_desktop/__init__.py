"""Conductor Desktop — desktop/mobile/web automation primitives + a Qwen3-VL
grounder, served over the Model Context Protocol."""

import os as _os

ENV_PREFIX = "CONDUCTOR_DESKTOP_"
# The variables were CONDUCTOR_MCP_* before the rename. Move any that are
# still set onto the new name (an explicitly set new name wins) at import,
# before anything reads them, so every read site only knows one spelling.
# Moving rather than copying keeps the token variables out of the
# environment that exec_shell inherits once config pops the new names.
LEGACY_ENV_PREFIX = "CONDUCTOR_MCP_"
LEGACY_ENV_RENAMED: list[str] = []
for _old in [k for k in _os.environ if k.startswith(LEGACY_ENV_PREFIX)]:
    _value = _os.environ.pop(_old)
    _new = ENV_PREFIX + _old[len(LEGACY_ENV_PREFIX):]
    _os.environ.setdefault(_new, _value)
    LEGACY_ENV_RENAMED.append(_old)
