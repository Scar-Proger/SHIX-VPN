from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    BigInteger,
    func
)
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timedelta
from typing import Literal
import logging
import os

logger = logging.getLogger(__name__)

PaymentResult = Literal["CONFIRMED", "PENDING", "CANCELED", "NOT_FOUND", "ERROR"]

# ==================================================
# DATABASE (Railway)
# ==================================================
DATABASE_URL = os.getenv("MYSQL_URL")

if not DATABASE_URL:
    raise RuntimeError("❌ MYSQL_URL not set in environment")

# fix для SQLAlchemy
DATABASE_URL = DATABASE_URL.replace("mysql://", "mysql+pymysql://")

print("DB:", DATABASE_URL)

# ==================================================
# SQLAlchemy
# ==================================================
engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=3600,
)

Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# ==================================================
# Utils
# ==================================================

def now_local():
    return datetime.utcnow() + timedelta(hours=3)


# ==================================================
# Таблица пользователей
# ==================================================
class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    referrer_id = Column(Integer, nullable=True, index=True)
    referrals_count = Column(Integer, default=0)

    full_name = Column(String(255))
    username = Column(String(255))
    sub_id = Column(String(255), unique=True, index=True)

    registration_date = Column(DateTime, default=now_local)
    subscription_end = Column(DateTime)

    vless_profile_id = Column(String(255))
    vless_profile_data = Column(String(2048))

    is_admin = Column(Boolean, default=False)
    notified = Column(Boolean, default=False)

    active_promo_id = Column(Integer, nullable=True)
    active_discount = Column(Integer, default=0)

    language = Column(String(10), default="ru")


class UserBalance(Base):
    __tablename__ = "user_balances"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    amount = Column(Integer, default=0)  # Текущий баланс
    stars = Column(Integer, default=0)   # Новый баланс звезд
    created_at = Column(DateTime, default=now_local)
    updated_at = Column(DateTime, default=now_local, onupdate=now_local)


class UserBalanceHistory(Base):
    __tablename__ = "user_balance_history"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    change = Column(Integer)        # +100, -50 персиков
    stars_change = Column(Integer, default=0)  # +5, -2 звезд
    reason = Column(String(255))   # "Пополнение", "Списание за подписку"
    payment_id = Column(Integer, index=True, nullable=True)
    created_at = Column(DateTime, default=now_local)


# ==================================================
# Таблица промокодов
# ==================================================
class PromoCode(Base):
    __tablename__ = 'promo_codes'

    id = Column(Integer, primary_key=True)
    code = Column(String(255), unique=True, index=True, nullable=False)
    bonus_days = Column(Integer, default=0)
    discount_percent = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=now_local)


# ==================================================
# История использования промокодов
# ==================================================
class UserPromoCode(Base):
    __tablename__ = "user_promo_codes"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    promo_id = Column(Integer, index=True, nullable=False)
    used_at = Column(DateTime, default=now_local)


# ==================================================
# Статические профили
# ==================================================
class StaticProfile(Base):
    __tablename__ = 'static_profiles'

    id = Column(Integer, primary_key=True)
    name = Column(String(255))
    vless_url = Column(String(2048))
    created_at = Column(DateTime, default=now_local)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)

    user_id = Column(Integer, index=True, nullable=False)

    transaction_id = Column(String(64), unique=True, index=True, nullable=False)

    amount = Column(Integer, nullable=False)
    months = Column(Integer, nullable=False)

    status = Column(String(32), default="PENDING")  # PENDING / CONFIRMED / CANCELED
    created_at = Column(DateTime, default=now_local)
    confirmed_at = Column(DateTime, nullable=True)

    # 🔥 Сохраняем ссылку на оплату
    pay_url = Column(String(2048), nullable=True)
    pay_url_created_at = Column(DateTime, default=now_local)


# ==================================================
# Инициализация базы
# ==================================================
async def init_db():
    Base.metadata.create_all(engine)
    logger.info("✅ Таблицы MySQL успешно созданы")

# ==================================================
# Пользователи
# ==================================================
async def get_user(telegram_id: int):
    with Session() as session:
        return session.query(User).filter_by(telegram_id=telegram_id).first()
    
async def get_all_users():
    with Session() as session:
        return session.query(User).all()



