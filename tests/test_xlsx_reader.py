from pathlib import Path

from openpyxl import Workbook

from lanus_ui_test.xlsx_reader import read_xlsx_cases, split_steps


def test_split_steps_keeps_conditional_text() -> None:
    steps = split_steps(
        "1:进入登录页\n"
        "2:输入账号：sample-user\n"
        "6:点击登录。若失败，则重复 3-5 后重试"
    )
    assert [step.step_id for step in steps] == [1, 2, 6]
    assert "若失败" in steps[-1].text


def test_read_template_shape(tmp_path: Path) -> None:
    path = tmp_path / "cases.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "AI执行用例"
    sheet.append(["用例名", "操作步骤"])
    sheet.append(["登录功能验证", "1:进入登录页\n2:输入账号：sample-user"])
    sheet.append(["交易计划功能验证", "3:点击交易计划\n4:新增便签"])
    workbook.save(path)
    workbook.close()

    parsed = read_xlsx_cases(path)
    assert parsed.sheet_name == "AI执行用例"
    assert [case.name for case in parsed.cases] == ["登录功能验证", "交易计划功能验证"]
    assert [step.step_id for step in parsed.cases[0].steps] == [1, 2]
    assert [step.step_id for step in parsed.cases[1].steps] == [3, 4]


def test_parse_user_template_when_available() -> None:
    template = (
        Path.home()
        / "Desktop"
        / "KNOWLEDGE"
        / "交易模型AI自动化测试用例版本1.xlsx"
    )
    if not template.exists():
        return
    parsed = read_xlsx_cases(template)
    assert parsed.sheet_name == "AI执行用例"
    assert len(parsed.cases) == 2
    assert parsed.cases[0].steps[0].step_id == 1
    assert parsed.cases[1].steps[-1].step_id == 9
    assert parsed.target_url == "http://127.0.0.1:5000/login?next=/"
