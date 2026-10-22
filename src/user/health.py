import asyncio
from typing import Literal

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from user.config import Settings
from user.errors import ErrorResponse, error_response

router = APIRouter()
logger = structlog.get_logger()


class HealthResponse(BaseModel):
    status: Literal["ok"] = Field(description="Всегда `ok`, если сервис готов.")

    model_config = {"json_schema_extra": {"example": {"status": "ok"}}}


# Ошибки зависимостей не должны раскрывать клиенту параметры подключения.
async def dependencies_ready(engine: AsyncEngine, settings: Settings) -> bool:
    try:
        async with asyncio.timeout(settings.db_timeout_seconds):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception as error:
        logger.error(
            "dependency_unavailable",
            dependency="postgresql",
            error_type=type(error).__name__,
        )
        return False
    return True


# Docker и gateway используют этот маршрут для проверки доступности БД.
@router.get(
    "/health",
    tags=["service"],
    summary="Проверка готовности сервиса",
    description=(
        "Выполняет `SELECT 1` к PostgreSQL. Возвращает 200, если БД отвечает, "
        "и 503 в едином формате ошибок, если нет."
    ),
    response_model=HealthResponse,
    responses={
        503: {
            "model": ErrorResponse,
            "description": "База данных недоступна.",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "Service is unavailable",
                            "details": {},
                        }
                    }
                }
            },
        }
    },
)
async def health(request: Request) -> JSONResponse:
    if await dependencies_ready(request.app.state.engine, request.app.state.settings):
        return JSONResponse({"status": "ok"})
    return error_response(503, "INTERNAL_ERROR", "Service is unavailable")
