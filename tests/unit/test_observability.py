import json
import logging

import pytest
from uvicorn import Config

from user.observability import configure_logging


def test_uvicorn_logs_use_the_service_json_format(
    capsys: pytest.CaptureFixture[str],
) -> None:
    Config("user.main:create_app", factory=True)
    configure_logging("INFO")
    logging.getLogger("uvicorn.error").info("Server started")
    logging.getLogger("uvicorn.access").info("GET /?token=private-query-value")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "private-query-value" not in captured.out
    event = json.loads(captured.out)
    assert event["message"] == "Server started"
    assert event["service"] == "smai-api-user"
    assert event["level"] == "info"
    assert event["timestamp"]
    assert event["trace_id"] is None
    assert event["request_id"] is None
