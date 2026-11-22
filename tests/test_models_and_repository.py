from pathlib import Path

import pytest
from pydantic import ValidationError

from lanus_ui_test.browser import PLANNER_OPERATIONS
from lanus_ui_test.models import (
    AssertionKind,
    AssertionSpec,
    CliCommand,
    PlanDecision,
    TaskState,
    TaskStatus,
)
from lanus_ui_test.repository import TaskRepository


def test_cli_command_rejects_session_override() -> None:
    with pytest.raises(ValidationError):
        CliCommand(op="click", args=["-s=other", "e1"])


def test_planner_cannot_close_browser_session() -> None:
    assert "close" not in PLANNER_OPERATIONS


def test_empty_deterministic_assertion_falls_back_to_semantic() -> None:
    decision = PlanDecision(
        commands=[],
        step_complete=True,
        assertion=AssertionSpec(kind=AssertionKind.URL_CONTAINS, expected=""),
    )
    assert decision.assertion is not None
    assert decision.assertion.kind == AssertionKind.SEMANTIC


def test_repository_round_trip_and_signal(tmp_path: Path) -> None:
    repository = TaskRepository(tmp_path / "tasks.sqlite3")
    state = TaskState(
        task_id="ui_test",
        source_file="cases.xlsx",
        sheet_name="AI执行用例",
        target_url="http://127.0.0.1:5000",
        cases=[],
    )
    repository.create(state)
    repository.set_signal("ui_test", pause=True)
    assert repository.get_signal("ui_test").pause is True
    loaded = repository.load("ui_test")
    assert loaded is not None
    loaded.status = TaskStatus.PAUSED
    repository.save(loaded)
    assert repository.get_status("ui_test") == TaskStatus.PAUSED
