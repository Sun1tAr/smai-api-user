import unicodedata
from datetime import UTC, datetime
from functools import cache
from importlib.resources import files
from typing import Annotated

from email_validator import EmailNotValidError, validate_email
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationInfo,
    field_serializer,
    field_validator,
)
from pydantic_core import PydanticCustomError

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
LOGIN_MAX_LENGTH = 254


@cache
def common_passwords() -> frozenset[str]:
    text = files("user").joinpath("common_passwords.txt").read_text(encoding="utf-8")
    return frozenset(
        line.strip().casefold()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    )


# Один логин — один аккаунт: пробелы по краям обрезаются, затем NFKC и нижний
# регистр, поэтому `A@x.ru` и `a@x.ru` не могут быть двумя пользователями.
def normalize_login(value: str) -> str:
    login = unicodedata.normalize("NFKC", value.strip()).lower()
    if not 0 < len(login) <= LOGIN_MAX_LENGTH:
        raise PydanticCustomError(
            "login_length", "Login must be 1 to {max} characters", {"max": 254}
        )
    try:
        validate_email(login, check_deliverability=False)
    except EmailNotValidError:
        raise PydanticCustomError(
            "login_not_email", "Login must be an email address"
        ) from None
    return login


# StrictStr: число или null вместо строки — это 400, а не молчаливое приведение.
Login = Annotated[StrictStr, AfterValidator(normalize_login)]


class LoginRequest(BaseModel):
    login: Login = Field(
        description=(
            "Email пользователя, до 254 символов. Пробелы по краям обрезаются, "
            "значение приводится к нижнему регистру (NFKC)."
        ),
        examples=["user@example.com"],
    )
    # Верхняя граница защищает от огромных строк в хэшировании; нижняя не
    # проверяется: неверный пароль любой длины — это 401, а не 400.
    password: StrictStr = Field(
        min_length=1,
        max_length=PASSWORD_MAX_LENGTH,
        description="Пароль, 1–128 символов. Не обрезается и не нормализуется.",
        examples=["correct-horse-battery"],
    )


class RegisterRequest(LoginRequest):
    password: StrictStr = Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
        description=(
            "Пароль, 12–128 символов. Не равен логину и не содержит его, не входит "
            "в список частых паролей. Правил про регистр букв, цифры и символы нет. "
            "Не обрезается и не нормализуется."
        ),
        examples=["correct-horse-battery"],
    )

    @field_validator("password", mode="after")
    @classmethod
    def check_strength(cls, value: str, info: ValidationInfo) -> str:
        login = info.data.get("login")
        if login is not None and login.casefold() in value.casefold():
            raise PydanticCustomError(
                "password_contains_login", "Password must not contain the login"
            )
        if value.casefold() in common_passwords():
            raise PydanticCustomError("password_too_common", "Password is too common")
        return value


class UserIdResponse(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True, json_schema_extra={"example": {"userId": 42}}
    )

    user_id: int = Field(
        alias="userId",
        description=(
            "Идентификатор пользователя. Токен сервис не выдаёт: JWT подписывает "
            "gateway."
        ),
        examples=[42],
    )


class ProfileResponse(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "userId": 42,
                "login": "user@example.com",
                "createdAt": "2026-10-14T12:00:00Z",
            }
        },
    )

    user_id: int = Field(
        alias="userId", description="Идентификатор пользователя.", examples=[42]
    )
    login: str = Field(
        description="Логин (email) в нижнем регистре, как он сохранён при регистрации.",
        examples=["user@example.com"],
    )
    created_at: datetime = Field(
        alias="createdAt",
        description="Время регистрации в UTC, ISO 8601 с `Z`, с точностью до секунды.",
        examples=["2026-10-14T12:00:00Z"],
    )

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        utc = value.astimezone(UTC).replace(microsecond=0)
        return utc.isoformat().replace("+00:00", "Z")