async def create_user(
    telegram_id: int,
    full_name: str,
    username: str = None,
    is_admin: bool = False,
    referrer_telegram_id: int = None,
    language: str = "ru"
):
    from functions import RemnawaveWrapper

    api = RemnawaveWrapper()

    try:
        await api._ensure_session()

        # =========================
        # 1. Remnawave user create
        # =========================
        rw_user = await api.create_user(telegram_id)

        if not rw_user:
            logger.error(f"❌ RW create failed tg={telegram_id}")
            return None

        sub_url = rw_user.get("subscriptionUrl")
        rw_uuid = rw_user.get("uuid")

        if not sub_url or not rw_uuid:
            logger.error(f"❌ RW invalid data tg={telegram_id}")
            return None

        short_uuid = sub_url.rstrip("/").split("/")[-1]

        # =========================
        # 2. DB session
        # =========================
        with Session() as session:

            # ---------------------------
            # реферер (НЕ ТРОГАЕМ ЛОГИКУ)
            # ---------------------------
            referrer_id_db = None
            if referrer_telegram_id:
                referrer = session.query(User).filter_by(
                    telegram_id=referrer_telegram_id
                ).first()
                if referrer:
                    referrer_id_db = referrer.id

            # ---------------------------
            # создаём пользователя (ВАША ЛОГИКА + FIX)
            # ---------------------------
            user = User(
                telegram_id=telegram_id,
                full_name=full_name,
                username=username,
                sub_id=short_uuid,  # 🔥 FIX: теперь не None
                subscription_end=now_local() + timedelta(days=2),
                is_admin=is_admin,
                referrer_id=referrer_id_db,
                referrals_count=0,
                language=language,

                # 🔥 FIX: синхронизация с Remnawave
                vless_profile_id=rw_uuid,
                vless_profile_data=sub_url
            )

            session.add(user)
            session.commit()
            session.refresh(user)

            # ---------------------------
            # баланс (НЕ ТРОГАЕМ)
            # ---------------------------
            balance = UserBalance(
                user_id=user.id,
                amount=0,
                stars=0
            )

            session.add(balance)
            session.commit()

            # ---------------------------
            # реферальная система (НЕ ТРОГАЕМ)
            # ---------------------------
            if referrer_id_db:
                referrer.referrals_count = session.query(User).filter_by(
                    referrer_id=referrer_id_db
                ).count()
                session.commit()

                logger.info(
                    f"🔹 Ref updated tg={referrer_telegram_id} "
                    f"count={referrer.referrals_count}"
                )

            logger.info(
                f"✅ USER CREATED tg={telegram_id} "
                f"sub_id={short_uuid}"
            )

            return user

    finally:
        await api.close()

async def delete_user_profile(telegram_id: int):
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            user.vless_profile_data = None
            user.notified = False
            session.commit()
            logger.info(f"✅ Профиль пользователя удалён: {telegram_id}")

async def update_subscription(telegram_id: int, months: int):
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            return False

        now = now_local()
        if user.subscription_end and user.subscription_end > now:
            user.subscription_end += timedelta(days=months * 30)
        else:
            user.subscription_end = now + timedelta(days=months * 30)

        user.notified = False
        session.commit()
        logger.info(f"✅ Подписка продлена: {telegram_id}")
        return True


















async def sync_shortuuid_to_mysql():
    from functions import RemnawaveWrapper

    logger.info("🚀 [SYNC] sync_shortuuid_to_mysql STARTED")

    api = RemnawaveWrapper()

    success = 0
    failed = 0
    skipped = 0

    try:
        await api._ensure_session()
        logger.info("🌐 [SYNC] Remnawave session READY")

        with Session() as session:
            users = session.query(User).all()

            logger.info(f"📦 [SYNC] Loaded users from DB: {len(users)}")

            for user in users:
                logger.info(f"\n🔎 [SYNC] Processing tg_id={user.telegram_id}")

                try:
                    rw_user = await api.find_user_by_telegram_id(user.telegram_id)

                    if not rw_user:
                        logger.warning(f"❌ [SYNC] RW user NOT FOUND tg_id={user.telegram_id}")
                        failed += 1
                        continue

                    logger.info(f"✅ [SYNC] RW user FOUND tg_id={user.telegram_id}")
                    logger.debug(f"🧾 [SYNC] RW RAW DATA: {rw_user}")

                    short_uuid = rw_user.get("shortUuid")
                    subscription_url = rw_user.get("subscriptionUrl")
                    rw_uuid = rw_user.get("uuid")

                    logger.info(
                        f"📡 [SYNC] extracted -> "
                        f"uuid={rw_uuid}, shortUuid={short_uuid}, sub={subscription_url}"
                    )

                    if not rw_uuid:
                        logger.error(f"💥 [SYNC] UUID missing tg_id={user.telegram_id}")
                        failed += 1
                        continue

                    # =========================
                    # UPDATE FIELDS
                    # =========================
                    old_sub = user.sub_id
                    old_uuid = user.vless_profile_id

                    if short_uuid:
                        user.sub_id = short_uuid
                    elif subscription_url:
                        user.sub_id = subscription_url.rstrip("/").split("/")[-1]
                    else:
                        logger.warning(f"⚠️ [SYNC] no subscription_url tg_id={user.telegram_id}")
                        skipped += 1
                        continue

                    user.vless_profile_id = rw_uuid
                    user.vless_profile_data = subscription_url

                    session.add(user)

                    logger.info(
                        f"💾 [SYNC] UPDATED tg_id={user.telegram_id} "
                        f"sub_id: {old_sub} → {user.sub_id} | "
                        f"uuid: {old_uuid} → {rw_uuid}"
                    )

                    success += 1

                except Exception as e:
                    logger.exception(f"🔥 [SYNC] ERROR tg_id={user.telegram_id}: {e}")
                    failed += 1

            session.commit()
            logger.info("💾 [SYNC] DB COMMIT DONE")

    finally:
        await api.close()
        logger.info("🔌 [SYNC] Remnawave session CLOSED")

    logger.info(
        f"\n🏁 [SYNC DONE] success={success}, failed={failed}, skipped={skipped}"
    )

    return success, failed







