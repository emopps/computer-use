from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from os_computer_use.runtime.task_models import OperationResult, OperationStatus


class SchedulerError(RuntimeError):
    pass


@dataclass
class SchedulerNode:
    node_id: str
    agent_type: str
    action: str
    params: Dict[str, Any]
    description: str = ""
    dependencies: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: OperationStatus = OperationStatus.PENDING
    result: Any = None
    error: Optional[str] = None


class DAGScheduler:
    def __init__(self):
        self.nodes: Dict[str, SchedulerNode] = {}
        self.results: Dict[str, OperationResult] = {}

    def add_node(self, node: SchedulerNode) -> None:
        if node.node_id in self.nodes:
            raise SchedulerError(f"Duplicate scheduler node id: {node.node_id}")
        self.nodes[node.node_id] = node

    def validate(self) -> None:
        missing_dependencies = []
        for node in self.nodes.values():
            for dependency in node.dependencies:
                if dependency not in self.nodes:
                    missing_dependencies.append((node.node_id, dependency))
        if missing_dependencies:
            details = ", ".join(f"{node}->{dep}" for node, dep in missing_dependencies)
            raise SchedulerError(f"Scheduler graph has missing dependencies: {details}")
        self._ensure_acyclic()

    def _ensure_acyclic(self) -> None:
        temporary = set()
        permanent = set()

        def visit(node_id: str) -> None:
            if node_id in permanent:
                return
            if node_id in temporary:
                raise SchedulerError(f"Scheduler graph contains a cycle involving {node_id}")
            temporary.add(node_id)
            for dependency in self.nodes[node_id].dependencies:
                visit(dependency)
            temporary.remove(node_id)
            permanent.add(node_id)

        for node_id in self.nodes:
            visit(node_id)

    async def execute_all(
        self, executor: Callable[[SchedulerNode], Awaitable[Any]]
    ) -> Dict[str, OperationResult]:
        self.validate()
        pending = set(self.nodes.keys())

        while pending:
            blocked_now = [node_id for node_id in list(pending) if self.nodes[node_id].status == OperationStatus.BLOCKED]
            for node_id in blocked_now:
                pending.remove(node_id)
            if not pending:
                break
            ready = [
                self.nodes[node_id]
                for node_id in list(pending)
                if self._dependencies_completed(node_id)
            ]
            blocked_now = [node_id for node_id in list(pending) if self.nodes[node_id].status == OperationStatus.BLOCKED]
            for node_id in blocked_now:
                pending.remove(node_id)
            if not pending:
                break
            if not ready:
                failures = [
                    {
                        "node_id": node_id,
                        "status": self.nodes[node_id].status.value,
                        "error": self.nodes[node_id].error,
                    }
                    for node_id in sorted(pending)
                ]
                raise SchedulerError(
                    "No runnable nodes remain; current node states: {}".format(failures)
                )

            batch_results = await asyncio.gather(
                *(self._run_node(node, executor) for node in ready)
            )
            for result in batch_results:
                self.results[result.operation_id] = result
                pending.remove(result.operation_id)

        return self.results

    def _dependencies_completed(self, node_id: str) -> bool:
        node = self.nodes[node_id]
        for dependency in node.dependencies:
            dep_node = self.nodes[dependency]
            if dep_node.status == OperationStatus.FAILED:
                node.status = OperationStatus.BLOCKED
                node.error = f"Dependency failed: {dependency}"
                self.results[node.node_id] = OperationResult(
                    operation_id=node.node_id,
                    status=OperationStatus.BLOCKED,
                    error=node.error,
                )
                return False
            if dep_node.status != OperationStatus.COMPLETED:
                return False
        return True

    async def _run_node(
        self, node: SchedulerNode, executor: Callable[[SchedulerNode], Awaitable[Any]]
    ) -> OperationResult:
        if node.status == OperationStatus.BLOCKED:
            return self.results[node.node_id]

        node.status = OperationStatus.RUNNING
        try:
            node.result = await executor(node)
            node.status = OperationStatus.COMPLETED
            return OperationResult(
                operation_id=node.node_id,
                status=OperationStatus.COMPLETED,
                output=node.result,
            )
        except Exception as exc:
            node.error = str(exc)
            node.status = OperationStatus.FAILED
            return OperationResult(
                operation_id=node.node_id,
                status=OperationStatus.FAILED,
                error=node.error,
            )


TaskNode = SchedulerNode
TaskStatus = OperationStatus
