# -*- coding: utf-8 -*-
"""
===================================
帮助命令
===================================

显示可用命令列表和使用说明，支持根据 REPORT_LANGUAGE 自动切换英文/中文。
"""

from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from src.config import get_config

COMMAND_EN_DESCRIPTIONS = {
    "help": "Show command help menu",
    "analyze": "Run multi-agent quantitative analysis for a stock",
    "ask": "Deep-dive QA and strategy analysis with Agent skills",
    "market": "View market overview and major indices",
    "strategies": "List 15 quantitative strategy skills",
    "batch": "Batch analyze watchlist stocks",
    "status": "Check system, data provider & model status",
    "chat": "Conversational stock chat",
    "history": "View historical analysis reports",
    "research": "Deep-dive multi-agent stock research",
}


class HelpCommand(BotCommand):
    """
    帮助命令
    
    显示所有可用命令的列表和使用说明。
    也可以查看特定命令的详细帮助。
    """
    
    @property
    def name(self) -> str:
        return "help"
    
    @property
    def aliases(self) -> List[str]:
        return ["h", "帮助", "?", "start"]
    
    @property
    def description(self) -> str:
        is_en = getattr(get_config(), "report_language", "zh") == "en"
        return "Show command help" if is_en else "显示帮助信息"
    
    @property
    def usage(self) -> str:
        return "/help [command]"
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行帮助命令"""
        from bot.dispatcher import get_dispatcher
        
        dispatcher = get_dispatcher()
        is_en = getattr(get_config(), "report_language", "zh") == "en"
        
        if args:
            cmd_name = args[0]
            command = dispatcher.get_command(cmd_name)
            
            if command is None:
                err_msg = f"Unknown command: {cmd_name}" if is_en else f"未知命令: {cmd_name}"
                return BotResponse.error_response(err_msg)
            
            help_text = self._format_command_help(command, dispatcher.command_prefix, is_en=is_en)
            return BotResponse.markdown_response(help_text)
        
        commands = dispatcher.list_commands(include_hidden=False)
        prefix = dispatcher.command_prefix
        
        help_text = self._format_help_list(commands, prefix, is_en=is_en)
        return BotResponse.markdown_response(help_text)
    
    def _format_help_list(self, commands: List[BotCommand], prefix: str, is_en: bool = False) -> str:
        """格式化命令列表"""
        if is_en:
            lines = [
                "📚 **Daily Quants - Command Help**",
                "",
                "**Available Commands:**",
                "",
            ]
            
            for cmd in commands:
                aliases_str = ""
                if cmd.aliases:
                    en_aliases = [a for a in cmd.aliases if a.isascii() and a not in {"start", "h", "?"}]
                    if en_aliases:
                        aliases_str = f" ({', '.join(prefix + a for a in en_aliases[:2])})"
                
                desc = COMMAND_EN_DESCRIPTIONS.get(cmd.name, cmd.description)
                lines.append(f"• `{prefix}{cmd.name}`{aliases_str} — {desc}")
                lines.append("")

            lines.extend([
                "---",
                f"💡 Type `{prefix}help <command>` for detailed syntax.",
                "",
                "**Examples:**",
                f"• `{prefix}analyze AAPL` (US Stock)",
                f"• `{prefix}analyze RELIANCE.NS` (Indian Stock)",
                f"• `{prefix}ask NVDA Is current price a good buy?`",
                f"• `{prefix}market` (Market overview)",
                f"• `{prefix}strategies` (List 15 quant strategies)",
            ])
            return "\n".join(lines)

        lines = [
            "📚 **股票分析助手 - 命令帮助**",
            "",
            "可用命令：",
            "",
        ]
        
        for cmd in commands:
            aliases_str = ""
            if cmd.aliases:
                en_aliases = [a for a in cmd.aliases if a.isascii()]
                if en_aliases:
                    aliases_str = f" ({', '.join(prefix + a for a in en_aliases[:2])})"
            
            lines.append(f"• {prefix}{cmd.name}{aliases_str} - {cmd.description}")
            lines.append("")

        lines.extend([
            "",
            "---",
            f"💡 输入 {prefix}help <命令名> 查看详细用法",
            "",
            "**示例：**",
            "",
            f"• {prefix}analyze AAPL",
            f"• {prefix}analyze RELIANCE.NS",
            f"• {prefix}market",
            f"• {prefix}batch",
        ])
        
        return "\n".join(lines)
    
    def _format_command_help(self, command: BotCommand, prefix: str, is_en: bool = False) -> str:
        """格式化单个命令的详细帮助"""
        desc = COMMAND_EN_DESCRIPTIONS.get(command.name, command.description) if is_en else command.description
        if is_en:
            lines = [
                f"📖 **{prefix}{command.name}** — {desc}",
                "",
                f"**Usage:** `{command.usage}`",
                "",
            ]
            if command.aliases:
                aliases = [f"`{prefix}{a}`" if a.isascii() else f"`{a}`" for a in command.aliases]
                lines.append(f"**Aliases:** {', '.join(aliases)}")
                lines.append("")
            if command.admin_only:
                lines.append("⚠️ **Admin privileges required**")
                lines.append("")
            return "\n".join(lines)

        lines = [
            f"📖 **{prefix}{command.name}** - {command.description}",
            "",
            f"**用法：** `{command.usage}`",
            "",
        ]
        if command.aliases:
            aliases = [f"`{prefix}{a}`" if a.isascii() else f"`{a}`" for a in command.aliases]
            lines.append(f"**别名：** {', '.join(aliases)}")
            lines.append("")
        if command.admin_only:
            lines.append("⚠️ **需要管理员权限**")
            lines.append("")
        return "\n".join(lines)
