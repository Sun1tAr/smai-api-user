import threading

import anyio
import pytest
from argon2.exceptions import InvalidHashError

from user.config import Settings
from user.hashing import HashingBusy, PasswordService, dummy_hash


def make_service(**extra: object) -> PasswordService:
    values: dict[str, object] = {
        "db_host": "h",
        "db_name": "n",
        "db_user": "u",
        "db_password": "p",
        **extra,
    }
    return PasswordService(Settings(_env_file=None, **values))


async def test_hash_uses_documented_argon2id_parameters_and_random_salt() -> None:
    service = make_service()
    first = await service.hash("correct-horse-battery")
    second = await service.hash("correct-horse-battery")
    assert first.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    assert first != second
    # salt_len=16 и hash_len=32 байта в base64 без заполнения: 22 и 43 символа.
    _, _, _, _, salt, digest = first.split("$")
    assert (len(salt), len(digest)) == (22, 43)


async def test_verify_accepts_right_password_only() -> None:
    service = make_service()
    stored = await service.hash("correct-horse-battery")
    assert await service.verify(stored, "correct-horse-battery") is True
    assert await service.verify(stored, "correct-horse-batterY") is False


async def test_verify_is_false_for_a_corrupt_hash_and_raises_other_errors() -> None:
    service = make_service()
    assert await service.verify("not-a-hash", "whatever-password") is False

    class Broken:
        def verify(self, *args: str) -> bool:
            raise RuntimeError("boom")

    service._hasher = Broken()  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await service.verify("x", "y")
    # слот освобождён и после ошибки
    assert service._limiter.borrowed_tokens == 0


def test_invalid_hash_error_is_what_argon2_raises_for_garbage() -> None:
    from user.hashing import create_password_hasher

    with pytest.raises(InvalidHashError):
        create_password_hasher().verify("garbage", "x")


def test_dummy_hash_is_built_once_with_the_same_parameters() -> None:
    assert dummy_hash() is dummy_hash()
    assert dummy_hash().startswith("$argon2id$v=19$m=65536,t=3,p=1$")


class Gate:
    """Хэшер, который держит поток, пока тест не отпустит его."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()

    def hash(self, password: str) -> str:
        self.started.set()
        self.release.wait(10)
        return "hash"


async def test_full_queue_answers_busy_without_waiting() -> None:
    service = make_service(hash_concurrency=1, hash_queue_max=1)
    gate = Gate()
    service._hasher = gate  # type: ignore[assignment]
    results: list[object] = []

    async def call() -> None:
        try:
            results.append(await service.hash("p"))
        except HashingBusy:
            results.append("busy")

    async with anyio.create_task_group() as group:
        group.start_soon(call)  # занимает слот
        await anyio.to_thread.run_sync(gate.started.wait)
        group.start_soon(call)  # встаёт в очередь (1 из 1)
        await anyio.sleep(0.05)
        assert service._waiting == 1
        group.start_soon(call)  # очередь полна
        await anyio.sleep(0.05)
        assert results == ["busy"]
        assert service._waiting == 1  # ожидание не растёт
        gate.release.set()
    assert sorted(results, key=str) == ["busy", "hash", "hash"]
    assert service._waiting == 0


async def test_wait_longer_than_timeout_answers_busy() -> None:
    service = make_service(hash_concurrency=1, hash_queue_timeout_seconds=0.1)
    gate = Gate()
    service._hasher = gate  # type: ignore[assignment]
    async with anyio.create_task_group() as group:
        group.start_soon(service.hash, "p")
        await anyio.to_thread.run_sync(gate.started.wait)
        with pytest.raises(HashingBusy):
            await service.hash("p")
        assert service._waiting == 0
        gate.release.set()
