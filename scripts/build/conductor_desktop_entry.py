"""Nuitka build entry-point shim.

Pointing Nuitka directly at ``conductor_desktop/__main__.py`` would make
``__main__`` the top-level module; relative imports like
``from .core.models.host_node import HostNode`` then fail at runtime with
"attempted relative import beyond top-level package". This shim sits one
level above so ``conductor_desktop`` stays the package root and every relative
import inside ``conductor_desktop.*`` resolves correctly.
"""
from conductor_desktop.server import run

if __name__ == "__main__":
    run()
