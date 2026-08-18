# -*- coding: utf-8 -*-
"""
Real NSE-wide market discovery.

Closes the gap the Subex/Mysore-Petro episode exposed: this pipeline had
zero mechanism to see any stock outside a hand-typed watchlist. Uses
jugaad-data's NSELive.top_stocks() - NSE's own live snapshot endpoint,
already the fetcher this repo trusts for order-book depth (see
data_provider/jugaad_fetcher.py) - not a scrape, not a guess.

NSE's own response has a typo (`topLoosers`) and several categories that
come back empty outside active market hours (mostActiveValue/Volume,
volumeSpurtsValue) - this module normalizes the typo and treats an empty
category as empty, never fabricated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_CATEGORY_KEY_MAP = {
    "top_gainers": "topGainers",
    "top_losers": "topLoosers",  # NSE's own typo, normalized on our side
    "most_active_by_value": "mostActiveValue",
    "most_active_by_volume": "mostActiveVolume",
    "volume_spurts": "volumeSpurtsValue",
}


@dataclass
class NseMover:
    symbol: str          # e.g. "SUBEXLTD"
    ns_symbol: str        # e.g. "SUBEXLTD.NS"
    last_price: float
    pchange: float
    total_traded_volume: float
    total_traded_value: float


@dataclass
class NseUniverseSnapshot:
    timestamp: str
    categories: Dict[str, List[NseMover]] = field(default_factory=dict)
    source: str = "NSELive.top_stocks"

    def get(self, category: str) -> List[NseMover]:
        return self.categories.get(category, [])


def _to_mover(raw: dict) -> Optional[NseMover]:
    symbol = raw.get("symbol")
    last_price = raw.get("lastPrice")
    if not symbol or last_price is None:
        return None
    try:
        return NseMover(
            symbol=symbol,
            ns_symbol=f"{symbol}.NS",
            last_price=float(last_price),
            pchange=float(raw.get("pchange", 0.0) or 0.0),
            total_traded_volume=float(raw.get("totalTradedVolume", 0.0) or 0.0),
            total_traded_value=float(raw.get("totalTradedValue", 0.0) or 0.0),
        )
    except (TypeError, ValueError):
        return None


def fetch_nse_universe_snapshot(nse_live=None) -> Optional[NseUniverseSnapshot]:
    """Real snapshot of NSE-wide gainers/losers/most-active. None on any failure.

    Callers must treat None as "no discovery data this run", never
    substitute a cached/synthetic universe.
    """
    try:
        if nse_live is None:
            from jugaad_data.nse import NSELive

            nse_live = NSELive()
        raw = nse_live.top_stocks()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[NseUniverseDiscovery] top_stocks() fetch failed: %s", exc)
        return None

    if not isinstance(raw, dict):
        logger.warning("[NseUniverseDiscovery] unexpected response type: %s", type(raw))
        return None

    categories: Dict[str, List[NseMover]] = {}
    for our_key, nse_key in _CATEGORY_KEY_MAP.items():
        entries = raw.get(nse_key) or []
        movers = [m for m in (_to_mover(e) for e in entries) if m is not None]
        categories[our_key] = movers
        if not movers:
            logger.debug("[NseUniverseDiscovery] category '%s' empty this run (may be outside active session)", our_key)

    return NseUniverseSnapshot(timestamp=str(raw.get("timestamp", "")), categories=categories)


def discovered_symbols(
    snapshot: NseUniverseSnapshot,
    categories: Optional[List[str]] = None,
    top_n_per_category: int = 10,
) -> List[str]:
    """Deduped `.NS` symbols from the requested categories, ranked order preserved within each."""
    categories = categories or ["top_gainers", "top_losers", "most_active_by_value"]
    seen: Dict[str, None] = {}
    for cat in categories:
        for mover in snapshot.get(cat)[:top_n_per_category]:
            seen.setdefault(mover.ns_symbol, None)
    return list(seen.keys())
