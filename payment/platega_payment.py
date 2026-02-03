import asyncio
import requests
from config import config




# =========================================================
# СИНХРОННАЯ ЧАСТЬ (requests — как у тебя РАБОТАЕТ)
# =========================================================

def _create_platega_payment_sync(amount: int) -> dict:
    url = f"{config.PLATEGA_BASE_URL}/transaction/process"

    headers = {
        "Content-Type": "application/json",
        "X-MerchantId": config.MERCHANT_ID,
        "X-Secret": config.SECRET_KEY
    }

    payload = {
        "paymentMethod": 2,
        "paymentDetails": {
            "amount": amount,
            "currency": "RUB"
        },
        "description": "Оплата через Telegram-бот",
        "return": "https://google.com/success",
        "failedUrl": "https://google.com/fail"
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=10
    )

    if response.status_code != 200:
        print("❌ PLATEGA HTTP ERROR:", response.status_code, response.text)
        raise RuntimeError("Platega request failed")

    data = response.json()

    transaction_id = (
        data.get("id")
        or data.get("transactionId")
        or data.get("idTransaction")
        or data.get("uuid")
    )

    if not transaction_id or "redirect" not in data:
        print("❌ BAD PLATEGA RESPONSE:", data)
        raise RuntimeError("Bad Platega response")

    return {
        "transaction_id": transaction_id,
        "pay_url": data["redirect"]
    }


def _get_platega_payment_status_sync(transaction_id: str) -> dict:
    url = f"{config.PLATEGA_BASE_URL}/transaction/{transaction_id}"

    headers = {
        "X-MerchantId": config.MERCHANT_ID,
        "X-Secret": config.SECRET_KEY
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=10
    )

    if response.status_code != 200:
        print("❌ STATUS CHECK ERROR:", response.status_code, response.text)
        raise RuntimeError("Status check failed")

    return response.json()


# =========================================================
# ASYNC ОБЁРТКИ (ДЛЯ aiogram, ЧТОБЫ НЕ БЛОЧИТЬ БОТА)
# =========================================================

async def create_platega_payment(amount: int) -> dict | None:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None,
            _create_platega_payment_sync,
            amount
        )
    except Exception as e:
        print("❌ create_platega_payment ERROR:", e)
        return None


async def get_platega_payment_status(transaction_id: str) -> dict | None:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None,
            _get_platega_payment_status_sync,
            transaction_id
        )
    except Exception as e:
        print("❌ get_platega_payment_status ERROR:", e)
        return None
