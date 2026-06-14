import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    APP_NAME = "Citizen Alert System"
    APP_VERSION = "1.0.0"

    # Security
    SECRET_KEY = os.getenv("SECRET_KEY", "secret")
    ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = 60

    # Database
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "sqlite+aiosqlite:///./test.db"
    )

    # System flags
    DEBUG = True

settings = Settings()