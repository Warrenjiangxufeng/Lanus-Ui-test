from __future__ import annotations

from pathlib import Path

from lanus_ui_test.browser import BrowserController
from lanus_ui_test.config import Settings
from lanus_ui_test.engine import TestEngine as Engine
from lanus_ui_test.models import (
    AssertionKind,
    AssertionSpec,
    CliCommand,
    CommandExecution,
    JudgeDecision,
    PlanDecision,
    TaskState,
    TaskStatus,
    TestCase as CaseModel,
    TestStep as StepModel,
)
from lanus_ui_test.planner import JudgeRequest, PlanRequest
from lanus_ui_test.repository import TaskRepository


class FakePlanner:
    def plan_step(self, request: PlanRequest) -> PlanDecision:
        step = request.case.steps[request.step_index]
        if step.step_id == 1:
            return PlanDecision(
                commands=[
                    CliCommand(op="goto", args=["http://127.0.0.1:5000/login"]),
                    CliCommand(op="snapshot"),
                ],
                step_complete=True,
                assertion=AssertionSpec(kind=AssertionKind.COMMAND_SUCCESS),
            )
        return PlanDecision(
            commands=[],
            step_complete=True,
            assertion=AssertionSpec(
                kind=AssertionKind.TEXT_CONTAINS,
                expected="首页",
            ),
        )

    def judge_step(self, request: JudgeRequest) -> JudgeDecision:
        return JudgeDecision(passed=True, reason="页面符合预期")


class FakeBrowser:
    def __init__(self) -> None:
        self.closed = False
        self.commands: list[CliCommand] = []

    def execute(
        self,
        task_id: str,
        command: CliCommand,
        *,
        step_text: str,
        round_no: int,
        timeout: int | None = None,
    ) -> CommandExecution:
        self.commands.append(command)
        return CommandExecution(
            round_no=round_no,
            command=command,
            command_display=command.op,
            returncode=0,
            stdout="goto ok",
        )

    def observe(
        self,
        task_id: str,
        *,
        snapshot_path: Path,
        screenshot_path: Path,
        timeout: int | None = None,
    ) -> tuple[CommandExecution, CommandExecution]:
        return (
            CommandExecution(
                round_no=0,
                command=CliCommand(op="snapshot"),
                command_display="snapshot",
                returncode=0,
                stdout="登录成功，进入首页",
            ),
            CommandExecution(
                round_no=0,
                command=CliCommand(op="screenshot"),
                command_display="screenshot",
                returncode=0,
            ),
        )

    def close(self, task_id: str) -> None:
        self.closed = True

    def terminate(self) -> bool:
        return False


def test_graph_runs_case_and_writes_report(tmp_path: Path) -> None:
    settings = Settings(
        artifacts_dir=tmp_path / "artifacts",
        database_path=tmp_path / "tasks.sqlite3",
        max_action_rounds=6,
    )
    settings.ensure_directories()
    repository = TaskRepository(settings.database_path)
    state = TaskState(
        task_id="ui_engine_test",
        source_file="cases.xlsx",
        sheet_name="AI执行用例",
        target_url="http://127.0.0.1:5000",
        cases=[
            CaseModel(
                case_id=1,
                name="登录",
                steps=[
                    StepModel(step_id=1, text="进入登录页"),
                    StepModel(step_id=2, text="校验进入首页"),
                ],
            )
        ],
    )
    repository.create(state)
    browser = FakeBrowser()
    engine = Engine(settings, repository, FakePlanner(), browser)
    result = engine.run(state)

    assert result.status == TaskStatus.COMPLETED
    assert result.report_path is not None
    assert Path(result.report_path).exists()
    assert Path(result.report_path).with_suffix(".html").exists()
    assert browser.closed is True
    assert all(item.status == "PASS" for item in result.evidence)
    assert [command.op for command in browser.commands] == ["open", "goto", "snapshot"]


def test_pause_keeps_session_and_resume_rebuilds_graph(tmp_path: Path) -> None:
    settings = Settings(
        artifacts_dir=tmp_path / "artifacts",
        database_path=tmp_path / "tasks.sqlite3",
    )
    settings.ensure_directories()
    repository = TaskRepository(settings.database_path)
    state = TaskState(
        task_id="ui_pause_test",
        source_file="cases.xlsx",
        sheet_name="AI执行用例",
        target_url="http://127.0.0.1:5000",
        cases=[
            CaseModel(
                case_id=1,
                name="登录",
                steps=[
                    StepModel(step_id=1, text="进入登录页"),
                    StepModel(step_id=2, text="校验进入首页"),
                ],
            )
        ],
    )
    repository.create(state)
    repository.set_signal(state.task_id, pause=True)
    browser = FakeBrowser()

    paused = Engine(settings, repository, FakePlanner(), browser).run(state)
    assert paused.status == TaskStatus.PAUSED
    assert browser.closed is False
    assert browser.commands == []

    repository.clear_pause(state.task_id)
    resumed = Engine(settings, repository, FakePlanner(), browser).run(paused)
    assert resumed.status == TaskStatus.COMPLETED
    assert browser.closed is True
