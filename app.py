import asyncio
import logging
import warnings
import json
import uuid
from datetime import timedelta
import coloredlogs
from fastapi import FastAPI
from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from fastapi.staticfiles import StaticFiles
import os
from aiohttp import ClientError, ServerDisconnectedError
from functions import delete_client_by_id
from config import config
from handlers import setup_handlers
from database import (
    now_local,
    Session,
    User,
    init_db,
    get_all_users,
    delete_user_completely
)

from sync_user import sync_from_text

from btn import subscription_action_keyboard
from locales import TEXTS

warnings.filterwarnings("ignore", category=DeprecationWarning)

# -------------------- LOGGING --------------------
coloredlogs.install(level="info")
logger = logging.getLogger(__name__)

# -------------------- FASTAPI --------------------
app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app.mount(
    "/assets",
    StaticFiles(directory=os.path.join(BASE_DIR, "assets")),
    name="assets"
)

# -------------------- AIROGRAM -------------------
bot: Bot | None = None
dp: Dispatcher | None = None

def t(user, key: str, **kwargs) -> str:
    lang = getattr(user, "language", "ru") or "ru"
    lang_dict = TEXTS.get(lang, TEXTS["ru"])

    if key not in lang_dict:
        return f"❗{key}"

    return lang_dict[key].format(**kwargs)

def admin_user_keyboard(user):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Написать пользователю",
                    url=f"tg://user?id={user.telegram_id}"
                )
            ]
        ]
    )
    

async def safe_get_chat_member(bot, chat_id, user_id, retries=3):
    for attempt in range(retries):
        try:
            return await bot.get_chat_member(chat_id=chat_id, user_id=user_id)

        except ServerDisconnectedError:
            logger.warning(f"🔌 Telegram disconnected (attempt {attempt+1})")
            await asyncio.sleep(1)

        except ClientError as e:
            logger.warning(f"🌐 Client error: {e}")
            await asyncio.sleep(1)

        except Exception as e:
            logger.warning(f"⚠️ Unknown error get_chat_member: {e}")
            return None

    return None

def admin_channel_left_text(user) -> str:
    username = f"@{user.username}" if user.username else "Без имени"
    full_name = user.full_name or "Без имени"

    return (
        "🚫 <b>Пользователь вышел из канала</b>\n\n"
        f"• <b>{full_name}</b>\n"
        f"  ├ {username}\n"
        f"  └ <code>{user.telegram_id}</code>"
    )


def admin_bot_blocked_text(user) -> str:
    username = f"@{user.username}" if user.username else "Без имени"
    full_name = user.full_name or "Без имени"

    return (
        "⛔ <b>Пользователь заблокировал бота</b>\n\n"
        f"• <b>{full_name}</b>\n"
        f"  ├ {username}\n"
        f"  └ <code>{user.telegram_id}</code>"
    )


async def check_subscriptions():
    while True:
        try:
            now = now_local() 
            users = await get_all_users()

            for user in users:
                if not user.subscription_end:
                    continue

                delta = user.subscription_end - now

                # ---- уведомление за 2 часа ----
                if timedelta(0) < delta <= timedelta(hours=2) and not user.notified:
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            t(user, "sub_expire_soon"),
                            reply_markup=subscription_action_keyboard(is_active=True),
                            parse_mode="Markdown"
                        )

                        with Session() as session:
                            db_user = session.query(User).filter_by(
                                telegram_id=user.telegram_id
                            ).first()
                            if db_user:
                                db_user.notified = True
                                session.commit()

                    except Exception as e:
                        logger.warning(f"Ошибка уведомления пользователя {user.telegram_id}: {e}")

                # ---- подписка истекла ----
                elif delta <= timedelta(0):
                    try:
                        with Session() as session:
                            db_user = session.query(User).filter_by(
                                telegram_id=user.telegram_id
                            ).first()
                            if not db_user:
                                continue

                            db_user.sub_id = uuid.uuid4().hex
                            db_user.subscription_end = None
                            db_user.notified = False
                            session.commit()

                        await bot.send_message(
                            user.telegram_id,
                            t(user, "sub_expired"),
                            reply_markup=subscription_action_keyboard(is_active=False),
                            parse_mode="Markdown"
                        )

                    except Exception as e:
                        logger.warning(f"Ошибка обработки окончания подписки {user.telegram_id}: {e}")

        except Exception as e:
            logger.warning(f"Критическая ошибка в задаче проверки подписок: {e}")

        await asyncio.sleep(300)


