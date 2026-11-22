from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path

from .browser import PlaywrightCli
from .config import Settings
from .engine import TestEngine
from .models import TaskState, TaskStatus
from .planner import build_planner
from .report import build_report, write_report
from .repository import TaskRepository
from .xlsx_reader import read_xlsx_cases


class TaskManager:
    def __init__(self, settings: Settings) -> None:
        settings.ensure_directories()
        self.settings = settings
        assert settings.database_path is not None
        self.repository = TaskRepository(settings.database_path)
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.max_concurrency),
            thread_name_prefix="lanus-task",
        )
        self._lock = threading.Lock()
        self._futures: dict[str, Future[TaskState]] = {}
        self._browsers: dict[str, PlaywrightCli] = {}

    def create_task(
        self,
        case_file: str | Path,
        *,
        target_url: str | None = None,
        headed: bool | None = None,
        max_step_attempts: int | None = None,
        task_id: str | None = None,
    ) -> TaskState:
        cases = read_xlsx_cases(case_file)
        resolved_target = target_url or cases.target_url
        if not resolved_target:
            raise ValueError("用例未提供目标地址，请在启动任务时传入 target_url")
        state = TaskState(
            task_id=task_id or f"ui_{uuid.uuid4().hex[:12]}",
            source_file=cases.source_file,
            sheet_name=cases.sheet_name,
            target_url=resolved_target,
            headed=self.settings.headed_default if headed is None else headed,
            cases=cases.cases,
            max_step_attempts=max_step_attempts or self.settings.max_step_attempts,
            max_action_rounds=self.settings.max_action_rounds,
            status=TaskStatus.CREATED,
        )
        self.repository.create(state)
        return state

    def create_from_upload(
        self,
        filename: str,
        content: bytes,
        *,
        target_url: str | None = None,
        headed: bool | None = None,
        max_step_attempts: int | None = None,
    ) -> TaskState:
        if Path(filename).suffix.lower() != ".xlsx":
            raise ValueError("当前仅支持 .xlsx 用例文件")
        task_id = f"ui_{uuid.uuid4().hex[:12]}"
        upload_dir = self.settings.task_dir(task_id) / "uploads"
        path = upload_dir / Path(filename).name
        path.write_bytes(content)
        return self.create_task(
            path,
            target_url=target_url,
            headed=headed,
            max_step_attempts=max_step_attempts,
            task_id=task_id,
        )

    def start(self, task_id: str) -> Future[TaskState]:
        state = self._require_state(task_id)
        if state.status not in {TaskStatus.CREATED, TaskStatus.RUNNING, TaskStatus.RETRYING}:
            raise ValueError(f"任务状态 {state.status.value} 不能直接启动")
        with self._lock:
            current = self._futures.get(task_id)
            if current and not current.done():
                return current
            browser = PlaywrightCli(self.settings, headed=state.headed)
            self._browsers[task_id] = browser
            future = self._pool.submit(self._run_task, state, browser)
            self._futures[task_id] = future
            return future

    def _run_task(self, state: TaskState, browser: PlaywrightCli) -> TaskState:
        try:
            engine = TestEngine(
                self.settings,
                self.repository,
                build_planner(self.settings),
                browser,
            )
            state.status = TaskStatus.RUNNING
            self.repository.save(state)
            result = engine.run(state)
            self.repository.save(result)
            return result
        except Exception as exc:
            failed = self.repository.load(state.task_id) or state
            failed.status = TaskStatus.FAILED
            failed.error = f"任务启动失败：{exc}"
            write_report(failed, self.settings.task_dir(state.task_id) / "reports")
            self.repository.save(failed)
            browser.close(state.task_id)
            return failed
        finally:
            with self._lock:
                self._browsers.pop(state.task_id, None)

    def pause(self, task_id: str) -> TaskState:
        state = self._require_state(task_id)
        if state.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED}:
            raise ValueError(f"任务已结束：{state.status.value}")
        self.repository.set_signal(task_id, pause=True, stop=False)
        return self._require_state(task_id)

    def resume(self, task_id: str) -> Future[TaskState]:
        state = self._require_state(task_id)
        if state.status != TaskStatus.PAUSED:
            raise ValueError("只有 PAUSED 任务可以恢复")
        self.repository.clear_pause(task_id)
        state.status = TaskStatus.RUNNING
        state.feedback = ""
        self.repository.save(state)
        return self.start(task_id)

    def stop(self, task_id: str) -> TaskState:
        state = self._require_state(task_id)
        if state.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED}:
            raise ValueError(f"任务已结束：{state.status.value}")
        self.repository.set_signal(task_id, pause=False, stop=True)
        with self._lock:
            browser = self._browsers.get(task_id)
            future = self._futures.get(task_id)
        if browser:
            browser.terminate()
        if state.status == TaskStatus.PAUSED or not future or future.done():
            state.status = TaskStatus.STOPPED
            state.error = "用户终止任务"
            write_report(state, self.settings.task_dir(task_id) / "reports")
            self.repository.save(state)
            browser = browser or PlaywrightCli(self.settings, headed=state.headed)
            browser.close(task_id)
        return self._require_state(task_id)

    def get(self, task_id: str) -> TaskState:
        return self._require_state(task_id)

    def get_report(self, task_id: str) -> dict:
        state = self._require_state(task_id)
        return build_report(state)

    def wait(self, task_id: str, timeout: float | None = None) -> TaskState:
        with self._lock:
            future = self._futures.get(task_id)
        if future:
            wait([future], timeout=timeout)
            if future.done():
                return future.result()
        return self._require_state(task_id)

    def shutdown(self) -> None:
        with self._lock:
            task_ids = list(self._browsers)
        for task_id in task_ids:
            self.stop(task_id)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _require_state(self, task_id: str) -> TaskState:
        state = self.repository.load(task_id)
        if state is None:
            raise KeyError(task_id)
        return state
