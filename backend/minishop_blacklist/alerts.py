"""Queue Minishop events and deliver blacklist alerts from the worker."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from bot.infra import events
from bot.plugins.extensions import (
    DurableSubscription,
    ExtensionContributions,
    ExtensionError,
    JobHandler,
    OperationContext,
)
from bot.plugins.extensions.jobs import enqueue
from bot.plugins.packages import generation_is_current
from bot.plugins.spec import PluginContext
from db.extension_models import ExtensionOperation
from db.models import Payment, Subscription, User
from db.subscription_notification_models import SubscriptionLifecycleNotification
from pydantic import JsonValue
from sqlalchemy import exists, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .alert_text import render_alert
from .diagnostics import DiagnosticTrace, debug_enabled, lock_enabled_debug
from .settings import EventName, Settings
from .storage import get_matches, get_settings
from .telegram_delivery import TelegramMessage, record_delivery_failure, send_telegram

PLUGIN_ID = "minishop-blacklist"
_ALERT_JOB = "send-alert"
_PAYMENT_JOB = "send-payment-alert"
_TEST_JOB = "test-alert"
_BUS_EVENTS: dict[str, EventName] = {
    events.USER_REGISTERED: "register",
    events.TRIAL_ACTIVATED: "trial",
    events.SUBSCRIPTION_EXPIRED: "expired",
}
logger = logging.getLogger(__name__)


def _positive_id(value: object) -> int | None:
    return value if type(value) is int and 0 < value < 2**63 else None


def _account_id(value: object) -> int | None:
    return value if type(value) is int and -(2**63) <= value < 2**63 and value != 0 else None


def _event_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def event_job(
    event: EventName, payload: dict[str, object]
) -> tuple[str, dict[str, JsonValue]] | None:
    user_id = _account_id(payload.get("user_id"))
    if user_id is None:
        return None
    data: dict[str, JsonValue] = {"event": event, "user_id": user_id}
    if event == "register":
        telegram_id = _positive_id(payload.get("telegram_id"))
        if telegram_id is None or payload.get("registered_via") == "panel_sync":
            return None
        data["telegram_id"] = telegram_id
        return f"register:{user_id}:{telegram_id}", data

    end_date = _event_date(payload.get("end_date"))
    if end_date is None:
        return None
    data["end_date"] = end_date.isoformat()
    if event == "trial":
        return f"trial:{user_id}:{end_date.isoformat()}", data
    if event == "expired":
        subscription_id = _positive_id(payload.get("subscription_id"))
        if subscription_id is not None:
            data["subscription_id"] = subscription_id
            return f"expired:{subscription_id}:{end_date.isoformat()}", data
    return None


def _disabled_reason(settings: Settings, event: EventName) -> str | None:
    if not settings.events:
        return "notifications_disabled"
    if event not in settings.events:
        return "event_disabled"
    if settings.chat_id is None:
        return "chat_required"
    if not settings.sources:
        return "no_sources"
    return None


async def _queue_alert(
    session: AsyncSession,
    trace: DiagnosticTrace,
    job: str,
    key: str,
    payload: dict[str, JsonValue],
    *,
    user_id: int | None = None,
) -> str:
    already_queued = False
    try:
        async with session.begin_nested():
            if await lock_enabled_debug(session):
                already_queued = bool(
                    await session.scalar(
                        select(
                            exists().where(
                                ExtensionOperation.owner == PLUGIN_ID,
                                ExtensionOperation.kind == job,
                                ExtensionOperation.idempotency_key == key,
                            )
                        )
                    )
                )
    except SQLAlchemyError:
        logger.warning("Could not inspect blacklist diagnostics settings for queued alert")
    operation = await enqueue(
        session, job=f"{PLUGIN_ID}:{job}", idempotency_key=key, payload=payload, user_id=user_id
    )
    queued = replace(trace, operation_id=operation.id)
    if already_queued:
        await queued.write(session, "skipped", "duplicate")
    else:
        await queued.write(session, "queued")
    return operation.id


async def queue_test_notification(
    session: AsyncSession,
    chat_id: int,
    topic_id: int | None,
) -> str:
    return await _queue_alert(
        session,
        DiagnosticTrace("test"),
        _TEST_JOB,
        uuid4().hex,
        {"chat_id": chat_id, "topic_id": topic_id},
    )


def subscribe_to_events(ctx: PluginContext) -> None:
    session_factory = ctx.require_session_factory()

    async def receive(event_name: str, payload: dict[str, object]) -> None:
        event = _BUS_EVENTS[event_name]
        if not generation_is_current():
            return
        trace = DiagnosticTrace(
            event,
            user_id=_account_id(payload.get("user_id")),
            telegram_id=_positive_id(payload.get("telegram_id")),
        )
        try:
            async with session_factory.begin() as session:
                settings = await get_settings(session)
                if reason := _disabled_reason(settings, event):
                    await trace.write(session, "skipped", reason)
                    return
                candidate = event_job(event, payload)
                if candidate is None:
                    if event == "register" and payload.get("registered_via") == "panel_sync":
                        reason = "panel_sync"
                    elif event == "register" and trace.user_id and trace.telegram_id is None:
                        reason = "telegram_unlinked"
                    else:
                        reason = "invalid_event"
                    await trace.write(session, "skipped", reason)
                    return
                key, data = candidate
                # Registration can precede commit. Store the ID in the payload to
                # avoid a foreign-key check against the publishing transaction.
                await _queue_alert(session, trace, _ALERT_JOB, key, data)
        except (SQLAlchemyError, ExtensionError):
            logger.warning("Could not queue blacklist alert for event %s", event_name)
            await trace.persist(session_factory, "error", "queue_failed")

    for event_name in _BUS_EVENTS:
        events.subscribe(event_name, receive)

    async def balance_payment(_name: str, payload: dict[str, object]) -> None:
        # In 3.8.5, adding funds to the balance skips the transactional payment
        # subscription. The public event is emitted after the credit commits.
        sale_mode = payload.get("sale_mode")
        payment_id = _positive_id(payload.get("payment_db_id"))
        if (
            payment_id is None
            or not isinstance(sale_mode, str)
            or sale_mode.split("@", 1)[0].split("|", 1)[0] != "balance_topup"
            or not generation_is_current()
        ):
            return
        trace = DiagnosticTrace("payment", user_id=_account_id(payload.get("user_id")))
        try:
            async with session_factory.begin() as session:
                settings = await get_settings(session)
                if reason := _disabled_reason(settings, "payment"):
                    await trace.write(session, "skipped", reason)
                    return
                payment = await session.get(Payment, payment_id)
                if payment is None or payment.status != "succeeded":
                    await trace.write(session, "skipped", "invalid_event")
                    return
                trace = replace(trace, user_id=int(payment.user_id))
                if payment.amount <= 0:
                    await trace.write(session, "skipped", "non_positive_payment")
                    return
                await _queue_alert(
                    session,
                    trace,
                    _PAYMENT_JOB,
                    f"balance-topup:{payment_id}",
                    {"payload": {"payment_db_id": payment_id, "amount": payment.amount}},
                    user_id=int(payment.user_id),
                )
        except (SQLAlchemyError, ExtensionError):
            logger.warning("Could not queue blacklist alert for a balance top-up")
            await trace.persist(session_factory, "error", "queue_failed")

    events.subscribe(events.PAYMENT_SUCCEEDED, balance_payment)


async def _action_committed(
    session: AsyncSession, event: EventName, user_id: int, payload: dict[str, JsonValue]
) -> bool:
    if event in ("register", "payment"):
        # At this point the registered account, or the payment's transactional job,
        # has already been found by the worker.
        return True
    end_date = _event_date(payload.get("end_date"))
    if end_date is None:
        return False
    if event == "trial":
        query = select(
            exists().where(
                Subscription.user_id == user_id,
                Subscription.end_date == end_date,
                or_(Subscription.status_from_panel == "TRIAL", Subscription.provider == "trial"),
            )
        )
    else:
        query = select(
            exists().where(
                SubscriptionLifecycleNotification.subscription_id == Subscription.subscription_id,
                Subscription.subscription_id == payload.get("subscription_id"),
                Subscription.user_id == user_id,
                SubscriptionLifecycleNotification.notification_key == "expired:event",
                SubscriptionLifecycleNotification.period_end_date == end_date,
            )
        )
    return bool(await session.scalar(query))


async def _deliver(
    operation: OperationContext, event: EventName, payload: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    user_id = _account_id(operation.user_id) or _account_id(payload.get("user_id"))
    trace = DiagnosticTrace(event, operation.operation_id, user_id)
    ctx = operation.runtime
    session_factory = ctx.require_session_factory()
    if user_id is None:
        await trace.persist(session_factory, "skipped", "invalid_event")
        return {"sent": False}
    try:
        async with session_factory.begin() as session:
            settings = await get_settings(session)
            reason = _disabled_reason(settings, event)
            if reason is not None or settings.chat_id is None:
                await trace.write(session, "skipped", reason or "chat_required")
                return {"sent": False}
            user = await session.get(User, user_id)
            if user is None or not await _action_committed(session, event, user_id, payload):
                raise ExtensionError("blacklist_event_not_committed", 502)
            telegram_id = _positive_id(user.telegram_id)
            if telegram_id is None:
                await trace.write(session, "skipped", "telegram_unlinked")
                return {"sent": False}
            trace = replace(trace, telegram_id=telegram_id)
            if event == "register" and telegram_id != payload.get("telegram_id"):
                await trace.write(session, "skipped", "telegram_changed")
                return {"sent": False}

            match = (await get_matches(session, [telegram_id]))[telegram_id]
            if match.state == "unknown":
                raise ExtensionError("blacklist_sources_pending", 502)
            if match.state != "match":
                await trace.write(session, "skipped", "no_match")
                return {"sent": False}
            text, keyboard = render_alert(ctx, event, user, telegram_id, match)
            message = TelegramMessage(settings.chat_id, settings.topic_id, text, keyboard)
    except ExtensionError as exc:
        await record_delivery_failure(operation, trace, exc.code.removeprefix("blacklist_"))
        raise

    return await send_telegram(operation, trace, message)


async def _send_event(
    operation: OperationContext, payload: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    event = payload.get("event")
    if event == "register":
        return await _deliver(operation, "register", payload)
    if event == "trial":
        return await _deliver(operation, "trial", payload)
    if event == "expired":
        return await _deliver(operation, "expired", payload)
    return {"sent": False}


async def _send_payment(
    operation: OperationContext, payload: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    payment = payload.get("payload")
    trace = DiagnosticTrace("payment", operation.operation_id, _account_id(operation.user_id))
    session_factory = operation.runtime.require_session_factory()
    if not isinstance(payment, dict) or _positive_id(payment.get("payment_db_id")) is None:
        await trace.persist(session_factory, "skipped", "invalid_event")
        return {"sent": False}
    amount = payment.get("amount")
    if type(amount) not in (int, float) or not amount > 0:
        await trace.persist(session_factory, "skipped", "non_positive_payment")
        return {"sent": False}
    return await _deliver(operation, "payment", payment)


async def _send_test(
    operation: OperationContext,
    payload: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    ctx = operation.runtime
    trace = DiagnosticTrace("test", operation.operation_id)
    async with ctx.require_session_factory().begin() as session:
        if not await debug_enabled(session):
            return {"sent": False}
        settings = await get_settings(session)
        if settings.chat_id is None:
            await trace.write(session, "skipped", "chat_required")
            return {"sent": False}
        if settings.chat_id != payload.get("chat_id") or settings.topic_id != payload.get(
            "topic_id"
        ):
            await trace.write(session, "skipped", "destination_changed")
            return {"sent": False}
        language = (ctx.settings.DEFAULT_LANGUAGE or "ru").lower().split("-", 1)[0]
        text = (
            "<b>Чёрный список Minishop</b>\nТестовое уведомление"
            if language == "ru"
            else "<b>Minishop blacklist</b>\nTest notification"
        )
        message = TelegramMessage(settings.chat_id, settings.topic_id, text)
    return await send_telegram(operation, trace, message)


def alert_contributions() -> ExtensionContributions:
    return ExtensionContributions(
        jobs=(
            JobHandler(_ALERT_JOB, _send_event),
            JobHandler(_PAYMENT_JOB, _send_payment),
            JobHandler(_TEST_JOB, _send_test, max_attempts=1),
        ),
        events=(DurableSubscription(events.PAYMENT_SUCCEEDED, _PAYMENT_JOB),),
    )
