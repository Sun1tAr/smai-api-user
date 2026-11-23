import structlog
import uvicorn
from pydantic import ValidationError

from user.config import Settings
from user.main import create_app
from user.observability import configure_logging


# Ошибки настроек описываются именами полей и типами: значения могут быть секретами.
def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        configure_logging("INFO")
        structlog.get_logger().error(
            "invalid_configuration",
            fields=[
                {
                    "field": ".".join(str(part) for part in item["loc"]),
                    "type": item["type"],
                }
                for item in error.errors()
            ],
        )
        raise SystemExit(1) from None


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        # Внутри контейнера нужен 0.0.0.0; наружу порт публикует только Compose.
        host="0.0.0.0",
        port=8000,
        # Логирование настроено в create_app; HTTP-события пишет middleware.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
