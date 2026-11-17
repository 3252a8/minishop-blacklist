"""Run the stock Minishop admin middleware, event bus and job runner against PostgreSQL."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.app.web.admin_api_impl.auth import admin_auth_middleware
from bot.app.web.context import SESSION_FACTORY, SETTINGS
from bot.app.web.webapp.assets import _csrf_protection_middleware
from bot.app.web.webapp_auth import create_webapp_session_token
from bot.infra import events
from bot.plugins.extensions.jobs import run_once
from bot.plugins.loader import register, reset_plugins, run_setup
from bot.plugins.spec import WEB_SCOPE_WEBAPP, PluginContext
from bot.services.account_roles import ROLE_ADMIN, grant_role
from config.settings import Settings as HostSettings
from db.base import Base
from db.dal import payment_dal, subscription_dal
from db.migrator import run_migration_chains
from db.models import Subscription, User
from minishop_blacklist import plugin
from minishop_blacklist.sources import parse_source
from minishop_blacklist.storage import claim_due_sources, record_refresh
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_BASE = "/api/admin/minishop-blacklist"
_CUSTOMER_ID = -301
_TELEGRAM_ID = 998877661


class Telegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_message(self, **message: object) -> SimpleNamespace:
        self.sent.append(message)
        return SimpleNamespace(message_id=len(self.sent))


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch) -> AsyncIterator[tuple[PluginContext, Telegram]]:
    url = os.environ.get("MINISHOP_BLACKLIST_RUNTIME_TEST_DATABASE")
    if not url:
        pytest.skip("Set MINISHOP_BLACKLIST_RUNTIME_TEST_DATABASE to a PostgreSQL test database")
    monkeypatch.setenv("MINISHOP_PLUGIN_STORE", str(tmp_path / "plugin-store"))
    monkeypatch.delenv("MINISHOP_PLUGIN_GENERATION", raising=False)
    engine = create_async_engine(url)
    schema = f"blacklist_runtime_{uuid4().hex}"
    isolated = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    session_factory = async_sessionmaker(isolated, expire_on_commit=False)
    settings = HostSettings(
        _env_file=None,
        POSTGRES_USER="placeholder",
        POSTGRES_PASSWORD="placeholder",
        DEFAULT_LANGUAGE="ru",
        WEBAPP_SESSION_SECRET="local-regression-testing-only",
        SUBSCRIPTION_MINI_APP_URL="https://admin.example/shop/",
        TELEGRAM_ENABLED=False,
        PLUGINS_ENABLED=True,
        PLUGINS_STRICT=True,
    )
    monkeypatch.setattr("config.settings._settings_instance", settings)
    bot = Telegram()
    ctx = PluginContext(settings=settings, session_factory=session_factory, bot=bot)
    register(plugin)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        async with isolated.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(run_migration_chains, {plugin.name: plugin.migrations()})
        run_setup(ctx)
        async with session_factory.begin() as session:
            session.add_all(
                [
                    User(user_id=200, telegram_id=2001),
                    User(user_id=_CUSTOMER_ID, telegram_id=_TELEGRAM_ID, first_name="<Tester>"),
                ]
            )
            await session.flush()
            await grant_role(session, 200, ROLE_ADMIN)
        yield ctx, bot
    finally:
        events.reset_subscribers()
        reset_plugins()
        await isolated.dispose()
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await engine.dispose()


def _auth(settings: HostSettings, user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_webapp_session_token(settings, user_id)}"}


async def _client(ctx: PluginContext) -> TestClient:
    app = web.Application(middlewares=[_csrf_protection_middleware, admin_auth_middleware])
    app[SETTINGS] = ctx.settings
    app[SESSION_FACTORY] = ctx.require_session_factory()
    plugin.setup_web(ctx, app, scope=WEB_SCOPE_WEBAPP)
    return TestClient(TestServer(app))


async def _configure(ctx: PluginContext, client: TestClient) -> dict[str, object]:
    headers = _auth(ctx.settings, 200)
    response = await client.get(f"{_BASE}/settings", headers=headers)
    body = await response.json()
    assert response.status == 200 and body["settings"]["refreshHours"] == 24
    assert len(body["settings"]["sources"]) == 2
    settings = body["settings"]
    settings.update(
        refreshHours=48,
        chatId="-1001234567890",
        topicId="5",
        events=["register", "trial", "payment", "expired"],
    )
    response = await client.put(f"{_BASE}/settings", headers=headers, json=settings)
    assert response.status == 200
    assert (await response.json())["settings"]["refreshHours"] == 48

    now = datetime.now(UTC)
    async with ctx.require_session_factory().begin() as session:
        first, second = await claim_due_sources(session, now)
        await record_refresh(
            session,
            first,
            now,
            snapshot=parse_source(
                f"{_TELEGRAM_ID} # Причина А https://example.org/one\n"
                f"{_TELEGRAM_ID} # Причина Б\n".encode()
            ),
        )
        await record_refresh(
            session,
            second,
            now,
            snapshot=parse_source(
                f"{_TELEGRAM_ID} # Причина А https://example.org/two\n"
                f"{_TELEGRAM_ID} # <причина В>\n".encode()
            ),
        )
    return settings


@pytest.mark.asyncio
async def test_settings_and_statuses_require_admin_and_current_visibility(runtime) -> None:
    ctx, _bot = runtime
    async with await _client(ctx) as client:
        assert (await client.get(f"{_BASE}/settings")).status == 401
        assert (
            await client.get(f"{_BASE}/settings", headers=_auth(ctx.settings, _CUSTOMER_ID))
        ).status == 403

        settings = await _configure(ctx, client)
        headers = _auth(ctx.settings, 200)
        response = await client.post(
            f"{_BASE}/status",
            headers=headers,
            json={"view": "ticket", "telegramIds": [str(_TELEGRAM_ID), "123"]},
        )
        status = (await response.json())["statuses"]
        assert status["123"]["state"] == "clear"
        found = status[str(_TELEGRAM_ID)]
        assert found["reasons"] == [
            {"text": "Причина А", "links": ["https://example.org/one", "https://example.org/two"]},
            {"text": "Причина Б", "links": []},
            {"text": "<причина В>", "links": []},
        ]
        matches = await client.get(f"{_BASE}/matches", headers=headers)
        matched_page = await matches.json()
        assert matched_page["total"] == 1
        assert matched_page["rows"][0]["telegramId"] == str(_TELEGRAM_ID)
        assert matched_page["rows"][0]["userId"].startswith("ms_")

        cookie = "rw_webapp_session=" + create_webapp_session_token(ctx.settings, 200)
        settings["display"]["ticket"] = False
        response = await client.put(f"{_BASE}/settings", headers={"Cookie": cookie}, json=settings)
        assert response.status == 403
        response = await client.put(f"{_BASE}/settings", headers=headers, json=settings)
        assert response.status == 200
        response = await client.post(
            f"{_BASE}/status",
            headers=headers,
            json={"view": "ticket", "telegramIds": [str(_TELEGRAM_ID)]},
        )
        assert await response.json() == {"ok": True, "enabled": False, "statuses": {}}


@pytest.mark.asyncio
async def test_four_event_categories_use_target_topic_and_ignore_duplicates(runtime) -> None:
    ctx, bot = runtime
    async with await _client(ctx) as client:
        await _configure(ctx, client)
    now = datetime.now(UTC)
    async with ctx.require_session_factory().begin() as session:
        active_trial = Subscription(
            user_id=_CUSTOMER_ID,
            panel_user_uuid="trial",
            end_date=now + timedelta(days=1),
            status_from_panel="TRIAL",
        )
        expired = Subscription(user_id=_CUSTOMER_ID, panel_user_uuid="expired", end_date=now)
        session.add_all([active_trial, expired])
        await session.flush()
        await subscription_dal.record_subscription_notification(
            session, expired.subscription_id, "expired:event"
        )
        for sale_mode in ("subscription", "balance_topup"):
            payment = await payment_dal.create_payment_record(
                session,
                {
                    "user_id": _CUSTOMER_ID,
                    "amount": 10.0,
                    "currency": "RUB",
                    "status": "pending",
                    "provider": "test",
                    "sale_mode": sale_mode,
                },
            )
            await payment_dal.update_payment_status_by_db_id(
                session, payment.payment_id, "succeeded"
            )
            if sale_mode == "balance_topup":
                topup_payment_id = payment.payment_id

    registration = {"user_id": _CUSTOMER_ID, "telegram_id": _TELEGRAM_ID}
    trial = {"user_id": _CUSTOMER_ID, "end_date": active_trial.end_date.isoformat()}
    expiry = {
        "user_id": _CUSTOMER_ID,
        "subscription_id": expired.subscription_id,
        "end_date": expired.end_date.isoformat(),
    }
    topup = {"sale_mode": "balance_topup", "payment_db_id": topup_payment_id}
    for event, payload in (
        (events.USER_REGISTERED, registration),
        (events.TRIAL_ACTIVATED, trial),
        (events.SUBSCRIPTION_EXPIRED, expiry),
        (events.PAYMENT_SUCCEEDED, topup),
    ):
        await events.emit(event, payload)
        await events.emit(event, payload)
    while await run_once(ctx):
        pass

    assert len(bot.sent) == 5
    texts = [message["text"] for message in bot.sent]
    for title in ("Зарегистрировался", "Взял триал", "Истёк срок подписки"):
        assert sum(f"<b>{title}</b>" in text for text in texts) == 1
    assert sum("<b>Оплатил</b>" in text for text in texts) == 2
    for message in bot.sent:
        assert message["chat_id"] == -1001234567890
        assert message["message_thread_id"] == 5
        assert "&lt;Tester&gt;" in message["text"] and "&lt;причина В&gt;" in message["text"]
