from __future__ import annotations

import hashlib
import json
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Delivery, GameScore, Invite, Participant, PendingName, PendingSuggestion, Poll, Profile, Response, Suggestion


VALID_STATUSES = ("ready", "later", "no")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Counts:
    ready: int = 0
    later: int = 0
    no: int = 0
    unanswered: int = 0

    @property
    def total(self) -> int:
        return self.ready + self.later + self.no + self.unanswered

    def to_json(self) -> str:
        return json.dumps({key: getattr(self, key) for key in ("ready", "later", "no", "unanswered")})

    @classmethod
    def from_json(cls, value: str) -> "Counts":
        data = json.loads(value)
        return cls(**{key: int(data.get(key, 0)) for key in ("ready", "later", "no", "unanswered")})


async def ensure_admins(session: AsyncSession, admin_ids: frozenset[int]) -> None:
    existing = set((await session.scalars(select(Participant.telegram_user_id).where(Participant.telegram_user_id.in_(admin_ids)))).all())
    session.add_all(Participant(telegram_user_id=user_id) for user_id in admin_ids - existing)
    await session.commit()


async def create_invite(session: AsyncSession, uses: int, lifetime_hours: int) -> str:
    if not 1 <= uses <= 50 or not 1 <= lifetime_hours <= 168:
        raise ValueError("Invite limits are out of range")
    token = secrets.token_urlsafe(24)
    session.add(Invite(token_hash=hash_token(token), uses_left=uses, expires_at=utcnow() + timedelta(hours=lifetime_hours)))
    await session.commit()
    return token


async def register_with_invite(session: AsyncSession, telegram_user_id: int, token: str) -> str:
    participant = await session.scalar(select(Participant).where(Participant.telegram_user_id == telegram_user_id))
    if participant:
        if not participant.active:
            return "revoked"
        return "already"

    result = await session.execute(
        update(Invite)
        .where(
            Invite.token_hash == hash_token(token),
            Invite.uses_left > 0,
            Invite.expires_at > utcnow(),
        )
        .values(uses_left=Invite.uses_left - 1)
    )
    if not result.rowcount:
        await session.rollback()
        return "invalid"
    session.add(Participant(telegram_user_id=telegram_user_id))
    try:
        await session.commit()
    except IntegrityError:
        # Two near-simultaneous /start updates from the same Telegram account.
        # The failed transaction also restores its invite use.
        await session.rollback()
        participant = await session.scalar(select(Participant).where(Participant.telegram_user_id == telegram_user_id))
        return "already" if participant and participant.active else "invalid"
    return "registered"


async def get_active_participant(session: AsyncSession, telegram_user_id: int) -> Participant | None:
    return await session.scalar(
        select(Participant).where(Participant.telegram_user_id == telegram_user_id, Participant.active.is_(True))
    )


async def profile_for(session: AsyncSession, participant: Participant) -> Profile | None:
    return await session.scalar(select(Profile).where(Profile.participant_id == participant.id))


async def save_profile(
    session: AsyncSession, participant: Participant, public_name: str, telegram_username: str | None
) -> Profile | None:
    public_name_key = unicodedata.normalize("NFKC", public_name).casefold()
    profile = await profile_for(session, participant)
    if profile:
        profile.public_name = public_name
        profile.public_name_key = public_name_key
        profile.telegram_username = telegram_username
        profile.updated_at = utcnow()
    else:
        profile = Profile(
            participant_id=participant.id,
            public_name=public_name,
            public_name_key=public_name_key,
            telegram_username=telegram_username,
        )
        session.add(profile)
    try:
        await session.execute(
            update(Suggestion).where(Suggestion.participant_id == participant.id).values(author_label=public_name)
        )
        await session.execute(delete(PendingName).where(PendingName.participant_id == participant.id))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None
    await session.refresh(profile)
    return profile


async def begin_name_change(session: AsyncSession, participant: Participant) -> None:
    pending = await session.scalar(select(PendingName).where(PendingName.participant_id == participant.id))
    if pending:
        pending.created_at = utcnow()
    else:
        session.add(PendingName(participant_id=participant.id))
    await session.commit()


async def is_name_change_pending(session: AsyncSession, participant: Participant) -> bool:
    return bool(
        await session.scalar(select(PendingName.id).where(PendingName.participant_id == participant.id))
    )


async def sync_profile_username(
    session: AsyncSession, participant: Participant, telegram_username: str | None
) -> Profile | None:
    profile = await profile_for(session, participant)
    if profile and profile.telegram_username != telegram_username:
        profile.telegram_username = telegram_username
        profile.updated_at = utcnow()
        await session.commit()
    return profile