async def check_channel_membership():
    while True:
        try:
            # 1. СНАЧАЛА забираем только ID (быстро, без лишних полей)
            with Session() as session:
                user_ids = session.query(User.telegram_id).all()

            for (telegram_id,) in user_ids:
                try:
                    member = await safe_get_chat_member(
                        bot,
                        config.REQUIRED_CHANNEL_ID,
                        telegram_id
                    )

                    if not member:
                        continue

                    if member.status not in ("member", "administrator", "creator"):
                        logger.info(f"🚫 {telegram_id} вышел из канала")

                        # 2. получаем пользователя отдельно (короткая сессия)
                        with Session() as session:
                            user = session.query(User).filter_by(
                                telegram_id=telegram_id
                            ).first()

                        if not user:
                            continue

                        # уведомления админам (без БД)
                        await notify_admins_user_left(user)

                        # 3. удаление клиента
                        if user.vless_profile_data:
                            try:
                                data = json.loads(user.vless_profile_data)
                                rw_uuid = data.get("uuid")

                                if rw_uuid:
                                    await delete_client_by_id(rw_uuid)

                            except Exception as e:
                                logger.error(f"❌ RW error {telegram_id}: {e}")

                        # 4. удаление из БД (отдельная короткая сессия)
                        await delete_user_completely(telegram_id)

                        # 5. уведомление пользователя
                        try:
                            await bot.send_message(
                                telegram_id,
                                t(user, "channel_left")
                            )
                        except TelegramForbiddenError:
                            pass

                except TelegramForbiddenError:
                    logger.info(f"🚫 {telegram_id} заблокировал бота")

                    with Session() as session:
                        user = session.query(User).filter_by(
                            telegram_id=telegram_id
                        ).first()

                    if user:
                        await notify_admins_bot_blocked(user)

                        if user.vless_profile_data:
                            try:
                                data = json.loads(user.vless_profile_data)
                                rw_uuid = data.get("uuid")
                                if rw_uuid:
                                    await delete_client_by_id(rw_uuid)
                            except Exception as e:
                                logger.error(f"❌ RW error {telegram_id}: {e}")

                        await delete_user_completely(telegram_id)

                    continue

                except TelegramBadRequest:
                    continue

                # защита от лимитов Telegram
                await asyncio.sleep(0.3)

        except Exception as e:
            logger.error(f"❌ Критическая ошибка цикла: {e}")
            await asyncio.sleep(5)

        await asyncio.sleep(300)


async def notify_admins_user_left(user):
    text = admin_channel_left_text(user)

    for admin_id in config.ADMINS:
        try:
            await bot.send_message(
                admin_id,
                text,
                parse_mode="HTML",
                reply_markup=admin_user_keyboard(user)
            )
            
        except TelegramForbiddenError:
            pass
        except Exception as e:
            logger.warning(f"Ошибка уведомления админа {admin_id}: {e}")


async def notify_admins_bot_blocked(user):
    text = admin_bot_blocked_text(user)

    for admin_id in config.ADMINS:
        try:
            await bot.send_message(
                admin_id,
                text,
                parse_mode="HTML",
                reply_markup=admin_user_keyboard(user)
            )
        except TelegramForbiddenError:
            pass
        except Exception as e:
            logger.warning(f"Ошибка уведомления админа {admin_id}: {e}")







# =================================================
# ADMIN STATUS
# =================================================
async def update_admins_status():
    with Session() as session:
        session.query(User).update({User.is_admin: False})

        for admin_id in config.ADMINS:
            user = session.query(User).filter_by(telegram_id=admin_id).first()
            if user:
                user.is_admin = True
            else:
                session.add(
                    User(
                        telegram_id=admin_id,
                        full_name=f"Admin {admin_id}",
                        is_admin=True,
                    )
                )
        session.commit()


# =================================================
# BOT START
# =================================================
async def start_bot():
    global bot, dp

    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()

    await init_db()
    await update_admins_status()

    setup_handlers(dp)

    asyncio.create_task(check_subscriptions())
    asyncio.create_task(check_channel_membership())

    # 🔥 ТВОЙ ТЕКСТ
    text = """
    • Rare 🌙NEN🌙 ├ @Nikitas1k └ 5172115568
    • Матвей ├ @qer1337 └ 7565116548
    • Матвей ├ @qer1337 └ 7565116548
    • Вадим Ильмакачев ├ Без имени └ 8533340013
    • Без имени ├ @D1amonddddddd └ 8597501978
    • Mops ├ @yamopss └ 1657903588
    • #геткид ├ @getKlD └ 6109872847
    • Arsennisaev ├ @arsenisaevas └ 8166287562
    • Максим ├ @i26c4u └ 728517421
    • Алина ├ @amo_RALKA └ 1251360683
    """

    # 🔥 ЗАПУСК СИНХРОНИЗАЦИИ
    asyncio.create_task(sync_from_text(text))

    logger.info("🤖 Бот запущен!")
    await dp.start_polling(bot)







# =================================================
# FASTAPI STARTUP
# =================================================
@app.on_event("startup")
async def startup():
    asyncio.create_task(start_bot())
