"""Make the project importable no matter where pytest is invoked from.

The package lives in ``src/`` (a src-layout, so ``src`` cannot be imported by
accident and the metrics layer cannot accidentally import its own test
fixtures), and the fixture builder is not a package either. Both directories
plus the tests directory are put on ``sys.path`` here rather than in each test
file, so a reviewer reads one place and never has to run pytest from a
particular directory to get a green run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent
for _path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fixtures import build_fact  # noqa: E402  (needs the path set up first)


@pytest.fixture
def fact():
    """A fresh copy of the hand-built fact frame, 6 rows / 5 orders."""
    return build_fact()
