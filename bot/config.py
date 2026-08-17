from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    timezone: ZoneInfo
    daily_poll_time: str | None
    default_deadline_hours: int
    database_url: str
    aggregate_retention_days: int
    log_level: str
    mini_app_url: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        raw_admins = _required("ADMIN_IDS")
        try:
            admin_ids = frozenset(int(item.strip()) for item in raw_admins.split(",") if item.strip())
        except ValueError as exc:
            raise RuntimeError("ADMIN_IDS must contain comma-separated numeric Telegram IDs") from exc
        if not admin_ids:
            raise RuntimeError("At least one ADMIN_IDS value is required")

        timezone_name = os.getenv("TIMEZONE", "Europe/Moscow").strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(f"Unknown TIMEZONE: {timezone_name}") from exc

        daily_time = os.getenv("DAILY_POLL_TIME", "").strip() or None
        if daily_time:
            _parse_hhmm(daily_time)

        deadline_hours = int(os.getenv("DEFAULT_DEADLINE_HOURS", "4"))
        retention_days = int(os.getenv("AGGREGATE_RETENTION_DAYS", "30"))
        if deadline_hours < 1:
            raise RuntimeError("DEFAULT_DEADLINE_HOURS must be at least 1")
        if retention_days < 1:
            raise RuntimeError("AGGREGATE_RETENTION_DAYS must be at least 1")

        return cls(
            bot_token=_required("BOT_TOKEN"),
            admin_ids=admin_ids,
            timezone=timezone,
            daily_poll_time=daily_time,
            default_deadline_hours=deadline_hours,
            database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:////app/data/bot.db"),
            aggregate_retention_days=retention_days,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            mini_app_url=os.getenv("MINI_APP_URL", "").strip().rstrip("/") or None,
        )


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Time must use HH:MM format") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RuntimeError("Time must use HH:MM format")
    return hour, minute


def parse_hhmm(value: str) -> tuple[int, int]:
    return _parse_hhmm(value)
