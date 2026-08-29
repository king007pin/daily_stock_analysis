# -*- coding: utf-8 -*-
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot.models import BotMessage, BotResponse, ChatType
from bot.platforms.telegram import TelegramPlatform


def _make_platform(
    bot_token: str = "123456:ABC-DEF",
    webhook_secret: str = "my_secret_token",
    allowed_users: str = "",
    bot_name: str = "MyStockBot",
) -> TelegramPlatform:
    with patch(
        "src.config.get_config",
        return_value=SimpleNamespace(
            telegram_bot_token=bot_token,
            telegram_webhook_secret=webhook_secret,
            telegram_allowed_users=allowed_users,
            telegram_bot_name=bot_name,
        ),
    ):
        return TelegramPlatform()


def test_verify_request_with_valid_secret():
    platform = _make_platform(webhook_secret="correct_secret")
    headers = {"X-Telegram-Bot-Api-Secret-Token": "correct_secret"}
    assert platform.verify_request(headers, b"{}") is True


def test_verify_request_with_invalid_secret():
    platform = _make_platform(webhook_secret="correct_secret")
    headers = {"X-Telegram-Bot-Api-Secret-Token": "wrong_secret"}
    assert platform.verify_request(headers, b"{}") is False


def test_verify_request_missing_secret_header():
    platform = _make_platform(webhook_secret="correct_secret")
    headers = {}
    assert platform.verify_request(headers, b"{}") is False


def test_verify_request_no_secret_configured_allows_with_token():
    platform = _make_platform(bot_token="valid_token", webhook_secret="")
    assert platform.verify_request({}, b"{}") is True


def test_verify_request_no_token_configured_fails():
    platform = _make_platform(bot_token="", webhook_secret="")
    assert platform.verify_request({}, b"{}") is False


def test_parse_message_private_chat():
    platform = _make_platform()
    update_data = {
        "update_id": 1001,
        "message": {
            "message_id": 42,
            "from": {
                "id": 999,
                "is_bot": False,
                "first_name": "Alice",
                "username": "alice_w",
            },
            "chat": {
                "id": 999,
                "type": "private",
                "first_name": "Alice",
            },
            "date": 1700000000,
            "text": "/analyze AAPL",
        },
    }

    msg = platform.parse_message(update_data)
    assert msg is not None
    assert msg.platform == "telegram"
    assert msg.message_id == "42"
    assert msg.user_id == "999"
    assert msg.user_name == "alice_w"
    assert msg.chat_id == "999"
    assert msg.chat_type == ChatType.PRIVATE
    assert msg.content == "/analyze AAPL"
    assert msg.raw_content == "/analyze AAPL"


def test_parse_message_group_chat_and_strip_bot_mention():
    platform = _make_platform(bot_name="MyStockBot")
    update_data = {
        "update_id": 1002,
        "message": {
            "message_id": 43,
            "from": {
                "id": 888,
                "first_name": "Bob",
            },
            "chat": {
                "id": -1001234567,
                "type": "group",
                "title": "Stock Traders",
            },
            "date": 1700000000,
            "text": "/analyze@MyStockBot 600519",
        },
    }

    msg = platform.parse_message(update_data)
    assert msg is not None
    assert msg.chat_type == ChatType.GROUP
    assert msg.chat_id == "-1001234567"
    assert msg.content == "/analyze 600519"
    assert msg.user_name == "Bob"


def test_parse_message_whitelist_allowed_user():
    platform = _make_platform(allowed_users="999,charlie")
    update_data = {
        "update_id": 1003,
        "message": {
            "message_id": 44,
            "from": {"id": 999, "username": "other_name"},
            "chat": {"id": 999, "type": "private"},
            "text": "/market",
        },
    }
    assert platform.parse_message(update_data) is not None


def test_parse_message_whitelist_blocked_user():
    platform = _make_platform(allowed_users="999,charlie")
    update_data = {
        "update_id": 1004,
        "message": {
            "message_id": 45,
            "from": {"id": 12345, "username": "stranger"},
            "chat": {"id": 12345, "type": "private"},
            "text": "/market",
        },
    }
    assert platform.parse_message(update_data) is None


