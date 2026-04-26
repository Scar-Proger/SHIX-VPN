import logging
import aiohttp
import asyncio

from database import Session, User
from functions import RemnawaveWrapper
from config import config

logger = logging.getLogger(__name__)


# =========================
# HAPP ENCRYPT
# =========================
async def encrypt_to_happ(session: aiohttp.ClientSession, sub_url: str) -> str:
    url = f"{config.REMNAWAVE_API_URL}/system/tools/happ/encrypt"

    try:
        async with session.post(
            url,
            json={"linkToEncrypt": sub_url}  # ✅ ВОТ ЭТО КЛЮЧЕВОЕ
        ) as resp:

            if resp.status != 200:
                text = await resp.text()
                logger.error(f"❌ HAPP encrypt error: {resp.status} | {text}")
                return None

            data = await resp.json()
            return data.get("encryptedLink")

    except Exception as e:
        logger.error(f"❌ HAPP request failed: {e}")
        return None


# =========================
# SYNC RW → MYSQL (HAPP)
# =========================
async def sync_happ_to_mysql():
    api = RemnawaveWrapper()

    success = 0
    failed = 0
    skipped = 0

    try:
        await api._ensure_session()

        # 🔥 грузим всех пользователей из БД
        with Session() as db:
            users = db.query(User).all()

        logger.info(f"🚀 USERS: {len(users)}")

        # 🔥 HTTP клиент с таймаутом
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {config.REMNAWAVE_API_KEY}",
                "Content-Type": "application/json",
            }
        ) as http:

            for user in users:
                tg_id = user.telegram_id

                try:
                    # =========================
                    # ПРОПУСК ЕСЛИ УЖЕ HAPP
                    # =========================
                    if (
                        user.vless_profile_data
                        and user.vless_profile_data.startswith("happ://")
                    ):
                        skipped += 1
                        continue

                    # =========================
                    # RW USER
                    # =========================
                    rw_user = await api.find_user_by_telegram_id(tg_id)

                    if not rw_user:
                        logger.warning(f"❌ RW NOT FOUND: {tg_id}")
                        failed += 1
                        continue

                    sub_url = rw_user.get("subscriptionUrl")

                    if not sub_url:
                        logger.warning(f"⚠️ NO SUB URL: {tg_id}")
                        skipped += 1
                        continue

                    # =========================
                    # ENCRYPT → HAPP
                    # =========================
                    happ_link = await encrypt_to_happ(http, sub_url)

                    if not happ_link:
                        failed += 1
                        continue

                    # =========================
                    # UPDATE MYSQL
                    # =========================
                    with Session() as db:
                        db_user = db.query(User).filter_by(
                            telegram_id=tg_id
                        ).first()

                        if not db_user:
                            failed += 1
                            continue

                        old = db_user.vless_profile_data

                        # 🔥 ОБНОВЛЯЕМ ТОЛЬКО ССЫЛКУ
                        db_user.vless_profile_data = happ_link

                        db.commit()

                        logger.info(
                            f"✅ UPDATED {tg_id}\n"
                            f"OLD: {old}\n"
                            f"NEW: {happ_link[:60]}..."
                        )

                        success += 1

                    # 🔥 защита от перегрузки API
                    await asyncio.sleep(0.1)

                except Exception as e:
                    logger.error(f"❌ ERROR {tg_id}: {e}")
                    failed += 1

    finally:
        await api.close()

    logger.info(
        f"🏁 DONE\n"
        f"✅ success: {success}\n"
        f"⚠️ skipped: {skipped}\n"
        f"❌ failed: {failed}"
    )

    return success, skipped, failed