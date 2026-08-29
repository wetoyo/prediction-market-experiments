"""The resolution_alpha modules use bare same-directory imports (`from config
import ...`, `from kalshi_gateway import ...`), so the package dir has to be on
sys.path for the tests to import them regardless of pytest's rootdir/CWD.
"""

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[1]
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
