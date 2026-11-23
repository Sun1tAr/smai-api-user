import secrets
from collections.abc import Callable
from functools import cache
from typing import TypeVar

import anyio
from anyio import CapacityLimiter, to_thread
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from user.config import (
    ARGON2_HASH_LEN,
    ARGON2_MEMORY_COST_KIB,
    ARGON2_PARALLELISM,
    ARGON2_SALT_LEN,
    ARGON2_TIME_COST,
    Settings,
)

T = TypeVar("T")


class HashingBusy(Exception):
    """Нет свободного слота хэширования: очередь полна или ожидание истекло."""


def create_password_hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST_KIB,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        salt_len=ARGON2_SALT_LEN,
        type=Type.ID,
    )


# Хэш случайного секрета, который никто не знает: verify по нему для неизвестного
# логина стоит столько же, сколько настоящий, и никогда не проходит. Считается один
# раз на процесс.
@cache
def dummy_hash() -> str:
    return create_password_hasher().hash(secrets.token_urlsafe(32))


# Хэширование и проверка идут в потоках: argon2 держит процессор и 64 МиБ памяти
# около 100 мс, на цикле событий это остановило бы все запросы.
class PasswordService:
    def __init__(self, settings: Settings) -> None:
        self._hasher = create_password_hasher()
        self._limiter = CapacityLimiter(settings.hash_concurrency)
        self._queue_max = settings.hash_queue_max
        self._timeout = settings.hash_queue_timeout_seconds
        self._waiting = 0
        self.dummy_hash = dummy_hash()

    async def hash(self, password: str) -> str:
        return await self._run(self._hasher.hash, password)

    # False для неверного пароля и испорченного хэша; остальные ошибки летят дальше.
    async def verify(self, password_hash: str, password: str) -> bool:
        try:
            return await self._run(self._hasher.verify, password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False

    async def _run(self, function: Callable[..., T], *args: str) -> T:
        await self._acquire_slot()
        try:
            return await to_thread.run_sync(function, *args)
        finally:
            self._limiter.release()

    # Очередь ограничена и по длине, и по времени: иначе поток регистраций копил бы
    # ожидающие запросы и память сервиса.
    async def _acquire_slot(self) -> None:
        try:
            self._limiter.acquire_nowait()
            return
        except anyio.WouldBlock:
            pass
        if self._waiting >= self._queue_max:
            raise HashingBusy
        self._waiting += 1
        try:
            with anyio.fail_after(self._timeout):
                await self._limiter.acquire()
        except TimeoutError:
            raise HashingBusy from None
        finally:
            self._waiting -= 1
