import os
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, ClassVar

class Config(BaseModel):
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "8049723934:AAEIzB88UMoEHA2pLz2PYqbY9G6L89ONY54")
    ADMINS: List[int] = Field(default_factory=list)

    REMNAWAVE_API_URL: str = os.getenv("REMNAWAVE_API_URL", "https://shix-vpn.space/api")
    REMNAWAVE_API_KEY: str = os.getenv("REMNAWAVE_API_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1dWlkIjoiNTE0NDc4YzAtM2E1Ny00YTEzLWIwZWItNDcxNzgxZDE3OTg4IiwidXNlcm5hbWUiOm51bGwsInJvbGUiOiJBUEkiLCJpYXQiOjE3Njg5OTk0NjMsImV4cCI6MTA0MDg5MTMwNjN9.6bm2hWclnLqCI3EcNuNn9a-rrbRadj-tq3aS2U5ppKE")
    
    PAYMENT_TOKEN: str = os.getenv("PAYMENT_TOKEN", "ew0rCIUFqgaJPCghzoGqcIPrcYGrNYVY8eEnu8h1rBg7nARLsvEIXkA1t4wLOKlg")
    INBOUND_ID: int = Field(default_factory=lambda: int(os.getenv("INBOUND_ID", 1)))

    LAVA_API_KEY: str = "NGdW3ZDj2g73UNuNIVf6QRO4nteeaW7nTgl6HJ3DtTvneKWhZ0WxBOtAov91Tr8j"

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
    ADMINS=os.getenv("ADMINS", ""),
    INBOUND_ID=os.getenv("INBOUND_ID", 1)
)