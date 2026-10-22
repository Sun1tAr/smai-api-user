# smai-api-user

MS-3 — сервис пользователей SmartMeal (SMAI-100, SMAI-101). Регистрирует
пользователей, проверяет логин и пароль и отдаёт профиль. Возвращает только
`userId`: JWT подписывает gateway
(приватный ключ хранится только в нём), поэтому в ответах сервиса нет `token`.
Это отклонение от текста контрактов API-8/API-9, где `token` указан и для сервиса;
клиент получает `{userId, token}` от gateway (`POST /auth/register`,
`POST /auth/login`).

Сервис доступен только из внутренней сети контура: порт на хост не публикуется,
gateway обращается к нему по имени `user` в сети `smai`.

## Эндпоинты

Интерактивная документация (Swagger UI) — `/docs`, схема OpenAPI — `/openapi.json`.

| Метод и путь | Назначение | Ответы | Статус |
| --- | --- | --- | --- |
| `POST /register` | Создаёт пользователя `{login, password}`, возвращает `{"userId": N}` | 201, 400 `VALIDATION_ERROR`, 409 `LOGIN_ALREADY_EXISTS`, 500 `INTERNAL_ERROR`, 503 `SERVICE_BUSY` | API-8 |
| `POST /login` | Проверяет `{login, password}`, возвращает `{"userId": N}` | 200, 400 `VALIDATION_ERROR`, 401 `INVALID_CREDENTIALS`, 500 `INTERNAL_ERROR`, 503 `SERVICE_BUSY` | API-9 |
| `GET /profile` | Профиль по заголовку `X-User-Id` (его ставит gateway из JWT): `{"userId", "login", "createdAt"}`, `createdAt` в UTC, ISO 8601 с `Z` | 200, 401 `UNAUTHORIZED` (нет, повтор или не целое > 0), 404 `USER_NOT_FOUND`, 500 `INTERNAL_ERROR` | API-11 |
| `GET /health` | Готовность: `SELECT 1` к PostgreSQL; успешный ответ логируется на DEBUG, 503 — на INFO | 200 `{"status":"ok"}`, 503 `INTERNAL_ERROR` | служебный |
| `GET /metrics` | Метрики Prometheus (вне схемы OpenAPI); успешный ответ на DEBUG | 200 `text/plain` | служебный |
| `GET /docs`, `GET /openapi.json` | Swagger UI и схема OpenAPI | 200 | служебный |

`GET /profile` не проверяет токен: сервис доверяет `X-User-Id`, потому что
доступен только из сети `smai`, а клиентский заголовок gateway срезает. На
`404 USER_NOT_FOUND` (токен выдан удалённому пользователю) gateway отвечает
клиенту 401.

Пример:

```sh
curl -s -X POST http://127.0.0.1:8003/register -H 'Content-Type: application/json' \
  -d '{"login": "user@example.com", "password": "correct-horse-battery"}'
# 201 {"userId": 1}
curl -s -X POST http://127.0.0.1:8003/login -H 'Content-Type: application/json' \
  -d '{"login": "User@Example.com", "password": "correct-horse-battery"}'
# 200 {"userId": 1}
```

### Правила для данных

- `login` — email до 254 символов. Пробелы по краям обрезаются, значение
  приводится к нормальной форме NFKC и нижнему регистру: `A@x.ru` и `a@x.ru` — один
  логин (в БД хранится строчный вариант, уникальность обычным `UNIQUE`).
- `password` при регистрации — от 12 до 128 символов (128 защищает от больших
  входов в хэширование), не содержит логин и не входит в список частых паролей
  (`src/user/common_passwords.txt`). Правил про регистр букв, цифры и символы нет.
  Пароль не обрезается и не нормализуется. Пример из спецификации `secret` под эти
  правила не проходит и получает 400. При входе пароль проверяется только на длину
  1–128: неверный пароль любой длины — это 401.
- Оба поля — строго строки. Пустое тело, невалидный JSON, не-JSON `Content-Type`,
  число или `null` вместо строки — 400.

### Ошибки

Единый формат без обёртки `data`/`meta`:

```json
{"error": {"code": "VALIDATION_ERROR", "message": "Request validation failed",
           "details": {"fields": [{"loc": ["body", "password"], "type": "string_too_short"}]}}}
```

Коды: `VALIDATION_ERROR` (400; в `details.fields` только место и тип, введённые
значения и пароль не возвращаются), `INVALID_CREDENTIALS` (401; неизвестный логин и
неверный пароль неразличимы по ответу и по времени — для неизвестного логина
проверяется фиктивный хэш тех же параметров), `LOGIN_ALREADY_EXISTS` (409),
`UNAUTHORIZED` (401, `/profile` без корректного `X-User-Id`),
`USER_NOT_FOUND` (404, `/profile`),
`SERVICE_BUSY` (503 с `Retry-After`), `INTERNAL_ERROR` (500; также 503 на
`/health`), `NOT_FOUND` (404), `METHOD_NOT_ALLOWED` (405). Каждый ответ содержит
`X-Request-ID` (входящее значение принимается, если это до 128 символов
`A-Za-z0-9._-`, иначе генерируется UUID4). Тела запросов и пароли не пишутся в
логи и спаны; тексты исключений и SQL в ответы и логи не попадают.

