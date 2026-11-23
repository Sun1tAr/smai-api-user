# Smart Meal AI — User Service (MS-3)

Сервис пользователей **Smart Meal AI**: регистрация, проверка логина и пароля,
выдача профиля. JWT выпускает gateway, поэтому в ответах сервиса есть только
`userId`, без токена.

> **Ветка.** Актуальный код и полная документация — в ветке `develop`. Этот
> README описывает состояние `develop`.

## Роль в системе

MS-3 доступен только из внутренней сети контура: порт на хост не публикуется,
gateway обращается к сервису по имени `user`. Gateway вызывает MS-3 при
регистрации и входе, подписывает JWT своим ключом и дальше передаёт в сервисы
только `X-User-Id`.

## Стек

- Python 3.12, FastAPI, Pydantic Settings;
- SQLAlchemy 2 (async) + asyncpg, PostgreSQL;
- Alembic;
- argon2-cffi (`argon2id`), хэширование в пуле потоков;
- structlog, OpenTelemetry, Prometheus;
- uv, Ruff, pytest, Docker Compose, GitHub Actions.

## API

Интерактивная документация — `/docs`, схема OpenAPI — `/openapi.json`.

| Метод | Путь | Назначение | Ответы |
|---|---|---|---|
| `POST` | `/register` | Создать пользователя `{login, password}` → `{"userId": N}` | 201, 400, 409, 500, 503 |
| `POST` | `/login` | Проверить `{login, password}` → `{"userId": N}` | 200, 400, 401, 500, 503 |
| `GET` | `/profile` | Профиль по `X-User-Id`: `{userId, login, createdAt}` | 200, 401, 404, 500 |
| `GET` | `/health` | Готовность: `SELECT 1` к PostgreSQL | 200, 503 |
| `GET` | `/metrics` | Метрики Prometheus | 200 |
| `GET` | `/docs`, `/openapi.json` | Swagger UI и схема | 200 |

Пример:

```sh
curl -s -X POST http://127.0.0.1:8003/register \
  -H 'Content-Type: application/json' \
  -d '{"login": "user@example.com", "password": "correct-horse-battery"}'
# 201 {"userId": 1}

curl -s -X POST http://127.0.0.1:8003/login \
  -H 'Content-Type: application/json' \
  -d '{"login": "User@Example.com", "password": "correct-horse-battery"}'
# 200 {"userId": 1}
```

`GET /profile` не проверяет токен и доверяет `X-User-Id`: сервис доступен только
из сети контура, а клиентский заголовок срезает gateway.

## Правила данных

- `login` — email до 254 символов; обрезается по краям, приводится к NFKC и
  нижнему регистру. `A@x.ru` и `a@x.ru` — один логин.
- `password` при регистрации — 12–128 символов, не содержит логин и не входит в
  список частых паролей (`src/user/common_passwords.txt`). Пароль не обрезается и
  не нормализуется.
- При входе пароль проверяется только на длину 1–128; неверный пароль даёт 401.
- Пустое тело, невалидный JSON, не-JSON `Content-Type`, число или `null` вместо
  строки — 400.

## Ошибки

```json
{"error": {"code": "VALIDATION_ERROR", "message": "Request validation failed",
           "details": {"fields": [{"loc": ["body", "password"], "type": "string_too_short"}]}}}
```

Коды: `VALIDATION_ERROR`, `INVALID_CREDENTIALS`, `LOGIN_ALREADY_EXISTS`,
`UNAUTHORIZED`, `USER_NOT_FOUND`, `SERVICE_BUSY`, `INTERNAL_ERROR`, `NOT_FOUND`,
`METHOD_NOT_ALLOWED`. Каждый ответ содержит `X-Request-ID`. Тела запросов и
пароли не попадают в логи и спаны.

## Хэширование паролей

`argon2id` с параметрами `time_cost=3`, `memory_cost=65536` КиБ (64 МиБ),
`parallelism=1`, `hash_len=32`, `salt_len=16`. Хэш хранится в формате PHC в
`users.password_hash`.

Одновременно выполняется не больше `HASH_CONCURRENCY` операций; очередь ожидания
ограничена `HASH_QUEUE_MAX` и `HASH_QUEUE_TIMEOUT_SECONDS`. При переполнении
сервис сразу отвечает `503 SERVICE_BUSY` с `Retry-After`.

## Данные

Таблица `users` создаётся ревизией `0001_create_users`; таблица версий Alembic —
`alembic_version_user`.

| Колонка | Тип | Ограничения |
|---|---|---|
| `id` | `integer` | identity, первичный ключ |
| `login` | `text` | `NOT NULL`, `UNIQUE` |
| `password_hash` | `text` | `NOT NULL` |
| `created_at` | `timestamptz` | `NOT NULL`, `DEFAULT now()` |

## Конфигурация

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Подключение к PostgreSQL | обязательны |
| `DB_PORT` | Порт PostgreSQL | `5432` |
| `DB_TIMEOUT_SECONDS` | Предельное ожидание БД | `5` |
| `MIGRATION_LOCK_TIMEOUT_SECONDS` | Ожидание блокировки при миграциях | `30` |
| `HASH_CONCURRENCY` | Параллельных хэширований, 1–64 | `4` |
| `HASH_QUEUE_MAX` | Ожидающих запросов, 0–1024 | `32` |
| `HASH_QUEUE_TIMEOUT_SECONDS` | Ожидание слота до 503 | `5` |
| `LOG_LEVEL` | Уровень логирования | `INFO` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Адрес OTLP HTTP-коллектора | экспорт отключён |
| `USER_HTTP_PORT` | Хостовой порт `docker-compose.dev.yml` | `8003` |

## Локальный запуск

```sh
docker network create smai   # один раз
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build --wait
curl --fail http://127.0.0.1:8003/health
docker compose -f docker-compose.yml -f docker-compose.dev.yml down -v
```

`docker-compose.dev.yml` публикует `127.0.0.1:${USER_HTTP_PORT:-8003}`; основной
Compose портов не открывает. При старте выполняется `alembic upgrade head`, затем
запускается uvicorn. Контейнер работает от пользователя `user` (UID 10001).

## Разработка и тесты

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/unit -p no:cacheprovider -q

docker compose -p user-tests -f docker-compose.test.yml up --build \
  --abort-on-container-exit --exit-code-from tests
docker compose -p user-tests -f docker-compose.test.yml down -v
```

Тестовый Compose запускает unit- и интеграционные тесты на PostgreSQL 17 с
`FAIL_ON_SKIP=1` и порогом покрытия 98 %.

## Известные ограничения

- Нет внутренней аутентификации: сервис доверяет `X-User-Id` и защищён только
  сетевой изоляцией.
- JWT не выпускается и не проверяется сервисом — это ответственность gateway.
- Одна реплика; при нескольких репликах общий лимит хэширования не
  синхронизируется.

## Связанные репозитории

- [SmartMeal-AI](https://github.com/Sun1tAr/SmartMeal-AI);
- [smai-api-gateway](https://github.com/Sun1tAr/smai-api-gateway);
- [smai-infra](https://github.com/Sun1tAr/smai-infra).
