from pathlib import Path


def test_agent_js_runtime_gate_uses_runtime_id_not_source() -> None:
    js = Path("src/ai_mime/editor/web/agent.js").read_text(encoding="utf-8")

    assert "active.runtime_id" in js
    assert "Switch your agent runtime to" in js
    assert "session-meta" in js
    assert "runtimeLabel(sessionRuntime)" in js
    assert "active.source !== activeRuntime" not in js


def test_agent_css_keeps_runtime_and_model_labels_readable() -> None:
    css = Path("src/ai_mime/editor/web/agent.css").read_text(encoding="utf-8")

    assert ".session-meta" in css
    assert "min-width: 230px" in css
    assert "max-width: 320px" in css
