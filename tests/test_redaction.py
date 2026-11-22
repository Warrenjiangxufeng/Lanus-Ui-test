from lanus_ui_test.models import CliCommand
from lanus_ui_test.redaction import redact_command, redact_step_text, redact_text
from lanus_ui_test.report import _redact_snapshot


def test_redacts_credentials_from_steps_and_output() -> None:
    step = "输入账号：sample-user\n输入密码：sample-secret\n验证码：a1b2"
    redacted = redact_step_text(step)
    assert "sample-user" not in redacted
    assert "sample-secret" not in redacted
    assert "a1b2" not in redacted
    assert redact_text("password=abc&token=xyz", ["special-value"]).count("[REDACTED]") == 2
    command = redact_command(
        CliCommand(op="fill", args=["e5", "sample-secret"]),
        "输入密码",
    )
    assert command.args[-1] == "[REDACTED]"
    assert "sample-secret" not in redact_text("密码输入框成功填入 sample-secret")
    assert "a1b2" not in redact_text("验证码图像识别结果为 a1b2")
    assert "a1b2" not in redact_text("验证码图像可见为 a1b2")
    snapshot = "\n".join(
        [
            'textbox "密码" [ref=e1]:',
            "  - /placeholder: 请输入密码",
            '  - text: "sample-secret"',
        ]
    )
    assert "sample-secret" not in _redact_snapshot(snapshot)
