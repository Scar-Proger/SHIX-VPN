import aiohttp
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from config import config
from database import get_user


logger = logging.getLogger(__name__)

class RemnawaveWrapper:
    """Обёртка Remnawave API для работы с пользователями и подписками"""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    # -------------------------
    # Сессия
    # -------------------------
    async def _ensure_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {config.REMNAWAVE_API_KEY}",
                    "Content-Type": "application/json",
                }
            )

    def _url(self, path: str) -> str:
        return f"{config.REMNAWAVE_API_URL.rstrip('/')}/{path.lstrip('/')}"
    

    # -------------------------
    # Поиск пользователя
    # -------------------------
    async def find_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict]:
        await self._ensure_session()

        async with self.session.get(self._url("/users/")) as resp:
            if resp.status != 200:
                logger.error("❌ Не удалось получить список пользователей Remnawave")
                return None

            data = await resp.json()
            response = data.get("response", {})

            users = response.get("items", [])
            if not isinstance(users, list):
                logger.error(
                    f"❌ Неожиданный формат списка пользователей Remnawave. "
                    f"Ожидался список, получено: {type(users)}"
                )
                return None

            for user in users:
                if not isinstance(user, dict):
                    logger.warning(
                        f"⚠️ Пропущен пользователь с некорректным форматом: {type(user)}"
                    )
                    continue

                if user.get("note") == f"tg:{telegram_id}":
                    logger.info(f"🔎 Пользователь с Telegram ID {telegram_id} найден в Remnawave")
                    return user

        logger.info(f"ℹ️ Пользователь с Telegram ID {telegram_id} в Remnawave не найден")
        return None


    # -------------------------
    # Обновление пользователя (PATCH)
    # -------------------------
    async def update_user(self, user_uuid: str, payload: Dict) -> Optional[Dict]:
        await self._ensure_session()

        payload = {
            "uuid": user_uuid,
            **payload
        }

        async with self.session.patch(
            self._url("/users/"),
            json=payload
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"❌ Ошибка обновления пользователя {user_uuid}: {text}")
                return None

            data = await resp.json()
            return data.get("response")


    # -------------------------
    # CREATE или UPDATE пользователя
    # -------------------------
    async def create_user(self, telegram_id: int) -> Optional[Dict]:
        await self._ensure_session()

        expire_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

        existing_user = await self.find_user_by_telegram_id(telegram_id)

        # =========================
        # UPDATE
        # =========================
        if existing_user and "uuid" in existing_user:
            logger.info(f"🔁 Обновляем пользователя {telegram_id}")

            updated = await self.update_user(
                existing_user["uuid"],
                {
                    "expireAt": expire_at,
                    "enabled": True,
                    "trafficLimitBytes": 0,
                    "activeInternalSquads": [
                        config.REMNAWAVE_DEFAULT_SQUAD_ID
                    ],
                },
            )

            if not updated:
                return None

            updated["sub_url"] = updated.get("subscriptionUrl")
            return updated

        # =========================
        # CREATE
        # =========================
        logger.info(f"🆕 Создаём пользователя {telegram_id}")

        user_uuid = str(uuid.uuid4())

        async with self.session.post(
            self._url("/users/"),
            json={
                "uuid": user_uuid,
                "username": f"user_{telegram_id}",
                "expireAt": expire_at,
                "enabled": True,
                "trafficLimitBytes": 0,
                "note": f"tg:{telegram_id}",
            },
        ) as resp:
            if resp.status not in (200, 201):
                logger.error(f"❌ Ошибка создания пользователя {telegram_id}: {await resp.text()}")
                return None

            created = (await resp.json()).get("response")

        # ⚠️ ВАЖНО: squad назначаем ТОЛЬКО через PATCH
        await self.update_user(
            created["uuid"],
            {
                "activeInternalSquads": [
                    config.REMNAWAVE_DEFAULT_SQUAD_ID
                ]
            }
        )

        created["sub_url"] = created.get("subscriptionUrl")
        logger.info(f"✅ Пользователь {telegram_id} успешно создан и добавлен в squad")

        return created





    async def delete_user(self, user_id: str) -> bool:
        await self._ensure_session()
        async with self.session.delete(self._url(f"/users/{user_id}/")) as resp:
            return resp.status in (200, 204)

    async def get_user(self, user_id: str) -> Optional[Dict]:
        await self._ensure_session()
        async with self.session.get(self._url(f"/users/{user_id}/")) as resp:
            if resp.status != 200:
                return None
            return await resp.json()

    # -------------------------
    # Статистика и онлайн
    # -------------------------
    async def get_user_stats(self, user_id: str) -> Dict:
        await self._ensure_session()
        async with self.session.get(self._url(f"/users/{user_id}/stats/")) as resp:
            if resp.status != 200:
                return {"upload": 0, "download": 0}
            data = await resp.json()
            return {
                "upload": data.get("upload", 0),
                "download": data.get("download", 0),
            }

    async def get_online_users(self) -> int:
        await self._ensure_session()
        async with self.session.get(self._url("/stats/online/")) as resp:
            if resp.status != 200:
                return 0
            data = await resp.json()
            return int(data.get("online", 0))

    # -------------------------
    # Закрытие сессии
    # -------------------------
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


# -------------------------
# Вспомогательные функции
# -------------------------

async def create_vless_profile(telegram_id: int):
    """Создаёт профиль и возвращает данные с рабочей ссылкой"""
    api = RemnawaveWrapper()
    try:
        profile = await api.create_user(telegram_id)
        if not profile:
            logger.error(f"❌ Не удалось создать профиль для {telegram_id}")
            return None
        return profile
    finally:
        await api.close()

async def delete_client_by_id(user_id: str):
    api = RemnawaveWrapper()
    try:
        return await api.delete_user(user_id)
    finally:
        await api.close()

async def get_user_stats(user_id: str):
    api = RemnawaveWrapper()
    try:
        return await api.get_user_stats(user_id)
    finally:
        await api.close()

async def get_online_users():
    api = RemnawaveWrapper()
    try:
        return await api.get_online_users()
    finally:
        await api.close()
