from __future__ import annotations

from typing import Any, Dict

EXECUTION_EVENT_SCHEMA_VERSION = 1
EXECUTION_EVENT_FAMILY = "execution"
LEGACY_EVENT_SCHEMA_VERSION = 0
LEGACY_EVENT_FAMILY = "legacy_compat"

_REQUIRED_EXECUTION_EVENT_FIELDS = {
    "schema_version",
    "event_family",
    "event_name",
    "phase",
    "attempt",
    "subject_type",
    "subject_id",
    "instruction",
    "task_summary",
    "outcome",
    "risk_level",
}


def build_execution_event(
    *,
    event_name: str,
    phase: str,
    attempt: int,
    subject_type: str,
    subject_id: str,
    instruction: str,
    task_summary: str,
    outcome: str,
    risk_level: str,
    **extra: Any,
) -> Dict[str, Any]:
    payload = {
        "schema_version": EXECUTION_EVENT_SCHEMA_VERSION,
        "event_family": EXECUTION_EVENT_FAMILY,
        "event_name": event_name,
        "phase": phase,
        "attempt": attempt,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "instruction": instruction,
        "task_summary": task_summary,
        "outcome": outcome,
        "risk_level": risk_level,
    }
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def is_execution_event_payload(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    return _REQUIRED_EXECUTION_EVENT_FIELDS.issubset(payload.keys())


def build_legacy_event_payload(
    *,
    legacy_event_type: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    legacy_payload = {
        "schema_version": LEGACY_EVENT_SCHEMA_VERSION,
        "event_family": LEGACY_EVENT_FAMILY,
        "legacy_event": True,
        "legacy_event_type": legacy_event_type,
        "compatibility_only": True,
    }
    legacy_payload.update(payload)
    return legacy_payload
