import logging
import os
import sys
import traceback
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from user.config import Settings

SERVICE_NAME = "smai-api-user"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep
# Кадры сервиса показывают место в нашем коде, нижние кадры — саму причину.
MAX_SERVICE_FRAMES = 10
MAX_INNER_FRAMES = 20
MAX_CAUSES = 5


# Связываем JSON-логи с активным трейсом без логирования содержимого запросов.
def add_context(
    logger: Any,
    method_name: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    context = trace.get_current_span().get_span_context()
    event["service"] = SERVICE_NAME
    event["trace_id"] = f"{context.trace_id:032x}" if context.is_valid else None
    event.setdefault("request_id", None)
    return event


# Тип, причины и кадры стека без текста исключения: сообщения драйверов могут
# содержать адреса подключения, SQL и данные запроса.
def exception_summary(
    logger: Any,
    method_name: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    exc_info = event.pop("exc_info", None)
    if exc_info is True:
        exc_info = sys.exc_info()
    elif isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if not exc_info or exc_info[0] is None:
        return event
    error_type, error, trace = exc_info
    causes: list[str] = []
    current = error
    while len(causes) < MAX_CAUSES:
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
        if current is None:
            break
        causes.append(type(current).__name__)
    frames = traceback.StackSummary.extract(
        traceback.walk_tb(trace), lookup_lines=False
    )
    service = [
        index
        for index, frame in enumerate(frames)
        if frame.filename.startswith(PACKAGE_DIR)
    ]
    inner = range(max(len(frames) - MAX_INNER_FRAMES, 0), len(frames))
    event["exception"] = {
        "type": error_type.__name__,
        "causes": causes,
        "frames": [
            f"{frames[index].filename}:{frames[index].lineno} {frames[index].name}"
            for index in sorted({*service[-MAX_SERVICE_FRAMES:], *inner})
        ],
    }
    return event


# Одинаковый формат применяется к structlog и стандартным логгерам библиотек.
def configure_logging(level: str) -> None:
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_context,
        exception_summary,
    ]
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.EventRenamer("message"),
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    logging.basicConfig(handlers=[handler], level=level, force=True)
    for name in ("uvicorn", "uvicorn.error"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
        server_logger.setLevel(level)
    # HTTP-события пишет middleware без query; access log мог бы раскрыть query.
    logging.getLogger("uvicorn.access").disabled = True
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


# Без коллектора сохраняем trace_id в логах, не делая внешних запросов.
def create_tracer_provider(settings: Settings) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    if settings.otel_exporter_otlp_endpoint is not None:
        endpoint = str(settings.otel_exporter_otlp_endpoint).rstrip("/") + "/v1/traces"
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=endpoint,
                    timeout=5,
                )
            )
        )
    return provider
