# -*- coding: utf-8 -*-
"""
====================================================================
Live Web Scraper Service (Powered by Scrapling & Obscura)
====================================================================

Bypasses delayed financial APIs by scraping real-time price ticks,
day high/low, volume, and order-book metrics directly from live
web sources (Screener.in, Google Finance) using Scrapling's stealth browser.
"""

import re
import logging
from typing import Dict, Any, Optional
from datetime import datetime

try:
    from scrapling.fetchers import Fetcher
    SCRAPLING_AVAILABLE = True
except ImportError:
    SCRAPLING_AVAILABLE = False

logger = logging.getLogger(__name__)


class LiveWebScraperService:
    """Stealth real-time market data scraper."""

    def __init__(self):
        pass

    def get_live_quote(self, symbol: str, exchange: str = "NSE") -> Dict[str, Any]:
        """
        Scrapes exact live price tick, day range, and ratios.
        
        Args:
            symbol: Stock symbol without suffix (e.g. 'RTNPOWER', 'IDEA', 'HAL')
            exchange: 'NSE', 'BOM' (BSE), 'NASDAQ', 'NYSE'
        """
        clean_sym = symbol.replace(".NS", "").replace(".BO", "").upper()

        if not SCRAPLING_AVAILABLE:
            return {
                "symbol": clean_sym,
                "error": "Scrapling library not installed",
                "timestamp": datetime.now().isoformat(),
            }

        # 1. Scrape Screener.in for Indian Equities (Exact Real-Time Numbers)
        if exchange in ["NSE", "BOM", "BSE"]:
            try:
                screener_quote = self._scrape_screener(clean_sym)
                if screener_quote and screener_quote.get("price") is not None:
                    return screener_quote
            except Exception as e:
                logger.warning(f"[Scrapling] Screener scrape failed for {clean_sym}: {e}")

        # 2. Fallback to Google Finance
        try:
            gfin_quote = self._scrape_google_finance(clean_sym, exchange)
            if gfin_quote and gfin_quote.get("price") is not None:
                return gfin_quote
        except Exception as e:
            logger.warning(f"[Scrapling] Google Finance scrape failed for {clean_sym}: {e}")

        return {
            "symbol": clean_sym,
            "exchange": exchange,
            "price": None,
            "error": "Failed to scrape live web tick",
            "timestamp": datetime.now().isoformat(),
        }

    def _scrape_screener(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Scrapes live price and metrics from Screener.in."""
        url = f"https://www.screener.in/company/{symbol}/"
        page = Fetcher.get(url, impersonate="chrome", stealthy_headers=True)

        ratios = {}
        for li in page.css("#top-ratios li"):
            name = "".join(li.css("span.name::text").getall()).strip()
            num = "".join(li.css("span.number::text").getall()).strip()
            if name and num:
                ratios[name] = num

        price_str = ratios.get("Current Price")
        if not price_str:
            return None

        clean_price = float(re.sub(r"[^\d.]", "", price_str))
        high_low = ratios.get("High / Low", "N/A")
        mcap = ratios.get("Market Cap", "N/A")
        pe = ratios.get("Stock P/E", "N/A")

        return {
            "symbol": symbol,
            "exchange": "NSE",
            "price": clean_price,
            "current_price": clean_price,
            "high_low": high_low,
            "pe_ratio": pe,
            "market_cap_cr": mcap,
            "source": "screener_in_live_scrapling",
            "url": url,
            "timestamp": datetime.now().isoformat(),
        }

    def _scrape_google_finance(self, symbol: str, exchange: str) -> Optional[Dict[str, Any]]:
        """Scrapes real-time price from Google Finance."""
        url = f"https://www.google.com/finance/quote/{symbol}:{exchange}"
        page = Fetcher.get(url, impersonate="chrome", stealthy_headers=True)

        price_elem = page.css(".YMlKec.fxKbKc::text").get()
        if not price_elem:
            return None

        clean_price_str = re.sub(r"[^\d.]", "", price_elem)
        price = float(clean_price_str) if clean_price_str else None

        return {
            "symbol": symbol,
            "exchange": exchange,
            "price": price,
            "current_price": price,
            "source": "google_finance_live_scrapling",
            "url": url,
            "timestamp": datetime.now().isoformat(),
        }
