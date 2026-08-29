# -*- coding: utf-8 -*-
"""
Strategies / Skills listing command.

Shows all available trading strategies and their activation status with English localization support.
"""

import logging
from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from src.config import get_config

logger = logging.getLogger(__name__)


class StrategiesCommand(BotCommand):
    """
    List available trading strategies.

    Usage:
        /strategies         - List all strategies
        /strategies active  - Show only active strategies
    """

    @property
    def name(self) -> str:
        return "strategies"

    @property
    def aliases(self) -> List[str]:
        return ["skills", "策略", "策略列表"]

    @property
    def description(self) -> str:
        is_en = getattr(get_config(), "report_language", "zh") == "en"
        return "List available quant trading strategies" if is_en else "查看可用交易策略"

    @property
    def usage(self) -> str:
        return "/strategies [active]"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """Execute the strategies list command."""
        is_en = getattr(get_config(), "report_language", "zh") == "en"
        show_active_only = bool(args and args[0].lower() in ("active", "激活", "已激活"))

        try:
            from src.agent.factory import get_skill_manager, DEFAULT_AGENT_SKILLS

            config = get_config()
            sm = get_skill_manager(config)

            configured_active: set = set(config.agent_skills or DEFAULT_AGENT_SKILLS)

            all_skills = sm.list_skills()
            if not all_skills:
                msg = "📋 No strategies available. Check strategies/ directory." if is_en else "📋 暂无可用策略。请检查 strategies/ 目录。"
                return BotResponse.text_response(msg)

            skills = all_skills
            if show_active_only:
                skills = [s for s in all_skills if s.name in configured_active]
                if not skills:
                    msg = "📋 No active strategies found." if is_en else "📋 当前没有激活的策略。"
                    return BotResponse.text_response(msg)

            if is_en:
                categories = {
                    "trend": "📈 Trend Following",
                    "pattern": "📊 Price Patterns",
                    "reversal": "🔄 Mean Reversion",
                    "framework": "🧩 Structural Frameworks",
                }
                lines = ["📋 **Quantitative Strategy Library**", ""]
            else:
                categories = {
                    "trend": "📈 趋势类",
                    "pattern": "📊 形态类",
                    "reversal": "🔄 反转类",
                    "framework": "🧩 框架类",
                }
                lines = ["📋 **交易策略列表**", ""]

            grouped = {}
            for skill in skills:
                cat = skill.category or "trend"
                grouped.setdefault(cat, []).append(skill)

            ordered_keys = ["trend", "pattern", "reversal", "framework"]
            for cat_key in ordered_keys + [k for k in grouped if k not in ordered_keys]:
                cat_skills = grouped.get(cat_key)
                if not cat_skills:
                    continue
                cat_label = categories.get(cat_key, f"📌 {cat_key.title()}")
                lines.append(f"**{cat_label}**")
                for s in cat_skills:
                    status = "✅" if s.name in configured_active else "⬜"
                    source_tag = " (custom)" if is_en else " (自定义)"
                    source_str = source_tag if (s.source and s.source != "builtin") else ""
                    lines.append(f"  {status} `{s.name}` — **{s.display_name}**{source_str}")
                    if s.description:
                        lines.append(f"      _{s.description}_")
                lines.append("")

            active_count = sum(1 for s in all_skills if s.name in configured_active)
            total_count = len(all_skills)
            
            if is_en:
                lines.append(f"Total: {total_count} strategies ({active_count} active)")
                lines.append("\n💡 Use `/ask <ticker> <strategy_id>` to analyze with a specific skill.")
            else:
                lines.append(f"共 {total_count} 个策略，已激活 {active_count} 个")
                lines.append("\n💡 使用 `/ask <股票代码> <策略名>` 指定策略分析")

            return BotResponse.markdown_response("\n".join(lines))

        except Exception as e:
            logger.error(f"[StrategiesCommand] Failed to list strategies: {e}")
            err_msg = f"Failed to list strategies: {e}" if is_en else f"获取策略列表失败: {e}"
            return BotResponse.error_response(err_msg)
