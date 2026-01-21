import asyncio
import logging
import warnings
import uuid
from datetime import datetime, timedelta

import coloredlogs
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher

from config import config
from handlers import setup_handlers, load_lava_prices
from database import (
    Session,
    User,
    init_db,
    get_all_users,
)

warnings.filterwarnings("ignore", category=DeprecationWarning)

# -------------------- LOGGING --------------------
coloredlogs.install(level="info")
logger = logging.getLogger(__name__)

# -------------------- FASTAPI --------------------
app = FastAPI()

# -------------------- AIROGRAM -------------------
bot: Bot | None = None
dp: Dispatcher | None = None


# =================================================
# BACKGROUND TASK — ПРОВЕРКА ПОДПИСОК (REMNAWAVE)
# =================================================
async def check_subscriptions():
    while True:
        try:
            now = datetime.utcnow()
            users = await get_all_users()

            for user in users:
                # ---------- уведомление за 24 часа ----------
                if (
                    user.subscription_end
                    and user.subscription_end - now < timedelta(days=1)
                    and user.subscription_end >= now
                    and not user.notified
                ):
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            "⚠️ Ваша подписка истекает через 24 часа!"
                        )
                        with Session() as session:
                            db_user = session.query(User).filter_by(
                                telegram_id=user.telegram_id
                            ).first()
                            if db_user:
                                db_user.notified = True
                                session.commit()
                    except Exception as e:
                        logger.warning(f"Notify error: {e}")

                # ---------- подписка истекла ----------
                if user.subscription_end and user.subscription_end <= now:
                    try:
                        with Session() as session:
                            db_user = session.query(User).filter_by(
                                telegram_id=user.telegram_id
                            ).first()
                            if not db_user:
                                continue

                            # 🔑 REMNAWAVE LOGIC
                            # меняем sub_id — старая подписка умирает
                            db_user.sub_id = uuid.uuid4().hex
                            db_user.subscription_end = None
                            db_user.notified = False
                            session.commit()

                        await bot.send_message(
                            user.telegram_id,
                            "❌ Подписка истекла.\nДоступ к VPN отключён."
                        )

                    except Exception as e:
                        logger.warning(f"Expire handling error: {e}")

        except Exception as e:
            logger.warning(f"Subscription check error: {e}")

        await asyncio.sleep(3600)


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
    load_lava_prices(config.LAVA_API_KEY)

    asyncio.create_task(check_subscriptions())

    logger.info("🤖 Bot started")
    await dp.start_polling(bot)


# =================================================
# FASTAPI STARTUP
# =================================================
@app.on_event("startup")
async def startup():
    asyncio.create_task(start_bot())


# =================================================
# HEALTH CHECK
# =================================================
@app.get("/health")
async def health():
    return {"status": "ok"}


# =================================================
# PAYMENT WEBHOOK
# =================================================
@app.post("/payment/webhook")
async def payment_webhook(request: Request):
    payload = await request.json()
    logger.info(f"💰 Payment webhook: {payload}")

    if payload.get("status") != "paid":
        return {"ok": True}

    meta = payload.get("metadata", {})
    telegram_id = meta.get("telegram_id")
    months = int(meta.get("months", 1))

    if not telegram_id:
        return {"error": "telegram_id missing"}

    with Session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            return {"error": "user not found"}

        now = datetime.utcnow()

        # если подписка активна — продлеваем
        if user.subscription_end and user.subscription_end > now:
            user.subscription_end += timedelta(days=30 * months)
        else:
            user.subscription_end = now + timedelta(days=30 * months)

        user.notified = False

        # если sub_id нет — создаём
        if not user.sub_id:
            user.sub_id = uuid.uuid4().hex

        session.commit()

    await bot.send_message(
        telegram_id,
        f"✅ Оплата получена!\nПодписка продлена на {months} мес."
    )

    return {"ok": True}

