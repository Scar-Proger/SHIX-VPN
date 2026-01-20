from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, func
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timedelta
import logging
import secrets

logger = logging.getLogger(__name__)

def generate_sub_id() -> str:
    return secrets.token_urlsafe(12).replace("-", "").replace("_", "")

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True)
    referrer_id = Column(Integer, nullable=True, index=True)  # ID пригласившего пользователя
    referrals_count = Column(Integer, default=0)              # количество приглашённых друзей
    full_name = Column(String)
    username = Column(String)
    sub_id = Column(String, unique=True, index=True)
    registration_date = Column(DateTime, default=datetime.utcnow)
    subscription_end = Column(DateTime)
    vless_profile_id = Column(String)
    vless_profile_data = Column(String)
    is_admin = Column(Boolean, default=False)
    notified = Column(Boolean, default=False)
    
    # Новое поле для текущего промо
    active_promo_id = Column(Integer, nullable=True)         # ID активного промо
    active_discount = Column(Integer, default=0)             # текущая скидка %

# ------------------------------
# Таблица для промокодов
# ------------------------------
class PromoCode(Base):
    __tablename__ = 'promo_codes'
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, index=True, nullable=False)
    bonus_days = Column(Integer, default=0)           # оставляем для совместимости, можно 0
    discount_percent = Column(Integer, default=0)     # новая скидка в %
    is_active = Column(Boolean, default=True)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

# ------------------------------
# История использования промокодов пользователями
# ------------------------------
class UserPromoCode(Base):
    __tablename__ = "user_promo_codes"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)   # ID пользователя из таблицы User
    promo_id = Column(Integer, index=True, nullable=False)  # ID промокода из таблицы PromoCode
    used_at = Column(DateTime, default=datetime.utcnow)     # Когда был использован

class StaticProfile(Base):
    __tablename__ = 'static_profiles'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    vless_url = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

engine = create_engine('sqlite:///users.db', echo=False)
Session = sessionmaker(bind=engine)

async def init_db():
    Base.metadata.create_all(engine)
    logger.info("✅ Database tables created")

async def get_user(telegram_id: int):
    with Session() as session:
        return session.query(User).filter_by(telegram_id=telegram_id).first()

async def create_user(telegram_id: int, full_name: str, username: str = None,
    is_admin: bool = False, referrer_id: int = None):
    with Session() as session:
        user = User(
            telegram_id=telegram_id,
            full_name=full_name,
            username=username,
            sub_id=generate_sub_id(),
            subscription_end=datetime.utcnow() + timedelta(days=3),
            is_admin=is_admin,
            referrer_id=referrer_id,
            referrals_count=0
        )
        session.add(user)
        session.commit()
        logger.info(f"✅ New user created: {telegram_id}")
        return user

async def delete_user_profile(telegram_id: int):
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            user.vless_profile_data = None
            user.notified = False
            session.commit()
            logger.info(f"✅ User profile deleted: {telegram_id}")

async def update_subscription(telegram_id: int, months: int):
    """Обновляет подписку с учетом текущего состояния"""
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            now = datetime.utcnow()
            # Если подписка активна, добавляем к текущей дате окончания
            if user.subscription_end > now:
                user.subscription_end += timedelta(days=months * 30)
            else:
                # Если подписка истекла, начинаем с текущей даты
                user.subscription_end = now + timedelta(days=months * 30)
            
            # Сбрасываем флаг уведомления
            user.notified = False
            session.commit()
            logger.info(f"✅ Subscription updated for {telegram_id}: +{months} months")
            return True
        return False

async def get_all_users(with_subscription: bool = None):
    with Session() as session:
        query = session.query(User)
        if with_subscription is not None:
            if with_subscription:
                query = query.filter(User.subscription_end > datetime.utcnow())
            else:
                query = query.filter(User.subscription_end <= datetime.utcnow())
        return query.all()

async def create_static_profile(name: str, vless_url: str):
    with Session() as session:
        profile = StaticProfile(name=name, vless_url=vless_url)
        session.add(profile)
        session.commit()
        logger.info(f"✅ Static profile created: {name}")
        return profile

async def get_static_profiles():
    with Session() as session:
        return session.query(StaticProfile).all()

async def get_user_stats():
    with Session() as session:
        total = session.query(func.count(User.id)).scalar()
        with_sub = session.query(func.count(User.id)).filter(User.subscription_end > datetime.utcnow()).scalar()
        without_sub = total - with_sub
        return total, with_sub, without_sub
    
