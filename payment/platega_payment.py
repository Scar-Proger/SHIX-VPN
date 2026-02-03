import aiohttp
from config import config




async def create_platega_payment(amount: int) -> dict | None:
    url = f"{config.PLATEGA_BASE_URL}/transaction/process"

    headers = {
        "Content-Type": "application/json",
        "X-MerchantId": config.MERCHANT_ID,
        "X-Secret": config.SECRET_KEY
    }

    payload = {
        "paymentMethod": "SBPQR",
        "paymentDetails": {
            "amount": amount,
            "currency": "RUB"
        },
        "description": "Оплата через Telegram-бот",
        "return": "https://google.com/success",
        "failedUrl": "https://google.com/fail"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                return None

            data = await resp.json()

            transaction_id = (
                data.get("id")
                or data.get("transactionId")
                or data.get("idTransaction")
                or data.get("uuid")
            )

            if not transaction_id or "redirect" not in data:
                print("❌ BAD PLATEGA RESPONSE:", data)
                return None

            return {
                "transaction_id": transaction_id,
                "pay_url": data["redirect"]
            }

async def get_platega_payment_status(transaction_id: str) -> dict | None:
    url = f"{config.PLATEGA_BASE_URL}/transaction/{transaction_id}"

    headers = {
        "X-MerchantId": config.MERCHANT_ID,
        "X-Secret": config.SECRET_KEY
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()

    return None
