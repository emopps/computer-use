from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

Signal = pyqtSignal
Slot = pyqtSlot

from os_computer_use.app_runtime import (
    build_agent,
    initialize_run_directories,
    warmup_runtime,
)
from os_computer_use.agents.intent import TaskClarificationRequired, TaskPlanningError
from os_computer_use.logging import logger
from os_computer_use.runtime.meeting_checkpoint import clear_checkpoint, load_checkpoint_for_prompt, persist_checkpoint_for_prompt
from os_computer_use.runtime.task_models import OperationSpec, TaskSpec


class TaskCancelledError(RuntimeError):
    pass


class AgentWorker(QObject):
    warmup_status = Signal(str)
    warmup_finished = Signal(bool, str)
    busy_changed = Signal(bool)
    log_message = Signal(str, str)
    task_message = Signal(str)
    plan_ready = Signal(str, list)
    operation_update = Signal(str, str, str, str)
    clarification_requested = Signal(str)
    clarification_consumed = Signal()
    authorization_requested = Signal(str, str)
    task_finished = Signal(dict)
    task_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._clarification_event = threading.Event()
        self._clarification_text = ""
        self._authorization_event = threading.Event()
        self._authorization_value = False
        self._busy = False
        self._warmup_complete = False
        self._cancel_event = threading.Event()

    @Slot()
    def warmup_models(self) -> None:
        if self._warmup_complete:
            self.warmup_finished.emit(True, "模型已就绪")
            return
        try:
            self.warmup_status.emit("正在启动时加载文本模型和图像模型...")
            asyncio.run(warmup_runtime())
            self._warmup_complete = True
            self.warmup_finished.emit(True, "模型已就绪")
        except Exception as exc:
            self.warmup_finished.emit(False, "模型预加载失败：{}".format(exc))

    @Slot(str)
    def process_prompt(self, prompt: str) -> None:
        if self._busy:
            self.task_message.emit("当前已有任务在执行。")
            return

        self._cancel_event.clear()
        self._busy = True
        self.busy_changed.emit(True)
        logger.subscribe(self._forward_log)
        try:
            result = asyncio.run(self._run_prompt(prompt))
            self.task_finished.emit(result)
        except Exception as exc:
            self.task_failed.emit(str(exc))
        finally:
            logger.unsubscribe(self._forward_log)
            self._busy = False
            self.busy_changed.emit(False)

    @Slot(str)
    def submit_clarification(self, text: str) -> None:
        self._clarification_text = text.strip()
        self._clarification_event.set()

    @Slot(bool)
    def submit_authorization(self, allowed: bool) -> None:
        self._authorization_value = bool(allowed)
        self._authorization_event.set()

    @Slot()
    def cancel_task(self) -> None:
        self._cancel_event.set()
        self._clarification_text = "__CANCELLED__"
        self._clarification_event.set()
        self._authorization_value = False
        self._authorization_event.set()

    def _forward_log(self, text: str, color: str) -> None:
        self.log_message.emit(str(text), str(color))

    def _request_clarification(self, question: str) -> str:
        self._clarification_text = ""
        self._clarification_event.clear()
        self.clarification_requested.emit(question)
        self._clarification_event.wait()
        if self._clarification_text == "__CANCELLED__":
            raise TaskCancelledError("任务已取消。")
        return self._clarification_text

    def _request_authorization(self, action_type: str, details: str) -> bool:
        self._authorization_value = False
        self._authorization_event.clear()
        self.authorization_requested.emit(action_type, details)
        self._authorization_event.wait()
        if self._cancel_event.is_set():
            raise TaskCancelledError("任务已取消。")
        return self._authorization_value

    def _handle_progress(self, payload: Dict[str, Any]) -> None:
        event_type = payload.get("event")
        if event_type == "plan":
            self.plan_ready.emit(str(payload.get("summary", "")), payload.get("operations", []))
            return
        if event_type == "operation":
            self.operation_update.emit(
                str(payload.get("operation_id", "")),
                str(payload.get("kind", "")),
                str(payload.get("status", "")),
                str(payload.get("error", "")),
            )
            return
        if event_type == "task":
            status = str(payload.get("status", ""))
            message = str(payload.get("summary") or payload.get("error") or status)
            self.task_message.emit(message)

    def _coerce_clarification_error(self, exc: Exception) -> TaskClarificationRequired | None:
        message = str(exc).strip()
        if not message:
            return None
        missing_key = "\u8fd8\u7f3a\u5c11"
        ask_more = "\u8bf7\u8865\u5145"
        if missing_key not in message and ask_more not in message:
            return None
        candidates = [part.strip() for part in message.split("|") if part.strip()]
        for part in reversed(candidates):
            if missing_key in part or ask_more in part:
                return TaskClarificationRequired(part)
        return TaskClarificationRequired(message)

    async def _run_prompt(self, prompt: str) -> Dict[str, Any]:
        output_dir, memory_dir, session_memory_dir = initialize_run_directories()
        agents = await build_agent(output_dir, memory_dir, session_memory_dir)
        agents["audit"].authorization_callback = self._request_authorization
        try:
            agents["action"].desktop.progress_callback = self._handle_progress
        except Exception:
            pass

        normalized_input = prompt.strip()
        checkpoint_seed_steps = load_checkpoint_for_prompt(normalized_input)
        clarification_rounds = 0
        max_clarification_rounds = 3
        max_decision_retries = 2
        max_execution_cycles = 20

        while True:
            try:
                if self._cancel_event.is_set():
                    raise TaskCancelledError("\u4efb\u52a1\u5df2\u53d6\u6d88\u3002")
                normalized_intent = agents["intent"].normalize(normalized_input)
                evaluation = {"satisfied": False, "reason": "not executed"}
                executed_steps: List[Dict[str, Any]] = [dict(item) for item in checkpoint_seed_steps]
                execution_results: Dict[str, Any] = {}
                previous_task_spec = None
                planning_error = None
                decision_attempt = 0
                cycle_count = 0
                while True:
                    if self._cancel_event.is_set():
                        raise TaskCancelledError("\u4efb\u52a1\u5df2\u53d6\u6d88\u3002")
                    if cycle_count >= max_execution_cycles:
                        raise RuntimeError("达到单步执行上限，任务仍未完成。")

                    logger.log("Planning task...", "cyan")
                    task_spec = agents["decision"].annotate(
                        agents["planner"].plan(
                            normalized_intent,
                            previous_error=planning_error,
                            previous_plan=previous_task_spec,
                            previous_results=executed_steps,
                        ),
                        memory_agent=agents["memory"],
                    )
                    previous_task_spec = task_spec

                    if task_spec.metadata.get("task_completed"):
                        unmet_side_effect_reason = self._detect_unmet_required_side_effect(
                            normalized_intent,
                            executed_steps,
                        )
                        if unmet_side_effect_reason:
                            planning_error = unmet_side_effect_reason
                            logger.log(
                                "Task completion rejected: {}".format(unmet_side_effect_reason),
                                "yellow",
                            )
                            continue
                        evaluation_spec = self._build_evaluation_task_spec(
                            normalized_intent,
                            executed_steps,
                            task_spec.summary,
                        )
                        evaluation = agents["decision"].evaluate_result(
                            instruction=normalized_intent,
                            task_spec=evaluation_spec,
                            execution_results={item["eval_id"]: item["output"] for item in executed_steps},
                        ) if executed_steps else {"satisfied": True, "reason": task_spec.summary}

                        logger.log(
                            "Decision evaluate: satisfied={}, reason={}".format(
                                evaluation["satisfied"],
                                evaluation["reason"],
                            ),
                            "green" if evaluation["satisfied"] else "yellow",
                        )
                        if evaluation["satisfied"]:
                            clear_checkpoint()
                            agents["memory"].append_history(
                                {
                                    "instruction": normalized_intent,
                                    "status": "success",
                                    "reason": evaluation["reason"],
                                }
                            )
                            break
                        if evaluation.get("manual_takeover_required"):
                            logger.log(
                                "Manual takeover required: {}".format(evaluation["reason"]),
                                "yellow",
                            )
                            agents["memory"].append_history(
                                {
                                    "instruction": normalized_intent,
                                    "status": "manual_takeover_required",
                                    "reason": evaluation["reason"],
                                }
                            )
                            break
                        if decision_attempt >= max_decision_retries:
                            logger.log(
                                "Decision retry limit reached, task incomplete: {}".format(
                                    evaluation["reason"]
                                ),
                                "red",
                            )
                            agents["memory"].append_history(
                                {
                                    "instruction": normalized_intent,
                                    "status": "failed",
                                    "reason": evaluation["reason"],
                                }
                            )
                            break
                        decision_attempt += 1
                        planning_error = evaluation["reason"]
                        logger.log(
                            "Decision not satisfied, replanning (attempt {}/{})...".format(
                                decision_attempt + 1,
                                max_decision_retries + 1,
                            ),
                            "yellow",
                        )
                        continue

                    execution_results = await agents["planner"].execute_task(
                        instruction=normalized_intent,
                        task_spec=task_spec,
                        action_agent=agents["action"],
                        audit_agent=agents["audit"],
                        memory_agent=agents["memory"],
                        max_replans=2,
                        replan_callback=self._handle_progress,
                        previous_results=executed_steps,
                        should_cancel=lambda: self._cancel_event.is_set(),
                    )
                    cycle_count += 1
                    planning_error = None
                    for operation in task_spec.operations:
                        eval_id = "step_{}_{}".format(cycle_count, operation.id)
                        executed_steps.append(
                            {
                                "eval_id": eval_id,
                                "operation_id": operation.id,
                                "kind": operation.kind,
                                "description": operation.description,
                                "arguments": dict(operation.arguments),
                                "output": execution_results.get(operation.id),
                            }
                        )
                    persist_checkpoint_for_prompt(normalized_input, executed_steps)

                agents["memory"].summarize_session(normalized_intent, evaluation)
                execution_results = {item["eval_id"]: item["output"] for item in executed_steps}
                return {
                    "instruction": normalized_intent,
                    "evaluation": evaluation,
                    "results": execution_results,
                    "output_dir": output_dir,
                    "session_memory_dir": session_memory_dir,
                }
            except TaskClarificationRequired as exc:
                if clarification_rounds >= max_clarification_rounds:
                    raise RuntimeError("\u6f84\u6e05\u8f6e\u6b21\u8fc7\u591a\uff0c\u8bf7\u91cd\u65b0\u63cf\u8ff0\u4efb\u52a1\u3002") from exc
                answer = self._request_clarification(exc.question)
                if not answer:
                    raise RuntimeError("\u672a\u63d0\u4f9b\u8865\u5145\u4fe1\u606f\uff0c\u4efb\u52a1\u5df2\u53d6\u6d88\u3002") from exc
                self.clarification_consumed.emit()
                normalized_input = "{}\n\u8865\u5145\u8bf4\u660e\uff1a{}".format(normalized_input, answer)
                clarification_rounds += 1
            except TaskPlanningError as exc:
                clarification = self._coerce_clarification_error(exc)
                if clarification is None:
                    raise
                if clarification_rounds >= max_clarification_rounds:
                    raise RuntimeError("\u6f84\u6e05\u8f6e\u6b21\u8fc7\u591a\uff0c\u8bf7\u91cd\u65b0\u63cf\u8ff0\u4efb\u52a1\u3002") from exc
                answer = self._request_clarification(clarification.question)
                if not answer:
                    raise RuntimeError("\u672a\u63d0\u4f9b\u8865\u5145\u4fe1\u606f\uff0c\u4efb\u52a1\u5df2\u53d6\u6d88\u3002") from exc
                self.clarification_consumed.emit()
                normalized_input = "{}\n\u8865\u5145\u8bf4\u660e\uff1a{}".format(normalized_input, answer)
                clarification_rounds += 1

            except TaskCancelledError as exc:
                raise RuntimeError(str(exc))

    @staticmethod
    def _build_evaluation_task_spec(
        instruction: str,
        executed_steps: List[Dict[str, Any]],
        summary: str,
    ) -> TaskSpec:
        operations = [
            OperationSpec(
                id=str(item["eval_id"]),
                kind=str(item.get("kind", "") or "operation"),
                description=str(item.get("description", "") or item.get("kind", "") or "operation"),
                arguments=dict(item.get("arguments", {}) or {}),
                depends_on=[],
                risky=False,
            )
            for item in executed_steps
        ]
        return TaskSpec(
            summary=str(summary or instruction).strip() or instruction,
            success_criteria=[],
            operations=operations,
            metadata={"source": "stepwise_execution"},
        )

    @staticmethod
    def _detect_unmet_required_side_effect(
        instruction: str,
        executed_steps: List[Dict[str, Any]],
    ) -> str:
        text = str(instruction or "")
        lowered = text.lower()
        requires_spreadsheet_write = any(
            token in text for token in ["写入", "填写", "单元格", "表格", "工作表", "Excel", "WPS"]
        ) or any(token in lowered for token in [".xlsx", ".xls", ".csv", "spreadsheet"])
        if requires_spreadsheet_write:
            target_count = AgentWorker._infer_required_item_count(text)
            meaningful_items: List[str] = []
            for item in executed_steps:
                if str(item.get("kind", "") or "") != "spreadsheet.write_cell":
                    continue
                output = item.get("output")
                if not isinstance(output, dict) or output.get("error"):
                    continue
                written_lines = output.get("written_lines", [])
                candidates = written_lines if isinstance(written_lines, list) and written_lines else str(
                    output.get("text", "") or ""
                ).splitlines()
                for candidate in candidates:
                    normalized = AgentWorker._normalize_meaningful_item(candidate)
                    if normalized and normalized not in meaningful_items:
                        meaningful_items.append(normalized)
            if len(meaningful_items) >= target_count:
                return ""
            return "The required spreadsheet write step has not been completed yet."
        requires_assignment_extract = any(token in text for token in ["会议", "纪要", "转写", "摘要"]) and any(
            token in text for token in ["任务分配", "待办", "行动项", "每个发言", "发言的人要做什么"]
        )
        if requires_assignment_extract and not any(
            str(item.get("kind", "") or "") == "meeting.extract_actions" for item in executed_steps
        ):
            return "The meeting assignment extraction step has not been completed yet."
        requires_assignment_send = any(token in text for token in ["发邮件", "发送邮件", "邮件", "邮箱"]) and any(
            token in text for token in ["会议", "纪要", "任务分配", "转写", "摘要"]
        )
        if requires_assignment_send and not any(
            str(item.get("kind", "") or "") in {"meeting.send_assignments", "browser.send"} for item in executed_steps
        ):
            return "The required meeting assignment email delivery step has not been completed yet."
        return ""

    @staticmethod
    def _infer_required_item_count(text: str) -> int:
        digit_match = __import__("re").search(r"(\d+)", text)
        if digit_match:
            try:
                return max(1, int(digit_match.group(1)))
            except Exception:
                return 1
        if __import__("re").search(r"[三3]", text):
            return 3
        if __import__("re").search(r"[二2]", text):
            return 2
        return 1

    @staticmethod
    def _normalize_meaningful_item(value: Any) -> str:
        import re

        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"^\s*\d+\s*[\.\)\-:]\s*", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 2 or len(text) > 80:
            return ""
        placeholder_patterns = [
            r"^title\s*\d*$",
            r"^essay\s*title\s*\d*$",
            r"^placeholder$",
            r"^todo$",
            r"^tbd$",
            r"^第?[一二三四五六七八九十0-9]+\s*篇?\s*(标题|题目)$",
            r"^第?[一二三四五六七八九十0-9]+\s*个?\s*(标题|题目)$",
            r"^(标题|题目)\s*[一二三四五六七八九十0-9]*$",
        ]
        for pattern in placeholder_patterns:
            if re.match(pattern, text, flags=re.IGNORECASE):
                return ""
        return text
