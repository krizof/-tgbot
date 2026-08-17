from __future__ import annotations

import html
import json
import logging
from datetime import datetime, time, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings, parse_hhmm
from .db import init_db, make_engine, make_session_factory
from .keyboards import STATUS_LABELS, vote_keyboard
from .models import Participant, Poll, Profile, Response
from .service import (
    Counts,
    active_participants,
    begin_name_change,
    begin_suggestion,
    close_due_polls,
    close_poll,
    counts_for,
    create_invite,
    create_poll,
    current_poll,
    ensure_admins,
    game_leaderboard,
    get_active_participant,
    is_name_change_pending,
    pending_suggestion,
    profile_for,
    public_profiles,
    purge_old_aggregates,
    register_with_invite,
    record_game_score,
    revoke_participant,
    save_delivery,
    save_profile,
    save_suggestion,
    set_response,
    suggestions_for,
    sync_profile_username,
    utcnow,
)


logger = logging.getLogger(__name__)
router = Router()

WELCOME = (
    "Привет! Это закрытый чек-ин нашей компании.\n\n"
    "Когда появится опрос, выбери один статус. Ответ можно менять до дедлайна. "
    "Другие участники увидят только общие числа — без имён и индивидуальных ответов. "
    "Твоё выбранное имя видно в списке компании, но Telegram-аккаунт скрыт.\n\n"
    "Команды: /today — сегодняшний опрос, /games — игры, /leaderboard — рейтинг, "
    "/people — участники, /name — сменить имя, "
    "/privacy — приватность, /help — помощь."
)


def _is_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.admin_ids


def _clean_public_name(value: str) -> str | None:
    name = " ".join(value.strip().split())
    if not 2 <= len(name) <= 32 or name.startswith("/") or "@" in name:
        return None
    return name


def _admin_identity(username: str | None, telegram_user_id: int) -> str:
    return f"@{username}" if username else f"Telegram ID {telegram_user_id}"


def _has_copyable_media(message: Message) -> bool:
    return bool(
        message.video
        or message.photo
        or message.animation
        or message.document
        or message.audio
        or message.voice
        or message.video_note
        or message.sticker
    )


GAME_LABELS = {"snake": "🐍 Змейка", "tap": "⚡ Тап-спринт"}


def _format_leaderboard(mode: str, rows: list[tuple[str, int]]) -> str:
    title = GAME_LABELS.get(mode, mode)
    if not rows:
        return f"<b>{title}</b>\nПока никто не набрал очки."
    places = "\n".join(
        f"{index}. {html.escape(name)} — <b>{score}</b>"
        for index, (name, score) in enumerate(rows, start=1)
    )
    return f"<b>{title}</b>\n{places}"


def _local_now(settings: Settings) -> datetime:
    return datetime.now(settings.timezone)


def _deadline_local(poll: Poll, settings: Settings) -> datetime:
    return poll.deadline_at.replace(tzinfo=timezone.utc).astimezone(settings.timezone)


def format_counts(counts: Counts) -> str:
    return (
        f"✅ Готовы: <b>{counts.ready}</b>\n"
        f"🤔 Думают / позже: <b>{counts.later}</b>\n"
        f"❌ Не готовы: <b>{counts.no}</b>\n"
        f"⚪ Пока без ответа: <b>{counts.unanswered}</b>"
    )


def poll_text(poll: Poll, counts: Counts, settings: Settings, selected: str | None = None) -> str:
    deadline = _deadline_local(poll, settings).strftime("%H:%M")
    selected_line = f"\nТвой выбор: <b>{STATUS_LABELS[selected]}</b>\n" if selected else "\n"
    return (
        f"<b>{html.escape(poll.question)}</b>\n"
        f"Ответы принимаются до <b>{deadline}</b>."
        f"{selected_line}\n{format_counts(counts)}\n\n"
        "Можно нажать другую кнопку и изменить свой ответ."
    )


def closed_text(poll: Poll, counts: Counts) -> str:
    return f"<b>{html.escape(poll.question)}</b>\nОпрос завершён.\n\n{format_counts(counts)}"


