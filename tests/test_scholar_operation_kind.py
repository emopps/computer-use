from __future__ import annotations

from os_computer_use.runtime.capabilities import is_supported_operation_kind


def test_scholar_baidu_search_is_supported() -> None:
    assert is_supported_operation_kind("scholar.baidu_search") is True


def test_research_collect_literature_is_supported() -> None:
    assert is_supported_operation_kind("research.collect_literature") is True
