# -*- coding: utf-8 -*-
"""
===================================
Telegram 平台适配器
===================================

负责：
1. 验证 Telegram Webhook 请求签名 / Secret Token
2. 解析 Telegram 消息与命令为统一格式 (BotMessage)
3. 格式化并发送 Telegram 消息 (支持 Markdown / 纯文本降级 / 超长分块)
4. 支持 Webhook 与 Long-Polling 两种双向交互模式
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from bot.models import BotMessage, BotResponse, ChatType, WebhookResponse
from bot.platforms.base import BotPlatform
from src.formatters import strip_hidden_markdown_metadata

logger = logging.getLogger(__name__)

# Telegram 单条消息最大长度
TELEGRAM_MAX_MESSAGE_LENGTH = 4096


class TelegramPlatform(BotPlatform):
    """Telegram 双向交互平台适配器"""

    def __init__(self):
        from src.config import get_config

        config = get_config()
        self._bot_token = (getattr(config, "telegram_bot_token", None) or "").strip()
        self._webhook_secret = (getattr(config, "telegram_webhook_secret", None) or "").strip()
        self._bot_name = (getattr(config, "telegram_bot_name", None) or "").strip().lstrip("@")
        
        allowed_raw = getattr(config, "telegram_allowed_users", None) or ""
        self._allowed_users: Set[str] = {
            u.strip().lstrip("@").lower()
            for u in allowed_raw.split(",")
            if u.strip()
        }

    @property
    def platform_name(self) -> str:
        return "telegram"

    @property
    def bot_token(self) -> str:
        return self._bot_token

    def verify_request(self, headers: Dict[str, str], body: bytes) -> bool:
        """
        验证 Telegram Webhook 请求。

        如果配置了 TELEGRAM_WEBHOOK_SECRET，则检查请求头 X-Telegram-Bot-Api-Secret-Token。
        未配置 Secret 时，只要配置了 TELEGRAM_BOT_TOKEN 即接受。
        """
        if self._webhook_secret:
            normalized_headers = {str(k).lower(): str(v) for k, v in headers.items()}
            received_secret = normalized_headers.get("x-telegram-bot-api-secret-token", "").strip()
            if not received_secret:
                logger.warning("[Telegram] 缺少 X-Telegram-Bot-Api-Secret-Token 头，拒绝请求")
                return False
            if received_secret != self._webhook_secret:
                logger.warning("[Telegram] Webhook Secret 不匹配，拒绝请求")
                return False
            return True

        if not self._bot_token:
            logger.warning("[Telegram] 未配置 TELEGRAM_BOT_TOKEN，拒绝请求")
            return False

        return True

    def handle_webhook(
        self,
        headers: Dict[str, str],
        body: bytes,
        data: Dict[str, Any],
    ) -> Tuple[Optional[BotMessage], Optional[WebhookResponse]]:
        """处理 Telegram Webhook 请求"""
        if not self.verify_request(headers, body):
            return None, WebhookResponse.error("Invalid Telegram secret token", 401)

        message = self.parse_message(data)
        return message, None

    def _is_user_allowed(self, user_id: str, username: str) -> bool:
        """检查用户是否在白名单中（若未配置白名单则允许所有用户）"""
        if not self._allowed_users:
            return True
        return user_id in self._allowed_users or username.lower() in self._allowed_users

    def _clean_bot_mention(self, text: str) -> str:
        """清理命令中针对本 Bot 的 @mention，例如 /analyze@MyStockBot AAPL -> /analyze AAPL"""
        if not text:
            return ""
        text = text.strip()
        # 去除 /command@botname 中的 @botname
        text = re.sub(r"^/([a-zA-Z0-9_]+)@\S+", r"/\1", text)
        # 去除开头的 @botname
        if self._bot_name:
            text = re.sub(rf"^@{re.escape(self._bot_name)}\s+", "", text, flags=re.IGNORECASE)
        return text.strip()

    def parse_message(self, data: Dict[str, Any]) -> Optional[BotMessage]:
        """
        解析 Telegram Update 对象为统一的 BotMessage。

        支持：
        - message (常规文字消息)
        - edited_message (编辑后的消息)
        - channel_post (频道消息)
        - callback_query (按钮回调)
        """
        if not isinstance(data, dict):
            return None

        # 优先提取 message，兼容 edited_message、channel_post、callback_query
        msg_payload = (
            data.get("message")
            or data.get("edited_message")
            or data.get("channel_post")
        )
        
        callback_query = data.get("callback_query")
        if callback_query and isinstance(callback_query, dict):
            msg_payload = callback_query.get("message") or {}
            raw_text = callback_query.get("data", "")
            from_user = callback_query.get("from", {})
        elif msg_payload and isinstance(msg_payload, dict):
            raw_text = msg_payload.get("text", "") or msg_payload.get("caption", "")
            from_user = msg_payload.get("from", {})
        else:
            return None

        if not raw_text:
            return None

        user_id = str(from_user.get("id") or "")
        username = str(from_user.get("username") or "").strip().lstrip("@")
        first_name = str(from_user.get("first_name") or "")
        last_name = str(from_user.get("last_name") or "")
        display_name = username or f"{first_name} {last_name}".strip() or user_id or "unknown"

        # 白名单过滤
        if not self._is_user_allowed(user_id, username):
            logger.warning(
                "[Telegram] 用户 %s (%s) 不在白名单内，忽略消息",
                user_id,
                username,
            )
            return None

        chat = msg_payload.get("chat", {})
        chat_id = str(chat.get("id") or user_id or "")
        chat_type_raw = str(chat.get("type", "private")).lower()

        if chat_type_raw == "private":
            chat_type = ChatType.PRIVATE
        elif chat_type_raw in {"group", "supergroup", "channel"}:
            chat_type = ChatType.GROUP
        else:
            chat_type = ChatType.UNKNOWN

        message_id = str(msg_payload.get("message_id") or data.get("update_id") or "")
        message_thread_id = msg_payload.get("message_thread_id")

        date_val = msg_payload.get("date")
        try:
            timestamp = datetime.fromtimestamp(int(date_val)) if date_val else datetime.now()
        except (ValueError, TypeError):
            timestamp = datetime.now()

        cleaned_content = self._clean_bot_mention(raw_text)

        # 检查是否 @了机器人
        mentioned = bool(
            self._bot_name
            and (
                f"@{self._bot_name.lower()}" in raw_text.lower()
                or raw_text.lower().startswith(f"/{self._bot_name.lower()}")
            )
        )

        return BotMessage(
            platform=self.platform_name,
            message_id=message_id,
            user_id=user_id,
            user_name=display_name,
            chat_id=chat_id,
            chat_type=chat_type,
            content=cleaned_content,
            raw_content=raw_text,
            mentioned=mentioned,
            mentions=[self._bot_name] if mentioned and self._bot_name else [],
            timestamp=timestamp,
            raw_data={
                **data,
                "_message_thread_id": message_thread_id,
            },
        )

    def _convert_to_telegram_markdown(self, content: str) -> str:
        """
        转换标准 Markdown 为 Telegram 兼容格式。
        复用与 TelegramSender 相同的转义规则。
        """
        # 转义非保留字符，避免 Markdown 解析错误
        # 保护链接 [text](url)
        links = []
        def save_link(match):
            links.append(match.group(0))
            return f"__LINK_PLACEHOLDER_{len(links)-1}__"

        temp_content = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', save_link, content)

        # 保护代码块
        code_blocks = []
        def save_code_block(match):
            code_blocks.append(match.group(0))
            return f"__CODE_BLOCK_{len(code_blocks)-1}__"

        temp_content = re.sub(r'```[\s\S]*?```', save_code_block, temp_content)

        # 保护行内代码
        inline_codes = []
        def save_inline_code(match):
            inline_codes.append(match.group(0))
            return f"__INLINE_CODE_{len(inline_codes)-1}__"

        temp_content = re.sub(r'`[^`]+`', save_inline_code, temp_content)

        # 移除不支持的标题语法 (# 标题)
        temp_content = re.sub(r'^#+\s*(.*?)$', r'*\1*', temp_content, flags=re.MULTILINE)

        # 还原保护的内容
        for i, code in enumerate(inline_codes):
            temp_content = temp_content.replace(f"__INLINE_CODE_{i}__", code)
        for i, block in enumerate(code_blocks):
            temp_content = temp_content.replace(f"__CODE_BLOCK_{i}__", block)
        for i, link in enumerate(links):
            temp_content = temp_content.replace(f"__LINK_PLACEHOLDER_{i}__", link)

        return temp_content

    def format_response(self, response: BotResponse, message: BotMessage) -> WebhookResponse:
        """格式化 Telegram Webhook 响应"""
        if not response.text:
            return WebhookResponse.success()

        sanitized_text = strip_hidden_markdown_metadata(response.text).strip()
        telegram_text = self._convert_to_telegram_markdown(sanitized_text)

        payload: Dict[str, Any] = {
            "method": "sendMessage",
            "chat_id": message.chat_id,
            "text": telegram_text,
            "parse_mode": "Markdown",
        }

        thread_id = message.raw_data.get("_message_thread_id")
        if thread_id:
            payload["message_thread_id"] = thread_id

        return WebhookResponse.success(payload)

    def send_chat_action(self, chat_id: str, action: str = "typing") -> bool:
        """发送聊天动作提示（如 typing 输入中）"""
        if not self._bot_token:
            return False
        try:
            url = f"https://api.telegram.org/bot{self._bot_token}/sendChatAction"
            resp = requests.post(url, json={"chat_id": chat_id, "action": action}, timeout=5)
            return resp.status_code == 200
        except Exception as exc:
            logger.debug("[Telegram] 发送 chat action 失败: %s", exc)
            return False

    def _split_chunks(self, text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> List[str]:
        """将长消息按段落拆分为多个满足长度限制的块"""
        if len(text) <= max_length:
            return [text]

        chunks = []
        current_chunk = []
        current_length = 0

        for line in text.split("\n"):
            line_len = len(line) + 1
            if current_length + line_len > max_length and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = [line]
                current_length = line_len
            else:
                current_chunk.append(line)
                current_length += line_len

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        return chunks

    def _send_single_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: Optional[str] = "Markdown",
        message_thread_id: Optional[Any] = None,
        timeout: float = 15.0,
    ) -> bool:
        """发送单条 Telegram 消息，在 Markdown 失败时自动降级到纯文本"""
        if not self._bot_token:
            logger.warning("[Telegram] 未配置 TELEGRAM_BOT_TOKEN，无法发送消息")
            return False

        api_url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        try:
            resp = requests.post(api_url, json=payload, timeout=timeout)
            if resp.status_code == 200:
                return True

            # 如果是因为 Markdown 格式错误导致的 400，自动降级为纯文本重试
            if resp.status_code == 400 and parse_mode:
                logger.warning(
                    "[Telegram] Markdown 消息发送失败 (400)，尝试纯文本降级发送: %s",
                    resp.text,
                )
                payload.pop("parse_mode", None)
                retry_resp = requests.post(api_url, json=payload, timeout=timeout)
                return retry_resp.status_code == 200

            logger.error("[Telegram] 发送消息失败: status=%s, response=%s", resp.status_code, resp.text)
            return False
        except Exception as exc:
            logger.error("[Telegram] 发送消息异常: %s", exc)
            return False

    def send_followup(
        self,
        response: BotResponse,
        message: BotMessage,
    ) -> bool:
        """
        异步或后续主动向用户发送回复消息。
        自动处理超长消息分块与 Markdown 格式转换。
        """
        if not response.text:
            return True

        chat_id = message.chat_id
        if not chat_id:
            logger.warning("[Telegram] 无法发送消息：缺少 chat_id")
            return False

        sanitized_text = strip_hidden_markdown_metadata(response.text).strip()
        if not sanitized_text:
            return True

        telegram_text = self._convert_to_telegram_markdown(sanitized_text)
        chunks = self._split_chunks(telegram_text)
        thread_id = message.raw_data.get("_message_thread_id")

        success = True
        for chunk in chunks:
            ok = self._send_single_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode="Markdown",
                message_thread_id=thread_id,
            )
            if not ok:
                success = False

        return success

    def set_webhook(self, webhook_url: str, secret_token: Optional[str] = None) -> bool:
        """注册 Telegram Webhook URL"""
        if not self._bot_token:
            return False
        try:
            url = f"https://api.telegram.org/bot{self._bot_token}/setWebhook"
            payload: Dict[str, Any] = {
                "url": webhook_url,
                "allowed_updates": ["message", "edited_message", "callback_query"],
            }
            secret = secret_token or self._webhook_secret
            if secret:
                payload["secret_token"] = secret

            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if data.get("ok"):
                logger.info("[Telegram] 成功设置 Webhook: %s", webhook_url)
                return True
            logger.error("[Telegram] 设置 Webhook 失败: %s", data)
            return False
        except Exception as exc:
            logger.error("[Telegram] 设置 Webhook 异常: %s", exc)
            return False

    def delete_webhook(self) -> bool:
        """删除 Telegram Webhook（切换到 Polling 模式时必须先删除）"""
        if not self._bot_token:
            return False
        try:
            url = f"https://api.telegram.org/bot{self._bot_token}/deleteWebhook"
            resp = requests.post(url, json={"drop_pending_updates": False}, timeout=10)
            data = resp.json()
            return bool(data.get("ok"))
        except Exception as exc:
            logger.warning("[Telegram] 删除 Webhook 异常: %s", exc)
            return False
