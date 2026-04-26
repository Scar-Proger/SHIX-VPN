import logging
from database import Session, User
from functions import RemnawaveWrapper

logger = logging.getLogger(__name__)



# =========================
# SYNC ОДНОГО ПОЛЬЗОВАТЕЛЯ
# =========================
async def sync_user_to_rw(user: User) -> bool:
    api = RemnawaveWrapper()

    try:
        telegram_id = user.telegram_id

        existing = await api.find_user_by_telegram_id(telegram_id)

        if existing:
            logger.info(f"⏭ Уже существует в RW: {telegram_id}")
            return True

        # 🔥 ВАЖНО: используем ТОЛЬКО create
        created = await api.create_user_only(telegram_id)

        if not created:
            logger.error(f"❌ Ошибка создания RW: {telegram_id}")
            return False

        logger.info(f"✅ Создан в RW: {telegram_id}")
        return True

    finally:
        await api.close()

# =========================
# SYNC ВСЕХ ИЗ MYSQL → RW
# =========================
async def sync_all_users_to_rw():
    with Session() as session:
        users = session.query(User).all()

    logger.info(f"🚀 Найдено пользователей в MYSQL: {len(users)}")

    api = RemnawaveWrapper()
    await api._ensure_session()

    success = 0
    failed = 0
    skipped = 0

    try:
        for user in users:
            telegram_id = user.telegram_id

            try:
                existing = await api.find_user_by_telegram_id(telegram_id)

                if existing:
                    skipped += 1
                    logger.info(f"⏭ Уже есть: {telegram_id}")
                    continue

                created = await api.create_user_only(telegram_id)

                if created:
                    success += 1
                    logger.info(f"✅ Создан: {telegram_id}")
                else:
                    failed += 1

            except Exception as e:
                logger.error(f"❌ ERROR {telegram_id}: {e}")
                failed += 1

    finally:
        await api.close()

    logger.info(f"✅ Создано: {success}")
    logger.info(f"⚠️ Уже были: {skipped}")
    logger.info(f"❌ Ошибки: {failed}")

    return success, skipped, failed