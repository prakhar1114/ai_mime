from __future__ import annotations

from pathlib import Path


def test_failure_banner_hidden_attribute_overrides_visible_flex_rule() -> None:
    css_path = Path("src/ai_mime/editor/web/replay.css")
    css = css_path.read_text(encoding="utf-8")

    visible_rule = css.index(".failure-banner {")
    hidden_rule = css.index(".failure-banner[hidden]")

    assert hidden_rule > visible_rule
    assert ".failure-banner[hidden] { display: none; }" in css


def test_replay_ui_uses_real_agent_transport() -> None:
    html = Path("src/ai_mime/editor/web/replay.html").read_text(encoding="utf-8")
    js = Path("src/ai_mime/editor/web/replay.js").read_text(encoding="utf-8")

    assert 'data-api-prefix="/api/tasks/__TASK_ID__/replay-agent"' in html
    assert '<script src="/static/agent.js"></script>' in html
    assert "TODO(agent-runner)" not in js
    assert "buildReplayAgentPrompt" in js
    assert "stdoutTail" in js
    assert "stderrTail" in js
    assert "AI_MIME_REPLAY_HANDOFF_TO_SKILL_BUILD" in js


def test_replay_ui_supports_nested_repeatable_inputs() -> None:
    js = Path("src/ai_mime/editor/web/replay.js").read_text(encoding="utf-8")
    css = Path("src/ai_mime/editor/web/replay.css").read_text(encoding="utf-8")

    assert "type: \"array\"" in js
    assert "renderArrayField" in js
    assert "data-array-add" in js
    assert "data-array-remove" in js
    assert "setPathValue(values, path" in js
    assert ".field-array" in css
    assert ".array-item" in css
