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

# Validate MODE before the calendar gate: an unknown mode is a caller bug and
# must still fail fast with the usage message below, not get silently swallowed
# by a "market closed" exit 0.
case "$MODE" in
  premarket|eod|intraday) ;;
  *)
    echo "Usage: $0 premarket|eod|intraday" >&2
    exit 1
    ;;
esac

# Trading-day gate — applies to every mode, before any of them does real work.
# launchd's StartCalendarInterval is calendar-agnostic, so com.dsa.intraday fired
# all 4 slots on Sat 2026-08-22 and Sun 2026-08-23 and emitted intraday BUY
# signals (with share quantities) against Friday's stale closes.
#
# cwd: `python -m` needs the repo root importable, but the branches below each cd
# on their own and intraday cds to $VAULT_BRIDGE, not the repo. Run the guard in a
# subshell that cds to $REPO_ROOT so it is correct regardless of mode and the cd
# does not leak into whichever branch runs next.
#
# The guard is fail-closed on the Python side: if it cannot positively establish
# that today is a trading day (missing/unparseable holiday data, etc.) it reports
# closed. Skipping a live session costs one run; trading off stale prices does not
# fail loudly.
#
# exit 0, not non-zero: a closed market is the normal outcome on weekends and
# exchange holidays, not a job failure. Exiting non-zero would make launchd treat
# every weekend run as a crash and start backing the job off.
if ! (cd "$REPO_ROOT" && "$PYTHON_EXEC" -m src.services.nse_trading_day_guard --check); then
  echo "[$(date -Iseconds)] $MODE skipped — NSE closed"
  exit 0
fi

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
    # Unreachable in practice — MODE is validated above the gate. Kept as a
    # defensive backstop so the dispatch case never falls through silently.
    echo "Usage: $0 premarket|eod|intraday" >&2
    exit 1
    ;;
esac

# Names the mode that actually ran, not a fixed entrypoint. The previous
# hardcoded "main.py finished" printed on every mode including intraday, which
# runs the vault bridge scanner and never invokes main.py at all — reading the
# log then suggested the analysis pipeline had run when it had not.
echo "[$(date -Iseconds)] $MODE finished, syncing vault"
"$PYTHON_EXEC" "$VAULT_SYNC" --sync-all
echo "[$(date -Iseconds)] $MODE run complete"
