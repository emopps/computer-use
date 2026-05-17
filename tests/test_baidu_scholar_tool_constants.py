from __future__ import annotations

from os_computer_use.desktop.research_literature_workflow import (
    BAIDU_SCHOLAR_HOME_URL,
    SCHOLAR_SEARCH_TEXTAREA_SELECTOR,
)


def test_primary_selector_is_textarea_atomic() -> None:
    assert "textarea" in SCHOLAR_SEARCH_TEXTAREA_SELECTOR
    assert "atomic-textarea-box" in SCHOLAR_SEARCH_TEXTAREA_SELECTOR
    assert "xueshu.baidu.com" in BAIDU_SCHOLAR_HOME_URL
