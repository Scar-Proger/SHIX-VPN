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
import secrets
import os
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

PaymentResult = Literal["CONFIRMED", "PENDING", "CANCELED", "NOT_FOUND", "ERROR"]

# ==================================================
# Пути проекта и SSL-сертификат
# ==================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CA_PATH = os.path.join(BASE_DIR, "certs", "timeweb-root.crt")

if not os.path.exists(CA_PATH):
    raise RuntimeError(f"❌ CA certificate not found: {CA_PATH}")

# ==================================================
# Настройки MySQL (MyJino / Timeweb / Cloud)
# ==================================================
DB_HOST = "78df91fc76d0aaa3e2e1071b.twc1.net"
DB_NAME = "default_db"
DB_USER = "gen_user"
DB_PASSWORD = quote_plus("71VWf@$={n!=l8")

DATABASE_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:3306/{DB_NAME}?charset=utf8mb4"
)

# ==================================================
# SQLAlchemy setup (ВАЖНО: SSL через CA)
# ==================================================
engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=3600,
    connect_args={
        "ssl": {
            "ca": CA_PATH
        }
    }
)

Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# ==================================================
# Утилиты
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
    referrer_id: int = None,
    language: str = "ru"
):
    with Session() as session:
        user = User(
            telegram_id=telegram_id,
            full_name=full_name,
            username=username,
            sub_id=generate_sub_id(),
            subscription_end=now_local() + timedelta(days=30),
            is_admin=is_admin,
            referrer_id=referrer_id,
            referrals_count=0,
            language=language
        )
        session.add(user)
        session.commit()
        session.refresh(user)  # 🔥 ВАЖНО
        logger.info(f"✅ Новый пользователь создан: {telegram_id}")
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
# Статические профили
# ==================================================
async def create_static_profile(name: str, vless_url: str):
    with Session() as session:
        profile = StaticProfile(name=name, vless_url=vless_url)
        session.add(profile)
        session.commit()
        return profile

async def get_static_profiles():
    with Session() as session:
        return session.query(StaticProfile).all()

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
    months: int
) -> Payment:
    """
    Создаёт платёж в БД со статусом PENDING
    """
    with Session() as session:
        payment = Payment(
            user_id=user_id,
            transaction_id=transaction_id,
            amount=amount,
            months=months,
            status="PENDING"
        )
        session.add(payment)
        session.commit()
        session.refresh(payment)
        return payment


async def process_payment_result(
    transaction_id: str,
    payment_status: str
) -> PaymentResult:
    """
    Обновляет платёж и подписку пользователя по статусу платежа
    """
    with Session() as session:
        payment = session.query(Payment).filter_by(
            transaction_id=transaction_id
        ).first()

        if not payment:
            return "NOT_FOUND"

        if payment_status == "CONFIRMED":
            if payment.status != "CONFIRMED":
                user = session.query(User).get(payment.user_id)
                if not user:
                    return "ERROR"

                now = now_local()
                base_date = max(user.subscription_end or now, now)
                user.subscription_end = base_date + timedelta(days=30 * payment.months)

                payment.status = "CONFIRMED"
                payment.confirmed_at = now

                session.commit()

            return "CONFIRMED"

        if payment_status == "PENDING":
            return "PENDING"

        if payment_status == "CANCELED":
            payment.status = "CANCELED"
            session.commit()
            return "CANCELED"

        return "ERROR"