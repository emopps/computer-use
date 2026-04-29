from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from os_computer_use.runtime.event_schema import build_legacy_event_payload
from os_computer_use.runtime.event_schema import is_execution_event_payload


class MemoryAgent:
    def __init__(self, memory_root: str, session_dir: str):
        self.memory_root = Path(memory_root)
        self.memory_root.mkdir(parents=True, exist_ok=True)
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.persistent_dir = self.memory_root / "persistent"
        self.history_dir = self.persistent_dir / "history"
        self.preferences_dir = self.persistent_dir / "preferences"
        self.index_dir = self.persistent_dir / "index"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.preferences_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.events_path = self.session_dir / "execution_trace.jsonl"
        self.task_state_path = self.session_dir / "task_state.json"
        self.session_history_path = self.session_dir / "task_history.jsonl"
        self.global_history_path = self.history_dir / "task_history.jsonl"
        self.history_index_path = self.index_dir / "history_index.jsonl"
        self.preferences_path = self.preferences_dir / "preferences.json"
        self.memory_manifest_path = self.session_dir / "memory_manifest.json"
        self.rules_path = Path(__file__).resolve().parent.parent / "knowledge" / "rules.json"
        self._ensure_session_files()
        self._write_manifest()

    def record(self, event_type: str, payload: Dict[str, Any]) -> None:
        safe_payload = self._make_json_safe(payload)
        if is_execution_event_payload(safe_payload):
            stored_payload = safe_payload
        else:
            stored_payload = build_legacy_event_payload(
                legacy_event_type=event_type,
                payload=safe_payload,
            )
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "payload": stored_payload,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def record_task_state(self, status: str, payload: Dict[str, Any]) -> None:
        state = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "payload": self._make_json_safe(payload),
        }
        with self.task_state_path.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        self.record("task_state", state)

    def append_history(self, payload: Dict[str, Any]) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": self._make_json_safe(payload),
        }
        with self.session_history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        with self.global_history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._append_history_index(event)

    def search_history(
        self,
        query: str,
        limit: int = 3,
        required_operation_kinds: List[str] = None,
        min_overlap: int = 2,
    ) -> List[Dict[str, Any]]:
        if not query or not self.history_index_path.exists():
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        required_kinds = set(required_operation_kinds or [])
        ranked = []
        with self.history_index_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item_kinds = set(item.get("operation_kinds", []))
                if required_kinds and not item_kinds.intersection(required_kinds):
                    continue
                item_tokens = set(item.get("tokens", []))
                overlap = len(query_tokens.intersection(item_tokens))
                if overlap < min_overlap:
                    continue
                denominator = max(len(query_tokens), 1)
                score = overlap / denominator
                ranked.append((score, overlap, item))

        ranked.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
        return [item for _, _, item in ranked[:limit]]

    def history_outcome_profile(
        self,
        query: str,
        limit: int = 10,
        required_operation_kinds: List[str] = None,
        min_overlap: int = 2,
    ) -> Dict[str, Any]:
        items = self.search_history(
            query,
            limit=limit,
            required_operation_kinds=required_operation_kinds,
            min_overlap=min_overlap,
        )
        outcome_counts: Dict[str, int] = {}
        for item in items:
            outcome = str(item.get("decision_outcome") or "unknown")
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        return {
            "matches": len(items),
            "outcome_counts": outcome_counts,
        }

    def load_rules(self) -> List[Dict[str, Any]]:
        if not self.rules_path.exists():
            return []
        with self.rules_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return list(payload.get("rules", []))

    def match_rules(self, task_spec) -> List[Dict[str, Any]]:
        operation_kinds = {operation.kind for operation in task_spec.operations}
        summary_text = " ".join(
            part
            for part in [
                str(getattr(task_spec, "summary", "") or ""),
                str(getattr(task_spec, "metadata", {}).get("original_instruction", "") or ""),
            ]
            if part
        ).lower()
        matches = []
        for rule in self.load_rules():
            match = rule.get("match", {})
            rule_kinds = set(match.get("operation_kinds", []))
            task_keywords = [str(item).lower() for item in match.get("task_keywords", [])]
            kind_ok = not rule_kinds or bool(operation_kinds.intersection(rule_kinds))
            keyword_ok = not task_keywords or any(keyword in summary_text for keyword in task_keywords)
            if kind_ok and keyword_ok:
                matches.append(rule)
        return matches

    def remember_preference(self, key: str, value: Any) -> None:
        preferences = self.recall_preferences()
        preferences[str(key)] = self._make_json_safe(value)
        with self.preferences_path.open("w", encoding="utf-8") as handle:
            json.dump(preferences, handle, ensure_ascii=False, indent=2)

    def recall_preferences(self) -> Dict[str, Any]:
        if not self.preferences_path.exists():
            return {}
        with self.preferences_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def preview_persistent_history_rebuild(self) -> Dict[str, Any]:
        valid_events = []
        quarantined_events = []
        rewritten_events = []
        stats = {
            "scanned": 0,
            "kept": 0,
            "quarantined": 0,
            "rewritten": 0,
        }

        for event in self._read_jsonl(self.global_history_path):
            stats["scanned"] += 1
            normalized = self._normalize_history_event(event)
            if normalized is None:
                quarantined_events.append(
                    {
                        "timestamp": event.get("timestamp"),
                        "reason": "malformed_or_unrecoverable",
                        "event": self._make_json_safe(event),
                    }
                )
                continue

            clean_event = normalized["event"]
            if normalized["quarantined"]:
                quarantined_events.append(
                    {
                        "timestamp": clean_event.get("timestamp"),
                        "reason": normalized["reason"],
                        "event": clean_event,
                    }
                )
                continue

            if normalized["rewritten"]:
                rewritten_events.append(
                    {
                        "timestamp": clean_event.get("timestamp"),
                        "reason": normalized["reason"],
                        "event": clean_event,
                    }
                )
                stats["rewritten"] += 1

            valid_events.append(clean_event)
            stats["kept"] += 1

        stats["quarantined"] = len(quarantined_events)
        maintenance_decision = self._maintenance_decision(stats)
        return {
            "valid_events": valid_events,
            "quarantined_events": quarantined_events,
            "rewritten_events": rewritten_events,
            "stats": stats,
            "maintenance_risk_level": maintenance_decision["risk_level"],
            "audit_recommendation": maintenance_decision["audit_recommendation"],
        }

    def rebuild_persistent_history(self) -> Dict[str, Any]:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        cleanup_dir = self.persistent_dir / "cleanup"
        cleanup_dir.mkdir(parents=True, exist_ok=True)
        preview = self.preview_persistent_history_rebuild()
        valid_events = preview["valid_events"]
        quarantined_events = preview["quarantined_events"]
        rewritten_events = preview["rewritten_events"]
        stats = preview["stats"]
        maintenance_risk_level = preview["maintenance_risk_level"]
        audit_recommendation = preview["audit_recommendation"]

        history_backup_path = cleanup_dir / "task_history.pre_rebuild_{}.jsonl".format(timestamp)
        index_backup_path = cleanup_dir / "history_index.pre_rebuild_{}.jsonl".format(timestamp)
        quarantine_path = cleanup_dir / "task_history.quarantine_{}.jsonl".format(timestamp)
        rewrite_path = cleanup_dir / "task_history.rewritten_{}.jsonl".format(timestamp)
        report_path = cleanup_dir / "rebuild_report_{}.json".format(timestamp)

        self._backup_if_exists(self.global_history_path, history_backup_path)
        self._backup_if_exists(self.history_index_path, index_backup_path)
        self._write_jsonl(self.global_history_path, valid_events)
        self._rebuild_history_index(valid_events)
        self._write_jsonl(quarantine_path, quarantined_events)
        self._write_jsonl(rewrite_path, rewritten_events)

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "history_path": str(self.global_history_path),
            "history_index_path": str(self.history_index_path),
            "backup_history_path": str(history_backup_path),
            "backup_index_path": str(index_backup_path),
            "quarantine_path": str(quarantine_path),
            "rewrite_path": str(rewrite_path),
            "stats": stats,
            "maintenance_risk_level": maintenance_risk_level,
            "audit_recommendation": audit_recommendation,
        }
        with report_path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        report["report_path"] = str(report_path)
        return report

    def preview_persistent_history_report(self) -> Dict[str, Any]:
        preview = self.preview_persistent_history_rebuild()
        quarantined_sample = [
            {
                "timestamp": item.get("timestamp"),
                "reason": item.get("reason"),
                "instruction": item.get("event", {}).get("payload", {}).get("instruction", ""),
            }
            for item in preview["quarantined_events"][:5]
        ]
        rewritten_sample = [
            {
                "timestamp": item.get("timestamp"),
                "reason": item.get("reason"),
                "instruction": item.get("event", {}).get("payload", {}).get("instruction", ""),
                "decision_outcome": item.get("event", {}).get("payload", {}).get("decision_outcome", ""),
            }
            for item in preview["rewritten_events"][:5]
        ]
        return {
            "history_path": str(self.global_history_path),
            "history_index_path": str(self.history_index_path),
            "stats": preview["stats"],
            "maintenance_risk_level": preview["maintenance_risk_level"],
            "audit_recommendation": preview["audit_recommendation"],
            "quarantined_sample": quarantined_sample,
            "rewritten_sample": rewritten_sample,
        }

    def _maintenance_decision(self, stats: Dict[str, int]) -> Dict[str, str]:
        quarantined = int(stats.get("quarantined", 0) or 0)
        rewritten = int(stats.get("rewritten", 0) or 0)
        if quarantined > 0:
            return {
                "risk_level": "high",
                "audit_recommendation": "review_required",
            }
        if rewritten > 0:
            return {
                "risk_level": "medium",
                "audit_recommendation": "review_recommended",
            }
        return {
            "risk_level": "low",
            "audit_recommendation": "record_only",
        }

    def _write_manifest(self) -> None:
        manifest = {
            "session_dir": str(self.session_dir),
            "persistent_dir": str(self.persistent_dir),
            "execution_trace_schema": {
                "primary_family": "execution",
                "primary_schema_version": 1,
                "legacy_family": "legacy_compat",
                "legacy_schema_version": 0,
            },
            "files": {
                "events": str(self.events_path),
                "task_state": str(self.task_state_path),
                "session_history": str(self.session_history_path),
                "global_history": str(self.global_history_path),
                "history_index": str(self.history_index_path),
                "preferences": str(self.preferences_path),
                "rules": str(self.rules_path),
            },
        }
        with self.memory_manifest_path.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)

    def _ensure_session_files(self) -> None:
        for path in [self.events_path, self.session_history_path]:
            if not path.exists():
                path.write_text("", encoding="utf-8")

    def _append_history_index(self, event: Dict[str, Any]) -> None:
        payload = event.get("payload", {})
        instruction = str(payload.get("instruction", "") or "")
        summary = str(payload.get("summary", "") or "")
        status = str(payload.get("status", "") or "")
        decision_outcome = str(payload.get("decision_outcome", "") or "")
        task_risk_level = str(payload.get("task_risk_level", "") or "")
        matched_rule_ids = " ".join(str(item) for item in payload.get("matched_rule_ids", []) or [])
        operation_kinds = " ".join(str(item) for item in payload.get("operation_kinds", []) or [])
        text = " ".join(
            part
            for part in [instruction, summary, status, decision_outcome, task_risk_level, matched_rule_ids, operation_kinds]
            if part
        ).strip()
        tokens = sorted(self._tokenize(text))
        index_entry = {
            "timestamp": event.get("timestamp"),
            "instruction": instruction,
            "summary": summary,
            "status": status,
            "decision_outcome": decision_outcome,
            "task_risk_level": task_risk_level,
            "matched_rule_ids": list(payload.get("matched_rule_ids", []) or []),
            "operation_kinds": list(payload.get("operation_kinds", []) or []),
            "tokens": tokens,
        }
        with self.history_index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(index_entry, ensure_ascii=False) + "\n")

    def _rebuild_history_index(self, events: List[Dict[str, Any]]) -> None:
        with self.history_index_path.open("w", encoding="utf-8") as handle:
            handle.write("")
        for event in events:
            self._append_history_index(event)

    def _normalize_history_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(event, dict):
            return None
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None

        normalized_event = {
            "timestamp": str(event.get("timestamp") or datetime.now(timezone.utc).isoformat()),
            "payload": self._make_json_safe(payload),
        }
        payload = normalized_event["payload"]

        for key in ("instruction", "summary", "status", "decision_outcome", "task_risk_level"):
            if key in payload and payload.get(key) is not None:
                payload[key] = str(payload.get(key))

        if self._looks_garbled(str(payload.get("instruction", ""))) or self._looks_garbled(
            str(payload.get("summary", ""))
        ):
            return {
                "event": normalized_event,
                "quarantined": True,
                "rewritten": False,
                "reason": "garbled_text",
            }

        rewrite_reason = None
        normalized_outcome = self._normalized_decision_outcome(payload)
        if normalized_outcome is not None and normalized_outcome != payload.get("decision_outcome"):
            payload["decision_outcome"] = normalized_outcome
            rewrite_reason = "decision_outcome_aligned_with_rules"

        return {
            "event": normalized_event,
            "quarantined": False,
            "rewritten": rewrite_reason is not None,
            "reason": rewrite_reason,
        }

    def _normalized_decision_outcome(self, payload: Dict[str, Any]) -> Optional[str]:
        matched_rule_ids = list(payload.get("matched_rule_ids", []) or [])
        if not matched_rule_ids:
            return payload.get("decision_outcome")

        rules_by_id = {rule.get("id"): rule for rule in self.load_rules()}
        outcome = "register"
        for rule_id in matched_rule_ids:
            rule = rules_by_id.get(rule_id)
            if not rule:
                continue
            candidate = str(rule.get("outcome") or self._decision_to_outcome(rule.get("decision")))
            outcome = self._pick_outcome(outcome, candidate)
        return outcome

    def _decision_to_outcome(self, decision: Optional[str]) -> str:
        mapping = {
            "require_audit": "confirm",
            "track_only": "track_only",
            "standard_execute": "register",
            "risk_only": "risk",
        }
        return mapping.get(str(decision or ""), "register")

    def _pick_outcome(self, current: str, candidate: str) -> str:
        priority = {
            "risk": 4,
            "confirm": 3,
            "track_only": 2,
            "register": 1,
        }
        if priority.get(candidate, 0) > priority.get(current, 0):
            return candidate
        return current

    def _looks_garbled(self, text: str) -> bool:
        if not text:
            return False
        question_marks = text.count("?")
        if question_marks < 3:
            return False
        visible_chars = [char for char in text if not char.isspace()]
        if not visible_chars:
            return False
        return question_marks / len(visible_chars) >= 0.3

    def _read_jsonl(self, path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        items = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    items.append({"raw_line": line})
        return items

    def _write_jsonl(self, path: Path, items: List[Dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _backup_if_exists(self, source: Path, target: Path) -> None:
        if not source.exists():
            return
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    def _tokenize(self, text: str) -> set:
        lowered = (text or "").lower()
        ascii_tokens = set(re.findall(r"[a-z0-9_./:-]+", lowered))
        cjk_tokens = set()
        for phrase in re.findall(r"[\u4e00-\u9fff]+", lowered):
            cjk_tokens.add(phrase)
            cjk_tokens.update(char for char in phrase)
            if len(phrase) >= 2:
                cjk_tokens.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
        return ascii_tokens.union(cjk_tokens)

    def _make_json_safe(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): self._make_json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._make_json_safe(item) for item in value]
        return str(value)

    def summarize_session(self, instruction: str, evaluation: Dict[str, Any]) -> None:
        """任务完成后总结记忆：记录指令、执行结果、经验教训。"""
        summary = {
            "instruction": instruction,
            "status": evaluation.get("satisfied", False) and "success" or "failed",
            "reason": evaluation.get("reason", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # 写入会话总结文件
        summary_path = self.session_dir / "session_summary.json"
        existing = []
        if summary_path.exists():
            try:
                existing = json.loads(summary_path.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except (json.JSONDecodeError, OSError):
                existing = []
        existing.append(summary)
        summary_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 同时记录到执行轨迹
        self.record("session_summary", summary)
