"""HostNode — which host an executor drives.

One per server: the resident host, this machine. The other host types are
names a config can still ask for, so ``create_executor`` can explain that
phones are driven through the ``mobile_*`` tools and that the remote-desktop
and VM executors were never built.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

HostType = Literal["resident", "vnc", "rdp", "vbox", "adb", "wda"]
HostStatus = Literal["online", "offline", "connecting", "error"]


class HostNode(BaseModel):
    """The host an executor is attached to."""

    uuid: str = Field(default_factory=lambda: str(_uuid.uuid4()))
    label: str
    host_type: HostType
    connection_uri: str = ""
    status: HostStatus = "offline"
    #: The platform route: linux-x11, linux-kde, macos, windows, ... Before
    #: connect, a requested one (CONDUCTOR_DESKTOP_PLATFORM, "" to detect); after,
    #: the one in use.
    platform: str = ""
    #: Why connect picked that route, for ``diagnose``.
    platform_reason: str = ""
    last_seen: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
