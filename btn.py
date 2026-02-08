from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def subscription_action_keyboard(is_active: bool):
    """
    is_active = True  -> 💵 Оформить сейчас
    is_active = False -> 💵 Купить
    """
    text = "💵 Оформить сейчас" if is_active else "💵 Купить подписку"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=text,
            callback_data="renew_sub"
        )
    )
    return builder.as_markup()