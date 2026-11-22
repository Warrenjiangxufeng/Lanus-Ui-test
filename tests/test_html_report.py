from __future__ import annotations

import json
from pathlib import Path

from lanus_ui_test.report import write_html_report_from_json


def test_build_html_report_from_existing_json(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    screenshot = tmp_path / "screenshots" / "step.png"
    screenshot.parent.mkdir()
    screenshot.write_bytes(b"png")
    report = {
        "task_id": "ui_html",
        "source_file": "<cases>.xlsx",
        "sheet_name": "AI执行用例",
        "target_url": "http://127.0.0.1:5000",
        "status": "COMPLETED",
        "verdict": "PASS",
        "error": "",
        "execution_count": 1,
        "cases": [
            {
                "case_id": 1,
                "case_name": "<script>alert(1)</script>",
                "status": "PASS",
                "steps": [
                    {
                        "step_id": 1,
                        "text": "打开首页",
                        "status": "PASS",
                        "attempt_count": 1,
                        "assertion": {
                            "kind": "text_contains",
                            "expected": "首页",
                            "description": "",
                        },
                        "judge": {
                            "passed": True,
                            "reason": "页面包含首页",
                            "evidence_refs": [],
                        },
                        "error": "",
                        "snapshot": "首页",
                        "screenshot_path": str(screenshot),
                        "commands": [],
                    }
                ],
            }
        ],
    }
    json_path = report_dir / "report.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False),
        encoding="utf-8",
    )

    html_path = write_html_report_from_json(json_path)
    content = html_path.read_text(encoding="utf-8")

    assert html_path == report_dir / "report.html"
    assert "UI 自动化测试报告" in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content
    assert "../screenshots/step.png" in content
    assert "report.json" in content
    assert '<body data-view="brief">' in content
    assert 'data-view-button="brief"' in content
    assert 'data-view-button="expanded"' in content
    assert "metric-pass" in content
    assert "count-pass" in content
    assert "count-fail" in content
    assert 'data-src="../screenshots/step.png"' in content
    assert '<img class="shot" src=' not in content
