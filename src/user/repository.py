from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from user.migration_diagnostics import find_postgres_error
from user.models import User

UNIQUE_VIOLATION = "23505"
LOGIN_UNIQUE_CONSTRAINT = "uq_users_login"


class LoginAlreadyExists(Exception):
    pass


@dataclass(frozen=True)
class Credentials:
    id: int
    password_hash: str


# Дубликат определяем по SQLSTATE и имени ограничения логина, а не по тексту ошибки.
# Любой другой IntegrityError — это сбой, а не «логин занят».
async def create_user(
    session_factory: async_sessionmaker[AsyncSession], login: str, password_hash: str
) -> int:
    try:
        async with session_factory() as session, session.begin():
            user = User(login=login, password_hash=password_hash)
            session.add(user)
            await session.flush()
            return user.id
    except IntegrityError as error:
        driver_error = find_postgres_error(error)
        if (
            getattr(driver_error, "sqlstate", None) == UNIQUE_VIOLATION
            and getattr(driver_error, "constraint_name", None)
            == LOGIN_UNIQUE_CONSTRAINT
        ):
            raise LoginAlreadyExists from None
        raise


async def find_credentials(
    session_factory: async_sessionmaker[AsyncSession], login: str
) -> Credentials | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(User.id, User.password_hash).where(User.login == login)
            )
        ).one_or_none()
    return None if row is None else Credentials(row.id, row.password_hash)
