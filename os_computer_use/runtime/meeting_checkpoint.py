from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List


def checkpoint_path() -> str:
    return os.path.abspath("./output/meeting_assignment_checkpoint.json")


def clear_checkpoint(path: str | None = None) -> None:
    target = Path(path or checkpoint_path())
    try:
        if target.exists():
            target.unlink()
    except Exception:
        return


def load_checkpoint_for_prompt(prompt_key: str, path: str | None = None) -> List[Dict[str, Any]]:
    target = Path(path or checkpoint_path())
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        clear_checkpoint(str(target))
        return []
    if str(payload.get("prompt_key", "") or "") != str(prompt_key or ""):
        clear_checkpoint(str(target))
        return []
    steps = payload.get("steps", [])
    if not isinstance(steps, list):
        clear_checkpoint(str(target))
        return []
    normalized = []
    for item in steps:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind", "") or "") != "meeting.extract_actions":
            continue
        normalized.append(
            {
                "eval_id": str(item.get("eval_id", "") or "checkpoint_extract_meeting_actions"),
                "operation_id": str(item.get("operation_id", "") or "extract_meeting_actions"),
                "kind": "meeting.extract_actions",
                "description": str(item.get("description", "") or "从 checkpoint 恢复会议任务抽取结果"),
                "arguments": dict(item.get("arguments", {}) or {}),
                "output": item.get("output"),
            }
        )
    return normalized


def persist_checkpoint_for_prompt(prompt_key: str, executed_steps: List[Dict[str, Any]], path: str | None = None) -> None:
    filtered_steps = []
    for item in executed_steps or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind", "") or "") != "meeting.extract_actions":
            continue
        if item.get("output") is None:
            continue
        filtered_steps.append(
            {
                "eval_id": str(item.get("eval_id", "") or "checkpoint_extract_meeting_actions"),
                "operation_id": str(item.get("operation_id", "") or "extract_meeting_actions"),
                "kind": "meeting.extract_actions",
                "description": str(item.get("description", "") or ""),
                "arguments": dict(item.get("arguments", {}) or {}),
                "output": item.get("output"),
            }
        )
    target = Path(path or checkpoint_path())
    if not filtered_steps:
        clear_checkpoint(str(target))
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "prompt_key": str(prompt_key or ""),
                "updated_at": int(time.time()),
                "steps": filtered_steps,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
