# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

from bot.models import BotMessage, BotResponse, ChatType
from bot.platforms.telegram import TelegramPlatform
from bot.platforms.telegram_polling import (
    TelegramPollingClient,
    start_telegram_polling_background,
    stop_telegram_polling,
    is_telegram_polling_running,
)


@patch("bot.platforms.telegram_polling.requests.get")
def test_polling_client_run_once_processes_updates(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "ok": True,
            "result": [
                {
                    "update_id": 101,
                    "message": {
                        "message_id": 1,
                        "from": {"id": 123, "username": "user1"},
                        "chat": {"id": 123, "type": "private"},
                        "text": "/help",
                    },
                },
                {
                    "update_id": 102,
                    "message": {
                        "message_id": 2,
                        "from": {"id": 123, "username": "user1"},
                        "chat": {"id": 123, "type": "private"},
                        "text": "/market",
                    },
                },
            ],
        },
    )

    platform = MagicMock(spec=TelegramPlatform)
    platform.bot_token = "123456:TOKEN"
    platform.parse_message.side_effect = [
        BotMessage(
            platform="telegram",
            message_id="1",
            user_id="123",
            user_name="user1",
            chat_id="123",
            chat_type=ChatType.PRIVATE,
            content="/help",
        ),
        BotMessage(
            platform="telegram",
            message_id="2",
            user_id="123",
            user_name="user1",
            chat_id="123",
            chat_type=ChatType.PRIVATE,
            content="/market",
        ),
    ]

    client = TelegramPollingClient(platform=platform)
    processed_count = client.run_once()

    assert processed_count == 2
    assert client.offset == 103


@patch("bot.platforms.telegram_polling.requests.get")
def test_polling_client_handles_network_failure(mock_get):
    mock_get.side_effect = Exception("Connection timeout")
    platform = MagicMock(spec=TelegramPlatform)
    platform.bot_token = "123456:TOKEN"

    client = TelegramPollingClient(platform=platform)
    assert client.run_once() == 0


@patch("bot.platforms.telegram.TelegramPlatform.delete_webhook")
@patch("src.config.get_config")
def test_start_and_stop_polling_background(mock_config, mock_delete_webhook):
    mock_config.return_value = MagicMock(
        telegram_bot_token="123456:TOKEN",
        telegram_webhook_secret="",
        telegram_allowed_users="",
        telegram_bot_name="",
    )

    with patch.object(TelegramPollingClient, "run_once", return_value=0):
        started = start_telegram_polling_background()
        assert started is True
        assert is_telegram_polling_running() is True

        stop_telegram_polling()
