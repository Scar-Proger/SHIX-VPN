import os
import logging
import json
import asyncio
from database import now_local
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile
from functions import create_vless_profile, get_user_stats, get_online_users, sync_remnawave_expire
from payment.platega_payment import create_platega_payment, get_platega_payment_status
from datetime import datetime, timedelta
from aiogram import Dispatcher, Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, WebAppInfo

from config import config
from locales import TEXTS, TARIFFS
from database import (
    get_user, create_user, apply_promo_code, create_or_update_promo_code, 
    get_all_promocodes_list, delete_promocode,
    get_all_users, create_payment, process_payment_result,
    User, PromoCode, Session, get_user_stats as db_user_stats
)

logger = logging.getLogger(__name__)

router = Router()

MAX_MESSAGE_LENGTH = 4096
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class AdminStates(StatesGroup):
    ADD_TIME = State()
    REMOVE_TIME = State()
    CREATE_STATIC_PROFILE = State()
    SEND_MESSAGE = State()
    ADD_TIME_USER = State()
    REMOVE_TIME_USER = State()
    ADD_TIME_AMOUNT = State()
    REMOVE_TIME_AMOUNT = State()
    SEND_MESSAGE_TARGET = State()

# ------------------------------
# Состояния для ввода промокода
# ------------------------------
class PromoCodeStates(StatesGroup):
    waiting_for_code = State()

# ------------------------------
# Состояния для админ-промокода
# ------------------------------
class AdminPromoStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_discount = State()   
    waiting_for_max_uses = State()

USERS_PER_PAGE = 5

blocked_users = []
    
# ------------------------------
# Получение промокода из БД
# ------------------------------
async def get_promo_code(code: str):
    """Возвращает объект PromoCode из БД, если он активен"""
    with Session() as session:
        return session.query(PromoCode).filter_by(code=code.upper(), is_active=True).first()
    

def is_subscription_active(user) -> bool:
    if not user.subscription_end:
        return False
    return user.subscription_end > now_local()


def t(user, key: str, **kwargs) -> str:
    lang = getattr(user, "language", "ru") or "ru"
    lang_dict = TEXTS.get(lang, TEXTS["ru"])

    if key not in lang_dict:
        return f"❗{key}"

    return lang_dict[key].format(**kwargs)


def admin_channel_joined_text(user) -> str:
    username = f"@{user.username}" if user.username else "—"
    full_name = user.full_name or "Без имени"

    return (
        "✅ <b>Пользователь подписался на канал</b>\n\n"
        f"• <b>{full_name}</b>\n"
        f"  ├ {username}\n"
        f"  └ <code>{user.telegram_id}</code>"
    )


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=config.REQUIRED_CHANNEL_ID,
            user_id=user_id
        )
        return member.status in ("member", "administrator", "creator")
    except TelegramBadRequest:
        return False
    

async def send_subscribe_required(bot: Bot, chat_id: int):
    kb = InlineKeyboardBuilder()

    kb.button(
        text="📢 Подписаться на канал",
        url=config.REQUIRED_CHANNEL_URL
    )

    kb.button(
        text="✅ Я подписался",
        callback_data="check_subscription"
    )

    kb.adjust(1)

    await bot.send_photo(
        chat_id=chat_id,
        photo=FSInputFile("assets/vpn_banner.jpg"),  # путь к картинке
        caption=(
            "🔒 **Для использования бота необходимо подписаться на канал**\n\n"
            "После подписки нажмите кнопку ниже 👇"
        ),
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


async def notify_admins_user_joined(bot: Bot, user):
    text = admin_channel_joined_text(user)

    for admin_id in config.ADMINS:
        try:
            await bot.send_message(
                admin_id,
                text,
                parse_mode="HTML"
            )
        except TelegramForbiddenError:
            pass
        except Exception as e:
            logger.warning(f"Ошибка уведомления админа {admin_id}: {e}")


@router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, bot: Bot):
    telegram_id = callback.from_user.id
    full_name = callback.from_user.full_name
    username = callback.from_user.username

    # Проверяем подписку
    if not await is_subscribed(bot, telegram_id):
        await callback.answer("🚫 Вы ещё не подписались", show_alert=True)
        return

    # Удаляем сообщение с кнопкой
    try:
        await callback.message.delete()
    except Exception:
        pass

    # Получаем referrer_id, если есть (можно хранить в БД или передавать через callback)
    referrer_id = None

    # Создаём или получаем пользователя с правильными данными
    user = await ensure_user(
        bot,
        telegram_id,
        full_name=full_name,
        username=username,
        referrer_id=referrer_id
    )

    # Показываем меню
    await show_menu(bot, chat_id=telegram_id)


