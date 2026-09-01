# -*- coding: utf-8 -*-
"""
Comprehensive Obsidian Vault & Bridge Integrity Test Suite.

Validates the full working condition of:
1. Vault directory structure across all 13 institutional modules.
2. Core dashboard hubs, visual canvases, and audit records.
3. Complete 15-agent roster and persona prompt notes.
4. Watchlist definitions and universe parser integration.
5. Markdown encoding, frontmatter schemas, and cross-reference wiki-links.
6. Automation bridge scripts compilation and dry-run execution.
7. SQLite database connection, schema migration, and signal tables.
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest

# 这些路径指向开发者本机的 Obsidian vault 与运行时数据库，CI runner 上不存在。
# 硬编码绝对路径会让整个文件在 CI 上必然失败（2026-08-31 首次 CI 运行：55 failed），
# 同时在本机始终通过 —— 与 .env 泄漏正好相反的同一类缺陷：测试绑定了某一台机器。
# 允许用环境变量覆盖，并在目标不存在时跳过而不是失败。
VAULT_ROOT = os.environ.get(
    "DSA_VAULT_ROOT",
    "/Users/shubhammac/SSD/Obsidian/Daily Stock Analysis/Daily Stock Analysis",
)
SCRIPTS_BRIDGE_DIR = os.path.join(VAULT_ROOT, "06-Scripts-Bridge")

# Vendored third-party source trees: repositories checked out inside the vault so the
# platform they document sits beside the notes. They are not curated notes, and the fix for
# any drift they report would be editing someone else's repository.
#
# 13-OpenAlgo-Execution-Platform/openalgo alone ships hundreds of markdown files with their
# own frontmatter and link conventions, plus a virtualenv. On 2026-09-01 it was still being
# installed while this suite ran, and these checks failed twice for different reasons: 13
# unresolved wiki-links from another project's notes, then a FileNotFoundError when rglob
# walked into site-packages that were being written underneath it.
#
# Keep in step with VENDORED_DIRS in 06-Scripts-Bridge/test_vault_integrity.py.
VENDORED_DIRS = (
    "13-OpenAlgo-Execution-Platform/openalgo",
)


def _walk_curated():
    """Walk the vault, pruning vendored trees and tool directories as we go.

    Pruning matters more than filtering: a `glob("**/*")` descends into 50 MB of git
    objects and site-packages before anything can be discarded, which took this file from
    3 seconds to 173. `os.walk` lets the directories be skipped rather than read.
    """
    skip_dirs = {".git", ".venv", "venv", "node_modules", "__pycache__", ".obsidian"}
    vendored = {os.path.join(VAULT_ROOT, v.replace("/", os.sep)) for v in VENDORED_DIRS}

    for dirpath, dirnames, filenames in os.walk(VAULT_ROOT):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in skip_dirs and os.path.join(dirpath, d) not in vendored
        ]
        for filename in filenames:
            yield os.path.join(dirpath, filename)


def curated_markdown_files():
    """Every markdown note this vault's own conventions actually govern."""
    return [path for path in _walk_curated() if path.endswith(".md")]


def curated_basenames():
    """File names a wiki-link may resolve to, vendored trees excluded."""
    return {os.path.basename(path) for path in _walk_curated()}
DB_PATH = os.environ.get(
    "DSA_DB_PATH",
    os.path.join(Path(__file__).resolve().parents[1], "data", "stock_analysis.db"),
)

# 本文件是针对本机产物的集成检查：vault 不在时整体跳过。
pytestmark = pytest.mark.skipif(
    not os.path.isdir(VAULT_ROOT),
    reason=(
        "Obsidian vault not present - local integration check. "
        "Set DSA_VAULT_ROOT to run it elsewhere."
    ),
)

# 数据库用例另有依赖：vault 存在但库文件缺失时，只跳过这些用例。
requires_local_db = pytest.mark.skipif(
    not os.path.isfile(DB_PATH),
    reason=(
        "stock_analysis.db not present - local integration check. "
        "Set DSA_DB_PATH to run it elsewhere."
    ),
)

EXPECTED_MODULE_DIRS = [
    "00-Dashboard",
    "01-Agents",
    "02-Reports",
    "03-Watchlists",
    "04-Playbooks-Strategies",
    "05-Templates",
    "06-Scripts-Bridge",
    "07-Codebase-Research",
    "08-Indian-Finance-Laws-Taxation",
    "09-Quant-Factor-Lab",
    "10-Macro-Liquidity-Radar",
    "11-Portfolio-Risk-Optimization",
    "12-Awesome-Systematic-Trading",
]

