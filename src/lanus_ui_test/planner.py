from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from .browser import PLANNER_OPERATIONS
from .config import Settings
from .models import (
    AssertionSpec,
    CliCommand,
    JudgeDecision,
    PlanDecision,
    StepEvidence,
    TestCase,
)
from .redaction import redact_text


class PlanRequest(BaseModel):
    task_id: str
    target_url: str | None = None
    case: TestCase
    step_index: int
    attempt: int
    action_round: int
    snapshot: str
    screenshot_paths: list[str] = Field(default_factory=list)
    recent_executions: list[dict[str, Any]] = Field(default_factory=list)
    previous_steps: list[StepEvidence] = Field(default_factory=list)
    feedback: str = ""


class JudgeRequest(BaseModel):
    task_id: str
    case: TestCase
    step_index: int
    snapshot: str
    screenshot_paths: list[str] = Field(default_factory=list)
    assertion: AssertionSpec | None = None
    execution_history: list[dict[str, Any]] = Field(default_factory=list)


class Planner(Protocol):
    def plan_step(self, request: PlanRequest) -> PlanDecision: ...

    def judge_step(self, request: JudgeRequest) -> JudgeDecision: ...


class PlannerError(RuntimeError):
    pass


def _json_from_text(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        value = json.loads(clean)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        value = None
        for match in re.finditer(r"\{", clean):
            try:
                candidate, _ = decoder.raw_decode(clean[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            raise PlannerError("模型未返回可解析的 JSON")
    if not isinstance(value, dict):
        raise PlannerError("模型返回的 JSON 顶层必须是对象")
    return value


def _image_data_url(path: str) -> str:
    image_path = Path(path)
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _validation_error_message(exc: ValidationError) -> str:
    details = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        details.append(f"{location}: {error.get('msg', 'invalid value')}")
    return "; ".join(details)


def _normalize_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept common model shorthand without weakening the PlanDecision schema."""
    if "op" in payload and "commands" not in payload:
        return {
            "commands": [payload],
            "step_complete": False,
            "assertion": None,
            "note": payload.get("note", "模型返回了单条命令"),
        }
    commands = payload.get("commands")
    if isinstance(commands, dict):
        return {**payload, "commands": [commands]}
    return payload


def _extract_responses_text(body: dict[str, Any]) -> str:
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    chunks: list[str] = []
    for item in body.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    if chunks:
        return "\n".join(chunks)
    try:
        raw = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise PlannerError("模型接口返回格式异常") from exc
    return str(raw)


def _step_prompt(request: PlanRequest) -> str:
    step = request.case.steps[request.step_index]
    steps = "\n".join(f"{item.step_id}: {item.text}" for item in request.case.steps)
    operations = ", ".join(sorted(PLANNER_OPERATIONS))
    target = request.target_url or "从自然语言步骤中识别"
    recent = json.dumps(
        request.recent_executions[-12:], ensure_ascii=False, indent=2
    )
    previous = [
        {
            "step_id": item.step_id,
            "status": item.status.value,
            "error": item.error,
            "judge": item.judge.model_dump(mode="json") if item.judge else None,
        }
        for item in request.previous_steps[-12:]
    ]
    return f"""你是 UI 自动化测试执行器中的“单步规划器”。你只负责当前一个编号步骤，不负责宣布整条用例完成。

【任务目标地址】
{target}

【当前用例】
{request.case.name}

【完整用例步骤】
{steps}

【当前需要执行的步骤】
第 {step.step_id} 步：{step.text}

【最近一次页面快照】
{request.snapshot[-24000:] or "尚无页面快照；首个动作通常应先 open/goto，然后 snapshot。"}

【最近命令执行结果】
{recent or "无"}

【此前步骤证据摘要】
{json.dumps(previous, ensure_ascii=False)}

【本轮反馈】
{request.feedback or "无"}

可用操作：{operations}

规则：
1. 只输出 JSON，不要 markdown，不要解释。
2. commands 中每个命令必须是 {{"op":"操作名","args":["参数"]}}，不能输出 shell 字符串。
3. 严禁在参数里指定 -s/session、--filename、--config、--profile 或 --persistent；这些由引擎控制。
4. 必须使用页面快照中真实存在的元素 ref；元素未知时先 snapshot/find。
5. 需要识别验证码、图片文字或复杂视觉信息时，先执行 snapshot/screenshot 获取证据，再在下一轮判断。
6. 自然语言步骤含有“若/如果/失败后重试”等条件时，严格按当前观察到的事实决定分支。
7. 当前步骤仍需动作时 step_complete=false；当前步骤动作已完成且可以验收时 step_complete=true。
8. step_complete=true 时必须给出 assertion；能确定文本/URL就使用 text_contains、text_not_contains、url_contains，否则使用 semantic。
9. 不执行、不授权文件上传、任意 JavaScript、网络 mock 或浏览器配置修改。
10. 如果当前步骤只是执行动作而未要求结果校验，assertion.kind 使用 command_success。
11. 除 command_success 外，assertion.expected 必须填写非空、可在页面证据中核对的内容；不确定时使用 semantic。
12. 顶层必须是 PlanDecision 对象；命令对象必须放在 commands 数组里，禁止把单个命令对象作为顶层结果。

输出 schema：
{{
  "commands": [{{"op": "snapshot", "args": []}}],
  "step_complete": false,
  "assertion": null,
  "note": "简短决策依据"
}}"""


def _judge_prompt(request: JudgeRequest) -> str:
    step = request.case.steps[request.step_index]
    history = json.dumps(request.execution_history[-20:], ensure_ascii=False, indent=2)
    assertion = (
        request.assertion.model_dump(mode="json") if request.assertion else "无结构化断言"
    )
    return f"""你是 UI 自动化测试的“步骤验收器”。请仅根据实际快照、截图和执行证据判断第 {step.step_id} 步是否通过。

用例：{request.case.name}
当前步骤：{step.text}
验收条件：{json.dumps(assertion, ensure_ascii=False)}

执行证据：
{history}

页面快照：
{request.snapshot[-30000:] or "无"}

规则：
1. 不得因为“命令返回成功”就认定自然语言目标已达成；必须核对页面可见结果。
2. 找不到明确证据时 passed=false。
3. 只输出 JSON：{{"passed":true,"reason":"可复核的证据说明","evidence_refs":["快照中的关键文本或截图文件名"]}}
4. 不输出 markdown 和额外字段。"""


class ResponsesApiPlanner:
    """Plan and verify inside Lanus through a direct Responses API request."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.llm_base_url or not settings.llm_model:
            raise PlannerError("缺少 LANUS_LLM_BASE_URL 或 LANUS_LLM_MODEL")
        base = settings.llm_base_url.rstrip("/")
        self.endpoint = base if base.endswith("/responses") else f"{base}/responses"

    def _call(
        self,
        prompt: str,
        response_model: type[BaseModel],
        image_paths: list[str],
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for path in image_paths[:3]:
            if Path(path).exists():
                content.append(
                    {
                        "type": "input_image",
                        "image_url": _image_data_url(path),
                    }
                )
        payload = {
            "model": self.settings.llm_model,
            "input": [{"role": "user", "content": content}],
            "temperature": 0,
            "store": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        with httpx.Client(timeout=self.settings.llm_timeout) as client:
            response = client.post(self.endpoint, headers=headers, json=payload)
        if response.status_code >= 400:
            raise PlannerError(f"模型接口返回 HTTP {response.status_code}")
        return _json_from_text(_extract_responses_text(response.json()))

    def plan_step(self, request: PlanRequest) -> PlanDecision:
        payload = self._call(
            _step_prompt(request),
            PlanDecision,
            request.screenshot_paths,
        )
        try:
            return PlanDecision.model_validate(_normalize_plan_payload(payload))
        except ValidationError as exc:
            raise PlannerError(
                f"规划结果不符合约定：{_validation_error_message(exc)}"
            ) from exc

    def judge_step(self, request: JudgeRequest) -> JudgeDecision:
        payload = self._call(
            _judge_prompt(request),
            JudgeDecision,
            request.screenshot_paths,
        )
        try:
            return JudgeDecision.model_validate(payload)
        except ValidationError as exc:
            raise PlannerError(
                f"验收结果不符合约定：{_validation_error_message(exc)}"
            ) from exc


class OpenAICompatiblePlanner:
    """Use any OpenAI-compatible chat-completions endpoint."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.llm_base_url or not settings.llm_model:
            raise PlannerError("OpenAI-compatible 后端缺少 LANUS_LLM_BASE_URL 或 LANUS_LLM_MODEL")
        base = settings.llm_base_url.rstrip("/")
        self.endpoint = (
            base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        )

    def _call(
        self,
        prompt: str,
        response_model: type[BaseModel],
        image_paths: list[str],
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for path in image_paths[:3]:
            if Path(path).exists():
                content.append(
                    {"type": "image_url", "image_url": {"url": _image_data_url(path)}}
                )
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        with httpx.Client(timeout=self.settings.llm_timeout) as client:
            response = client.post(self.endpoint, headers=headers, json=payload)
            if response.status_code == 400:
                payload.pop("response_format", None)
                response = client.post(self.endpoint, headers=headers, json=payload)
            response.raise_for_status()
        body = response.json()
        try:
            raw = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PlannerError("OpenAI-compatible 返回格式异常") from exc
        if isinstance(raw, list):
            raw = "".join(
                item.get("text", "") for item in raw if isinstance(item, dict)
            )
        return _json_from_text(str(raw))

    def plan_step(self, request: PlanRequest) -> PlanDecision:
        payload = self._call(
            _step_prompt(request),
            PlanDecision,
            request.screenshot_paths,
        )
        try:
            return PlanDecision.model_validate(_normalize_plan_payload(payload))
        except ValidationError as exc:
            raise PlannerError(
                f"规划结果不符合约定：{_validation_error_message(exc)}"
            ) from exc

    def judge_step(self, request: JudgeRequest) -> JudgeDecision:
        payload = self._call(
            _judge_prompt(request),
            JudgeDecision,
            request.screenshot_paths,
        )
        try:
            return JudgeDecision.model_validate(payload)
        except ValidationError as exc:
            raise PlannerError(
                f"验收结果不符合约定：{_validation_error_message(exc)}"
            ) from exc


def build_planner(settings: Settings) -> Planner:
    backend = settings.planner_backend.strip().lower()
    if backend in {"responses", "responses_api", "direct"}:
        return ResponsesApiPlanner(settings)
    if backend in {"openai_compatible", "openai", "http"}:
        return OpenAICompatiblePlanner(settings)
    raise PlannerError(f"未知规划后端：{settings.planner_backend}")
