"""Use database leases so concurrent workers do not replace the same snapshot."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .sources import SourceError, fetch_source
from .storage import RefreshLease, claim_due_sources, record_refresh

_CONCURRENT_REFRESHES = 4


async def refresh_sources(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory.begin() as session:
        leases = await claim_due_sources(session, datetime.now(UTC))

    semaphore = asyncio.Semaphore(_CONCURRENT_REFRESHES)

    async def refresh(lease: RefreshLease) -> bool:
        async with semaphore:
            try:
                snapshot = await fetch_source(lease.url)
            except SourceError as exc:
                async with session_factory.begin() as session:
                    return await record_refresh(session, lease, datetime.now(UTC), error=exc.code)
            async with session_factory.begin() as session:
                return await record_refresh(session, lease, datetime.now(UTC), snapshot=snapshot)

    async with asyncio.TaskGroup() as group:
        attempts = [group.create_task(refresh(lease)) for lease in leases]
    return sum(attempt.result() for attempt in attempts)
