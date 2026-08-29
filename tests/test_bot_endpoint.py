# -*- coding: utf-8 -*-
import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from api.app import create_app
from bot.models import BotMessage, BotResponse, ChatType


def test_bot_status_endpoint():
    app = create_app()
    client = TestClient(app)

    response = client.get("/api/v1/bot/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "telegram" in data["platforms"]
    assert "dingtalk" in data["platforms"]
    assert "feishu" in data["platforms"]


@patch("bot.platforms.telegram.TelegramPlatform.handle_webhook")
def test_telegram_webhook_endpoint(mock_handle_webhook):
    fake_msg = BotMessage(
        platform="telegram",
        message_id="101",
        user_id="999",
        user_name="alice",
        chat_id="999",
        chat_type=ChatType.PRIVATE,
        content="/help",
    )
    mock_handle_webhook.return_value = (fake_msg, None)

    app = create_app()
    client = TestClient(app)

    payload = {
        "update_id": 12345,
        "message": {
            "message_id": 101,
            "from": {"id": 999, "username": "alice"},
            "chat": {"id": 999, "type": "private"},
            "text": "/help",
        },
    }

    with patch("bot.dispatcher.CommandDispatcher.dispatch_async") as mock_dispatch:
        mock_dispatch.return_value = BotResponse.text_response("Help content")
        response = client.post("/api/v1/bot/telegram", json=payload)

    assert response.status_code == 200
    assert response.headers.get("content-type") == "application/json"
    res_data = response.json()
    assert res_data.get("method") == "sendMessage"
