"""Admin endpoints for the Telegram diagnostic tab."""

from aiohttp import web
from bot.app.web.context import get_session_factory

from .alerts import queue_test_notification
from .api import _BASE, _MAX_INT64, InvalidRequest, _body, _guard, _optional_id, _response
from .diagnostics import (
    DiagnosticEntry,
    clear_diagnostics,
    debug_enabled,
    list_diagnostics,
    set_debug_enabled,
)
from .storage import get_settings


def _entry_record(entry: DiagnosticEntry) -> dict[str, object]:
    return {
        "id": str(entry.id),
        "timestamp": entry.created_at.isoformat(),
        "event": entry.event,
        "status": entry.status,
        "reason": entry.reason,
        "operationId": entry.operation_id,
        "userId": str(entry.user_id) if entry.user_id is not None else None,
        "telegramId": str(entry.telegram_id) if entry.telegram_id is not None else None,
        "request": entry.request,
        "response": entry.response,
    }


@_guard
async def read_diagnostics(request: web.Request) -> web.Response:
    limit = _optional_id(
        request.query.get("limit", "30"), low=1, high=100, error="invalid_pagination"
    )
    before_id = _optional_id(
        request.query.get("beforeId"), low=1, high=_MAX_INT64, error="invalid_pagination"
    )
    if limit is None:
        raise InvalidRequest("invalid_pagination")
    async with get_session_factory(request)() as session:
        enabled = await debug_enabled(session)
        settings = await get_settings(session)
        entries, next_before_id = await list_diagnostics(session, limit=limit, before_id=before_id)
        return _response(
            enabled=enabled,
            chatId=str(settings.chat_id) if settings.chat_id is not None else None,
            topicId=str(settings.topic_id) if settings.topic_id is not None else None,
            entries=[_entry_record(entry) for entry in entries],
            nextBeforeId=next_before_id,
        )


@_guard
async def update_diagnostics(request: web.Request) -> web.Response:
    enabled = (await _body(request)).get("enabled")
    if type(enabled) is not bool:
        raise InvalidRequest("invalid_debug_enabled")
    async with get_session_factory(request)() as session:
        await set_debug_enabled(session, enabled)
        await session.commit()
    return _response(enabled=enabled)


@_guard
async def delete_diagnostics(request: web.Request) -> web.Response:
    async with get_session_factory(request)() as session:
        await clear_diagnostics(session)
        await session.commit()
    return _response()


@_guard
async def send_test_notification(request: web.Request) -> web.Response:
    async with get_session_factory(request)() as session:
        if not await debug_enabled(session):
            raise InvalidRequest("debug_disabled")
        settings = await get_settings(session)
        if settings.chat_id is None:
            raise InvalidRequest("chat_required")
        operation_id = await queue_test_notification(session, settings.chat_id, settings.topic_id)
        await session.commit()
    return _response(operationId=operation_id)


def setup_diagnostic_routes(app: web.Application) -> None:
    app.router.add_get(f"{_BASE}/diagnostics", read_diagnostics)
    app.router.add_put(f"{_BASE}/diagnostics", update_diagnostics)
    app.router.add_delete(f"{_BASE}/diagnostics", delete_diagnostics)
    app.router.add_post(f"{_BASE}/diagnostics/test", send_test_notification)
