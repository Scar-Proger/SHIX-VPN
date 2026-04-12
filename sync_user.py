import logging
import re

from database import Session, User, create_user
from functions import RemnawaveWrapper

logger = logging.getLogger(__name__)


# =========================================================
# PARSER (из твоего текста)
# =========================================================
def parse_users(text: str):
    """
    • Name ├ @username └ 123456
    """

    pattern = re.compile(
        r"•\s*(.*?)\s*├\s*(@[\w\d_]+|Без имени)\s*└\s*(\d+)"
    )

    users = []

    for match in pattern.finditer(text):
        full_name = match.group(1).strip()

        username_raw = match.group(2).strip()
        username = None if username_raw == "Без имени" else username_raw.replace("@", "")

        telegram_id = int(match.group(3))

        users.append({
            "full_name": full_name,
            "username": username,
            "telegram_id": telegram_id
        })

    return users


# =========================================================
# MAIN SYNC (MySQL + Remnawave)
# =========================================================
async def ensure_user_exists(telegram_id: int, full_name: str = None, username: str = None):

    full_name = full_name or "Unknown"

    # -------------------------
    # MYSQL
    # -------------------------
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()

        if not user:
            logger.info(f"🆕 MYSQL CREATE | {full_name} | @{username} | {telegram_id}")

            user = await create_user(
                telegram_id=telegram_id,
                full_name=full_name,
                username=username
            )
        else:
            logger.info(f"✅ MYSQL EXISTS | {user.full_name} | @{user.username} | {telegram_id}")

    # -------------------------
    # REMNAWAVE
    # -------------------------
    api = RemnawaveWrapper()

    try:
        rw_user = await api.find_user_by_telegram_id(telegram_id)

        note = f"{full_name} | @{username or 'no_username'} | tg:{telegram_id}"

        if rw_user and rw_user.get("uuid"):
            logger.info(f"✅ RW EXISTS | {telegram_id}")

            await api.update_user(
                rw_user["uuid"],
                {
                    "enabled": True,
                    "note": note,
                    "username": f"user_{telegram_id}"
                }
            )

            return True

        logger.info(f"🆕 RW CREATE | {telegram_id}")

        created = await api.create_user(telegram_id)

        if not created:
            logger.error(f"❌ RW ERROR | {telegram_id}")
            return False

        if created.get("uuid"):
            await api.update_user(
                created["uuid"],
                {
                    "note": note
                }
            )

        return True

    finally:
        await api.close()


# =========================================================
# RUN BULK FROM TEXT
# =========================================================
async def sync_from_text(text: str):
    """
    ВСТАВЛЯЕШЬ ТЕКСТ И ОНО ВСЁ ДЕЛАЕТ САМО
    """

    users = parse_users(text)

    logger.info(f"🚀 найдено пользователей: {len(users)}")

    for u in users:
        try:
            await ensure_user_exists(
                telegram_id=u["telegram_id"],
                full_name=u["full_name"],
                username=u["username"]
            )

        except Exception as e:
            logger.error(f"❌ ERROR {u['telegram_id']}: {e}")