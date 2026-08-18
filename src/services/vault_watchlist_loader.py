# -*- coding: utf-8 -*-
"""
Shared vault-watchlist universe loader.

Originally written inline in 06-Scripts-Bridge/run_intraday_live_scanner.py;
extracted here so run_eod_pipeline.py (or anything else) can reuse the same
real universe instead of a second hardcoded/duplicated list.
"""

from __future__ import annotations

import glob
import os
import re
from typing import List

DEFAULT_VAULT_WATCHLISTS_DIR = "/Users/shubhammac/SSD/Obsidian/Daily Stock Analysis/Daily Stock Analysis/03-Watchlists"


def load_universe_from_watchlists(vault_watchlists_dir: str = DEFAULT_VAULT_WATCHLISTS_DIR) -> List[str]:
    """Every `.NS` ticker already curated in the vault's watchlist notes.

    This is watchlist-scoped, not NSE-market-wide. Broad NSE discovery is a
    separate, unbuilt piece of work - not folded into this loader.
    """
    symbols = set()
    for path in glob.glob(os.path.join(vault_watchlists_dir, "*.md")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        for match in re.findall(r"`([A-Z0-9&]+\.NS)`", content):
            symbols.add(match)
    return sorted(symbols)
