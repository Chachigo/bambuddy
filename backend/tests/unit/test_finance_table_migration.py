"""Regression tests for finance tables on upgraded databases."""

import os
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import (
    _migrate_add_print_archive_cost_center,
    _migrate_create_finance_indexes,
    _migrate_create_finance_tables,
    _migrate_finance_money_to_numeric,
)

EXPECTED_TABLES = {
    "cost_centers",
    "wallet_transactions",
    "budget_reservations",
    "cost_center_members",
    "user_wallets",
}


@pytest.mark.asyncio
async def test_finance_tables_are_created_idempotently_on_sqlite():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        async with engine.begin() as conn:
            with patch("backend.app.core.database.is_sqlite", return_value=True):
                await _migrate_create_finance_tables(conn)
                await _migrate_create_finance_tables(conn)

            rows = await conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name IN "
                    "('cost_centers', 'wallet_transactions', 'budget_reservations', "
                    "'cost_center_members', 'user_wallets')"
                )
            )
            invitation_table = await conn.scalar(
                text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cost_center_invitations'")
            )
            wallet_columns = await conn.execute(text("PRAGMA table_info(user_wallets)"))
            transaction_columns = await conn.execute(text("PRAGMA table_info(wallet_transactions)"))

        assert {row[0] for row in rows} == EXPECTED_TABLES
        assert invitation_table is None
        assert {row[1]: row[2] for row in wallet_columns}["balance"] == "NUMERIC(14,2)"
        transaction_types = {row[1]: row[2] for row in transaction_columns}
        assert transaction_types["amount"] == "NUMERIC(14,2)"
        assert transaction_types["balance_after"] == "NUMERIC(14,2)"
        assert transaction_types["is_voided"] == "BOOLEAN"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_cost_center_indexes_are_delayed_until_columns_exist():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE cost_centers (id INTEGER PRIMARY KEY, name VARCHAR(150) NOT NULL)"))

            with patch("backend.app.core.database.is_sqlite", return_value=True):
                await _migrate_create_finance_tables(conn)

            await conn.execute(text("ALTER TABLE cost_centers ADD COLUMN code VARCHAR(32)"))
            await conn.execute(text("CREATE INDEX ix_cost_centers_code ON cost_centers (code)"))
            await conn.execute(text("INSERT INTO cost_centers (id, name, code) VALUES (1, 'One', 'one')"))
            await _migrate_create_finance_indexes(conn)

            result = await conn.execute(text("PRAGMA index_list(cost_centers)"))
            code_index = next(row for row in result if row[1] == "ix_cost_centers_code")

            with pytest.raises(IntegrityError):
                await conn.execute(text("INSERT INTO cost_centers (id, name, code) VALUES (2, 'Two', 'one')"))

        assert code_index[2] == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_print_archive_cost_center_is_added_idempotently_on_sqlite():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys = ON"))
            await conn.execute(text("CREATE TABLE cost_centers (id INTEGER PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE print_archives (id INTEGER PRIMARY KEY)"))

            await _migrate_add_print_archive_cost_center(conn)
            await _migrate_add_print_archive_cost_center(conn)

            columns = await conn.execute(text("PRAGMA table_info(print_archives)"))
            foreign_keys = await conn.execute(text("PRAGMA foreign_key_list(print_archives)"))

        assert "cost_center_id" in {row[1] for row in columns}
        assert any(
            row[2] == "cost_centers" and row[3] == "cost_center_id" and row[6].upper() == "SET NULL"
            for row in foreign_keys
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_finance_ddl_uses_postgres_types():
    statements: list[str] = []

    async def capture_statement(_conn, sql: str) -> None:
        statements.append(sql)

    with (
        patch("backend.app.core.database.is_sqlite", return_value=False),
        patch("backend.app.core.database._safe_execute", side_effect=capture_statement),
    ):
        await _migrate_create_finance_tables(object())

    create_statements = [sql for sql in statements if "CREATE TABLE" in sql]
    assert len(create_statements) == len(EXPECTED_TABLES)
    assert all("IF NOT EXISTS" in sql for sql in create_statements)
    assert all("DATETIME" not in sql for sql in create_statements)
    assert all("id SERIAL PRIMARY KEY" in sql for sql in create_statements)
    assert "TIMESTAMP" in "\n".join(create_statements)
    assert "NUMERIC(14,2)" in "\n".join(create_statements)
    assert "is_voided BOOLEAN NOT NULL DEFAULT FALSE" in "\n".join(create_statements)

    created_tables = {
        sql.split("CREATE TABLE IF NOT EXISTS", 1)[1].split("(", 1)[0].strip() for sql in create_statements
    }
    assert created_tables == EXPECTED_TABLES


@pytest.mark.asyncio
async def test_postgres_finance_money_columns_are_migrated_to_numeric():
    statements: list[str] = []

    async def capture_statement(_conn, sql: str) -> None:
        statements.append(sql)

    with (
        patch("backend.app.core.database.is_sqlite", return_value=False),
        patch("backend.app.core.database._safe_execute", side_effect=capture_statement),
    ):
        await _migrate_finance_money_to_numeric(object())

    assert len(statements) == 6
    assert all("TYPE NUMERIC(14,2)" in sql for sql in statements)
    assert all("USING ROUND(" in sql for sql in statements)
    assert any("wallet_transactions ALTER COLUMN amount" in sql for sql in statements)
    assert any("wallet_transactions ALTER COLUMN balance_after" in sql for sql in statements)


@pytest.mark.asyncio
async def test_finance_tables_are_created_idempotently_on_postgres():
    database_url = os.getenv("BAMBUDDY_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("BAMBUDDY_TEST_POSTGRES_URL is not configured")

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            # Minimal pre-billing schema: these are the only tables referenced
            # by foreign keys in the new finance tables.
            await conn.execute(text("CREATE TABLE users (id SERIAL PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE print_archives (id SERIAL PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE print_queue (id SERIAL PRIMARY KEY)"))

            with patch("backend.app.core.database.is_sqlite", return_value=False):
                await _migrate_create_finance_tables(conn)
                await _migrate_create_finance_tables(conn)
                await conn.execute(
                    text(
                        "ALTER TABLE wallet_transactions ALTER COLUMN amount "
                        "TYPE DOUBLE PRECISION USING amount::double precision"
                    )
                )
                await _migrate_finance_money_to_numeric(conn)
                await _migrate_finance_money_to_numeric(conn)
                await _migrate_add_print_archive_cost_center(conn)
                await _migrate_add_print_archive_cost_center(conn)
                await _migrate_create_finance_indexes(conn)
                await _migrate_create_finance_indexes(conn)

            rows = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(:tables)"
                ),
                {"tables": sorted(EXPECTED_TABLES)},
            )
            timestamp_type = await conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'cost_centers' AND column_name = 'created_at'"
                )
            )
            money_types = await conn.execute(
                text(
                    "SELECT table_name, column_name, data_type, numeric_precision, numeric_scale "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND (table_name, column_name) IN ("
                    "('cost_centers', 'total_budget'), ('cost_centers', 'monthly_budget'), "
                    "('user_wallets', 'balance'), ('wallet_transactions', 'amount'), "
                    "('wallet_transactions', 'balance_after'), ('budget_reservations', 'amount'))"
                )
            )
            money_type_rows = money_types.all()
            archive_cost_center = await conn.execute(
                text(
                    "SELECT c.data_type, rc.delete_rule "
                    "FROM information_schema.columns c "
                    "JOIN information_schema.key_column_usage kcu "
                    "  ON kcu.table_schema = c.table_schema "
                    " AND kcu.table_name = c.table_name "
                    " AND kcu.column_name = c.column_name "
                    "JOIN information_schema.referential_constraints rc "
                    "  ON rc.constraint_schema = kcu.constraint_schema "
                    " AND rc.constraint_name = kcu.constraint_name "
                    "WHERE c.table_schema = 'public' "
                    "AND c.table_name = 'print_archives' "
                    "AND c.column_name = 'cost_center_id'"
                )
            )

        assert {row[0] for row in rows} == EXPECTED_TABLES
        assert timestamp_type.scalar_one() == "timestamp without time zone"
        assert len(money_type_rows) == 6
        assert all(row[2:] == ("numeric", 14, 2) for row in money_type_rows)
        assert archive_cost_center.one() == ("integer", "SET NULL")
    finally:
        await engine.dispose()
