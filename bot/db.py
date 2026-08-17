from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base


def make_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url)
    if url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=DELETE")
            cursor.execute("PRAGMA secure_delete=ON")
            cursor.close()
    return engine


async def init_db(engine: AsyncEngine) -> None:
    if engine.dialect.name == "sqlite":
        await _remove_legacy_daily_poll_limit(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def _remove_legacy_daily_poll_limit(engine: AsyncEngine) -> None:
    """Upgrade databases that had UNIQUE(polls.local_date), preserving all data."""
    async with engine.connect() as connection:
        table_exists = await connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='polls'"
        )
        if table_exists.scalar_one_or_none() is None:
            return

        unique_local_date = False
        indexes = (await connection.exec_driver_sql("PRAGMA index_list('polls')")).all()
        for index in indexes:
            if not index[2]:
                continue
            index_name = str(index[1]).replace('"', '""')
            columns = (await connection.exec_driver_sql(f'PRAGMA index_info("{index_name}")')).all()
            if [column[2] for column in columns] == ["local_date"]:
                unique_local_date = True
                break
        if not unique_local_date:
            return

        await connection.commit()
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await autocommit.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        await autocommit.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            await autocommit.exec_driver_sql("DROP INDEX IF EXISTS ix_polls_local_date")
            await autocommit.exec_driver_sql("DROP INDEX IF EXISTS ix_polls_deadline_at")
            await autocommit.exec_driver_sql("ALTER TABLE polls RENAME TO polls_legacy_daily")
            await autocommit.exec_driver_sql(
                """
                CREATE TABLE polls (
                    id INTEGER NOT NULL PRIMARY KEY,
                    local_date DATE NOT NULL,
                    question VARCHAR(300) NOT NULL,
                    deadline_at DATETIME NOT NULL,
                    closed_at DATETIME,
                    aggregate_json TEXT,
                    created_at DATETIME NOT NULL
                )
                """
            )
            await autocommit.exec_driver_sql(
                """
                INSERT INTO polls (id, local_date, question, deadline_at, closed_at, aggregate_json, created_at)
                SELECT id, local_date, question, deadline_at, closed_at, aggregate_json, created_at
                FROM polls_legacy_daily
                """
            )
            await autocommit.exec_driver_sql("DROP TABLE polls_legacy_daily")
            await autocommit.exec_driver_sql("CREATE INDEX ix_polls_local_date ON polls (local_date)")
            await autocommit.exec_driver_sql("CREATE INDEX ix_polls_deadline_at ON polls (deadline_at)")
            await autocommit.exec_driver_sql("COMMIT")
        except Exception:
            await autocommit.exec_driver_sql("ROLLBACK")
            raise
        finally:
            await autocommit.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
            await autocommit.exec_driver_sql("PRAGMA foreign_keys=ON")

        broken_links = (await autocommit.exec_driver_sql("PRAGMA foreign_key_check")).first()
        if broken_links:
            raise RuntimeError(f"SQLite migration left a broken foreign key: {broken_links}")


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
