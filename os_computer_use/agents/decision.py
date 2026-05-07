from __future__ import annotations

from collections import Counter
import json
import re
from typing import Any, Dict, List, Optional

from os_computer_use.runtime.task_models import TaskSpec


class DecisionAgent:
    """决策代理：执行前评估风险 + 执行后评估结果是否满足用户要求。

    执行前（annotate）：根据操作类型、历史记录、规则匹配，标注风险等级。
    执行后（evaluate_result）：用 LLM 判断执行输出是否满足用户原始要求，
    返回 (satisfied, reason)，不满足时触发重做。"""

    def __init__(self, provider=None):
        """provider: LLM 提供者，用于 evaluate_result 的结果评估。"""
        self.provider = provider

    DECISION_PRIORITY = {
        "risk": 4,
        "confirm": 3,
        "track_only": 2,
        "register": 1,
    }
    RISK_LEVEL_PRIORITY = {
        "high": 3,
        "medium": 2,
        "low": 1,
        "none": 0,
    }
    RISKY_KINDS = {
        "filesystem.move",
        "filesystem.rename",
        "filesystem.delete",
        "filesystem.copy",
        "filesystem.overwrite",
        "browser.send",
        "command.run",
    }
    TEXT_EXTENSIONS = {".txt", ".md", ".log"}
    SPREADSHEET_EXTENSIONS = {".et", ".xlsx", ".xls", ".csv"}
    SIDE_EFFECT_KINDS = {
        "filesystem.write_text",
        "spreadsheet.write_cell",
        "browser.send",
        "command.run",
    }

    def assess_memory_maintenance(self, report: Dict[str, Any], action_type: str) -> Dict[str, Any]:
        stats = dict(report.get("stats", {}) or {})
        quarantined = int(stats.get("quarantined", 0) or 0)
        rewritten = int(stats.get("rewritten", 0) or 0)
        scanned = int(stats.get("scanned", 0) or 0)

        outcome = "register"
        risk_level = "low"
        reasons = []

        if action_type == "memory.rebuild":
            outcome = "track_only"
            reasons.append("Memory rebuild changes persistent history and index files.")
        else:
            reasons.append("Memory rebuild preview is read-only.")

        if quarantined > 0:
            outcome = self._pick_outcome(outcome, "confirm")
            risk_level = self._pick_risk_level(risk_level, "high")
            reasons.append("Quarantined history entries require audit review.")
        elif rewritten > 0:
            outcome = self._pick_outcome(outcome, "track_only")
            risk_level = self._pick_risk_level(risk_level, "medium")
            reasons.append("Rewritten history entries should be reviewed after maintenance.")
        elif scanned > 0:
            risk_level = self._pick_risk_level(risk_level, "low")
            reasons.append("Maintenance completed without quarantined or rewritten entries.")
        else:
            risk_level = self._pick_risk_level(risk_level, "none")
            reasons.append("No persistent history entries were scanned.")

        return {
            "action_type": action_type,
            "outcome": outcome,
            "risk_level": risk_level,
            "reasons": reasons,
            "stats": stats,
        }

    def annotate(self, task_spec: TaskSpec, memory_agent=None) -> TaskSpec:
        rule_matches: List[Dict[str, Any]] = []
        similar_tasks: List[Dict[str, Any]] = []
        history_profile: Dict[str, Any] = {"matches": 0, "outcome_counts": {}}
        query_text = self._task_query_text(task_spec)
        operation_kinds = [operation.kind for operation in task_spec.operations]
        if memory_agent is not None:
            rule_matches = memory_agent.match_rules(task_spec)
            similar_tasks = memory_agent.search_history(
                query_text,
                required_operation_kinds=operation_kinds,
                min_overlap=2,
            )
            history_profile = memory_agent.history_outcome_profile(
                query_text,
                required_operation_kinds=operation_kinds,
                min_overlap=2,
            )
            task_spec.metadata["decision_rules"] = [
                {
                    "id": rule.get("id"),
                    "decision": rule.get("decision"),
                    "reason": rule.get("reason"),
                    "risk_level": rule.get("risk_level", "none"),
                }
                for rule in rule_matches
            ]
            task_spec.metadata["similar_tasks"] = [
                {
                    "timestamp": item.get("timestamp"),
                    "summary": item.get("summary"),
                    "status": item.get("status"),
                }
                for item in similar_tasks
            ]
            task_spec.metadata["history_profile"] = history_profile

        operation_decisions = []
        task_risk_level = "none"
        for operation in task_spec.operations:
            matched_rules = []
            outcome = "register"
            risk_level = "none"
            reasons = []

            if operation.kind in self.RISKY_KINDS:
                operation.risky = True
                outcome = self._pick_outcome(outcome, "confirm")
                risk_level = self._pick_risk_level(risk_level, "medium")
                reasons.append("Built-in risky operation kind.")
            for rule in rule_matches:
                if not self._rule_applies_to_operation(rule, task_spec.summary, operation.kind):
                    continue
                rule_kinds = set(rule.get("match", {}).get("operation_kinds", []))
                if operation.kind in rule_kinds and rule.get("decision") == "require_audit":
                    operation.risky = True
                matched_rules.append(rule.get("id"))
                rule_outcome = str(rule.get("outcome") or self._decision_to_outcome(rule.get("decision")))
                outcome = self._pick_outcome(outcome, rule_outcome)
                risk_level = self._pick_risk_level(risk_level, str(rule.get("risk_level", "none")))
                if rule.get("reason"):
                    reasons.append(str(rule.get("reason")))

            if operation.risky:
                outcome = self._pick_outcome(outcome, "confirm")
                risk_level = self._pick_risk_level(risk_level, "medium")

            outcome = self._apply_history_signal(outcome, history_profile)
            task_risk_level = self._pick_risk_level(task_risk_level, risk_level)

            operation_decisions.append(
                {
                    "operation_id": operation.id,
                    "kind": operation.kind,
                    "outcome": outcome,
                    "risk_level": risk_level,
                    "matched_rule_ids": matched_rules,
                    "reasons": reasons,
                }
            )

        task_spec.metadata["decision_summary"] = self._build_decision_summary(
            task_spec,
            rule_matches,
            similar_tasks,
            history_profile,
            operation_decisions,
            task_risk_level,
        )
        task_spec.metadata["operation_decisions"] = operation_decisions
        return task_spec

    def _build_decision_summary(
        self,
        task_spec: TaskSpec,
        rule_matches: List[Dict[str, Any]],
        similar_tasks: List[Dict[str, Any]],
        history_profile: Dict[str, Any],
        operation_decisions: List[Dict[str, Any]],
        task_risk_level: str,
    ) -> Dict[str, Any]:
        outcome_counter = Counter(item["outcome"] for item in operation_decisions)
        dominant_outcome = "register"
        for candidate in sorted(
            outcome_counter.keys(),
            key=lambda item: self.DECISION_PRIORITY.get(item, 0),
            reverse=True,
        ):
            dominant_outcome = candidate
            break

        return {
            "risky_operations": [operation.id for operation in task_spec.operations if operation.risky],
            "matched_rule_ids": [rule.get("id") for rule in rule_matches],
            "total_operations": len(task_spec.operations),
            "similar_task_count": len(similar_tasks),
            "history_match_count": history_profile.get("matches", 0),
            "dominant_outcome": dominant_outcome,
            "outcome_counts": dict(outcome_counter),
            "task_risk_level": task_risk_level,
        }

    def _decision_to_outcome(self, decision: Optional[str]) -> str:
        mapping = {
            "require_audit": "confirm",
            "track_only": "track_only",
            "standard_execute": "register",
            "risk_only": "risk",
        }
        return mapping.get(str(decision or ""), "register")

    def _pick_outcome(self, current: str, candidate: str) -> str:
        current_score = self.DECISION_PRIORITY.get(current, 0)
        candidate_score = self.DECISION_PRIORITY.get(candidate, 0)
        if candidate_score > current_score:
            return candidate
        return current

    def _pick_risk_level(self, current: str, candidate: str) -> str:
        current_score = self.RISK_LEVEL_PRIORITY.get(current, 0)
        candidate_score = self.RISK_LEVEL_PRIORITY.get(candidate, 0)
        if candidate_score > current_score:
            return candidate
        return current

    def _apply_history_signal(self, outcome: str, history_profile: Dict[str, Any]) -> str:
        outcome_counts = history_profile.get("outcome_counts", {})
        if not outcome_counts:
            return outcome
        if outcome in ("confirm", "risk"):
            return outcome
        if outcome_counts.get("confirm", 0) >= 2:
            return self._pick_outcome(outcome, "confirm")
        if outcome_counts.get("track_only", 0) >= 2:
            return self._pick_outcome(outcome, "track_only")
        return outcome

    def _task_query_text(self, task_spec: TaskSpec) -> str:
        parts = [
            str(getattr(task_spec, "summary", "") or ""),
            str(task_spec.metadata.get("original_instruction", "") or ""),
        ]
        return " ".join(part for part in parts if part).strip()

    def _rule_applies_to_operation(self, rule: Dict[str, Any], task_summary: str, operation_kind: str) -> bool:
        match = rule.get("match", {})
        operation_kinds = set(match.get("operation_kinds", []))
        task_keywords = [str(item).lower() for item in match.get("task_keywords", [])]
        summary_text = (task_summary or "").lower()

        kind_ok = not operation_kinds or operation_kind in operation_kinds
        keyword_ok = not task_keywords or any(keyword in summary_text for keyword in task_keywords)
        return kind_ok and keyword_ok

    def evaluate_result(
        self,
        instruction: str,
        task_spec: TaskSpec,
        execution_results: Dict[str, Any],
    ) -> Dict[str, Any]:
        """执行后评估：判断执行结果是否满足用户原始要求。

        Args:
            instruction: 用户原始指令
            task_spec: 任务规划
            execution_results: 各操作的执行结果 {op_id: output}

        Returns:
            {"satisfied": bool, "reason": str, "failed_operations": [str]}
        """
        mismatch_reason = self._detect_suffix_route_mismatch(instruction, task_spec)
        if mismatch_reason:
            return {
                "satisfied": False,
                "reason": mismatch_reason,
                "manual_takeover_required": False,
                "failed_operations": [],
            }
        spreadsheet_output_success = self._detect_spreadsheet_output_success(
            instruction,
            task_spec,
            execution_results,
        )
        if spreadsheet_output_success:
            return {
                "satisfied": True,
                "reason": spreadsheet_output_success,
                "manual_takeover_required": False,
                "failed_operations": [],
            }
        search_quality_reason = self._detect_unusable_search_results(task_spec, execution_results)
        if search_quality_reason:
            manual_takeover = search_quality_reason.startswith("MANUAL_TAKEOVER_REQUIRED::")
            return {
                "satisfied": False,
                "reason": search_quality_reason.replace("MANUAL_TAKEOVER_REQUIRED::", "", 1),
                "manual_takeover_required": manual_takeover,
                "failed_operations": [],
            }

        if self.provider is None:
            # 无 LLM 时，仅基于执行状态判断（有失败则不满足）
            failed = [
                op_id for op_id, result in execution_results.items()
                if isinstance(result, dict) and result.get("error")
            ]
            satisfied = len(failed) == 0
            return {
                "satisfied": satisfied,
                "reason": "无LLM，仅检查执行状态" if satisfied else f"操作失败: {failed}",
                "manual_takeover_required": False,
                "failed_operations": failed,
            }

        title_collection_success = self._detect_title_collection_success(
            instruction,
            task_spec,
            execution_results,
        )
        if title_collection_success:
            return {
                "satisfied": True,
                "reason": title_collection_success,
                "manual_takeover_required": False,
                "failed_operations": [],
            }

        # 构建评估 prompt
        results_summary = self._summarize_results(task_spec, execution_results)
        prompt = (
            "你是任务结果评估器。根据用户原始指令和执行结果，判断任务是否完成。\n"
            "评估标准：\n"
            "1. 用户要求的所有操作是否都成功执行\n"
            "2. 执行结果的内容是否有意义（不是占位符、错误信息或空值）\n"
            "3. 搜索结果是否包含实际数据（不是重复的链接标题）\n\n"
            f"用户指令：{instruction}\n\n"
            f"执行结果：\n{results_summary}\n\n"
            "返回JSON：{ \"satisfied\": boolean, \"reason\": \"评估理由\" }\n"
            "只返回JSON，不要其他内容。"
        )

        try:
            response = self.provider.call([{"role": "user", "content": prompt}])
            # 解析 JSON
            parsed = self._extract_json(response)
            if isinstance(parsed, dict):
                return {
                    "satisfied": bool(parsed.get("satisfied", False)),
                    "reason": str(parsed.get("reason", "")),
                    "manual_takeover_required": False,
                    "failed_operations": [],
                }
        except Exception as e:
            print(f"DecisionAgent 评估出错: {e}")

        # 降级：基于执行状态判断
        failed = [
            op_id for op_id, result in execution_results.items()
            if isinstance(result, dict) and result.get("error")
        ]
        return {
            "satisfied": len(failed) == 0,
            "reason": f"LLM评估失败，降级为状态检查。失败操作: {failed}" if failed else "LLM评估失败，但所有操作执行成功",
            "manual_takeover_required": False,
            "failed_operations": failed,
        }

    def _detect_title_collection_success(
        self,
        instruction: str,
        task_spec: TaskSpec,
        execution_results: Dict[str, Any],
    ) -> str:
        instruction_text = str(instruction or "")
        wants_titles = any(token in instruction_text.lower() for token in ["title", "titles"]) or ("标题" in instruction_text)
        if not wants_titles:
            return ""

        write_results: List[str] = []
        for operation in task_spec.operations:
            if operation.kind != "spreadsheet.write_cell":
                continue
            result = execution_results.get(operation.id)
            if not isinstance(result, dict) or result.get("error"):
                continue
            written_lines = result.get("written_lines", [])
            candidates = written_lines if isinstance(written_lines, list) and written_lines else str(
                result.get("text", "") or ""
            ).splitlines()
            for candidate in candidates:
                written_text = re.sub(r"^\s*\d+\s*[\.\、]\s*", "", str(candidate or "").strip())
                written_text = re.sub(r"^\s*[-*]\s*", "", written_text).strip()
                if not written_text:
                    continue
                if len(written_text) > 40:
                    continue
                write_results.append(written_text)

        unique_titles = []
        for item in write_results:
            if item not in unique_titles:
                unique_titles.append(item)

        target_count = 3
        digit_match = re.search(r"(\d+)\s*(?:个|篇|条|行)?", instruction_text)
        if digit_match:
            try:
                target_count = max(1, int(digit_match.group(1)))
            except Exception:
                target_count = 3
        elif re.search(r"[三3]", instruction_text):
            target_count = 3

        if len(unique_titles) < target_count:
            return ""
        return "已成功提取并写入至少三条作文标题。"

        search_outputs = []
        for operation in task_spec.operations:
            if operation.kind != "browser.search":
                continue
            result = execution_results.get(operation.id)
            if not isinstance(result, dict):
                continue
            text = str(result.get("text", "") or "").strip()
            if text:
                search_outputs.append(text)
        if not search_outputs:
            return ""

        combined_search_text = "\n".join(search_outputs)
        matched_titles = [title for title in unique_titles if title and title in combined_search_text]
        if len(matched_titles) >= 3:
            return "已成功提取并写入至少三条作文标题。"
        return ""

    def _detect_spreadsheet_output_success(
        self,
        instruction: str,
        task_spec: TaskSpec,
        execution_results: Dict[str, Any],
    ) -> str:
        has_search = any(operation.kind == "browser.search" for operation in task_spec.operations)
        if not has_search:
            return ""

        collected_items: List[str] = []
        write_success_count = 0
        for operation in task_spec.operations:
            if operation.kind != "spreadsheet.write_cell":
                continue
            result = execution_results.get(operation.id)
            if not isinstance(result, dict) or result.get("error"):
                continue
            write_success_count += 1

            written_lines = result.get("written_lines", [])
            if isinstance(written_lines, list) and written_lines:
                candidates = written_lines
            else:
                candidates = str(result.get("text", "") or "").splitlines()

            for candidate in candidates:
                normalized = self._normalize_output_item(candidate)
                if normalized:
                    collected_items.append(normalized)

        if write_success_count == 0:
            return ""

        unique_items: List[str] = []
        for item in collected_items:
            if item not in unique_items:
                unique_items.append(item)

        target_count = self._infer_ascii_target_count(instruction, default=3)
        if len(unique_items) >= target_count:
            return "Successfully wrote {} extracted items to the spreadsheet.".format(target_count)
        return ""

    @staticmethod
    def _normalize_output_item(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"^\s*\d+\s*[\.\)\-:]\s*", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 2 or len(text) > 80:
            return ""
        lower_text = text.lower()
        placeholder_patterns = [
            r"^title\s*\d*$",
            r"^essay\s*title\s*\d*$",
            r"^placeholder$",
            r"^todo$",
            r"^tbd$",
            r"^section\s*\d*$",
            r"^part\s*\d*$",
            r"^第?[一二三四五六七八九十0-9]+\s*篇?\s*(标题|题目)$",
            r"^第?[一二三四五六七八九十0-9]+\s*个?\s*(标题|题目)$",
            r"^(标题|题目)\s*[一二三四五六七八九十0-9]*$",
        ]
        for pattern in placeholder_patterns:
            if re.match(pattern, text, flags=re.IGNORECASE):
                return ""
        if "title placeholder" in lower_text or "essay title" == lower_text:
            return ""
        return text

    @staticmethod
    def _infer_ascii_target_count(instruction: str, default: int = 3) -> int:
        text = str(instruction or "")
        digit_match = re.search(r"(\d+)", text)
        if digit_match:
            try:
                return max(1, int(digit_match.group(1)))
            except Exception:
                return default
        lowered = text.lower()
        if "three" in lowered or "third" in lowered or "3rd" in lowered:
            return 3
        if re.search(r"[三3]", text):
            return 3
        if re.search(r"[二2]", text):
            return 2
        if re.search(r"[一1]", text):
            return 1
        return default

    def _summarize_results(self, task_spec: TaskSpec, execution_results: Dict[str, Any]) -> str:
        """Build a concise execution summary for decision evaluation."""
        lines = []
        for op in task_spec.operations:
            result = execution_results.get(op.id, {})
            if isinstance(result, dict):
                text = self._result_text_for_summary(op.kind, result)
                if len(text) > 200:
                    text = text[:200] + "..."
                lines.append(f"- {op.kind}({op.id}): {text}")
            else:
                lines.append(f"- {op.kind}({op.id}): {result}")
        return "\n".join(lines)

    def _result_text_for_summary(self, operation_kind: str, result: Dict[str, Any]) -> str:
        if operation_kind == "browser.search":
            text = str(result.get("text", "") or "").strip()
            useful = result.get("useful")
            reason = str(result.get("reason", "") or "").strip()
            if reason:
                return "{} (useful={}, reason={})".format(text, useful, reason)
            return "{} (useful={})".format(text, useful)
        return str(result.get("text", result.get("output", result)))

    def _detect_unusable_search_results(
        self,
        task_spec: TaskSpec,
        execution_results: Dict[str, Any],
    ) -> str:
        consumers_by_source: Dict[str, List[Any]] = {}
        for operation in task_spec.operations:
            source_id = str(operation.arguments.get("from_operation", "") or "").strip()
            if source_id:
                consumers_by_source.setdefault(source_id, []).append(operation)

        for operation in task_spec.operations:
            if operation.kind != "browser.search":
                continue
            result = execution_results.get(operation.id, {})
            if not isinstance(result, dict):
                continue
            if result.get("manual_takeover_required"):
                reason = str(result.get("reason", "") or "").strip() or "manual_takeover_required"
                query = str(result.get("query", operation.arguments.get("text", "")) or "").strip()
                return "MANUAL_TAKEOVER_REQUIRED::Browser search for '{}' requires human takeover: {}".format(
                    query,
                    reason,
                )
            if result.get("useful", True):
                continue
            consumers = consumers_by_source.get(operation.id, [])
            if consumers and self._all_consumers_completed(consumers, execution_results):
                # 搜索结果虽然一般，但已经被后续有副作用的操作消费完成；
                # 此时整单重跑通常只会重复写入/重复打开，不应由 decision 触发自动回滚式重试。
                continue
            if self._search_result_was_persisted(task_spec, execution_results, result):
                continue
            if self._has_nonempty_spreadsheet_output(execution_results):
                continue
            reason = str(result.get("reason", "") or "").strip()
            query = str(result.get("query", operation.arguments.get("text", "")) or "").strip()
            if reason:
                return "Browser search result for '{}' was not usable: {}".format(query, reason)
            return "Browser search result for '{}' was not usable.".format(query)
        return ""

    def _search_result_was_persisted(
        self,
        task_spec: TaskSpec,
        execution_results: Dict[str, Any],
        search_result: Dict[str, Any],
    ) -> bool:
        search_text = str(search_result.get("text", "") or "").strip()
        if not search_text:
            return False
        search_lines = [line.strip() for line in search_text.splitlines() if line.strip()]
        if not search_lines:
            return False

        for operation in task_spec.operations:
            if operation.kind != "spreadsheet.write_cell":
                continue
            result = execution_results.get(operation.id)
            if not isinstance(result, dict) or result.get("error"):
                continue

            written_lines = [str(line).strip() for line in result.get("written_lines", []) if str(line).strip()]
            if written_lines and written_lines == search_lines[:len(written_lines)]:
                return True

            written_text = str(result.get("text", "") or "").strip()
            if written_text and written_text == search_text:
                return True
        return False

    @staticmethod
    def _has_nonempty_spreadsheet_output(execution_results: Dict[str, Any]) -> bool:
        for result in execution_results.values():
            if not isinstance(result, dict) or result.get("error"):
                continue
            if "cell" not in result:
                continue
            written_lines = result.get("written_lines", [])
            if isinstance(written_lines, list) and any(str(line).strip() for line in written_lines):
                return True
            text = str(result.get("text", "") or "").strip()
            if text:
                return True
        return False

    @staticmethod
    def _all_consumers_completed(consumers: List[Any], execution_results: Dict[str, Any]) -> bool:
        side_effect_consumers = [
            operation for operation in consumers
            if getattr(operation, "kind", "") in DecisionAgent.SIDE_EFFECT_KINDS
        ]
        if not side_effect_consumers:
            return False
        for operation in side_effect_consumers:
            result = execution_results.get(operation.id)
            if not isinstance(result, dict):
                return False
            if result.get("error"):
                return False
            if not result:
                return False
        return True

    def _detect_suffix_route_mismatch(self, instruction: str, task_spec: TaskSpec) -> str:
        suffix = self._infer_primary_suffix(instruction)
        if not suffix:
            return ""

        operation_kinds = [operation.kind for operation in task_spec.operations]
        if suffix in self.TEXT_EXTENSIONS and any(kind.startswith("spreadsheet.") for kind in operation_kinds):
            return "计划把文本文件按表格文件处理了，需要改走文本文件路径。"
        if suffix in self.SPREADSHEET_EXTENSIONS and any(
            kind in {"filesystem.write_text"} for kind in operation_kinds
        ):
            return "计划把表格文件按纯文本文件处理了，需要改走表格路径。"
        return ""

    def _infer_primary_suffix(self, instruction: str) -> str:
        match = re.search(r"([^\s\"']+\.(txt|md|log|et|xlsx|xls|csv))", str(instruction or ""), flags=re.IGNORECASE)
        if not match:
            return ""
        return "." + str(match.group(2) or "").lower()

    @staticmethod
    def _extract_json(text: str) -> Any:
        """从文本中提取 JSON。"""
        text = text.strip()
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 尝试从 markdown 代码块中提取
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        # 尝试找到第一个 { ... }
        match = re.search(r'\{[^{}]*"satisfied"[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {}
