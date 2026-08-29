# -*- coding: utf-8 -*-
"""
===================================
Telegram Long-Polling 客户端
===================================

用于在无需公网 IP / 域名 / Webhook 的环境下实现 Telegram 双向交互。
通过 Telegram Bot API 的 getUpdates 长轮询接收用户命令并自动回复。
"""

import asyncio
import logging
import threading
import time
from typing import Optional

import requests

from bot.dispatcher import get_dispatcher
from bot.platforms.telegram import TelegramPlatform

logger = logging.getLogger(__name__)

_polling_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_is_running = False


class TelegramPollingClient:
    """Telegram 长轮询客户端"""

    def __init__(self, platform: Optional[TelegramPlatform] = None):
        self.platform = platform or TelegramPlatform()
        self.bot_token = self.platform.bot_token
        self.offset = 0
        self.timeout = 25

    def is_configured(self) -> bool:
        return bool(self.bot_token)

    def run_once(self) -> int:
        """执行一次轮询，返回处理的 update 数量"""
        if not self.bot_token:
            return 0

        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params = {
            "offset": self.offset,
            "timeout": self.timeout,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        }

        try:
            resp = requests.get(url, params=params, timeout=self.timeout + 10)
            if resp.status_code != 200:
                logger.warning(
                    "[TelegramPolling] getUpdates 失败: status=%d, body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
                return 0

            data = resp.json()
            if not data.get("ok"):
                logger.warning("[TelegramPolling] getUpdates 返回错误: %s", data)
                return 0

            updates = data.get("result", [])
            for update in updates:
                update_id = update.get("update_id", 0)
                if update_id >= self.offset:
                    self.offset = update_id + 1

                self._process_update(update)

            return len(updates)
        except requests.exceptions.Timeout:
            return 0
        except requests.exceptions.RequestException as exc:
            logger.debug("[TelegramPolling] 轮询网络异常: %s", exc)
            time.sleep(2)
            return 0
        except Exception as exc:
            logger.error("[TelegramPolling] 处理 Update 异常: %s", exc, exc_info=True)
            return 0

    def _process_update(self, update: dict) -> None:
        """处理单条 Telegram Update"""
        try:
            bot_msg = self.platform.parse_message(update)
            if not bot_msg or not bot_msg.content:
                return

            logger.info(
                "[TelegramPolling] 收到来自 %s (%s) 的消息: %s",
                bot_msg.user_name,
                bot_msg.user_id,
                bot_msg.content[:60],
            )

            # 在后台线程处理耗时分析命令，避免阻塞 polling 循环
            threading.Thread(
                target=self._dispatch_and_reply,
                args=(bot_msg,),
                daemon=True,
            ).start()
        except Exception as exc:
            logger.error("[TelegramPolling] 解析消息失败: %s", exc)

    def _dispatch_and_reply(self, bot_msg) -> None:
        """分发命令并回复用户"""
        try:
            # 发送 typing 状态
            self.platform.send_chat_action(bot_msg.chat_id, "typing")

            dispatcher = get_dispatcher()
            response = dispatcher.dispatch(bot_msg)

            if response and response.text:
                self.platform.send_followup(response, bot_msg)
        except Exception as exc:
            logger.error("[TelegramPolling] 命令执行或回复失败: %s", exc, exc_info=True)
            try:
                from bot.models import BotResponse
                err_resp = BotResponse.error_response(f"执行异常: {exc}")
                self.platform.send_followup(err_resp, bot_msg)
            except Exception:
                pass

    def start_loop(self, stop_event: threading.Event) -> None:
        """进入持续长轮询循环"""
        logger.info("[TelegramPolling] 启动 Telegram Polling 循环...")
        # 启动前清理旧 Webhook，保证 getUpdates 正常工作
        self.platform.delete_webhook()

        consecutive_errors = 0
        while not stop_event.is_set():
            try:
                self.run_once()
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                wait_time = min(30, 2 ** min(consecutive_errors, 5))
                logger.error("[TelegramPolling] 轮询异常 (重试前等待 %ds): %s", wait_time, exc)
                time.sleep(wait_time)

        logger.info("[TelegramPolling] Telegram Polling 循环已停止")


def start_telegram_polling_background() -> bool:
    """在后台线程中启动 Telegram Long-Polling"""
    global _polling_thread

    if _is_running and _polling_thread and _polling_thread.is_alive():
        logger.info("[TelegramPolling] 服务已在运行中")
        return True

    from src.config import get_config
    config = get_config()
    token = getattr(config, "telegram_bot_token", None)
    if not token:
        logger.warning("[TelegramPolling] 未配置 TELEGRAM_BOT_TOKEN，无法启动 Polling")
        return False

    _stop_event.clear()
    client = TelegramPollingClient()

    def _worker():
        global _is_running
        _is_running = True
        try:
            client.start_loop(_stop_event)
        finally:
            _is_running = False

    _polling_thread = threading.Thread(
        target=_worker,
        name="telegram-polling-worker",
        daemon=True,
    )
    _polling_thread.start()
    logger.info("[TelegramPolling] Telegram 轮询服务已在后台启动")
    return True


def stop_telegram_polling() -> None:
    """停止 Telegram Long-Polling"""
    global _is_running
    _stop_event.set()
    _is_running = False
    logger.info("[TelegramPolling] 已发送停止信号")


def is_telegram_polling_running() -> bool:
    """检查 Telegram Polling 是否正在运行"""
    return bool(_is_running and _polling_thread and _polling_thread.is_alive())
