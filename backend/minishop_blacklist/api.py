"""Administrator-only settings and lookup routes for the Minishop web app."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable

from aiohttp import web
from bot.app.web.admin_api_impl.auth import _require_admin_user_id
from bot.app.web.context import get_session_factory

from .settings import (
    EVENT_NAMES,
    MAX_REFRESH_HOURS,
    DisplaySettings,
    Settings,
    SourceURLValidationError,
    validate_source_urls,
)
from .storage import (
    Match,
    SourceStatus,
    get_matches,
    get_settings,
    list_matched_users,
    list_source_statuses,
    save_settings,
)

_BASE = "/api/admin/minishop-blacklist"
_MAX_INT64 = 2**63 - 1
_MAX_INT32 = 2**31 - 1
_MAX_PAGE_SIZE = 100
_DECIMAL = re.compile(r"-?[0-9]{1,19}\Z")


class InvalidRequest(ValueError):
    """A stable error code for the administrator interface."""


def _optional_id(value: object, *, low: int, high: int, error: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and _DECIMAL.fullmatch(value):
        number = int(value)
        if low <= number <= high and number != 0:
            return number
    raise InvalidRequest(error)


def parse_settings(body: dict[str, object]) -> Settings:
    sources = body.get("sources")
    if not isinstance(sources, list) or not all(isinstance(url, str) for url in sources):
        raise InvalidRequest("invalid_sources")
    try:
        urls = validate_source_urls(sources)
    except SourceURLValidationError as exc:
        raise InvalidRequest("invalid_sources") from exc

    hours = body.get("refreshHours")
    if type(hours) is not int or not 1 <= hours <= MAX_REFRESH_HOURS:
        raise InvalidRequest("invalid_refresh_hours")
    display = body.get("display")
    if not isinstance(display, dict) or any(
        type(display.get(key)) is not bool for key in ("users", "userCard", "ticket")
    ):
        raise InvalidRequest("invalid_display")

    chat_id = _optional_id(
        body.get("chatId"), low=-(2**63), high=_MAX_INT64, error="invalid_chat_id"
    )
    topic_id = _optional_id(body.get("topicId"), low=1, high=_MAX_INT32, error="invalid_topic_id")
    events = body.get("events")
    if not isinstance(events, list) or any(event not in EVENT_NAMES for event in events):
        raise InvalidRequest("invalid_events")
    if chat_id is None and (events or topic_id is not None):
        raise InvalidRequest("chat_required")

    return Settings(
        sources=urls,
        refresh_hours=hours,
        display=DisplaySettings(
            users=display["users"], user_card=display["userCard"], ticket=display["ticket"]
        ),
        chat_id=chat_id,
        topic_id=topic_id,
        events=tuple(event for event in EVENT_NAMES if event in events),
    )


async def _body(request: web.Request) -> dict[str, object]:
    try:
        value = await request.json()
    except (json.JSONDecodeError, UnicodeError, LookupError) as exc:
        raise InvalidRequest("invalid_json") from exc
    if not isinstance(value, dict):
        raise InvalidRequest("invalid_json")
    return value


def _response(*, ok: bool = True, status: int = 200, **payload: object) -> web.Response:
    return web.json_response(
        {"ok": ok, **payload}, status=status, headers={"Cache-Control": "private, no-store"}
    )


def _guard(
    handler: Callable[[web.Request], Awaitable[web.Response]],
) -> Callable[[web.Request], Awaitable[web.Response]]:
    async def guarded(request: web.Request) -> web.Response:
        _require_admin_user_id(request)
        try:
            return await handler(request)
        except InvalidRequest as exc:
            return _response(ok=False, error=str(exc), status=400)

    return guarded


def _settings_record(settings: Settings, statuses: list[SourceStatus]) -> web.Response:
    return _response(
        settings={
            "sources": settings.sources,
            "refreshHours": settings.refresh_hours,
            "display": {
                "users": settings.display.users,
                "userCard": settings.display.user_card,
                "ticket": settings.display.ticket,
            },
            "chatId": str(settings.chat_id) if settings.chat_id is not None else None,
            "topicId": str(settings.topic_id) if settings.topic_id is not None else None,
            "events": settings.events,
        },
        sourceStatuses=[
            {
                "url": item.url,
                "lastSuccessAt": item.last_success_at and item.last_success_at.isoformat(),
                "lastAttemptAt": item.last_attempt_at and item.last_attempt_at.isoformat(),
                "lastError": item.last_error,
                "count": item.count,
                "invalidLines": item.invalid_lines,
            }
            for item in statuses
        ],
    )


def _match_record(match: Match) -> dict[str, object]:
    return {
        "state": match.state,
        "stale": match.stale,
        "reasons": [{"text": item.text, "links": item.links} for item in match.reasons],
    }


@_guard
async def read_settings(request: web.Request) -> web.Response:
    async with get_session_factory(request)() as session:
        settings = await get_settings(session)
        statuses = await list_source_statuses(session)
    return _settings_record(settings, statuses)


@_guard
async def update_settings(request: web.Request) -> web.Response:
    settings = parse_settings(await _body(request))
    async with get_session_factory(request)() as session:
        await save_settings(session, settings)
        statuses = await list_source_statuses(session)
        await session.commit()
    return _settings_record(settings, statuses)


@_guard
async def matches(request: web.Request) -> web.Response:
    limit = _optional_id(
        request.query.get("limit", "25"), low=1, high=_MAX_PAGE_SIZE, error="invalid_pagination"
    )
    offset = request.query.get("offset", "0")
    if limit is None or not re.fullmatch(r"[0-9]{1,10}", offset) or int(offset) > _MAX_INT32:
        raise InvalidRequest("invalid_pagination")

    async with get_session_factory(request)() as session:
        settings = await get_settings(session)
        if not settings.display.users:
            return _response(enabled=False, rows=[], total=0)
        rows, total = await list_matched_users(session, limit=limit, offset=int(offset))
        result = [
            {
                **_match_record(match),
                "userId": str(user.minishop_id or user.user_id),
                "telegramId": str(user.telegram_id),
                "name": " ".join(filter(None, (user.first_name, user.last_name))) or None,
                "username": user.username,
            }
            for user, match in rows
        ]
    return _response(enabled=True, rows=result, total=total)


@_guard
async def statuses(request: web.Request) -> web.Response:
    body = await _body(request)
    view = body.get("view")
    values = body.get("telegramIds")
    if view not in ("userCard", "ticket") or not isinstance(values, list) or len(values) > 100:
        raise InvalidRequest("invalid_status_request")
    telegram_ids: list[int] = []
    for value in values:
        telegram_id = _optional_id(value, low=1, high=_MAX_INT64, error="invalid_status_request")
        if telegram_id is None:
            raise InvalidRequest("invalid_status_request")
        telegram_ids.append(telegram_id)

    async with get_session_factory(request)() as session:
        settings = await get_settings(session)
        enabled = settings.display.user_card if view == "userCard" else settings.display.ticket
        result = await get_matches(session, telegram_ids) if enabled and telegram_ids else {}
    return _response(
        enabled=enabled,
        statuses={str(telegram_id): _match_record(match) for telegram_id, match in result.items()},
    )


def setup_routes(app: web.Application) -> None:
    app.router.add_get(f"{_BASE}/settings", read_settings)
    app.router.add_put(f"{_BASE}/settings", update_settings)
    app.router.add_get(f"{_BASE}/matches", matches)
    app.router.add_post(f"{_BASE}/status", statuses)