# ==================================================
# Статистика
# ==================================================
async def get_user_stats():
    with Session() as session:
        total = session.query(func.count(User.id)).scalar()
        with_sub = session.query(func.count(User.id)) \
            .filter(User.subscription_end > now_local()).scalar()
        without_sub = total - with_sub
        return total, with_sub, without_sub
    
async def delete_user_completely(telegram_id: int):
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            session.delete(user)
            session.commit()
            logger.info(f"🗑 Пользователь полностью удалён из БД: {telegram_id}")

async def sync_from_remnawave_to_db():
    from functions import RemnawaveWrapper

    api = RemnawaveWrapper()

    success = 0
    deleted = 0
    failed = 0

    try:
        await api._ensure_session()

        # =========================
        # 1. ГРУЗИМ ВСЕХ RW ЮЗЕРОВ
        # =========================
        rw_map = {}

        start = 0
        size = 200

        while True:
            async with api.session.get(
                api._url("/users/"),
                params={"start": start, "size": size}
            ) as resp:

                if resp.status != 200:
                    logger.error(f"❌ RW users fetch error: {resp.status}")
                    break

                data = await resp.json()
                response = data.get("response", {})

                users = response.get("users", [])
                total = response.get("total", 0)

                for u in users:
                    tg_id = None

                    note = u.get("note") or ""
                    username = u.get("username") or ""

                    if note.startswith("tg:"):
                        try:
                            tg_id = int(note.replace("tg:", ""))
                        except:
                            pass

                    elif username.startswith("user_"):
                        try:
                            tg_id = int(username.replace("user_", ""))
                        except:
                            pass

                    if tg_id:
                        rw_map[tg_id] = u

                start += size
                if start >= total or not users:
                    break

        # =========================
        # 2. ГРУЗИМ БД
        # =========================
        with Session() as session:
            db_users = session.query(User).all()

        # =========================
        # 3. СИНХРОНИЗАЦИЯ
        # =========================
        for user in db_users:
            try:
                rw_user = rw_map.get(user.telegram_id)

                # ❌ НЕТ В REMNAWAVE → УДАЛЯЕМ ИЗ MYSQL
                if not rw_user:
                    with Session() as session:
                        db_user = session.query(User).get(user.id)
                        if db_user:
                            session.delete(db_user)
                            session.commit()

                    deleted += 1
                    continue

                # ✅ ЕСТЬ → ОБНОВЛЯЕМ ПОДПИСКУ
                expire_at = rw_user.get("expireAt")

                if expire_at:
                    expire_dt = datetime.fromisoformat(
                        expire_at.replace("Z", "+00:00")
                    )

                    with Session() as session:
                        db_user = session.query(User).get(user.id)

                        if db_user:
                            db_user.subscription_end = expire_dt
                            db_user.vless_profile_id = rw_user.get("uuid")
                            db_user.vless_profile_data = rw_user.get("subscriptionUrl")
                            session.commit()

                    success += 1
                else:
                    failed += 1

            except Exception as e:
                logger.error(f"❌ sync error {user.telegram_id}: {e}")
                failed += 1

    finally:
        await api.close()

    return success, deleted, failed

# ==================================================
# Промокоды
# ==================================================
async def create_or_update_promo_code(
    code: str,
    discount_percent: int,
    max_uses: int
) -> dict:
    with Session() as session:
        promo = session.query(PromoCode).filter_by(code=code).first()

        if promo:
            promo.discount_percent = discount_percent
            promo.max_uses = max_uses
        else:
            promo = PromoCode(
                code=code,
                discount_percent=discount_percent,
                max_uses=max_uses
            )
            session.add(promo)

        session.commit()

        # 🔥 ВОЗВРАЩАЕМ ТОЛЬКО ДАННЫЕ
        return {
            "code": promo.code,
            "discount_percent": promo.discount_percent,
            "max_uses": promo.max_uses,
        }