# ------------------------------
# Функции работы с промокодами (только со скидкой)
# ------------------------------
async def create_or_update_promo_code(code: str, discount_percent: int = 0, max_uses: int = 1):
    """Создает новый промокод или обновляет существующий"""
    with Session() as session:
        code_upper = code.upper()
        promo = session.query(PromoCode).filter_by(code=code_upper).first()
        
        if promo:
            # Обновляем существующий промокод
            promo.discount_percent = discount_percent
            promo.max_uses = max_uses
            promo.is_active = True
            promo.used_count = 0
            session.commit()
        else:
            # Создаем новый промокод
            promo = PromoCode(
                code=code_upper,
                discount_percent=discount_percent,
                max_uses=max_uses,
                bonus_days=0  # оставляем для совместимости
            )
            session.add(promo)
            session.commit()
        
        return {
            "code": promo.code,
            "discount_percent": promo.discount_percent,
            "max_uses": promo.max_uses,
            "used_count": promo.used_count,
            "is_active": promo.is_active,
            "id": promo.id
        }


async def use_promo_code(code: str):
    """Отметить промокод как использованный"""
    with Session() as session:
        promo = session.query(PromoCode).filter_by(code=code.upper(), is_active=True).first()
        if not promo:
            return None
        
        promo.used_count += 1
        if promo.used_count >= promo.max_uses:
            promo.is_active = False  # деактивируем, если достигли лимита
        session.commit()
        return {
            "code": promo.code,
            "discount_percent": promo.discount_percent,
            "used_count": promo.used_count,
            "max_uses": promo.max_uses,
            "is_active": promo.is_active,
            "id": promo.id
        }


async def apply_promo_code(user_id: int, code: str):
    """Применить промокод к пользователю (только скидка)"""
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            return {"error": "Пользователь не найден"}

        promo = session.query(PromoCode).filter_by(code=code.upper()).first()
        if not promo:
            return {"error": "Промокод не найден"}

        if not promo.is_active:
            return {"error": "Промокод больше недоступен"}

        # Проверяем, использовал ли пользователь этот промо раньше
        used = session.query(UserPromoCode).filter_by(user_id=user.id, promo_id=promo.id).first()
        if used:
            return {"error": "Вы уже использовали этот промокод"}

        # Удаляем старый промо у пользователя (если есть)
        if user.active_promo_id:
            old_usage = session.query(UserPromoCode).filter_by(user_id=user.id, promo_id=user.active_promo_id).first()
            if old_usage:
                session.delete(old_usage)

        # Применяем новый промо
        usage = UserPromoCode(user_id=user.id, promo_id=promo.id)
        session.add(usage)

        user.active_promo_id = promo.id
        user.active_discount = promo.discount_percent

        # Увеличиваем использований и проверяем активность
        promo.used_count += 1
        promo.is_active = promo.used_count < promo.max_uses

        session.commit()
        session.refresh(promo)
        session.refresh(user)

        return {
            "success": True,
            "discount_percent": promo.discount_percent,
            "is_active": promo.is_active,
            "used_count": promo.used_count,
            "max_uses": promo.max_uses,
            "promo_id": promo.id
        }

# ------------------------------
# Удаление промокода из базы
# ------------------------------
async def delete_promocode(code: str):
    """Удаляет промокод по его коду из базы данных"""
    with Session() as session:
        promo = session.query(PromoCode).filter_by(code=code.upper()).first()
        if not promo:
            return False  # промокод не найден
        session.delete(promo)
        session.commit()
        return True


async def get_all_promocodes_list():
    """Возвращает все промокоды как список словарей, сортировка по id"""
    with Session() as session:
        promos = session.query(PromoCode).order_by(PromoCode.id).all()
        return [
            {
                "id": p.id,
                "code": p.code,
                "discount_percent": p.discount_percent,
                "max_uses": p.max_uses,
                "used_count": p.used_count,
                "is_active": p.is_active
            } for p in promos
        ]

    """Возвращает все промокоды как список словарей, сортировка по id"""
    with Session() as session:
        promos = session.query(PromoCode).order_by(PromoCode.id).all()
        return [
            {
                "id": p.id,
                "code": p.code,
                "bonus_days": p.bonus_days,
                "max_uses": p.max_uses,
                "used_count": p.used_count,
                "is_active": p.is_active
            } for p in promos
        ]

    """Список всех активных промокодов"""
    with Session() as session:
        return session.query(PromoCode).filter_by(is_active=True).all()