import logging
import asyncio
from datetime import datetime

from database import Session, User
from functions import RemnawaveWrapper

logger = logging.getLogger(__name__)








# =========================
# SYNC RW → MYSQL (FIXED)
# =========================
async def sync_remnawave_to_mysql_fixed():
    api = RemnawaveWrapper()

    success = 0
    failed = 0
    skipped = 0

    try:
        await api._ensure_session()

        with Session() as db:
            users = db.query(User).all()
            logger.info(f"🚀 USERS: {len(users)}")

            for user in users:
                try:
                    tg_id = user.telegram_id

                    # =========================
                    # ИЩЕМ ПОЛЬЗОВАТЕЛЯ В RW
                    # =========================
                    rw_user = await api.find_user_by_telegram_id(tg_id)

                    if not rw_user:
                        logger.warning(f"❌ RW NOT FOUND: {tg_id}")
                        failed += 1
                        continue

                    # =========================
                    # ПАРСИМ
                    # =========================
                    short_uuid = rw_user.get("shortUuid")
                    expire_at = rw_user.get("expireAt")
                    created_at = rw_user.get("createdAt")
                    rw_uuid = rw_user.get("uuid")          # ✅ ВАЖНО
                    vless_uuid = rw_user.get("vlessUuid")  # просто доп поле

                    if not expire_at:
                        logger.warning(f"⚠️ NO expireAt: {tg_id}")
                        skipped += 1
                        continue

                    expire_dt = datetime.fromisoformat(
                        expire_at.replace("Z", "+00:00")
                    )

                    created_dt = None
                    if created_at:
                        created_dt = datetime.fromisoformat(
                            created_at.replace("Z", "+00:00")
                        )

                    # =========================
                    # OLD
                    # =========================
                    old_sub = user.sub_id
                    old_reg = user.registration_date
                    old_expire = user.subscription_end
                    old_uuid = user.vless_profile_id

                    # =========================
                    # UPDATE
                    # =========================
                    if short_uuid:
                        user.sub_id = short_uuid

                    if created_dt:
                        user.registration_date = created_dt

                    user.subscription_end = expire_dt

                    # ✅ ВАЖНО: храним UUID пользователя RW
                    if rw_uuid:
                        user.vless_profile_id = rw_uuid

                    # ❗ vlessUuid НЕ ТРОГАЕМ (или добавь отдельное поле)

                    # =========================
                    # LOG
                    # =========================
                    logger.info(
                        f"✅ UPDATED {tg_id}\n"
                        f"sub_id: {old_sub} → {user.sub_id}\n"
                        f"reg: {old_reg} → {user.registration_date}\n"
                        f"expire: {old_expire} → {user.subscription_end}\n"
                        f"uuid: {old_uuid} → {user.vless_profile_id}"
                    )

                    success += 1

                    await asyncio.sleep(0.05)

                except Exception as e:
                    logger.error(f"❌ ERROR {user.telegram_id}: {e}")
                    failed += 1

            db.commit()
            logger.info("💾 DB COMMIT DONE")

    finally:
        await api.close()

    logger.info(
        f"🏁 DONE\n"
        f"✅ success: {success}\n"
        f"⚠️ skipped: {skipped}\n"
        f"❌ failed: {failed}"
    )

    return success, skipped, failed