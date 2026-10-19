"""Vercel function: GET /api/sets (see warframe_set_arbitrage.web)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from warframe_set_arbitrage.web import ApiHandler  # noqa: E402


class handler(ApiHandler):  # noqa: N801 (Vercel looks for this name)
    pass
