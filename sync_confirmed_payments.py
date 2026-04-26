import logging
from datetime import timedelta

from database import Session, User, Payment, now_local
from functions import sync_remnawave_expire

logger = logging.getLogger(__name__)


async def sync_confirmed_payments():
    success = 0
    skipped = 0
    failed = 0

    with Session() as session:
        payments = session.query(Payment).filter_by(status="CONFIRMED").all()

        logger.info(f"💰 CONFIRMED PAYMENTS: {len(payments)}")

        for payment in payments:
            try:
                user = session.query(User).get(payment.user_id)

                if not user:
                    logger.error(f"❌ USER NOT FOUND id={payment.user_id}")
                    failed += 1
                    continue

                if not payment.confirmed_at:
                    logger.warning(f"⚠️ NO confirmed_at payment={payment.id}")
                    skipped += 1
                    continue

                # =========================
                # ❗ ПРОВЕРКА: уже применён?
                # =========================
                if user.subscription_end and user.subscription_end >= payment.confirmed_at:
                    skipped += 1
                    continue

                now = now_local()

                # =========================
                # 🔥 ЛОГИКА ПРОДЛЕНИЯ
                # =========================
                if user.subscription_end and user.subscription_end > now:
                    base_date = user.subscription_end
                else:
                    base_date = payment.confirmed_at

                new_end = base_date + timedelta(days=payment.months * 30)

                old_end = user.subscription_end

                # =========================
                # UPDATE MYSQL
                # =========================
                user.subscription_end = new_end

                # =========================
                # UPDATE REMNAWAVE
                # =========================
                rw_ok = await sync_remnawave_expire(
                    telegram_id=user.telegram_id,
                    new_end=new_end
                )

                if not rw_ok:
                    logger.error(f"❌ RW UPDATE FAILED tg={user.telegram_id}")
                    failed += 1
                    continue

                logger.info(
                    f"✅ UPDATED tg={user.telegram_id}\n"
                    f"{old_end} → {new_end}"
                )

                success += 1

            except Exception as e:
                logger.error(f"❌ ERROR payment_id={payment.id}: {e}")
                failed += 1

        session.commit()
        logger.info("💾 DB COMMIT DONE")

    logger.info(
        f"🏁 DONE\n"
        f"✅ success: {success}\n"
        f"⚠️ skipped: {skipped}\n"
        f"❌ failed: {failed}"
    )

    return success, skipped, failed