#!/bin/bash
# Daily automation entrypoint for launchd (Phase 00 of the Quant Desk Roadmap).
#
# Usage: run_daily_analysis.sh premarket|eod|intraday
#
#   premarket — scoped to the NSE Sub-Rs10 intraday watchlist, timed to finish
#               before the 09:15 IST market open.
#   eod       — full STOCK_LIST run after market close, for swing analysis.
#   intraday  — live scanner (watchlist + real NSE-wide discovered movers via
#               NSELive.top_stocks()), intended to fire several times during
#               market hours (09:45/10:30/13:30/14:15 IST per the vault's own
#               Awesome-Intraday-Engine-Master-Implementation-Plan.md cadence).
#
# All modes end with `sync_vault.py --sync-all` to push generated reports
# into the Obsidian vault (intraday's own script already does this too, so
# the trailing sync below is a harmless no-op duplicate for that mode).
set -euo pipefail

MODE="${1:-}"
REPO_ROOT="/Users/shubhammac/daily_stock_analysis"
PYTHON_EXEC="$REPO_ROOT/.venv/bin/python"
VAULT_BRIDGE="/Users/shubhammac/SSD/Obsidian/Daily Stock Analysis/Daily Stock Analysis/06-Scripts-Bridge"
VAULT_SYNC="$VAULT_BRIDGE/sync_vault.py"

PENNY_INTRADAY_WATCHLIST="IDEA.NS,RTNPOWER.NS,PCJEWELLER.NS,JPPOWER.NS,EASEMYTRIP.NS,BCG.NS,GREENPOWER.NS,DISHTV.NS,GTLINFRA.NS,ALOKINDS.NS"

case "$MODE" in
  premarket)
    echo "[$(date -Iseconds)] premarket run starting — scope: NSE Sub-Rs10 intraday watchlist"
    cd "$REPO_ROOT"
    # --no-market-review: default MARKET_REVIEW_REGION=cn triggers a slow, unrelated
    # China A-share market review on every run otherwise (observed ~2min+ from AkShare
    # alone during Phase 00 smoke testing) — irrelevant to an NSE-only premarket scope.
    "$PYTHON_EXEC" main.py --stocks "$PENNY_INTRADAY_WATCHLIST" --force-run --no-notify --no-market-review
    ;;
  eod)
    echo "[$(date -Iseconds)] eod run starting — scope: STOCK_LIST + CN screening candidates"
    cd "$REPO_ROOT"
    # Screening (Phase 02) only widens the CN side of the universe — it's
    # CN-only in practice (all shipped strategies declare market_scope: [cn],
    # India isn't in its allow-list at all). US/NSE tickers still come only
    # from STOCK_LIST / the premarket watchlist.
    "$PYTHON_EXEC" scripts/screen_and_analyze_eod.py
    ;;
  intraday)
    echo "[$(date -Iseconds)] intraday run starting — watchlist + NSE-wide discovered movers"
    cd "$VAULT_BRIDGE"
    "$PYTHON_EXEC" run_intraday_live_scanner.py
    ;;
  *)
    echo "Usage: $0 premarket|eod|intraday" >&2
    exit 1
    ;;
esac

echo "[$(date -Iseconds)] main.py finished, syncing vault"
"$PYTHON_EXEC" "$VAULT_SYNC" --sync-all
echo "[$(date -Iseconds)] $MODE run complete"