async def public_profiles(session: AsyncSession) -> list[tuple[Profile, Participant]]:
    rows = await session.execute(
        select(Profile, Participant)
        .join(Participant, Participant.id == Profile.participant_id)
        .where(Participant.active.is_(True))
        .order_by(Profile.public_name)
    )
    return list(rows.all())


async def record_game_score(
    session: AsyncSession, participant: Participant, mode: str, score: int
) -> GameScore:
    if mode not in {"snake", "tap"} or not 0 <= score <= 1_000_000:
        raise ValueError("Invalid game score")
    game_score = await session.scalar(
        select(GameScore).where(GameScore.participant_id == participant.id, GameScore.mode == mode)
    )
    if game_score:
        game_score.last_score = score
        game_score.best_score = max(game_score.best_score, score)
        game_score.updated_at = utcnow()
    else:
        game_score = GameScore(
            participant_id=participant.id, mode=mode, best_score=score, last_score=score
        )
        session.add(game_score)
    await session.commit()
    await session.refresh(game_score)
    return game_score


async def game_leaderboard(session: AsyncSession, mode: str, limit: int = 20) -> list[tuple[str, int]]:
    rows = await session.execute(
        select(Profile.public_name, GameScore.best_score)
        .join(Participant, Participant.id == Profile.participant_id)
        .join(GameScore, GameScore.participant_id == Participant.id)
        .where(Participant.active.is_(True), GameScore.mode == mode)
        .order_by(GameScore.best_score.desc(), GameScore.updated_at.asc())
        .limit(limit)
    )
    return [(name, int(score)) for name, score in rows.all()]


async def create_poll(session: AsyncSession, local_date: date, question: str, deadline_utc: datetime) -> Poll | None:
    existing = await session.scalar(
        select(Poll).where(Poll.closed_at.is_(None)).order_by(Poll.id.desc()).limit(1)
    )
    if existing:
        return None
    poll = Poll(local_date=local_date, question=question[:300], deadline_at=deadline_utc)
    session.add(poll)
    await session.commit()
    await session.refresh(poll)
    return poll


async def current_poll(session: AsyncSession, local_date: date) -> Poll | None:
    return await session.scalar(
        select(Poll).where(Poll.local_date == local_date).order_by(Poll.id.desc()).limit(1)
    )


async def active_participants(session: AsyncSession) -> list[Participant]:
    return list(
        (
            await session.scalars(
                select(Participant)
                .join(Profile, Profile.participant_id == Participant.id)
                .where(Participant.active.is_(True))
            )
        ).all()
    )


async def save_delivery(session: AsyncSession, poll_id: int, participant_id: int, chat_id: int, message_id: int) -> None:
    session.add(Delivery(poll_id=poll_id, participant_id=participant_id, chat_id=chat_id, message_id=message_id))
    await session.commit()


async def counts_for(session: AsyncSession, poll: Poll) -> Counts:
    if poll.closed_at and poll.aggregate_json:
        return Counts.from_json(poll.aggregate_json)
    rows = (await session.execute(
        select(Response.status, func.count(Response.id)).where(Response.poll_id == poll.id).group_by(Response.status)
    )).all()
    values = {status: count for status, count in rows}
    total = int(
        await session.scalar(
            select(func.count(Participant.id))
            .join(Profile, Profile.participant_id == Participant.id)
            .where(Participant.active.is_(True))
        )
        or 0
    )
    answered = sum(values.values())
    return Counts(
        ready=values.get("ready", 0), later=values.get("later", 0), no=values.get("no", 0),
        unanswered=max(0, total - answered),
    )


async def set_response(session: AsyncSession, poll: Poll, participant: Participant, status: str) -> Counts:
    if status not in VALID_STATUSES:
        raise ValueError("Unknown status")
    now = utcnow()
    if poll.closed_at or poll.deadline_at <= now:
        raise RuntimeError("Poll is closed")
    # A harmless UPDATE obtains the SQLite write lock before checking/saving a
    # vote. It serializes the last click with the deadline-closing transaction.
    lock = await session.execute(
        update(Poll)
        .where(Poll.id == poll.id, Poll.closed_at.is_(None), Poll.deadline_at > now)
        .values(created_at=Poll.created_at)
    )
    if not lock.rowcount:
        await session.rollback()
        raise RuntimeError("Poll is closed")
    response = await session.scalar(
        select(Response).where(Response.poll_id == poll.id, Response.participant_id == participant.id)
    )
    if response:
        response.status = status
        response.updated_at = utcnow()
    else:
        session.add(Response(poll_id=poll.id, participant_id=participant.id, status=status))
    await session.execute(
        delete(PendingSuggestion).where(
            PendingSuggestion.poll_id == poll.id, PendingSuggestion.participant_id == participant.id
        )
    )
    await session.execute(
        delete(Suggestion).where(Suggestion.poll_id == poll.id, Suggestion.participant_id == participant.id)
    )
    try:
        await session.commit()
    except IntegrityError:
        # A very fast double-click may race on the unique response row.
        await session.rollback()
        response = await session.scalar(
            select(Response).where(Response.poll_id == poll.id, Response.participant_id == participant.id)
        )
        if not response:
            raise
        response.status = status
        response.updated_at = utcnow()
        await session.commit()
    return await counts_for(session, poll)