async def send_poll_to_participant(
    bot: Bot,
    session: AsyncSession,
    participant: Participant,
    poll: Poll,
    settings: Settings,
    *,
    remember_delivery: bool = True,
) -> bool:
    counts = await counts_for(session, poll)
    try:
        message = await bot.send_message(
            participant.telegram_user_id,
            poll_text(poll, counts, settings),
            reply_markup=vote_keyboard(poll.id),
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        return False
    if remember_delivery:
        await save_delivery(session, poll.id, participant.id, message.chat.id, message.message_id)
    return True


async def broadcast_poll(bot: Bot, factory: async_sessionmaker[AsyncSession], poll: Poll, settings: Settings) -> tuple[int, int]:
    sent = failed = 0
    async with factory() as session:
        for participant in await active_participants(session):
            if await send_poll_to_participant(bot, session, participant, poll, settings):
                sent += 1
            else:
                failed += 1
    logger.info("Poll %s delivery finished: sent=%s failed=%s", poll.id, sent, failed)
    return sent, failed


async def finalize_poll(bot: Bot, factory: async_sessionmaker[AsyncSession], poll_id: int) -> Counts | None:
    async with factory() as session:
        poll = await session.get(Poll, poll_id)
        if not poll:
            return None
        counts, deliveries = await close_poll(session, poll)
        text = closed_text(poll, counts)
        for delivery in deliveries:
            try:
                await bot.edit_message_text(text, chat_id=delivery.chat_id, message_id=delivery.message_id)
            except (TelegramForbiddenError, TelegramBadRequest):
                pass
        logger.info("Poll %s finalized; individual responses erased", poll.id)
        return counts


async def create_today_poll(bot: Bot, factory: async_sessionmaker[AsyncSession], settings: Settings) -> Poll | None:
    now = _local_now(settings)
    deadline = (now + timedelta(hours=settings.default_deadline_hours)).astimezone(timezone.utc).replace(tzinfo=None)
    async with factory() as session:
        poll = await create_poll(session, now.date(), "Кто сегодня готов зайти в Discord?", deadline)
    if poll:
        await broadcast_poll(bot, factory, poll, settings)
    return poll


async def close_expired_job(bot: Bot, factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    async with factory() as session:
        due_ids = [poll.id for poll in await close_due_polls(session)]
    for poll_id in due_ids:
        await finalize_poll(bot, factory, poll_id)
    async with factory() as session:
        await purge_old_aggregates(session, settings.aggregate_retention_days)


@router.message(F.chat.type != ChatType.PRIVATE)
async def reject_group_chat(message: Message) -> None:
    await message.reply("Ради приватности этот бот работает только в личном чате.")


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_handler(
    message: Message,
    command: CommandObject,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not message.from_user:
        return
    async with session_factory() as session:
        participant = await get_active_participant(session, message.from_user.id)
        if participant:
            profile = await sync_profile_username(session, participant, message.from_user.username)
            if not profile:
                await message.answer(
                    "Инвайт принят. Теперь напиши имя, которое будут видеть друзья. "
                    "От 2 до 32 символов, без @username. Например: <b>Саня</b>."
                )
                return
            await message.answer(WELCOME)
            return
        if not command.args:
            await message.answer("Бот закрытый. Попроси администратора прислать персональную ссылку-приглашение.")
            return
        result = await register_with_invite(session, message.from_user.id, command.args.strip())
        if result in {"invalid", "revoked"}:
            await message.answer("Ссылка недействительна или истекла. Попроси администратора создать новую.")
            return
        participant = await get_active_participant(session, message.from_user.id)
        if participant:
            await message.answer(
                "Инвайт принят. Напиши имя, которое будут видеть друзья. "
                "От 2 до 32 символов, без @username. Например: <b>Саня</b>."
            )


@router.message(Command("help"), F.chat.type == ChatType.PRIVATE)
async def help_handler(message: Message, settings: Settings) -> None:
    text = WELCOME
    if message.from_user and _is_admin(message.from_user.id, settings):
        text += (
            "\n\n<b>Администратор:</b>\n"
            "/invite [число] [часы] — ссылка (по умолчанию 1 использование, 48 часов)\n"
            "/broadcast [текст] — разослать сообщение или прикреплённое медиа\n"
            "/poll [HH:MM] [текст] — создать и разослать опрос\n"
            "/summary — текущая сводка\n/close — завершить опрос\n"
            "/details — кто что выбрал и уточнения по времени\n"
            "/members — имена, @username и Telegram ID\n/revoke TELEGRAM_ID — отключить участника"
        )
    await message.answer(text)


@router.message(Command("games"), F.chat.type == ChatType.PRIVATE)
async def games_handler(
    message: Message, session_factory: async_sessionmaker[AsyncSession], settings: Settings,
) -> None:
    if not message.from_user:
        return
    async with session_factory() as session:
        participant = await get_active_participant(session, message.from_user.id)
        profile = await profile_for(session, participant) if participant else None
    if not participant or not profile:
        await message.answer("Сначала зарегистрируйся и задай публичное имя.")
        return
    if not settings.mini_app_url:
        await message.answer("Игровое приложение ещё не опубликовано. Попробуй немного позже.")
        return
    game_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🎮 Открыть игры", web_app=WebAppInfo(url=settings.mini_app_url))]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    ranking_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏆 Рейтинг", callback_data="game:leaderboard")]]
    )
    await message.answer(
        "Нажми кнопку под полем ввода, выбери режим и набирай очки.",
        reply_markup=game_keyboard,
    )
    await message.answer("Рекорды всех участников доступны отдельно.", reply_markup=ranking_keyboard)


