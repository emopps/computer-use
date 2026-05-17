from __future__ import annotations

import json
from pathlib import Path

from os_computer_use.runtime.meeting_checkpoint import clear_checkpoint, load_checkpoint_for_prompt, persist_checkpoint_for_prompt


def test_meeting_checkpoint_persists_only_extract_action(tmp_path):
    checkpoint = tmp_path / "meeting_checkpoint.json"
    persist_checkpoint_for_prompt(
        "prompt-a",
        [
            {
                "eval_id": "step_1_extract_meeting_actions",
                "operation_id": "extract_meeting_actions",
                "kind": "meeting.extract_actions",
                "description": "extract",
                "arguments": {"source_url": "https://meeting.tencent.com/demo"},
                "output": {"meeting_title": "Demo", "assignments": [{"owner": "李国昌"}]},
            },
            {
                "eval_id": "step_2_send_assignment_emails",
                "operation_id": "send_assignment_emails",
                "kind": "meeting.send_assignments",
                "description": "send",
                "arguments": {"from_operation": "extract_meeting_actions"},
                "output": {"sent_count": 2},
            },
        ],
        path=str(checkpoint),
    )

    loaded = load_checkpoint_for_prompt("prompt-a", path=str(checkpoint))

    assert len(loaded) == 1
    assert loaded[0]["kind"] == "meeting.extract_actions"
    assert loaded[0]["output"]["meeting_title"] == "Demo"


def test_meeting_checkpoint_is_deleted_for_different_prompt(tmp_path):
    checkpoint = tmp_path / "meeting_checkpoint.json"
    persist_checkpoint_for_prompt(
        "prompt-a",
        [
            {
                "eval_id": "step_1_extract_meeting_actions",
                "operation_id": "extract_meeting_actions",
                "kind": "meeting.extract_actions",
                "description": "extract",
                "arguments": {},
                "output": {"meeting_title": "Demo"},
            }
        ],
        path=str(checkpoint),
    )

    loaded = load_checkpoint_for_prompt("prompt-b", path=str(checkpoint))

    assert loaded == []
    assert not checkpoint.exists()


def test_meeting_checkpoint_is_cleared_when_no_extract_steps(tmp_path):
    checkpoint = tmp_path / "meeting_checkpoint.json"
    checkpoint.write_text(json.dumps({"prompt_key": "prompt-a", "steps": []}), encoding="utf-8")

    persist_checkpoint_for_prompt("prompt-a", [], path=str(checkpoint))

    assert not checkpoint.exists()
    clear_checkpoint(str(checkpoint))
