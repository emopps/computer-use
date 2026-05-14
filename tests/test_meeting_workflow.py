from __future__ import annotations

from os_computer_use.desktop.meeting_workflow import MeetingWorkflow


class FakeProvider:
    def call(self, messages):
        return """
        {
          "meeting_title": "转写_大模型与智能软工组——论文分享会",
          "meeting_date": "2026-04-27 14:56:43",
          "source_url": "https://meeting.tencent.com/ctm/2pE5ZXWDaa",
          "overall_summary": "会议围绕论文分享和后续材料整理展开。",
          "assignments": [
            {
              "owner": "张三",
              "email": "zhangsan@example.com",
              "tasks": ["整理论文分享会提纲并明天下午前发到组里", "补齐相关参考文献"],
              "deadline": "明天下午前",
              "notes": ""
            }
          ]
        }
        """


def test_extract_actions_with_rule_fallback(tmp_path):
    workflow = MeetingWorkflow(provider=FakeProvider(), artifact_root=str(tmp_path))
    result = workflow.extract_actions(
        """
        请根据真实腾讯会议纪要做任务分配并发邮件。
        张三=zhangsan@example.com
        """,
        {
            "url": "https://meeting.tencent.com/ctm/2pE5ZXWDaa，读取真实会议内容和逐字稿",
            "meeting_title": "转写_大模型与智能软工组——论文分享会",
            "meeting_date": "2026-04-27 14:56:43",
            "summary_text": "会议主题：论文分享会",
            "transcript_text": "张三 14:56 这周先把论文分享会的提纲整理出来，明天下午前发到组里，另外把相关参考文献补齐。",
            "body_text": "真实会议页面正文",
            "page_title": "转写_大模型与智能软工组——论文分享会",
        },
    )

    assert result["meeting_title"] == "转写_大模型与智能软工组——论文分享会"
    assert result["source_url"] == "https://meeting.tencent.com/ctm/2pE5ZXWDaa"
    assert len(result["assignments"]) == 1
    assert result["assignments"][0]["mail_subject"]
    assert "https://meeting.tencent.com/ctm/2pE5ZXWDaa" in result["assignments"][0]["mail_body"]
    assert result["summary_path"]
    assert result["assignments_path"]


def test_send_assignments_preview_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("OCU_SMTP_HOST", raising=False)
    monkeypatch.delenv("OCU_ASSIGNMENT_MAIL_MODE", raising=False)
    workflow = MeetingWorkflow(provider=FakeProvider(), artifact_root=str(tmp_path))
    extracted = workflow.extract_actions(
        "张三=zhangsan@example.com",
        {
            "url": "https://meeting.tencent.com/ctm/2pE5ZXWDaa，读取真实会议内容和逐字稿",
            "meeting_title": "论文分享会",
            "meeting_date": "2026-04-27 14:56:43",
            "summary_text": "会议主题：论文分享会",
            "transcript_text": "张三 14:56 负责整理论文提纲，并在明天下午前发到组里。",
            "body_text": "真实会议页面正文",
            "page_title": "论文分享会",
        },
    )

    result = workflow.send_assignments(extracted)

    assert result["delivery_mode"] == "preview"
    assert result["preview_count"] == 1
    assert result["results"][0]["preview_path"].endswith(".eml")


def test_normalize_assignments_keeps_string_task_as_single_item():
    workflow = MeetingWorkflow(provider=None)
    normalized = workflow._normalize_assignments(
        [
            {
                "owner": "Alice",
                "email": "alice@example.com",
                "tasks": "Finalize the API field mapping document and send it to the group before 17:00 today.",
                "deadline": "2026-05-12 17:00:00",
                "notes": "Before 17:00 today",
            }
        ],
        {},
    )

    assert len(normalized) == 1
    assert normalized[0]["tasks"] == [
        "Finalize the API field mapping document and send it to the group before 17:00 today."
    ]


def test_email_map_recipient_is_kept_even_without_tasks():
    workflow = MeetingWorkflow(provider=None)
    normalized = workflow._ensure_assignments_cover_email_map(
        workflow._normalize_assignments([], {"徐浩然": "xu@example.com"}),
        {"徐浩然": "xu@example.com"},
    )

    assert len(normalized) == 1
    assert normalized[0]["owner"] == "徐浩然"
    assert normalized[0]["email"] == "xu@example.com"
    assert normalized[0]["tasks"] == []


def test_mail_body_without_tasks_becomes_minutes_notice():
    workflow = MeetingWorkflow(provider=None)
    subject = workflow._build_mail_subject("示例会议", "徐浩然", has_tasks=False)
    body = workflow._build_mail_body(
        meeting_title="示例会议",
        meeting_date="2026-05-12 10:00:00",
        source_url="https://meeting.tencent.com/ctm/demo",
        overall_summary="会议总结内容。",
        assignment={"owner": "徐浩然", "email": "xu@example.com", "tasks": [], "deadline": "", "notes": ""},
    )

    assert subject.startswith("[会议纪要]")
    assert "未提取到分配给你的明确待办事项" in body
