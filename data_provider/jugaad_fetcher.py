# -*- coding: utf-8 -*-
"""
JugaadDataFetcher — NSE (India) daily-bar fallback (Priority 6)

Data source: jugaad-data (NSE bhavcopy-derived historical data, no API key required)
Markets: NSE (`.NS` suffix) only — BSE (`.BO`) is not covered by this fetcher.

Historical bars are typically 1-2 trading days behind (bhavcopy publish lag),
so this is a daily-bar fallback for YfinanceFetcher, not a live-quote source.
"""

import logging
import os
from datetime import datetime
from typing import Optional

import pandas as pd

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.services.market_symbol_utils import get_suffix_market

logger = logging.getLogger(__name__)


def _positive_or_none(value):
    """NSELive returns 0 for an empty order-book side, not a missing field —
    treat 0 as "no order at that level", not a real price/qty of zero."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


class JugaadDataFetcher(BaseFetcher):
    name = "JugaadDataFetcher"
    priority = int(os.getenv("JUGAAD_PRIORITY", "6"))

    def __init__(self):
        self._nse_live = None

    def _get_nse_live(self):
        if self._nse_live is None:
            from jugaad_data.nse import NSELive

            self._nse_live = NSELive()
        return self._nse_live

    def _is_nse_suffix_stock(self, stock_code: str) -> bool:
        return get_suffix_market(stock_code) == "in" and stock_code.strip().upper().endswith(".NS")

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        if not self._is_nse_suffix_stock(stock_code):
            return None

        symbol = stock_code.strip().upper()[:-len(".NS")]
        try:
            self.random_sleep(0.1, 0.3)
            q = self._get_nse_live().stock_quote(symbol)
        except Exception as e:
            logger.debug(f"[JugaadData] realtime quote failed for {symbol}: {e}")
            return None

        if not q:
            return None

        order_book = q.get("orderBook") or {}
        meta = q.get("metaData") or {}
        trade = q.get("tradeInfo") or {}

        price = _positive_or_none(order_book.get("lastPrice"))
        if price is None:
            return None

        return UnifiedRealtimeQuote(
            code=stock_code,
            name=str(meta.get("companyName") or "").strip(),
            source=RealtimeSource.JUGAAD_NSE,
            price=price,
            change_pct=meta.get("pChange"),  # signed: can legitimately be negative or zero
            change_amount=meta.get("change"),  # signed: can legitimately be negative or zero
            open_price=_positive_or_none(meta.get("open")),
            high=_positive_or_none(meta.get("dayHigh")),
            low=_positive_or_none(meta.get("dayLow")),
            pre_close=_positive_or_none(meta.get("previousClose")),
            volume=int(trade["totalTradedVolume"]) if trade.get("totalTradedVolume") else None,
            amount=trade.get("totalTradedValue"),
            bid_price=_positive_or_none(order_book.get("buyPrice1")),
            bid_qty=int(order_book["buyQuantity1"]) if _positive_or_none(order_book.get("buyQuantity1")) else None,
            ask_price=_positive_or_none(order_book.get("sellPrice1")),
            ask_qty=int(order_book["sellQuantity1"]) if _positive_or_none(order_book.get("sellQuantity1")) else None,
        )

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        if not self._is_nse_suffix_stock(stock_code):
            raise DataFetchError(f"[JugaadData] {stock_code} is not an NSE (.NS) stock")

        symbol = stock_code.strip().upper()[:-len(".NS")]
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()

        try:
            from jugaad_data.nse import stock_df

            self.random_sleep(0.3, 0.8)
            df = stock_df(symbol=symbol, from_date=start_dt, to_date=end_dt, series="EQ")
        except DataFetchError:
            raise
        except Exception as e:
            raise DataFetchError(f"[JugaadData] fetch failed for {symbol}: {e}") from e

        if df is None or df.empty:
            raise DataFetchError(f"[JugaadData] no data returned for {symbol}")

        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        if df.empty:
            return df

        df = df.copy()
        df["date"] = pd.to_datetime(df["DATE"]).dt.date
        df = df.rename(columns={
            "OPEN": "open", "HIGH": "high", "LOW": "low",
            "CLOSE": "close", "VOLUME": "volume", "VALUE": "amount",
        })
        df["pct_chg"] = df["close"].pct_change() * 100
        df["pct_chg"] = df["pct_chg"].fillna(0).round(2)
        df["code"] = stock_code

        keep = ["code"] + STANDARD_COLUMNS
        df = df[[col for col in keep if col in df.columns]]
        return df
