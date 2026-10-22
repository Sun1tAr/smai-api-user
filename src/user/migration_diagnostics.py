from typing import Any

from asyncpg import PostgresError

# Класс 42: синтаксис и объекты схемы; сообщение называет объект, но не данные.
MESSAGE_SQLSTATE_CLASS = "42"


# Ищем исключение драйвера среди обёрток SQLAlchemy (orig) и причин (__cause__).
def find_postgres_error(error: BaseException) -> PostgresError | None:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, PostgresError):
            return current
        for linked in (getattr(current, "orig", None), current.__cause__):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return None


# Только безопасные поля: тип и SQLSTATE; основное сообщение сервера БД — лишь для
# класса 42. Текст обёрток SQLAlchemy (SQL, параметры) и DETAIL/HINT не включаются.
def describe_migration_error(error: BaseException) -> dict[str, Any]:
    postgres_error = find_postgres_error(error)
    fields: dict[str, Any] = {
        "error_type": type(error).__name__,
        "sqlstate": getattr(postgres_error, "sqlstate", None),
    }
    if postgres_error is not None:
        fields["driver_error_type"] = type(postgres_error).__name__
        # Сообщения остальных классов могут содержать данные строк, например
        # `invalid input syntax for type integer: "abc"` (класс 22).
        if (fields["sqlstate"] or "").startswith(MESSAGE_SQLSTATE_CLASS):
            fields["db_message"] = getattr(postgres_error, "message", None)
    return fields
