import os
import logging
import json
import re
from database import now_local
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramBadRequest,
)

from aiogram.types import FSInputFile
from functions import create_vless_profile, get_user_stats, get_online_users, sync_remnawave_expire
from payment.platega_payment import get_platega_payment_status
from datetime import datetime, timedelta
from aiogram import Dispatcher, Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery, CallbackQuery, InlineKeyboardButton, WebAppInfo

from config import config
from locales import TEXTS, TARIFFS, TARIFFS_PEACHES, TARIFFS_STARS
from database import ( 
    get_user, create_user, apply_promo_code, create_or_update_promo_code, 
    get_all_promocodes_list, delete_promocode, sync_shortuuid_to_mysql,
    get_all_users, get_or_create_payment, process_payment_result,
    User, PromoCode, Payment, UserBalance, UserBalanceHistory, Session, get_user_stats as db_user_stats
)

from sync_mysql_to_rw import sync_all_users_to_rw
from sync_happ_to_mysql import sync_happ_to_mysql
from sync_remnawave_full import sync_remnawave_to_mysql_fixed
from sync_confirmed_payments import sync_confirmed_payments

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
    SEND_TO_IDS = State()
    COMPOSE_TEXT = State()
    FORMAT_TEXT = State()
    CONFIRM_SEND = State()

    PREVIEW = State()              # 📝 просмотр
    EDIT_MENU = State()            # кнопка "Редактировать"
    FIND_TEXT = State()            # ввод текста для поиска
    EDIT_FRAGMENT = State()        # форматирование фрагмента

    CHOOSE_TARGET = State()
    ENTER_IDS = State()
    ENTER_TEXT = State()

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

class ConvertStates(StatesGroup):
    WAIT_AMOUNT = State()
    CONFIRM = State()

class TopUpStars(StatesGroup):
    waiting_for_amount = State()

class TransferBalance(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_amount = State()

class WithdrawState(StatesGroup):
    choosing_type = State()
    choosing_payment = State()
    entering_card = State()
    entering_name = State()
    entering_amount = State()
    preview = State()
    decline_reason = State()

class ComplaintState(StatesGroup):
    write = State()
    preview = State()

class AdminDeclineStates(StatesGroup):
    WRITE_REASON = State()
    PREVIEW = State()
    EDIT_MENU = State()
    FIND_TEXT = State()
    EDIT_FRAGMENT = State()


USERS_PER_PAGE = 7

blocked_users = []

STAR_TO_RUB_RATE = 1.81


async def get_payment_by_tx(transaction_id: str) -> Payment | None:
    with Session() as session:
        return session.query(Payment).filter_by(transaction_id=transaction_id).first()
    
async def get_user_balance(user_id: int):
    with Session() as session:
        balance = session.query(UserBalance).filter_by(user_id=user_id).first()
        if not balance:
            return 0, 0
        return balance.amount, balance.stars
    
async def safe_edit_caption(
    bot,
    chat_id: int,
    message_id: int,
    caption: str,
    reply_markup=None,
    parse_mode: str = "Markdown"
):
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=caption,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )

    except TelegramBadRequest as e:
        error_text = str(e)

        if "message is not modified" in error_text:
            return

        if "message to edit not found" in error_text:
            return

        if "message can't be edited" in error_text:
            return

        raise



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


def admin_user_button(user_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Написать пользователю",
                    url=f"tg://user?id={user_id}"
                )
            ]
        ]
    )


def admin_channel_joined_text(user) -> str:
    username = f"@{user.username}" if user.username else "Без имени"
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
                parse_mode="HTML",
                reply_markup=admin_user_button(user.telegram_id)
            )
        except TelegramForbiddenError:
            pass
        except Exception as e:
            logger.warning(f"Ошибка уведомления админа {admin_id}: {e}")


@router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, bot: Bot, state: FSMContext):
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

    data = await state.get_data()
    referrer_telegram_id = data.get("referrer_telegram_id")

    # Создаём или получаем пользователя с правильными данными
    user = await ensure_user(
        bot,
        telegram_id,
        full_name=full_name,
        username=username,
        referrer_telegram_id=referrer_telegram_id
    )

    # Показываем меню
    await state.clear()
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

    # Всего дней
    total_days = total_seconds // 86400

    # Рассчитываем годы, месяцы, дни
    years = total_days // 365
    months = (total_days % 365) // 30
    days = (total_days % 365) % 30

    # Часы, минуты, секунды
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    # Переводы
    y = t(user, "time_year")      # "г."
    mo = t(user, "time_month")    # "мес."
    d = t(user, "time_day")       # "дн."
    h = t(user, "time_hour")      # "ч."
    m = t(user, "time_min")       # "мин."
    s = t(user, "time_sec")       # "сек."

    parts = []

    if years > 0:
        # Гарантированно показываем годы
        parts.append(f"{years} {y}")
        # Показываем месяцы даже если 0
        parts.append(f"{months} {mo}")
    elif months > 0:
        parts.append(f"{months} {mo}")
        parts.append(f"{days} {d}")
    elif days > 0:
        parts.append(f"{days} {d}")
        parts.append(f"{hours} {h}")
    elif hours > 0:
        parts.append(f"{hours} {h}")
        parts.append(f"{minutes} {m}")
    elif minutes > 0:
        parts.append(f"{minutes} {m}")
        parts.append(f"{seconds} {s}")
    else:
        parts.append(f"{seconds} {s}")

    return " ".join(parts)


