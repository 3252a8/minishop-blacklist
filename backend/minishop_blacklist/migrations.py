"""The plugin's migration chain for Minishop's PostgreSQL database."""

from db.migrator.engine import Migration
from sqlalchemy import text
from sqlalchemy.engine import Connection

_INITIAL_DDL = (
    """CREATE TABLE ext_minishop_blacklist_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        refresh_hours INTEGER NOT NULL DEFAULT 24 CHECK (refresh_hours BETWEEN 1 AND 8760),
        users_visible BOOLEAN NOT NULL DEFAULT TRUE,
        user_card_visible BOOLEAN NOT NULL DEFAULT TRUE,
        ticket_visible BOOLEAN NOT NULL DEFAULT TRUE,
        chat_id BIGINT CHECK (chat_id <> 0),
        topic_id INTEGER CHECK (topic_id IS NULL OR (topic_id > 0 AND chat_id IS NOT NULL)),
        events JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(events) = 'array')
    )""",
    """CREATE TABLE ext_minishop_blacklist_sources (
        id UUID PRIMARY KEY,
        url VARCHAR(2048) NOT NULL UNIQUE,
        position INTEGER NOT NULL CHECK (position >= 0),
        last_attempt_at TIMESTAMPTZ,
        last_success_at TIMESTAMPTZ,
        last_error VARCHAR(48),
        count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0),
        invalid_lines INTEGER NOT NULL DEFAULT 0 CHECK (invalid_lines >= 0),
        lease_token UUID,
        lease_until TIMESTAMPTZ,
        CHECK ((lease_token IS NULL) = (lease_until IS NULL))
    )""",
    """CREATE TABLE ext_minishop_blacklist_entries (
        source_id UUID NOT NULL REFERENCES ext_minishop_blacklist_sources(id) ON DELETE CASCADE,
        telegram_id BIGINT NOT NULL CHECK (telegram_id > 0),
        reasons JSONB NOT NULL CHECK (jsonb_typeof(reasons) = 'array'),
        PRIMARY KEY (source_id, telegram_id)
    )""",
    "CREATE INDEX ext_minishop_blacklist_entries_telegram_id "
    "ON ext_minishop_blacklist_entries (telegram_id)",
    "INSERT INTO ext_minishop_blacklist_settings (id) VALUES (1)",
    """INSERT INTO ext_minishop_blacklist_sources (id, url, position) VALUES
        ('0f32fd58-84ab-4f63-8db6-0e2a3b964198',
         'https://raw.githubusercontent.com/Blin4ickUSE/ban-vpn/refs/heads/main/blacklist.txt', 0),
        ('300edab1-eb7f-4f20-82bd-35aba9f62c1a',
         'https://raw.githubusercontent.com/BEDOLAGA-DEV/VPN-BLACKLIST/refs/heads/main/blacklist.txt',
         1)
    """,
)


def _initial_schema(connection: Connection) -> None:
    for statement in _INITIAL_DDL:
        connection.execute(text(statement))


def migrations() -> list[Migration]:
    return [
        Migration(
            id="minishop-blacklist.0001_initial",
            description="Blacklist settings, source snapshots and Telegram ID index",
            upgrade=_initial_schema,
        )
    ]