def split_text(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list:
    """Разбивает текст на части указанной максимальной длины"""
    if len(text) <= max_length:
        return [text]
    
    parts = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break
        part = text[:max_length]
        last_newline = part.rfind('\n')
        if last_newline != -1:
            part = part[:last_newline]
        parts.append(part)
        text = text[len(part):].lstrip()
    return parts


async def edit_text(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardBuilder | InlineKeyboardMarkup | None = None,
    parse_mode: str = "Markdown"
):
    if isinstance(reply_markup, InlineKeyboardBuilder):
        reply_markup = reply_markup.as_markup()

    await callback.message.edit_text(
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )


async def edit_caption(
    bot: Bot,
    chat_id: int,
    message_id: int,
    caption: str,
    reply_markup=None,
    parse_mode="Markdown"
):
    await bot.edit_message_caption(
        chat_id=chat_id,
        message_id=message_id,
        caption=caption,
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )


def format_time_left(end_date: datetime, user) -> str:
    now = now_local()
    delta = end_date - now

    if delta.total_seconds() <= 0:
        return t(user, "time_expired")

    total_seconds = int(delta.total_seconds())

    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    d = t(user, "time_day")
    h = t(user, "time_hour")
    m = t(user, "time_min")
    s = t(user, "time_sec")

    if days > 0:
        return f"{days} {d} {hours} {h}"
    elif hours > 0:
        return f"{hours} {h} {minutes} {m}"
    elif minutes > 0:
        return f"{minutes} {m} {seconds} {s}"
    else:
        return f"{seconds} {s}"



async def ensure_user(
    bot: Bot,
    telegram_id: int,
    full_name: str = "",
    username: str | None = None,
    referrer_id: int | None = None
) -> User:
    """
    Создаёт пользователя, если его нет, создаёт профиль, уведомляет админов и реферера.
    Возвращает объект пользователя.
    """
    user = await get_user(telegram_id)
    if user:
        return user
    
    if not isinstance(full_name, str):
        logger.error(f"❌ full_name не строка: {type(full_name)} | {full_name}")
        full_name = ""

    # 1️⃣ Создаём пользователя
    await create_user(
        telegram_id=telegram_id,
        full_name=full_name,
        username=username,
        is_admin=telegram_id in config.ADMINS,
        referrer_id=referrer_id,
        language="ru"
    )
    user = await get_user(telegram_id)

    # 2️⃣ Создаём профиль Remnawave
    profile_data = await create_vless_profile(telegram_id)
    if profile_data:
        sub_url = profile_data.get("sub_url")
        with Session() as session:
            db_user = session.query(User).filter_by(id=user.id).first()
            db_user.vless_profile_data = json.dumps(profile_data)
            db_user.sub_id = sub_url
            session.commit()
        logger.info(f"✅ Профиль создан для {telegram_id}")
    else:
        logger.error(f"❌ Не удалось создать профиль для {telegram_id}")

    # 3️⃣ Уведомляем админов
    await notify_admins_user_joined(bot, user)

    # 4️⃣ Уведомляем реферера
    if referrer_id:
        await bot.send_message(
            referrer_id,
            t(
                user,
                "referral_notify",
                name=username or full_name,
                id=telegram_id
            ),
            parse_mode="Markdown"
        )

    return user


# =========================================================
# UX
# =========================================================
async def show_menu(bot: Bot, chat_id: int, message_id: int = None):
    user = await get_user(chat_id)
    if not user:
        return

    now = now_local()

    if not user.subscription_end or user.subscription_end < now:
        status = t(user, "no_subscription")
        expire_date = t(user, "subscription_inactive")
        time_left = t(user, "subscription_inactive")

        time_left_text = t(user, "subscription_left", time=time_left)
        sub_text = ""

    else:
        status = t(user, "active")
        expire_date = user.subscription_end.strftime("%d-%m-%Y %H:%M")
        time_left = format_time_left(user.subscription_end, user)
        time_left_text = t(user, "subscription_left", time=time_left)

        sub_text = (
            t(user, "sub_link", link=user.sub_id)
            if user.sub_id else ""
        )

    text = (
        t(user, "profile", name=user.full_name) + "\n\n" +
        t(user, "telegram_id", id=user.telegram_id) + "\n\n" +
        sub_text +
        t(user, "subscription_status", status=status) + "\n\n" +
        time_left_text + "\n\n" +
        t(user, "subscription_end", date=expire_date) + "\n\n" +
        t(user, "menu_hint")
    )

    builder = InlineKeyboardBuilder()

    renew_text = (
        t(user, "btn_renew")
        if status == t(user, "active")
        else t(user, "btn_buy")
    )

    # 1 ряд — btn_connect
    builder.row(
        InlineKeyboardButton(
            text=t(user, "btn_connect"),
            callback_data="connect"
        )
    )

    # 2 ряд — renew_sub и btn_promo
    builder.row(
        InlineKeyboardButton(
            text=renew_text,
            callback_data="renew_sub"
        ),
        InlineKeyboardButton(
            text=t(user, "btn_promo"),
            callback_data="promo_code"
        )
    )

    # 3 ряд — только btn_referral
    builder.row(
        InlineKeyboardButton(
            text=t(user, "btn_referral"),
            callback_data="referral"
        )
    )

    # 4 ряд — btn_help и btn_settings
    builder.row(
        InlineKeyboardButton(
            text=t(user, "btn_help"),
            callback_data="help"
        ),
        InlineKeyboardButton(
            text=t(user, "btn_settings"),
            callback_data="settings"
        )
    )

    # 5 ряд — админ панель (если админ)
    if user.is_admin:
        builder.row(
            InlineKeyboardButton(
                text=t(user, "btn_admin"),
                callback_data="admin_menu"
            )
        )

    # 6 ряд — btn_support (ссылка)
    builder.row(
        InlineKeyboardButton(
            text=t(user, "btn_support"),
            url="https://t.me/shix_vpn"
        )
    )


    if message_id:
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=text,
                reply_markup=builder.as_markup(),
                parse_mode="Markdown"
            )
            return
        except TelegramBadRequest:
            pass

    # если message_id нет ИЛИ edit упал — шлём новое фото
    await bot.send_photo(
        chat_id=chat_id,
        photo=FSInputFile("assets/vpn_banner.jpg"),
        caption=text,
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# =========================================================
# /start
# =========================================================
@router.message(Command("start"))
async def start_cmd(message: Message, bot: Bot):
    telegram_id = message.from_user.id

    # 🔒 Проверка подписки
    if not await is_subscribed(bot, telegram_id):
        await send_subscribe_required(bot, message.chat.id)
        return

    logger.info(f"ℹ️ /start от {telegram_id}")

    # -------------------------------
    # Получаем реферера (если есть)
    # -------------------------------
    referrer_id = None
    if message.text and " " in message.text:
        arg = message.text.split(" ", 1)[1]
        if arg.isdigit():
            referrer_id = int(arg)

    # ===============================
    # Проверяем и создаём пользователя
    # ===============================
    user = await get_user(telegram_id)
    if not user:
        # UX: стикер + ожидание
        wait_sticker = await message.answer_sticker(
            "CAACAgEAAxkBAAFBQzppd5A_4Wk22T_jJFOGCrkkcV8ZLwACXA4AAoI0egEaqUfk_mnHQTgE"
        )
        wait_msg = await message.answer(TEXTS["ru"]["creating_profile"])

        # ✅ Даем пользователю 2 секунды увидеть стикер
        await asyncio.sleep(2)

        # Убираем ожидание
        await wait_msg.delete()
        await wait_sticker.delete()

        # создаём пользователя и профиль
        user = await ensure_user(
            bot,
            telegram_id,
            full_name=message.from_user.full_name,
            username=message.from_user.username,
            referrer_id=referrer_id
        )

        # -------------------------------
        # Welcome
        # -------------------------------
        msg = await bot.send_photo(
            chat_id=telegram_id,
            photo=FSInputFile("assets/vpn_banner.jpg"),
            caption=t(user, "welcome", bot_name=(await bot.get_me()).full_name),
            parse_mode="Markdown"
        )

        # 🔔 уведомляем админов
        await notify_admins_user_joined(bot, user)

        # -------------------------------
        # уведомляем реферера
        # -------------------------------
        if referrer_id:
            await bot.send_message(
                referrer_id,
                t(
                    user,
                    "referral_notify",
                    name=message.from_user.username or message.from_user.full_name,
                    id=telegram_id
                ),
                parse_mode="Markdown"
            )

        # Показываем меню с переданным message_id
        await show_menu(bot, chat_id=telegram_id, message_id=msg.message_id)
        return

    # =====================================================
    # Существующий пользователь — обновляем данные
    # =====================================================
    with Session() as session:
        db_user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not db_user:
            logger.error(f"❌ Пользователь не найден: {telegram_id}")
            return

        updated = False
        if db_user.full_name != message.from_user.full_name:
            db_user.full_name = message.from_user.full_name
            updated = True
        if db_user.username != message.from_user.username:
            db_user.username = message.from_user.username
            updated = True
        if updated:
            session.commit()
            logger.info(f"🔄 Обновлены данные пользователя {telegram_id}")

    # Показываем меню
    await show_menu(bot, telegram_id)


# ------------------------------
# Профиль
# ------------------------------
@router.message(Command("menu"))
async def menu_cmd(message: Message, bot: Bot):
    user = await get_user(message.from_user.id)
    if not user:
        await start_cmd(message, bot)
        return
    
    # Проверяем изменения данных
    update_data = {}
    if user.full_name != message.from_user.full_name:
        update_data["full_name"] = message.from_user.full_name
    if user.username != message.from_user.username:
        update_data["username"] = message.from_user.username
    
    # Обновляем данные если есть изменения
    if update_data:
        with Session() as session:
            db_user = session.query(User).get(user.id)
            for key, value in update_data.items():
                setattr(db_user, key, value)
            session.commit()
            logger.info(f"🔄  Данные пользователя обновлены в меню: {message.from_user.id}")
    
    await show_menu(bot, message.from_user.id)


# ------------------------------
# Настройки
# ------------------------------
@router.callback_query(F.data == "settings")
async def settings_cb(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.answer()

    builder = InlineKeyboardBuilder()

    builder.button(text=t(user, "lang_ru"), callback_data="set_lang:ru")
    builder.button(text=t(user, "lang_en"), callback_data="set_lang:en")
    builder.button(text=t(user, "lang_zh"), callback_data="set_lang:zh")
    builder.button(text=t(user, "back"), callback_data="back_to_menu")

    builder.adjust(1, 2, 1)

    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption=t(user, "choose_language"),
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# ------------------------------
# Сохранение языка
# ------------------------------
@router.callback_query(F.data.startswith("set_lang:"))
async def set_language(callback: CallbackQuery, bot: Bot):
    lang = callback.data.split(":")[1]

    with Session() as session:
        db_user = session.query(User).filter_by(
            telegram_id=callback.from_user.id
        ).first()

        if not db_user:
            await callback.answer()
            return

        db_user.language = lang
        session.commit()

    # 🔁 берём пользователя УЖЕ с новым языком
    user = await get_user(callback.from_user.id)

    await callback.answer(t(user, "settings_saved"))

    # ✅ РЕДАКТИРУЕМ текущее сообщение, а не шлём новое
    await show_menu(
        bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id
    )


# ------------------------------
# Помощь
# ------------------------------
@router.callback_query(F.data == "help")
async def help_msg(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer()
        return

    await callback.answer()

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=t(user, "help_channel"),
            url="https://t.me/+42MdJtX5B9I4ZDIy"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=t(user, "help_terms"),
            url="https://telegra.ph/Polzovatelskoe-soglashenie-01-28-69"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=t(user, "help_privacy"),
            url="https://telegra.ph/Politika-konfidencialnosti-01-28-101"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=t(user, "back"),
            callback_data="back_to_menu"
        )
    )

    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption=t(user, "help_text"),
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# ------------------------------
# Реферальная программа
# ------------------------------
@router.callback_query(F.data == "referral")
async def referral_program(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("🛑 Profile error")
        return

    await callback.answer()

    referral_link = f"https://t.me/MegaShixVPN_bot?start={user.telegram_id}"

    text = t(
        user,
        "referral_text",
        link=referral_link
    )

    builder = InlineKeyboardBuilder()
    builder.button(text=t(user, "back"), callback_data="back_to_menu")

    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption=text,
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# ------------------------------
# Помощь по подключению
# ------------------------------
@router.callback_query(F.data == "connect")
async def connect_profile(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer(t(None, "connect_no_profile"))
        return

    # Проверка подписки
    now = datetime.utcnow()
    if not user.subscription_end or user.subscription_end < now:
        await callback.answer(t(user, "connect_sub_expired"))
        return

    # Проверяем наличие ссылки
    sub_url = None
    if user.vless_profile_data:
        profile_data = safe_json_loads(user.vless_profile_data, default={})
        sub_url = profile_data.get("sub_url") or profile_data.get("subscriptionUrl")

    if not sub_url and user.sub_id:
        sub_url = f"https://sub.shix-vpn.space/{user.sub_id}"

    if not sub_url:
        await callback.answer(t(user, "connect_not_ready"))
        return

    await callback.answer()

    text = t(
        user,
        "connect_text",
        sub_url=sub_url
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text=t(user, "btn_connect_now"),
        web_app=WebAppInfo(url=sub_url)
    )
    builder.button(text=t(user, "back"), callback_data="back_to_menu")
    builder.adjust(1, 1)

    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption=text,
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# ------------------------------
# Продление подписки
# ------------------------------
@router.callback_query(F.data == "renew_sub")
async def renew_cb(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.answer()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(user, "tariff_1m"), callback_data="tariff_1m")],
        [InlineKeyboardButton(text=t(user, "tariff_3m"), callback_data="tariff_3m")],
        [InlineKeyboardButton(text=t(user, "tariff_6m"), callback_data="tariff_6m")],
        [InlineKeyboardButton(text=t(user, "tariff_1y"), callback_data="tariff_1y")],
        [InlineKeyboardButton(text=t(user, "tariff_2y"), callback_data="tariff_2y")],
        [InlineKeyboardButton(text=t(user, "back"), callback_data="back_to_menu")]
    ])

    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption=t(user, "renew_text"),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ------------------------------
