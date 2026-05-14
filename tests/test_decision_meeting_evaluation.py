from __future__ import annotations

from os_computer_use.agents.decision import DecisionAgent
from os_computer_use.runtime.task_models import OperationSpec, TaskSpec


def test_meeting_workflow_structured_evaluation_accepts_real_artifacts():
    agent = DecisionAgent(provider=None)
    task_spec = TaskSpec(
        summary="会议纪要任务分配已完成",
        success_criteria=["会议纪要已读取并完成任务分配邮件处理"],
        operations=[
            OperationSpec(
                id="extract_meeting_actions",
                kind="meeting.extract_actions",
                description="提取会议任务",
            ),
            OperationSpec(
                id="send_assignment_emails",
                kind="meeting.send_assignments",
                description="发送会议任务邮件",
            ),
        ],
        metadata={"task_completed": True, "scenario": "meeting_assignment"},
    )

    result = agent.evaluate_result(
        instruction="读取真实会议内容和逐字稿，提取每位发言人的待办事项，生成会议纪要并发送邮件。",
        task_spec=task_spec,
        execution_results={
            "extract_meeting_actions": {
                "meeting_title": "组会纪要",
                "overall_summary": "会议总结",
                "summary_path": "/tmp/meeting_minutes.md",
                "assignments_path": "/tmp/task_assignments.json",
                "transcript_text": "张三 10:00 今天把任务文档上传到共享文档。",
                "assignments": [
                    {
                        "owner": "张三",
                        "email": "zhangsan@example.com",
                        "tasks": ["今天把任务文档上传到共享文档。"],
                    },
                    {
                        "owner": "李四",
                        "email": "lisi@example.com",
                        "tasks": [],
                    },
                ],
            },
            "send_assignment_emails": {
                "delivery_mode": "browser",
                "sent_count": 2,
                "preview_count": 0,
                "results": [
                    {"owner": "张三", "status": "sent"},
                    {"owner": "李四", "status": "sent"},
                ],
            },
        },
    )

    assert result["satisfied"] is True
    assert "会议任务" in result["reason"] or "待办" in result["reason"] or "发送" in result["reason"]


def test_meeting_workflow_structured_evaluation_rejects_missing_tasks():
    agent = DecisionAgent(provider=None)
    task_spec = TaskSpec(
        summary="读取真实会议纪要并抽取任务分配",
        success_criteria=["已从真实会议页面提取每位负责人的任务"],
        operations=[
            OperationSpec(
                id="extract_meeting_actions",
                kind="meeting.extract_actions",
                description="提取会议任务",
            )
        ],
        metadata={"scenario": "meeting_assignment"},
    )

    result = agent.evaluate_result(
        instruction="读取真实会议内容和逐字稿，提取每位发言人的待办事项并生成会议纪要。",
        task_spec=task_spec,
        execution_results={
            "extract_meeting_actions": {
                "meeting_title": "组会纪要",
                "overall_summary": "会议总结",
                "summary_path": "/tmp/meeting_minutes.md",
                "transcript_text": "今天主要讨论论文。",
                "assignments": [
                    {
                        "owner": "张三",
                        "email": "zhangsan@example.com",
                        "tasks": [],
                    }
                ],
            }
        },
    )

    assert result["satisfied"] is False
    assert "待办事项" in result["reason"]


def test_research_literature_evaluation_does_not_fall_back_to_composition_title_reason():
    agent = DecisionAgent(provider=None)
    task_spec = TaskSpec(
        summary="将检索到的文献逐格写入 Excel",
        success_criteria=["已通过 spreadsheet.write_cell 将文献结果写入表格"],
        operations=[
            OperationSpec(
                id="write_title",
                kind="spreadsheet.write_cell",
                description="写入标题",
                arguments={"cell": "B2", "text": "基于思维链增强的分类分级大模型微调方法研究"},
            ),
            OperationSpec(
                id="write_author",
                kind="spreadsheet.write_cell",
                description="写入作者",
                arguments={"cell": "C2", "text": "徐允彪，邓莎，李亚荣"},
            ),
        ],
        metadata={"scenario": "research_literature", "phase": "spreadsheet_write"},
    )
    result = agent.evaluate_result(
        instruction="帮我在百度学术搜索和大模型微调有关的论文，并写入表格，输出标题、作者、发表年份。",
        task_spec=task_spec,
        execution_results={
            "write_title": {"text": "基于思维链增强的分类分级大模型微调方法研究"},
            "write_author": {"text": "徐允彪，邓莎，李亚荣"},
        },
    )
    assert result["satisfied"] is True
    assert "作文标题" not in result["reason"]
