import os
from pydantic import BaseModel
from typing import List



#7833570599:AAFUPOwN3AWmWExYiwmyKJlcjysbiXIR3zU
#8067598636:AAHM-I14-dWlxNuBqOZIh3oYdHh3uLgrwYw тест
class Config(BaseModel):
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "7833570599:AAFUPOwN3AWmWExYiwmyKJlcjysbiXIR3zU")
    ADMINS: List[int] = [
        1799274098,
        6805707915, 
        7859739054 
    ]

    PLATEGA_BASE_URL: str = os.getenv("PLATEGA_BASE_URL", "https://app.platega.io")
    MERCHANT_ID: str = os.getenv("MERCHANT_ID","6120e89a-93dc-4359-a578-8eed028e8c1f")
    SECRET_KEY: str = os.getenv("SECRET_KEYMERCHANT_ID", "FSfD28jrJxiKZlbaqp5e4EgD8QI3ClHgV1uvh8FHZ3qxlOXo46AaOoNG3oJqWFqdgDDgmDbTFJ8FuY09SMeSQw6MdUQUe6HFX0Gn")

    REMNAWAVE_API_URL: str = os.getenv("REMNAWAVE_API_URL", "https://panel.shix-vpn.space/api")
    REMNAWAVE_API_KEY: str = os.getenv("REMNAWAVE_API_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1dWlkIjoiMTljZjA2MzAtNjlmZC00MjQyLWIxNGMtZmI0YTlmNWU5MDYzIiwidXNlcm5hbWUiOm51bGwsInJvbGUiOiJBUEkiLCJpYXQiOjE3NjkxMTk5NjIsImV4cCI6MTA0MDkwMzM1NjJ9.Q800pLD7zQfFlHEKPzMXEzPXg6W2QXm2LgyRgb_dOQU")
    REMNAWAVE_DEFAULT_SQUAD_ID: str = os.getenv("REMNAWAVE_DEFAULT_SQUAD_ID", "03374238-4afc-4e7d-baa2-2bd81c28ef2c")

    REQUIRED_CHANNEL_ID: int = int(os.getenv("REQUIRED_CHANNEL_ID", "-1003654012741"))
    REQUIRED_CHANNEL_URL: str = os.getenv("REQUIRED_CHANNEL_URL", "https://t.me/+puDopsmMAV01ZWYy")

config = Config()
