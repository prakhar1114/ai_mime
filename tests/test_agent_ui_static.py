from pathlib import Path


def test_agent_js_runtime_gate_uses_runtime_id_not_source() -> None:
    js = Path("src/ai_mime/editor/web/agent.js").read_text(encoding="utf-8")

    assert "active.runtime_id" in js
    assert "Switch your agent runtime to continue it" in js
    assert "active.source !== activeRuntime" not in js