Дубликат логина распознаётся по SQLSTATE `23505` и имени ограничения
`uq_users_login`; любая другая ошибка БД — 500.

## Хэширование паролей

argon2id (`argon2-cffi`), параметры заданы в `src/user/config.py` явно:
`time_cost=3`, `memory_cost=65536` КиБ (64 МиБ), `parallelism=1`, `hash_len=32`,
`salt_len=16`. Хэш — строка в формате PHC (содержит параметры и соль), хранится в
`users.password_hash`.

Хэширование и проверка идут в потоках. Одновременно работают не больше
`HASH_CONCURRENCY` операций (по 64 МиБ каждая). Ожидающих не больше
`HASH_QUEUE_MAX`, и ждут они не дольше `HASH_QUEUE_TIMEOUT_SECONDS`: иначе сервис
сразу отвечает `503 SERVICE_BUSY` с `Retry-After`. Gateway переотдаёт это как 503
`EXTERNAL_SERVICE_ERROR`.

## Локальный запуск

Требуются Docker и Docker Compose. Сервис подключается к внешней сети `smai`,
общей для контура.

```sh
docker network create smai   # один раз
cp .env.example .env         # проверьте значения, это локальные заглушки
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build --wait
curl --fail http://127.0.0.1:8003/health
curl --fail http://127.0.0.1:8003/docs
docker compose -f docker-compose.yml -f docker-compose.dev.yml down -v
```

`docker-compose.dev.yml` только для разработки: он публикует
`127.0.0.1:${USER_HTTP_PORT:-8003}` (контейнер слушает `0.0.0.0:8000`). В основном
`docker-compose.yml` портов нет — так и выкатывается на контур. При старте
контейнер выполняет `alembic upgrade head`, затем запускает uvicorn; приложение не
стартует, если БД недоступна. Контейнер работает от пользователя `user` (UID
10001). Защита «только от gateway» — изоляция сети `smai`; общего заголовка или
mTLS между сервисами нет.

## Конфигурация

Настройки читаются из окружения (локально — также из `.env` в рабочей директории).
Пустое значение (`KEY=`) считается незаданным. Некорректная конфигурация
останавливает запуск.

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Подключение к PostgreSQL | Обязательны |
| `DB_PORT` | Порт PostgreSQL | `5432` |
| `DB_TIMEOUT_SECONDS` | Предельное ожидание БД (0, 60]: подключение и запросы приложения. Запросы миграций не ограничиваются | `5` |
| `MIGRATION_LOCK_TIMEOUT_SECONDS` | Ожидание чужой блокировки при миграциях, (0, 3600] | `30` |
| `HASH_CONCURRENCY` | Сколько паролей хэшируется одновременно, 1–64 (по 64 МиБ памяти) | `4` |
| `HASH_QUEUE_MAX` | Сколько запросов может ждать свободный слот, 0–1024 | `32` |
| `HASH_QUEUE_TIMEOUT_SECONDS` | Сколько ждать слот до 503, (0, 60] | `5` |
| `LOG_LEVEL` | DEBUG / INFO / WARNING / ERROR | `INFO` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Базовый адрес OTLP HTTP, например `http://collector:4318` | Экспорт отключён |
| `USER_HTTP_PORT` | Только `docker-compose.dev.yml`: порт на хосте | `8003` |

## Данные и миграции

Таблица `users` (LE-11), ревизия `0001`:

| Колонка | Тип | Ограничения |
| --- | --- | --- |
| `id` | `integer` | `GENERATED BY DEFAULT AS IDENTITY`, первичный ключ `pk_users` |
| `login` | `text` | `NOT NULL`, `UNIQUE` (`uq_users_login`) |
| `password_hash` | `text` | `NOT NULL` |
| `created_at` | `timestamptz` | `NOT NULL`, `DEFAULT now()` |

Внешних ключей и лишних `CHECK` нет: связь 1:1 с сохранёнными параметрами
(SMAI-102) лежит в их базе. Таблица версий Alembic — `alembic_version_user`
(общий PostgreSQL на стенде). Имена БД и роли выдаёт DevOps, как для других
сервисов.

## Разработка и тесты

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/unit -p no:cacheprovider -q
# все тесты, включая интеграционные на реальном PostgreSQL
docker compose -p user-tests -f docker-compose.test.yml up --build \
  --abort-on-container-exit --exit-code-from tests
docker compose -p user-tests -f docker-compose.test.yml down -v
```

`docker-compose.test.yml` запускает unit и интеграционные тесты (регистрация →
вход на PostgreSQL 17) с `FAIL_ON_SKIP=1` и порогом покрытия `--cov-fail-under=98`.
Локальный `uv run pytest tests/unit` этих ограничений не применяет, а
интеграционные тесты без `TEST_DB_HOST` пропускаются.
