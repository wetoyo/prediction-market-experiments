"""The shared modules use bare imports (`from sim_bankroll import ...`), so
../ and this directory (fake_kalshi.py) go on sys.path."""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _dir in (_HERE.parent, _HERE):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))
