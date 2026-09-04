"""Test bootstrap: make the repo tree win over the installed wheel.

The node package is pip-installed into .venv (the live node service runs
from it), so plain ``pytest`` would import stale site-packages code.
Prepend the repo root instead — test-only, never touches the install.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))