# Ссылка для оплаты подписки
# ------------------------------
@router.callback_query(F.data.startswith("tariff_"))
async def tariff_selected(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.answer()

    tariff_key = callback.data
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        return

    price = tariff["price"]
    months = tariff["months"]
    total_amount = price * months

    payment_data = await create_platega_payment(total_amount)
    if not payment_data:
        await callback.answer(
            t(user, "payment_create_error"),
            show_alert=True
        )
        return

    # 🔥 СОХРАНЯЕМ ПЛАТЁЖ В БД
    await create_payment(
        user_id=user.id,
        transaction_id=payment_data["transaction_id"],
        amount=total_amount,
        months=months
    )

    title = t(user, tariff_key)

    text = (
        f"{t(user, 'pay_tariff', title=title)}\n"
        f"{t(user, 'pay_price', price=price)}\n"
        f"{t(user, 'pay_period', months=months)}\n\n"
        f"{t(user, 'pay_total', total=total_amount)}"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=t(user, "btn_pay"),
                web_app=WebAppInfo(url=payment_data["pay_url"])
            )
        ],
        [
            InlineKeyboardButton(
                text=t(user, "btn_check_payment"),
                callback_data=f"check_payment:{payment_data['transaction_id']}"
            )
        ],
        [
            InlineKeyboardButton(
                text=t(user, "back"),
                callback_data="renew_sub"
            )
        ]
    ])

    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption=text,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ------------------------------
