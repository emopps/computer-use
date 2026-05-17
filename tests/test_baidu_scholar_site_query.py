"""baidu_scholar_site_in_query：含 site:xueshu.baidu.com 时解析为站内关键词。"""
from __future__ import annotations

from os_computer_use.desktop.research_literature_workflow import baidu_scholar_site_in_query


def test_strip_site_xueshu_baidu() -> None:
    hit, q = baidu_scholar_site_in_query("参数高效微调 大模型 site:xueshu.baidu.com")
    assert hit is True
    assert "site:" not in q.lower()
    assert "参数高效微调" in q


def test_plain_query_unchanged() -> None:
    hit, q = baidu_scholar_site_in_query("杭州天气")
    assert hit is False
    assert q == "杭州天气"
