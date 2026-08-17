from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


STATUS_LABELS = {
    "ready": "✅ Готов",
    "later": "🤔 Думаю / позже",
    "no": "❌ Не готов",
}


def vote_keyboard(poll_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"vote:{poll_id}:{status}")]
            for status, label in STATUS_LABELS.items()
        ] + [[InlineKeyboardButton(text="✍️ Смогу в другое время", callback_data=f"vote:{poll_id}:custom")]]
    )
