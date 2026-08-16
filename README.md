<div align="center">

# 📈 AI Stock Analysis System

[![GitHub stars](https://img.shields.io/github/stars/king007pin/daily_stock_analysis?style=social)](https://github.com/king007pin/daily_stock_analysis/stargazers)
[![CI](https://github.com/king007pin/daily_stock_analysis/actions/workflows/ci.yml/badge.svg)](https://github.com/king007pin/daily_stock_analysis/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-Ready-2088FF?logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/zhulinsen/daily_stock_analysis)

> 🤖 **AI-powered stock analysis system for Indian Stock Market (NSE / BSE), US, Hong Kong, A-Shares, Japan, Korea, and Taiwan.**
> Automatically analyzes your watchlist daily and delivers an actionable **Decision Dashboard** to Telegram, Discord, Slack, Email, Feishu, or WeChat Work.

[**Product Preview**](#-product-preview) · [**Key Features**](#-key-features) · [**Quick Start**](#-quick-start) · [**Sample Output**](#-sample-output) · [**Documentation Index**](docs/INDEX_EN.md) · [**Full Guide**](docs/full-guide_EN.md)

**English** | [简体中文](docs/README_ZH.md) | [繁體中文](docs/README_CHT.md)

</div>

## 🖥️ Product Preview

<p align="center">
  <img src="docs/assets/readme_workspace_tour_20260510.gif" alt="DSA Web Workspace Demo" width="720">
</p>

## ✨ Key Features

| Capability | Coverage |
|------------|----------|
| **AI Decision Reports** | Core conclusions, scoring (0-100), trend predictions, entry/exit levels, risk warnings, catalysts, and action checklists. |
| **Multi-Market Support** | Full coverage for **Indian Market (NSE `.NS` / BSE `.BO`)**, US, Hong Kong, China A-Shares, Japan (`.T`), Korea (`.KS`/`.KQ`), Taiwan (`.TW`/`.TWO`), and ETFs. |
| **Trading Calendars** | Multi-timezone market calendar aware (India `Asia/Kolkata`, US `America/New_York`, Asia markets) with automatic holiday & trading session detection. |
| **Web & Desktop Workspace** | Modern UI for manual analysis, task monitoring, history logs, backtesting, portfolio management, light/dark themes. |
| **Agent Strategy Q&A** | Multi-turn conversational stock diagnosis with 15 built-in investment strategies (Moving Averages, Trend Following, Value Investing, Growth, Momentum, etc.). |
| **Automation & Push** | Automated execution via GitHub Actions, Docker, or local scheduler with instant alerts to Telegram, Discord, Slack, and Email. |

### Supported Markets & Ticker Formats

| Market | Ticker Format Example | Exchange Identifier | Data Provider |
|--------|----------------------|---------------------|---------------|
| **Indian NSE** | `RELIANCE.NS`, `TCS.NS`, `INFY.NS`, `M&M.NS` | National Stock Exchange | Yahoo Finance |
| **Indian BSE** | `RELIANCE.BO`, `TCS.BO`, `500325.BO` | Bombay Stock Exchange | Yahoo Finance |
| **US Stocks** | `AAPL`, `TSLA`, `NVDA`, `MSFT`, `BRK.B` | NASDAQ / NYSE | Yahoo Finance / Longbridge |
| **Hong Kong** | `0700.HK`, `9988.HK`, `hk00700` | HKEX | Yahoo Finance / TickFlow |
| **China A-Shares** | `600519`, `000001`, `sz000001` | SSE / SZSE / BSE | AkShare / Tushare / Baostock |
| **Japan** | `7203.T` (Toyota), `9984.T` | Tokyo Stock Exchange | Yahoo Finance |
| **Korea** | `005930.KS` (Samsung), `035420.KS` | KRX | Yahoo Finance |
| **Taiwan** | `2330.TW` (TSMC), `2454.TW` | TWSE / TPEx | Yahoo Finance |

---

## 🚀 Quick Start

### Method 1: Local Setup (Recommended for Development & Testing)

```bash
# 1. Clone your repository
git clone https://github.com/king007pin/daily_stock_analysis.git
cd daily_stock_analysis

# 2. Create and activate Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your configuration file
cp .env.example .env

# 5. Edit .env with your AI API Key and Watchlist
# Example:
# GEMINI_API_KEY=your_gemini_api_key_here
# REPORT_LANGUAGE=en
# STOCK_LIST=RELIANCE.NS,TCS.NS,INFY.NS,AAPL

# 6. Run analysis
python main.py
```

### Method 2: GitHub Actions (Free, Automated Daily Runs)

1. **Fork this repository** to your GitHub account.
2. Go to **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:
   - `GEMINI_API_KEY` (or `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`): Your AI API Key.
   - `STOCK_LIST`: Comma-separated tickers (e.g. `RELIANCE.NS,TCS.NS,AAPL`).
   - `REPORT_LANGUAGE`: `en` (for English reports).
   - Notification secrets (e.g. `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`, `DISCORD_WEBHOOK_URL`, or `EMAIL_SENDER` & `EMAIL_PASSWORD`).
3. Enable GitHub Actions in the **Actions** tab.
4. The workflow runs automatically every weekday at market close, or you can trigger it manually via **Run workflow**.

---

## 💻 CLI Commands

```bash
# Run analysis for specific Indian & US stocks in dry-run mode (no LLM tokens spent)
python main.py --stocks RELIANCE.NS,TCS.NS,AAPL --dry-run --force

# Run full live AI analysis on custom stocks
python main.py --stocks RELIANCE.NS,TCS.NS,INFY.NS

# Run market index review for India / Global markets
python main.py --market-review

# Launch local Web UI Dashboard
python main.py --webui

# Start background scheduler
python main.py --schedule
```

---

## 📱 Sample Output (Decision Dashboard)

```
🎯 2026-08-16 Decision Dashboard
Analyzed: 3 stocks | 🟢 Buy: 1 | 🟡 Hold/Watch: 2 | 🔴 Sell: 0

📊 Analysis Summary
🟢 RELIANCE.NS: Buy | Score: 78 | Bullish
🟡 TCS.NS: Watch | Score: 62 | Sideways
🟡 INFY.NS: Watch | Score: 58 | Sideways

==================================================
🟢 RELIANCE INDUSTRIES LTD (RELIANCE.NS)
==================================================
📰 Key Highlights:
- Sentiment: Bullish momentum driven by robust retail expansion and energy margins.
- Catalysts: Strong volume breakouts above key resistance level (₹1305).

🚨 Risk Warnings:
- Global crude price volatility.
- Foreign Institutional Investor (FII) short-term hedging.

✨ Action Checklist:
- Target Entry: ₹1300 - ₹1315
- Stop-Loss: ₹1270
- Target Resistance: ₹1365 / ₹1400

---
Generated at: 18:00 IST
```

---

## ⚙️ Configuration Options (`.env`)

| Variable | Description | Default |
|----------|-------------|---------|
| `STOCK_LIST` | Comma-separated list of stock tickers | `RELIANCE.NS,TCS.NS,AAPL` |
| `REPORT_LANGUAGE` | Output language for AI reports (`en`, `zh`, `ko`) | `en` |
| `GEMINI_API_KEY` | Google Gemini API Key | - |
| `OPENAI_API_KEY` | OpenAI / DeepSeek / Compatible API Key | - |
| `SINGLE_STOCK_NOTIFY` | Send notifications per stock immediately vs bundled | `false` |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token for notification push | - |
| `DISCORD_WEBHOOK_URL` | Discord Webhook URL | - |

---

## 📄 License

[MIT License](LICENSE) © 2026

## ⚠️ Disclaimer

This project is for educational, research, and informational purposes only. It is not financial or investment advice. Always perform your own due diligence before making investment decisions.
