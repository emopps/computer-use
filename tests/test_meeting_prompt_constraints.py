from __future__ import annotations

import json

from os_computer_use.desktop.meeting_workflow import MeetingWorkflow


class CaptureProvider:
    def __init__(self):
        self.messages = []

    def call(self, messages, functions=None):
        self.messages = messages
        return json.dumps(
            {
                "meeting_title": "示例会议",
                "meeting_date": "2026-04-27 14:56",
                "source_url": "https://meeting.tencent.com/ctm/demo",
                "overall_summary": "中文摘要",
                "assignments": [
                    {
                        "owner": "李国昌",
                        "email": "a@example.com",
                        "tasks": ["中文任务"],
                        "deadline": "",
                        "notes": "",
                    }
                ],
            },
            ensure_ascii=False,
        )


def test_extract_prompt_requires_chinese_output_and_owner_separation():
    provider = CaptureProvider()
    workflow = MeetingWorkflow(provider=provider)

    workflow._extract_with_model(
        meeting_title="示例会议",
        meeting_date="2026-04-27 14:56",
        source_url="https://meeting.tencent.com/ctm/demo",
        summary_text="summary",
        transcript_text="李国昌 14:56: please send the paper link",
        grounded_transcript="李国昌 14:56: please send the paper link",
        email_map={"李国昌": "a@example.com", "徐浩然": "b@example.com"},
    )

    prompt = provider.messages[0]["content"]
    assert "Write overall_summary, tasks, deadline, and notes entirely in Simplified Chinese." in prompt
    assert "Keep each owner's tasks strictly separated. Never place one person's task under another person's assignment." in prompt