async def ensure_user(
    bot: Bot,
    telegram_id: int,
    full_name: str = "",
    username: str | None = None,
    referrer_telegram_id: int | None = None
) -> User:

    user = await get_user(telegram_id)
    if user:
        return user

    if not isinstance(full_name, str):
        logger.error(f"❌ full_name не строка: {type(full_name)} | {full_name}")
        full_name = ""

    # ✅ СОЗДАЁМ ТОЛЬКО ОДИН РАЗ
    user = await create_user(
        telegram_id=telegram_id,
        full_name=full_name,
        username=username,
        is_admin=telegram_id in config.ADMINS,
        referrer_telegram_id=referrer_telegram_id,
        language="ru"
    )

    user = await get_user(telegram_id)

    # =========================
    # 🔔 Админы
    # =========================
    await notify_admins_user_joined(bot, user)

    # =========================
    # 👥 Рефералка
    # =========================
    if referrer_telegram_id and referrer_telegram_id != telegram_id:
        with Session() as session:
            referrer = session.query(User).filter_by(
                telegram_id=referrer_telegram_id
            ).first()

            if referrer:
                await bot.send_message(
                    referrer_telegram_id,
                    t(
                        user,
                        "referral_notify",
                        name=username or full_name,
                        id=telegram_id
                    ),
                    parse_mode="Markdown"
                )

                logger.info(f"🔹 Реферал {telegram_id} сохранён за {referrer_telegram_id}")

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

        sub_link = (
            f"https://sub.shix-vpn.space/{user.sub_id}"
            if user.sub_id else ""
        )
        sub_text = t(user, "sub_link", link=sub_link) if sub_link else ""

    amount, stars = await get_user_balance(user.id)

    text = (
        t(user, "profile", name=user.full_name) + "\n\n" +
        t(user, "telegram_id", id=user.telegram_id) + "\n\n" +
        t(user, "balance", amount=amount, stars=stars) + "\n\n" +
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

    # 1 ряд — btn_connect и renew_sub
    builder.row(
        InlineKeyboardButton(
            text=t(user, "btn_connect"),
            callback_data="connect"
        ),
        InlineKeyboardButton(
            text=renew_text,
            callback_data="renew_sub"
        ),
    )

    # 2 ряд — btn_topup и btn_promo
    builder.row(
        InlineKeyboardButton(
            text=t(user, "btn_topup"),
            callback_data="topup_balance"
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
async def start_cmd(message: Message, bot: Bot, state: FSMContext):
    telegram_id = message.from_user.id
    full_name = message.from_user.full_name
    username = message.from_user.username

    # 🔹 Получаем реферера через deep link (ПЕРЕНЕСЕНО ВЫШЕ)
    referrer_telegram_id = None
    if message.text:
        import re
        m = re.match(r"^/start\s*(\d+)?", message.text)
        if m and m.group(1):
            referrer_telegram_id = int(m.group(1))
            if referrer_telegram_id == telegram_id:
                referrer_telegram_id = None

    # ✅ Сохраняем ДО проверки подписки (КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ)
    await state.update_data(referrer_telegram_id=referrer_telegram_id)

    # Проверка подписки (ОСТАВИЛ КАК ЕСТЬ)
    if not await is_subscribed(bot, telegram_id):
        await send_subscribe_required(bot, message.chat.id)
        return

    # Проверяем пользователя
    user = await get_user(telegram_id)
    if not user:
        # Создаём пользователя с referrer
        user = await ensure_user(
            bot,
            telegram_id,
            full_name=full_name,
            username=username,
            referrer_telegram_id=referrer_telegram_id
        )

        # Отправляем welcome + меню
        msg = await bot.send_photo(
            chat_id=telegram_id,
            photo=FSInputFile("assets/vpn_banner.jpg"),
            caption=t(user, "welcome", bot_name=(await bot.get_me()).full_name),
            parse_mode="Markdown"
        )
        await show_menu(bot, chat_id=telegram_id, message_id=msg.message_id)
        return

    # Обновляем данные существующего пользователя
    updated = False
    with Session() as session:
        db_user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if db_user.full_name != full_name:
            db_user.full_name = full_name
            updated = True
        if db_user.username != username:
            db_user.username = username
            updated = True
        if updated:
            session.commit()

    # Показываем меню
    await show_menu(bot, chat_id=telegram_id)


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
# Обновить подписки и профили 
# ------------------------------
@router.message(Command("reload"))
async def reload_all(message: Message):
    if message.from_user.id not in config.ADMINS:
        return

    msg = await message.answer("⏳ Синхронизация с Remnawave...")

    success, skipped, failed = await sync_shortuuid_to_mysql()

    text = (
        f"🔄 Синхронизация завершена\n\n"
        f"✅ Обновлено: {success}\n"
        f"⚠️ Пропущено: {skipped}\n"
        f"❌ Ошибки: {failed}"
    )

    await msg.edit_text(text)

@router.message(Command("sync_rw"))
async def sync_rw_handler(message: Message):
    if message.from_user.id not in config.ADMINS:
        return

    msg = await message.answer("⏳ Синхронизация MYSQL → Remnawave...")

    try:
        await sync_all_users_to_rw()

        await msg.edit_text(
            "✅ Синхронизация завершена\n\n"
            "Пользователи из MYSQL добавлены в Remnawave"
        )

    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

@router.message(Command("reload_happ"))
async def reload_happ(message: Message):
    if message.from_user.id not in config.ADMINS:
        return

    msg = await message.answer("⏳ Обновляю happ ссылки...")

    success, skipped, failed = await sync_happ_to_mysql()

    await msg.edit_text(
        f"✅ Готово\n\n"
        f"Обновлено: {success}\n"
        f"Пропущено: {skipped}\n"
        f"Ошибки: {failed}"
    )

@router.message(Command("reload_rw"))
async def reload_rw(message: Message):
    if message.from_user.id not in config.ADMINS:
        return

    msg = await message.answer("⏳ Синхронизация RW...")

    success, skipped, failed = await sync_remnawave_to_mysql_fixed()

    await msg.edit_text(
        f"✅ Готово\n\n"
        f"Обновлено: {success}\n"
        f"Пропущено: {skipped}\n"
        f"Ошибки: {failed}"
    )


@router.message(Command("sync_payments"))
async def sync_payments_cmd(message: Message):
    if message.from_user.id not in config.ADMINS:
        return

    msg = await message.answer("⏳ Синхронизирую платежи...")

    success, skipped, failed = await sync_confirmed_payments()

    await msg.edit_text(
        f"✅ Готово\n\n"
        f"Обновлено: {success}\n"
        f"Пропущено: {skipped}\n"
        f"Ошибки: {failed}"
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

    with Session() as session:
        referrals_count = session.query(User).filter(
            User.referrer_id == user.id
        ).count()

    referral_link = f"https://t.me/MegaShixVPN_bot?start={user.telegram_id}"

    text = t(
        user,
        "referral_text",
        link=referral_link,
        count=referrals_count
    )

    builder = InlineKeyboardBuilder()
    builder.button(text=t(user, "back"), callback_data="back_to_menu")

    await callback.answer()

    await callback.message.edit_caption(
        caption=text,
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    

# ------------------------------
# Помощь по подключению
# ------------------------------
BASE_SUB_URL = "https://sub.shix-vpn.space"


def build_sub_url(sub_id: str) -> str:
    if not sub_id:
        return None
    return f"{BASE_SUB_URL.rstrip('/')}/{sub_id.lstrip('/')}"


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

    # ------------------------------
    # Получаем ссылку
    # ------------------------------
    sub_url = None

    if user.vless_profile_data:
        profile_data = safe_json_loads(user.vless_profile_data, default={})
        sub_url = profile_data.get("sub_url") or profile_data.get("subscriptionUrl")

    # если ссылки нет — строим из sub_id
    if not sub_url and user.sub_id:
        sub_url = build_sub_url(user.sub_id)

    # если всё ещё пусто — ошибка
    if not sub_url:
        await callback.answer(t(user, "connect_not_ready"))
        return

    # ------------------------------
    # ВАЖНО: Telegram требует валидный URL
    # ------------------------------
    if not sub_url.startswith("http://") and not sub_url.startswith("https://"):
        sub_url = build_sub_url(sub_url)

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
    builder.button(
        text=t(user, "back"),
        callback_data="back_to_menu"
    )
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
        [InlineKeyboardButton(text=t(user, "pay_sbp"), callback_data="pay_sbp")],
        [InlineKeyboardButton(text=t(user, "pay_crypto"), callback_data="pay_crypto")],
        [InlineKeyboardButton(text=t(user, "pay_stars"), callback_data="pay_stars")],
        [InlineKeyboardButton(text=t(user, "pay_peaches"), callback_data="pay_peaches")],
        [InlineKeyboardButton(text=t(user, "back"), callback_data="back_to_menu")]
    ])

    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption=t(user, "renew_text"),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@router.callback_query(F.data.in_(["pay_crypto"]))
async def pay_other_cb(callback: CallbackQuery):
    await callback.answer("🚧 Этот способ оплаты скоро будет доступен", show_alert=True)


@router.callback_query(F.data == "pay_sbp")
async def pay_sbp_cb(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.answer()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(user, "tariff_1m"), callback_data="tariff_1m")],
        [InlineKeyboardButton(text=t(user, "tariff_3m"), callback_data="tariff_3m")],
        [InlineKeyboardButton(text=t(user, "tariff_6m"), callback_data="tariff_6m")],
        [InlineKeyboardButton(text=t(user, "tariff_1y"), callback_data="tariff_1y")],
        [InlineKeyboardButton(text=t(user, "tariff_2y"), callback_data="tariff_2y")],
        [InlineKeyboardButton(text=t(user, "back"), callback_data="renew_sub")]
    ])

    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption=t(user, "renew_sbp_text"),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "pay_peaches")
async def pay_peaches_cb(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.answer()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(user, "peach_tariff_1m"), callback_data="peach_tariff_1m")],
        [InlineKeyboardButton(text=t(user, "peach_tariff_3m"), callback_data="peach_tariff_3m")],
        [InlineKeyboardButton(text=t(user, "peach_tariff_6m"), callback_data="peach_tariff_6m")],
        [InlineKeyboardButton(text=t(user, "peach_tariff_1y"), callback_data="peach_tariff_1y")],
        [InlineKeyboardButton(text=t(user, "peach_tariff_2y"), callback_data="peach_tariff_2y")],
        [InlineKeyboardButton(text=t(user, "back"), callback_data="renew_sub")]
    ])

    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption="🍑 Оплата персиками\n\nВыберите тариф 👇",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "pay_stars")
