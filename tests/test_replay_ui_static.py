from __future__ import annotations

from pathlib import Path


def test_failure_banner_hidden_attribute_overrides_visible_flex_rule() -> None:
    css_path = Path("src/ai_mime/editor/web/replay.css")
    css = css_path.read_text(encoding="utf-8")

    visible_rule = css.index(".failure-banner {")
    hidden_rule = css.index(".failure-banner[hidden]")

    assert hidden_rule > visible_rule
    assert ".failure-banner[hidden] { display: none; }" in css
