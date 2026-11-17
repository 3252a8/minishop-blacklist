import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from db.migrator.engine import run_migration_chains
from db.models import User
from minishop_blacklist.migrations import migrations
from minishop_blacklist.settings import DEFAULT_SETTINGS
from minishop_blacklist.sources import Reason, parse_source
from minishop_blacklist.storage import (
    Match,
    claim_due_sources,
    get_matches,
    get_settings,
    list_matched_users,
    list_source_statuses,
    record_refresh,
    save_settings,
)
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_source_leases_snapshots_and_pagination() -> None:
    database = os.environ.get("MINISHOP_BLACKLIST_TEST_DATABASE")
    if not database:
        pytest.skip("MINISHOP_BLACKLIST_TEST_DATABASE is not set")

    schema = f"blacklist_test_{uuid4().hex}"
    admin = create_async_engine(database)
    engine = create_async_engine(
        database, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
        async with engine.begin() as connection:
            await connection.run_sync(User.__table__.create)
            await connection.run_sync(run_migration_chains, {"minishop-blacklist": migrations()})
            await connection.execute(
                insert(User.__table__),
                [
                    {"user_id": 1, "telegram_id": 1001},
                    {"user_id": 2, "telegram_id": 1002},
                    {"user_id": 3, "telegram_id": None},
                ],
            )

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        async with sessions.begin() as session:
            assert await get_settings(session) == DEFAULT_SETTINGS
            assert await get_matches(session, [1001]) == {1001: Match("unknown")}
            first, second = await claim_due_sources(session, now)
            async with sessions.begin() as competing:
                assert await claim_due_sources(competing, now) == []

        async with sessions.begin() as session:
            assert await claim_due_sources(session, now) == []
            assert await record_refresh(
                session,
                first,
                now,
                snapshot=parse_source(b"1001 # first\n1002 # other\n500 # no user"),
            )
            assert await record_refresh(
                session, second, now, snapshot=parse_source(b"1001 # first\n1001 # second")
            )
            assert await get_matches(session, [1001, 1003]) == {
                1001: Match("match", (Reason("first"), Reason("second"))),
                1003: Match("clear"),
            }
            rows, count = await list_matched_users(session, limit=1, offset=0)
            assert [user.user_id for user, _ in rows] == [2]
            assert count == 2
            rows, count = await list_matched_users(session, limit=1, offset=1)
            assert [user.user_id for user, _ in rows] == [1]
            assert rows[0][1].reasons == (Reason("first"), Reason("second"))
            assert count == 2
            assert await list_matched_users(session, limit=1, offset=2) == ([], 2)

        async with sessions.begin() as session:
            one_hour_later = now + timedelta(hours=1)
            assert await claim_due_sources(session, one_hour_later) == []
            await save_settings(session, replace(DEFAULT_SETTINGS, refresh_hours=1))
            next_first, next_second = await claim_due_sources(session, one_hour_later)
            assert not await record_refresh(session, first, one_hour_later, error="source_timeout")
            assert await record_refresh(session, next_first, one_hour_later, error="source_timeout")
            assert await record_refresh(
                session, next_second, one_hour_later, snapshot=parse_source(b"")
            )
            statuses = await list_source_statuses(session)
            assert statuses[0].count == 3
            assert statuses[0].last_success_at == now
            assert statuses[0].last_attempt_at == one_hour_later
            assert statuses[0].last_error == "source_timeout"
            assert statuses[1].count == 0
            assert await get_matches(session, [1001, 1002, 1003]) == {
                1001: Match("match", (Reason("first"),), stale=True),
                1002: Match("match", (Reason("other"),), stale=True),
                1003: Match("unknown"),
            }

        async with sessions.begin() as session:
            await save_settings(session, replace(DEFAULT_SETTINGS, sources=(second.url,)))
            assert await get_matches(session, [1001]) == {1001: Match("clear")}
            assert await list_matched_users(session, limit=1, offset=0) == ([], 0)
            await save_settings(session, replace(DEFAULT_SETTINGS, sources=()))
            assert await get_matches(session, [1001]) == {1001: Match("unknown")}
            await save_settings(session, DEFAULT_SETTINGS)
            new_first, new_second = await claim_due_sources(session, now)
            assert (first.url, second.url) == (new_first.url, new_second.url)
            assert (first.source_id, second.source_id) != (
                new_first.source_id,
                new_second.source_id,
            )
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await admin.dispose()
