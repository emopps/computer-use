"""research_literature_workflow 场景识别（与会议/天气解耦）。"""
from __future__ import annotations

from os_computer_use.desktop.research_literature_workflow import (
    example_demo_prompt,
    is_research_literature_table_scenario,
)


def test_scholar_excel_triggers_scenario() -> None:
    assert is_research_literature_table_scenario(example_demo_prompt())


def test_weather_query_not_triggered() -> None:
    assert not is_research_literature_table_scenario("打开浏览器搜索杭州天气")


def test_meeting_email_not_triggered() -> None:
    assert not is_research_literature_table_scenario(
        "打开 https://meeting.tencent.com/dm/demo 根据会议纪要给张三发任务邮件"
    )


def test_papers_without_table_not_triggered() -> None:
    assert not is_research_literature_table_scenario("在谷歌学术搜索大模型微调论文")
