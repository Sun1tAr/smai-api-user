import asyncpg
from sqlalchemy.exc import DBAPIError, OperationalError


# Как у настоящей ошибки сервера: код, основное сообщение и DETAIL с данными.
def server_error(code: str, message: str, detail: str | None = None):
    fields = {"S": "ERROR", "C": code, "M": message}
    if detail:
        fields["D"] = detail
    return asyncpg.exceptions.PostgresError.new(fields)


def test_postgres_error_exposes_sqlstate_and_server_message() -> None:
    from user.migration_diagnostics import describe_migration_error

    error = server_error(
        "42P01", 'relation "habit" does not exist', "Key (token)=(private-value)"
    )
    fields = describe_migration_error(error)
    assert "private-value" not in str(fields)
    assert fields == {
        "error_type": "UndefinedTableError",
        "sqlstate": "42P01",
        "driver_error_type": "UndefinedTableError",
        "db_message": 'relation "habit" does not exist',
    }


def test_wrapped_error_is_unwrapped_without_statement_and_parameters() -> None:
    from user.migration_diagnostics import describe_migration_error

    driver_error = server_error("23505", "duplicate key")
    error = DBAPIError(
        "INSERT INTO t VALUES (:p)", {"p": "private-password"}, driver_error
    )
    fields = describe_migration_error(error)
    assert fields["error_type"] == "DBAPIError"
    assert fields["sqlstate"] == "23505"
    assert fields["driver_error_type"] == "UniqueViolationError"
    assert "private-password" not in str(fields) and "INSERT" not in str(fields)


def test_cause_chain_is_followed() -> None:
    from user.migration_diagnostics import describe_migration_error

    wrapper = OperationalError("SELECT secret", {"p": "private-password"}, OSError())
    wrapper.__cause__ = server_error("57014", "canceling statement")
    assert describe_migration_error(wrapper)["sqlstate"] == "57014"


def test_non_postgres_error_has_no_sqlstate_or_message() -> None:
    from user.migration_diagnostics import describe_migration_error

    fields = describe_migration_error(ConnectionRefusedError("10.1.2.3 password=x"))
    assert fields == {"error_type": "ConnectionRefusedError", "sqlstate": None}


def test_message_is_kept_only_for_schema_and_syntax_class() -> None:
    from user.migration_diagnostics import describe_migration_error

    syntax = server_error("42601", 'syntax error at or near "SELEC"')
    assert describe_migration_error(syntax)["db_message"].startswith("syntax error")


def test_message_with_row_data_is_not_logged() -> None:
    from user.migration_diagnostics import describe_migration_error

    for code, message in (
        ("22P02", 'invalid input syntax for type integer: "private-abc"'),
        ("23505", "duplicate key value violates unique constraint"),
        ("23502", "null value in column"),
    ):
        fields = describe_migration_error(server_error(code, message))
        assert "db_message" not in fields
        assert fields["sqlstate"] == code and fields["driver_error_type"]
        assert "private-abc" not in str(fields)
