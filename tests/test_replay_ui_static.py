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
    assert "buildUiAgentFallbackPrompt" in js
    assert "startUiAgentFallback" in js
    assert "submitAgentPrompt" in js
    assert "stdoutTail" in js
    assert "stderrTail" in js
    assert "references/fallback_plan.md" in js
    assert "$AI_MIME_UI_AGENT_CMD" in js
    assert "First triage the failure before editing anything" in js
    assert "Closed tabs" in js
    assert "Restore or continue the expected UI state first" in js
    assert "Only rewrite run.sh, scripts/run.py, or other skill files if the logs/script show a real skill defect" in js
    assert "replay:agent-context" in js
    assert "replay:handover:" not in js
    assert "AI_MIME_REPLAY_HANDOFF_TO_SKILL_BUILD" not in js
    assert "window.location.assign(`/skill-build/" not in js
    assert "Do not switch to skill-build mode" not in js
    assert "Handing off to the UI agent to complete the task" in html


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


def test_replay_ui_does_not_submit_fill_in_hints_as_values() -> None:
    js = Path("src/ai_mime/editor/web/replay.js").read_text(encoding="utf-8")

    assert "fillInHintMatch" in js
    assert "isFillInHintValue" in js
    assert 'setPathValue(values, path, isFillInHintValue(input.value) ? "" : input.value)' in js
    assert "isFillInHintValue(v)" in js
    assert "raw.map((itemRaw, index)" in js
    assert 'if (!isFillInHintValue(input.value)) return;' in js
    assert 'input.value = "";' in js


def test_replay_ui_renders_logs_section_for_older_runs() -> None:
    js = Path("src/ai_mime/editor/web/replay.js").read_text(encoding="utf-8")

    assert 'res.logs = extractSection("Logs")' in js
    assert "if (parsed.logs)" in js
    assert 'line.startsWith("[stderr] ")' in js
