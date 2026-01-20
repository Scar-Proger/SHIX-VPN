import asyncio
import os
import logging
import requests
import json
from functions import XUIAPI  
from datetime import datetime, timedelta
from aiogram import Dispatcher, Router, F, Bot
from aiogram.types import InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, WebAppInfo
from config import config
from database import (
    StaticProfile, get_user, create_user, apply_promo_code, create_or_update_promo_code, 
    get_all_promocodes_list, delete_promocode,
    get_all_users, create_static_profile, get_static_profiles, 
    User, PromoCode, Session, get_user_stats as db_user_stats
)
from typing import Dict, TypedDict, List
from functions import create_vless_profile, delete_client_by_email, get_user_stats, create_static_client, get_global_stats, get_online_users

logger = logging.getLogger(__name__)

router = Router()

MAX_MESSAGE_LENGTH = 4096
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANNER_PATH = os.path.join(BASE_DIR, "img", "vpn_banner.jpeg")

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
    
# ------------------------------
# Получение промокода из БД
# ------------------------------
async def get_promo_code(code: str):
    """Возвращает объект PromoCode из БД, если он активен"""
    with Session() as session:
        return session.query(PromoCode).filter_by(code=code.upper(), is_active=True).first()

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

# =========================================================
# LAVA
# =========================================================

class LavaPrice(TypedDict):
    offer_id: str
    amount: int  # RUB

LAVA_PRICES: Dict[int, List[LavaPrice]] = {}
DISCOUNTS = [0, 5, 10, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100]

def load_lava_prices(api_key: str) -> None:
    """Загружает Lava прайс и сопоставляет его с нашим прайс-листом"""
    url = "https://gate.lava.top/api/v2/products"
    headers = {
        "accept": "application/json",
        "X-Api-Key": api_key
    }

    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"🛑 Ошибка запроса к Lava API: {e}")
        return

    data = r.json()
    logger.info(f"🔥 Lava raw data: {data}")

    LAVA_PRICES.clear()

    for product in data.get("items", []):
        for offer in product.get("offers", []):
            # Ищем цену в RUB
            rub_price = None
            for price in offer.get("prices", []):
                if price.get("currency") == "RUB":
                    rub_price = int(price["amount"])
                    break

            if not rub_price:
                continue

            # Сопоставляем с нашим прайсом
            months = None
            for m, base_price in config.CUSTOM_LAVA_PRICES.items():
                for discount in DISCOUNTS:
                    if abs(rub_price - int(base_price * (100 - discount) / 100)) <= 1:
                        months = m
                        break
                if months:
                    break

            if months:
                if months not in LAVA_PRICES:
                    LAVA_PRICES[months] = []
                LAVA_PRICES[months].append({
                    "offer_id": offer["id"],
                    "amount": rub_price
                })

            else:
                logger.warning(f"[LOG] Цена {rub_price} ₽ не соответствует нашему прайсу, offer {offer.get('id')} пропущен")

    if not LAVA_PRICES:
        logger.error("[LOG] Lava цены загружены, но словарь LAVA_PRICES пуст. Проверьте продукты и валюту.")
    else:
        logger.info(f"[LOG] Lava prices успешно загружены: {LAVA_PRICES}")

def price_with_discount(base: int, discount: int) -> int:
    return int(base * (100 - discount) / 100)

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