def test_parse_message_callback_query():
    platform = _make_platform()
    update_data = {
        "update_id": 1005,
        "callback_query": {
            "id": "cb_1",
            "from": {"id": 999, "username": "alice_w"},
            "message": {
                "message_id": 50,
                "chat": {"id": 999, "type": "private"},
            },
            "data": "/analyze NVDA",
        },
    }
    msg = platform.parse_message(update_data)
    assert msg is not None
    assert msg.content == "/analyze NVDA"
    assert msg.chat_id == "999"


def test_format_response():
    platform = _make_platform()
    msg = BotMessage(
        platform="telegram",
        message_id="1",
        user_id="999",
        user_name="alice",
        chat_id="999",
        chat_type=ChatType.PRIVATE,
        content="/help",
        raw_data={"_message_thread_id": 123},
    )
    resp = BotResponse.text_response("# 标题\n内容详情")
    webhook_resp = platform.format_response(resp, msg)

    assert webhook_resp.status_code == 200
    assert webhook_resp.body["method"] == "sendMessage"
    assert webhook_resp.body["chat_id"] == "999"
    assert webhook_resp.body["message_thread_id"] == 123
    assert "*标题*" in webhook_resp.body["text"]


@patch("bot.platforms.telegram.requests.post")
def test_send_followup_success(mock_post):
    mock_post.return_value = MagicMock(status_code=200)
    platform = _make_platform()
    msg = BotMessage(
        platform="telegram",
        message_id="1",
        user_id="999",
        user_name="alice",
        chat_id="999",
        chat_type=ChatType.PRIVATE,
        content="/analyze AAPL",
    )
    resp = BotResponse.text_response("Analysis Result for AAPL")
    success = platform.send_followup(resp, msg)

    assert success is True
    mock_post.assert_called_once()
    payload = mock_post.call_args[1]["json"]
    assert payload["chat_id"] == "999"
    assert "Analysis Result for AAPL" in payload["text"]


@patch("bot.platforms.telegram.requests.post")
def test_send_followup_markdown_failure_fallbacks_to_plain_text(mock_post):
    # 第一次返回 400 (Bad Markdown), 第二次纯文本返回 200
    mock_resp_400 = MagicMock(status_code=400, text="Bad Request: can't parse entities")
    mock_resp_200 = MagicMock(status_code=200)
    mock_post.side_effect = [mock_resp_400, mock_resp_200]

    platform = _make_platform()
    msg = BotMessage(
        platform="telegram",
        message_id="1",
        user_id="999",
        user_name="alice",
        chat_id="999",
        chat_type=ChatType.PRIVATE,
        content="/analyze AAPL",
    )
    resp = BotResponse.text_response("Malformed Markdown _* [test")
    success = platform.send_followup(resp, msg)

    assert success is True
    assert mock_post.call_count == 2
    second_payload = mock_post.call_args_list[1][1]["json"]
    assert "parse_mode" not in second_payload


@patch("bot.platforms.telegram.requests.post")
def test_send_followup_chunks_long_message(mock_post):
    mock_post.return_value = MagicMock(status_code=200)
    platform = _make_platform()
    msg = BotMessage(
        platform="telegram",
        message_id="1",
        user_id="999",
        user_name="alice",
        chat_id="999",
        chat_type=ChatType.PRIVATE,
        content="/analyze",
    )
    long_content = "Line of text\n" * 500  # > 4096 chars
    resp = BotResponse.text_response(long_content)
    success = platform.send_followup(resp, msg)

    assert success is True
    assert mock_post.call_count >= 2


@patch("bot.platforms.telegram.requests.post")
def test_send_chat_action(mock_post):
    mock_post.return_value = MagicMock(status_code=200)
    platform = _make_platform()
    assert platform.send_chat_action("999", "typing") is True
    payload = mock_post.call_args[1]["json"]
    assert payload["chat_id"] == "999"
    assert payload["action"] == "typing"
