# -*- coding: utf-8 -*-
"""
===================================
股票分析命令
===================================

分析指定股票，调用 AI 多智能体生成量化分析报告。
支持根据 REPORT_LANGUAGE 自动切换英文/中文回复。
"""

import logging
import re
from typing import List, Optional

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from data_provider.base import canonical_stock_code
from src.config import get_config
from src.services.stock_code_utils import resolve_index_stock_code_for_analysis

logger = logging.getLogger(__name__)


class AnalyzeCommand(BotCommand):
    """
    股票分析命令
    
    分析指定股票代码，生成 AI 分析报告并推送。
    
    用法：
        /analyze AAPL          - 分析美股
        /analyze RELIANCE.NS   - 分析印度 NSE 股票
        /analyze 600519        - 分析 A 股
        /analyze 600519 full   - 分析并生成完整报告
    """
    
    @property
    def name(self) -> str:
        return "analyze"
    
    @property
    def aliases(self) -> List[str]:
        return ["a", "分析", "查"]
    
    @property
    def description(self) -> str:
        is_en = getattr(get_config(), "report_language", "zh") == "en"
        return "Run multi-agent quantitative stock analysis" if is_en else "分析指定股票"
    
    @property
    def usage(self) -> str:
        return "/analyze <ticker> [full]"
    
    def validate_args(self, args: List[str]) -> Optional[str]:
        """验证参数"""
        is_en = getattr(get_config(), "report_language", "zh") == "en"
        if not args:
            return "Please provide a stock ticker (e.g. /analyze AAPL)" if is_en else "请输入股票代码"
        
        raw_code = args[0].strip()
        cleaned = canonical_stock_code(raw_code)
        
        # 允许格式：
        # A股：6位数字
        # 港股：0700.HK, HK00700
        # 印度股：RELIANCE.NS, TCS.BO, 500325.BO
        # 美股：AAPL, TSLA, BRK.B
        # 日韩台：7203.T, 005930.KS, 2330.TW
        valid_pattern = re.match(
            r"^(\d{5,6}|[A-Za-z0-9&_\.\-]+)$",
            raw_code,
            re.IGNORECASE,
        )

        if not valid_pattern and not cleaned:
            if is_en:
                return f"Invalid stock ticker: {raw_code} (Examples: AAPL, RELIANCE.NS, 0700.HK, 600519)"
            return f"无效的股票代码: {raw_code}（例如 AAPL, RELIANCE.NS, 0700.HK, 600519）"
        
        return None
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行分析命令"""
        is_en = getattr(get_config(), "report_language", "zh") == "en"
        code = resolve_index_stock_code_for_analysis(args[0])
        
        report_type = "simple"
        if len(args) > 1 and args[1].lower() in ["full", "完整", "详细"]:
            report_type = "full"
            
        logger.info(f"[AnalyzeCommand] 分析股票: {code}, 报告类型: {report_type}")
        
        try:
            from src.services.task_service import get_task_service
            from src.enums import ReportType
            
            service = get_task_service()
            
            result = service.submit_analysis(
                code=code,
                report_type=ReportType.from_str(report_type),
                source_message=message,
            )
            
            if result.get("success"):
                task_id = result.get("task_id", "")
                if is_en:
                    return BotResponse.markdown_response(
                        f"✅ **Analysis Task Queued**\n\n"
                        f"• **Stock**: `{code}`\n"
                        f"• **Report Mode**: `{report_type.title()}`\n"
                        f"• **Task ID**: `{task_id[:20]}...`\n\n"
                        f"Multi-Agent quant pipeline is running. Full report will be delivered here shortly."
                    )
                return BotResponse.markdown_response(
                    f"✅ **分析任务已提交**\n\n"
                    f"• 股票代码: `{code}`\n"
                    f"• 报告类型: {ReportType.from_str(report_type).display_name}\n"
                    f"• 任务 ID: `{task_id[:20]}...`\n\n"
                    f"分析完成后将自动推送结果。"
                )
            else:
                error = result.get("error", "Unknown error" if is_en else "未知错误")
                err_prefix = "Failed to submit analysis" if is_en else "提交分析任务失败"
                return BotResponse.error_response(f"{err_prefix}: {error}")
                
        except Exception as e:
            logger.error(f"[AnalyzeCommand] 执行失败: {e}")
            err_prefix = "Analysis failed" if is_en else "分析失败"
            return BotResponse.error_response(f"{err_prefix}: {str(e)[:100]}")
