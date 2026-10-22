from pathlib import Path

import pytest

CONFTEST = (Path(__file__).resolve().parents[1] / "conftest.py").read_text()

TESTS = """
import pytest

@pytest.mark.skipif(True, reason="no db")
def test_skipped(): ...

def test_runtime_skip():
    pytest.skip("runtime")

@pytest.mark.xfail(reason="known")
def test_xfail(): assert False

def test_ok(): ...
"""

MODULE_SKIP = """
import pytest
pytest.importorskip("module_that_does_not_exist")
def test_never(): ...
"""

MODULE_LEVEL_SKIP = """
import pytest
pytest.skip("whole module", allow_module_level=True)
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeconftest(CONFTEST.replace("pytest_plugins", "_unused"))
    pytester.makepyfile(test_cases=TESTS)
    return pytester


def test_skips_are_allowed_by_default(project, monkeypatch) -> None:
    monkeypatch.delenv("FAIL_ON_SKIP", raising=False)
    result = project.runpytest_subprocess()
    result.assert_outcomes(passed=1, skipped=2, xfailed=1)


def test_skips_fail_with_fail_on_skip_but_xfail_and_pass_do_not(
    project, monkeypatch
) -> None:
    monkeypatch.setenv("FAIL_ON_SKIP", "1")
    result = project.runpytest_subprocess()
    # skipif падает на этапе setup (error), runtime-skip — на call (failed).
    result.assert_outcomes(passed=1, failed=1, errors=1, xfailed=1)
    result.stdout.fnmatch_lines(["*Skipped test is not allowed with FAIL_ON_SKIP=1*"])


@pytest.mark.parametrize("source", [MODULE_SKIP, MODULE_LEVEL_SKIP])
def test_skips_during_collection_fail_with_fail_on_skip(
    project, monkeypatch, source: str
) -> None:
    monkeypatch.setenv("FAIL_ON_SKIP", "1")
    project.makepyfile(test_collect=source)
    result = project.runpytest_subprocess("test_collect.py")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*Skipped test is not allowed with FAIL_ON_SKIP=1*"])
