"""The dashboard must not contain SQL or read secrets; ui_queries is the only SQL layer."""

import re
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
FILES = sorted(DASHBOARD.rglob("*.py"))

UPPER_SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE)\b")
LOWER_SQL = re.compile(
    r"\b(insert\s+into|delete\s+from|update\s+\S+\s+set|select\s+[\w*,.\s]+\s+from\s+\S+)",
    re.IGNORECASE,
)


def test_dashboard_files_found() -> None:
    assert FILES, "expected dashboard/*.py"


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(DASHBOARD)))
def test_no_sql_in_dashboard(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not UPPER_SQL.search(text), f"SQL keyword in {path.name}"
    assert not LOWER_SQL.search(text), f"SQL-looking statement in {path.name}"


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(DASHBOARD)))
def test_dashboard_never_reads_secret_values(path: Path) -> None:
    assert "get_secret_value" not in path.read_text(encoding="utf-8")
