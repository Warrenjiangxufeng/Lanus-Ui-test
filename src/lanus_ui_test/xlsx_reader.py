from __future__ import annotations

import re
from pathlib import Path

from openpyxl import load_workbook

from .models import TestCase, TestCaseSet, TestStep


CASE_HEADERS = {"用例名", "用例名称", "测试用例", "case", "case name", "name"}
STEP_HEADERS = {"操作步骤", "步骤", "测试步骤", "steps", "step"}
URL_HEADERS = {"目标地址", "url", "目标url", "地址"}

STEP_PATTERN = re.compile(r"^\s*(?:步骤\s*)?(\d+)\s*[:：.、]\s*(.+?)\s*$")
URL_PATTERN = re.compile(r"https?://[^\s，。；;]+", re.IGNORECASE)


def _normalize(value: object) -> str:
    return str(value or "").strip()


def _normalized_header(value: object) -> str:
    return _normalize(value).lower().replace(" ", "")


def _find_header_row(rows: list[tuple[object, ...]]) -> tuple[int, int, int, int | None]:
    for row_index, row in enumerate(rows[:20]):
        normalized = [_normalized_header(cell) for cell in row]
        case_col = next(
            (i for i, value in enumerate(normalized) if value in {_normalized_header(v) for v in CASE_HEADERS}),
            None,
        )
        step_col = next(
            (i for i, value in enumerate(normalized) if value in {_normalized_header(v) for v in STEP_HEADERS}),
            None,
        )
        if case_col is not None and step_col is not None:
            url_col = next(
                (i for i, value in enumerate(normalized) if value in {_normalized_header(v) for v in URL_HEADERS}),
                None,
            )
            return row_index, case_col, step_col, url_col
    raise ValueError(
        "未找到用例表头。至少需要包含『用例名』和『操作步骤』两列。"
    )


def split_steps(cell_text: str) -> list[TestStep]:
    """Split one or more numbered lines while preserving their original order."""
    lines = [line.strip() for line in re.split(r"\r?\n", cell_text) if line.strip()]
    steps: list[TestStep] = []
    for line in lines:
        match = STEP_PATTERN.match(line)
        if match:
            steps.append(TestStep(step_id=int(match.group(1)), text=match.group(2).strip()))
        else:
            next_id = steps[-1].step_id + 1 if steps else 1
            steps.append(TestStep(step_id=next_id, text=line))
    return steps


def read_xlsx_cases(path: str | Path, sheet_name: str | None = None) -> TestCaseSet:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    workbook = load_workbook(source, read_only=True, data_only=True)
    try:
        sheet = workbook[sheet_name] if sheet_name else workbook.active
        rows = list(sheet.iter_rows(values_only=True))
        header_index, case_col, step_col, url_col = _find_header_row(rows)

        cases: list[TestCase] = []
        case_by_name: dict[str, TestCase] = {}
        target_url: str | None = None

        for row in rows[header_index + 1 :]:
            case_name = _normalize(row[case_col] if case_col < len(row) else "")
            step_text = _normalize(row[step_col] if step_col < len(row) else "")
            if url_col is not None and url_col < len(row):
                target_url = _normalize(row[url_col]) or target_url
            if not step_text:
                continue
            if not case_name:
                if not cases:
                    continue
                case = cases[-1]
            else:
                case = case_by_name.get(case_name)
                if case is None:
                    case = TestCase(case_id=len(cases) + 1, name=case_name, steps=[])
                    cases.append(case)
                    case_by_name[case_name] = case
            case.steps.extend(split_steps(step_text))

        if not cases:
            raise ValueError("Excel 中没有可执行的用例步骤。")
        for case in cases:
            if not case.steps:
                raise ValueError(f"用例『{case.name}』没有任何步骤。")
            if not target_url:
                for step in case.steps:
                    match = URL_PATTERN.search(step.text)
                    if match:
                        target_url = match.group(0).rstrip(".,:：;；)")
                        break
        return TestCaseSet(
            source_file=str(source),
            sheet_name=sheet.title,
            target_url=target_url,
            cases=cases,
        )
    finally:
        workbook.close()
