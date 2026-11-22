from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any

from .models import StepEvidence, StepStatus, TaskState, TaskStatus
from .redaction import redact_step_text, redact_text


SENSITIVE_SNAPSHOT_FIELD_RE = re.compile(
    r"(密码|口令|账号|用户名|验证码|password|passwd|username|captcha)",
    re.IGNORECASE,
)
SNAPSHOT_TEXT_RE = re.compile(
    r"^(\s*-\s*text:\s*)(.*)$",
    re.IGNORECASE,
)


def _redact_snapshot(value: str) -> str:
    lines: list[str] = []
    sensitive_context = 0
    for line in str(value or "").splitlines():
        if SENSITIVE_SNAPSHOT_FIELD_RE.search(line):
            sensitive_context = 4
        if sensitive_context > 0 and SNAPSHOT_TEXT_RE.search(line):
            line = SNAPSHOT_TEXT_RE.sub(r"\1[REDACTED]", line)
        lines.append(line)
        sensitive_context = max(0, sensitive_context - 1)
    return redact_text("\n".join(lines))


def _redact_command_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload["stdout"] = _redact_snapshot(payload.get("stdout", ""))
    payload["stderr"] = _redact_snapshot(payload.get("stderr", ""))
    return payload


def _latest_evidence(
    evidence: list[StepEvidence], case_index: int, step_index: int
) -> StepEvidence | None:
    matches = [
        item
        for item in evidence
        if item.case_index == case_index and item.step_index == step_index
    ]
    return matches[-1] if matches else None


def build_report(state: TaskState) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    all_passed = True
    for case_index, case in enumerate(state.cases):
        steps: list[dict[str, Any]] = []
        case_passed = True
        for step_index, step in enumerate(case.steps):
            evidence = _latest_evidence(state.evidence, case_index, step_index)
            status = evidence.status if evidence else StepStatus.NOT_RUN
            judge_payload = None
            if evidence and evidence.judge:
                judge_payload = evidence.judge.model_dump(mode="json")
                judge_payload["reason"] = redact_text(judge_payload.get("reason", ""))
                judge_payload["evidence_refs"] = [
                    redact_text(value)
                    for value in judge_payload.get("evidence_refs", [])
                ]
            assertion_payload = None
            if evidence and evidence.assertion:
                assertion_payload = evidence.assertion.model_dump(mode="json")
                assertion_payload["expected"] = redact_text(
                    assertion_payload.get("expected", "")
                )
                assertion_payload["description"] = redact_text(
                    assertion_payload.get("description", "")
                )
            if status != StepStatus.PASS:
                case_passed = False
                all_passed = False
            steps.append(
                {
                    "step_id": step.step_id,
                    "text": redact_step_text(step.text),
                    "status": status.value,
                    "attempt_count": sum(
                        1
                        for item in state.evidence
                        if item.case_index == case_index and item.step_index == step_index
                    ),
                    "assertion": assertion_payload,
                    "judge": judge_payload,
                    "error": redact_text(evidence.error) if evidence else "",
                    "snapshot": _redact_snapshot(evidence.snapshot) if evidence else "",
                    "screenshot_path": evidence.screenshot_path if evidence else None,
                    "commands": (
                        [_redact_command_payload(item.model_dump(mode="json")) for item in evidence.commands]
                        if evidence
                        else []
                    ),
                }
            )
        cases.append(
            {
                "case_id": case.case_id,
                "case_name": case.name,
                "status": StepStatus.PASS.value
                if case_passed
                else StepStatus.FAIL.value,
                "steps": steps,
            }
        )

    verdict = (
        "PASS"
        if all_passed and state.status == TaskStatus.COMPLETED
        else "STOPPED"
        if state.status == TaskStatus.STOPPED
        else "PAUSED"
        if state.status == TaskStatus.PAUSED
        else "FAIL"
    )
    return {
        "task_id": state.task_id,
        "source_file": state.source_file,
        "sheet_name": state.sheet_name,
        "target_url": state.target_url,
        "status": state.status.value,
        "verdict": verdict,
        "error": redact_text(state.error),
        "cases": cases,
        "execution_count": sum(
            len(case_step_commands)
            for case in cases
            for case_step_commands in (item["commands"] for item in case["steps"])
        ),
    }


