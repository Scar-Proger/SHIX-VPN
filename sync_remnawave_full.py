import logging
import aiohttp
import asyncio
from datetime import datetime

from database import Session, User
from config import config

logger = logging.getLogger(__name__)


# =========================
# GET USER BY UUID
# =========================
async def get_rw_user(session: aiohttp.ClientSession, uuid: str):
    url = f"{config.REMNAWAVE_API_URL}/users/{uuid}"

    try:
        async with session.get(url) as resp:
            text = await resp.text()

            if resp.status != 200:
                logger.error(f"❌ RW GET ERROR {resp.status}: {text}")
                return None

            return await resp.json()

    except Exception as e:
        logger.error(f"❌ RW REQUEST FAILED: {e}")
        return None







# =========================
# SYNC RW → MYSQL
# =========================
async def sync_remnawave_to_mysql_full():
    success = 0
    failed = 0
    skipped = 0

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers={
            "Authorization": f"Bearer {config.REMNAWAVE_API_KEY}",
            "Content-Type": "application/json",
        }
    ) as http:

        with Session() as db:
            users = db.query(User).all()
            logger.info(f"🚀 USERS: {len(users)}")

            for user in users:
                try:
                    # =========================
                    # НУЖЕН UUID
                    # =========================
                    if not user.vless_profile_id:
                        skipped += 1
                        continue

                    rw_user = await get_rw_user(http, user.vless_profile_id)

                    if not rw_user:
                        failed += 1
                        continue

                    # =========================
                    # ПАРСИНГ
                    # =========================
                    short_uuid = rw_user.get("shortUuid")
                    expire_at = rw_user.get("expireAt")
                    created_at = rw_user.get("createdAt")
                    vless_uuid = rw_user.get("vlessUuid")

                    if not expire_at:
                        logger.warning(f"⚠️ NO expireAt: {user.telegram_id}")
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
                    # OLD VALUES
                    # =========================
                    old_sub = user.sub_id
                    old_reg = user.registration_date
                    old_expire = user.subscription_end
                    old_vless = user.vless_profile_id

                    # =========================
                    # UPDATE
                    # =========================
                    if short_uuid:
                        user.sub_id = short_uuid

                    if created_dt:
                        user.registration_date = created_dt

                    user.subscription_end = expire_dt

                    if vless_uuid:
                        user.vless_profile_id = vless_uuid

                    # =========================
                    # LOG
                    # =========================
                    logger.info(
                        f"✅ UPDATED {user.telegram_id}\n"
                        f"sub_id: {old_sub} → {user.sub_id}\n"
                        f"reg: {old_reg} → {user.registration_date}\n"
                        f"expire: {old_expire} → {user.subscription_end}\n"
                        f"vless_uuid: {old_vless} → {user.vless_profile_id}"
                    )

                    success += 1

                    # анти-флуд API
                    await asyncio.sleep(0.05)

                except Exception as e:
                    logger.error(f"❌ ERROR {user.telegram_id}: {e}")
                    failed += 1

            # =========================
            # COMMIT
            # =========================
            db.commit()
            logger.info("💾 DB COMMIT DONE")

    logger.info(
        f"🏁 DONE\n"
        f"✅ success: {success}\n"
        f"⚠️ skipped: {skipped}\n"
        f"❌ failed: {failed}"
    )

    return success, skipped, failed