"""Test setup.

The modules tested here (api, websocket, queries, parsers) don't import Home
Assistant, so they're loaded without running the package __init__ (which
does). Platform/coordinator tests need pytest-homeassistant-custom-component.
"""

from __future__ import annotations

from pathlib import Path
import sys
import types

PACKAGE = "tibber_app_bridge"
PACKAGE_DIR = Path(__file__).parents[1] / "custom_components" / PACKAGE

if PACKAGE not in sys.modules:
    module = types.ModuleType(PACKAGE)
    module.__path__ = [str(PACKAGE_DIR)]
    sys.modules[PACKAGE] = module