def write_report(state: TaskState, report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(state)
    json_path = report_dir / "report.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path = report_dir / "report.md"
    markdown_path.write_text(_to_markdown(report), encoding="utf-8")
    html_path = report_dir / "report.html"
    html_path.write_text(
        render_html_report(report, report_dir),
        encoding="utf-8",
    )
    return json_path


def write_html_report_from_json(
    json_path: str | Path,
    output_path: str | Path | None = None,
) -> Path:
    source = Path(json_path).expanduser().resolve()
    report = json.loads(source.read_text(encoding="utf-8"))
    destination = (
        Path(output_path).expanduser().resolve()
        if output_path
        else source.with_suffix(".html")
    )
    destination.write_text(
        render_html_report(report, destination.parent),
        encoding="utf-8",
    )
    return destination


def render_html_report(report: dict[str, Any], report_dir: Path) -> str:
    cases = report.get("cases", [])
    steps = [step for case in cases for step in case.get("steps", [])]
    passed_steps = sum(step.get("status") == "PASS" for step in steps)
    passed_cases = sum(case.get("status") == "PASS" for case in cases)
    failed_cases = len(cases) - passed_cases
    total_steps = len(steps)
    failed_steps = total_steps - passed_steps
    pass_rate = round(passed_steps / total_steps * 100, 1) if total_steps else 0
    verdict = str(report.get("verdict", "UNKNOWN"))
    status = str(report.get("status", "UNKNOWN"))
    error_html = (
        f'<div class="alert alert-error"><strong>执行错误</strong>'
        f"<p>{_e(report['error'])}</p></div>"
        if report.get("error")
        else ""
    )
    case_sections = "\n".join(
        _render_case(case, report_dir) for case in cases
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lanus UI Test · {_e(report.get("task_id", "测试报告"))}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f6f8;
      --surface: #ffffff;
      --ink: #17212b;
      --muted: #66727f;
      --line: #dce2e8;
      --brand: #2457d6;
      --pass: #16834f;
      --fail: #c33b36;
      --warn: #b87316;
      --shadow: 0 12px 30px rgba(31, 48, 67, .08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at 5% 0%, rgba(36, 87, 214, .13), transparent 30rem),
        radial-gradient(circle at 95% 14%, rgba(22, 131, 79, .08), transparent 28rem),
        var(--bg);
      color: var(--ink);
      font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI",
            "PingFang SC", "Microsoft YaHei", sans-serif;
    }}
    .page {{ width: min(1180px, calc(100% - 40px)); margin: 34px auto 60px; }}
    .hero, .case, .metric {{
      background: var(--surface);
      border: 1px solid rgba(220, 226, 232, .9);
      box-shadow: var(--shadow);
    }}
    .hero {{
      position: relative;
      border-radius: 24px;
      padding: 32px;
      overflow: hidden;
    }}
    .hero::after {{
      content: "";
      position: absolute;
      inset: 0 0 auto;
      height: 4px;
      background: linear-gradient(90deg, var(--brand), #55b5ff, var(--pass));
    }}
    .hero-top {{ display: flex; justify-content: space-between; gap: 24px; align-items: start; }}
    .eyebrow {{ color: var(--brand); font-size: 12px; font-weight: 750; letter-spacing: .12em; text-transform: uppercase; }}
    h1 {{ margin: 7px 0 8px; font-size: clamp(26px, 4vw, 42px); line-height: 1.16; letter-spacing: -.03em; }}
    h2 {{ margin: 0; font-size: 21px; letter-spacing: -.015em; }}
    h3 {{ margin: 0; font-size: 16px; }}
    p {{ margin: 0; }}
    .muted {{ color: var(--muted); }}
    .task-id {{ margin-top: 10px; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted); }}
    .verdict {{
      min-width: 118px; padding: 16px 20px; border-radius: 18px; text-align: center;
      background: #eef3ff; color: var(--brand); font-weight: 800; font-size: 19px;
    }}
    .verdict.PASS, .badge.PASS {{ color: var(--pass); background: #eaf7f0; }}
    .verdict.FAIL, .badge.FAIL {{ color: var(--fail); background: #fceded; }}
    .verdict.STOPPED, .verdict.PAUSED, .badge.STOPPED, .badge.PAUSED,
    .badge.SKIPPED, .badge.NOT_RUN {{ color: var(--warn); background: #fff5e5; }}
    .meta {{
      margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--line);
      display: grid; grid-template-columns: 1.2fr 1fr 1.5fr; gap: 20px;
    }}
    .meta-label {{ display: block; margin-bottom: 3px; color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }}
    .meta-value {{ overflow-wrap: anywhere; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 18px 0 28px; }}
    .metric {{ border-radius: 18px; padding: 18px 20px; position: relative; overflow: hidden; }}
    .metric::before {{
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 4px;
      background: #b7c1cb;
    }}
    .metric.metric-pass::before {{ background: var(--pass); }}
    .metric.metric-fail::before {{ background: var(--fail); }}
    .metric.metric-rate::before {{ background: var(--brand); }}
    .metric strong {{ display: block; font-size: 30px; line-height: 1.2; letter-spacing: -.03em; }}
    .metric .pair {{ display: flex; align-items: baseline; gap: 9px; }}
    .count-pass {{ color: var(--pass); }}
    .count-fail {{ color: var(--fail); }}
    .count-sep {{ color: #9aa6b2; font-size: 22px; }}
    .metric em {{ font-style: normal; color: var(--muted); font-size: 12px; font-weight: 650; }}
    .metric span {{ color: var(--muted); font-size: 12px; }}
    .section-title {{ display: flex; justify-content: space-between; align-items: end; margin: 0 0 13px; }}
    .section-title p {{ color: var(--muted); }}
    .section-tools {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
    .view-switch {{
      display: inline-flex;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #eef2f6;
    }}
    .view-switch button {{
      border: 0;
      border-radius: 9px;
      padding: 7px 16px;
      color: #5a6774;
      background: transparent;
      font: inherit;
      font-size: 12px;
      font-weight: 750;
      cursor: pointer;
    }}
    .view-switch button.active {{
      color: var(--ink);
      background: white;
      box-shadow: 0 3px 10px rgba(31, 48, 67, .12);
    }}
    .case {{ border-radius: 18px; margin-bottom: 16px; overflow: hidden; box-shadow: 0 8px 24px rgba(31, 48, 67, .06); }}
    .case-head {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 20px 22px; border-bottom: 1px solid var(--line); }}
    .case-body {{ padding: 6px 22px 20px; }}
    .case-note {{ margin-top: 4px; color: var(--muted); font-size: 12px; }}
    .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 10px; font-size: 11px; font-weight: 800; letter-spacing: .04em; background: #eef2f6; color: #536170; white-space: nowrap; }}
    .step {{
      padding: 18px 0;
      border-bottom: 1px solid var(--line);
      border-left: 3px solid transparent;
      padding-left: 14px;
    }}
    .step[data-status="PASS"] {{ border-left-color: var(--pass); }}
    .step[data-status="FAIL"] {{ border-left-color: var(--fail); }}
    .step[data-status="SKIPPED"], .step[data-status="NOT_RUN"] {{ border-left-color: var(--warn); }}
    .step:last-child {{ border-bottom: 0; padding-bottom: 4px; }}
    .step-grid {{ display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; gap: 14px; align-items: start; }}
    .step-no {{ display: grid; place-items: center; width: 34px; height: 34px; border-radius: 10px; background: #f0f3f7; font-weight: 800; color: #4a5867; }}
    .step-text {{ white-space: pre-wrap; overflow-wrap: anywhere; font-weight: 600; }}
    .evidence {{ margin: 12px 0 0 0; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
    .evidence-block {{ border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; background: #fbfcfd; }}
    .evidence-block strong {{ display: block; margin-bottom: 4px; font-size: 12px; color: #465361; }}
    .evidence-block p {{ overflow-wrap: anywhere; }}
    details {{ margin-top: 10px; border: 1px solid var(--line); border-radius: 12px; background: #fbfcfd; }}
    summary {{ cursor: pointer; padding: 11px 14px; color: #344253; font-weight: 700; }}
    pre {{ margin: 0; padding: 14px; overflow: auto; border-top: 1px solid var(--line); background: #f7f9fb; color: #263442; font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .shot {{ display: block; width: 100%; max-height: 420px; object-fit: contain; object-position: top; background: #eef1f4; border: 1px solid var(--line); border-radius: 12px; }}
    .shot-link {{ color: var(--brand); font-size: 12px; font-weight: 700; text-decoration: none; }}
    .brief-message {{
      margin: 10px 0 0 48px;
      padding: 9px 12px;
      border-radius: 10px;
      color: #9e302b;
      background: #fff0ef;
      font-size: 12px;
    }}
    body[data-view="brief"] .step-details {{ display: none; }}
    body[data-view="expanded"] .step-details {{ display: block; }}
    .alert {{ border-radius: 14px; padding: 15px 17px; margin-bottom: 16px; }}
    .alert-error {{ color: #8c2925; background: #fceded; border: 1px solid #f3cfcd; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 17px; }}
    .button {{ border: 0; border-radius: 10px; padding: 9px 13px; background: var(--brand); color: white; font: inherit; font-weight: 700; text-decoration: none; cursor: pointer; }}
    .button.secondary {{ color: #344253; background: #eef2f6; }}
    .footer {{ margin-top: 24px; text-align: center; color: var(--muted); font-size: 12px; }}
    @media (max-width: 800px) {{
      .page {{ width: min(100% - 24px, 1180px); margin-top: 16px; }}
      .hero {{ padding: 22px; }}
      .hero-top {{ display: block; }}
      .verdict {{ display: inline-block; margin-top: 16px; }}
      .meta, .evidence {{ grid-template-columns: 1fr 1fr; }}
      .metrics {{ grid-template-columns: 1fr 1fr; }}
      .step-grid {{ grid-template-columns: 36px minmax(0, 1fr); }}
      .step-grid > .badge {{ grid-column: 2; justify-self: start; }}
      .brief-message {{ margin-left: 48px; }}
    }}
    @media (max-width: 520px) {{
      .meta, .metrics, .evidence {{ grid-template-columns: 1fr; }}
      .case-head {{ align-items: start; }}
    }}
    @media print {{
      body {{ background: white; }}
      .page {{ width: 100%; margin: 0; }}
      .hero, .case, .metric {{ box-shadow: none; break-inside: avoid; }}
      .actions {{ display: none; }}
    }}
  </style>
</head>
<body data-view="brief">
  <main class="page">
    <section class="hero">
      <div class="hero-top">
        <div>
          <div class="eyebrow">Lanus UI Test Report</div>
          <h1>UI 自动化测试报告</h1>
          <p class="muted">由 LangGraph 编排、playwright-cli 执行、证据驱动验收</p>
          <div class="task-id">TASK · {_e(report.get("task_id", ""))}</div>
        </div>
        <div class="verdict {_status_class(verdict)}">{_e(verdict)}</div>
      </div>
      <div class="meta">
        <div><span class="meta-label">用例文件</span><span class="meta-value">{_e(report.get("source_file", ""))}</span></div>
        <div><span class="meta-label">工作表</span><span class="meta-value">{_e(report.get("sheet_name", ""))}</span></div>
        <div><span class="meta-label">目标地址</span><span class="meta-value">{_e(report.get("target_url") or "-")}</span></div>
      </div>
      <div class="actions">
        <button class="button" onclick="window.print()">打印 / 保存 PDF</button>
        <a class="button secondary" href="report.json">查看原始 JSON</a>
      </div>
    </section>

    <section class="metrics" aria-label="测试汇总">
      <div class="metric metric-pass">
        <div class="pair"><strong class="count-pass">{passed_cases}</strong><span class="count-sep">/</span><strong class="count-fail">{failed_cases}</strong></div>
        <span>通过用例 / 失败用例</span>
      </div>
      <div class="metric metric-pass">
        <div class="pair"><strong class="count-pass">{passed_steps}</strong><span class="count-sep">/</span><strong class="count-fail">{failed_steps}</strong></div>
        <span>通过步骤 / 失败步骤</span>
      </div>
      <div class="metric metric-rate"><strong>{pass_rate}%</strong><span>步骤通过率</span></div>
      <div class="metric"><strong>{_e(report.get("execution_count", 0))}</strong><span>浏览器命令</span></div>
    </section>

    {error_html}

    <div class="section-title">
      <div><h2>执行明细</h2><p>状态、断言与原始证据</p></div>
      <div class="section-tools">
        <div class="view-switch" role="group" aria-label="执行明细显示模式">
          <button type="button" class="active" data-view-button="brief">简要</button>
          <button type="button" data-view-button="expanded">展开</button>
        </div>
        <span class="badge {_status_class(status)}">{_e(status)}</span>
      </div>
    </div>
    {case_sections}
    <div class="footer">Generated by Lanus UI Test · Data source: report.json</div>
  </main>
  <script>
    (() => {{
      const body = document.body;
      const buttons = document.querySelectorAll("[data-view-button]");
      const setView = (view) => {{
        body.dataset.view = view;
        buttons.forEach((button) => {{
          button.classList.toggle("active", button.dataset.viewButton === view);
        }});
        if (view === "expanded") {{
          document.querySelectorAll("img[data-src]").forEach((image) => {{
            if (!image.src) image.src = image.dataset.src;
          }});
        }}
      }};
      buttons.forEach((button) => {{
        button.addEventListener("click", () => setView(button.dataset.viewButton));
      }});
    }})();
  </script>
</body>
</html>
"""


def _render_case(case: dict[str, Any], report_dir: Path) -> str:
    steps = case.get("steps", [])
    step_count = len(steps)
    step_html = "\n".join(_render_step(step, report_dir) for step in steps)
    return f"""
    <section class="case">
      <div class="case-head">
        <div>
          <h2>{_e(case.get("case_name", "未命名用例"))}</h2>
          <div class="case-note">{step_count} 个步骤</div>
        </div>
        <span class="badge {_status_class(case.get("status", ""))}">{_e(case.get("status", ""))}</span>
      </div>
      <div class="case-body">{step_html}</div>
    </section>
    """


def _render_step(step: dict[str, Any], report_dir: Path) -> str:
    status = str(step.get("status", "NOT_RUN"))
    evidence_blocks: list[str] = []
    judge = step.get("judge") or {}
    if judge.get("reason"):
        evidence_blocks.append(
            f'<div class="evidence-block"><strong>验收结论</strong><p>{_e(judge["reason"])}</p></div>'
        )
    if step.get("error"):
        evidence_blocks.append(
            f'<div class="evidence-block"><strong>失败原因</strong><p>{_e(step["error"])}</p></div>'
        )
    assertion = step.get("assertion")
    if assertion:
        assertion_text = (
            f'{assertion.get("kind", "")} · '
            f'{assertion.get("expected", "") or assertion.get("description", "")}'
        )
        evidence_blocks.append(
            f'<div class="evidence-block"><strong>断言</strong><p>{_e(assertion_text)}</p></div>'
        )
    if step.get("attempt_count"):
        evidence_blocks.append(
            f'<div class="evidence-block"><strong>尝试次数</strong><p>{_e(step["attempt_count"])}</p></div>'
        )

    screenshot = _screenshot_html(step.get("screenshot_path"), report_dir)
    if screenshot:
        evidence_blocks.append(
            f'<div class="evidence-block"><strong>页面截图</strong>{screenshot}</div>'
        )

    command_payload = json.dumps(
        step.get("commands", []),
        ensure_ascii=False,
        indent=2,
    )
    commands_html = (
        f"<details><summary>查看 {len(step.get('commands', []))} 条执行命令</summary>"
        f"<pre>{_e(command_payload)}</pre></details>"
        if step.get("commands")
        else ""
    )
    snapshot_html = (
        f"<details><summary>查看页面快照</summary><pre>{_e(step.get('snapshot', ''))}</pre></details>"
        if step.get("snapshot")
        else ""
    )
    evidence_html = (
        f'<div class="evidence">{"".join(evidence_blocks)}</div>'
        if evidence_blocks
        else ""
    )
    brief_message = (
        f'<div class="brief-message">{_e(step.get("error"))}</div>'
        if status != "PASS" and step.get("error")
        else ""
    )
    details_html = (
        f'<div class="step-details">{evidence_html}{commands_html}{snapshot_html}</div>'
        if evidence_html or commands_html or snapshot_html
        else ""
    )
    return f"""
      <article class="step" data-status="{_e(status)}">
        <div class="step-grid">
          <div class="step-no">{_e(step.get("step_id", ""))}</div>
          <div class="step-text">{_e(step.get("text", ""))}</div>
          <span class="badge {_status_class(status)}">{_e(status)}</span>
        </div>
        {brief_message}
        {details_html}
      </article>
    """


def _screenshot_html(path: Any, report_dir: Path) -> str:
    if not path:
        return ""
    source = Path(str(path))
    if not source.exists():
        return ""
    relative = Path(os.path.relpath(source, report_dir)).as_posix()
    quoted = html.escape(relative, quote=True)
    return (
        f'<a href="{quoted}" target="_blank" rel="noopener">'
        f'<img class="shot" data-src="{quoted}" loading="lazy" alt="步骤截图"></a>'
        f'<a class="shot-link" href="{quoted}" target="_blank" rel="noopener">打开原图</a>'
    )


def _status_class(value: Any) -> str:
    return "".join(
        char if char.isalnum() or char in {"_", "-"} else "_"
        for char in str(value).upper()
    )


def _e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Lanus UI Test 报告",
        "",
        f"- 任务：`{report['task_id']}`",
        f"- 用例文件：`{report['source_file']}`",
        f"- 状态：**{report['status']}**",
        f"- 结论：**{report['verdict']}**",
        "",
    ]
    if report.get("error"):
        lines.extend([f"> 错误：{report['error']}", ""])
    for case in report["cases"]:
        lines.extend(
            [
                f"## {case['case_name']}",
                "",
                "| 步骤 | 自然语言步骤 | 结果 | 证据 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for step in case["steps"]:
            evidence = step.get("screenshot_path") or (
                step.get("judge", {}).get("reason") if step.get("judge") else ""
            )
            clean_evidence = str(evidence or step.get("error") or "").replace("|", "\\|")
            clean_text = step["text"].replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {step['step_id']} | {clean_text} | {step['status']} | {clean_evidence} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