EXPECTED_AGENTS = [
    "00-Agent-Hub.md",
    "01-Orchestrator-Agent.md",
    "02-Technical-Analyst-Agent.md",
    "03-Kronos-Forecaster-Agent.md",
    "04-News-Sentiment-Agent.md",
    "05-Risk-Manager-Agent.md",
    "06-Intraday-Penny-Agent.md",
    "07-Codebase-Research-Agent.md",
    "08-Kronos-Deep-Research-Agent.md",
    "09-Backtest-Validation-Agent.md",
    "10-Pattern-Memory-Agent.md",
    "11-Live-Web-Scraper-Agent.md",
    "12-Indian-Regulatory-Tax-Agent.md",
    "13-Stat-Arb-Pairs-Trading-Agent.md",
    "14-Macro-Institutional-Flow-Agent.md",
    "15-Options-Greeks-Volatility-Agent.md",
]

EXPECTED_BRIDGE_SCRIPTS = [
    "sync_vault.py",
    "live_market_data_engine.py",
    "scan_upper_circuit_candidates.py",
    "subex_exit_monitor.py",
    "run_nse_discovery_scan.py",
    "run_eod_pipeline.py",
    "run_intraday_live_scanner.py",
]


class TestVaultStructure:
    """Validates structural completeness of the Obsidian Vault."""

    def test_vault_root_directory_exists(self):
        assert os.path.isdir(VAULT_ROOT), f"Vault root directory does not exist: {VAULT_ROOT}"

    @pytest.mark.parametrize("dir_name", EXPECTED_MODULE_DIRS)
    def test_all_13_modules_directories_exist(self, dir_name: str):
        dir_path = os.path.join(VAULT_ROOT, dir_name)
        assert os.path.isdir(dir_path), f"Required module directory missing: {dir_path}"

    def test_dashboard_core_files_exist(self):
        dashboard_dir = os.path.join(VAULT_ROOT, "00-Dashboard")
        expected_files = [
            "Market-Command-Center.md",
            "System-State-of-Record.md",
            "Quant-Maturity-Assessment.md",
            "Sector-Rotation-Radar.canvas",
        ]
        for fname in expected_files:
            fpath = os.path.join(dashboard_dir, fname)
            assert os.path.isfile(fpath), f"Dashboard file missing: {fpath}"
            assert os.path.getsize(fpath) > 50, f"Dashboard file appears empty: {fpath}"

    @pytest.mark.parametrize("agent_file", EXPECTED_AGENTS)
    def test_complete_agent_roster_exists(self, agent_file: str):
        fpath = os.path.join(VAULT_ROOT, "01-Agents", agent_file)
        assert os.path.isfile(fpath), f"Agent definition file missing: {fpath}"
        assert os.path.getsize(fpath) > 100, f"Agent definition note is empty: {fpath}"

    def test_governance_and_playbook_indices_exist(self):
        indices = [
            ("04-Playbooks-Strategies", "00-Playbook-Index.md"),
            ("07-Codebase-Research", "00-Research-Hub.md"),
            ("12-Awesome-Systematic-Trading", "00-Systematic-Trading-Hub.md"),
            ("02-Reports/Intraday-Picks", "00-Signal-Log-Index.md"),
        ]
        for folder, fname in indices:
            fpath = os.path.join(VAULT_ROOT, folder, fname)
            assert os.path.isfile(fpath), f"Hub index file missing: {fpath}"

    def test_template_stock_analysis_exists_and_valid(self):
        tpath = os.path.join(VAULT_ROOT, "05-Templates", "Template-Stock-Analysis.md")
        assert os.path.isfile(tpath), f"Stock analysis template missing: {tpath}"
        with open(tpath, "r", encoding="utf-8") as f:
            content = f.read()
        assert "{{ticker}}" in content
        assert "{{score}}" in content


class TestWatchlistsAndUniverse:
    """Validates watchlists and integration with Python universe loader."""

    def test_all_watchlist_files_exist(self):
        wl_dir = os.path.join(VAULT_ROOT, "03-Watchlists")
        expected_wl = ["Indian-NSE-BSE.md", "Penny-Intraday-Sub10.md", "US-Tech-Leaders.md"]
        for fname in expected_wl:
            fpath = os.path.join(wl_dir, fname)
            assert os.path.isfile(fpath), f"Watchlist note missing: {fpath}"
            assert os.path.getsize(fpath) > 100, f"Watchlist note is empty: {fpath}"

    def test_vault_watchlist_loader_extracts_symbols(self):
        from src.services.vault_watchlist_loader import load_universe_from_watchlists
        symbols = load_universe_from_watchlists(os.path.join(VAULT_ROOT, "03-Watchlists"))
        assert isinstance(symbols, list)
        assert len(symbols) >= 15, f"Expected at least 15 symbols, extracted {len(symbols)}"
        
        # Verify core symbols are present
        for expected_sym in ["RELIANCE.NS", "TCS.NS", "RTNPOWER.NS", "JPPOWER.NS", "HAL.NS"]:
            assert expected_sym in symbols, f"Expected {expected_sym} in extracted universe, got {symbols}"

    def test_all_extracted_symbols_have_valid_nse_format(self):
        from src.services.vault_watchlist_loader import load_universe_from_watchlists
        symbols = load_universe_from_watchlists(os.path.join(VAULT_ROOT, "03-Watchlists"))
        pattern = re.compile(r"^[A-Z0-9&-]+\.NS$")
        for sym in symbols:
            assert pattern.match(sym), f"Malformed symbol extracted: {sym}"


