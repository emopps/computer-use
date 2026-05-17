from __future__ import annotations

import asyncio

from os_computer_use.agents.action import ActionAgent
from os_computer_use.agents.planner import PlannerAgent
from os_computer_use.runtime.task_models import ExecutionContext, OperationResult, OperationSpec, OperationStatus, TaskSpec


def test_seed_previous_results_populates_execution_context():
    context = ExecutionContext(
        instruction="send meeting assignment email",
        task_spec=TaskSpec(summary="send", success_criteria=[], operations=[], metadata={"task_completed": True}),
    )
    previous_results = [
        {
            "operation_id": "extract_meeting_actions",
            "kind": "meeting.extract_actions",
            "output": {"meeting_title": "Demo", "assignments": [{"owner": "Alice"}]},
        }
    ]

    PlannerAgent._seed_previous_results(context, previous_results)

    assert "extract_meeting_actions" in context.results
    assert context.results["extract_meeting_actions"].status == OperationStatus.COMPLETED
    assert context.results["extract_meeting_actions"].output["meeting_title"] == "Demo"


class FakeDesktop:
    def __init__(self):
        self.calls = []

    async def browser_send(self, to, subject="", body="", attachments=None, page_key=None, authorize_before_send=None):
        self.calls.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "attachments": list(attachments or []),
                "page_key": page_key,
                "authorize_before_send": authorize_before_send,
            }
        )
        return True


def test_meeting_send_assignments_uses_browser_send_and_deferred_authorization():
    desktop = FakeDesktop()
    action_agent = ActionAgent(
        desktop=desktop,
        file_tool=None,
        reasoning_model=None,
        vision_model=None,
        artifact_dir="",
    )
    auth_cb = object()
    action_agent.authorization_callback = auth_cb

    payload = {
        "meeting_title": "Demo Meeting",
        "meeting_date": "2026-05-12 10:00:00",
        "source_url": "https://meeting.tencent.com/ctm/demo",
        "overall_summary": "summary",
        "email_map": {"Alice": "alice@example.com"},
        "assignments": [
            {
                "owner": "Alice",
                "email": "alice@example.com",
                "tasks": ["Finalize the slides"],
                "deadline": "",
                "notes": "",
                "mail_subject": "[任务分配] Demo Meeting - Alice",
                "mail_body": "Alice, please finalize the slides.",
            }
        ],
    }

    operation = OperationSpec(
        id="send_assignment_emails",
        kind="meeting.send_assignments",
        description="send assignment emails",
        arguments={"from_operation": "extract_meeting_actions"},
        depends_on=[],
        risky=False,
    )
    context = ExecutionContext(
        instruction="send meeting assignment email",
        task_spec=TaskSpec(summary="send", success_criteria=[], operations=[operation]),
        results={
            "extract_meeting_actions": OperationResult(
                operation_id="extract_meeting_actions",
                status=OperationStatus.COMPLETED,
                output=payload,
            )
        },
    )

    result = asyncio.run(action_agent.execute(operation, context))

    assert result["delivery_mode"] == "browser"
    assert result["sent_count"] == 1
    assert result["results"][0]["email"] == "alice@example.com"
    assert len(desktop.calls) == 1
    assert desktop.calls[0]["authorize_before_send"] is auth_cb
