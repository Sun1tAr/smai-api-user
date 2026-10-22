import pytest


def test_server_listens_on_all_interfaces_port_8000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import user.__main__ as entrypoint

    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        entrypoint.uvicorn, "run", lambda app, **options: calls.append((app, options))
    )
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_NAME", "user")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "local-only")
    entrypoint.main()
    options = calls[0][1]
    assert options["host"] == "0.0.0.0"
    assert options["port"] == 8000
    assert options["access_log"] is False


def test_invalid_configuration_exits_with_names_but_without_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    import user.__main__ as entrypoint

    started: list[object] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda *a, **k: started.append(a))
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_NAME", "user")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "private-env-password")
    monkeypatch.setenv("DB_PORT", "private-not-a-port")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://u:private-url-pass@otel")
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.main()
    assert exit_info.value.code == 1
    assert started == []
    output = capsys.readouterr()
    text = output.out + output.err
    for secret in ("private-env-password", "private-not-a-port", "private-url-pass"):
        assert secret not in text
    assert "Traceback" not in text
    event = json.loads(output.out.splitlines()[-1])
    assert event["message"] == "invalid_configuration"
    assert {item["field"] for item in event["fields"]} == {
        "db_port",
        "otel_exporter_otlp_endpoint",
    }