async def pay_stars_cb(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.answer()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(user, "stars_tariff_1m"), callback_data="stars_tariff_1m")],
        [InlineKeyboardButton(text=t(user, "stars_tariff_3m"), callback_data="stars_tariff_3m")],
        [InlineKeyboardButton(text=t(user, "stars_tariff_6m"), callback_data="stars_tariff_6m")],
        [InlineKeyboardButton(text=t(user, "stars_tariff_1y"), callback_data="stars_tariff_1y")],
        [InlineKeyboardButton(text=t(user, "stars_tariff_2y"), callback_data="stars_tariff_2y")],
        [InlineKeyboardButton(text=t(user, "back"), callback_data="renew_sub")]
    ])

    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption="⭐ Оплата Telegram Stars\n\nВыберите тариф 👇",
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

    try:
        # 🔥 Используем функцию get_or_create_payment
        payment = await get_or_create_payment(user.id, total_amount, months)
    except Exception:
        await callback.answer(
            t(user, "payment_create_error"),
            show_alert=True
        )
        return

    title = t(user, tariff_key)

    text = (
        f"{t(user, 'pay_tariff', title=title)}\n"
        f"{t(user, 'pay_price', total=total_amount)}\n"
        f"{t(user, 'pay_period', months=months)}\n\n"
        f"{t(user, 'pay_total', total=total_amount)}"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=t(user, "btn_pay"),
                url=payment.pay_url
            )
        ],
        [
            InlineKeyboardButton(
                text=t(user, "btn_check_payment"),
                callback_data=f"check_payment:{payment.transaction_id}"
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


@router.callback_query(F.data.startswith(("peach_tariff_", "stars_tariff_")))
async def balance_tariff_preview(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.answer()

    # Определяем тип валюты
    is_peach = callback.data.startswith("peach_")
    tariff_type_key = callback.data  # полный ключ, например "peach_tariff_1m" или "stars_tariff_1m"

    # Берём тариф из словаря
    tariff_key_only = tariff_type_key.split("_", 1)[1]  # "tariff_1m" и т.д.
    tariff = TARIFFS_PEACHES.get(tariff_key_only) if is_peach else TARIFFS_STARS.get(tariff_key_only)
    if not tariff:
        return

    price = tariff["price"]
    months = tariff["months"]
    total = price * months

    # Баланс пользователя
    peaches, stars = await get_user_balance(user.id)

    # Проверка, хватает ли средств
    if is_peach and peaches < total:
        await safe_edit_caption(
            bot=callback.bot,
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            caption=t(user, "error_not_enough_peaches").format(balance=peaches)
        )
        return

    if not is_peach and stars < total:
        await safe_edit_caption(
            bot=callback.bot,
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            caption=t(user, "error_not_enough_stars").format(balance=stars)
        )
        return

    currency = "🍑 Персики" if is_peach else "⭐ Telegram Stars"
    balance = peaches if is_peach else stars

    tariff_text = t(user, tariff_type_key)  

    text = t(user, "balance_tariff_preview_text", 
            tariff=tariff_text,
            currency=currency,
            price=price,
            months=months,
            total=total,
            balance=balance)

    # Кнопки: подтвердить и назад
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=t(user, "confirm"),
                callback_data=f"confirm_{'peach' if is_peach else 'stars'}:{tariff_key_only}"
            )
        ],
        [
            InlineKeyboardButton(
                text=t(user, "back"),
                callback_data="renew_sub"  # возвращаем на шаг выбора оплаты
            )
        ]
    ])

    # Обновляем сообщение
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

    payment = await get_payment_by_tx(tx_id)
    if not payment:
        await callback.answer(t(user, "payment_not_found"), show_alert=True)
        return

    now = now_local()

    # Если PENDING и прошло >30 мин — считаем отменённым
    if payment.status == "PENDING" and payment.pay_url_created_at + timedelta(minutes=30) < now:
        with Session() as session:
            db_payment = session.query(Payment).filter_by(id=payment.id).first()
            db_payment.status = "CANCELED"
            session.commit()
        payment.status = "CANCELED"

    # Если PENDING и ссылка ещё действительна — проверяем через Platega
    result = payment.status
    if payment.status == "PENDING":
        data = await get_platega_payment_status(tx_id)
        if data:
            status = data.get("status") or data.get("state") or data.get("paymentStatus")
            if status:
                result = await process_payment_result(tx_id, status)

    # =========================================================
    # ТЕКСТ ДЛЯ ПОЛЬЗОВАТЕЛЯ
    # =========================================================
    if result == "CONFIRMED":
        caption = t(user, "payment_success")

        username = callback.from_user.username
        user_id = callback.from_user.id

        if username:
            support_text = (
                "💰 Новая оплата\n\n"
                f"👤 Username: @{username}\n"
                f"🆔 ID: {user_id}\n"
                f"💳 TX: {tx_id}"
            )
        else:
            support_text = (
                "💰 Новая оплата\n\n"
                f"👤 ID: {user_id}\n"
                f"💳 TX: {tx_id}"
            )

        kb = None
        if username:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💬 Написать пользователю",
                            url=f"https://t.me/{username}"
                        )
                    ]
                ]
            )

        for admin_id in config.ADMINS:
            try:
                await callback.bot.send_message(
                    admin_id,
                    support_text,
                    reply_markup=kb
                )
            except Exception:
                try:
                    await callback.bot.send_message(
                        admin_id,
                        support_text
                    )
                except Exception as e2:
                    print(f"❌ Ошибка отправки админу {admin_id}: {e2}")
    elif result == "PENDING":
        caption = t(user, "payment_pending", time=now.strftime('%d.%m.%Y %H:%M:%S'))
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

    # Показать кнопку "Оплатить" только если PENDING и ссылка <30 мин
    if result == "PENDING" and payment.pay_url and payment.pay_url_created_at + timedelta(minutes=30) > now:
        builder.row(
            InlineKeyboardButton(
                text=t(user, "btn_pay"),
                url=payment.pay_url
            )
        )

    # Кнопка "Проверить ещё раз" — только для PENDING
    if result == "PENDING":
        builder.row(
            InlineKeyboardButton(
                text=t(user, "btn_check_again"),
                callback_data=f"check_payment:{payment.transaction_id}"
            )
        )

    # Назад — всегда
    builder.row(
        InlineKeyboardButton(
            text=t(user, "back"),
            callback_data="renew_sub"
        )
    )

    try:
        await callback.bot.edit_message_caption(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            caption=caption,
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


@router.callback_query(F.data.startswith(("confirm_peach:", "confirm_stars:")))
async def confirm_balance_payment(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    await callback.answer()

    is_peach = callback.data.startswith("confirm_peach:")
    tariff_key = callback.data.split(":", 1)[1]

    # Получаем тариф
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        return

    price = tariff["price"]
    months = tariff["months"]
    total = price * months

    # -------------------------
    # Списание баланса
    # -------------------------
    with Session() as session:
        balance = session.query(UserBalance).filter_by(user_id=user.id).first()

        if is_peach:
            if balance.amount < total:
                await safe_edit_caption(
                    bot=callback.bot,
                    chat_id=callback.from_user.id,
                    message_id=callback.message.message_id,
                    caption=t(user, "error_not_enough_peaches").format(balance=balance.amount)
                )
                return
            balance.amount -= total
            history = UserBalanceHistory(
                user_id=user.id,
                change=-total,
                reason="Списание за подписку (персики)"
            )
        else:
            if balance.stars < total:
                await safe_edit_caption(
                    bot=callback.bot,
                    chat_id=callback.from_user.id,
                    message_id=callback.message.message_id,
                    caption=t(user, "error_not_enough_stars").format(balance=balance.stars)
                )
                return
            balance.stars -= total
            history = UserBalanceHistory(
                user_id=user.id,
                stars_change=-total,
                reason="Списание за подписку (Stars)"
            )

        session.add(history)
        session.commit()

    # -------------------------
    # Продление подписки и синхронизация Remnawave
    # -------------------------

    # Получаем свежего пользователя из базы
    with Session() as session:
        user = session.query(User).filter_by(id=user.id).first()

    if not user.vless_profile_id:
        # Создаём профиль, если ещё нет
        profile = await create_vless_profile(user.telegram_id)
        if profile:
            with Session() as session:
                db_user = session.query(User).filter_by(id=user.id).first()
                db_user.vless_profile_id = profile.get("uuid")
                db_user.vless_profile_data = str(profile)
                session.commit()
    else:
        # Обновляем дату окончания в Remnawave
        await sync_remnawave_expire(user.telegram_id, user.subscription_end)

    # -------------------------
    # Редактируем сообщение
    # -------------------------
    await callback.bot.edit_message_caption(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        caption=t(user, "payment_success"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t(user, "btn_connect"), callback_data="connect")],
            [InlineKeyboardButton(text=t(user, "back"), callback_data="renew_sub")]
        ]),
        parse_mode="Markdown"
    )


