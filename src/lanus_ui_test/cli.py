from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .models import TaskStatus
from .report import write_html_report_from_json
from .service import TaskManager
from .xlsx_reader import read_xlsx_cases


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lanus-ui-test")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="启动 FastAPI 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    parse = subparsers.add_parser("parse", help="解析并预览 Excel 用例")
    parse.add_argument("case_file")
    parse.add_argument("--sheet")

    html_report = subparsers.add_parser(
        "html",
        help="从已有 report.json 生成 HTML 测试报告",
    )
    html_report.add_argument("json_file")
    html_report.add_argument("-o", "--output")

    run = subparsers.add_parser("run", help="直接执行一个 Excel 用例文件")
    run.add_argument("case_file")
    run.add_argument("--target-url")
    run.add_argument("--headed", action="store_true")
    run.add_argument("--max-step-attempts", type=int)
    run.add_argument("--max-action-rounds", type=int)
    run.add_argument("--timeout", type=float, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings()
    settings.ensure_directories()

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "lanus_ui_test.api:app",
            host=args.host,
            port=args.port,
            reload=False,
        )
        return 0

    if args.command == "parse":
        cases = read_xlsx_cases(args.case_file, args.sheet)
        print(json.dumps(cases.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0

    if args.command == "html":
        path = write_html_report_from_json(args.json_file, args.output)
        print(path)
        return 0

    manager = TaskManager(settings)
    try:
        state = manager.create_task(
            args.case_file,
            target_url=args.target_url,
            headed=args.headed,
            max_step_attempts=args.max_step_attempts,
        )
        if args.max_action_rounds:
            state.max_action_rounds = args.max_action_rounds
            manager.repository.save(state)
        print(f"task_id={state.task_id}")
        manager.start(state.task_id)
        final = manager.wait(state.task_id, timeout=args.timeout)
        print(
            json.dumps(
                {
                    "task_id": final.task_id,
                    "status": final.status.value,
                    "report_path": final.report_path,
                    "error": final.error,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if final.status == TaskStatus.COMPLETED else 1
    finally:
        manager.shutdown()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
