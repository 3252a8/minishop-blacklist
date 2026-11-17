"""Run the Telegram request and record only safe details of its outcome."""

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardMarkup
from bot.plugins.extensions import ExtensionError, OperationContext
from db.extension_models import ExtensionOperation
from pydantic import JsonValue
from sqlalchemy.exc import SQLAlchemyError

from .diagnostics import DiagnosticTrace, debug_enabled

_REQUEST_TIMEOUT = 25
logger = logging.getLogger(__name__)
_BAD_REQUESTS = (
    ("message thread not found", "telegram_topic_not_found", "Message thread not found"),
    ("topic_closed", "telegram_topic_not_found", "Topic is closed"),
    ("chat not found", "telegram_chat_not_found", "Chat not found"),
    ("can't parse entities", "telegram_invalid_message", "Can't parse message entities"),
    ("message is too long", "telegram_invalid_message", "Message is too long"),
    (
        "button_user_privacy_restricted",
        "telegram_invalid_button",
        "User privacy restricts the button",
    ),
    ("button_url_invalid", "telegram_invalid_button", "Button URL is invalid"),
    ("not enough rights", "telegram_forbidden", "Bot is missing permissions to send messages"),
    ("have no rights", "telegram_forbidden", "Bot is missing permissions to send messages"),
)


def classify_error(error: TelegramAPIError | TimeoutError) -> tuple[str, dict[str, JsonValue]]:
    """Classify stable Telegram errors without copying raw provider text or URLs."""
    details: dict[str, JsonValue] = {"type": type(error).__name__}
    if isinstance(error, TelegramRetryAfter):
        return "telegram_rate_limited", {**details, "retry_after": error.retry_after}
    if isinstance(error, TelegramMigrateToChat):
        return "telegram_chat_migrated", {
            **details,
            "migrate_to_chat_id": str(error.migrate_to_chat_id),
        }
    if isinstance(error, TelegramUnauthorizedError):
        return "telegram_unauthorized", details
    if isinstance(error, TelegramForbiddenError):
        return "telegram_forbidden", details
    if isinstance(error, TelegramNotFound):
        return "telegram_chat_not_found", details
    if isinstance(error, TelegramBadRequest):
        message = error.message.lower()
        for text, code, detail in _BAD_REQUESTS:
            if text in message:
                return code, {**details, "detail": detail}
    if isinstance(error, TelegramNetworkError):
        return "telegram_network_error", details
    if isinstance(error, TimeoutError):
        return "telegram_timeout", details
    return "telegram_api_error", details


def _safe_button_url(value: str) -> str:
    """Show public destinations while stripping credentials in private URLs."""
    try:
        url = urlsplit(value)
    except ValueError:
        return "[redacted]"
    if url.scheme == "tg" and url.netloc == "user" and url.query.startswith("id="):
        return value if url.query[3:].isdecimal() and not url.fragment else "[redacted]"
    if url.scheme not in {"http", "https"} or not url.hostname:
        return "[redacted]"
    netloc = url.netloc.rsplit("@", 1)[-1]
    query = url.query
    start = query.removeprefix("start=admin_user_")
    if query and not (
        url.hostname == "t.me"
        and start != query
        and start.replace("-", "").replace("_", "").isalnum()
    ):
        query = "[redacted]"
    fragment = "[redacted]" if url.fragment else ""
    return urlunsplit((url.scheme, netloc, url.path, query, fragment))


@dataclass(frozen=True)
class TelegramMessage:
    chat_id: int
    topic_id: int | None
    text: str
    keyboard: InlineKeyboardMarkup | None = None

    def trace_payload(self) -> dict[str, JsonValue]:
        request: dict[str, JsonValue] = {
            "chat_id": self.chat_id,
            "text": self.text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if self.topic_id is not None:
            request["message_thread_id"] = self.topic_id
        if self.keyboard is not None:
            inline_keyboard: list[JsonValue] = []
            for row in self.keyboard.inline_keyboard:
                buttons: list[JsonValue] = []
                for button in row:
                    fields: dict[str, JsonValue] = {"text": button.text}
                    if button.url is not None:
                        fields["url"] = _safe_button_url(button.url)
                    if button.callback_data is not None:
                        fields["callback_data"] = button.callback_data
                    buttons.append(fields)
                inline_keyboard.append(buttons)
            request["reply_markup"] = {"inline_keyboard": inline_keyboard}
        return request


async def record_delivery_failure(
    operation: OperationContext,
    trace: DiagnosticTrace,
    reason: str,
    *,
    response: dict[str, JsonValue] | None = None,
    request: dict[str, JsonValue] | None = None,
) -> None:
    try:
        async with operation.runtime.require_session_factory().begin() as session:
            if not await debug_enabled(session):
                return
            row = await session.get(ExtensionOperation, operation.operation_id)
            can_retry = row is None or row.attempts < row.max_attempts
            await trace.write(
                session,
                "retry" if can_retry else "error",
                reason,
                request=request,
                response=response,
            )
    except SQLAlchemyError:
        logger.warning("Could not commit the blacklist delivery failure trace")


async def send_telegram(
    operation: OperationContext, trace: DiagnosticTrace, message: TelegramMessage
) -> dict[str, JsonValue]:
    ctx = operation.runtime
    bot = ctx.bot
    if bot is None:
        await record_delivery_failure(operation, trace, "telegram_unavailable")
        raise ExtensionError("blacklist_telegram_unavailable", 502)

    request = message.trace_payload()
    session_factory = ctx.require_session_factory()
    await trace.persist(session_factory, "sending", request=request)
    try:
        operation.assert_current()
    except ExtensionError:
        # Generation changes are retried by Minishop without consuming an attempt.
        await trace.persist(session_factory, "retry", "generation_changed")
        raise

    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT):
            result = await bot.send_message(
                chat_id=message.chat_id,
                message_thread_id=message.topic_id,
                text=message.text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=message.keyboard,
            )
    except (TelegramAPIError, TimeoutError) as exc:
        reason, details = classify_error(exc)
        await record_delivery_failure(operation, trace, reason, request=request, response=details)
        raise ExtensionError("blacklist_telegram_send_failed", 502) from exc

    await trace.persist(
        session_factory, "sent", request=request, response={"message_id": result.message_id}
    )
    return {"sent": True, "message_id": result.message_id}