async def use_promo_code(code: str):
    with Session() as session:
        promo = session.query(PromoCode).filter_by(
            code=code.upper(),
            is_active=True
        ).first()

        if not promo:
            return None

        promo.used_count += 1
        if promo.used_count >= promo.max_uses:
            promo.is_active = False

        session.commit()
        return promo

async def apply_promo_code(user_id: int, code: str):
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return {"error": "user_not_found"}

        promo = session.query(PromoCode).filter_by(code=code.upper()).first()
        if not promo:
            return {"error": "promo_not_found"}

        # ❌ пользователь уже использовал
        used = session.query(UserPromoCode).filter_by(
            user_id=user.id,
            promo_id=promo.id
        ).first()
        if used:
            return {"error": "promo_already_used"}

        # ❌ промокод закончился
        if not promo.is_active or promo.used_count >= promo.max_uses:
            return {"error": "promo_expired"}


        # ✅ применяем
        session.add(UserPromoCode(user_id=user.id, promo_id=promo.id))

        user.active_promo_id = promo.id
        user.active_discount = promo.discount_percent

        promo.used_count += 1
        promo.is_active = promo.used_count < promo.max_uses

        session.commit()

        return {
            "success": True,
            "code": promo.code,
            "discount_percent": promo.discount_percent
        }

async def delete_promocode(code: str):
    with Session() as session:
        promo = session.query(PromoCode).filter_by(code=code.upper()).first()
        if not promo:
            return False
        session.delete(promo)
        session.commit()
        return True

async def get_all_promocodes_list():
    with Session() as session:
        return session.query(PromoCode).order_by(PromoCode.id).all()
    


async def create_payment(
    user_id: int,
    transaction_id: str,
    amount: int,
    months: int,
    pay_url: str  # добавили сюда
) -> Payment:
    with Session() as session:
        payment = Payment(
            user_id=user_id,
            transaction_id=transaction_id,
            amount=amount,
            months=months,
            status="PENDING",
            pay_url=pay_url
        )
        session.add(payment)
        session.commit()
        session.refresh(payment)
        return payment 


async def process_payment_result(
    transaction_id: str,
    payment_status: str
) -> str:
    """
    Обновляет статус платежа и подписку пользователя.
    Возвращает статус: CONFIRMED, PENDING, CANCELED, ERROR, NOT_FOUND
    """
    from functions import sync_remnawave_expire  

    with Session() as session:
        payment = session.query(Payment).filter_by(transaction_id=transaction_id).first()

        if not payment:
            return "NOT_FOUND"

        # ------------------------
        # Платёж подтверждён
        # ------------------------
        if payment_status == "CONFIRMED" and payment.status != "CONFIRMED":
            user = session.query(User).get(payment.user_id)
            if not user:
                return "ERROR"

            now = now_local()
            base_date = max(user.subscription_end or now, now)

            new_end = base_date + timedelta(days=30 * payment.months)
            user.subscription_end = new_end

            payment.status = "CONFIRMED"
            payment.confirmed_at = now
            session.commit()

            # Синхронизация с Remnawave
            success = await sync_remnawave_expire(
                telegram_id=user.telegram_id,
                new_end=new_end
            )
            if not success:
                logger.error(f"❌ Remnawave sync failed for tg={user.telegram_id}")

            return "CONFIRMED"

        # ------------------------
        # Платёж отменён
        # ------------------------
        if payment_status == "CANCELED" and payment.status != "CANCELED":
            payment.status = "CANCELED"
            session.commit()
            return "CANCELED"

        # ------------------------
        # PENDING оставляем
        # ------------------------
        if payment_status == "PENDING":
            return "PENDING"

        return "ERROR"


async def get_or_create_payment(user_id: int, amount: int, months: int) -> Payment:
    """
    Возвращает действующий PENDING платеж с ссылкой <30 мин
    или создаёт новый, если старый просрочен.
    """
    from payment.platega_payment import create_platega_payment

    with Session() as session:
        now = now_local()

        # Берём последнюю PENDING платежку пользователя с нужной суммой и месяцами
        payment = session.query(Payment)\
            .filter_by(user_id=user_id, amount=amount, months=months, status="PENDING")\
            .order_by(Payment.pay_url_created_at.desc())\
            .first()

        if payment and payment.pay_url and payment.pay_url_created_at + timedelta(minutes=30) > now:
            # Ссылка ещё действительна
            return payment

        # Иначе создаём новый платеж (не перезаписываем старый)
        payment_data = await create_platega_payment(amount)
        if not payment_data:
            raise RuntimeError("Ошибка создания платежа")

        new_payment = Payment(
            user_id=user_id,
            transaction_id=payment_data["transaction_id"],
            amount=amount,
            months=months,
            status="PENDING",
            pay_url=payment_data["pay_url"],
            pay_url_created_at=now
        )
        session.add(new_payment)
        session.commit()
        session.refresh(new_payment)
        return new_payment
  