# ------------------------------
# Пополнение баланса
# ------------------------------
@router.callback_query(F.data == "topup_balance")
async def topup_balance_handler(call: CallbackQuery, state: FSMContext):
    user = await get_user(call.from_user.id)
    if not user:
        return

    await state.clear()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(user, "btn_topup_stars"), callback_data="topup_stars")],
            [InlineKeyboardButton(text="💸 Вывод", callback_data="withdraw_balance")],
            [InlineKeyboardButton(text=t(user, "btn_transfer_balance"), callback_data="transfer_balance")],
            [InlineKeyboardButton(text=t(user, "btn_convert"), callback_data="convert_peaches")],
            [InlineKeyboardButton(text=t(user, "back"), callback_data="back_to_menu")]
        ]
    )

    amount, stars = await get_user_balance(user.id)

    text = (
        f"{t(user, 'balance_title')}\n\n"
        f"{t(user, 'balance_peaches', amount=amount)}\n"
        f"{t(user, 'balance_stars', stars=stars)}\n\n"
        f"{t(user, 'balance_hint')}"
    )

    await call.message.edit_caption(
        caption=text,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

    await call.answer()


# ------------------------------
# Пополнение звёздами
# ------------------------------
@router.callback_query(F.data == "topup_stars")
async def topup_stars_handler(call: CallbackQuery, state: FSMContext):
    user = await get_user(call.from_user.id)
    if not user:
        return

    await state.set_state(TopUpStars.waiting_for_amount)

    # сохраняем ID сообщения с caption
    await state.update_data(caption_message_id=call.message.message_id)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(user, "back"), callback_data="topup_balance")]
        ]
    )

    await call.message.edit_caption(
        caption=t(user, "topup_stars_text"),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

    await call.answer()


# ------------------------------
# Создание счета
# ------------------------------
@router.message(TopUpStars.waiting_for_amount)
async def process_stars_amount(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        return

    data = await state.get_data()
    caption_message_id = data.get("caption_message_id")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(user, "back"), callback_data="topup_balance")]
        ]
    )

    # ❌ не число
    if not message.text.isdigit():
        await safe_edit_caption (
            chat_id=message.chat.id,
            message_id=caption_message_id,
            caption=t(user, "error_only_number"),
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        await message.delete()
        return

    amount = int(message.text)

    # ❌ меньше или равно 0
    if amount <= 0:
        await safe_edit_caption (
            chat_id=message.chat.id,
            message_id=caption_message_id,
            caption=t(user, "error_positive"),
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        await message.delete()
        return

    await state.clear()
    await message.delete()

    prices = [LabeledPrice(
        label=t(user, "invoice_label", amount=amount),
        amount=amount
    )]

    await message.bot.edit_message_caption(
        chat_id=message.chat.id,
        message_id=caption_message_id,
        caption=t(user, "topup_invoice_created", amount=amount),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

    # ⚠️ Инвойс ВСЕГДА отдельным сообщением
    await message.answer_invoice(
        title=t(user, "invoice_title"),
        description=t(user, "invoice_description", amount=amount),
        payload=f"topup_stars:{user.id}:{amount}",
        provider_token="",
        currency="XTR",
        prices=prices
    )


# ------------------------------
# Подтверждение платежа
# ------------------------------
@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


# ------------------------------
# Успешная оплата
# ------------------------------
@router.message(F.successful_payment)
async def successful_stars_payment(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    user = await get_user(message.from_user.id)

    if not user or not payload.startswith("topup_stars"):
        return

    _, user_id, amount = payload.split(":")
    amount = int(amount)

    with Session() as session:
        balance = session.query(UserBalance).filter_by(user_id=user.id).first()
        if balance:
            balance.stars += amount

            session.add(UserBalanceHistory(
                user_id=user.id,
                change=0,
                stars_change=amount,
                reason=t(user, "history_topup_stars")
            ))

            session.commit()

    await message.answer(
        t(user, "topup_success", amount=amount),
        parse_mode="Markdown"
    )

# ------------------------------
# Перевод баланса
# ------------------------------
@router.callback_query(F.data == "transfer_balance")
async def transfer_balance_start(call: CallbackQuery, state: FSMContext):
    user = await get_user(call.from_user.id)
    if not user:
        return

    await state.clear()

    await call.message.edit_caption(
        caption=t(user, "transfer_intro"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=t(user, "btn_transfer_by_id"),
                    callback_data="transfer_by_id"
                )],
                [InlineKeyboardButton(
                    text=t(user, "back"),
                    callback_data="topup_balance"
                )]
            ]
        )
    )

    await call.answer()


# ------------------------------
# Перевод по ID
# ------------------------------
@router.callback_query(F.data == "transfer_by_id")
async def transfer_by_id(call: CallbackQuery, state: FSMContext):
    user = await get_user(call.from_user.id)
    if not user:
        return

    await state.set_state(TransferBalance.waiting_for_user_id)
    await state.update_data(caption_message_id=call.message.message_id)

    await call.message.edit_caption(
        caption=t(user, "transfer_enter_id"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=t(user, "back"),
                    callback_data="transfer_balance"
                )]
            ]
        )
    )

    await call.answer()


# ------------------------------
# Получение ID для перевода баланса
# ------------------------------
@router.message(TransferBalance.waiting_for_user_id)
async def transfer_get_user(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        return

    data = await state.get_data()
    caption_message_id = data.get("caption_message_id")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(user, "back"), callback_data="topup_balance")]
        ]
    )

    if not message.text.isdigit():
        await message.delete()
        await safe_edit_caption(
            bot=message.bot,
            chat_id=message.chat.id,
            message_id=caption_message_id,
            caption=t(user, "error_invalid_id"),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    target_telegram_id = int(message.text)

    with Session() as session:
        target_user = session.query(User).filter_by(
            telegram_id=target_telegram_id
        ).first()

    await message.delete()

    if not target_user:
        await safe_edit_caption(
            bot=message.bot,
            chat_id=message.chat.id,
            message_id=caption_message_id,
            caption=t(user, "error_user_not_found"),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    await state.update_data(target_user_id=target_user.id)
    await state.set_state(TransferBalance.waiting_for_amount)

    sender = await get_user(message.from_user.id)
    amount, _ = await get_user_balance(sender.id)

    await message.bot.edit_message_caption(
        chat_id=message.chat.id,
        message_id=caption_message_id,
        caption=t(
            user,
            "transfer_enter_amount",
            id=target_telegram_id,
            balance=amount
        ),
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# ------------------------------
# Перевод баланса пользователю
# ------------------------------
@router.message(TransferBalance.waiting_for_amount)
async def transfer_amount(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        return

    data = await state.get_data()
    caption_message_id = data.get("caption_message_id")
    target_user_id = data.get("target_user_id")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(user, "back"), callback_data="topup_balance")]
        ]
    )

    menu = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(user, "back"), callback_data="back_to_menu")]
        ]
    )

    if not message.text.isdigit():
        await message.delete()
        await safe_edit_caption(
            bot=message.bot,
            chat_id=message.chat.id,
            message_id=caption_message_id,
            caption=t(user, "error_not_number"),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    amount = int(message.text)

    if amount <= 0:
        await message.delete()
        await safe_edit_caption(
            bot=message.bot,
            chat_id=message.chat.id,
            message_id=caption_message_id,
            caption=t(user, "error_positive"),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    with Session() as session:
        sender = session.query(User).filter_by(
            telegram_id=message.from_user.id
        ).first()

        receiver = session.query(User).filter_by(
            id=target_user_id
        ).first()

        sender_balance = session.query(UserBalance).filter_by(
            user_id=sender.id
        ).first()

        receiver_balance = session.query(UserBalance).filter_by(
            user_id=receiver.id
        ).first()

        if not sender_balance or sender_balance.amount < amount:
            await message.delete()
            await safe_edit_caption(
                bot=message.bot,
                chat_id=message.chat.id,
                message_id=caption_message_id,
                caption=t(user, "error_not_enough"),
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            return

        if not receiver_balance:
            receiver_balance = UserBalance(user_id=receiver.id, amount=0)
            session.add(receiver_balance)

        sender_balance.amount -= amount
        receiver_balance.amount += amount

        receiver_tg_id = receiver.telegram_id
        sender_tg_id = sender.telegram_id
        sender_name = sender.full_name or sender.username or str(sender.telegram_id)

        session.add_all([
            UserBalanceHistory(
                user_id=sender.id,
                change=-amount,
                reason="TRANSFER_SENT"
            ),
            UserBalanceHistory(
                user_id=receiver.id,
                change=amount,
                reason="TRANSFER_RECEIVED"
            )
        ])

        session.commit()

    await message.delete()
    await state.clear()

    await message.bot.edit_message_caption(
        chat_id=message.chat.id,
        message_id=caption_message_id,
        caption=t(user, "transfer_success", amount=amount),
        parse_mode="Markdown",
        reply_markup=keyboard
    )

    try:
        receiver_user = await get_user(receiver_tg_id)

        await message.bot.send_message(
            chat_id=receiver_tg_id,
            text=t(
                receiver_user,
                "transfer_received",
                name=sender_name,
                id=sender_tg_id,
                amount=amount
            ),
            parse_mode="Markdown",
            reply_markup=menu
        )
    except Exception as e:
        logger.warning(f"Notify error: {e}")


# ------------------------------
# Конвертация выбор
# ------------------------------
@router.callback_query(F.data == "convert_peaches")
async def convert_peaches_start(call: CallbackQuery, state: FSMContext):
    user = await get_user(call.from_user.id)
    if not user:
        return

    # 🔥 сохраняем message_id для дальнейшего редактирования
    await state.update_data(caption_message_id=call.message.message_id)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(user, "convert_peaches_button"), callback_data="convert_stars_to_peaches")],
            [InlineKeyboardButton(text=t(user, "convert_stars_button"), callback_data="convert_peaches_to_stars")],
            [InlineKeyboardButton(text=t(user, "back"), callback_data="topup_balance")]
        ]
    )

    await call.message.edit_caption(
        caption=t(user, "convert_choose"),
        parse_mode="Markdown",
        reply_markup=kb
    )

    await state.clear()
    await call.answer()


