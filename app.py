import asyncio
import logging
import warnings
import json
import uuid
from datetime import timedelta
import coloredlogs
from fastapi import FastAPI
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from fastapi.staticfiles import StaticFiles
import os
from functions import delete_client_by_id


from config import config
from handlers import setup_handlers
from database import (
    now_local,
    Session,
    User,
    init_db,
    get_all_users,
)

from btn import subscription_action_keyboard

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
                            "🎁 **Ваша тестовая подписка почти закончилась!**\n\n"
                            "⏳ **Осталось всего 2 часа**\n\n"
                            "🔒 Продлите на **30 дней всего за 99 ₽** и пользуйтесь VPN без ограничений.\n\n"
                            "⚡️ Нажмите **«Оформить сейчас»** ⬇️",
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
                            "❌ **Срок действия подписки истёк!**\n\n"
                            "🔒 VPN временно отключён\n\n"
                            "⛔️ Доступ к сервисам приостановлен\n\n"
                            "👉 Чтобы восстановить подключение, продлите подписку ⬇️",
                            reply_markup=subscription_action_keyboard(is_active=False),
                            parse_mode="Markdown"
                        )

                    except Exception as e:
                        logger.warning(f"Ошибка обработки окончания подписки {user.telegram_id}: {e}")

        except Exception as e:
            logger.warning(f"Критическая ошибка в задаче проверки подписок: {e}")

        await asyncio.sleep(600)

async def check_channel_membership():
    while True:
        try:
            users = await get_all_users()

            for user in users:
                try:
                    member = await bot.get_chat_member(
                        chat_id=config.REQUIRED_CHANNEL_ID,
                        user_id=user.telegram_id
                    )

                    if member.status in ("left", "kicked"):
                        logger.info(f"🚫 {user.telegram_id} вышел из канала")

                        # 1️⃣ удаляем в Remnawave
                        if user.vless_profile_data:
                            try:
                                data = json.loads(user.vless_profile_data)
                                rw_uuid = data.get("uuid")

                                if rw_uuid:
                                    deleted = await delete_client_by_id(rw_uuid)
                                    if deleted:
                                        logger.info(f"✅ RW пользователь удалён: {rw_uuid}")
                                    else:
                                        logger.warning(f"⚠️ RW пользователь не удалён: {rw_uuid}")

                            except (json.JSONDecodeError, TypeError) as e:
                                logger.error(f"❌ Ошибка vless_profile_data у {user.telegram_id}: {e}")

                        # 2️⃣ чистим БД
                        with Session() as session:
                            db_user = session.query(User).filter_by(
                                telegram_id=user.telegram_id
                            ).first()
                            if db_user:
                                db_user.subscription_end = None
                                db_user.sub_id = None
                                db_user.vless_profile_data = None
                                session.commit()

                        # 3️⃣ уведомляем
                        await bot.send_message(
                            user.telegram_id,
                            "🚫 Вы вышли из канала, доступ к VPN отключён"
                        )

                except TelegramBadRequest:
                    # если пользователь вообще недоступен
                    continue

        except Exception as e:
            logger.error(f"Ошибка проверки подписки на канал: {e}")

        await asyncio.sleep(60)  # раз в 10 минут






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

    logger.info("🤖 Бот запущен!")
    await dp.start_polling(bot)


# =================================================
# FASTAPI STARTUP
# =================================================
@app.on_event("startup")
async def startup():
    asyncio.create_task(start_bot())