# Проверка оплаты подписки 
# ------------------------------
@router.callback_query(F.data.startswith("check_payment:"))
async def check_payment(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    
    tx_id = callback.data.split(":", 1)[1]

    data = await get_platega_payment_status(tx_id)
    if not data:
        await callback.answer(
            t(user, "payment_check_error"),
            show_alert=True
        )
        return

    print("🔍 PLATEGA STATUS RESPONSE:", data)

    status = (
        data.get("status")
        or data.get("state")
        or data.get("paymentStatus")
    )

    if not status:
        await callback.answer(
            t(user, "payment_status_unknown"),
            show_alert=True
        )
        return

    result = await process_payment_result(
        transaction_id=tx_id,
        payment_status=status
    )

    # =========================================================
    # ТЕКСТ
    # =========================================================
    if result == "CONFIRMED":
        caption = t(user, "payment_success")

    elif result == "PENDING":
        caption = t(
            user,
            "payment_pending",
            time=now_local().strftime('%d.%m.%Y %H:%M:%S')
        )

    elif result == "CANCELED":
        caption = t(user, "payment_canceled")

    elif result == "NOT_FOUND":
        caption = t(user, "payment_not_found")

    else:
        caption = t(user, "payment_error")

    # =========================================================
    # КНОПКИ 
    # =========================================================
    builder = InlineKeyboardBuilder()

    # 🔄 Проверить ещё раз — ТОЛЬКО если PENDING
    if result == "PENDING":
        builder.row(
            InlineKeyboardButton(
                text=t(user, "btn_check_again"),
                callback_data=f"check_payment:{tx_id}"
            )

        )

    # Назад — всегда
    builder.row(
        InlineKeyboardButton(
            text=t(user, "back"),
            callback_data="renew_sub"
        )
    )

    # =========================================================
    # РЕДАКТИРУЕМ СООБЩЕНИЕ
    # =========================================================
    try:
        await callback.bot.edit_message_caption(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            caption=caption,
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    except TelegramBadRequest:
        # если Telegram сказал "message is not modified"
        pass

    await callback.answer()


# ------------------------------
# Обработчик кнопки "Промокод"
# ------------------------------
@router.callback_query(F.data == "promo_code")
async def promo_code_cb(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    await callback.answer()

    builder = InlineKeyboardBuilder()
    builder.button(text=t(user, "back"), callback_data="back_to_menu")

    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption=t(user, "promo_enter"),
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

    # сохраняем ID сообщения (оно НЕ меняется)
    await state.update_data(bot_message_id=callback.message.message_id)
    await state.set_state(PromoCodeStates.waiting_for_code)


# ------------------------------
# Обработчик ввода промокода
# ------------------------------
@router.message(PromoCodeStates.waiting_for_code)
async def enter_promo_code(message: Message, state: FSMContext, bot: Bot):
    user = await get_user(message.from_user.id)
    if not user:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    chat_id = message.chat.id
    user_message_id = message.message_id

    # ❌ удаляем сообщение пользователя
    try:
        await bot.delete_message(chat_id, user_message_id)
    except:
        pass

    code_input = message.text.strip().upper()
    result = await apply_promo_code(user.telegram_id, code_input)

    # ---------- ❌ ОШИБКИ ----------
    if "error" in result:
        builder = InlineKeyboardBuilder()
        builder.button(text=t(user, "back"), callback_data="back_to_menu")
        builder.adjust(1)

        error_map = {
            "promo_expired": "promo_expired",
            "promo_already_used": "promo_already_used",
            "promo_not_found": "promo_invalid",
        }

        text_key = error_map.get(result["error"], "promo_invalid")

        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=bot_message_id,
            caption=t(user, text_key),
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        return

    builder = InlineKeyboardBuilder()
    builder.button(text=t(user, "back"), callback_data="back_to_menu")
    builder.adjust(1)

    await bot.edit_message_caption(
        chat_id=chat_id,
        message_id=bot_message_id,
        caption=t(
            user,
            "promo_applied",
            code=result["code"],
            discount=result["discount_percent"]
        ),
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

    await state.clear()
























# ------------------------------
# Админ меню
# ------------------------------
@router.callback_query(F.data == "admin_menu")
async def admin_menu(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()

    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("🛑 Доступ запрещён")
        return

    await callback.answer()

    try:
        await bot.delete_message(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id
        )
    except Exception:
        pass

    # -------- статистика --------
    total, with_sub, without_sub = await db_user_stats()
    online_count = await get_online_users()
    offline_count = max(with_sub - online_count, 0)

    text = (
        "🔒 **Панель администратора**\n\n"

        "👥 **Пользователи**\n"
        f"├ Всего: `{total}`\n"
        f"├ С подпиской: `{with_sub}`\n"
        f"└ Без подписки: `{without_sub}`\n\n"

        "📡 **Активность**\n"
        f"├ 🟢 Онлайн: `{online_count}`\n"
        f"└ 🔴 Офлайн: `{offline_count}`\n\n"

        "_Выберите действие ниже 👇_"
    )

    builder = InlineKeyboardBuilder()

    builder.button(text="+ время", callback_data="admin_add_time")
    builder.button(text="- время", callback_data="admin_remove_time")

    builder.button(text="📋 Список пользователей", callback_data="admin_user_list")
    #builder.button(text="📊 Статистика сети", callback_data="admin_network_stats")
    builder.button(text="🔄 Обновить подписку", callback_data="admin_fix_subscription")

    builder.button(text="📢 Рассылка", callback_data="admin_send_message")

    builder.button(text="🎁 Создать промокод", callback_data="admin_create_promo")
    builder.button(text="📦 Список промокодов", callback_data="admin_promocodes")

    builder.button(text="Выйти", callback_data="exit_admin")

    builder.adjust(2, 1, 1, 1, 1, 1, 1, 1)

    await bot.send_message(
        chat_id=callback.from_user.id,
        text=text,
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# ------------------------------
# Кнопка создания промокода в админке
# ------------------------------
@router.callback_query(F.data == "admin_create_promo")
async def admin_create_promo_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()  # очищаем состояние

    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("🛑 Доступ запрещен!")
        return

    # удаляем старое сообщение меню
    try:
        await callback.message.delete()
    except:
        pass

    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data="admin_menu")
    kb.adjust(1)

    # отправляем новое сообщение, а не edit_text
    msg = await callback.message.answer(
        "🎁 Введите новый промокод (например: SUPERVPN2026):",
        reply_markup=kb.as_markup()
    )

    # сохраняем ID для удаления на следующем шаге
    await state.update_data(messages_to_delete=[msg.message_id])
    await state.set_state(AdminPromoStates.waiting_for_code)


# ------------------------------
# Ввод самого промокода
# ------------------------------
@router.message(AdminPromoStates.waiting_for_code)
async def enter_promo_code_admin(message: Message, state: FSMContext):
    code_input = message.text.strip().upper()
    await state.update_data(code=code_input)

    data = await state.get_data()
    messages_to_delete = data.get("messages_to_delete", [])

    # Добавляем сообщение пользователя (его ввод)
    messages_to_delete.append(message.message_id)

    # Удаляем все старые сообщения
    for msg_id in messages_to_delete:
        try:
            await message.bot.delete_message(message.chat.id, msg_id)
        except:
            pass

    # Сохраняем новый список для следующего шага
    messages_to_delete = []

    # Кнопки выбора скидки
    kb = InlineKeyboardBuilder()
    buttons = [InlineKeyboardButton(text=f"{p}%", callback_data=f"promo_discount_{p}") for p in range(5, 101, 5)]
    for i in range(0, len(buttons), 3):
        kb.row(*buttons[i:i+3])
    kb.row(InlineKeyboardButton(text="Назад", callback_data="admin_menu"))

    # Отправляем сообщение с кнопками
    msg = await message.answer(
        "💰 Выберите процент скидки:",
        reply_markup=kb.as_markup()
    )

    # Сохраняем новое сообщение бота
    messages_to_delete.append(msg.message_id)
    await state.update_data(messages_to_delete=messages_to_delete)

    # Переходим к следующему шагу
    await state.set_state(AdminPromoStates.waiting_for_discount)


# ------------------------------
# Ввод процента скидки
# ------------------------------
@router.callback_query(F.data.startswith("promo_discount_"))
async def promo_discount_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    discount = int(callback.data.split("_")[-1])
    await state.update_data(discount_percent=discount)

    # ✅ Удаляем именно сообщение с кнопками выбора процента
    try:
        await callback.message.delete()
    except:
        pass

    # Кнопки для следующего шага (ввод максимального количества использований)
    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data="admin_create_promo")
    kb.adjust(1)

    # Отправляем новое сообщение
    msg = await callback.message.answer(
        f"💰 Выбрана скидка: **{discount}%**\n\n"
        "🔢 Введите максимальное количество использований промокода:",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )

    # Сохраняем новое сообщение для следующего шага
    await state.update_data(messages_to_delete=[msg.message_id])

    await state.set_state(AdminPromoStates.waiting_for_max_uses)


# ------------------------------
# Ввод самого промокода
# ------------------------------
@router.message(AdminPromoStates.waiting_for_code)
async def enter_promo_code_admin(message: Message, state: FSMContext):
    code_input = message.text.strip().upper()
    await state.update_data(code=code_input)

    # Получаем список сообщений для удаления
    data = await state.get_data()
    messages_to_delete = data.get("messages_to_delete", [])

    # Добавляем сообщение пользователя (его текст)
    messages_to_delete.append(message.message_id)

    # Удаляем все старые сообщения
    for msg_id in messages_to_delete:
        try:
            await message.bot.delete_message(message.chat.id, msg_id)
        except:
            pass

    # Кнопки выбора скидки
    kb = InlineKeyboardBuilder()
    buttons = [InlineKeyboardButton(text=f"{p}%", callback_data=f"promo_discount_{p}") for p in range(5, 101, 5)]
    for i in range(0, len(buttons), 3):
        kb.row(*buttons[i:i+3])
    kb.row(InlineKeyboardButton(text="Назад", callback_data="admin_menu"))

    # Отправляем новое сообщение
    msg = await message.answer(
        "💰 Выберите процент скидки:",
        reply_markup=kb.as_markup()
    )

    # Сохраняем **только новое сообщение бота** для следующего шага
    await state.update_data(messages_to_delete=[msg.message_id])

    await state.set_state(AdminPromoStates.waiting_for_discount)


# ------------------------------
# Выбор процента скидки
# ------------------------------
@router.callback_query(F.data.startswith("promo_discount_"))
async def promo_discount_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    discount = int(callback.data.split("_")[-1])
    await state.update_data(discount_percent=discount)

    # Получаем список сообщений для удаления
    data = await state.get_data()
    messages_to_delete = data.get("messages_to_delete", [])

    # Добавляем текущее сообщение callback (с кнопкой) в список
    messages_to_delete.append(callback.message.message_id)

    # Удаляем все старые сообщения
    for msg_id in messages_to_delete:
        try:
            await callback.message.bot.delete_message(callback.message.chat.id, msg_id)
        except:
            pass

    # Кнопки для ввода максимального количества использований
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin_create_promo")
    kb.adjust(1)

    msg = await callback.message.answer(
        f"💰 Выбрана скидка: **{discount}%**\n\n"
        "🔢 Введите максимальное количество использований промокода:",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )

    # Сохраняем только новое сообщение бота
    await state.update_data(messages_to_delete=[msg.message_id])

    await state.set_state(AdminPromoStates.waiting_for_max_uses)


# ------------------------------
# Ввод максимального количества использований
# ------------------------------
@router.message(AdminPromoStates.waiting_for_max_uses)
async def enter_max_uses(message: Message, state: FSMContext):
    # --------- валидируем ввод ---------
    try:
        max_uses = int(message.text.strip())
        if max_uses <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            "❌ Введите корректное положительное число для максимальных использований:"
        )
        return

    data = await state.get_data()
    messages_to_delete = data.get("messages_to_delete", [])

    # сохраняем сообщение пользователя для удаления
    messages_to_delete.append(message.message_id)

    # удаляем старые сообщения
    for msg_id in messages_to_delete:
        try:
            await message.bot.delete_message(message.chat.id, msg_id)
        except Exception:
            pass

    # --------- берём данные из FSM ---------
    code: str = data["code"]
    discount_percent: int = data["discount_percent"]

    # --------- СОЗДАЁМ ПРОМОКОД ---------
    promo_data = await create_or_update_promo_code(
        code=code,
        discount_percent=discount_percent,
        max_uses=max_uses
    )

    # promo_data — это dict, НЕ ORM
    promo_code = promo_data["code"]
    promo_discount = promo_data["discount_percent"]
    promo_max_uses = promo_data["max_uses"]

    # --------- клавиатура ---------
    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Админ. меню", callback_data="admin_menu")
    kb.adjust(1)

    # --------- отправляем сообщение ---------
    await message.answer(
        "✅ **Промокод создан!**\n\n"
        f"🎁 Код: `{promo_code}`\n"
        f"💰 Скидка: `{promo_discount}%`\n"
        f"🔢 Максимум использований: `{promo_max_uses}`",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )

    # --------- очищаем FSM ---------
    await state.clear()


# ------------------------------
# Кнопка "Список промокодов" в админке
# ------------------------------
@router.callback_query(F.data == "admin_promocodes")
async def show_promocodes(callback: CallbackQuery, state: FSMContext):
    promos = await get_all_promocodes_list()
    if not promos:
        kb = InlineKeyboardBuilder()
        kb.button(text="Назад", callback_data="admin_menu")

        await callback.message.edit_text(
            "❌ Промокодов пока нет.",
            reply_markup=kb.as_markup()
        )
        return

    await state.update_data(promos=promos, index=0)
    await _edit_promocode_message(callback.message, state)


# ------------------------------
# Отображение промокода с кнопкой "Удалить"
# ------------------------------
async def _edit_promocode_message(message, state: FSMContext):
    data = await state.get_data()
    promos = data.get("promos", [])
    index = data.get("index", 0)

    if not promos:
        await message.edit_text("❌ Промокодов нет.")
        return

    promo = promos[index]

    remaining_uses = promo.max_uses - promo.used_count
    is_active = remaining_uses > 0

    status_text = (
        "<b>🟢 Активен</b>: Да" if is_active
        else "<b>🔴 Активен</b>: Нет"
    )

    text = (
        f"🎁 Промокод #{index + 1}\n\n"
        f"ℹ️ Информация:\n"
        f"├ 🔑 Код: `{promo.code}`\n"
        f"├ 💰 Скидка: `{promo.discount_percent}%`\n"
        f"├ 📊 Максимум использований: `{promo.max_uses}`\n"
        f"├ ✅ Использован: `{promo.used_count}` раз\n"
        f"├ 🔹 Осталось: `{remaining_uses}`\n"
        f"└ {status_text}"
    )

    builder = InlineKeyboardBuilder()

    # Навигация
    if index > 0:
        builder.button(text="⬅️", callback_data="promocode_prev")
    if index < len(promos) - 1:
        builder.button(text="➡️", callback_data="promocode_next")

    # Удалить
    builder.button(
        text="Удалить промокод",
        callback_data=f"promocode_delete_{promo.id}"
    )

    # Назад
    builder.button(text="Назад", callback_data="admin_menu")

    # Раскладка
    if index > 0 and index < len(promos) - 1:
        builder.adjust(2, 1, 1)
    elif index > 0 or index < len(promos) - 1:
        builder.adjust(1, 1, 1)
    else:
        builder.adjust(1, 1)

    await message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# ------------------------------
# Удаление промокода
# ------------------------------
@router.callback_query(F.data.startswith("promocode_delete_"))
async def promocode_delete(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Удаляю промокод...")

    promo_id = int(callback.data.split("_")[-1])

    data = await state.get_data()
    promos = data.get("promos", [])

    promo_to_delete = next((p for p in promos if p.id == promo_id), None)
    if not promo_to_delete:
        await callback.message.answer("❌ Промокод не найден.")
        return

    # Удаляем из БД
    await delete_promocode(promo_to_delete.code)

    # Удаляем из списка
    promos = [p for p in promos if p.id != promo_id]
    await state.update_data(promos=promos)

    if not promos:
        kb = InlineKeyboardBuilder()
        kb.button(text="Назад", callback_data="admin_menu")
        kb.adjust(1)
        await callback.message.edit_text(
            "❌ Промокодов больше нет.",
            reply_markup=kb.as_markup()
        )
        return

    # Обновляем индекс
    new_index = min(data.get("index", 0), len(promos) - 1)
    await state.update_data(index=new_index)

    await _edit_promocode_message(callback.message, state)


# ------------------------------
# Следующий промокод
# ------------------------------
@router.callback_query(F.data == "promocode_next")
async def promocode_next(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    index = data.get("index", 0)
    promos = data.get("promos", [])
    if index < len(promos) - 1:
        await state.update_data(index=index + 1)
        await _edit_promocode_message(callback.message, state)


# ------------------------------
# Предыдущий промокод
# ------------------------------
@router.callback_query(F.data == "promocode_prev")
async def promocode_prev(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    index = data.get("index", 0)
    if index > 0:
        await state.update_data(index=index - 1)
        await _edit_promocode_message(callback.message, state)


# ------------------------------
# Статистика сети
# ------------------------------
@router.callback_query(F.data == "stats")
async def user_stats(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.vless_profile_data:
        await callback.answer("⚠️ Профиль не создан")
        return
    await callback.message.edit_text("⚙️ Загружаем вашу статистику...")
    profile_data = safe_json_loads(user.vless_profile_data, default={})
    stats = await get_user_stats(profile_data["email"])

    logger.debug(stats)
    upload = f"{stats.get('upload', 0) / 1024 / 1024:.2f}"
    upload_size = 'MB' if int(float(upload)) < 1024 else 'GB'
    if upload_size == "GB":
        upload = f"{int(float(upload) / 1024):.2f}"

    download = f"{stats.get('download', 0) / 1024 / 1024:.2f}"
    download_size = 'MB' if int(float(download)) < 1024 else 'GB'
    if download_size == "GB":
        download = f"{int(float(download) / 1024):.2f}"

    await callback.message.delete()
    text = (
        "📊 **Ваша статистика:**\n\n"
        f"🔼 Загружено: `{upload} {upload_size}`\n"
        f"🔽 Скачано: `{download} {download_size}`\n"
    )
    await callback.message.answer(text, parse_mode='Markdown')


# ------------------------------
# Начало массового обновления подписки
# ------------------------------
@router.callback_query(F.data == "admin_fix_subscription")
async def admin_fix_subscription(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()

    # Кнопки выбора срока подписки
    builder = InlineKeyboardBuilder()
    periods = [1, 2, 5, 10, 20, 50, 100]
    for years in periods:
        builder.button(text=f"{years} год(а)", callback_data=f"fix_sub_all_{years}")
    
    builder.adjust(3)  # по 3 кнопки в ряд для годов

    # Кнопка назад — отдельный ряд
    builder.row(
        InlineKeyboardButton(text="Назад", callback_data="admin_menu")
    )

    await callback.message.edit_text(
        "Выберите срок подписки для всех пользователей:",
        reply_markup=builder.as_markup()
    )
    await state.set_state("FIX_SUB_ALL_PERIOD")


# ------------------------------
# Выбор периода для всех
# ------------------------------
@router.callback_query(F.data.startswith("fix_sub_all_"), F.state == "FIX_SUB_ALL_PERIOD")
async def fix_sub_all_period(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    years = int(callback.data.split("_")[-1])
    new_end = now_local() + timedelta(days=years*365)  # фиксируем на X лет

    with Session() as session:
        users = session.query(User).all()
        if not users:
            await callback.message.edit_text("❌ Пользователи не найдены")
            await state.clear()
            return

        updated_count = 0
        for user in users:
            # 🔁 SYNC с Remnawave
            ok = await sync_remnawave_expire(user.telegram_id, new_end)
            if ok:
                user.subscription_end = new_end
                updated_count += 1

        session.commit()

    await callback.message.edit_text(
        f"✅ Подписка обновлена для {updated_count} пользователей до {new_end.strftime('%d-%m-%Y %H:%M')}",
        parse_mode="Markdown"
    )

    await state.clear()




























# ------------------------------
# Обработчики для управления дабавления временем подписки
# ------------------------------
@router.callback_query(F.data == "admin_add_time")
async def admin_add_time_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    await callback.message.answer("Введите Telegram ID пользователя:")
    await state.set_state(AdminStates.ADD_TIME_USER)

@router.message(AdminStates.ADD_TIME_USER)
async def admin_add_time_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(user_id=user_id)
        await message.answer("Введите количество времени в формате:\nМесяцы Дни Часы Минуты\nПример: 1 0 0 0")
        await state.set_state(AdminStates.ADD_TIME_AMOUNT)
    except ValueError:
        await message.answer("Ошибка: ID должен быть числом")

@router.message(AdminStates.ADD_TIME_AMOUNT)
async def admin_add_time_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data["user_id"]

    try:
        months, days, hours, minutes = map(int, message.text.split())
    except ValueError:
        await message.answer("❌ Ошибка формата")
        return

    delta = timedelta(
        days=months * 30 + days,
        hours=hours,
        minutes=minutes
    )

    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await message.answer("❌ Пользователь не найден")
            return

        now = now_local()
        new_end = (
            user.subscription_end + delta
            if user.subscription_end and user.subscription_end > now
            else now + delta
        )

        # 🔁 SYNC WITH REMNAWAVE
        ok = await sync_remnawave_expire(user_id, new_end)
        if not ok:
            await message.answer("⚠️ Не удалось обновить Remnawave")
            return

        user.subscription_end = new_end
        session.commit()

    await message.answer(
        f"✅ Подписка обновлена до {new_end.strftime('%d-%m-%Y %H:%M')}"
    )
    await state.clear()


# ------------------------------
# Обработчики для управления удаления временем подписки
# ------------------------------
@router.callback_query(F.data == "admin_remove_time")
async def admin_remove_time_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    await callback.message.answer("Введите Telegram ID пользователя:")
    await state.set_state(AdminStates.REMOVE_TIME_USER)

@router.message(AdminStates.REMOVE_TIME_USER)
async def admin_remove_time_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(user_id=user_id)
        await message.answer("Введите количество времени в формате:\nМесяцы Дни Часы Минуты\nПример: 1 0 0 0")
        await state.set_state(AdminStates.REMOVE_TIME_AMOUNT)
    except ValueError:
        await message.answer("Ошибка: ID должен быть числом")

@router.message(AdminStates.REMOVE_TIME_AMOUNT)
async def admin_remove_time_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data["user_id"]

    try:
        months, days, hours, minutes = map(int, message.text.split())
    except ValueError:
        await message.answer("❌ Ошибка формата")
        return

    delta = timedelta(
        days=months * 30 + days,
        hours=hours,
        minutes=minutes
    )

    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user or not user.subscription_end:
            await message.answer("❌ Подписка не найдена")
            return

        new_end = max(now_local(), user.subscription_end - delta)

        # 🔁 SYNC WITH REMNAWAVE
        ok = await sync_remnawave_expire(user_id, new_end)
        if not ok:
            await message.answer("⚠️ Не удалось обновить Remnawave")
            return

        user.subscription_end = new_end
        session.commit()

    await message.answer(
        f"✅ Подписка сокращена до {new_end.strftime('%d-%m-%Y %H:%M')}"
    )
    await state.clear()













# ------------------------------
# Обработчики для вывода списка пользователей
# ------------------------------
@router.callback_query(F.data == "admin_user_list")
async def admin_user_list(callback: CallbackQuery):
    await callback.answer()

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ С подпиской", callback_data="user_list:active:1")
    builder.button(text="🛑 Без подписки", callback_data="user_list:inactive:1")
    builder.button(text="Назад", callback_data="admin_menu")
    builder.adjust(1)

    await callback.message.edit_text(
        "<b>📋 Список пользователей</b>\n\nВыберите фильтр:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("user_list:"))
async def user_list_paginated(callback: CallbackQuery):
    await callback.answer()

    _, list_type, page_str = callback.data.split(":")
    page = int(page_str)

    if list_type == "active":
        users = await get_all_users(with_subscription=True)
        title = "👑 <b>Пользователи с подпиской</b>"
    else:
        users = await get_all_users(with_subscription=False)
        title = "👤 <b>Пользователи без подписки</b>"

    if not users:
        await callback.message.edit_text(
            "❌ Пользователей нет",
            parse_mode="HTML"
        )
        return

    total = len(users)
    total_pages = (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE
    page = max(1, min(page, total_pages))

    start = (page - 1) * USERS_PER_PAGE
    end = start + USERS_PER_PAGE
    current_users = users[start:end]

    text = (
        f"{title}\n\n"
        f"📄 Страница {page} из {total_pages}\n\n"
    )

    for user in current_users:
        username = f"@{user.username}" if user.username else "—"
        line = f"• <b>{user.full_name}</b>\n"
        line += f"  ├ {username}\n"
        line += f"  └ <code>{user.telegram_id}</code>\n"

        if list_type == "active" and user.subscription_end:
            expire = user.subscription_end.strftime("%d.%m.%Y %H:%M")
            line += f"  ⏳ до <code>{expire}</code>\n"

        text += line + "\n"

    builder = InlineKeyboardBuilder()

    nav_buttons = []

    if page > 1:
        nav_buttons.append(
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"user_list:{list_type}:{page - 1}"
            )
        )

    if page < total_pages:
        nav_buttons.append(
            InlineKeyboardButton(
                text="Далее",
                callback_data=f"user_list:{list_type}:{page + 1}"
            )
        )

    # Строка навигации (если есть кнопки)
    if nav_buttons:
        builder.row(*nav_buttons)

    # ВСЕГДА отдельная строка
    builder.row(
        InlineKeyboardButton(
            text="К фильтрам",
            callback_data="admin_user_list"
        )
    )

    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )







# ------------------------------
# Обработчики для рассылки сообщений
# ------------------------------
@router.callback_query(F.data == "admin_send_message")
async def admin_send_message_start(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ С подпиской", callback_data="target_active")
    builder.button(text="🛑 Без подписки", callback_data="target_inactive")
    builder.button(text="👥 Всем пользователям", callback_data="target_all")
    builder.button(text="Назад", callback_data="admin_menu")
    builder.adjust(1)
    
    await callback.message.edit_text(
        "Выберите целевую аудиторию для рассылки:",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data == "back_to_targets")
async def back_to_targets(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()   # ← ВАЖНО

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ С подпиской", callback_data="target_active")
    builder.button(text="🛑 Без подписки", callback_data="target_inactive")
    builder.button(text="👥 Всем пользователям", callback_data="target_all")
    builder.button(text="⬅️ Назад", callback_data="admin_menu")
    builder.adjust(1)

    await callback.message.edit_text(
        "Выберите целевую аудиторию для рассылки:",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("target_"))
async def admin_send_message_target(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    target = callback.data.split("_")[1]
    await state.update_data(target=target)

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_targets")
    builder.adjust(1)

    await callback.message.edit_text(
        "✏️ Введите сообщение для рассылки:",
        reply_markup=builder.as_markup()
    )

    await state.set_state(AdminStates.SEND_MESSAGE)

@router.callback_query(F.data.startswith("show_blocked_users:"))
async def show_blocked_users(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    page = int(callback.data.split(":")[1])
    data = await state.get_data()
    blocked_users = data.get("blocked_users", [])

    if not blocked_users:
        await callback.message.edit_text("🚫 Заблокированных пользователей нет.")
        return

    PER_PAGE = 10
    total = len(blocked_users)
    total_pages = (total + PER_PAGE - 1) // PER_PAGE

    # защита от дурака
    page = max(1, min(page, total_pages))

    start = (page - 1) * PER_PAGE
    end = start + PER_PAGE
    current_users = blocked_users[start:end]

    text = (
        f"🚫 <b>Заблокировали бота:</b>\n"
        f"Страница {page}/{total_pages}\n\n"
    )

    for uid in current_users:
        text += f"• ID: <code>{uid}</code>\n"

    builder = InlineKeyboardBuilder()

    # ⬅️ Назад
    if page > 1:
        builder.button(
            text="Назад",
            callback_data=f"show_blocked_users:{page - 1}"
        )

    # ➡️ Далее
    if page < total_pages:
        builder.button(
            text="Далее",
            callback_data=f"show_blocked_users:{page + 1}"
        )

    builder.adjust(2)

    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup() if builder.buttons else None,
        parse_mode="HTML"
    )

@router.message(AdminStates.SEND_MESSAGE)
async def admin_send_message(message: Message, state: FSMContext, bot: Bot):
    if not message.text:
        await message.answer("❗ Пожалуйста, отправьте текстовое сообщение.")
        return

    data = await state.get_data()
    target = data['target']
    text = message.text

    if target == "active":
        users = await get_all_users(with_subscription=True)
    elif target == "inactive":
        users = await get_all_users(with_subscription=False)
    else:
        users = await get_all_users()

    logger.info(f"📨 Рассылка начата. Цель: {target}, пользователей: {len(users)}")

    success = 0
    failed = 0
    blocked_users = []

    for user in users:
        try:
            await bot.send_message(user.telegram_id, text)
            success += 1

        except TelegramForbiddenError:
            logger.info(f"🚫 Пользователь {user.telegram_id} заблокировал бота")
            blocked_users.append(user.telegram_id)
            failed += 1

        except TelegramBadRequest as e:
            logger.warning(f"⚠️ Ошибка запроса для пользователя {user.telegram_id}: {e.message}")
            failed += 1

        except TelegramRetryAfter as e:
            logger.warning(f"⏳ Превышен лимит Telegram, ожидание {e.retry_after} сек.")
            await asyncio.sleep(e.retry_after)
            await bot.send_message(user.telegram_id, text)
            success += 1

        except Exception as e:
            logger.error(f"❌ Неизвестная ошибка для пользователя {user.telegram_id}: {e}")
            failed += 1

    # сохраняем список в FSM, чтобы потом листать
    await state.update_data(blocked_users=blocked_users)

    builder = InlineKeyboardBuilder()

    if blocked_users:
        builder.button(
            text=f"🚫 Заблокировали бота ({len(blocked_users)})",
            callback_data="show_blocked_users:1"
        )

    builder.button(text="⚠️ Админ. меню", callback_data="admin_menu")

    builder.adjust(1)

    await message.answer(
        f"📨 Результаты рассылки:\n\n"
        f"• Успешно: {success}\n"
        f"• Не удалось: {failed}\n"
        f"• Всего: {len(users)}",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

    # FSM НЕ чистим, он нужен для списка


















































@router.callback_query(F.data == "exit_admin")
async def exit_admin(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await callback.answer()
    await state.clear()

    # 🗑 УДАЛЯЕМ админ-панель / любое текущее сообщение
    try:
        await bot.delete_message(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id
        )
    except Exception:
        pass

    # ПРИСЫЛАЕМ ГЛАВНОЕ МЕНЮ НОВЫМ СООБЩЕНИЕМ
    await show_menu(
        bot=bot,
        chat_id=callback.from_user.id
    )


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await callback.answer()
    await state.clear()

    await show_menu(
        bot=bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id
    )



def setup_handlers(dp: Dispatcher):
    dp.include_router(router)
    logger.info("✅  Обработчики успешно настроены")


def safe_json_loads(data, default=None):
    if not data:
        return default
    try:
        return json.loads(data)
    except Exception:
        return default
