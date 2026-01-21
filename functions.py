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
    # Пользователи
    # -------------------------
    async def create_user(self, telegram_id: int) -> Optional[Dict]:
        """
        Создаёт пользователя на Remnawave и возвращает реальные данные с рабочей ссылкой подписки.
        """
        await self._ensure_session()

        user = await get_user(telegram_id)
        if not user:
            return None

        expire_at = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        user_uuid = str(uuid.uuid4())  # создаём UUID на всякий случай

        payload = {
            "uuid": user_uuid,  # ⚡ добавляем UUID
            "username": f"user_{telegram_id}",
            "expireAt": expire_at,
            "traffic_limit": 0,
            "enabled": True,
            "note": f"tg:{telegram_id}",
        }

        async with self.session.post(self._url("/users/"), json=payload) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                logger.error("❌ Create user failed: %s", text)
                return None

            data = await resp.json()
            response = data.get("response", {})  # ✅ реальные данные пользователя

            # Берём рабочую ссылку подписки
            sub_url = response.get("subscriptionUrl")
            if not sub_url:
                sub_url = f"https://sub.shix-vpn.space/{user_uuid}"
                logger.warning(f"⚠️ subscriptionUrl не найден, используем UUID для пользователя {telegram_id}")

            response["sub_url"] = sub_url
            return response

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
