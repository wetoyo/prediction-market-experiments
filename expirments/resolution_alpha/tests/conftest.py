"""The resolution_alpha modules use bare same-directory imports (`from config
import ...`, `from kalshi_gateway import ...`), so the package dir has to be on
sys.path for the tests to import them regardless of pytest's rootdir/CWD.
"""

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[1]
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
# sim_bankroll.py lives in ../../shared/ (order_manager.py adds it too).
_SHARED_DIR = _PKG_DIR.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.append(str(_SHARED_DIR))
