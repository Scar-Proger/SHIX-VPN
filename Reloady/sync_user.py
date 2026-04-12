import logging
import re
import unicodedata

from database import Session, User, create_user
from functions import RemnawaveWrapper

logger = logging.getLogger(__name__)


# =========================
# CLEAN TEXT (ВАЖНО)
# =========================
def clean_text(text: str) -> str:
    if not text:
        return "Unknown"

    # нормализация unicode
    text = unicodedata.normalize("NFKC", text)

    # убираем NULL байты и мусор
    text = text.replace("\x00", "")

    return text.strip()


# =========================
# PARSER
# =========================
def parse_users(text: str):
    pattern = re.compile(
        r"•\s*(.*?)\s*├\s*(@[\w\d_]+|Без имени)\s*└\s*(\d+)"
    )

    users = []

    for match in pattern.finditer(text):
        full_name = clean_text(match.group(1))
        username_raw = match.group(2).strip()

        username = None
        if username_raw != "Без имени":
            username = username_raw.replace("@", "")

        users.append({
            "full_name": full_name,
            "username": username,
            "telegram_id": int(match.group(3))
        })

    return users


# =========================
# MYSQL + RW SYNC
# =========================
async def ensure_user_exists(telegram_id: int, full_name: str = None, username: str = None):

    full_name = clean_text(full_name or "Unknown")

    # ---------------- MYSQL ----------------
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

    # ---------------- REMNAWAVE ----------------
    api = RemnawaveWrapper()

    try:
        rw_user = await api.find_user_by_telegram_id(telegram_id)

        note = clean_text(f"{full_name} | @{username or 'no_username'} | tg:{telegram_id}")

        if rw_user and rw_user.get("uuid"):
            await api.update_user(
                rw_user["uuid"],
                {
                    "enabled": True,
                    "note": note,
                    "username": f"user_{telegram_id}"
                }
            )

            logger.info(f"✅ RW EXISTS | {telegram_id}")
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


# =========================
# BULK SYNC
# =========================
async def sync_from_text(text: str):
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