import pytest
from pydantic import ValidationError

from user.schemas import LoginRequest, RegisterRequest, common_passwords

GOOD_PASSWORD = "correct-horse-battery"


def register(**changes: object) -> RegisterRequest:
    values = {"login": "user@example.com", "password": GOOD_PASSWORD, **changes}
    return RegisterRequest.model_validate(values)


def failure(**changes: object) -> dict:
    with pytest.raises(ValidationError) as caught:
        register(**changes)
    (error,) = caught.value.errors()
    return {"loc": error["loc"], "type": error["type"]}


def test_login_is_trimmed_and_lowercased() -> None:
    assert register(login="  User@Example.COM \n").login == "user@example.com"


def test_login_is_nfkc_normalized() -> None:
    # Полноширинная «Ａ» и «＠» приводятся к обычным символам.
    assert register(login="Ａ＠example.com").login == "a@example.com"


@pytest.mark.parametrize(
    "login", ["", "   ", "plain", "a@", "@x.ru", "a b@x.ru", "a@x"]
)
def test_login_must_be_an_email(login: str) -> None:
    assert failure(login=login)["loc"] == ("login",)


def test_login_longer_than_254_is_rejected() -> None:
    long_login = "a" * 250 + "@x.ru"
    assert failure(login=long_login) == {"loc": ("login",), "type": "login_length"}


@pytest.mark.parametrize("value", [None, 1, True, ["a"], {"a": 1}])
def test_non_string_values_are_rejected(value: object) -> None:
    assert failure(login=value)["type"] == "string_type"
    assert failure(password=value)["type"] == "string_type"


def test_password_length_bounds() -> None:
    assert failure(password="x" * 11)["type"] == "string_too_short"
    assert failure(password="x" * 129)["type"] == "string_too_long"
    assert register(password="Zq" * 6).password == "Zq" * 6  # 12: граница
    assert register(password="Zq" * 64).password == "Zq" * 64


def test_password_is_not_trimmed_or_normalized() -> None:
    padded = f"  {GOOD_PASSWORD}  "
    assert register(password=padded).password == padded


def test_password_needs_no_character_classes() -> None:
    assert register(password="all lowercase words only").password


def test_password_must_not_contain_the_login() -> None:
    expected = {"loc": ("password",), "type": "password_contains_login"}
    assert failure(login="alexander@x.ru", password="alexander@x.ru") == expected
    assert failure(login="alexander@x.ru", password="XX-ALEXANDER@x.ru-XX") == expected


@pytest.mark.parametrize("password", ["Password1234", "QWERTYUIOP12", "123456789012"])
def test_common_passwords_are_rejected_case_insensitively(password: str) -> None:
    assert failure(password=password) == {
        "loc": ("password",),
        "type": "password_too_common",
    }


def test_common_password_list_only_has_policy_length_entries() -> None:
    assert common_passwords()
    assert all(len(entry) >= 12 for entry in common_passwords())


def test_login_request_does_not_apply_registration_rules_to_the_password() -> None:
    assert LoginRequest(login="U@x.ru", password="short").login == "u@x.ru"
    with pytest.raises(ValidationError):
        LoginRequest(login="u@x.ru", password="")
    with pytest.raises(ValidationError):
        LoginRequest(login="u@x.ru", password="x" * 129)
