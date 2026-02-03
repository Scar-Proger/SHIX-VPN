import aiohttp
from config import config

async def create_platega_payment(amount: float):
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

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("redirect")

    return None