# ------------------------------
# Конвертация звёзд в персики
# ------------------------------
@router.callback_query(F.data == "convert_stars_to_peaches")
async def convert_stars_to_peaches(call: CallbackQuery, state: FSMContext):
    user = await get_user(call.from_user.id)
    if not user:
        return

    await state.update_data(
        direction="stars_to_peaches",
        main_message_id=call.message.message_id
    )

    kb = InlineKeyboardBuilder()
    kb.button(text=t(user, "back"), callback_data="convert_peaches")
    kb.adjust(1)

    caption = t(user, "convert_stars_to_peaches", rate=STAR_TO_RUB_RATE)

    await call.message.edit_caption(
        caption=caption,
        reply_markup=kb.as_markup()
    )

    await state.set_state(ConvertStates.WAIT_AMOUNT)
    await call.answer()


# ------------------------------
# Конвертация персики в звёзды
# ------------------------------
@router.callback_query(F.data == "convert_peaches_to_stars")
async def convert_peaches_to_stars(call: CallbackQuery, state: FSMContext):
    user = await get_user(call.from_user.id)
    if not user:
        return

    await state.update_data(
        direction="peaches_to_stars",
        main_message_id=call.message.message_id
    )

    kb = InlineKeyboardBuilder()
    kb.button(text=t(user, "back"), callback_data="convert_peaches")
    kb.adjust(1)

    caption = t(user, "convert_peaches_to_stars", rate=STAR_TO_RUB_RATE)

    await call.message.edit_caption(
        caption=caption,
        reply_markup=kb.as_markup()
    )

    await state.set_state(ConvertStates.WAIT_AMOUNT)
    await call.answer()


# ------------------------------
# Ввод суммы конвертации
# ------------------------------
@router.message(ConvertStates.WAIT_AMOUNT)
async def convert_amount_process(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.delete()
        return

    data = await state.get_data()
    main_message_id = data["main_message_id"]
    direction = data["direction"]

    kb = InlineKeyboardBuilder()
    kb.button(text=t(user, "back"), callback_data="convert_peaches")
    kb.adjust(1)

    # Проверка ввода
    if not message.text.isdigit():
        await message.delete()
        await safe_edit_caption(
            bot=message.bot,
            chat_id=message.chat.id,
            message_id=main_message_id,
            caption=t(user, "convert_invalid_number"),
            reply_markup=kb.as_markup()
        )
        return

    amount = int(message.text)

    if amount <= 0:
        await message.delete()
        await safe_edit_caption(
            bot=message.bot,
            chat_id=message.chat.id,
            message_id=main_message_id,
            caption=t(user, "convert_invalid_amount"),
            reply_markup=kb.as_markup()
        )
        return

    # Получаем баланс пользователя
    with Session() as session:
        balance = session.query(UserBalance).filter_by(user_id=user.id).first()

        if direction == "stars_to_peaches":
            if amount > balance.stars:
                await message.delete()
                await safe_edit_caption(
                    bot=message.bot,
                    chat_id=message.chat.id,
                    message_id=main_message_id,
                    caption=t(user, "convert_not_enough_stars", balance=balance.stars),
                    reply_markup=kb.as_markup()
                )
                return
            result = int(amount * STAR_TO_RUB_RATE)
        else:
            if amount > balance.amount:
                await message.delete()
                await safe_edit_caption(
                    chat_id=message.chat.id,
                    message_id=main_message_id,
                    caption=t(user, "convert_not_enough_peaches", balance=balance.amount),
                    reply_markup=kb.as_markup()
                )
                return
            result = int(amount / STAR_TO_RUB_RATE)

    # Подтверждение обмена
    kb = InlineKeyboardBuilder()
    kb.button(text=t(user, "confirm"), callback_data=f"confirm_convert_{amount}")
    kb.button(text=t(user, "cancel"), callback_data="convert_peaches")
    kb.adjust(1)

    await message.bot.edit_message_caption(
        chat_id=message.chat.id,
        message_id=main_message_id,
        caption = t(user, "convert_confirm", amount=amount, result=result, rate=STAR_TO_RUB_RATE),
        reply_markup=kb.as_markup()
    )

    await state.update_data(amount=amount, result=result)
    await state.set_state(ConvertStates.CONFIRM)

    await message.delete()


# ------------------------------
# Подтверждение конвертации
# ------------------------------
@router.callback_query(ConvertStates.CONFIRM, F.data.startswith("confirm_convert_"))
async def confirm_convert(call: CallbackQuery, state: FSMContext):
    user = await get_user(call.from_user.id)
    if not user:
        return

    data = await state.get_data()
    amount = data["amount"]
    result = data["result"]
    direction = data["direction"]

    kb = InlineKeyboardBuilder()
    kb.button(text=t(user, "back"), callback_data="topup_balance")
    kb.adjust(1)

    # Обновление баланса
    with Session() as session:
        balance = session.query(UserBalance).filter_by(user_id=user.id).first()
        if direction == "stars_to_peaches":
            balance.stars -= amount
            balance.amount += result
            direction_text = "Telegram Stars→Персики"
        else:
            balance.amount -= amount
            balance.stars += result
            direction_text = "Персики→Telegram Stars"
        session.commit()

    await call.message.edit_caption(
        caption=t(user, "convert_success",
            direction=direction_text,
            amount=amount,
            result=result
        ),
        reply_markup=kb.as_markup()
    )

    await state.clear()
    await call.answer()










































@router.callback_query(F.data == "withdraw_balance")
async def withdraw_start(call: CallbackQuery, state: FSMContext):
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Звезды", callback_data="withdraw_stars")
    kb.button(text="🍑 Персики", callback_data="withdraw_amount")
    kb.button(text="Назад", callback_data="topup_balance")
    kb.adjust(1)

    await call.message.edit_caption(
        caption="Что хотите вывести?",
        reply_markup=kb.as_markup()
    )

    await state.set_state(WithdrawState.choosing_type)


@router.callback_query(F.data.in_(["withdraw_stars", "withdraw_amount"]))
async def choose_type(call: CallbackQuery, state: FSMContext):
    await state.update_data(withdraw_type=call.data.replace("withdraw_", ""))

    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Через карту", callback_data="withdraw_card")
    kb.button(text="Назад", callback_data="withdraw_balance")
    kb.adjust(1)

    await call.message.edit_caption(
        caption="Выберите способ выплаты:",
        reply_markup=kb.as_markup()
    )

    await state.set_state(WithdrawState.choosing_payment)


@router.callback_query(F.data == "withdraw_card")
async def withdraw_card(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data="withdraw_balance")
    kb.adjust(1)

    msg = await call.message.edit_caption(
        caption="Введите номер карты:",
        reply_markup=kb.as_markup()
    )

    # Сохраняем ID сообщения бота
    await state.update_data(main_message_id=msg.message_id)
    await state.set_state(WithdrawState.entering_card)


# --- Ввод карты ---
@router.message(WithdrawState.entering_card)
async def get_card(message: Message, state: FSMContext):
    # Проверка: только цифры и длина от 13 до 19
    if not message.text.isdigit() or not (13 <= len(message.text) <= 19):
        # Ошибка через caption
        data = await state.get_data()
        main_message_id = data.get("main_message_id", message.message_id)
        kb = InlineKeyboardBuilder().button(text="Назад", callback_data="withdraw_balance").adjust(1)
        await safe_edit_caption (
            message.bot,
            chat_id=message.chat.id,
            message_id=main_message_id,
            caption="❌ Некорректный номер карты. Введите 13-19 цифр.",
            reply_markup=kb.as_markup()
        )
        return await message.delete()

    data = await state.get_data()
    main_message_id = data["main_message_id"]

    await state.update_data(card_number=message.text)

    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data="withdraw_card")
    kb.adjust(1)

    await safe_edit_caption (
        message.bot,
        chat_id=message.chat.id,
        message_id=main_message_id,
        caption="Введите Имя и Фамилию владельца карты:",
        reply_markup=kb.as_markup()
    )

    await message.delete()
    await state.set_state(WithdrawState.entering_name)


