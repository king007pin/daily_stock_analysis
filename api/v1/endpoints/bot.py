# -*- coding: utf-8 -*-
"""
===================================
Bot Webhook API Endpoints
===================================

提供各机器人平台 (Telegram, DingTalk, Feishu, Discord 等) 的 Webhook 接收端点。
"""

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel

from bot.handler import handle_webhook_async
from bot.platforms import (
    ALL_PLATFORMS,
    DINGTALK_STREAM_AVAILABLE,
    FEISHU_SDK_AVAILABLE,
    is_telegram_polling_running,
)
from src.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter()


class SetTelegramWebhookRequest(BaseModel):
    url: str
    secret_token: Optional[str] = None


@router.get("/status", summary="获取机器人平台接入状态")
async def get_bot_status() -> Dict[str, Any]:
    """获取所有机器人平台的配置与运行状态"""
    config = get_config()

    telegram_bot_token = getattr(config, "telegram_bot_token", None) or ""
    telegram_chat_id = getattr(config, "telegram_chat_id", None) or ""
    telegram_webhook_secret = getattr(config, "telegram_webhook_secret", None) or ""
    telegram_polling_enabled = getattr(config, "telegram_polling_enabled", False)

    return {
        "status": "ok",
        "bot_enabled": getattr(config, "bot_enabled", True),
        "platforms": {
            "telegram": {
                "configured": bool(telegram_bot_token),
                "has_chat_id": bool(telegram_chat_id),
                "has_webhook_secret": bool(telegram_webhook_secret),
                "polling_enabled": bool(telegram_polling_enabled),
                "polling_running": is_telegram_polling_running(),
                "webhook_path": "/api/v1/bot/telegram",
            },
            "dingtalk": {
                "configured": bool(getattr(config, "dingtalk_app_key", None)),
                "stream_enabled": bool(getattr(config, "dingtalk_stream_enabled", False)),
                "stream_available": DINGTALK_STREAM_AVAILABLE,
                "webhook_path": "/api/v1/bot/dingtalk",
            },
            "feishu": {
                "configured": bool(getattr(config, "feishu_app_id", None)),
                "stream_enabled": bool(getattr(config, "feishu_stream_enabled", False)),
                "stream_available": FEISHU_SDK_AVAILABLE,
                "webhook_path": "/api/v1/bot/feishu",
            },
            "discord": {
                "configured": bool(getattr(config, "discord_interactions_public_key", None)),
                "webhook_path": "/api/v1/bot/discord",
            },
        },
    }


@router.post("/telegram/set-webhook", summary="快捷配置 Telegram Webhook")
async def set_telegram_webhook(req: SetTelegramWebhookRequest):
    """向 Telegram API 注册 Webhook URL"""
    from bot.platforms.telegram import TelegramPlatform

    platform = TelegramPlatform()
    if not platform.bot_token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not configured"}

    success = platform.set_webhook(req.url, req.secret_token)
    return {"ok": success}


@router.post("/telegram/delete-webhook", summary="删除 Telegram Webhook")
async def delete_telegram_webhook():
    """删除 Telegram Webhook（切换到 Polling 模式时需要）"""
    from bot.platforms.telegram import TelegramPlatform

    platform = TelegramPlatform()
    if not platform.bot_token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not configured"}

    success = platform.delete_webhook()
    return {"ok": success}


@router.post("/{platform_name}", summary="通用机器人 Webhook 回调入口")
async def bot_webhook_endpoint(
    platform_name: str,
    request: Request,
) -> Response:
    """
    处理第三方机器人平台的 Webhook POST 请求。
    统一路由到 handle_webhook_async。
    """
    platform_key = platform_name.lower().strip()
    headers = dict(request.headers)
    body = await request.body()
    query_params = dict(request.query_params)

    webhook_resp = await handle_webhook_async(
        platform_name=platform_key,
        headers=headers,
        body=body,
        query_params=query_params,
    )

    content_data = webhook_resp.body
    if isinstance(content_data, (dict, list)):
        content_str = json.dumps(content_data, ensure_ascii=False)
        media_type = "application/json"
    elif isinstance(content_data, str):
        content_str = content_data
        media_type = "text/plain"
    elif content_data is None:
        content_str = ""
        media_type = "text/plain"
    else:
        content_str = str(content_data)
        media_type = "text/plain"

    return Response(
        content=content_str,
        status_code=webhook_resp.status_code,
        media_type=media_type,
    )
