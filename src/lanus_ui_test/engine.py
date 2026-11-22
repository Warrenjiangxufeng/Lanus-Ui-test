from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from langgraph.graph import END, START, StateGraph

from .browser import BrowserController, TaskStopped
from .config import Settings
from .models import (
    AssertionKind,
    AssertionSpec,
    CliCommand,
    CommandExecution,
    JudgeDecision,
    PlanDecision,
    StepEvidence,
    StepStatus,
    TaskState,
    TaskStatus,
)
from .planner import JudgeRequest, PlanRequest, Planner, PlannerError
from .redaction import extract_secrets, redact_command, redact_text
from .report import build_report, write_report
from .repository import TaskRepository, utc_now


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestEngine:
    def __init__(
        self,
        settings: Settings,
        repository: TaskRepository,
        planner: Planner,
        browser: BrowserController,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.planner = planner
        self.browser = browser
        self._graph = self._build_graph()

    def _durable(self, fn: Callable[[TaskState], TaskState]) -> Callable[[TaskState], dict]:
        def wrapped(state: TaskState) -> dict:
            next_state = fn(state.model_copy(deep=True))
            next_state.updated_at = utc_now()
            self.repository.save(next_state)
            return next_state.model_dump(mode="python")

        return wrapped

    def _build_graph(self):
        graph = StateGraph(TaskState)
        graph.add_node("bootstrap", self._durable(self._bootstrap))
        graph.add_node("gate", self._durable(self._gate))
        graph.add_node("plan", self._durable(self._plan))
        graph.add_node("execute", self._durable(self._execute))
        graph.add_node("observe", self._durable(self._observe))
        graph.add_node("verify", self._durable(self._verify))
        graph.add_node("finalize", self._durable(self._finalize))
        graph.add_node("release", self._durable(self._release))

        graph.add_edge(START, "bootstrap")
        graph.add_edge("bootstrap", "gate")
        graph.add_conditional_edges(
            "gate",
            self._route_gate,
            {
                "plan": "plan",
                "execute": "execute",
                "observe": "observe",
                "finalize": "finalize",
                "release": "release",
            },
        )
        graph.add_conditional_edges(
            "plan",
            self._route_plan,
            {"execute": "execute", "observe": "observe", "gate": "gate"},
        )
        graph.add_edge("execute", "gate")
        graph.add_edge("observe", "verify")
        graph.add_edge("verify", "gate")
        graph.add_edge("finalize", "release")
        graph.add_edge("release", END)
        return graph.compile()

    def _bootstrap(self, state: TaskState) -> TaskState:
        signal = self.repository.get_signal(state.task_id)
        if signal.stop:
            state.status = TaskStatus.STOPPED
            state.error = "用户终止任务"
            return state
        if signal.pause:
            state.status = TaskStatus.PAUSED
            return state
        if state.browser_started:
            return state
        if not state.target_url:
            state.status = TaskStatus.FAILED
            state.error = "缺少目标地址，无法启动浏览器"
            return state

        command = CliCommand(op="open", args=[state.target_url])
        try:
            execution = self.browser.execute(
                state.task_id,
                command,
                step_text="浏览器初始化",
                round_no=0,
            )
        except Exception as exc:
            state.status = TaskStatus.FAILED
            state.error = redact_text(f"浏览器启动失败：{exc}")
            return state

        echo = execution.stdout + "\n" + execution.stderr
        execution.stdout = redact_text(execution.stdout)
        execution.stderr = redact_text(execution.stderr)
        state.executions.append(execution)
        if execution.returncode != 0:
            state.status = TaskStatus.FAILED
            state.error = f"浏览器启动失败：{redact_text(echo)[-800:]}"
            return state

        state.browser_started = True
        state.status = TaskStatus.RUNNING
        case = state.current_case()
        step = state.current_step()
        if case is not None and step is not None:
            evidence = self._get_or_create_evidence(state)
            evidence.commands.append(execution)
            task_dir = self.settings.task_dir(state.task_id)
            stem = (
                f"case_{state.case_index + 1}_step_{step.step_id}_"
                f"attempt_{state.step_attempt}_bootstrap"
            )
            try:
                snapshot, screenshot = self.browser.observe(
                    state.task_id,
                    snapshot_path=task_dir / "snapshots" / f"{stem}.md",
                    screenshot_path=task_dir / "screenshots" / f"{stem}.png",
                )
                for item in (snapshot, screenshot):
                    item.stdout = redact_text(item.stdout)
                    item.stderr = redact_text(item.stderr)
                    state.executions.append(item)
                    evidence.commands.append(item)
                state.last_snapshot = snapshot.stdout or snapshot.stderr
                evidence.snapshot = state.last_snapshot
                if screenshot.returncode == 0 and screenshot.artifact_path:
                    state.last_screenshot = screenshot.artifact_path
                    evidence.screenshot_path = state.last_screenshot
            except Exception as exc:
                state.feedback = redact_text(f"页面证据采集失败：{exc}")
        return state

    def run(self, state: TaskState) -> TaskState:
        state.status = TaskStatus.RUNNING
        state.error = ""
        initial = state.model_dump(mode="python")
        try:
            raw = self._graph.invoke(
                initial,
                config={"recursion_limit": self.settings.recursion_limit},
            )
            return TaskState.model_validate(raw)
        except TaskStopped as exc:
            state = self.repository.load(state.task_id) or state
            state.status = TaskStatus.STOPPED
            state.error = str(exc)
            self._finalize_stopped_state(state)
            return state
        except Exception as exc:
            state = self.repository.load(state.task_id) or state
            state.status = TaskStatus.FAILED
            state.error = str(exc)
            self._finalize_stopped_state(state)
            return state

    def _finalize_stopped_state(self, state: TaskState) -> None:
        report_path = write_report(state, self.settings.task_dir(state.task_id) / "reports")
        state.report_path = str(report_path)
        self.repository.save(state)
        self.browser.close(state.task_id)

    def _gate(self, state: TaskState) -> TaskState:
        signal = self.repository.get_signal(state.task_id)
        if signal.stop:
            state.status = TaskStatus.STOPPED
            state.error = "用户终止任务"
            return state
        if signal.pause:
            state.status = TaskStatus.PAUSED
            return state
        if state.status in {
            TaskStatus.STOPPED,
            TaskStatus.FAILED,
            TaskStatus.COMPLETED,
        }:
            return state
        if state.cursor_at_end():
            state.status = TaskStatus.COMPLETED
            return state
        case = state.current_case()
        if case is None:
            state.status = TaskStatus.FAILED
            state.error = "当前用例游标无效"
            return state
        if state.step_index >= len(case.steps):
            state.case_index += 1
            state.step_index = 0
            state.step_attempt = 1
            state.action_round = 0
            state.awaiting_verify = False
            state.pending_commands = []
            return self._gate(state)
        state.status = TaskStatus.RUNNING
        return state

    @staticmethod
    def _route_gate(state: TaskState) -> str:
        if state.status == TaskStatus.PAUSED:
            return "release"
        if state.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.STOPPED,
        }:
            return "finalize"
        if state.pending_commands:
            return "execute"
        if state.awaiting_verify:
            return "observe"
        return "plan"

    def _plan(self, state: TaskState) -> TaskState:
        case = state.current_case()
        step = state.current_step()
        if case is None or step is None:
            state.status = TaskStatus.FAILED
            state.error = "规划时找不到当前用例或步骤"
            return state
        state.action_round += 1
        if state.action_round > state.max_action_rounds:
            self._record_step_failure(
                state,
                f"当前步骤超过最大动作轮数 {state.max_action_rounds}",
            )
            return state

        previous = [
            item
            for item in state.evidence
            if item.case_index == state.case_index and item.step_index < state.step_index
        ]
        request = PlanRequest(
            task_id=state.task_id,
            target_url=state.target_url,
            case=case,
            step_index=state.step_index,
            attempt=state.step_attempt,
            action_round=state.action_round,
            snapshot=state.last_snapshot,
            screenshot_paths=[state.last_screenshot] if state.last_screenshot else [],
            recent_executions=[
                item.model_dump(mode="json") for item in state.executions[-12:]
            ],
            previous_steps=previous,
            feedback=state.feedback,
        )
        try:
            decision = self.planner.plan_step(request)
            invalid_ops = [
                command.op
                for command in decision.commands
                if command.op not in self._allowed_operations()
            ]
            if invalid_ops:
                raise PlannerError(f"规划了不允许的 CLI 操作：{invalid_ops}")
        except Exception as exc:
            reason = redact_text(f"规划失败：{exc}")
            if state.planner_repairs < self.settings.max_planner_repairs:
                state.planner_repairs += 1
                evidence = self._get_or_create_evidence(state)
                evidence.error = reason
                state.feedback = (
                    f"上一次规划无法执行：{reason}。请基于最新页面快照重新规划；"
                    "若元素未知，先 snapshot。"
                )
                state.pending_commands = [CliCommand(op="snapshot")]
                state.awaiting_verify = False
                state.status = TaskStatus.RETRYING
                return state
            self._record_step_failure(state, reason)
            return state

        evidence = self._get_or_create_evidence(state)
        evidence.started_at = evidence.started_at or _now()
        evidence.error = ""
        state.planner_repairs = 0
        state.current_plan = decision
        state.pending_commands = list(decision.commands)
        state.awaiting_verify = decision.step_complete
        state.feedback = ""
        state.status = TaskStatus.RUNNING
        signal = self.repository.get_signal(state.task_id)
        if signal.stop:
            state.status = TaskStatus.STOPPED
            state.error = "用户终止任务"
            state.pending_commands = []
            state.awaiting_verify = False
        elif signal.pause:
            state.status = TaskStatus.PAUSED
            state.pending_commands = []
            state.awaiting_verify = False
        return state

    @staticmethod
    def _route_plan(state: TaskState) -> str:
        if state.status in {TaskStatus.FAILED, TaskStatus.PAUSED, TaskStatus.STOPPED}:
            return "gate"
        if state.pending_commands:
            return "execute"
        if state.awaiting_verify:
            return "observe"
        return "gate"

    def _execute(self, state: TaskState) -> TaskState:
        case = state.current_case()
        step = state.current_step()
        if case is None or step is None or not state.pending_commands:
            state.status = TaskStatus.FAILED
            state.error = "执行时找不到当前用例、步骤或待执行命令"
            return state
        command = state.pending_commands.pop(0)
        try:
            execution = self.browser.execute(
                state.task_id,
                command,
                step_text=step.text,
                round_no=state.action_round,
            )
        except TaskStopped:
            raise
        except Exception as exc:
            execution = CommandExecution(
                round_no=state.action_round,
                command=command,
                command_display=command.op,
                returncode=-1,
                stderr=str(exc),
            )

        secrets = extract_secrets(execution.command, step.text)
        execution.stdout = redact_text(execution.stdout, secrets)
        execution.stderr = redact_text(execution.stderr, secrets)
        execution.command = redact_command(execution.command, step.text)
        state.executions.append(execution)
        evidence = self._get_or_create_evidence(state)
        evidence.commands.append(execution)
        if command.op == "snapshot" and execution.returncode == 0:
            state.last_snapshot = execution.stdout or execution.stderr
        if command.op == "screenshot" and execution.artifact_path:
            state.last_screenshot = execution.artifact_path

        if execution.returncode != 0:
            state.pending_commands = []
            state.awaiting_verify = False
            state.status = TaskStatus.RETRYING
            reason = execution.stderr or execution.stdout or f"returncode={execution.returncode}"
            self._record_step_failure(state, f"命令执行失败：{reason}")
        return state

    def _observe(self, state: TaskState) -> TaskState:
        case = state.current_case()
        step = state.current_step()
        if case is None or step is None:
            state.status = TaskStatus.FAILED
            state.error = "采集证据时找不到当前用例或步骤"
            return state
        task_dir = self.settings.task_dir(state.task_id)
        stem = f"case_{state.case_index + 1}_step_{step.step_id}_attempt_{state.step_attempt}"
        evidence = self._get_or_create_evidence(state)
        try:
            snapshot, screenshot = self.browser.observe(
                state.task_id,
                snapshot_path=task_dir / "snapshots" / f"{stem}.md",
                screenshot_path=task_dir / "screenshots" / f"{stem}.png",
            )
            text = snapshot.stdout or snapshot.stderr
            state.last_snapshot = redact_text(text)
            evidence.snapshot = state.last_snapshot
            if screenshot.returncode == 0:
                expected = task_dir / "screenshots" / f"{stem}.png"
                state.last_screenshot = str(expected) if expected.exists() else None
            evidence.screenshot_path = state.last_screenshot
        except Exception as exc:
            state.feedback = f"证据采集失败：{exc}"
        return state

    def _verify(self, state: TaskState) -> TaskState:
        case = state.current_case()
        step = state.current_step()
        if case is None or step is None:
            state.status = TaskStatus.FAILED
            state.error = "验收时找不到当前用例或步骤"
            return state
        assertion = state.current_plan.assertion if state.current_plan else None
        evidence = self._get_or_create_evidence(state)
        evidence.assertion = assertion
        passed, judge = self._evaluate(assertion, state, evidence)
        evidence.judge = judge
        evidence.finished_at = _now()

        if passed:
            evidence.status = StepStatus.PASS
            evidence.error = ""
            state.step_index += 1
            state.step_attempt = 1
            state.action_round = 0
            state.planner_repairs = 0
            state.pending_commands = []
            state.awaiting_verify = False
            state.current_plan = None
            state.feedback = ""
            state.status = TaskStatus.RUNNING
            return state

        evidence.status = StepStatus.FAIL
        evidence.error = judge.reason
        self._record_step_failure(state, judge.reason)
        return state

    def _evaluate(
        self,
        assertion: AssertionSpec | None,
        state: TaskState,
        evidence: StepEvidence,
    ) -> tuple[bool, JudgeDecision]:
        assertion = assertion or AssertionSpec(
            kind=AssertionKind.SEMANTIC,
            expected="当前步骤已达成",
        )
        observed = "\n".join(
            [
                state.last_snapshot,
                *[
                    f"{item.stdout}\n{item.stderr}"
                    for item in evidence.commands[-10:]
                ],
            ]
        )
        expected = assertion.expected.strip()
        if assertion.kind == AssertionKind.COMMAND_SUCCESS:
            passed = bool(evidence.commands) and all(
                item.returncode == 0 for item in evidence.commands
            )
            return passed, JudgeDecision(
                passed=passed,
                reason="所有规划命令返回成功" if passed else "存在非零返回码命令",
                evidence_refs=[item.command_display for item in evidence.commands[-5:]],
            )
        if assertion.kind == AssertionKind.TEXT_CONTAINS:
            passed = bool(expected) and expected.casefold() in observed.casefold()
            return passed, JudgeDecision(
                passed=passed,
                reason=f"快照{'包含' if passed else '未包含'}预期文本：{expected}",
                evidence_refs=[expected] if passed else [],
            )
        if assertion.kind == AssertionKind.TEXT_NOT_CONTAINS:
            passed = bool(expected) and expected.casefold() not in observed.casefold()
            return passed, JudgeDecision(
                passed=passed,
                reason=f"快照{'未包含' if passed else '仍包含'}禁止文本：{expected}",
                evidence_refs=[expected] if passed else [],
            )
        if assertion.kind == AssertionKind.URL_CONTAINS:
            passed = bool(expected) and expected.casefold() in observed.casefold()
            return passed, JudgeDecision(
                passed=passed,
                reason=f"执行输出{'包含' if passed else '未包含'}目标地址片段：{expected}",
                evidence_refs=[expected] if passed else [],
            )
        try:
            case = state.current_case()
            if case is None:
                raise PlannerError("当前用例不存在")
            judge = (
                self.planner.judge_step(
                    JudgeRequest(
                        task_id=state.task_id,
                        case=case,
                        step_index=state.step_index,
                        snapshot=state.last_snapshot,
                        screenshot_paths=(
                            [state.last_screenshot] if state.last_screenshot else []
                        ),
                        assertion=assertion,
                        execution_history=[
                            item.model_dump(mode="json")
                            for item in evidence.commands[-20:]
                        ],
                    )
                )
            )
            return judge.passed, judge
        except Exception as exc:
            return False, JudgeDecision(
                passed=False,
                reason=f"语义验收失败：{exc}",
            )

    def _record_step_failure(self, state: TaskState, reason: str) -> None:
        reason = redact_text(reason)
        case = state.current_case()
        step = state.current_step()
        if case is None or step is None:
            state.status = TaskStatus.FAILED
            state.error = reason
            return
        evidence = self._get_or_create_evidence(state)
        evidence.status = StepStatus.FAIL
        evidence.error = reason
        evidence.finished_at = evidence.finished_at or _now()
        if state.step_attempt >= state.max_step_attempts:
            for remaining_index, remaining_step in enumerate(
                case.steps[state.step_index + 1 :], start=state.step_index + 1
            ):
                state.evidence.append(
                    StepEvidence(
                        case_index=state.case_index,
                        case_name=case.name,
                        step_index=remaining_index,
                        step_id=remaining_step.step_id,
                        step_text=remaining_step.text,
                        attempt=state.step_attempt,
                        status=StepStatus.SKIPPED,
                        error=f"前置步骤 {step.step_id} 失败，未执行",
                    )
                )
            state.case_index += 1
            state.step_index = 0
            state.step_attempt = 1
            state.action_round = 0
            state.planner_repairs = 0
            state.feedback = ""
            state.status = TaskStatus.RUNNING
        else:
            state.step_attempt += 1
            state.action_round = 0
            state.planner_repairs = 0
            state.feedback = reason
            state.pending_commands = []
            state.awaiting_verify = False
            state.status = TaskStatus.RETRYING

    def _get_or_create_evidence(self, state: TaskState) -> StepEvidence:
        case = state.current_case()
        step = state.current_step()
        if case is None or step is None:
            raise RuntimeError("当前用例或步骤不存在")
        for item in reversed(state.evidence):
            if (
                item.case_index == state.case_index
                and item.step_index == state.step_index
                and item.attempt == state.step_attempt
            ):
                return item
        item = StepEvidence(
            case_index=state.case_index,
            case_name=case.name,
            step_index=state.step_index,
            step_id=step.step_id,
            step_text=step.text,
            attempt=state.step_attempt,
            started_at=_now(),
        )
        state.evidence.append(item)
        return item

    def _finalize(self, state: TaskState) -> TaskState:
        if state.status == TaskStatus.RUNNING:
            state.status = TaskStatus.COMPLETED
        state.report_path = str(
            write_report(state, self.settings.task_dir(state.task_id) / "reports")
        )
        state.current_plan = None
        state.pending_commands = []
        return state

    def _release(self, state: TaskState) -> TaskState:
        if state.status != TaskStatus.PAUSED:
            self.browser.close(state.task_id)
        return state

    @staticmethod
    def _allowed_operations() -> set[str]:
        from .browser import PLANNER_OPERATIONS

        return PLANNER_OPERATIONS
