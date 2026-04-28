import os
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")


def _normalize_database_url(raw_url: str) -> str:
    if raw_url.startswith("mysql://"):
        return raw_url.replace("mysql://", "mysql+pymysql://", 1)
    return raw_url


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = _normalize_database_url(
        os.environ.get(
            "DATABASE_URL",
            f"sqlite:///{os.path.join(INSTANCE_DIR, 'romina_parking.db')}",
        )
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True
    WTF_CSRF_TIME_LIMIT = None
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024
    APP_NAME = "Estacionamiento Romina"
    SEED_DEMO_USERS = False
    ENABLE_SECURITY_HEADERS = True
    PREFERRED_URL_SCHEME = "https"
    ADMIN_BOOTSTRAP_USERNAME = os.environ.get("ADMIN_BOOTSTRAP_USERNAME", "admin")
    ADMIN_BOOTSTRAP_NAME = os.environ.get("ADMIN_BOOTSTRAP_NAME", "Administrador General")
    ADMIN_BOOTSTRAP_PASSWORD = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD")


class DevelopmentConfig(Config):
    DEBUG = True
    SEED_DEMO_USERS = True
    PREFERRED_URL_SCHEME = "http"
    ADMIN_BOOTSTRAP_PASSWORD = os.environ.get(
        "ADMIN_BOOTSTRAP_PASSWORD", "AdminRomina2026!"
    )


class TestingConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    ENABLE_SECURITY_HEADERS = False
    SEED_DEMO_USERS = True
    ADMIN_BOOTSTRAP_PASSWORD = "AdminRomina2026!"


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config():
    env_name = os.environ.get("FLASK_ENV", "development").lower()
    return CONFIG_MAP.get(env_name, DevelopmentConfig)