async def begin_suggestion(session: AsyncSession, poll: Poll, participant: Participant) -> None:
    pending = await session.scalar(
        select(PendingSuggestion).where(PendingSuggestion.participant_id == participant.id)
    )
    if pending:
        pending.poll_id = poll.id
        pending.created_at = utcnow()
    else:
        session.add(PendingSuggestion(poll_id=poll.id, participant_id=participant.id))
    await session.commit()


async def pending_suggestion(session: AsyncSession, participant: Participant) -> PendingSuggestion | None:
    return await session.scalar(
        select(PendingSuggestion).where(PendingSuggestion.participant_id == participant.id)
    )


async def save_suggestion(
    session: AsyncSession, poll: Poll, participant: Participant, author_label: str, text: str
) -> Suggestion:
    if poll.closed_at or poll.deadline_at <= utcnow():
        raise RuntimeError("Poll is closed")
    suggestion = await session.scalar(
        select(Suggestion).where(Suggestion.poll_id == poll.id, Suggestion.participant_id == participant.id)
    )
    if suggestion:
        suggestion.author_label = author_label[:160]
        suggestion.text = text[:500]
        suggestion.updated_at = utcnow()
    else:
        suggestion = Suggestion(
            poll_id=poll.id, participant_id=participant.id, author_label=author_label[:160], text=text[:500]
        )
        session.add(suggestion)
    await session.execute(
        delete(PendingSuggestion).where(PendingSuggestion.participant_id == participant.id)
    )
    await session.commit()
    return suggestion


async def suggestions_for(session: AsyncSession, poll: Poll) -> list[Suggestion]:
    return list(
        (await session.scalars(select(Suggestion).where(Suggestion.poll_id == poll.id).order_by(Suggestion.updated_at))).all()
    )


async def close_poll(session: AsyncSession, poll: Poll) -> tuple[Counts, list[Delivery]]:
    if poll.closed_at:
        return await counts_for(session, poll), []
    lock = await session.execute(
        update(Poll)
        .where(Poll.id == poll.id, Poll.closed_at.is_(None))
        .values(created_at=Poll.created_at)
    )
    if not lock.rowcount:
        await session.rollback()
        await session.refresh(poll)
        return await counts_for(session, poll), []
    counts = await counts_for(session, poll)
    deliveries = list((await session.scalars(select(Delivery).where(Delivery.poll_id == poll.id))).all())
    poll.aggregate_json = counts.to_json()
    poll.closed_at = utcnow()
    await session.execute(delete(Response).where(Response.poll_id == poll.id))
    await session.execute(delete(PendingSuggestion).where(PendingSuggestion.poll_id == poll.id))
    await session.execute(delete(Suggestion).where(Suggestion.poll_id == poll.id))
    await session.execute(delete(Delivery).where(Delivery.poll_id == poll.id))
    await session.commit()
    return counts, deliveries


async def close_due_polls(session: AsyncSession) -> list[Poll]:
    return list((await session.scalars(select(Poll).where(Poll.closed_at.is_(None), Poll.deadline_at <= utcnow()))).all())


async def purge_old_aggregates(session: AsyncSession, days: int) -> int:
    cutoff = utcnow() - timedelta(days=days)
    result = await session.execute(delete(Poll).where(Poll.closed_at.is_not(None), Poll.closed_at < cutoff))
    await session.commit()
    return int(result.rowcount or 0)


async def revoke_participant(session: AsyncSession, telegram_user_id: int) -> bool:
    participant = await session.scalar(select(Participant).where(Participant.telegram_user_id == telegram_user_id))
    if not participant or not participant.active:
        return False
    participant.active = False
    await session.execute(delete(Response).where(Response.participant_id == participant.id))
    await session.execute(delete(Delivery).where(Delivery.participant_id == participant.id))
    await session.execute(delete(PendingSuggestion).where(PendingSuggestion.participant_id == participant.id))
    await session.execute(delete(PendingName).where(PendingName.participant_id == participant.id))
    await session.execute(delete(GameScore).where(GameScore.participant_id == participant.id))
    await session.execute(delete(Suggestion).where(Suggestion.participant_id == participant.id))
    await session.execute(delete(Profile).where(Profile.participant_id == participant.id))
    await session.commit()
    return True
