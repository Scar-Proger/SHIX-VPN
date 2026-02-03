import asyncio
import requests
from aiogram import Bot, Dispatcher
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup

# ==========================
# НАСТРОЙКИ
# ==========================
TELEGRAM_TOKEN = "8067598636:AAHM-I14-dWlxNuBqOZIh3oYdHh3uLgrwYw"

PLATEGA_BASE_URL = "https://app.platega.io"
MERCHANT_ID = "6120e89a-93dc-4359-a578-8eed028e8c1f"
SECRET_KEY = "FSfD28jrJxiKZlbaqp5e4EgD8QI3ClHgV1uvh8FHZ3qxlOXo46AaOoNG3oJqWFqdgDDgmDbTFJ8FuY09SMeSQw6MdUQUe6HFX0Gn"

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# ==========================
# ФУНКЦИЯ СОЗДАНИЯ ССЫЛКИ
# ==========================
def create_platega_payment(amount: float):
    url = f"{PLATEGA_BASE_URL}/transaction/process"

    headers = {
        "Content-Type": "application/json",
        "X-MerchantId": MERCHANT_ID,
        "X-Secret": SECRET_KEY
    }

    payload = {
        "paymentMethod": 2,  # СБП QR
        "paymentDetails": {
            "amount": amount,
            "currency": "RUB"
        },
        "description": "Оплата через Telegram-бот",
        "return": "https://google.com/success",
        "failedUrl": "https://google.com/fail"
    }

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code == 200:
        return response.json().get("redirect")
    else:
        print(response.status_code, response.text)
        return None

# ==========================
# /start
# ==========================
@dp.message()
async def start(message: Message):
    amount = 500  # Фиксированная сумма в рублях

    payment_link = create_platega_payment(amount)

    if not payment_link:
        await message.answer("❌ Не удалось создать ссылку оплаты")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=payment_link)]
        ]
    )

    await message.answer(
        f"💰 Сумма к оплате: {amount} ₽",
        reply_markup=keyboard
    )

# ==========================
# ЗАПУСК БОТА
# ==========================
async def main():
    print("🤖 Бот запущен")
    await dp.start_polling(bot)

asyncio.run(main())