# =========================================================
# UX
# =========================================================
async def show_menu(bot: Bot, chat_id: int, message_id: int = None):
    """Функция для отображения меню (редактировать существующее сообщение или отправлять новое)"""
    user = await get_user(chat_id)
    if not user:
        return

    # Проверка подписки
    now = datetime.utcnow()
    if not user.subscription_end or user.subscription_end < now:
        status = "Нет подписки"
        expire_date = "-"
        sub_text = ""  # <-- ссылка не показываем
    else:
        status = "Активна"
        expire_date = user.subscription_end.strftime("%d-%m-%Y %H:%M")
        sub_text = f"🔗 **Ваша ссылка для подключения:** `https://shix-vpn.space:2096/sub/{user.sub_id}`\n\n"

    # Формируем текст меню
    text = (
        f"👤 **Профиль:** `{user.full_name}`\n\n"
        f"🆔 **ID Telegram:** `{user.telegram_id}`\n\n"
        f"{sub_text}"
        f"✅ **Статус подписки:** `{status}`\n\n"
        f"📅 **Дата окончания подписки:** `{expire_date}`\n\n"
        f"💡 Используйте кнопки ниже, чтобы управлять подпиской и получать максимум от SHIX VPN."
    )

    builder = InlineKeyboardBuilder()

    renew_text = "💵 Продлить" if status == "Активна" else "💵 Купить"

    # === Основные кнопки по 2 в ряд ===
    callback_buttons = [
        (renew_text, "renew_sub"),
        ("✅ Подключить", "connect"),
        ("🎁 Промокод", "promo_code"),
        ("ℹ️ О нас", "help")
    ]
    for i in range(0, len(callback_buttons), 2):
        row = callback_buttons[i:i+2]
        builder.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])

    # === Кнопки в отдельной строке ===
    # Реферальная программа
    builder.row(InlineKeyboardButton(text="👥 Реферальная программа", callback_data="referral"))
    # Поддержка (URL)
    if user.is_admin:
        builder.row(InlineKeyboardButton(text="⚠️ Админ. меню", callback_data="admin_menu"))

    builder.row(InlineKeyboardButton(text="🆘 Поддержка", url="https://t.me/shix_vpn?direct"))
    # Админ меню (только для админов)

    # Отправка или редактирование сообщения
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=builder.as_markup(),
                parse_mode='Markdown'
            )
        except TelegramBadRequest as e:
            if "there is no text in the message to edit" in str(e):
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=builder.as_markup(),
                    parse_mode='Markdown'
                )
            else:
                raise
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode='Markdown'
        )

# ------------------------------
# Обновить сообщение
# ------------------------------
async def update_message(bot: Bot, callback: CallbackQuery, text: str = None, photo: FSInputFile = None, reply_markup: InlineKeyboardBuilder = None, parse_mode: str = "Markdown"):
    """Удаляет старое сообщение и отправляет новое (с текстом или фото)"""
    chat_id = callback.from_user.id
    message_id = callback.message.message_id

    # Удаляем старое
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass  # если не удалось удалить, просто идем дальше

    # Отправляем новое
    if photo:
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=text or "",
            parse_mode=parse_mode,
            reply_markup=reply_markup.as_markup() if reply_markup else None
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=text or "",
            parse_mode=parse_mode,
            reply_markup=reply_markup.as_markup() if reply_markup else None
        )

# ------------------------------
# Создание профиля
# ------------------------------
@router.message(Command("start"))
async def start_cmd(message: Message, bot: Bot):
    logger.info(f"ℹ️ Команда start от {message.from_user.id}")

    xui = XUIAPI()  # создаём X-UI API сессию

    # -------------------------------
    # 1️⃣ Получаем реферера из ссылки
    # -------------------------------
    referrer_id = None
    if " " in message.text:
        candidate = message.text.split(" ")[1]
        if candidate.isdigit():
            referrer_id = int(candidate)

    # -------------------------------
    # 2️⃣ Получаем пользователя
    # -------------------------------
    user = await get_user(message.from_user.id)

    if user:
        # Обновляем данные
        updated = False
        with Session() as session:
            db_user = session.query(User).filter_by(id=user.id).first()
            if db_user.full_name != message.from_user.full_name:
                db_user.full_name = message.from_user.full_name
                updated = True
            if db_user.username != message.from_user.username:
                db_user.username = message.from_user.username
                updated = True
            if updated:
                session.commit()
                logger.info(f"🔄 Данные пользователя обновлены: {message.from_user.id}")

    else:
        # -------------------------------
        # 3️⃣ Создаём нового пользователя
        # -------------------------------
        is_admin = message.from_user.id in config.ADMINS
        await create_user(
            telegram_id=message.from_user.id,
            full_name=message.from_user.full_name,
            username=message.from_user.username,
            is_admin=is_admin,
            referrer_id=referrer_id  # сохраняем пригласившего
        )

        # Получаем свежего пользователя
        user = await get_user(message.from_user.id)

        # -------------------------------
        # 4️⃣ Создаём X-UI профиль
        # -------------------------------
        profile_data = await create_vless_profile(user.telegram_id)
        if profile_data:
            with Session() as session:
                db_user = session.query(User).filter_by(id=user.id).first()
                db_user.vless_profile_data = json.dumps(profile_data)
                session.commit()
            logger.info(f"✅ X-UI профиль создан для {user.telegram_id}")
        else:
            logger.warning(f"⚠️ Не удалось создать X-UI профиль для {user.telegram_id}")

        # -------------------------------
        # 5️⃣ Приветственное сообщение
        # -------------------------------
        welcome_text = (
            f"Добро пожаловать в `{(await bot.get_me()).full_name}`!\n\n"
            "Воспользуйтесь бесплатным доступом к нашему VPN сервису.\n\n"
            "Мы №1 там, где другие сдаются.\n"
            "Первый сервис от экосистемы Shix Space Labs — для тех, кто выбирает лучшее."
        )

        if referrer_id:
            # Доп. бонусное сообщение, если есть реферер
            welcome_text += (
                "\n\n🎁 На вашем балансе ждёт бонус за приглашение! "
                "Пригласите 3 друзей и получите полный пакет MIND 💰."
            )

            # Уведомляем пригласившего
            await bot.send_message(
                referrer_id,
                f"⚡️ Ваш реферал [@{message.from_user.username or 'Без имени'}](tg://user?id={message.from_user.id}) только что присоединился! "
                "Не тормози, приглашай друзей!",
                parse_mode="Markdown"
            )

        await message.answer(welcome_text, parse_mode='Markdown')
        await asyncio.sleep(2)

    # -------------------------------
    # 6️⃣ Закрываем X-UI сессию
    # -------------------------------
    await xui.close()

    # -------------------------------
    # 7️⃣ Показываем меню
    # -------------------------------
    await show_menu(bot, message.from_user.id)

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
            logger.info(f"🔄 Данные пользователя обновлены в меню: {message.from_user.id}")
    
    await show_menu(bot, message.from_user.id)

