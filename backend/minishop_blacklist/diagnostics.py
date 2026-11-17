"""Bounded, administrator-only traces for Telegram notifications."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import JsonValue
from sqlalchemy import BigInteger, DateTime, String, delete, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from .settings import EventName
from .storage_models import Base

MAX_ENTRIES = 300
type DiagnosticEvent = EventName | Literal["test"]
type DiagnosticStatus = Literal["queued", "skipped", "sending", "sent", "retry", "error"]
logger = logging.getLogger(__name__)


class DiagnosticSettings(Base):
    __tablename__ = "ext_minishop_blacklist_diagnostics_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool]


class DiagnosticEntry(Base):
    __tablename__ = "ext_minishop_blacklist_diagnostics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event: Mapped[DiagnosticEvent] = mapped_column(String(12))
    status: Mapped[DiagnosticStatus] = mapped_column(String(12))
    reason: Mapped[str | None] = mapped_column(String(64))
    operation_id: Mapped[str | None] = mapped_column(String(32))
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    request: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    response: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))


async def debug_enabled(session: AsyncSession) -> bool:
    return bool(
        await session.scalar(select(DiagnosticSettings.enabled).where(DiagnosticSettings.id == 1))
    )


async def lock_enabled_debug(session: AsyncSession) -> bool:
    # Serialize writers while enabled, with no row lock in normal operation.
    return bool(
        await session.scalar(
            select(DiagnosticSettings.enabled)
            .where(DiagnosticSettings.id == 1, DiagnosticSettings.enabled.is_(True))
            .with_for_update()
        )
    )


async def set_debug_enabled(session: AsyncSession, enabled: bool) -> None:
    settings = await session.get(DiagnosticSettings, 1, with_for_update=True)
    if settings is None:
        raise RuntimeError("blacklist diagnostics migration is missing")
    settings.enabled = enabled


async def clear_diagnostics(session: AsyncSession) -> None:
    # Serialize clearing with writers so an entry cannot escape the clear transaction.
    await session.get(DiagnosticSettings, 1, with_for_update=True)
    await session.execute(delete(DiagnosticEntry))


async def list_diagnostics(
    session: AsyncSession, *, limit: int, before_id: int | None
) -> tuple[list[DiagnosticEntry], str | None]:
    query = select(DiagnosticEntry).order_by(DiagnosticEntry.id.desc()).limit(limit + 1)
    if before_id is not None:
        query = query.where(DiagnosticEntry.id < before_id)
    entries = list(await session.scalars(query))
    return entries[:limit], str(entries[limit - 1].id) if len(entries) > limit else None


@dataclass(frozen=True)
class DiagnosticTrace:
    event: DiagnosticEvent
    operation_id: str | None = None
    user_id: int | None = None
    telegram_id: int | None = None

    async def write(
        self,
        session: AsyncSession,
        status: DiagnosticStatus,
        reason: str | None = None,
        *,
        request: dict[str, JsonValue] | None = None,
        response: dict[str, JsonValue] | None = None,
    ) -> None:
        try:
            async with session.begin_nested():
                if not await lock_enabled_debug(session):
                    return
                session.add(
                    DiagnosticEntry(
                        event=self.event,
                        status=status,
                        reason=reason,
                        operation_id=self.operation_id,
                        user_id=self.user_id,
                        telegram_id=self.telegram_id,
                        request=request,
                        response=response,
                    )
                )
                await session.flush()
                cutoff = (
                    select(DiagnosticEntry.id)
                    .order_by(DiagnosticEntry.id.desc())
                    .offset(MAX_ENTRIES - 1)
                    .limit(1)
                    .scalar_subquery()
                )
                await session.execute(delete(DiagnosticEntry).where(DiagnosticEntry.id < cutoff))
        except SQLAlchemyError:
            # A debug write must never change the delivery outcome or log credentials.
            logger.warning("Could not record the blacklist notification trace")

    async def persist(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        status: DiagnosticStatus,
        reason: str | None = None,
        *,
        request: dict[str, JsonValue] | None = None,
        response: dict[str, JsonValue] | None = None,
    ) -> None:
        try:
            async with session_factory.begin() as session:
                await self.write(session, status, reason, request=request, response=response)
        except SQLAlchemyError:
            logger.warning("Could not commit the blacklist notification trace")