@router.message(Command("leaderboard"), F.chat.type == ChatType.PRIVATE)
async def leaderboard_handler(message: Message, session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        snake = await game_leaderboard(session, "snake")
        tap = await game_leaderboard(session, "tap")
    await message.answer(_format_leaderboard("snake", snake) + "\n\n" + _format_leaderboard("tap", tap))


@router.callback_query(F.data == "game:leaderboard")
async def leaderboard_callback(callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        snake = await game_leaderboard(session, "snake")
        tap = await game_leaderboard(session, "tap")
    if callback.message:
        await callback.message.answer(_format_leaderboard("snake", snake) + "\n\n" + _format_leaderboard("tap", tap))
    await callback.answer()


@router.message(F.web_app_data, F.chat.type == ChatType.PRIVATE)
async def game_score_handler(message: Message, session_factory: async_sessionmaker[AsyncSession]) -> None:
    if not message.from_user or not message.web_app_data:
        return
    try:
        payload = json.loads(message.web_app_data.data)
        mode = str(payload["mode"])
        score = int(payload["score"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        await message.answer("Не удалось принять результат игры.")
        return
    async with session_factory() as session:
        participant = await get_active_participant(session, message.from_user.id)
        if not participant or not await profile_for(session, participant):
            await message.answer("Сначала зарегистрируйся и задай публичное имя.")
            return
        try:
            saved = await record_game_score(session, participant, mode, score)
        except ValueError:
            await message.answer("Некорректный результат игры.")
            return
        rows = await game_leaderboard(session, mode)
    await message.answer(
        f"Результат: <b>{score}</b>. Личный рекорд: <b>{saved.best_score}</b>.\n\n"
        + _format_leaderboard(mode, rows)
    )


@router.message(Command("broadcast"), F.chat.type == ChatType.PRIVATE)
async def broadcast_handler(
    message: Message,
    command: CommandObject,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, settings):
        await message.answer("Команда доступна только администратору.")
        return

    source = message.reply_to_message or message
    text = (command.args or "").strip()
    has_media = _has_copyable_media(source)
    if not has_media and not text:
        if source is not message and source.text:
            text = source.text
        else:
            await message.answer(
                "Прикрепи видео, фото или другой файл с подписью <code>/broadcast текст</code>. "
                "Также можно ответить этой командой на ранее отправленное медиа."
            )
            return

    async with session_factory() as session:
        participants = await active_participants(session)

    sent = failed = 0
    for participant in participants:
        try:
            if has_media:
                if source is message:
                    # Strip the /broadcast command from the media caption.
                    caption = html.escape(text[:1024]) if text else ""
                else:
                    # With no replacement text, retain the replied media's original caption.
                    caption = html.escape(text[:1024]) if text else None
                await bot.copy_message(
                    chat_id=participant.telegram_user_id,
                    from_chat_id=source.chat.id,
                    message_id=source.message_id,
                    caption=caption,
                )
            else:
                await bot.send_message(participant.telegram_user_id, html.escape(text[:4096]))
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1

    await message.answer(f"Рассылка завершена. Доставлено: {sent}, не доставлено: {failed}.")


@router.message(Command("privacy"), F.chat.type == ChatType.PRIVATE)
async def privacy_handler(message: Message) -> None:
    await message.answer(
        "<b>Приватность</b>\n\n"
        "• участники получают опросы только в личном чате;\n"
        "• друзья не видят Telegram-аккаунты и отдельные ответы друг друга;\n"
        "• выбранное публичное имя видно друзьям, но Telegram username и ID — только администраторам;\n"
        "• администраторы видят, кто нажал каждую кнопку; остальные участники видят только общие числа;\n"
        "• если выбрать «Другое время», текст и ник увидят только администраторы;\n"
        "• пока опрос открыт, ответ связан с Telegram ID, чтобы его можно было изменить;\n"
        "• после дедлайна индивидуальные ответы удаляются, остаются только общие числа;\n"
        "• Telegram username и ID сохраняются для закрытого списка администраторов.\n\n"
        "Владелец сервера и Telegram технически видят Telegram ID. Это защита от раскрытия ответов друзьям, а не абсолютная анонимность."
    )


@router.message(Command("people"), F.chat.type == ChatType.PRIVATE)
async def people_handler(
    message: Message, session_factory: async_sessionmaker[AsyncSession], settings: Settings,
) -> None:
    if not message.from_user:
        return
    async with session_factory() as session:
        participant = await get_active_participant(session, message.from_user.id)
        if not participant or not await profile_for(session, participant):
            await message.answer("Сначала зарегистрируйся и задай публичное имя.")
            return
        profiles = await public_profiles(session)
    names = "\n".join(f"• {html.escape(profile.public_name)}" for profile, _ in profiles)
    await message.answer("<b>Наша компания:</b>\n" + (names or "Список пока пуст."))


@router.message(Command("name"), F.chat.type == ChatType.PRIVATE)
async def name_handler(
    message: Message, command: CommandObject, session_factory: async_sessionmaker[AsyncSession], settings: Settings,
) -> None:
    if not message.from_user:
        return
    async with session_factory() as session:
        participant = await get_active_participant(session, message.from_user.id)
        if not participant:
            await message.answer("Сначала нужна ссылка-приглашение от администратора.")
            return
        if not (command.args or "").strip():
            await begin_name_change(session, participant)
            await message.answer(
                "Напиши новое публичное имя следующим сообщением — от 2 до 32 символов, без @username."
            )
            return
        name = _clean_public_name(command.args or "")
        if not name:
            await message.answer("Имя должно содержать от 2 до 32 символов и не должно включать @username.")
            return
        profile = await save_profile(session, participant, name, message.from_user.username)
    if not profile:
        await message.answer("Это имя уже занято. Выбери другое.")
    else:
        await message.answer(f"Теперь друзья будут видеть тебя как <b>{html.escape(name)}</b>. Telegram username им не показывается.")


@router.message(Command("today"), F.chat.type == ChatType.PRIVATE)
async def today_handler(message: Message, bot: Bot, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    if not message.from_user:
        return
    async with session_factory() as session:
        participant = await get_active_participant(session, message.from_user.id)
        if not participant:
            await message.answer("Сначала нужна ссылка-приглашение от администратора.")
            return
        profile = await sync_profile_username(session, participant, message.from_user.username)
        if not profile:
            await message.answer("Сначала напиши публичное имя — просто отправь его сообщением боту.")
            return
        poll = await current_poll(session, _local_now(settings).date())
        if not poll:
            await message.answer("Сегодня опроса ещё нет.")
        elif poll.closed_at or poll.deadline_at <= utcnow():
            await message.answer(closed_text(poll, await counts_for(session, poll)))
        else:
            await send_poll_to_participant(bot, session, participant, poll, settings, remember_delivery=False)


@router.callback_query(F.data.startswith("vote:"))
async def vote_handler(
    callback: CallbackQuery,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not callback.data or not callback.from_user:
        return
    try:
        _, raw_poll_id, status = callback.data.split(":", 2)
        poll_id = int(raw_poll_id)
    except (ValueError, TypeError):
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    async with session_factory() as session:
        participant = await get_active_participant(session, callback.from_user.id)
        poll = await session.get(Poll, poll_id)
        profile = await sync_profile_username(session, participant, callback.from_user.username) if participant else None
        if not participant or not profile or not poll:
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            if status == "custom":
                counts = await set_response(session, poll, participant, "later")
                await begin_suggestion(session, poll, participant)
            else:
                counts = await set_response(session, poll, participant, status)
        except (RuntimeError, ValueError):
            await callback.answer("Опрос уже завершён", show_alert=True)
            return
        selected = "later" if status == "custom" else status
        admin_notice = (
            f"<b>{html.escape(profile.public_name)}</b> · "
            f"{html.escape(_admin_identity(profile.telegram_username, participant.telegram_user_id))}\n"
            f"Выбрал: <b>{STATUS_LABELS[selected]}</b>"
        )
        if status == "custom":
            admin_notice += "\nОжидается уточнение по времени."
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, admin_notice)
            except (TelegramForbiddenError, TelegramBadRequest):
                pass
        if callback.message:
            try:
                await callback.message.edit_text(poll_text(poll, counts, settings, selected=selected), reply_markup=vote_keyboard(poll.id))
            except TelegramBadRequest:
                pass
        if status == "custom" and callback.message:
            await callback.message.answer(
                "Напиши, когда сможешь. Например: <i>«В 22:00 не смогу, но буду в 22:30»</i>. "
                "Это сообщение и твой ник увидят только администраторы.",
                reply_markup=ForceReply(selective=True, input_field_placeholder="Смогу примерно в 22:30"),
            )
            await callback.answer("Теперь напиши удобное время")
        else:
            await callback.answer("Ответ сохранён")


@router.message(Command("invite"), F.chat.type == ChatType.PRIVATE)
async def invite_handler(
    message: Message, command: CommandObject, bot: Bot,
    session_factory: async_sessionmaker[AsyncSession], settings: Settings,
) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, settings):
        await message.answer("Команда доступна только администратору.")
        return
    parts = (command.args or "").split()
    try:
        uses = int(parts[0]) if parts else 1
        hours = int(parts[1]) if len(parts) > 1 else 48
        async with session_factory() as session:
            token = await create_invite(session, uses, hours)
    except ValueError:
        await message.answer("Формат: /invite [1–50 использований] [1–168 часов]")
        return
    username = (await bot.get_me()).username
    await message.answer(
        f"Приглашение на {uses} исп., действует {hours} ч.:\n"
        f"<code>https://t.me/{username}?start={token}</code>\n\n"
        "Передай ссылку лично. Не публикуй её в общем чате."
    )


@router.message(Command("poll"), F.chat.type == ChatType.PRIVATE)
async def poll_handler(
    message: Message, command: CommandObject, bot: Bot,
    session_factory: async_sessionmaker[AsyncSession], settings: Settings,
) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, settings):
        await message.answer("Команда доступна только администратору.")
        return
    parts = (command.args or "").split(maxsplit=1)
    now = _local_now(settings)
    question = "Кто сегодня готов зайти в Discord?"
    deadline_local = now + timedelta(hours=settings.default_deadline_hours)
    if parts:
        try:
            hour, minute = parse_hhmm(parts[0])
        except RuntimeError:
            await message.answer("Формат: /poll [HH:MM] [необязательный текст]")
            return
        deadline_local = datetime.combine(now.date(), time(hour, minute), tzinfo=settings.timezone)
        if deadline_local <= now:
            deadline_local += timedelta(days=1)
        if len(parts) > 1:
            question = parts[1].strip()[:300] or question
    deadline_utc = deadline_local.astimezone(timezone.utc).replace(tzinfo=None)
    async with session_factory() as session:
        poll = await create_poll(session, now.date(), question, deadline_utc)
    if not poll:
        await message.answer("Сначала заверши текущий открытый опрос командой /close.")
        return
    sent, failed = await broadcast_poll(bot, session_factory, poll, settings)
    await message.answer(
        f"Опрос создан. Доставлено: {sent}, не доставлено: {failed}. "
        "Друзья видят только общие числа; администраторы видят персональные ответы через /details."
    )


@router.message(Command("summary"), F.chat.type == ChatType.PRIVATE)
async def summary_handler(message: Message, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, settings):
        await message.answer("Команда доступна только администратору.")
        return
    async with session_factory() as session:
        poll = await current_poll(session, _local_now(settings).date())
        if not poll:
            await message.answer("Сегодня опроса нет.")
            return
        counts = await counts_for(session, poll)
        text = closed_text(poll, counts) if poll.closed_at else poll_text(poll, counts, settings)
        await message.answer(text)


@router.message(Command("details"), F.chat.type == ChatType.PRIVATE)
async def details_handler(message: Message, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, settings):
        await message.answer("Команда доступна только администратору.")
        return
    async with session_factory() as session:
        poll = await current_poll(session, _local_now(settings).date())
        if not poll:
            await message.answer("Сегодня опроса нет.")
            return
        if poll.closed_at:
            await message.answer(
                "Опрос уже завершён. Персональные ответы удалены; осталась только общая сводка /summary."
            )
            return
        profiles = await public_profiles(session)
        responses = {
            response.participant_id: response.status
            for response in (await session.scalars(select(Response).where(Response.poll_id == poll.id))).all()
        }
        suggestion_map = {
            suggestion.participant_id: suggestion.text
            for suggestion in await suggestions_for(session, poll)
        }
    lines = [f"<b>{html.escape(poll.question)}</b>", "<b>Ответы участников:</b>"]
    for profile, participant in profiles:
        identity = _admin_identity(profile.telegram_username, participant.telegram_user_id)
        status = responses.get(participant.id)
        status_label = STATUS_LABELS[status] if status else "⚪ Пока не ответил"
        line = f"\n<b>{html.escape(profile.public_name)}</b> · {html.escape(identity)}\n{status_label}"
        if participant.id in suggestion_map:
            line += f"\n<i>{html.escape(suggestion_map[participant.id])}</i>"
        lines.append(line)
    await message.answer("\n".join(lines))


@router.message(Command("close"), F.chat.type == ChatType.PRIVATE)
async def close_handler(
    message: Message, bot: Bot, session_factory: async_sessionmaker[AsyncSession], settings: Settings,
) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, settings):
        await message.answer("Команда доступна только администратору.")
        return
    async with session_factory() as session:
        poll = await current_poll(session, _local_now(settings).date())
    if not poll:
        await message.answer("Сегодня опроса нет.")
    elif poll.closed_at:
        await message.answer("Опрос уже завершён.")
    else:
        counts = await finalize_poll(bot, session_factory, poll.id)
        await message.answer("Опрос завершён. Индивидуальные ответы удалены.\n\n" + format_counts(counts or Counts()))


@router.message(Command("members"), F.chat.type == ChatType.PRIVATE)
async def members_handler(message: Message, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, settings):
        await message.answer("Команда доступна только администратору.")
        return
    async with session_factory() as session:
        profiles = await public_profiles(session)
        active_total = int(await session.scalar(select(func.count(Participant.id)).where(Participant.active.is_(True))) or 0)
    lines = [f"<b>Участники: {len(profiles)}</b>"]
    for profile, participant in profiles:
        identity = _admin_identity(profile.telegram_username, participant.telegram_user_id)
        lines.append(
            f"• <b>{html.escape(profile.public_name)}</b> — {html.escape(identity)} — "
            f"<code>{participant.telegram_user_id}</code>"
        )
    pending = active_total - len(profiles)
    if pending:
        lines.append(f"\nЕщё не выбрали публичное имя: {pending}")
    await message.answer("\n".join(lines))


@router.message(Command("revoke"), F.chat.type == ChatType.PRIVATE)
async def revoke_handler(
    message: Message, command: CommandObject, session_factory: async_sessionmaker[AsyncSession], settings: Settings,
) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, settings):
        await message.answer("Команда доступна только администратору.")
        return
    try:
        telegram_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Формат: /revoke TELEGRAM_ID")
        return
    if telegram_id in settings.admin_ids:
        await message.answer("Администратора нельзя отключить этой командой.")
        return
    async with session_factory() as session:
        changed = await revoke_participant(session, telegram_id)
    await message.answer("Участник отключён." if changed else "Активный участник с таким ID не найден.")


@router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def text_or_fallback_handler(
    message: Message, bot: Bot, session_factory: async_sessionmaker[AsyncSession], settings: Settings,
) -> None:
    if not message.from_user or not message.text:
        return
    async with session_factory() as session:
        participant = await get_active_participant(session, message.from_user.id)
        if not participant:
            await message.answer("Сначала нужна ссылка-приглашение от администратора.")
            return
        profile = await profile_for(session, participant)
        if not profile:
            name = _clean_public_name(message.text)
            if not name:
                await message.answer("Имя должно содержать от 2 до 32 символов и не должно включать @username.")
                return
            profile = await save_profile(session, participant, name, message.from_user.username)
            if not profile:
                await message.answer("Это имя уже занято. Напиши другое.")
                return
            await message.answer(
                f"Готово. Для друзей ты — <b>{html.escape(name)}</b>. Твой Telegram-аккаунт видят только администраторы.\n\n"
                + WELCOME
            )
            today_poll = await current_poll(session, _local_now(settings).date())
            if today_poll and not today_poll.closed_at and today_poll.deadline_at > utcnow():
                await send_poll_to_participant(bot, session, participant, today_poll, settings)
            return
        await sync_profile_username(session, participant, message.from_user.username)
        if await is_name_change_pending(session, participant):
            name = _clean_public_name(message.text)
            if not name:
                await message.answer("Имя должно содержать от 2 до 32 символов и не должно включать @username.")
                return
            updated_profile = await save_profile(session, participant, name, message.from_user.username)
            if not updated_profile:
                await message.answer("Это имя уже занято. Напиши другое.")
                return
            await message.answer(
                f"Готово. Теперь друзья будут видеть тебя как <b>{html.escape(name)}</b>."
            )
            return
        pending = await pending_suggestion(session, participant)
        if not pending:
            await message.answer("Не понял сообщение. Нажми /help, чтобы увидеть команды.")
            return
        poll = await session.get(Poll, pending.poll_id)
        if not poll:
            await message.answer("Опрос уже недоступен.")
            return
        text = message.text.strip()
        if not text or len(text) > 500:
            await message.answer("Напиши уточнение длиной от 1 до 500 символов.")
            return
        label = profile.public_name
        try:
            await save_suggestion(session, poll, participant, label, text)
        except RuntimeError:
            await message.answer("Опрос уже завершён, уточнение не сохранено.")
            return
    notification = (
        f"<b>Уточнение от {html.escape(label)}</b> · "
        f"{html.escape(_admin_identity(message.from_user.username, message.from_user.id))}\n"
        f"{html.escape(text)}\n\n"
        "Все текущие уточнения: /details"
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, notification)
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    await message.answer("Уточнение сохранено и отправлено администраторам. Другие участники его не увидят.")


@router.message(F.chat.type == ChatType.PRIVATE)
async def fallback_handler(message: Message, settings: Settings) -> None:
    if message.from_user and _is_admin(message.from_user.id, settings) and _has_copyable_media(message):
        await message.answer(
            "Чтобы разослать это всем, отправь медиа с подписью <code>/broadcast текст</code> "
            "или ответь на него командой <code>/broadcast текст</code>."
        )
        return
    await message.answer("Отправь текстовое сообщение или нажми /help.")


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine = make_engine(settings.database_url)
    await init_db(engine)
    factory = make_session_factory(engine)
    async with factory() as session:
        await ensure_admins(session, settings.admin_ids)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher["settings"] = settings
    dispatcher["session_factory"] = factory

    await bot.set_my_commands([
        BotCommand(command="today", description="Сегодняшний опрос"),
        BotCommand(command="games", description="Открыть игры"),
        BotCommand(command="leaderboard", description="Таблица рекордов"),
        BotCommand(command="people", description="Публичные имена участников"),
        BotCommand(command="name", description="Изменить публичное имя"),
        BotCommand(command="privacy", description="Как защищены ответы"),
        BotCommand(command="help", description="Помощь"),
    ])
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        close_expired_job, IntervalTrigger(seconds=30), args=(bot, factory, settings),
        id="close-expired", max_instances=1, coalesce=True,
    )
    if settings.daily_poll_time:
        hour, minute = parse_hhmm(settings.daily_poll_time)
        scheduler.add_job(
            create_today_poll, CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
            args=(bot, factory, settings), id="daily-poll", max_instances=1, coalesce=True,
        )
    scheduler.start()
    logger.info("Bot started; active participant identities and votes are never logged")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()