# --- Ввод ФИО ---
@router.message(WithdrawState.entering_name)
async def get_name(message: Message, state: FSMContext):
    # Только буквы и пробелы
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё\s]+", message.text):
        data = await state.get_data()
        main_message_id = data.get("main_message_id", message.message_id)
        kb = InlineKeyboardBuilder().button(text="Назад", callback_data="withdraw_card").adjust(1)
        await safe_edit_caption (
            message.bot,
            chat_id=message.chat.id,
            message_id=main_message_id,
            caption="❌ ФИО должно содержать только буквы и пробелы.",
            reply_markup=kb.as_markup()
        )
        return await message.delete()

    data = await state.get_data()
    main_message_id = data["main_message_id"]
    await state.update_data(card_holder=message.text)

    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data="withdraw_card")
    kb.adjust(1)

    await safe_edit_caption (
        message.bot,
        chat_id=message.chat.id,
        message_id=main_message_id,
        caption="Введите сумму для вывода:",
        reply_markup=kb.as_markup()
    )

    await message.delete()
    await state.set_state(WithdrawState.entering_amount)


# --- Ввод суммы ---
@router.message(WithdrawState.entering_amount)
async def get_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    main_message_id = data["main_message_id"]

    # Проверка: только цифры >= 1
    if not message.text.isdigit() or int(message.text) < 1:
        kb = InlineKeyboardBuilder().button(text="Назад", callback_data="withdraw_card").adjust(1)
        await safe_edit_caption (
            message.bot,
            chat_id=message.chat.id,
            message_id=main_message_id,
            caption="❌ Введите корректное число >= 1.",
            reply_markup=kb.as_markup()
        )
        return await message.delete()

    amount = int(message.text)
    user = await get_user(message.from_user.id)
    balance, stars = await get_user_balance(user.id)

    # Проверка баланса
    if data["withdraw_type"] == "stars" and amount > stars:
        kb = InlineKeyboardBuilder().button(text="Назад", callback_data="withdraw_card").adjust(1)
        await safe_edit_caption (
            message.bot,
            chat_id=message.chat.id,
            message_id=main_message_id,
            caption=f"❌ Недостаточно звезд. Доступно: {stars}",
            reply_markup=kb.as_markup()
        )
        return await message.delete()

    if data["withdraw_type"] == "amount" and amount > balance:
        kb = InlineKeyboardBuilder().button(text="Назад", callback_data="withdraw_card").adjust(1)
        await safe_edit_caption (
            message.bot,
            chat_id=message.chat.id,
            message_id=main_message_id,
            caption=f"❌ Недостаточно 🍑 Персиков. Доступно: {balance}",
            reply_markup=kb.as_markup()
        )
        return await message.delete()

    await state.update_data(amount=amount)

    # Предпросмотр
    preview = (
        f"📋 Проверьте данные:\n\n"
        f"Тип: {'⭐ Звезды' if data['withdraw_type']=='stars' else '🍑 Персики'}\n"
        f"Сумма: {amount}\n"
        f"Карта: {data['card_number']}\n"
        f"ФИО: {data['card_holder']}\n\n"
        f"Все верно?"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать", callback_data="withdraw_balance")
    kb.button(text="Отправить", callback_data="confirm_withdraw")
    kb.button(text="Назад", callback_data="withdraw_balance")
    kb.adjust(1)

    await message.bot.edit_message_caption(
        chat_id=message.chat.id,
        message_id=main_message_id,
        caption=preview,
        reply_markup=kb.as_markup()
    )

    await message.delete()
    await state.set_state(WithdrawState.preview)


# --- Подтверждение заявки ---
@router.callback_query(F.data == "confirm_withdraw")
async def confirm_withdraw(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await get_user(call.from_user.id)

    withdraw_type_display = "⭐ Звезды" if data['withdraw_type'] == "stars" else "🍑 Персики"

    # 1️⃣ Обновляем caption фото — убираем кнопки
    caption_text = (
        f"Тип: {withdraw_type_display}\n"
        f"Сумма: {data['amount']}\n"
        f"Карта: {data['card_number']}\n"
        f"ФИО: {data['card_holder']}"
    )

    await call.bot.edit_message_caption(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        caption=caption_text,
        reply_markup=None  # убираем кнопки
    )

    # 2️⃣ Отправляем заявку админу
    ADMIN_ID = 1799274098
    admin_text = (
        f"📥 Новая заявка\n\n"
        f"👤 {user.full_name}\n"
        f"🆔 {user.telegram_id}\n"
        f"💳 {data['card_number']}\n"
        f"👤 ФИО: {data['card_holder']}\n"
        f"💰 {data['amount']}\n"
        f"📦 {withdraw_type_display}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="Подтвердить",
        callback_data=f"admin_confirm_{user.telegram_id}_{data['withdraw_type']}_{data['amount']}"
    )
    kb.button(
        text="Отклонить",
        callback_data=f"admin_decline_{user.telegram_id}"
    )
    kb.adjust(1)

    await call.bot.send_message(ADMIN_ID, admin_text, reply_markup=kb.as_markup())

    # 3️⃣ Отправляем пользователю уведомление
    await call.message.reply("✅ Ваша заявка отправлена на проверку.")

    # 4️⃣ Очищаем состояние
    await state.clear()


@router.callback_query(F.data.startswith("admin_decline_"))
async def admin_decline_start(call: CallbackQuery, state: FSMContext):
    telegram_id = int(call.data.split("_")[2])

    await state.clear()
    await state.update_data(
        target_user=telegram_id,
        main_message_id=call.message.message_id,
        composed_text="",
        current_style=None
    )

    await call.message.edit_text(
        "✏️ Введите причину отклонения заявки:",
        reply_markup=InlineKeyboardBuilder()
        .button(text="Назад", callback_data="admin_menu")
        .adjust(1)
        .as_markup()
    )

    await state.set_state(AdminDeclineStates.WRITE_REASON)


@router.message(AdminDeclineStates.WRITE_REASON)
async def admin_write_reason(message: Message, state: FSMContext):
    data = await state.get_data()

    await state.update_data(composed_text=message.text)

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать", callback_data="decline_edit_menu")
    kb.button(text="Отправить", callback_data="decline_send")
    kb.button(text="Назад", callback_data="admin_menu")
    kb.adjust(1, 1, 1)

    await message.bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=data["main_message_id"],
        text=f"📋 Предпросмотр причины:\n\n{message.text}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await message.delete()
    await state.set_state(AdminDeclineStates.PREVIEW)


@router.callback_query(F.data == "decline_edit_menu")
async def decline_edit_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()

    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Найти текст", callback_data="decline_find_text")
    kb.button(text="Назад", callback_data="decline_back_preview")
    kb.adjust(1, 1)

    await callback.message.edit_text(
        f"📝 Текущий текст:\n\n{data['composed_text']}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await state.set_state(AdminDeclineStates.EDIT_MENU)


@router.callback_query(F.data == "decline_find_text")
async def decline_find_text(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()

    text = (
        "✏️ Введите фрагмент для редактирования:\n\n"
        f"{data['composed_text']}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardBuilder()
        .button(text="Назад", callback_data="decline_edit_menu")
        .adjust(1)
        .as_markup(),
        parse_mode="HTML"
    )

    await state.set_state(AdminDeclineStates.FIND_TEXT)


@router.message(AdminDeclineStates.FIND_TEXT)
async def process_decline_find(message: Message, state: FSMContext):
    data = await state.get_data()
    full_text = data["composed_text"]
    fragment = message.text

    if fragment not in full_text:
        await message.answer("❌ Фрагмент не найден.")
        return

    await state.update_data(
        edit_fragment=fragment,
        edit_fragment_edited=fragment,
        current_style=None
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="B", callback_data="decline_format_bold")
    kb.button(text="I", callback_data="decline_format_italic")
    kb.button(text="U", callback_data="decline_format_underline")
    kb.button(text="S", callback_data="decline_format_strike")
    kb.button(text="`", callback_data="decline_format_mono")
    kb.button(text="»", callback_data="decline_format_quote")
    kb.button(text="Сохранить", callback_data="decline_save_fragment")
    kb.button(text="Назад", callback_data="decline_edit_menu")
    kb.adjust(3, 3, 1, 1)

    await message.bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=data["main_message_id"],
        text=f"✏️ Редактируем:\n\n{fragment}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await message.delete()
    await state.set_state(AdminDeclineStates.EDIT_FRAGMENT)


@router.callback_query(F.data.startswith("decline_format_"))
async def decline_format(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    data = await state.get_data()
    fragment = data["edit_fragment_edited"]
    current_style = data.get("current_style")

    action = callback.data.split("_")[2]

    tags = {
        "bold": ("<b>", "</b>"),
        "italic": ("<i>", "</i>"),
        "underline": ("<u>", "</u>"),
        "strike": ("<s>", "</s>"),
        "mono": ("<code>", "</code>"),
        "quote": ("<blockquote>", "</blockquote>")
    }

    if current_style:
        start, end = tags[current_style]
        fragment = fragment.replace(start, "").replace(end, "")

    start, end = tags[action]
    fragment = f"{start}{fragment}{end}"

    await state.update_data(
        edit_fragment_edited=fragment,
        current_style=action
    )

    await callback.message.edit_text(
        f"✏️ Редактируем:\n\n{fragment}",
        reply_markup=callback.message.reply_markup,
        parse_mode="HTML"
    )


@router.callback_query(F.data == "decline_save_fragment")
async def decline_save_fragment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()

    full = data["composed_text"]
    old = data["edit_fragment"]
    new = data["edit_fragment_edited"]

    full = full.replace(old, new, 1)

    await state.update_data(
        composed_text=full,
        current_style=None
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Найти ещё", callback_data="decline_find_text")
    kb.button(text="Назад к предпросмотру", callback_data="decline_back_preview")
    kb.adjust(1, 1)

    await callback.message.edit_text(
        f"📝 Текущий текст:\n\n{full}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await state.set_state(AdminDeclineStates.EDIT_MENU)


@router.callback_query(F.data == "decline_back_preview")
async def decline_back_preview(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать", callback_data="decline_edit_menu")
    kb.button(text="Отправить", callback_data="decline_send")
    kb.button(text="Назад", callback_data="admin_menu")
    kb.adjust(1, 1, 1)

    await callback.message.edit_text(
        f"📋 Предпросмотр причины:\n\n{data['composed_text']}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await state.set_state(AdminDeclineStates.PREVIEW)


@router.callback_query(F.data == "decline_send")
async def decline_send(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()

    await callback.bot.send_message(
        data["target_user"],
        f"❌ Ваша заявка на вывод отклонена.\n\nПричина:\n{data['composed_text']}",
        parse_mode="HTML"
    )

    await callback.message.edit_text("✅ Причина отправлена пользователю.")
    await state.clear()























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

        await safe_edit_caption (
            bot=message.bot,
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

    builder = InlineKeyboardBuilder()
    periods = [1, 2, 5, 10, 20, 50]  # допустимые годы
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
# Выбор периода для всех (перезапись + UX)
# ------------------------------
@router.callback_query(F.data.startswith("fix_sub_all_"))
async def fix_sub_all_period(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    years = int(callback.data.split("_")[-1])
    now = now_local()

    rw_years = min(years, 50)  # максимум для Remnawave

    updated_count = 0
    failed_count = 0

    # Новый конец подписки — именно через N лет от текущего момента
    new_end = now + timedelta(days=years*365)
    rw_end = now + timedelta(days=rw_years*365)

    # ----------------------------
    # UX: Стикер + ожидание
    # ----------------------------
    wait_message = await callback.message.answer_sticker(
        "CAACAgEAAxkBAAFBQzppd5A_4Wk22T_jJFOGCrkkcV8ZLwACXA4AAoI0egEaqUfk_mnHQTgE"
    )
    wait_text = await callback.message.answer("⏳ Обработка подписок…")

    # Скрываем исходное сообщение с кнопками
    try:
        await callback.message.delete()
    except:
        pass

    # ----------------------------
    # Обновление подписок
    # ----------------------------
    with Session() as session:
        users = session.query(User).all()
        if not users:
            await wait_message.delete()
            await wait_text.delete()
            await state.clear()
            return await callback.message.answer("❌ Пользователи не найдены")

        for user in users:
            # Синхронизация с Remnawave
            try:
                ok = await sync_remnawave_expire(user.telegram_id, rw_end)
                if not ok:
                    failed_count += 1
            except Exception as e:
                failed_count += 1
                logger.error(f"Ошибка синхронизации RW для {user.telegram_id}: {e}")

            # Перезаписываем БД новой датой
            user.subscription_end = new_end
            updated_count += 1

        session.commit()  # коммит после всех изменений

    # ----------------------------
    # Завершаем UX: удаляем стикер и текст
    # ----------------------------
    await wait_message.delete()
    await wait_text.delete()

    # Показываем итоговое сообщение
    await callback.message.answer(
        f"✅ Подписка обновлена для {updated_count} пользователей.\n"
        f"⚠️ Не удалось синхронизировать с Remnawave для {failed_count} пользователей.",
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
    builder.button(text="👤 Пользователь", callback_data="admin_send_message_id")
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
    builder.button(text="👤 Пользователь", callback_data="admin_send_message_id")
    builder.button(text="👥 Всем пользователям", callback_data="target_all")
    builder.button(text="Назад", callback_data="admin_menu")
    builder.adjust(1)

    await callback.message.edit_text(
        "Выберите целевую аудиторию для рассылки:",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("target_"))
async def admin_send_message_target(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    target = callback.data.replace("target_", "")

    # --- формируем user_ids ---
    if target == "active":
        users = await get_all_users(with_subscription=True)
    elif target == "inactive":
        users = await get_all_users(with_subscription=False)
    elif target == "all":
        users = await get_all_users()
    else:
        return

    user_ids = [u.telegram_id for u in users]

    await state.clear()
    await state.update_data(
        user_ids=user_ids,
        composed_text="",
        current_style=None
    )

    # Отправляем сообщение и сохраняем его ID
    msg = await callback.message.edit_text(
        "✏️ Введите текст рассылки (будет доступно форматирование после ввода):",
        reply_markup=InlineKeyboardBuilder()
        .button(text="Назад", callback_data="back_to_targets")
        .adjust(1)
        .as_markup()
    )

    await state.update_data(main_message_id=msg.message_id)  # <-- важно!

    await state.set_state(AdminStates.COMPOSE_TEXT)

# -------------------------
# Старт рассылки по ID
# -------------------------
@router.callback_query(F.data == "admin_send_message_id")
async def admin_send_message_by_id(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()

    msg = await callback.message.edit_text(
        "✏️ Введите ID пользователей через запятую (например: 12345,67890):",
        reply_markup=InlineKeyboardBuilder()
        .button(text="Назад", callback_data="back_to_targets")
        .adjust(1)
        .as_markup()
    )

    await state.update_data(main_message_id=callback.message.message_id)
    await state.set_state(AdminStates.SEND_TO_IDS)


# -------------------------
# Ввод ID пользователей
# -------------------------
@router.message(AdminStates.SEND_TO_IDS)
async def enter_user_ids(message: Message, state: FSMContext):
    try:
        ids = [int(i.strip()) for i in message.text.split(",") if i.strip().isdigit()]
        if not ids:
            raise ValueError
    except:
        await message.answer("❌ Введите корректные ID через запятую.")
        return

    data = await state.get_data()
    main_message_id = data["main_message_id"]

    await state.update_data(user_ids=ids, composed_text="", current_style=None)

    await message.bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=main_message_id,
        text="✏️ Введите текст рассылки (будет доступно форматирование после ввода):",
        reply_markup=InlineKeyboardBuilder()
        .button(text="Назад", callback_data="admin_send_message_id")
        .adjust(1)
        .as_markup()
    )

    await message.delete()
    await state.set_state(AdminStates.COMPOSE_TEXT)


# -------------------------
# Назад к вводу текста
# -------------------------
@router.callback_query(F.data == "back_to_text")
async def back_to_text(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    await callback.message.edit_text(
        "✏️ Введите текст рассылки (будет доступно форматирование после ввода):",
        reply_markup=InlineKeyboardBuilder()
        .button(text="Назад", callback_data="admin_send_message_id")
        .adjust(1)
        .as_markup()
    )

    await state.set_state(AdminStates.COMPOSE_TEXT)


# -------------------------
# Ввод текста рассылки
# -------------------------
@router.message(AdminStates.COMPOSE_TEXT)
async def compose_text(message: Message, state: FSMContext):
    data = await state.get_data()
    main_message_id = data["main_message_id"]

    await state.update_data(composed_text=message.text)

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать", callback_data="edit_menu")
    kb.button(text="Назад", callback_data="back_to_text")
    kb.adjust(1, 1)

    await message.bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=main_message_id,
        text=f"📝 Текущий текст для рассылки:\n\n{message.text}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await message.delete()
    await state.set_state(AdminStates.PREVIEW)


# -------------------------
# Редактировать сообщение
# -------------------------
@router.callback_query(F.data == "edit_menu")
async def edit_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Изменить текст", callback_data="find_text")
    kb.button(text="Назад", callback_data="back_to_preview")
    kb.adjust(1, 1)

    await callback.message.edit_text(
        callback.message.text,
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await state.set_state(AdminStates.EDIT_MENU)


# -------------------------
# Найти сообщение
# -------------------------
@router.callback_query(F.data == "find_text")
async def find_text(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()

    preview = data["composed_text"]

    text = (
        "📝 Текущий текст для рассылки:\n\n"
        f"============================\n\n"
        f"{preview}\n\n"
        f"============================\n\n"
        "✏️ <b>Введите текст, который нужно отредактировать:</b>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data="edit_menu")
    kb.adjust(1)

    await callback.message.edit_text(
        text,
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await state.set_state(AdminStates.FIND_TEXT)


# -------------------------
# Нашли сообщение
# -------------------------
@router.message(AdminStates.FIND_TEXT)
async def process_find_text(message: Message, state: FSMContext):
    data = await state.get_data()
    full_text = data["composed_text"]
    fragment = message.text

    if fragment not in full_text:
        await message.answer("❌ Такой текст не найден.")
        return

    await state.update_data(
        edit_fragment=fragment,
        edit_fragment_edited=fragment,
        current_style=None
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="B", callback_data="format_bold")
    kb.button(text="I", callback_data="format_italic")
    kb.button(text="S", callback_data="format_strike")
    kb.button(text="U", callback_data="format_underline")
    kb.button(text="»", callback_data="format_quote")
    kb.button(text="`", callback_data="format_mono")
    kb.button(text="Сохранить", callback_data="save_fragment")
    kb.button(text="Назад", callback_data="edit_menu")
    kb.adjust(3, 3, 1, 1)

    await message.bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=data["main_message_id"],
        text=f"✏️ Редактируем фрагмент:\n\n{fragment}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await message.delete()
    await state.set_state(AdminStates.EDIT_FRAGMENT)


# -------------------------
# Формат сообщения
# -------------------------
@router.callback_query(F.data.startswith("format_"))
async def format_fragment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    data = await state.get_data()
    fragment = data["edit_fragment_edited"]
    current_style = data.get("current_style")
    action = callback.data.split("_")[1]

    # убираем старый стиль
    tags = {
        "bold": ("<b>", "</b>"),
        "italic": ("<i>", "</i>"),
        "underline": ("<u>", "</u>"),
        "strike": ("<s>", "</s>"),
        "mono": ("<code>", "</code>"),
        "quote": ("<blockquote>", "</blockquote>"),
    }

    if current_style:
        start, end = tags[current_style]
        fragment = fragment.replace(start, "").replace(end, "")

    start, end = tags[action]
    fragment = f"{start}{fragment}{end}"

    await state.update_data(
        edit_fragment_edited=fragment,
        current_style=action
    )

    await callback.message.edit_text(
        f"✏️ Редактируем фрагмент:\n\n{fragment}",
        reply_markup=callback.message.reply_markup,
        parse_mode="HTML"
    )


# -------------------------
# Сохранили сообщение
# -------------------------
@router.callback_query(F.data == "save_fragment")
async def save_fragment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    data = await state.get_data()
    full = data["composed_text"]
    old = data["edit_fragment"]
    new = data["edit_fragment_edited"]

    full = full.replace(old, new, 1)

    await state.update_data(
        composed_text=full,
        edit_fragment=None,
        edit_fragment_edited=None,
        current_style=None
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Найти ещё текст", callback_data="find_text")
    kb.button(text="Назад к предпросмотру", callback_data="back_to_preview")
    kb.adjust(1, 1)

    await callback.message.edit_text(
        f"📝 Текущий текст для рассылки:\n\n{full}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await state.set_state(AdminStates.EDIT_MENU)


# -------------------------
# Предпросмотр отправки сообщения
# -------------------------
@router.callback_query(F.data == "back_to_preview")
async def back_to_preview(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать", callback_data="edit_menu")
    kb.button(text="Отправить", callback_data="send_message_final")
    kb.button(text="Назад", callback_data="back_to_text")
    kb.adjust(1, 1, 1)

    await callback.message.edit_text(
        f"📝 Текущий текст для рассылки:\n\n{data['composed_text']}",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await state.set_state(AdminStates.PREVIEW)



@router.callback_query(F.data == "send_message_final")
async def send_message_final(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    data = await state.get_data()

    user_ids = data.get("user_ids", [])
    text = data.get("composed_text", "")

    if not user_ids or not text:
        return await callback.message.answer("❌ Ошибка: нет данных для отправки.")

    success = failed = 0
    blocked_users = []

    for uid in user_ids:
        try:
            await bot.send_message(
                uid,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            success += 1
        except:
            failed += 1
            blocked_users.append(uid)

    report = (
        f"<b>📨 Рассылка завершена!</b>\n\n"
        f"• Успешно: {success}\n"
        f"• Не удалось: {failed}\n"
        f"• Всего: {len(user_ids)}"
    )

    if blocked_users:
        report += f"\n• 🚫 Заблокировали: {', '.join(map(str, blocked_users))}"

    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Админ. меню", callback_data="admin_menu")
    kb.adjust(1)

    await callback.message.edit_text(report, reply_markup=kb.as_markup(), parse_mode="HTML")
    await state.clear()


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
