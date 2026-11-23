from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# Параметры argon2id заданы явно (не берутся из умолчаний библиотеки): смена версии
# argon2-cffi не должна незаметно менять стоимость хэширования.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST_KIB = 65536
ARGON2_PARALLELISM = 1
ARGON2_HASH_LEN = 32
ARGON2_SALT_LEN = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Пустое значение (KEY= в .env или ${KEY:-} в Compose) означает «не задано».
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    db_host: str = Field(min_length=1)
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
    db_password: SecretStr = Field(min_length=1)
    db_timeout_seconds: float = Field(default=5, gt=0, le=60)
    # Ожидание блокировки при миграциях (только Alembic): чужая блокировка не должна
    # подвешивать старт контейнера бесконечно.
    migration_lock_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    # Одновременно в потоках хэшируется не более hash_concurrency паролей (каждый
    # занимает 64 МиБ). Ожидающих не больше hash_queue_max, ждут не дольше
    # hash_queue_timeout_seconds, иначе 503 SERVICE_BUSY.
    hash_concurrency: int = Field(default=4, ge=1, le=64)
    hash_queue_max: int = Field(default=32, ge=0, le=1024)
    hash_queue_timeout_seconds: float = Field(default=5, gt=0, le=60)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    otel_exporter_otlp_endpoint: AnyHttpUrl | None = None

    # LOG_LEVEL=info допустим: регистр в окружении легко перепутать.
    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    # Пробельное значение так же бессмысленно, как пустое. Само значение не меняем:
    # пробелы могут быть частью пароля.
    @field_validator("db_host", "db_name", "db_user", mode="after")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("db_password", mode="after")
    @classmethod
    def reject_blank_password(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    # Учётные данные в URL попали бы в логи, трейсы и сообщения об ошибках.
    @field_validator("otel_exporter_otlp_endpoint", mode="after")
    @classmethod
    def reject_url_credentials(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None and (value.username or value.password):
            raise ValueError("must not contain credentials (user:password@host)")
        return value

    # URL.create сохраняет спецсимволы пароля без ручного экранирования.
    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
