from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskModelError(ValueError):
    pass


class OperationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class OperationSpec:
    id: str
    kind: str
    description: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    risky: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OperationSpec":
        op = cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            description=str(data.get("description", data["kind"])),
            arguments=dict(data.get("arguments", {})),
            depends_on=[str(item) for item in data.get("depends_on", [])],
            risky=bool(data.get("risky", False)),
        )
        op.validate()
        return op

    def validate(self) -> None:
        if not self.id:
            raise TaskModelError("Operation id cannot be empty.")
        if not self.kind:
            raise TaskModelError(f"Operation {self.id} is missing kind.")


@dataclass
class TaskSpec:
    summary: str
    success_criteria: List[str]
    operations: List[OperationSpec]
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskSpec":
        operations = [OperationSpec.from_dict(item) for item in data.get("operations", [])]
        spec = cls(
            summary=str(data.get("summary", "")).strip(),
            success_criteria=[str(item) for item in data.get("success_criteria", [])],
            operations=operations,
            metadata=dict(data.get("metadata", {})),
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        if not self.summary:
            raise TaskModelError("Task summary cannot be empty.")
        if not self.operations:
            raise TaskModelError("Task must include at least one operation.")

        seen = set()
        for operation in self.operations:
            operation.validate()
            if operation.id in seen:
                raise TaskModelError(f"Duplicate operation id: {operation.id}")
            seen.add(operation.id)

        for operation in self.operations:
            missing = [dep for dep in operation.depends_on if dep not in seen]
            if missing:
                raise TaskModelError(
                    f"Operation {operation.id} depends on unknown operations: {missing}"
                )


@dataclass
class OperationResult:
    operation_id: str
    status: OperationStatus
    output: Any = None
    error: Optional[str] = None


@dataclass
class ExecutionContext:
    instruction: str
    task_spec: TaskSpec
    results: Dict[str, OperationResult] = field(default_factory=dict)
    artifacts: Dict[str, Any] = field(default_factory=dict)
