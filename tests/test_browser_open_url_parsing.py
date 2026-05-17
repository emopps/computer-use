from os_computer_use.agents.intent import IntentAgent


def test_extract_first_url_strips_trailing_chinese_instruction():
    text = (
        "https://meeting.tencent.com/ctm/2pE5ZXWDaa，读取会议纪要和逐字稿内容，"
        "提取每位发言人的待办事项。"
    )
    assert IntentAgent._extract_first_url(text) == "https://meeting.tencent.com/ctm/2pE5ZXWDaa"


def test_normalize_browser_open_arguments_keeps_only_url():
    agent = IntentAgent(provider=None)
    normalized = agent._normalize_operation_arguments(
        "打开 https://meeting.tencent.com/ctm/2pE5ZXWDaa，读取会议纪要并发邮件。",
        "browser.open",
        {
            "url": "https://meeting.tencent.com/ctm/2pE5ZXWDaa，读取会议纪要并发邮件。"
        },
    )
    assert normalized["url"] == "https://meeting.tencent.com/ctm/2pE5ZXWDaa"


def test_normalize_meeting_extract_actions_arguments_keeps_only_url():
    agent = IntentAgent(provider=None)
    normalized = agent._normalize_operation_arguments(
        "打开 https://meeting.tencent.com/ctm/2pE5ZXWDaa，读取真实会议内容和逐字稿，提取每位发言人的待办事项。",
        "meeting.extract_actions",
        {
            "source_url": "https://meeting.tencent.com/ctm/2pE5ZXWDaa，读取真实会议内容和逐字稿，提取每位发言人的待办事项。"
        },
    )
    assert normalized["source_url"] == "https://meeting.tencent.com/ctm/2pE5ZXWDaa"


def test_deterministic_task_spec_uses_clean_url():
    agent = IntentAgent(provider=None)
    spec = agent._build_deterministic_task_spec(
        "打开 https://meeting.tencent.com/ctm/2pE5ZXWDaa，读取会议纪要并发邮件。"
    )
    assert spec is not None
    assert spec.operations[0].kind == "browser.open"
    assert spec.operations[0].arguments["url"] == "https://meeting.tencent.com/ctm/2pE5ZXWDaa"
