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
from aiogram.client.session.aiohttp import AiohttpSession
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
member_cache = {}

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
    




async def refresh_membership():
    while True:
        users = await get_all_users()

        for user in users:
            try:
                member = await bot.get_chat_member(
                    config.REQUIRED_CHANNEL_ID,
                    user.telegram_id
                )

                member_cache[user.telegram_id] = member.status

            except Exception:
                member_cache[user.telegram_id] = None

            await asyncio.sleep(0.5)  # 👈 rate limit

        await asyncio.sleep(300)  # обновление раз в 5 минут

async def safe_get_chat_member(bot, chat_id, user_id, retries=5):
    for attempt in range(retries):
        try:
            return await bot.get_chat_member(
                chat_id=chat_id,
                user_id=user_id
            )

        # 🔌 разрыв соединения
        except ServerDisconnectedError:
            logger.warning(
                f"🔌 Разрыв соединения с Telegram (попытка {attempt+1}) | user={user_id}"
            )

        # 🌐 сетевые ошибки
        except ClientError as e:
            logger.warning(
                f"🌐 Сетевая ошибка: {e} | user={user_id}"
            )

        # ⏱ таймаут asyncio
        except asyncio.TimeoutError:
            logger.warning(
                f"⏱ Таймаут запроса к Telegram (попытка {attempt+1}) | user={user_id}"
            )

        # 💥 прочие ошибки
        except Exception as e:
            text = str(e).lower()

            if "timeout" in text:
                logger.warning(
                    f"⏱ Таймаут (строковый) (попытка {attempt+1}) | user={user_id}"
                )
            else:
                logger.warning(
                    f"⚠️ Неизвестная ошибка get_chat_member: {e} | user={user_id}"
                )
                return None  # реально странная ошибка — выходим

        # 🧠 умная задержка (backoff)
        await asyncio.sleep(2 + attempt)

    logger.warning(f"❌ Не удалось получить статус пользователя после {retries} попыток | user={user_id}")
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


async def safe_send(user, text, **kwargs):
    try:
        await bot.send_message(
            user.telegram_id,
            text,
            **kwargs
        )
        return True

    except TelegramForbiddenError:
        logger.info(f"🚫 {user.telegram_id} заблокировал бота (safe_send)")

        await full_cleanup_user(user, reason="blocked")
        return False

    except Exception as e:
        # 💥 иногда Telegram кидает не тот тип ошибки
        if "bot was blocked by the user" in str(e):
            logger.info(f"🚫 {user.telegram_id} заблокировал бота (string)")

            await full_cleanup_user(user, reason="blocked")
            return False

        logger.warning(f"⚠️ Ошибка отправки {user.telegram_id}: {e}")
        return False
    

async def full_cleanup_user(user, reason="unknown"):
    logger.info(f"🧹 Удаление пользователя {user.telegram_id} | причина: {reason}")

    # 🔔 админы
    if reason == "blocked":
        await notify_admins_bot_blocked(user)
    elif reason == "left":
        await notify_admins_user_left(user)

    # 1️⃣ Remnawave
    if user.vless_profile_data:
        try:
            data = json.loads(user.vless_profile_data)
            rw_uuid = data.get("uuid")

            if rw_uuid:
                deleted = await delete_client_by_id(rw_uuid)
                if deleted:
                    logger.info(f"✅ RW удалён: {rw_uuid}")
                else:
                    logger.warning(f"⚠️ RW не удалён: {rw_uuid}")

        except Exception as e:
            logger.error(f"❌ Ошибка RW {user.telegram_id}: {e}")

    # 2️⃣ БД (без лишнего SELECT)
    await delete_user_completely(user.telegram_id)


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
                    sent = await safe_send(
                        user,
                        t(user, "sub_expire_soon"),
                        reply_markup=subscription_action_keyboard(is_active=True),
                        parse_mode="Markdown"
                    )

                    # ✅ записываем только если реально отправили
                    if sent:
                        with Session() as session:
                            db_user = session.query(User).filter_by(
                                telegram_id=user.telegram_id
                            ).first()
                            if db_user:
                                db_user.notified = True
                                session.commit()

                # ---- подписка истекла ----
                elif delta <= timedelta(0):
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

                    await safe_send(
                        user,
                        t(user, "sub_expired"),
                        reply_markup=subscription_action_keyboard(is_active=False),
                        parse_mode="Markdown"
                    )

        except Exception as e:
            logger.warning(f"Критическая ошибка в задаче проверки подписок: {e}")

        await asyncio.sleep(300)


async def check_channel_membership():
    while True:
        try:
            users = await get_all_users()

            for user in users:
                try:
                    status = member_cache.get(user.telegram_id)

                    # 🚫 вышел из канала
                    if status not in ("member", "administrator", "creator"):
                        logger.info(f"🚫 {user.telegram_id} вышел из канала")

                        await full_cleanup_user(user, reason="left")
                        continue

                except TelegramForbiddenError:
                    # 🔥 блокировка бота
                    logger.info(f"🚫 {user.telegram_id} заблокировал бота")

                    await full_cleanup_user(user, reason="blocked")
                    continue

                except Exception as e:
                    # 🔥 иногда блокировка приходит не тем типом
                    if "bot was blocked by the user" in str(e):
                        logger.info(f"🚫 {user.telegram_id} заблокировал бота (string)")

                        await full_cleanup_user(user, reason="blocked")
                        continue

                    logger.warning(f"⚠️ Ошибка проверки {user.telegram_id}: {e}")
                    continue

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

    session = AiohttpSession(
        timeout=60,
        connector_kwargs={"family": 2}  # 🔥 только IPv4
    )

    bot = Bot(
        token=config.BOT_TOKEN,
        session=session
    )
    dp = Dispatcher()

    await init_db()
    await update_admins_status()

    setup_handlers(dp)

    asyncio.create_task(check_subscriptions())
    asyncio.create_task(check_channel_membership())

    logger.info("🤖 Бот запущен!")
    await dp.start_polling(bot)







# =================================================
# FASTAPI STARTUP
# =================================================
@app.on_event("startup")
async def startup():
    asyncio.create_task(start_bot())
