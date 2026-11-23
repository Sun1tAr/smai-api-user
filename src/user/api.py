import re
from math import ceil
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from user.errors import ErrorResponse, error_response
from user.hashing import HashingBusy
from user.repository import (
    LoginAlreadyExists,
    create_user,
    find_credentials,
    find_profile,
)
from user.schemas import LoginRequest, ProfileResponse, RegisterRequest, UserIdResponse

router = APIRouter()
logger = structlog.get_logger()


def error_example(
    code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "application/json": {
            "example": {
                "error": {"code": code, "message": message, "details": details or {}}
            }
        }
    }


VALIDATION_EXAMPLE = error_example(
    "VALIDATION_ERROR",
    "Request validation failed",
    {"fields": [{"loc": ["body", "password"], "type": "string_too_short"}]},
)

COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": ErrorResponse,
        "description": (
            "Некорректные параметры: нет тела, невалидный JSON, не JSON content type, "
            "поле не строка или отсутствует, логин не email или длиннее 254 символов, "
            "пароль вне допустимой длины. В `details.fields` только место и тип "
            "ошибки, введённые значения не возвращаются."
        ),
        "content": VALIDATION_EXAMPLE,
    },
    500: {
        "model": ErrorResponse,
        "description": "Ошибка сервера, например БД недоступна.",
        "content": error_example("INTERNAL_ERROR", "Internal server error"),
    },
    503: {
        "model": ErrorResponse,
        "description": (
            "Все слоты хэширования заняты: очередь полна или ожидание превысило "
            "`HASH_QUEUE_TIMEOUT_SECONDS`. Повторите запрос через `Retry-After` "
            "секунд."
        ),
        "headers": {
            "Retry-After": {
                "description": "Через сколько секунд повторить запрос.",
                "schema": {"type": "string", "example": "5"},
            }
        },
        "content": error_example("SERVICE_BUSY", "Service is busy, retry later"),
    },
}


def busy_response(request: Request) -> JSONResponse:
    timeout = request.app.state.settings.hash_queue_timeout_seconds
    return error_response(
        503,
        "SERVICE_BUSY",
        "Service is busy, retry later",
        headers={"Retry-After": str(ceil(timeout))},
    )


def failure_response(event: str) -> JSONResponse:
    # Стек без текста исключения: сообщения драйвера могут содержать SQL и адреса.
    logger.error(event, exc_info=True)
    return error_response(500, "INTERNAL_ERROR", "Internal server error")


@router.post(
    "/register",
    tags=["auth"],
    summary="Регистрация пользователя (API-8)",
    description=(
        "Создаёт пользователя и возвращает его `userId`. Токен не выдаётся: JWT "
        "подписывает gateway после успешного ответа этого сервиса (SMAI-A-305), "
        "поэтому в ответе, в отличие от контракта API-8 для клиента, нет `token`.\n\n"
        "Логин обрезается по краям и приводится к нижнему регистру: `A@x.ru` и "
        "`a@x.ru` один и тот же логин. Пароль хэшируется argon2id и нигде не "
        "хранится и не логируется."
    ),
    status_code=201,
    response_model=UserIdResponse,
    response_model_by_alias=True,
    responses={
        409: {
            "model": ErrorResponse,
            "description": "Пользователь с таким логином уже существует.",
            "content": error_example(
                "LOGIN_ALREADY_EXISTS", "User with this login already exists"
            ),
        },
        **COMMON_RESPONSES,
    },
)
async def register(body: RegisterRequest, request: Request) -> Any:
    try:
        password_hash = await request.app.state.passwords.hash(body.password)
        user_id = await create_user(
            request.app.state.session_factory, body.login, password_hash
        )
    except LoginAlreadyExists:
        return error_response(
            409, "LOGIN_ALREADY_EXISTS", "User with this login already exists"
        )
    except HashingBusy:
        return busy_response(request)
    except Exception:
        return failure_response("register_failed")
    return JSONResponse({"userId": user_id}, status_code=201)


@router.post(
    "/login",
    tags=["auth"],
    summary="Проверка логина и пароля (API-9)",
    description=(
        "Проверяет учётные данные и возвращает `userId`. Токен выдаёт gateway. "
        "Неизвестный логин и неверный пароль дают один и тот же 401 и занимают "
        "одинаковое время (для неизвестного логина проверяется фиктивный хэш)."
    ),
    response_model=UserIdResponse,
    response_model_by_alias=True,
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Неверный логин или пароль.",
            "content": error_example(
                "INVALID_CREDENTIALS", "Invalid login or password"
            ),
        },
        **COMMON_RESPONSES,
    },
)
async def login(body: LoginRequest, request: Request) -> Any:
    passwords = request.app.state.passwords
    try:
        credentials = await find_credentials(
            request.app.state.session_factory, body.login
        )
        known_hash = credentials.password_hash if credentials else passwords.dummy_hash
        # verify выполняется ровно один раз и для неизвестного логина тоже.
        matches = await passwords.verify(known_hash, body.password)
    except HashingBusy:
        return busy_response(request)
    except Exception:
        return failure_response("login_failed")
    if credentials is None or not matches:
        return error_response(401, "INVALID_CREDENTIALS", "Invalid login or password")
    return JSONResponse({"userId": credentials.id})


# Колонка id — integer: больший идентификатор не существует и дал бы ошибку драйвера.
_USER_ID = re.compile(r"[1-9][0-9]{0,9}")
_MAX_USER_ID = 2**31 - 1


def requested_user_id(request: Request) -> int | None:
    values = request.headers.getlist("x-user-id")
    if len(values) != 1 or not _USER_ID.fullmatch(values[0]):
        return None
    user_id = int(values[0])
    return user_id if user_id <= _MAX_USER_ID else None


@router.get(
    "/profile",
    tags=["profile"],
    summary="Профиль пользователя (API-11)",
    description=(
        "Возвращает профиль пользователя, чей идентификатор передан в заголовке "
        "`X-User-Id`. Заголовок выставляет gateway из проверенного JWT; сервис "
        "ему доверяет и сам токен не проверяет, потому что доступен только из "
        "внутренней сети контура (`smai`). Вызывать напрямую снаружи нельзя."
    ),
    response_model=ProfileResponse,
    response_model_by_alias=True,
    responses={
        401: {
            "model": ErrorResponse,
            "description": (
                "Заголовок `X-User-Id` отсутствует, повторён или не является "
                "целым числом больше нуля."
            ),
            "content": error_example("UNAUTHORIZED", "User is not identified"),
        },
        404: {
            "model": ErrorResponse,
            "description": (
                "Пользователя с таким идентификатором нет (например, удалён после "
                "выдачи токена). Клиенту gateway отвечает на это 401."
            ),
            "content": error_example("USER_NOT_FOUND", "User not found"),
        },
        500: COMMON_RESPONSES[500],
    },
)
async def profile(
    request: Request,
    x_user_id: Annotated[
        str | None,
        Header(
            alias="X-User-Id",
            description=(
                "Идентификатор пользователя из токена; выставляет gateway. "
                "Целое число от 1 до 2147483647."
            ),
            examples=["42"],
        ),
    ] = None,
) -> Any:
    user_id = requested_user_id(request)
    if user_id is None:
        return error_response(401, "UNAUTHORIZED", "User is not identified")
    try:
        found = await find_profile(request.app.state.session_factory, user_id)
    except Exception:
        return failure_response("profile_failed")
    if found is None:
        return error_response(404, "USER_NOT_FOUND", "User not found")
    return ProfileResponse(
        user_id=found.id, login=found.login, created_at=found.created_at
    )
