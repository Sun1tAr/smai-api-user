from user.config import Settings


# Порт 1 заведомо закрыт: БД «недоступна» без обращения к сети.
def unavailable_db_settings(**extra: object) -> Settings:
    values: dict[str, object] = {
        "db_host": "127.0.0.1",
        "db_port": 1,
        "db_user": "user",
        "db_name": "user",
        "db_password": "private-test-password",
        "db_timeout_seconds": 1,
        **extra,
    }
    return Settings(_env_file=None, **values)
