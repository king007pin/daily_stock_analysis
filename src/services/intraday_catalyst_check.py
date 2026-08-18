# -*- coding: utf-8 -*-
"""
Real news-catalyst check for the intraday scanner.

Replaces the previous `has_news_catalyst=True` hardcode. That hardcode
mattered more than cosmetics: MultiAgentSwarmService's consensus rule is
`if volume_surge_ratio >= 1.5 or has_news_catalyst:` (multi_agent_swarm_
service.py) - forcing this True unconditionally short-circuited that OR on
every single call, inflating buy-approval odds regardless of real volume.

Deliberately does NOT reuse the full per-stock Gemini qualitative analysis
(the Phase 04 pattern in analyzer.py) - that's one LLM call per stock per
run and too expensive to spend on every scan cycle. Instead reuses the
already-configured SearchService (Tavily/Brave/...) and treats "at least
one recent, real result" as the catalyst signal.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.config import get_config
from src.search_service import SearchService

logger = logging.getLogger(__name__)

_search_service: Optional[SearchService] = None
_search_service_init_attempted = False


def _get_search_service() -> Optional[SearchService]:
    global _search_service, _search_service_init_attempted
    if _search_service_init_attempted:
        return _search_service
    _search_service_init_attempted = True
    try:
        config = get_config()
        service = SearchService(
            bocha_keys=config.bocha_api_keys,
            tavily_keys=config.tavily_api_keys,
            anspire_keys=config.anspire_api_keys,
            brave_keys=config.brave_api_keys,
            serpapi_keys=config.serpapi_keys,
            minimax_keys=config.minimax_api_keys,
            searxng_base_urls=config.searxng_base_urls,
            searxng_public_instances_enabled=config.searxng_public_instances_enabled,
            news_max_age_days=config.news_max_age_days,
            news_strategy_profile=getattr(config, "news_strategy_profile", "short"),
        )
        if not service.is_available:
            logger.warning("[IntradayCatalyst] no search provider configured - has_news_catalyst will default False")
            _search_service = None
            return None
        _search_service = service
    except Exception as exc:  # noqa: BLE001
        logger.warning("[IntradayCatalyst] search service init failed: %s", exc)
        _search_service = None
    return _search_service


def has_recent_news_catalyst(stock_code: str, stock_name: str) -> bool:
    """True only if a real, recent search result exists for this stock.

    False (not True) is the safe default on any failure or missing config -
    matches the swarm's OR-gate semantics: a false negative here just means
    the trade relies on volume_surge_ratio alone, which is correct if we
    have no real evidence of a catalyst.
    """
    service = _get_search_service()
    if service is None:
        return False

    try:
        response = service.search_stock_news(stock_code, stock_name, max_results=3)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[IntradayCatalyst] search failed for %s: %s", stock_code, exc)
        return False

    return bool(response.success and response.results)
