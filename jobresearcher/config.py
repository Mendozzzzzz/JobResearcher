from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

class Settings:
    API_ID: int = int(os.getenv("TELEGRAM_API_ID", "0"))
    API_HASH: str = os.getenv("TELEGRAM_API_HASH", "")
    PHONE: str = os.getenv("TELEGRAM_PHONE", "")
    CHANNELS: list[str] = [c.strip() for c in os.getenv("TELEGRAM_CHANNELS", "").split(",") if c.strip()]
    DB_PATH: Path = Path(os.getenv("DB_PATH", "jobresearcher.db"))
    SESSION_NAME: str = "jobresearcher_session"

settings = Settings()