import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from prometheus_client import (
    CollectorRegistry,
    GCCollector,
    PlatformCollector,
    ProcessCollector,
)
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.ext.asyncio import async_sessionmaker

from user.api import router as auth_router
from user.config import Settings
from user.database import create_engine
from user.errors import error_response, register_error_handlers
from user.hashing import PasswordService
from user.health import dependencies_ready, router
from user.observability import configure_logging, create_tracer_provider

logger = structlog.get_logger()

API_DESCRIPTION = (
    "Сервис пользователей SmartMeal (MS-3): регистрация и проверка логина и пароля. "
    "Возвращает `userId`; JWT выдаёт gateway. Доступен только из внутренней сети "
    "контура.\n\n"
    "Все ошибки возвращаются в едином формате "
    '`{"error": {"code": "...", "message": "...", "details": {}}}`.'
)

# Входящий X-Request-ID принимается только в безопасном виде (в логи и заголовки).
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")


# Служебные пробы вызываются каждые несколько секунд: без шума в логах и трейсах.
QUIET_PATHS = frozenset({"/health", "/metrics"})
# Шаблоны сопоставляются с полным URL, включая query (/health?x=1).
TRACING_EXCLUDED_URLS = ",".join(f"{path}(\\?.*)?$" for path in sorted(QUIET_PATHS))


# Шаблон пути без значений параметров. Маршруты Starlette (/docs, /openapi.json)
# не кладут route в scope, поэтому ищем их по endpoint.
def route_path(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None:
        return route.path
    endpoint = request.scope.get("endpoint")
    if endpoint is not None:
        for candidate in request.app.routes:
            if getattr(candidate, "endpoint", None) is endpoint:
                return candidate.path
    return "unmatched"


# Ошибки валидации отдаются в формате сервиса (400), поэтому 422 FastAPI не нужен.
def build_openapi(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema is None:
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        for path in schema["paths"].values():
            for operation in path.values():
                operation["responses"].pop("422", None)
        for name in ("HTTPValidationError", "ValidationError"):
            schema.get("components", {}).get("schemas", {}).pop(name, None)
        app.openapi_schema = schema
    return app.openapi_schema


# Starlette поднимает этот RuntimeError, когда клиент оборвал соединение до ответа.
CLIENT_CLOSED_REQUEST = 499


def is_client_disconnect(error: Exception) -> bool:
    return isinstance(error, RuntimeError) and str(error) == "No response returned."


def resolve_request_id(incoming: str | None) -> str:
    if incoming is not None and REQUEST_ID_PATTERN.fullmatch(incoming):
        return incoming
    return str(uuid4())


# Фабрика позволяет задавать отдельное окружение при интеграционном тестировании.
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    provider = create_tracer_provider(settings)
    # Единый реестр для всех метрик сервиса. Приватный реестр не
    # содержит метрик процесса по умолчанию, поэтому добавляем их явно.
    metrics_registry = CollectorRegistry()
    ProcessCollector(registry=metrics_registry)
    PlatformCollector(registry=metrics_registry)
    GCCollector(registry=metrics_registry)

    # Владение соединениями и экспортёром ограничено жизненным циклом приложения.
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        instrumentor = SQLAlchemyInstrumentor()
        instrumentor.instrument(engine=engine.sync_engine, tracer_provider=provider)
        try:
            if not await dependencies_ready(engine, settings):
                raise RuntimeError("Service dependencies are unavailable")
            logger.info("service_started")
            yield
        finally:
            await engine.dispose()
            instrumentor.uninstrument()
            provider.shutdown()
            logger.info("service_stopped")

    app = FastAPI(
        title="SmartMeal user service",
        version="0.1.0",
        description=API_DESCRIPTION,
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.passwords = PasswordService(settings)
    app.state.tracer_provider = provider
    app.state.metrics_registry = metrics_registry
    app.include_router(router)
    app.include_router(auth_router)
    app.openapi = lambda: build_openapi(app)

    # Request ID относится к текущему HTTP-вызову; query и тела не логируются.
    @app.middleware("http")
    async def request_logging(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        structlog.contextvars.clear_contextvars()
        request_id = resolve_request_id(request.headers.get("X-Request-ID"))
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception as error:
                if is_client_disconnect(error):
                    # Клиент ушёл, отвечать некому: не ошибка сервиса.
                    response = Response(status_code=CLIENT_CLOSED_REQUEST)
                else:
                    # 500 собирается здесь, а не в ServerErrorMiddleware: так в
                    # ответе есть X-Request-ID, а лог несёт тот же request_id.
                    logger.error("unhandled_error", exc_info=True)
                    response = error_response(
                        500, "INTERNAL_ERROR", "Internal server error"
                    )
            response.headers["X-Request-ID"] = request_id
            path = route_path(request)
            quiet = path in QUIET_PATHS and response.status_code < 400
            log = logger.debug if quiet else logger.info
            log(
                "http_request",
                method=request.method,
                path=path,
                status_code=response.status_code,
                duration_seconds=round(perf_counter() - started, 6),
            )
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    Instrumentator(
        registry=metrics_registry,
        should_group_status_codes=False,
        should_ignore_untemplated=True,
    ).instrument(app).expose(app, include_in_schema=False)
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, excluded_urls=TRACING_EXCLUDED_URLS
    )
    return app