class TestMarkdownIntegrityAndCrossLinks:
    """Validates UTF-8 encoding and internal wiki-link references."""

    def test_all_vault_markdown_files_are_valid_utf8(self):
        md_files = curated_markdown_files()
        assert len(md_files) > 50, f"Expected >50 markdown notes in vault, found {len(md_files)}"
        for fpath in md_files:
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    _ = f.read()
            except Exception as e:
                pytest.fail(f"Failed to read markdown file as UTF-8: {fpath} with error: {e}")

    def test_internal_wiki_links_resolve_to_existing_files(self):
        md_files = curated_markdown_files()
        known_basenames = curated_basenames()
        link_pattern = re.compile(r"\[\[([^\]\|]+)(?:\|[^\]]+)?\]\]")
        
        broken_links = []
        for fpath in md_files:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            
            for match in link_pattern.findall(content):
                target = match.strip()
                if not target:
                    continue
                # Strip heading anchors (#anchor) and escape slashes
                target = target.split("#")[0].rstrip("\\").strip()
                if not target:
                    continue
                target_with_ext = target if target.endswith(".md") or target.endswith(".canvas") else f"{target}.md"
                
                direct_path = os.path.join(VAULT_ROOT, target_with_ext)
                relative_path = os.path.join(os.path.dirname(fpath), target_with_ext)
                # A link is not resolved by a file inside a vendored tree, and walking one
                # while it is being installed raises FileNotFoundError mid-iteration.
                resolves_by_name = os.path.basename(target_with_ext) in known_basenames

                if not (os.path.exists(direct_path) or os.path.exists(relative_path) or resolves_by_name):
                    broken_links.append((os.path.basename(fpath), target))
                    
        assert len(broken_links) == 0, f"Found {len(broken_links)} broken wiki-links: {broken_links[:10]}"


class TestBridgeScripts:
    """Validates compilation, imports, and self-checks of 06-Scripts-Bridge."""

    @pytest.mark.parametrize("script_name", EXPECTED_BRIDGE_SCRIPTS)
    def test_bridge_scripts_exist(self, script_name: str):
        script_path = os.path.join(SCRIPTS_BRIDGE_DIR, script_name)
        assert os.path.isfile(script_path), f"Bridge script missing: {script_path}"

    @pytest.mark.parametrize("script_name", EXPECTED_BRIDGE_SCRIPTS)
    def test_bridge_scripts_compile_without_syntax_errors(self, script_name: str):
        script_path = os.path.join(SCRIPTS_BRIDGE_DIR, script_name)
        with open(script_path, "r", encoding="utf-8") as f:
            code = f.read()
        try:
            compile(code, script_path, "exec")
        except SyntaxError as e:
            pytest.fail(f"Syntax error compiling {script_name}: {e}")

    def test_sync_vault_cli_help(self):
        script_path = os.path.join(SCRIPTS_BRIDGE_DIR, "sync_vault.py")
        proc = subprocess.run(
            [sys.executable, script_path, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0
        assert "Obsidian Multi-Agent Vault Bridge" in proc.stdout

    def test_subex_exit_monitor_execution(self):
        script_path = os.path.join(SCRIPTS_BRIDGE_DIR, "subex_exit_monitor.py")
        proc = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0
        assert "SUBEXLTD.NS" in proc.stdout


@requires_local_db
class TestDatabaseIntegrityAndPersistence:
    """Validates SQLite database integrity and decision signal persistence."""

    def test_sqlite_database_file_exists(self):
        assert os.path.isfile(DB_PATH), f"SQLite database not found: {DB_PATH}"

    def test_sqlite_pragma_integrity_check(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        row = cursor.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "ok", f"Database integrity check failed: {row}"

    def test_core_tables_and_signal_counts(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("SELECT count(*) FROM decision_signals;")
        signals_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT count(*) FROM decision_signal_outcomes;")
        outcomes_count = cursor.fetchone()[0]
        
        conn.close()
        assert signals_count >= 40, f"Expected >= 40 decision signals, found {signals_count}"
        assert outcomes_count >= 50, f"Expected >= 50 signal outcomes, found {outcomes_count}"
