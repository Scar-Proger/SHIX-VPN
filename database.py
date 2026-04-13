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
import asyncio
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timedelta
from typing import Literal
import logging
import secrets
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
def generate_sub_id() -> str:
    return secrets.token_urlsafe(12).replace("-", "").replace("_", "")

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

async def create_user(
    telegram_id: int,
    full_name: str,
    username: str = None,
    is_admin: bool = False,
    referrer_telegram_id: int = None,  # передаём Telegram ID реферера
    language: str = "ru"
):
    with Session() as session:
        # ---------------------------
        # Находим реферера в базе (по Telegram ID)
        # ---------------------------
        referrer_id_db = None
        if referrer_telegram_id:
            referrer = session.query(User).filter_by(telegram_id=referrer_telegram_id).first()
            if referrer:
                referrer_id_db = referrer.id

        # ---------------------------
        # Создаём пользователя
        # ---------------------------
        user = User(
            telegram_id=telegram_id,
            full_name=full_name,
            username=username,
            sub_id=generate_sub_id(),
            subscription_end=now_local() + timedelta(days=2),
            is_admin=is_admin,
            referrer_id=referrer_id_db,
            referrals_count=0,
            language=language
        )
        session.add(user)
        session.commit()
        session.refresh(user)  # 🔥 ВАЖНО

        # ---------------------------
        # Создаём баланс пользователя (персики + звёзды)
        # ---------------------------
        balance = UserBalance(
            user_id=user.id,
            amount=0,
            stars=0
        )
        session.add(balance)
        session.commit()
        session.refresh(balance)

        logger.info(f"✅ Новый пользователь создан: {telegram_id} с балансом 0 и звёздами 0")

        # ---------------------------
        # Обновляем рефереру количество рефералов
        # ---------------------------
        if referrer_id_db:
            referrer.referrals_count = session.query(User).filter_by(referrer_id=referrer_id_db).count()
            session.commit()
            logger.info(f"🔹 Обновлён счётчик рефералов для {referrer_telegram_id}: {referrer.referrals_count}")

        return user

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

async def get_all_users(with_subscription: bool = None):
    with Session() as session:
        query = session.query(User)
        if with_subscription is not None:
            if with_subscription:
                query = query.filter(User.subscription_end > now_local())
            else:
                query = query.filter(User.subscription_end <= now_local())
        return query.all()

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
                data = await resp.json()
                users = data.get("response", {}).get("users", [])
                total = data.get("response", {}).get("total", 0)

                for u in users:
                    tg_id = None

                    if u.get("note", "").startswith("tg:"):
                        tg_id = int(u["note"].replace("tg:", ""))
                    elif u.get("username", "").startswith("user_"):
                        tg_id = int(u["username"].replace("user_", ""))

                    if tg_id:
                        rw_map[tg_id] = u

                start += size
                if start >= total:
                    break

        # =========================
        # 2. ГРУЗИМ БД
        # =========================
        with Session() as session:
            users = session.query(User).all()

        # =========================
        # 3. СИНХРА
        # =========================
        for user in users:
            try:
                rw_user = rw_map.get(user.telegram_id)

                # ❌ нет в RW → удаляем
                if not rw_user:
                    with Session() as session:
                        db_user = session.query(User).get(user.id)
                        if db_user:
                            session.delete(db_user)
                            session.commit()

                    deleted += 1
                    continue

                # ✅ есть → обновляем
                expire_at = rw_user.get("expireAt")

                if expire_at:
                    expire_dt = datetime.fromisoformat(
                        expire_at.replace("Z", "+00:00")
                    ) + timedelta(hours=3)

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
  


