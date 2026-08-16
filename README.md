<div align="center">

# AI Stock Analysis System

[![GitHub stars](https://img.shields.io/github/stars/king007pin/daily_stock_analysis?style=social)](https://github.com/king007pin/daily_stock_analysis/stargazers)
[![CI](https://github.com/king007pin/daily_stock_analysis/actions/workflows/ci.yml/badge.svg)](https://github.com/king007pin/daily_stock_analysis/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-Ready-2088FF?logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![Docker](https://hub.docker.com/r/zhulinsen/daily_stock_analysis)](https://hub.docker.com/r/zhulinsen/daily_stock_analysis)

<p align="center">
  <img src="https://trendshift.io/api/badge/trendshift/repositories/18527/daily?language=Python" alt="#1 Python Repository Of The Day | Trendshift" width="250" height="55"/>&nbsp;<a href="https://hellogithub.com/repository/ZhuLinsen/daily_stock_analysis" target="_blank"><img src="https://api.hellogithub.com/v1/widgets/recommend.svg?rid=6daa16e405ce46ed97b4a57706aeb29f&claim_uid=pfiJMqhR9uvDGlT&theme=neutral" alt="Featured｜HelloGitHub" width="230" /></a>
</p>

> 🤖 **AI-powered stock analysis system for Indian Stock Market (NSE / BSE), US, Hong Kong, China A-shares, Japan, Korea, and Taiwan.**
> Automatically analyzes your watchlist daily and delivers an actionable **Decision Dashboard** to Telegram, Discord, Slack, Email, Feishu, or WeChat Work.

[**Product Preview**](#-product-preview) · [**Key Features**](#-key-features) · [**Quick Start**](#-quick-start) · [**Sample Output**](#-sample-output) · [**Documentation Index**](docs/INDEX_EN.md) · [**Full Guide**](docs/full-guide_EN.md)

**English** | [简体中文](docs/README_ZH.md) | [繁體中文](docs/README_CHT.md)

</div>

## 💖 Sponsors

<div align="center">
  <p align="center">
    <a href="https://open.anspire.cn/?share_code=QFBC0FYC" target="_blank"><img src="docs/assets/anspire.png" alt="Anspire Open all-in-one model and search service" width="300" height="141" style="width: 300px; height: 141px; object-fit: contain;"></a>
    <a href="https://serpapi.com/baidu-search-api?utm_source=github_daily_stock_analysis" target="_blank"><img src="docs/assets/serpapi_banner_en.png" alt="Easily scrape real-time financial news data from search engines - SerpApi" width="300" height="141" style="width: 300px; height: 141px; object-fit: contain;"></a>
  </p>
</div>

## 🖥️ Product Preview

<p align="center">
  <img src="docs/assets/readme_workspace_tour_20260510.gif" alt="DSA Web workspace demo" width="720">
</p>

## ✨ Key Features

| Capability | Coverage |
|------------|----------|
| **AI Decision Reports** | Core conclusion, score (0-100), trend prediction, entry/exit levels, risk alerts, catalysts, and action checklist |
| **Multi-Market Data** | Full coverage for **Indian Market (NSE `.NS` / BSE `.BO`)**, US, Hong Kong, China A-shares, Japan (`.T`), Korea (`.KS`/`.KQ`), Taiwan (`.TW`/`.TWO`), and ETFs. Includes quotes, K-lines, technical indicators, news, announcements, fundamentals, and report context. See [Market Support Boundaries](docs/market-support.md) |
| **Web & Desktop Workspace** | Manual analysis, task progress, history, full Markdown reports, backtest, portfolio, settings, and light/dark themes |
| **Agent Strategy Chat** | Multi-turn Q&A with 15 built-in strategies across Web/Bot/API |
| **Smart Import & Autocomplete** | Image, CSV/Excel, clipboard import; code/name/pinyin/alias autocomplete |
| **Automation & Notifications** | GitHub Actions, Docker, local scheduler, FastAPI service, and Telegram / Discord / Slack / Email / WeChat Work / Feishu delivery |

> Detailed fields, fundamental P0 timeout semantics, trading rules, data-source priority, Web/API behavior, and troubleshooting live in the [Full Guide](docs/full-guide_EN.md).

### Tech Stack & Data Sources

| Type | Supported Providers |
|------|---------------------|
| **AI Models** | [Anspire](https://open.anspire.cn/?share_code=QFBC0FYC), [AIHubMix](https://inferera.com/?aff=CfMq), Google Gemini, OpenAI-compatible providers, DeepSeek, Qwen, Claude, Ollama (Local) |
| **Market Data** | [TickFlow](https://tickflow.org/auth/register?ref=WDSGSPS5XC), Yahoo Finance (India NSE/BSE, US, HK, JP, KR, TW), AkShare, Tushare, Pytdx, Baostock, Longbridge |
| **News Search** | [Anspire](https://open.anspire.cn/?share_code=QFBC0FYC), [SerpAPI](https://serpapi.com/baidu-search-api?utm_source=github_daily_stock_analysis), [Tavily](https://tavily.com/), [Bocha](https://open.bocha.cn/), [Brave](https://brave.com/search/api/), [MiniMax](https://platform.minimaxi.com/), SearXNG |
| **Social Sentiment** | [Stock Sentiment API](https://api.adanos.org/docs) (Reddit / X / Polymarket, US stocks only) |

> The project includes free market-data sources such as Yahoo Finance, AkShare, and Baostock and runs without extra paid credentials. These free sources can be rate-limited or fluctuate by network conditions. For scheduled runs or higher reliability, configure token-based sources like TickFlow, Tushare, or Longbridge; market coverage, Actions mappings, and fallback rules are documented in [Data Source Configuration](docs/full-guide_EN.md#data-source-configuration).

### Supported Markets & Ticker Formats

| Market | Ticker Format Example | Exchange Identifier | Data Provider |
|--------|----------------------|---------------------|---------------|
| **Indian NSE** | `RELIANCE.NS`, `TCS.NS`, `INFY.NS`, `M&M.NS` | National Stock Exchange | Yahoo Finance |
| **Indian BSE** | `RELIANCE.BO`, `TCS.BO`, `500325.BO` | Bombay Stock Exchange | Yahoo Finance |
| **US Stocks** | `AAPL`, `TSLA`, `NVDA`, `MSFT`, `BRK.B` | NASDAQ / NYSE | Yahoo Finance / Longbridge |
| **Hong Kong** | `0700.HK`, `9988.HK`, `hk00700` | HKEX | Yahoo Finance / TickFlow |
| **China A-Shares** | `600519`, `000001`, `sz000001` | SSE / SZSE / BSE | AkShare / Tushare / Baostock |
| **Japan** | `7203.T` (Toyota), `9984.T` (SoftBank) | Tokyo Stock Exchange | Yahoo Finance |
| **Korea** | `005930.KS` (Samsung), `035420.KS` (Naver) | KRX | Yahoo Finance |
| **Taiwan** | `2330.TW` (TSMC), `2454.TW` (MediaTek) | TWSE / TPEx | Yahoo Finance |

---

## 🚀 Quick Start

### Option 1: GitHub Actions (Recommended)

> Deploy in about 5 minutes, with no server and zero infrastructure cost.

#### 1. Fork this repository

Click `Fork` in the upper-right corner. (Star ⭐ if this project helps you!)

#### 2. Configure Secrets

In your forked repository, go to `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`.

**AI model configuration (configure at least one):**

Start with one provider and one API key. For multi-model routing, image recognition, local models, or advanced routing, see the [LLM Config Guide](docs/LLM_CONFIG_GUIDE_EN.md).

| Secret Name | Description | Required |
|-------------|-------------|:--------:|
| `GEMINI_API_KEY` | Google Gemini API key (Free tier available) | **Recommended** |
| `ANSPIRE_API_KEYS` | [Anspire](https://open.anspire.cn/?share_code=QFBC0FYC) API key, one key for popular LLMs and web search | **Recommended** |
| `AIHUBMIX_KEY` | [AIHubMix](https://inferera.com/?aff=CfMq) API key, one key for multiple model families | **Recommended** |
| `OPENAI_API_KEY` | OpenAI-compatible API key (DeepSeek, OpenAI, Groq, etc.) | Optional |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | Custom base URL / model name when using OpenAI-compatible providers | Optional |
| `ANTHROPIC_API_KEY` | Anthropic Claude API key | Optional |

> Ollama is better suited for local or Docker deployment. GitHub Actions runs smoothest with cloud APIs.

**Notification channels (configure at least one):**

| Secret Name | Description |
|-------------|-------------|
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram push alerts |
| `DISCORD_WEBHOOK_URL` | Discord channel webhook |
| `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_ID` | Slack channel bot |
| `EMAIL_SENDER` + `EMAIL_PASSWORD` | Direct email delivery |
| `WECHAT_WEBHOOK_URL` | WeChat Work bot |
| `FEISHU_WEBHOOK_URL` | Feishu / Lark bot |

More channels, signatures, email groups, and Markdown-to-image settings are in [Notification Configuration](docs/full-guide_EN.md#notification-channel-configuration).

**Watchlist configuration (required):**

| Secret Name | Description | Required |
|-------------|-------------|:--------:|
| `STOCK_LIST` | Comma-separated watchlist (e.g. `RELIANCE.NS,TCS.NS,AAPL,hk00700`) | ✅ |
| `REPORT_LANGUAGE` | Set to `en` for English analysis reports | Optional (`en` default) |

**News sources (recommended):**

News search strongly improves sentiment, announcements, events, and catalyst quality. Configure at least one search provider if possible.

| Secret Name | Description | Required |
|-------------|-------------|:--------:|
| `SERPAPI_API_KEYS` | [SerpAPI](https://serpapi.com/baidu-search-api?utm_source=github_daily_stock_analysis), search-engine results for realtime financial news | **Recommended** |
| `TAVILY_API_KEYS` | [Tavily](https://tavily.com/), general news search API | **Recommended** |
| `ANSPIRE_API_KEYS` | [Anspire AI Search](https://open.anspire.cn/?share_code=QFBC0FYC), global news and search | Optional |
| `BRAVE_API_KEYS` | [Brave Search](https://brave.com/search/api/), privacy-first news search | Optional |
| `MINIMAX_API_KEYS` | [MiniMax](https://platform.minimaxi.com/), structured search results | Optional |
| `SEARXNG_BASE_URLS` | Self-hosted SearXNG instances for quota-free fallback | Optional |

More search providers, social sentiment, and fallback behavior are in [Search Configuration](docs/full-guide_EN.md#search-service-configuration).

**Market data sources (optional):**

> Free sources like Yahoo Finance, AkShare, and Baostock are used by default. "Not configured" messages in logs are informational and do not affect execution.

| Secret Name | Market | Description |
|-------------|:------:|-------------|
| `TUSHARE_TOKEN` | A-shares | Improves historical A-share data stability |
| `LONGBRIDGE_OAUTH_CLIENT_ID` + `LONGBRIDGE_OAUTH_TOKEN_CACHE_B64` | HK/US stocks | Realtime Level-2 quote data, turnover rates, P/E |

#### 3. Enable Actions

Open the `Actions` tab and click `I understand my workflows, go ahead and enable them`.

#### 4. Manual Test

`Actions` -> `Daily Stock Analysis` -> `Run workflow` -> `Run workflow`.

#### Done

By default, the workflow runs automatically on weekdays at market close and skips holidays. Forced runs and trading-day check options are covered in the [Full Guide](docs/full-guide_EN.md#scheduled-task-configuration).

---

### Option 2: Local / Docker Deployment

```bash
# 1. Clone the project
git clone https://github.com/king007pin/daily_stock_analysis.git && cd daily_stock_analysis

# 2. Set up Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env && vim .env

# 5. Run analysis
python main.py
```

Common CLI commands:

```bash
# Dry run for specific Indian & US stocks (no LLM cost)
python main.py --stocks RELIANCE.NS,TCS.NS,AAPL --dry-run --force

# Full analysis on custom stocks
python main.py --stocks RELIANCE.NS,TCS.NS,INFY.NS

# Market overview analysis (Nifty 50, S&P 500, etc.)
python main.py --market-review

# Launch local Web UI Dashboard
python main.py --webui

# Start background automated scheduler
python main.py --schedule

# Run FastAPI backend only
python main.py --serve-only
```

> Docker deployment, scheduling, and cloud-server WebUI access are documented in the [Full Guide](docs/full-guide_EN.md).

---

## 📱 Sample Output

### Decision Dashboard

```markdown
🎯 2026-08-16 Decision Dashboard
Analyzed 3 stocks | 🟢 Buy: 1 🟡 Watch: 2 🔴 Sell: 0

📊 Analysis Summary
🟢 RELIANCE.NS: Buy | Score 78 | Bullish
🟡 TCS.NS: Watch | Score 62 | Sideways
🟡 INFY.NS: Watch | Score 58 | Sideways

==================================================
🟢 RELIANCE INDUSTRIES LTD (RELIANCE.NS)
==================================================
📰 Key Highlights:
- Sentiment: Bullish momentum driven by strong retail volume and oil-to-chemicals refining margins.
- Catalysts: Breakout above key ₹1305 resistance line; institutional accumulation (DII).

🚨 Risk Alerts:
Risk 1: Short-term profit-taking at higher levels.
Risk 2: Crude price volatility affecting petrochemical spreads.

✨ Action Checklist:
- Target Entry: ₹1300 - ₹1315
- Stop-Loss: ₹1270
- Target Resistance: ₹1365 / ₹1400

---
Generated at: 18:00 IST
```

### Market Review

```markdown
🎯 2026-08-16 Market Review (India & Global)

📊 Major Indices
- Nifty 50: 24,850.30 (🟢 +0.72%)
- BSE Sensex: 81,320.15 (🟢 +0.68%)
- S&P 500: 5,648.40 (🟢 +0.55%)
- Nasdaq 100: 19,720.80 (🟢 +0.82%)

📈 Market Breadth
Advance: 1,840 | Decline: 920 | Upper Circuit: 78 | Lower Circuit: 12

🔥 Sector Performance
Top Gainers: Information Technology, Oil & Gas, Banking & Financials
Top Laggards: Metals, Real Estate, FMCG
```

---

## ⚙️ Configuration

Full environment variables, model routing, notification channels, data-source priority, trading rules, fundamental P0 semantics, and deployment details are in the [Full Guide](docs/full-guide_EN.md).

---

## 🖥️ Web UI

The Web workspace supports settings, task monitoring, manual analysis, history reports, full Markdown reports, Agent strategy chat, backtesting, portfolio management, smart import, and light/dark themes.

```bash
python main.py --webui
python main.py --webui-only
```

Visit `http://127.0.0.1:8000`. Authentication, smart import, autocomplete, report copying, and cloud-server access are documented in [Local WebUI Management](docs/full-guide_EN.md#local-webui-management-interface).

---

## 🤖 Agent Strategy Chat

After configuring any available AI API key, the Web `/chat` page allows conversational strategy analysis with custom strategies. Set `AGENT_MODE=false` if you want to disable it.

- Built-in strategies: Moving Average Golden Cross, Chan Theory, Elliott Wave, Bullish Trend, Hot Themes, Event-Driven, Quality Growth, Expectation Repricing, Value Investing, and more.
- Calls realtime quotes, K-lines, technical indicators, news, and risk context.
- Supports multi-turn follow-up questions, session export, notification sending, and background execution.
- Supports custom strategy files and experimental multi-agent orchestration.

> Agent parameters, `skill` naming compatibility, multi-agent mode, and budget guards are covered in the [Full Guide](docs/full-guide_EN.md#local-webui-management-interface) and [LLM Config Guide](docs/LLM_CONFIG_GUIDE_EN.md).

---

## 🧩 Related Projects

> DSA focuses on daily analysis reports. Its screening implementation references AlphaSift, while AlphaEvo covers strategy validation and evolution.

| Project | Focus |
|---------|-------|
| [AlphaSift](https://github.com/ZhuLinsen/alphasift) | Reference project for DSA's screening implementation |
| [AlphaEvo](https://github.com/ZhuLinsen/alphaevo) | Strategy backtesting and self-evolution experiments for validating rules and iteratively exploring strategy parameters |

---

## 📞 Contact & Support

<table>
  <tr>
    <td width="92" valign="top"><strong>Email</strong></td>
    <td valign="top">
      <a href="mailto:zhuls345@gmail.com">zhuls345@gmail.com</a><br>
      Project consulting, deployment support, and feature extensions
    </td>
    <td align="center" rowspan="3" valign="middle" width="148">
      <a href="http://xhslink.com/m/tU520DWCKT" target="_blank"><img src="docs/assets/xiaohongshu_tick.jpg" width="112" alt="Xiaohongshu QR code"></a><br>
      <sub>Follow on Xiaohongshu</sub>
    </td>
  </tr>
  <tr>
    <td width="92" valign="top"><strong>Community</strong></td>
    <td valign="top"><a href="https://github.com/king007pin/daily_stock_analysis/discussions">GitHub Discussions</a></td>
  </tr>
  <tr>
    <td width="92" valign="top"><strong>Feedback</strong></td>
    <td valign="top"><a href="https://github.com/king007pin/daily_stock_analysis/issues">Submit Issue</a></td>
  </tr>
</table>

---

## 📄 License

[MIT License](LICENSE) © 2026

If you use or build on this project, attribution with a link back to this repository is appreciated.

## ⚠️ Disclaimer

This project is for informational and educational purposes only. AI-generated analysis is not investment advice. Stock market investing involves risk; do your own research and consult a licensed financial advisor when needed.
