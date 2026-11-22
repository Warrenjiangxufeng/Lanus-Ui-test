from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Protocol

from .config import Settings
from .models import CliCommand, CommandExecution
from .redaction import command_display


ALLOWED_OPERATIONS = {
    "open",
    "goto",
    "snapshot",
    "screenshot",
    "find",
    "click",
    "dblclick",
    "fill",
    "type",
    "press",
    "select",
    "check",
    "uncheck",
    "hover",
    "go-back",
    "go-forward",
    "reload",
    "tab-list",
    "tab-new",
    "tab-close",
    "tab-select",
    "dialog-accept",
    "dialog-dismiss",
    "requests",
    "request",
    "request-headers",
    "request-body",
    "response-headers",
    "response-body",
    "console",
    "cookie-list",
    "cookie-get",
    "localstorage-list",
    "localstorage-get",
    "sessionstorage-list",
    "sessionstorage-get",
    "close",
}

PLANNER_OPERATIONS = ALLOWED_OPERATIONS - {"close"}

PLANNER_OPTION_DENYLIST = {
    "--config",
    "--filename",
    "--persistent",
    "--profile",
}


class BrowserController(Protocol):
    def execute(
        self,
        task_id: str,
        command: CliCommand,
        *,
        step_text: str,
        round_no: int,
        timeout: int | None = None,
    ) -> CommandExecution: ...

    def observe(
        self,
        task_id: str,
        *,
        snapshot_path: Path,
        screenshot_path: Path,
        timeout: int | None = None,
    ) -> tuple[CommandExecution, CommandExecution]: ...

    def close(self, task_id: str) -> None: ...

    def terminate(self) -> bool: ...


class TaskStopped(RuntimeError):
    pass


class PlaywrightCli:
    def __init__(self, settings: Settings, *, headed: bool | None = None) -> None:
        self.settings = settings
        self.headed = settings.headed_default if headed is None else headed
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def _base_command(
        self,
        session: str,
        operation: str,
        args: list[str],
        *,
        trusted_options: bool,
    ) -> list[str]:
        if operation not in ALLOWED_OPERATIONS:
            raise ValueError(f"不允许执行 playwright-cli 操作：{operation}")
        if not trusted_options and any(arg in PLANNER_OPTION_DENYLIST for arg in args):
            raise ValueError("模型不得指定配置文件、输出文件或持久化目录")
        if self.settings.node_bin and self.settings.playwright_js:
            return [
                str(self.settings.node_bin),
                str(self.settings.playwright_js),
                f"-s={session}",
                operation,
                *args,
            ]
        if self.settings.playwright_bin:
            return [str(self.settings.playwright_bin), f"-s={session}", operation, *args]
        raise RuntimeError("未找到 playwright-cli，请设置 LANUS_PLAYWRIGHT_BIN")

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.settings.node_bin:
            env["PATH"] = (
                str(self.settings.node_bin.parent)
                + os.pathsep
                + env.get("PATH", "")
            )
        env.setdefault("NO_UPDATE_NOTIFIER", "1")
        return env

    def _normalize_command(self, command: CliCommand) -> CliCommand:
        if command.op != "open":
            return command
        args = list(command.args)
        if "--browser" not in args:
            args.extend(["--browser", "chrome"])
        if self.headed and "--headed" not in args:
            args.append("--headed")
        return command.model_copy(update={"args": args})

    def _run(
        self,
        task_id: str,
        command: CliCommand,
        *,
        step_text: str,
        round_no: int,
        timeout_seconds: int,
        respect_stop: bool = True,
        trusted_options: bool = False,
    ) -> CommandExecution:
        command = self._normalize_command(command)
        argv = self._base_command(
            task_id,
            command.op,
            command.args,
            trusted_options=trusted_options,
        )
        started = time.monotonic()
        if respect_stop and self._stop_event.is_set():
            raise TaskStopped("任务已收到强制停止信号")

        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._environment(),
            start_new_session=os.name != "nt",
        )
        with self._lock:
            self._process = process

        timed_out = False
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if respect_stop and self._stop_event.is_set():
                        self._kill_process_group(process)
                        raise TaskStopped("任务已收到强制停止信号")
                    if time.monotonic() - started >= timeout_seconds:
                        timed_out = True
                        self._kill_process_group(process)
                        stdout, stderr = process.communicate(timeout=5)
                        break
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None

        returncode = process.returncode if process.returncode is not None else -1
        if timed_out:
            returncode = 124
            stderr = (stderr or "") + f"\n命令执行超过 {timeout_seconds} 秒，已终止"
        return CommandExecution(
            round_no=round_no,
            command=command,
            command_display=command_display(command, step_text),
            returncode=returncode,
            stdout=(stdout or "")[:200_000],
            stderr=(stderr or "")[:100_000],
            duration_ms=round((time.monotonic() - started) * 1000),
            timed_out=timed_out,
        )

    def execute(
        self,
        task_id: str,
        command: CliCommand,
        *,
        step_text: str,
        round_no: int,
        timeout: int | None = None,
    ) -> CommandExecution:
        if command.op == "screenshot":
            screenshot_dir = self.settings.task_dir(task_id) / "screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            target = next(
                (value for value in command.args if not value.startswith("--")),
                None,
            )
            artifact = (
                screenshot_dir
                / f"planner_{round_no}_{time.time_ns()}.png"
            )
            args = [target] if target else []
            args.extend(["--filename", str(artifact), "--full-page"])
            command = CliCommand(op="screenshot", args=args)
            execution = self._run(
                task_id,
                command,
                step_text=step_text,
                round_no=round_no,
                timeout_seconds=timeout or self.settings.observe_timeout,
                trusted_options=True,
            )
            execution.artifact_path = str(artifact) if artifact.exists() else None
            return execution
        return self._run(
            task_id,
            command,
            step_text=step_text,
            round_no=round_no,
            timeout_seconds=timeout or self.settings.command_timeout,
        )

    def observe(
        self,
        task_id: str,
        *,
        snapshot_path: Path,
        screenshot_path: Path,
        timeout: int | None = None,
    ) -> tuple[CommandExecution, CommandExecution]:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = self._run(
            task_id,
            CliCommand(op="snapshot"),
            step_text="执行证据采集",
            round_no=0,
            timeout_seconds=timeout or self.settings.observe_timeout,
            trusted_options=True,
        )
        snapshot_path.write_text(snapshot.stdout or snapshot.stderr, encoding="utf-8")
        screenshot = self._run(
            task_id,
            CliCommand(op="screenshot", args=["--filename", str(screenshot_path), "--full-page"]),
            step_text="执行证据采集",
            round_no=0,
            timeout_seconds=timeout or self.settings.observe_timeout,
            trusted_options=True,
        )
        if screenshot.returncode == 0 and screenshot_path.exists():
            screenshot.artifact_path = str(screenshot_path)
        return snapshot, screenshot

    def close(self, task_id: str) -> None:
        try:
            self._run(
                task_id,
                CliCommand(op="close"),
                step_text="释放浏览器会话",
                round_no=0,
                timeout_seconds=15,
                respect_stop=False,
            )
        except Exception:
            pass

    def terminate(self) -> bool:
        self._stop_event.set()
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return False
        self._kill_process_group(process)
        return True

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[str]) -> None:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    timeout=5,
                )
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
