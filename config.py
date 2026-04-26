import os
from pydantic import BaseModel
from typing import List


class Config(BaseModel):

    BOT_TOKEN: str = os.getenv("BOT_TOKEN")

    ADMINS: List[int] = list(map(int, os.getenv("ADMINS", "").split(",")))

    PLATEGA_BASE_URL: str = os.getenv("PLATEGA_BASE_URL")
    MERCHANT_ID: str = os.getenv("MERCHANT_ID")
    SECRET_KEY: str = os.getenv("SECRET_KEY")

    REMNAWAVE_API_URL: str = os.getenv("REMNAWAVE_API_URL")
    REMNAWAVE_API_KEY: str = os.getenv("REMNAWAVE_API_KEY")
    REMNAWAVE_DEFAULT_SQUAD_ID: str = os.getenv("REMNAWAVE_DEFAULT_SQUAD_ID")

    REQUIRED_CHANNEL_ID: int = int(os.getenv("REQUIRED_CHANNEL_ID", "0"))
    REQUIRED_CHANNEL_URL: str = os.getenv("REQUIRED_CHANNEL_URL")

config = Config()

