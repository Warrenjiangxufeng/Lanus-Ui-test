from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class StepStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    NOT_RUN = "NOT_RUN"


class AssertionKind(str, Enum):
    COMMAND_SUCCESS = "command_success"
    TEXT_CONTAINS = "text_contains"
    TEXT_NOT_CONTAINS = "text_not_contains"
    URL_CONTAINS = "url_contains"
    SEMANTIC = "semantic"


class TestStep(BaseModel):
    step_id: int
    text: str


class TestCase(BaseModel):
    case_id: int
    name: str
    steps: list[TestStep]


class TestCaseSet(BaseModel):
    source_file: str
    sheet_name: str
    target_url: str | None = None
    cases: list[TestCase]


class CliCommand(BaseModel):
    op: str
    args: list[str] = Field(default_factory=list)

    @field_validator("op")
    @classmethod
    def validate_op(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]*", value):
            raise ValueError("CLI operation contains invalid characters")
        return value

    @field_validator("args")
    @classmethod
    def validate_args(cls, values: list[str]) -> list[str]:
        clean: list[str] = []
        for value in values:
            value = str(value)
            if "\x00" in value or "\n" in value or "\r" in value:
                raise ValueError("CLI arguments may not contain NUL or newlines")
            if value in {"-s", "--session"} or value.startswith("-s="):
                raise ValueError("Session flags are managed by the engine")
            clean.append(value)
        return clean


class AssertionSpec(BaseModel):
    kind: AssertionKind = AssertionKind.SEMANTIC
    expected: str = ""
    description: str = ""


class PlanDecision(BaseModel):
    commands: list[CliCommand] = Field(default_factory=list)
    step_complete: bool = False
    assertion: AssertionSpec | None = None
    note: str = ""

    @model_validator(mode="after")
    def validate_progress(self) -> "PlanDecision":
        if not self.step_complete and not self.commands:
            raise ValueError("A plan must execute at least one command or mark the step complete")
        if self.step_complete and self.assertion is None:
            self.assertion = AssertionSpec(
                kind=AssertionKind.SEMANTIC,
                expected="当前自然语言步骤已达成",
                description="由验证模型根据快照和执行证据判断",
            )
        if (
            self.assertion
            and self.assertion.kind != AssertionKind.COMMAND_SUCCESS
            and not self.assertion.expected.strip()
        ):
            self.assertion = AssertionSpec(
                kind=AssertionKind.SEMANTIC,
                expected="当前自然语言步骤已达成",
                description="规划结果缺少确定性预期值，降级为语义验收",
            )
        return self


class JudgeDecision(BaseModel):
    passed: bool
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)


class CommandExecution(BaseModel):
    round_no: int
    command: CliCommand
    command_display: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    artifact_path: str | None = None


class StepEvidence(BaseModel):
    case_index: int
    case_name: str
    step_index: int
    step_id: int
    step_text: str
    attempt: int
    status: StepStatus = StepStatus.NOT_RUN
    assertion: AssertionSpec | None = None
    judge: JudgeDecision | None = None
    commands: list[CommandExecution] = Field(default_factory=list)
    snapshot: str = ""
    screenshot_path: str | None = None
    error: str = ""
    started_at: str = ""
    finished_at: str = ""


class RuntimeSignal(BaseModel):
    pause: bool = False
    stop: bool = False


class TaskState(BaseModel):
    task_id: str
    source_file: str
    sheet_name: str
    target_url: str | None = None
    headed: bool = False
    cases: list[TestCase]
    case_index: int = 0
    step_index: int = 0
    step_attempt: int = 1
    action_round: int = 0
    max_step_attempts: int = 3
    max_action_rounds: int = 8
    status: TaskStatus = TaskStatus.CREATED
    pending_commands: list[CliCommand] = Field(default_factory=list)
    awaiting_verify: bool = False
    browser_started: bool = False
    planner_repairs: int = 0
    current_plan: PlanDecision | None = None
    last_snapshot: str = ""
    last_screenshot: str | None = None
    feedback: str = ""
    executions: list[CommandExecution] = Field(default_factory=list)
    evidence: list[StepEvidence] = Field(default_factory=list)
    error: str = ""
    report_path: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def current_case(self) -> TestCase | None:
        if 0 <= self.case_index < len(self.cases):
            return self.cases[self.case_index]
        return None

    def current_step(self) -> TestStep | None:
        case = self.current_case()
        if case and 0 <= self.step_index < len(case.steps):
            return case.steps[self.step_index]
        return None

    def cursor_at_end(self) -> bool:
        return self.case_index >= len(self.cases)

    def public_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload.pop("pending_commands", None)
        return payload


class TaskCreateRequest(BaseModel):
    case_file: str
    target_url: str | None = None
    headed: bool = False
    max_step_attempts: int | None = None


class TaskView(BaseModel):
    task_id: str
    status: TaskStatus
    case_index: int
    step_index: int
    report_path: str | None = None
    error: str = ""


class ProductProof(BaseModel):
    """A deliberately small proof object for future platform integration."""

    task_state: dict[str, Any]
    report_path: str | None = None
    report: dict[str, Any] | None = None
