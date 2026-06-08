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
    assert '<script src="/static/replay.js?v=20260608-template-hints"></script>' in html
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
    assert "optionalHintMatch" in js
    assert "isFillInHintValue" in js
    assert "isTemplateHintValue" in js
    assert "inputs.template.json values are UI support text only" in js
    assert "placeholder = optional[1].trim()" in js
    assert "default: emptyValueForType" in js
    assert 'setPathValue(values, path, isTemplateHintValue(input.value) ? "" : input.value)' in js
    assert "defaultVal = value" not in js
    assert "pre-filled default value" not in js
    assert "isTemplateHintValue(v)" in js
    assert 'data-placeholder="${escapeHtml(placeholder)}"' in js
    assert 'input.placeholder = "";' in js
    assert "input.placeholder = input.dataset.placeholder" in js


def test_replay_inputs_template_endpoint_uses_only_inputs_folder() -> None:
    server = Path("src/ai_mime/editor/server.py").read_text(encoding="utf-8")
    start = server.index('@app.get("/api/tasks/{task_id}/skill/inputs-template")')
    end = server.index('@app.post("/api/tasks/{task_id}/skill/run/stream")')
    endpoint = server[start:end]

    assert 'skill_dir / "inputs" / "inputs.template.json"' in endpoint
    assert "optimized_plan" not in endpoint
