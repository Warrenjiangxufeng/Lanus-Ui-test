from __future__ import annotations

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .config import Settings
from .models import TaskCreateRequest, TaskStatus
from .service import TaskManager


def create_app(
    settings: Settings | None = None,
    manager: TaskManager | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    task_manager = manager or TaskManager(resolved_settings)
    app = FastAPI(
        title="Lanus UI Test",
        version="0.1.0",
        description="LangGraph 驱动的自然语言 UI 自动化测试引擎",
    )
    app.state.settings = resolved_settings
    app.state.manager = task_manager

    @app.get("/healthz")
    def health() -> dict:
        return {
            "ok": True,
            "planner_backend": resolved_settings.planner_backend,
            "artifacts_dir": str(resolved_settings.artifacts_dir),
        }

    @app.post("/api/tasks", status_code=202)
    def create_task(request: TaskCreateRequest) -> dict:
        try:
            state = task_manager.create_task(
                request.case_file,
                target_url=request.target_url,
                headed=request.headed,
                max_step_attempts=request.max_step_attempts,
            )
            task_manager.start(state.task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"用例文件不存在：{exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _task_payload(task_manager, state.task_id)

    @app.post("/api/tasks/upload", status_code=202)
    async def upload_task(
        file: UploadFile = File(...),
        target_url: str | None = Form(default=None),
        headed: bool = Form(default=False),
        max_step_attempts: int | None = Form(default=None),
    ) -> dict:
        content = await file.read()
        if len(content) > 20 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="用例文件不能超过 20MB")
        try:
            state = task_manager.create_from_upload(
                file.filename or "testcases.xlsx",
                content,
                target_url=target_url,
                headed=headed,
                max_step_attempts=max_step_attempts,
            )
            task_manager.start(state.task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _task_payload(task_manager, state.task_id)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict:
        return _task_payload(task_manager, task_id)

    @app.post("/api/tasks/{task_id}/pause")
    def pause_task(task_id: str) -> dict:
        try:
            task_manager.pause(task_id)
        except (KeyError, ValueError) as exc:
            raise _http_error(exc) from exc
        return _task_payload(task_manager, task_id)

    @app.post("/api/tasks/{task_id}/resume", status_code=202)
    def resume_task(task_id: str) -> dict:
        try:
            task_manager.resume(task_id)
        except (KeyError, ValueError) as exc:
            raise _http_error(exc) from exc
        return _task_payload(task_manager, task_id)

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str) -> dict:
        try:
            task_manager.stop(task_id)
        except (KeyError, ValueError) as exc:
            raise _http_error(exc) from exc
        return _task_payload(task_manager, task_id)

    @app.get("/api/tasks/{task_id}/report")
    def get_report(task_id: str) -> JSONResponse:
        try:
            return JSONResponse(task_manager.get_report(task_id))
        except KeyError as exc:
            raise _http_error(exc) from exc

    return app


def _task_payload(manager: TaskManager, task_id: str) -> dict:
    try:
        state = manager.get(task_id)
    except KeyError as exc:
        raise _http_error(exc) from exc
    return {
        "task_id": state.task_id,
        "status": state.status.value,
        "case_index": state.case_index,
        "step_index": state.step_index,
        "case_count": len(state.cases),
        "report_path": state.report_path,
        "error": state.error,
    }


def _http_error(exc: Exception) -> HTTPException:
    status = 404 if isinstance(exc, KeyError) else 409
    return HTTPException(status_code=status, detail=str(exc))


app = create_app()
