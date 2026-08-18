# -*- coding: utf-8 -*-
"""Shared quantitative helpers used by more than one service.

Extracted so callers (kronos_service, intraday_signal_engine, …) share one
implementation instead of drifting apart over time.
"""


def compute_circuit_buffer_pct(current_price: float, stock_code: str) -> float:
    """Distance (in %) from current price down to the estimated lower circuit.

    NSE/BSE names and any sub-₹20 stock use a 5% circuit band; everything
    else uses 10%. Matches the convention already used in kronos_service's
    forecast output.
    """
    circuit_band_pct = 5.0 if (current_price < 20.0 or stock_code.endswith((".NS", ".BO"))) else 10.0
    lower_circuit_est = current_price * (1.0 - circuit_band_pct / 100.0)
    if current_price <= 0:
        return 0.0
    return ((current_price - lower_circuit_est) / current_price) * 100.0
