from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict

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

        normalized_input = prompt.strip()
        clarification_rounds = 0
        max_clarification_rounds = 3
        max_decision_retries = 2

        while True:
            try:
                if self._cancel_event.is_set():
                    raise TaskCancelledError("\u4efb\u52a1\u5df2\u53d6\u6d88\u3002")
                logger.log("Planning task...", "cyan")
                normalized_intent = agents["intent"].normalize(normalized_input)
                task_spec = agents["decision"].annotate(
                    agents["planner"].plan(normalized_intent),
                    memory_agent=agents["memory"],
                )
                evaluation = {"satisfied": False, "reason": "not executed"}
                execution_results = {}
                for decision_attempt in range(max_decision_retries + 1):
                    if self._cancel_event.is_set():
                        raise TaskCancelledError("\u4efb\u52a1\u5df2\u53d6\u6d88\u3002")
                    execution_results = await agents["planner"].execute_task(
                        instruction=normalized_intent,
                        task_spec=task_spec,
                        action_agent=agents["action"],
                        audit_agent=agents["audit"],
                        memory_agent=agents["memory"],
                        max_replans=2,
                        replan_callback=self._handle_progress,
                        should_cancel=lambda: self._cancel_event.is_set(),
                    )

                    evaluation = agents["decision"].evaluate_result(
                        instruction=normalized_intent,
                        task_spec=task_spec,
                        execution_results=execution_results or {},
                    )

                    logger.log(
                        "Decision evaluate: satisfied={}, reason={}".format(
                            evaluation["satisfied"],
                            evaluation["reason"],
                        ),
                        "green" if evaluation["satisfied"] else "yellow",
                    )

                    if evaluation["satisfied"]:
                        agents["memory"].append_history(
                            {
                                "instruction": normalized_intent,
                                "status": "success",
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

                    logger.log(
                        "Decision not satisfied, replanning (attempt {}/{})...".format(
                            decision_attempt + 2,
                            max_decision_retries + 1,
                        ),
                        "yellow",
                    )
                    task_spec = agents["decision"].annotate(
                        agents["planner"].replan(
                            instruction=normalized_intent,
                            failed_task_spec=task_spec,
                            failure_message=evaluation["reason"],
                        ),
                        memory_agent=agents["memory"],
                    )

                agents["memory"].summarize_session(normalized_intent, evaluation)
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
