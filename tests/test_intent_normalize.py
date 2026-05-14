from __future__ import annotations

from os_computer_use.agents.intent import IntentAgent


class FakeProvider:
    def __init__(self, response: str):
        self.response = response

    def call(self, messages, functions=None):
        return self.response


def test_normalize_restores_explicit_email_mappings_from_original_instruction():
    instruction = (
        "打开 https://meeting.tencent.com/ctm/2pE5ZXWDaa，读取真实会议内容和逐字稿，提取每位发言人的待办事项，"
        "生成会议纪要，并将任务分配邮件发送给以下人员：\n"
        "李国昌=receive_test2026@163.com\n"
        "徐浩然=receive_test2_2026@163.com"
    )
    provider_response = (
        "访问 https://meeting.tencent.com/ctm/2pE5ZXWDaa，提取会议内容和逐字稿中的每位发言人待办事项，"
        "生成会议纪要，并发送任务分配邮件至李国昌(receive_test2026@163.com)和徐浩然(receive_test2026@163.com)。"
    )
    agent = IntentAgent(provider=FakeProvider(provider_response))

    normalized = agent.normalize(instruction)

    assert "李国昌=receive_test2026@163.com" in normalized
    assert "徐浩然=receive_test2_2026@163.com" in normalized
    assert "徐浩然=receive_test2026@163.com" not in normalized
