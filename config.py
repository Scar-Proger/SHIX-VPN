import os
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, ClassVar

class Config(BaseModel):
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "8563843181:AAEQbL23Yqn3XOMl_yx9YBA4I2-DAQCIMKA")
    ADMINS: List[int] = Field(default_factory=list)
    XUI_API_URL: str = os.getenv("XUI_API_URL", "https://shix-vpn.space:5260")
    XUI_BASE_PATH: str = os.getenv("XUI_BASE_PATH", "5cXk7L6W7v3G5UT9Zf")
    XUI_USERNAME: str = os.getenv("XUI_USERNAME", "AUOmGS5WOK")
    XUI_PASSWORD: str = os.getenv("XUI_PASSWORD", "QvOsEz0oep")

    XUI_HOST: str = os.getenv("XUI_HOST", "72.56.65.20")
    XUI_SERVER_NAME: str = os.getenv("XUI_SERVER_NAME", "www.microsoft.com")
    
    PAYMENT_TOKEN: str = os.getenv("PAYMENT_TOKEN", "ew0rCIUFqgaJPCghzoGqcIPrcYGrNYVY8eEnu8h1rBg7nARLsvEIXkA1t4wLOKlg")
    INBOUND_ID: int = Field(default_factory=lambda: int(os.getenv("INBOUND_ID", 1)))

    LAVA_API_KEY: str = "NGdW3ZDj2g73UNuNIVf6QRO4nteeaW7nTgl6HJ3DtTvneKWhZ0WxBOtAov91Tr8j"

    REALITY_PUBLIC_KEY: str = os.getenv("REALITY_PUBLIC_KEY", "d8UDXbJV_VEDwaJAmpauSk8uGQDVNLi8KVz39nE-5Wo")
    REALITY_FINGERPRINT: str = os.getenv("REALITY_FINGERPRINT", "chrome")
    REALITY_SNI: str = os.getenv("REALITY_SNI", "www.microsoft.com")
    REALITY_SHORT_ID: str = os.getenv("REALITY_SHORT_ID", "5c9ab638b50e")
    REALITY_SPIDER_X: str = os.getenv("REALITY_SPIDER_X", "/")

    # Настройки цен и скидок
    CUSTOM_LAVA_PRICES: ClassVar[Dict[int, int]] = {
        1: 150,
        3: 417,
        6: 714,
        12: 1188,
        24: 1656
    }

    @field_validator('ADMINS', mode='before')
    def parse_admins(cls, value):
        if isinstance(value, str):
            return [int(admin) for admin in value.split(",") if admin.strip()]
        return value or []
    
    @field_validator('INBOUND_ID', mode='before')
    def parse_inbound_id(cls, value):
        if isinstance(value, str):
            return int(value)
        return value or 15

config = Config(
    ADMINS=os.getenv("ADMINS", "1799274098"),
    INBOUND_ID=os.getenv("INBOUND_ID", 1)
)