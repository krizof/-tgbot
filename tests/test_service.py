from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from bot.db import init_db, make_engine, make_session_factory
from bot.models import PendingSuggestion, Response, Suggestion
from bot.service import (
    begin_name_change,
    begin_suggestion,
    close_poll,
    counts_for,
    create_invite,
    create_poll,
    ensure_admins,
    get_active_participant,
    game_leaderboard,
    is_name_change_pending,
    register_with_invite,
    record_game_score,
    save_suggestion,
    save_profile,
    set_response,
    utcnow,
)


@pytest.fixture
async def factory():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    yield make_session_factory(engine)
    await engine.dispose()


async def test_invite_is_limited_and_does_not_store_plain_token(factory):
    async with factory() as session:
        token = await create_invite(session, uses=1, lifetime_hours=1)
        assert await register_with_invite(session, 1001, token) == "registered"
        assert await register_with_invite(session, 1002, token) == "invalid"
        assert await get_active_participant(session, 1001) is not None


async def test_vote_can_change_and_close_erases_individual_responses(factory):
    async with factory() as session:
        await ensure_admins(session, frozenset({1001, 1002, 1003}))
        for telegram_id in (1001, 1002, 1003):
            participant_to_name = await get_active_participant(session, telegram_id)
            assert participant_to_name is not None
            assert await save_profile(session, participant_to_name, f"Friend {telegram_id}", f"user{telegram_id}")
        poll = await create_poll(
            session, date(2026, 8, 11), "Discord?", utcnow() + timedelta(hours=1)
        )
        assert poll is not None
        participant = await get_active_participant(session, 1001)
        assert participant is not None

        counts = await set_response(session, poll, participant, "ready")
        assert (counts.ready, counts.unanswered) == (1, 2)
        counts = await set_response(session, poll, participant, "later")
        assert (counts.ready, counts.later, counts.unanswered) == (0, 1, 2)
        await begin_suggestion(session, poll, participant)
        await save_suggestion(session, poll, participant, "@friend", "Смогу в 22:30")

        final_counts, _ = await close_poll(session, poll)
        assert final_counts == counts
        remaining = await session.scalar(select(func.count(Response.id)))
        assert remaining == 0
        assert await session.scalar(select(func.count(Suggestion.id))) == 0
        assert await session.scalar(select(func.count(PendingSuggestion.id))) == 0
        assert await counts_for(session, poll) == final_counts


async def test_public_names_are_unique_case_insensitively(factory):
    async with factory() as session:
        await ensure_admins(session, frozenset({1001, 1002}))
        first = await get_active_participant(session, 1001)
        second = await get_active_participant(session, 1002)
        assert first is not None and second is not None
        assert await save_profile(session, first, "Саня", "sanya") is not None
        assert await save_profile(session, second, "саня", "other") is None


async def test_two_step_name_change_is_completed_by_next_name(factory):
    async with factory() as session:
        await ensure_admins(session, frozenset({1001}))
        participant = await get_active_participant(session, 1001)
        assert participant is not None
        assert await save_profile(session, participant, "Старое имя", "friend") is not None
        await begin_name_change(session, participant)
        assert await is_name_change_pending(session, participant)
        assert await save_profile(session, participant, "Новое имя", "friend") is not None
        assert not await is_name_change_pending(session, participant)


async def test_game_score_keeps_personal_best_and_builds_leaderboard(factory):
    async with factory() as session:
        await ensure_admins(session, frozenset({1001, 1002}))
        first = await get_active_participant(session, 1001)
        second = await get_active_participant(session, 1002)
        assert first is not None and second is not None
        await save_profile(session, first, "Саня", "sanya")
        await save_profile(session, second, "Дима", "dima")
        await record_game_score(session, first, "snake", 15)
        await record_game_score(session, first, "snake", 9)
        await record_game_score(session, second, "snake", 20)
        assert await game_leaderboard(session, "snake") == [("Дима", 20), ("Саня", 15)]


async def test_next_poll_is_allowed_after_close_on_same_day(factory):
    async with factory() as session:
        deadline = utcnow() + timedelta(hours=1)
        first = await create_poll(session, date(2026, 8, 11), "One", deadline)
        second = await create_poll(session, date(2026, 8, 11), "Two", deadline)
        assert first is not None
        assert second is None
        await close_poll(session, first)
        third = await create_poll(session, date(2026, 8, 11), "Three", deadline)
        assert third is not None
        assert third.id != first.id


async def test_legacy_daily_unique_database_is_migrated(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            """
            CREATE TABLE polls (
                id INTEGER PRIMARY KEY, local_date DATE NOT NULL UNIQUE,
                question VARCHAR(300) NOT NULL, deadline_at DATETIME NOT NULL,
                closed_at DATETIME, aggregate_json TEXT, created_at DATETIME NOT NULL
            )
            """
        )
        await connection.exec_driver_sql(
            "CREATE TABLE legacy_links (id INTEGER PRIMARY KEY, poll_id INTEGER REFERENCES polls(id))"
        )
        await connection.exec_driver_sql(
            "INSERT INTO polls VALUES (1, '2026-08-11', 'Old', '2026-08-11 20:00:00', "
            "'2026-08-11 20:00:00', '{}', '2026-08-11 19:00:00')"
        )
        await connection.exec_driver_sql("INSERT INTO legacy_links VALUES (1, 1)")

    await init_db(engine)
    async with engine.begin() as connection:
        foreign_target = (await connection.exec_driver_sql("PRAGMA foreign_key_list('legacy_links')")).first()
        assert foreign_target is not None and foreign_target[2] == "polls"
        await connection.exec_driver_sql(
            "INSERT INTO polls VALUES (2, '2026-08-11', 'New', '2026-08-11 22:00:00', "
            "NULL, NULL, '2026-08-11 21:00:00')"
        )
    await engine.dispose()
