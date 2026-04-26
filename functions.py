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
    

    async def create_user_only(self, telegram_id: int) -> Optional[Dict]:
        await self._ensure_session()

        expire_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

        user_uuid = str(uuid.uuid4())

        async with self.session.post(
            self._url("/users/"),
            json={
                "uuid": user_uuid,
                "username": f"user_{telegram_id}",
                "expireAt": expire_at,
                "enabled": True,
                "trafficLimitBytes": 1073741824,
                "trafficLimitStrategy": "DAY",
                "note": f"tg:{telegram_id}",
            },
        ) as resp:

            if resp.status not in (200, 201):
                logger.error(f"❌ CREATE ONLY ERROR {telegram_id}: {await resp.text()}")
                return None

            created = (await resp.json()).get("response")

        # squad отдельно
        await self.update_user(
            created["uuid"],
            {
                "activeInternalSquads": [
                    config.REMNAWAVE_DEFAULT_SQUAD_ID
                ]
            }
        )

        created["sub_url"] = created.get("subscriptionUrl")

        return created
    

    # -------------------------
    # Поиск пользователя
    # -------------------------
    async def find_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict]:
        await self._ensure_session()

        start = 0
        size = 100

        while True:
            async with self.session.get(
                self._url("/users/"),
                params={
                    "start": start,
                    "size": size,
                    "filters": "[]",
                    "sorting": "[]",
                },
            ) as resp:
                if resp.status != 200:
                    logger.error("❌ Remnawave: не удалось получить список пользователей")
                    return None

                data = await resp.json()
                response = data.get("response", {})
                users = response.get("users", [])
                total = response.get("total", 0)

                if not users:
                    break

                for user in users:
                    if not isinstance(user, dict):
                        continue

                    if user.get("username") == f"user_{telegram_id}":
                        logger.info(f"✅ Найден RW пользователь по username: {telegram_id}")
                        return user

                    if user.get("note") == f"tg:{telegram_id}":
                        logger.info(f"✅ Найден RW пользователь по note: {telegram_id}")
                        return user

                    if user.get("telegramId") == telegram_id:
                        logger.info(f"✅ Найден RW пользователь по telegramId: {telegram_id}")
                        return user

                start += size
                if start >= total:
                    break

        logger.warning(f"⚠️ RW пользователь не найден: telegram_id={telegram_id}")
        return None


    # -------------------------
    # Обновление пользователя (PATCH)
    # -------------------------
    async def update_user(self, user_uuid: str, payload: Dict) -> Optional[Dict]:
        await self._ensure_session()

        payload = {
            "uuid": user_uuid,
            **payload,
        }

        async with self.session.patch(self._url("/users/"), json=payload) as resp:
            text = await resp.text()

            if resp.status != 200:
                logger.error(
                    f"❌ Remnawave PATCH error [{resp.status}] for {user_uuid}: {text}"
                )
                return None

            data = await resp.json()
            return data.get("response")


    # -------------------------
    # CREATE пользователя
    # -------------------------
    async def create_user(self, telegram_id: int) -> Optional[Dict]:
        await self._ensure_session()

        expire_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

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
                    "trafficLimitBytes": 1073741824,  # 1 GB
                    "trafficLimitStrategy": "DAY",
                    "activeInternalSquads": [
                        config.REMNAWAVE_DEFAULT_SQUAD_ID,
                        config.REMNAWAVE_DE_PROFILE_SQUAD_ID
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
                "trafficLimitBytes": 1073741824,  # 1 GB
                "trafficLimitStrategy": "DAY",
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
                    config.REMNAWAVE_DEFAULT_SQUAD_ID,
                    config.REMNAWAVE_DE_PROFILE_SQUAD_ID
                ]
            }
        )

        created["sub_url"] = created.get("subscriptionUrl")
        logger.info(f"✅ Пользователь {telegram_id} успешно создан и добавлен в squad")

        return created


    # -------------------------
    # DELETE пользователя
    # -------------------------
    async def delete_user(self, user_id: str) -> bool:
        await self._ensure_session()
        async with self.session.delete(self._url(f"/users/{user_id}/")) as resp:
            return resp.status in (200, 204)


    # -------------------------
    # GET пользователя
    # -------------------------
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

        async with self.session.get(self._url("/system/stats")) as resp:
            if resp.status != 200:
                logger.error("❌ RW system/stats error")
                return 0

            data = await resp.json()
            return int(
                data
                .get("response", {})
                .get("onlineStats", {})
                .get("onlineNow", 0)
            )


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



async def sync_remnawave_expire(telegram_id: int, new_end: datetime) -> bool:
    api = RemnawaveWrapper()
    try:
        rw_user = await api.find_user_by_telegram_id(telegram_id)

        if not rw_user:
            logger.error(f"[Remnawave] Пользователь не найден tg={telegram_id}")
            return False

        if "uuid" not in rw_user:
            logger.error(f"[Remnawave] У пользователя нет uuid tg={telegram_id}")
            return False

        expire_at = new_end.astimezone(timezone.utc).isoformat()

        updated = await api.update_user(
            rw_user["uuid"],
            {
                "expireAt": expire_at,
                "trafficLimitBytes": 0,
                "trafficLimitStrategy": "NO_RESET"
            }
        )

        if not updated:
            logger.error(
                f"[Remnawave] Не удалось обновить пользователя tg={telegram_id}"
            )
            return False

        logger.info(
            f"✅ [Remnawave] подписка обновлена успешно tg={telegram_id} → {expire_at}"
        )
        logger.info(f"♾️ [Remnawave] Пользователь переведён на БЕЗЛИМИТ")
        return True

    finally:
        await api.close()

