"""Migration coverage for durable per-dispatch billing identities."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
import backend.app.models.external_link  # noqa: F401 - required by a legacy ALTER in run_migrations
import backend.app.models.print_log  # noqa: F401 - required by a legacy ALTER in run_migrations
from backend.app.core.database import Base, run_migrations


@pytest.mark.asyncio
async def test_billing_run_columns_and_legacy_archive_index_are_migrated(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'billing-run.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await run_migrations(conn)

            queue_columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(print_queue)"))).all()}
            archive_columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(print_archives)"))).all()}
            notification_columns = {
                row[1] for row in (await conn.execute(text("PRAGMA table_info(notification_providers)"))).all()
            }
            archive_index_sql = await conn.scalar(
                text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'uq_wallet_transactions_archive'")
            )

        assert "billing_run_id" in queue_columns
        assert "billing_run_id" in archive_columns
        assert "on_billing_charge_failed" in notification_columns
        assert archive_index_sql is not None
        assert "WHERE print_run_id IS NULL" in archive_index_sql
    finally:
        await engine.dispose()
