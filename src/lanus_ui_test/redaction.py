from __future__ import annotations

import re
from collections.abc import Iterable

from .models import CliCommand


SENSITIVE_STEP_RE = re.compile(
    r"(密码|口令|验证码|password|passwd|token|secret|credential|captcha)",
    re.IGNORECASE,
)
SENSITIVE_QUERY_RE = re.compile(
    r"(?i)(token|access_token|password|passwd|secret|api_key)=([^&\s]+)"
)
CREDENTIAL_FIELD_RE = re.compile(
    r"(?i)((?:密码|口令|账号|用户名|验证码|password|passwd|username|captcha)\s*[:：]\s*)"
    r"([^\s，。；;]+)"
)
CREDENTIAL_ACTION_RE = re.compile(
    r"(?i)((?:密码|口令|账号|用户名|验证码|password|passwd|username|captcha)"
    r"(?:输入框)?(?:中|里)?(?:已经|已|成功)?"
    r"(?:填入|输入|填写|为|是)\s*[:：]?\s*)"
    r"([^\s，。；;]+)"
)
CAPTCHA_RESULT_RE = re.compile(
    r"((?:验证码|图形码|captcha)(?:图像|图片)?(?:识别结果)?"
    r"(?:可见为|显示为|识别为|可见|显示|为|是)\s*[:：]?\s*)"
    r"([^\s，。；;]+)",
    re.IGNORECASE,
)


def redact_text(value: str, secrets: Iterable[str] = ()) -> str:
    redacted = value
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = SENSITIVE_QUERY_RE.sub(r"\1=[REDACTED]", redacted)
    redacted = CREDENTIAL_FIELD_RE.sub(r"\1[REDACTED]", redacted)
    redacted = CREDENTIAL_ACTION_RE.sub(r"\1[REDACTED]", redacted)
    return CAPTCHA_RESULT_RE.sub(r"\1[REDACTED]", redacted)


def redact_step_text(value: str) -> str:
    return redact_text(value)


def command_display(command: CliCommand, step_text: str) -> str:
    rendered_args = list(command.args)
    if SENSITIVE_STEP_RE.search(step_text) and command.op in {"fill", "type"}:
        if rendered_args:
            rendered_args[-1] = "[REDACTED]"
    return " ".join([command.op, *rendered_args]).strip()


def redact_command(command: CliCommand, step_text: str) -> CliCommand:
    args = list(command.args)
    if SENSITIVE_STEP_RE.search(step_text) and command.op in {"fill", "type"} and args:
        args[-1] = "[REDACTED]"
    return command.model_copy(update={"args": args})


def extract_secrets(command: CliCommand, step_text: str) -> list[str]:
    if command.op not in {"fill", "type"} or not command.args:
        return []
    if not SENSITIVE_STEP_RE.search(step_text):
        return []
    return [command.args[-1]]
