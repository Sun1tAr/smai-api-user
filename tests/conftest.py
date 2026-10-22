import os

import pytest

pytest_plugins = ["pytester"]


def forbid_skip(report, nodeid: str) -> None:
    # xfail — ожидаемое падение, а не пропуск окружения.
    if report.skipped and not hasattr(report, "wasxfail"):
        report.outcome = "failed"
        report.longrepr = f"Skipped test is not allowed with FAIL_ON_SKIP=1: {nodeid}"


# Пропущенный тест при FAIL_ON_SKIP=1 считается ошибкой: иначе недошедшие до
# контейнера TEST_DB_* молча отключили бы все интеграционные тесты.
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if os.getenv("FAIL_ON_SKIP") == "1":
        forbid_skip(outcome.get_result(), item.nodeid)


# Пропуски при сборе: pytest.skip(allow_module_level=True), importorskip.
@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    if os.getenv("FAIL_ON_SKIP") == "1":
        forbid_skip(outcome.get_result(), collector.nodeid)
