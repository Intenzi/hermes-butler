"""Pytest setup for this plugin repo.

The repository root has an ``__init__.py`` but its directory name is not a
valid Python identifier (the worktree suffix contains a hyphen). Pytest 9
therefore tries to import the root ``__init__.py`` as a top-level ``__init__``
module before running tests, which fails on its relative imports. Registering
the root package under that fallback name keeps collection focused on the test
modules instead of importing the whole plugin.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if "__init__" not in sys.modules:
    root_package = types.ModuleType("__init__")
    root_package.__file__ = str(ROOT / "__init__.py")
    root_package.__package__ = ""
    root_package.__path__ = [str(ROOT)]
    sys.modules["__init__"] = root_package
