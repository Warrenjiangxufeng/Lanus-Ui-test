import pytest

from lanus_ui_test.planner import (
    PlannerError,
    _json_from_text,
    _normalize_plan_payload,
)


def test_json_parser_accepts_first_object_with_trailing_text() -> None:
    payload = _json_from_text(
        '{"commands":[],"step_complete":true}\n'
        '{"unexpected":"second object"}'
    )
    assert payload["step_complete"] is True
    assert "unexpected" not in payload


def test_json_parser_rejects_non_json() -> None:
    with pytest.raises(PlannerError):
        _json_from_text("not json")


def test_single_command_shorthand_is_wrapped() -> None:
    normalized = _normalize_plan_payload(
        {"op": "screenshot", "args": ["e122"]}
    )
    assert normalized["commands"] == [{"op": "screenshot", "args": ["e122"]}]
    assert normalized["step_complete"] is False
