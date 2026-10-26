"""Operator dashboard — FastAPI/Starlette routes mounted on the
existing FastMCP uvicorn instance.

Wire-up: ``server.py`` calls ``dashboard.routes.register(mcp)`` once at
import time so the routes are registered on the FastMCP instance before
its HTTP transport spins up.
"""