# ------------------------------
# Помощь
# ------------------------------
@router.callback_query(F.data == "help")
async def help_msg(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🌐 Наш канал", url="https://t.me/+42MdJtX5B9I4ZDIy"))
    builder.row(InlineKeyboardButton(text="📄 Пользовательское соглашение", url="https://example.com/terms"))
    builder.row(InlineKeyboardButton(text="🔒 Политика конфиденциальности", url="https://example.com/privacy"))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="back_to_menu"))

    text = (
        "SHIX VPN — это первый сервис от экосистемы Shix Space Labs :\n"
        "• Высокая скорость\n"
        "• Стабильное соединение\n"
        "• Надёжность работы\n\n"
        "Там, где другие сдаются — мы продолжаем работать для вас."
    )

    photo = FSInputFile(BANNER_PATH)
    
    await update_message(bot, callback, text=text, photo=photo, reply_markup=builder, parse_mode="HTML")

# ------------------------------
# Реферальная программа
# ------------------------------
@router.callback_query(F.data == "referral")
async def referral_program(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("🛑 Ошибка профиля")
        return

    # 👉 твоя реферальная ссылка
    referral_link = f"https://t.me/shix_vpn_bot?start={user.telegram_id}"

    text = (
        "👑 **Станьте нашим партнёром и получайте 30%** со всех платежей "
        "ваших пользователей **пожизненно**.\n\n"
        "🌟 Выплаты начисляются **с каждого платежа**, пока клиент пользуется нашим сервисом.\n\n"
        "📊 **В личном кабинете доступно:**\n"
        "• История платежей\n"
        "• 💴 Баланс\n"
        "• 🤑 Запрос на вывод\n\n"
        "🔔 Вы будете получать **уведомления по каждой операции прямо в Telegram** — удобно и прозрачно.\n\n"
        "🔗 **Ваша реферальная ссылка:**\n"
        f"`{referral_link}`"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="back_to_menu")

    await callback.message.edit_text(
        text,
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
        await callback.answer("🛑 Ошибка профиля")
        return
    
    if user.subscription_end < datetime.utcnow():
        await callback.answer("⚠️ Подписка истекла! Продлите подписку.")
        return
    
    # ✅ Проверяем только наличие профиля, не создаём его
    if not user.vless_profile_data:
        await callback.answer("⚠️ Ваш VPN профиль ещё не создан. Попробуйте позже.")
        return
    
    profile_data = safe_json_loads(user.vless_profile_data, default={})
    if not profile_data:
        await callback.answer("⚠️ Ваш VPN профиль пуст. Попробуйте позже.")
        return
    
    sub_url = user.sub_id
    text = (
        "🎉 **Ваш VPN профиль готов!**\n\n"
        "ℹ️ **Инструкция по подключению:**\n"
        "1. Скачайте приложение для вашей платформы\n"
        "2. Скопируйте эту ссылку и импортируйте в приложение:\n\n"
        f"`https://shix-vpn.space:2096/sub/{sub_url}`\n\n"
        "3. Активируйте соединение в приложении."
    )

    builder = InlineKeyboardBuilder()
    builder.button(text='🖥️ Windows', url='https://github.com/2dust/v2rayN/releases/download/7.13.8/v2rayN-windows-64-desktop.zip')
    builder.button(text='🐧 Linux', url='https://github.com/MatsuriDayo/nekoray/releases/download/4.0.1/nekoray-4.0.1-2024-12-12-debian-x64.deb')
    builder.button(text='🍎 Mac', url='https://github.com/yanue/V2rayU/releases/download/v4.2.6/V2rayU-64.dmg ')
    builder.button(text='🍏 iOS', url='https://apps.apple.com/ru/app/v2raytun/id6476628951')
    builder.button(text='🤖 Android', url='https://github.com/2dust/v2rayNG/releases/download/1.10.16/v2rayNG_1.10.16_arm64-v8a.apk')
    builder.button(text="Назад", callback_data="back_to_menu")
    builder.adjust(2, 2, 1, 1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='Markdown')

# ------------------------------
# Продление подписки
# ------------------------------
@router.callback_query(F.data == "renew_sub")
async def renew_cb(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    photo = FSInputFile(BANNER_PATH)

    user = await get_user(callback.from_user.id)
    discount = user.active_discount if user else 0  # 0 если скидки нет

    kb = InlineKeyboardBuilder()
    found_any = False

    for months, base_price in config.CUSTOM_LAVA_PRICES.items():
        # Цена с учётом скидки
        price = int(base_price * (100 - discount) / 100)

        # Проверяем, есть ли offerId в LAVA_PRICES с такой ценой
        offers_for_month = LAVA_PRICES.get(months, [])
        offer_found = None
        for offer in offers_for_month:
            if offer["amount"] == price:
                offer_found = offer
                break

        if not offer_found:
            continue  # пропускаем тариф, если Lava не имеет такого offerId с этой ценой

        found_any = True

        # Формируем текст кнопки
        if months == 12:
            label = f"1 год — {price} ₽"
        elif months == 24:
            label = f"2 года — {price} ₽"
        else:
            label = f"{months} мес — {price} ₽/мес"

        if discount:
            label += f" (с учётом скидки {discount}%)"

        # callback_data с учётом offerId из Lava
        kb.button(text=label, callback_data=f"lava_{months}_{offer_found['offer_id']}")

    if not found_any:
        await update_message(bot, callback,
                             text="❌ Тарифы временно недоступны",
                             reply_markup=_back_kb("back_to_menu"))
        return

    kb.button(text="Назад", callback_data="back_to_menu")
    kb.adjust(1)

    await update_message(bot, callback,
                         text="🔥 Выберите тариф с учётом вашей скидки:",
                         photo=photo,
                         reply_markup=kb)

# ------------------------------
# Создание платежки через Lava
# ------------------------------
@router.callback_query(F.data.startswith("lava_"))
async def lava_pay_cb(callback: CallbackQuery, bot: Bot):
    await callback.answer()

    parts = callback.data.split("_")
    months = int(parts[1])
    offer_id = parts[2]

    user_email = f"user{callback.from_user.id}@example.com"

    # Создаём инвойс через Lava API
    try:
        r = requests.post(
            "https://gate.lava.top/api/v3/invoice",
            headers={
                "accept": "application/json",
                "Content-Type": "application/json",
                "X-Api-Key": config.LAVA_API_KEY,
            },
            json={
                "email": user_email,
                "offerId": offer_id,
                "currency": "RUB",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        await update_message(bot, callback,
                             text=f"❌ Ошибка Lava API:\n{e}",
                             reply_markup=_back_kb("renew_sub"))
        return

    if r.status_code == 201:
        pay_url = data.get("paymentUrl")

        kb = InlineKeyboardBuilder()
        kb.button(text="💳 Оплатить", web_app=WebAppInfo(url=pay_url))
        kb.button(text="Назад", callback_data="renew_sub")
        kb.adjust(1)

        user = await get_user(callback.from_user.id)
        discount_text = f" ({user.active_discount}% скидка)" if user and user.active_discount else ""

        await update_message(bot, callback,
                             text=f"💎 Подписка: {months} мес\n💰 Стоимость: {price_with_discount(config.CUSTOM_LAVA_PRICES[months], user.active_discount if user else 0)} ₽{discount_text}\n\nНажмите кнопку ниже для оплаты 👇",
                             reply_markup=kb)
    else:
        await update_message(bot, callback,
                             text=f"❌ Ошибка создания платежа\nКод: {r.status_code}",
                             reply_markup=_back_kb("renew_sub"))

# ------------------------------
# Обработчик кнопки "Промокод"
# ------------------------------
@router.callback_query(F.data == "promo_code")
async def promo_code_cb(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")

    msg = await callback.message.edit_text(
        "🎁 Введите ваш промокод для активации:",
        reply_markup=builder.as_markup()
    )

    # сохраняем ID сообщения бота
    await state.update_data(bot_message_id=msg.message_id)
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

    # Удаляем сообщение пользователя
    try:
        await bot.delete_message(chat_id, user_message_id)
    except:
        pass

    # Удаляем предыдущее сообщение бота
    if bot_message_id:
        try:
            await bot.delete_message(chat_id, bot_message_id)
        except:
            pass

    code_input = message.text.strip().upper()
    result = await apply_promo_code(user.telegram_id, code_input)

    # ---------- НЕВЕРНЫЙ ПРОМОКОД ----------
    if "error" in result:
        builder = InlineKeyboardBuilder()
        builder.button(text="Назад", callback_data="back_to_menu")

        msg = await bot.send_message(
            chat_id,
            "❌ Неверный промокод.\n\n🎁 Введите промокод ещё раз:",
            reply_markup=builder.as_markup()
        )

        await state.clear()
        await state.update_data(bot_message_id=msg.message_id)
        await state.set_state(PromoCodeStates.waiting_for_code)
        return

    # ---------- УСПЕХ ----------
    await bot.send_message(
        chat_id,
        (
            f"✅ Промокод применён!\n"
            f"💰 Ваша скидка обновлена на: {result['discount_percent']}%"
        ),
        parse_mode="Markdown"
    )

    await state.clear()
    await show_menu(bot, chat_id)































@router.callback_query(F.data == "admin_menu")
async def admin_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("🛑 Доступ запрещен!")
        return
    
    total, with_sub, without_sub = await db_user_stats()
    online_count = await get_online_users()
    
    text = (
        "🛡️ **Административное меню** 🛡️\n\n"
        f"👥 **Всего пользователей**: `{total}`\n"
        f"💎 **С подпиской**: `{with_sub}`\n"
        f"❌ **Без подписки**: `{without_sub}`\n"
        f"🟢 **Онлайн**: `{online_count}`\n"
        f"🔴 **Офлайн**: `{with_sub - online_count}`"
    )
    
    builder = InlineKeyboardBuilder()

    # Первая строка: + время и - время
    builder.button(text="+ время", callback_data="admin_add_time")
    builder.button(text="- время", callback_data="admin_remove_time")

    # Вторая строка: Список пользователей и Статистика сети
    builder.button(text="📋 Список пользователей", callback_data="admin_user_list")
    builder.button(text="📊 Статистика исп. сети", callback_data="admin_network_stats")

    # Третья строка: Рассылка
    builder.button(text="📢 Рассылка", callback_data="admin_send_message")

    # Четвёртая строка: слева Создать промокод, справа Список промокодов
    builder.button(text="🎁 Создать промокод", callback_data="admin_create_promo")
    builder.button(text="📦 Список промокодов", callback_data="admin_promocodes")

    # Пятая строка: Назад
    builder.button(text="Назад", callback_data="back_to_menu")

    # Настройка ширины строк (каждое число — количество кнопок в строке)
    builder.adjust(2, 1, 1, 1, 1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='Markdown')

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
    kb.button(text="⬅️ Назад", callback_data="admin_create_promo")
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
    try:
        max_uses = int(message.text.strip())
        if max_uses <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное положительное число для максимальных использований:")
        return

    data = await state.get_data()
    messages_to_delete = data.get("messages_to_delete", [])

    # Добавляем сообщение пользователя
    messages_to_delete.append(message.message_id)

    # Удаляем все старые сообщения
    for msg_id in messages_to_delete:
        try:
            await message.bot.delete_message(message.chat.id, msg_id)
        except:
            pass

    code = data.get("code")
    discount_percent = data.get("discount_percent")

    promo = await create_or_update_promo_code(
        code=code,
        discount_percent=discount_percent,
        max_uses=max_uses
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Админ. меню", callback_data="admin_menu")
    kb.adjust(1)

    await message.answer(
        f"✅ Промокод создан!\n\n"
        f"🎁 Код: `{promo['code']}`\n"
        f"💰 Скидка: `{promo['discount_percent']}%`\n"
        f"🔢 Максимум использований: `{promo['max_uses']}`",
        reply_markup=kb.as_markup(),
        parse_mode='Markdown'
    )

    # Чистим состояние — больше сообщений удалять не нужно
    await state.clear()













# ------------------------------
# Кнопка "Список промокодов" в админке
# ------------------------------
@router.callback_query(F.data == "admin_promocodes")
async def show_promocodes(callback: CallbackQuery, state: FSMContext):
    promos = await get_all_promocodes_list()
    if not promos:
        await callback.message.edit_text(
            "❌ Промокодов пока нет.",
            reply_markup=InlineKeyboardBuilder().button("Назад", callback_data="admin_menu").as_markup()
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
    promo = promos[index]

    remaining_uses = promo['max_uses'] - promo['used_count']
    is_active = remaining_uses > 0

    text = (
        f"🎁 Промокод #{index + 1}\n"  # нумерация
        f"Код: {promo['code']}\n"
        f"💰 Скидка: {promo['discount_percent']}%\n"
        f"🔢 Максимум использований: {promo['max_uses']}\n"
        f"✅ Использован: {promo['used_count']} раз\n"
        f"🔹 Осталось использований: {remaining_uses}\n"
        f"🔹 Активен: {'Да' if is_active else 'Нет'}"
    )

    builder = InlineKeyboardBuilder()

    # Навигационные стрелки
    if index > 0:
        builder.button(text="⬅️", callback_data="promocode_prev")
    if index < len(promos) - 1:
        builder.button(text="➡️", callback_data="promocode_next")

    # Удаление промокода
    builder.button(text="Удалить промокод", callback_data=f"promocode_delete_{index}")

    # Назад
    builder.button(text="Назад", callback_data="admin_menu")

    # Настройка ширины строк
    # 2 кнопки стрелок + удалить + назад = 4 на одной строке, лучше разнести:
    if index > 0 and index < len(promos) - 1:
        builder.adjust(2, 1, 1)  # стрелки, удалить, назад
    elif index > 0 or index < len(promos) - 1:
        builder.adjust(1, 1, 1)  # стрелка, удалить, назад
    else:
        builder.adjust(1, 1)  # удалить + назад

    await message.edit_text(text, reply_markup=builder.as_markup())

# ------------------------------
# Удаление промокода
# ------------------------------
@router.callback_query(F.data.startswith("promocode_delete_"))
async def promocode_delete(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Удаляю промокод...")
    index = int(callback.data.split("_")[-1])
    
    data = await state.get_data()
    promos = data.get("promos", [])
    
    if index >= len(promos):
        await callback.message.answer("❌ Промокод не найден.")
        return

    # Удаляем промокод из базы
    promo_to_delete = promos.pop(index)
    await delete_promocode(promo_to_delete['code'])

    # Обновляем список в состоянии
    await state.update_data(promos=promos)

    if not promos:
        kb = InlineKeyboardBuilder()
        kb.button(text="Назад", callback_data="admin_menu")
        kb.adjust(1)
        await callback.message.edit_text("❌ Промокодов больше нет.", reply_markup=kb.as_markup())
        return

    # Корректируем индекс для просмотра следующего/предыдущего
    new_index = min(index, len(promos) - 1)
    await state.update_data(index=new_index)

    # Перерисовываем сообщение с новым промокодом
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
























# Обработчики для управления временем подписки
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
    user_id = data['user_id']
    parts = message.text.split()
    
    if len(parts) != 4:
        await message.answer("Ошибка: нужно ввести 4 числа")
        return
    
    try:
        months, days, hours, minutes = map(int, parts)
        total_seconds = (
            months * 30 * 24 * 60 * 60 +
            days * 24 * 60 * 60 +
            hours * 60 * 60 +
            minutes * 60
        )
        
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if user:
                if user.subscription_end > datetime.utcnow():
                    user.subscription_end += timedelta(seconds=total_seconds)
                else:
                    user.subscription_end = datetime.utcnow() + timedelta(seconds=total_seconds)
                session.commit()
                await message.answer(f"✅ Добавлено время пользователю {user_id}")
            else:
                await message.answer("❌ Пользователь не найден")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")
    finally:
        await state.clear()

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
    user_id = data['user_id']
    parts = message.text.split()
    
    if len(parts) != 4:
        await message.answer("Ошибка: нужно ввести 4 числа")
        return
    
    try:
        months, days, hours, minutes = map(int, parts)
        total_seconds = (
            months * 30 * 24 * 60 * 60 +
            days * 24 * 60 * 60 +
            hours * 60 * 60 +
            minutes * 60
        )
        
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if user:
                new_end = user.subscription_end - timedelta(seconds=total_seconds)
                # Проверяем, чтобы не ушло в прошлое
                if new_end < datetime.utcnow():
                    new_end = datetime.utcnow()
                user.subscription_end = new_end
                session.commit()
                await message.answer(f"✅ Удалено время у пользователя {user_id}")
            else:
                await message.answer("❌ Пользователь не найден")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")
    finally:
        await state.clear()

# Обработчики для вывода списка пользователей
@router.callback_query(F.data == "admin_user_list")
async def admin_user_list(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ С подпиской", callback_data="user_list_active")
    builder.button(text="🛑 Без подписки", callback_data="user_list_inactive")
    builder.button(text="⏱️ Статические профили", callback_data="static_profiles_menu")
    builder.button(text="⬅️ Назад", callback_data="admin_menu")
    builder.adjust(1, 1, 1)
    await callback.message.edit_text("**Выберите фильтр**", reply_markup=builder.as_markup(), parse_mode='Markdown')

@router.callback_query(F.data == "user_list_active")
async def handle_user_list_active(callback: CallbackQuery):
    users = await get_all_users(with_subscription=True)
    await callback.answer()
    if not users:
        await callback.answer("Нет пользователей с активной подпиской")
        return
    
    text = "👤 <b>Пользователи с активной подпиской:</b>\n\n"
    for user in users:
        expire_date = user.subscription_end.strftime("%d.%m.%Y %H:%M")
        username = f"@{user.username}" if user.username else "none"
        user_line = f"• {user.full_name} ({username} | <code>{user.telegram_id}</code>) - до <code>{expire_date}</code>\n"
        
        # Если текст становится слишком длинным, отправляем текущую часть и начинаем новую
        if len(text) + len(user_line) > MAX_MESSAGE_LENGTH:
            await callback.message.answer(text, parse_mode="HTML")
            text = "👤 <b>Пользователи с активной подпиской (продолжение):</b>\n\n"
        
        text += user_line
    
    # Отправляем оставшуюся часть текста
    await callback.message.answer(text, parse_mode="HTML")

@router.callback_query(F.data == "user_list_inactive")
async def handle_user_list_inactive(callback: CallbackQuery):
    await callback.answer()
    users = await get_all_users(with_subscription=False)
    if not users:
        await callback.answer("Нет пользователей без подписки")
        return
    
    text = "👤 <b>Пользователи без подписки:</b>\n\n"
    for user in users:
        username = f"@{user.username}" if user.username else "none"
        user_line = f"• {user.full_name} ({username} | <code>{user.telegram_id}</code>)\n"
        
        # Если текст становится слишком длинным, отправляем текущую часть и начинаем новую
        if len(text) + len(user_line) > MAX_MESSAGE_LENGTH:
            await callback.message.answer(text, parse_mode="HTML")
            text = "👤 <b>Пользователи без подписки (продолжение):</b>\n\n"
        
        text += user_line
    
    # Отправляем оставшуюся часть текста
    await callback.message.answer(text, parse_mode="HTML")

# Обработчики для рассылки сообщений
@router.callback_query(F.data == "admin_send_message")
async def admin_send_message_start(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ С подпиской", callback_data="target_active")
    builder.button(text="🛑 Без подписки", callback_data="target_inactive")
    builder.button(text="👥 Всем пользователям", callback_data="target_all")
    builder.button(text="↩️ Назад", callback_data="admin_menu")
    builder.adjust(1)
    
    await callback.message.edit_text(
        "Выберите целевую аудиторию для рассылки:",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("target_"))
async def admin_send_message_target(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    target = callback.data.split("_")[1]
    await state.update_data(target=target)
    await callback.message.answer("Введите сообщение для рассылки:")
    await state.set_state(AdminStates.SEND_MESSAGE)

@router.message(AdminStates.SEND_MESSAGE)
async def admin_send_message(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target = data['target']
    text = message.text
    
    users = []
    if target == "active":
        users = await get_all_users(with_subscription=True)
    elif target == "inactive":
        users = await get_all_users(with_subscription=False)
    else:  # all
        users = await get_all_users()
    
    success = 0
    failed = 0
    
    for user in users:
        try:
            await bot.send_message(user.telegram_id, text)
            success += 1
        except Exception as e:
            logger.error(f"🛑 Ошибка отправки сообщения {user.telegram_id}: {e}")
            failed += 1
    
    await message.answer(
        f"📨 Результаты рассылки:\n\n"
        f"• Успешно: {success}\n"
        f"• Не удалось: {failed}\n"
        f"• Всего: {len(users)}"
    )
    await state.clear()

# Остальные обработчики остаются без изменений
@router.callback_query(F.data == "static_profiles_menu")
async def static_profiles_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="🆕 Добавить статический профиль", callback_data="static_profile_add")
    builder.button(text="📋 Вывести статические профили", callback_data="static_profile_list")
    builder.button(text="⬅️ Назад", callback_data="admin_user_list")
    builder.adjust(1)
    await callback.message.edit_text("**Выберите действие**", reply_markup=builder.as_markup(), parse_mode='Markdown')

@router.callback_query(F.data == "static_profile_add")
async def static_profile_add(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    await callback.message.answer("Введите имя для статического профиля:")
    await state.set_state(AdminStates.CREATE_STATIC_PROFILE)

@router.message(AdminStates.CREATE_STATIC_PROFILE)
async def process_static_profile_name(message: Message, state: FSMContext):
    profile_name = message.text
    profile_data = await create_static_client(profile_name)
    
    if profile_data:
        vless_url = "https://admin.com"
        await create_static_profile(profile_name, vless_url)
        profiles = await get_static_profiles()
        for profile in profiles:
            if profile.name == profile_name:
                id = profile.id
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑️ Удалить", callback_data=f"delete_static_{id}")
        await message.answer(f"Профиль создан!\n\n`{vless_url}`", reply_markup=builder.as_markup(), parse_mode='Markdown')
    else:
        await message.answer("Ошибка при создании профиля")
    
    await state.clear()

@router.callback_query(F.data == "static_profile_list")
async def static_profile_list(callback: CallbackQuery):
    profiles = await get_static_profiles()
    if not profiles:
        await callback.answer("Нет статических профилей")
        return
    
    for profile in profiles:
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑️ Удалить", callback_data=f"delete_static_{profile.id}")
        await callback.message.answer(
            f"**{profile.name}**\n`{profile.vless_url}`", 
            reply_markup=builder.as_markup(), parse_mode='Markdown'
        )

@router.callback_query(F.data.startswith("delete_static_"))
async def handle_delete_static_profile(callback: CallbackQuery):
    try:
        profile_id = int(callback.data.split("_")[-1])
        
        with Session() as session:
            profile = session.query(StaticProfile).filter_by(id=profile_id).first()
            if not profile:
                await callback.answer("⚠️ Профиль не найден")
                return
            
            success = await delete_client_by_email(profile.name)
            if not success:
                logger.error(f"🛑 Ошибка удаления клиента из инбаунда: {profile.name}")
            
            session.delete(profile)
            session.commit()
        
        await callback.answer("✅ Профиль удален!")
        await callback.message.delete()
    except Exception as e:
        logger.error(f"🛑 Ошибка при удалении статического профиля: {e}")
        await callback.answer("⚠️ Ошибка при удалении профиля")

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

@router.callback_query(F.data == "admin_network_stats")
async def network_stats(callback: CallbackQuery):
    stats = await get_global_stats()

    upload = f"{stats.get('upload', 0) / 1024 / 1024:.2f}"
    upload_size = 'MB' if int(float(upload)) < 1024 else 'GB'
    if upload_size == "GB":
        upload = f"{int(float(upload) / 1024):.2f}"

    download = f"{stats.get('download', 0) / 1024 / 1024:.2f}"
    download_size = 'MB' if int(float(download)) < 1024 else 'GB'
    if download_size == "GB":
        download = f"{int(float(download) / 1024):.2f}"
    
    await callback.answer()
    text = (
        "📊 **Статистика использования сети:**\n\n"
        f"🔼 Upload - `{upload} {upload_size}` | 🔽 Download - `{download} {download_size}`"
    )
    await callback.message.edit_text(text, parse_mode='Markdown')









@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await callback.answer()

    # Очистка состояния FSM (если было)
    await state.clear()

    chat_id = callback.from_user.id
    message_id = callback.message.message_id

    # 1. Удаляем сообщение (фото или текст)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass  # если не удалось удалить, идем дальше

    # 2. Отправляем профиль заново (текст)
    await show_menu(bot, chat_id)

def _back_kb(callback_data: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data=callback_data)
    return kb

def setup_handlers(dp: Dispatcher):
    dp.include_router(router)
    logger.info("✅ Обработчики успешно настроены")

def safe_json_loads(data, default=None):
    if not data:
        return default
    try:
        return json.loads(data)
    except Exception:
        return default
