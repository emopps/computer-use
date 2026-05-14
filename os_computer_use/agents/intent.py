from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from os_computer_use.logging import logger
from os_computer_use.runtime.capabilities import is_supported_operation_kind, supported_kinds_text
from os_computer_use.runtime.task_models import OperationSpec, TaskModelError, TaskSpec


class TaskPlanningError(RuntimeError):
    pass


class TaskClarificationRequired(TaskPlanningError):
    def __init__(self, question: str):
        super().__init__(question)
        self.question = str(question)


class IntentAgent:
    TEXT_EXTENSIONS = {".txt", ".md", ".log", ".csv"}
    SPREADSHEET_EXTENSIONS = {".et", ".xlsx", ".xls"}

    def __init__(self, provider: Optional[Any] = None):
        self.provider = provider

    def normalize(self, instruction: str) -> str:
        self._maybe_request_clarification(instruction)
        normalized = self._normalize_intent_with_model(instruction)
        return self._restore_explicit_email_mappings(instruction, normalized)

    def _normalize_intent_with_model(self, instruction: str) -> str:
        if self.provider is None:
            return instruction
        
        prompt = (
            "You are the Intent Agent for a desktop computer-use system.\n"
            "Normalize the user request into a clear, concise, and executable instruction.\n"
            "Remove conversational filler, but keep all technical details (file names, paths, application names).\n"
            "Preserve explicit identifiers exactly as written, including URLs, file paths, email addresses, and name=email mappings.\n"
            "Do not guess, correct, merge, deduplicate, or reinterpret any explicit email address or name=email mapping.\n"
            "If the user provides two different assignee lines, keep both lines and keep each email exactly as written, even if they are identical.\n"
            "If the user is Chinese, output the result in Chinese.\n"
            "Return only the normalized string. No JSON, no markdown.\n"
            "User instruction: {}\n"
            "Normalized:".format(instruction)
        )
        
        response = self.provider.call([{"role": "user", "content": prompt}])
        return response.strip()

    @staticmethod
    def _extract_explicit_email_mappings(text: str) -> List[tuple[str, str]]:
        mappings: List[tuple[str, str]] = []
        pattern = re.compile(
            r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_\-路]{0,40})\s*[=:：]\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
        )
        for line in str(text or "").splitlines():
            for name, email in pattern.findall(line):
                mappings.append((str(name).strip(), str(email).strip()))
        return mappings

    @classmethod
    def _restore_explicit_email_mappings(cls, original_instruction: str, normalized_instruction: str) -> str:
        original = str(original_instruction or "").strip()
        normalized = str(normalized_instruction or "").strip()
        explicit_mappings = cls._extract_explicit_email_mappings(original)
        if not explicit_mappings or not normalized:
            return normalized or original

        original_emails = [email for _, email in explicit_mappings]
        normalized_emails = re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", normalized)
        if normalized_emails == original_emails:
            return normalized

        original_mapping_lines = [
            f"{name}={email}" for name, email in explicit_mappings
        ]
        normalized_without_mapping_lines = []
        mapping_line_pattern = re.compile(
            r"^\s*[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_\-路]{0,40}\s*[=:：]\s*[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\s*$"
        )
        for line in normalized.splitlines():
            if mapping_line_pattern.match(line.strip()):
                continue
            normalized_without_mapping_lines.append(line.rstrip())
        normalized_body = "\n".join(line for line in normalized_without_mapping_lines if line.strip()).strip()
        if not normalized_body:
            return original
        return normalized_body + "\n" + "\n".join(original_mapping_lines)

    def replan(self, instruction: str, failed_task_spec: TaskSpec, failure_message: str) -> str:
        # 重新规划时返回规范化意图给 Planner 使用
        self._maybe_request_clarification(
            instruction,
            previous_error=failure_message,
            previous_plan=self._task_spec_to_dict(failed_task_spec),
        )
        return instruction # 重新规划时使用原始上下文

    def _maybe_request_clarification(
        self,
        instruction: str,
        previous_error: Optional[str] = None,
        previous_plan: Optional[Dict[str, Any]] = None,
    ) -> None:
        text = (instruction or "").strip()
        if not text:
            raise TaskPlanningError("User instruction cannot be empty.")

        # 仅检查复合任务（创建+重命名）等真正需要用户输入的情况
        compound_question = self._build_compound_task_clarification_question(text)
        if compound_question:
            raise TaskClarificationRequired(compound_question)

        if self._requires_assignment_email_mapping(text):
            raise TaskClarificationRequired(
                "请补充任务负责人的邮箱映射，格式如 张三=zhangsan@example.com；李四=lisi@example.com。"
            )

        # 让 planner 自然处理缺失参数：
        # - spreadsheet.open：自动生成默认路径
        # - 其他操作：planner 的 _validate_plan_params 处理依据检查

    @staticmethod
    def _requires_assignment_email_mapping(instruction: str) -> bool:
        lowered = str(instruction or "").lower()
        meeting_tokens = ["会议", "纪要", "转写", "摘要", "发言"]
        send_tokens = ["邮件", "邮箱", "发邮件", "发送", "任务分配", "assign"]
        has_meeting = any(token in instruction for token in meeting_tokens)
        has_send = any(token in instruction for token in send_tokens) or any(
            token in lowered for token in ["email", "mail", "send"]
        )
        if not (has_meeting and has_send):
            return False
        return re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", instruction) is None

    def _build_compound_task_clarification_question(self, instruction: str) -> Optional[str]:
        if not self._looks_like_create_text_then_rename(instruction):
            return None

        text_info = self._infer_text_file_from_instruction(instruction)
        rename_info = self._infer_rename_from_instruction(instruction)
        missing: List[str] = []
        if not text_info.get("file_path"):
            missing.append("file_path")
        if not rename_info.get("new_name"):
            missing.append("new_name")

        if not missing:
            return None

        if self.provider is not None:
            prompt = (
                "You are the Intent Agent for a desktop computer-use system.\n"
                "The user described a multi-step task: create a new text file, write content, then rename that newly created file.\n"
                "Ask exactly one short clarification question in Chinese.\n"
                "Return JSON only with this schema:\n"
                '{{ "question": string }}\n'
                "Rules:\n"
                "1. Ask only about the missing information.\n"
                "2. Do NOT ask for the original file/folder path unless the user explicitly mentioned an existing file or folder.\n"
                "3. Treat the rename target as the file created earlier in the same request.\n"
                "4. Keep the question concise and specific.\n"
                "User instruction:\n{instruction}\n"
                "Known information:\n{text_info}\n{rename_info}\n"
                "Missing fields:\n{missing}\n"
            ).format(
                instruction=instruction,
                text_info=json.dumps(text_info, ensure_ascii=False),
                rename_info=json.dumps(rename_info, ensure_ascii=False),
                missing=json.dumps(missing, ensure_ascii=False),
            )
            for _ in range(2):
                response = self.provider.call([{"role": "user", "content": prompt}])
                try:
                    parsed = self._extract_json_value(response)
                except TaskPlanningError:
                    continue
                if isinstance(parsed, dict):
                    question = str(parsed.get("question", "") or "").strip()
                    if question:
                        return question

        if missing == ["file_path", "new_name"]:
            return "你想新建的 txt 文件叫什么名字？重命名后要叫什么名字？"
        if missing == ["file_path"]:
            return "你想新建的 txt 文件叫什么名字？"
        if missing == ["new_name"]:
            return "这个 txt 文件重命名后要叫什么名字？"
        return None

    def _looks_like_create_text_then_rename(self, instruction: str) -> bool:
        text = instruction or ""
        lowered = text.lower()
        has_rename = bool(re.search(r"(\u91cd\u547d\u540d|rename)", text, flags=re.IGNORECASE))
        has_create = bool(
            re.search("(\u65b0\u5efa|\u521b\u5efa).*(txt|\u6587\u672c|text)", text, flags=re.IGNORECASE)
        )
        has_rename = bool(re.search("(\u91cd\u547d\u540d|rename)", text, flags=re.IGNORECASE))
        has_write = bool(re.search("(\u5199\u5165|\u8f93\u5165)", text, flags=re.IGNORECASE))
        has_sequence = any(token in text for token in ["\u7136\u540e", "\u518d", "\u63a5\u7740", "\u5e76", "\u5e76\u4e14"])
        return has_rename and ((has_create and has_write) or (has_create and has_sequence) or (has_write and has_sequence) or ("txt" in lowered and has_write))

    # -- Layer 1: LLM parameter extraction --

    _TASK_TYPE_MAP = {
        "rename": "filesystem.rename",
        "重命名": "filesystem.rename",
        "delete": "filesystem.delete",
        "删除": "filesystem.delete",
        "move": "filesystem.move",
        "移动": "filesystem.move",
        "copy": "filesystem.copy",
        "复制": "filesystem.copy",
        "create_folder": "filesystem.create_folder",
        "新建文件夹": "filesystem.create_folder",
        "创建文件夹": "filesystem.create_folder",
        "open_path": "filesystem.open_path",
        "打开": "filesystem.open_path",
        "write_text": "filesystem.write_text",
        "新建文件": "filesystem.write_text",
        "send_email": "browser.send",
        "发邮件": "browser.send",
        "发送邮件": "browser.send",
        "search": "browser.search",
        "搜索": "browser.search",
        "open_browser": "browser.open",
        "打开浏览器": "browser.open",
        "spreadsheet_open": "spreadsheet.open",
        "打开表格": "spreadsheet.open",
        "打开WPS": "spreadsheet.open",
        "spreadsheet_write": "spreadsheet.write_cell",
        "写入表格": "spreadsheet.write_cell",
        "填写单元格": "spreadsheet.write_cell",
    }

    def _extract_intent_params_with_model(self, instruction: str) -> Optional[Dict[str, Any]]:
        if self.provider is None:
            return None

        prompt = (
            "You are the Intent Agent for a desktop computer-use system.\n"
            "Extract the task type and key parameters from the user instruction.\n"
            "Return JSON only. No markdown. No prose.\n"
            "Schema:\n"
            "{{\n"
            '  "task_type": string,\n'
            '  "params": {{\n'
            '    "src": string | null,\n'
            '    "dst_dir": string | null,\n'
            '    "new_name": string | null,\n'
            '    "path": string | null,\n'
            '    "file_path": string | null,\n'
            '    "text": string | null,\n'
            '    "url": string | null,\n'
            '    "to": string | null,\n'
            '    "body": string | null\n'
            '  }}\n'
            "}}\n"
            "Rules:\n"
            "1. task_type must be one of: rename, delete, move, copy, create_folder, open_path, write_text, send_email, search, open_browser, spreadsheet_open, spreadsheet_write.\n"
            "2. Only extract values that are EXPLICITLY stated in the instruction.\n"
            "3. If a value is not stated, set it to null. Do NOT invent or guess values.\n"
            "4. For rename, src is the old file path, new_name is the new filename.\n"
            "5. For move/copy, src is the source, dst_dir is the destination folder.\n"
            "6. For spreadsheet_open, file_path is the path to the excel/wps file.\n"
            "7. For spreadsheet_write, cell is the position (e.g. 'A1'), text is the content.\n"
            "User instruction:\n"
            "{}\n".format(instruction)
        )

        for _ in range(2):
            response = self.provider.call([{"role": "user", "content": prompt}])
            try:
                parsed = self._extract_json_value(response)
            except TaskPlanningError:
                continue
            if isinstance(parsed, dict) and parsed.get("task_type"):
                return parsed
        return None

    # -- Layer 2: Deterministic missing param check --

    _REQUIRED_PARAMS = {
        "filesystem.rename": ["src", "new_name"],
        "filesystem.delete": ["path"],
        "filesystem.move": ["src", "dst_dir"],
        "filesystem.copy": ["src", "dst_dir"],
        "filesystem.create_folder": ["path"],
        "filesystem.open_path": ["path"],
        "filesystem.write_text": ["file_path"],
        "browser.open": ["url"],
        "browser.search": ["text"],
        "browser.send": ["to", "body"],
        "spreadsheet.open": [],
        "spreadsheet.write_cell": ["cell", "text"],
    }

    def _deterministic_check_missing(self, task_type: str, extracted: Dict[str, Any], instruction: str) -> List[str]:
        # 将中文/通用任务类型映射到操作类型
        kind = self._TASK_TYPE_MAP.get(task_type, task_type)
        required = self._REQUIRED_PARAMS.get(kind)
        if not required:
            return []

        # 检查指令是否包含补充信息
        supplement = ""
        if "补充说明：" in instruction:
            supplement = instruction.split("补充说明：", 1)[-1].strip()

        missing: List[str] = []
        for field in required:
            value = extracted.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                # 即使 LLM 未提取，也检查指令文本是否包含
                if field == "src" and ("src" not in extracted or not extracted.get("src")):
                    # 尝试用正则在指令中查找路径
                    path_match = re.search(r"(/[\w/.\-\u4e00-\u9fff]+\.[\w]+|[~/][\w/.\-\u4e00-\u9fff]+)", instruction)
                    if path_match:
                        extracted["src"] = path_match.group(1)
                        continue
                if field == "path" and ("path" not in extracted or not extracted.get("path")):
                    path_match = re.search(r"(/[\w/.\-\u4e00-\u9fff]+\.[\w]+|[~/][\w/.\-\u4e00-\u9fff]+)", instruction)
                    if path_match:
                        extracted["path"] = path_match.group(1)
                        continue
                if supplement:
                    continue
                missing.append(field)
        return missing

    # -- Layer 3: Generate clarification question --

    _FIELD_DISPLAY = {
        "src": "源文件路径",
        "dst_dir": "目标文件夹",
        "new_name": "新文件名",
        "path": "目标路径",
        "file_path": "文件路径",
        "text": "内容",
        "url": "网址",
        "to": "收件人",
        "body": "正文内容",
        "cell": "单元格位置(如A1)",
    }

    def _build_clarification_question(self, instruction: str, task_type: str, missing: List[str], extracted: Dict[str, Any]) -> str:
        # 优先尝试 LLM 生成的问题
        if self.provider is not None:
            kind = self._TASK_TYPE_MAP.get(task_type, task_type)
            prompt = (
                "You are the Intent Agent for a desktop computer-use system.\n"
                "The user wants to: {kind}\n"
                "But the following required info is missing: {missing}\n"
                "Already known: {known}\n"
                "Write ONE short, specific clarification question in Chinese.\n"
                "Return JSON: {{ \"question\": string }}\n"
                "Do NOT ask for info already provided.\n"
            ).format(
                kind=kind,
                missing=", ".join(missing),
                known=json.dumps(extracted, ensure_ascii=False),
            )
            for _ in range(2):
                response = self.provider.call([{"role": "user", "content": prompt}])
                try:
                    parsed = self._extract_json_value(response)
                except TaskPlanningError:
                    continue
                if isinstance(parsed, dict):
                    question = str(parsed.get("question", "") or "").strip()
                    if question:
                        return question

        # 降级：确定性生成问题
        readable = "、".join(self._FIELD_DISPLAY.get(f, f) for f in missing)
        return "还缺少关键信息：{}。请补充后我再继续。".format(readable)

    def _plan_with_model(
        self,
        instruction: str,
        previous_error: Optional[str] = None,
        previous_plan: Optional[Dict[str, Any]] = None,
    ) -> TaskSpec:
        text = (instruction or "").strip()
        if not text:
            raise TaskPlanningError("User instruction cannot be empty.")
        if self.provider is None:
            raise TaskPlanningError("IntentAgent requires a configured language model provider.")

        planning_errors: List[str] = []
        for attempt in range(3):
            prompt = self._build_planning_prompt(
                text,
                previous_error=planning_errors[-1] if planning_errors else previous_error,
                previous_plan=previous_plan,
            )
            response = self.provider.call([{"role": "user", "content": prompt}])
            logger.log(
                "Planner raw output (attempt {}): {}".format(attempt + 1, self._truncate_text(response)),
                "gray",
            )
            try:
                return self._parse_and_normalize_plan(text, response)
            except TaskClarificationRequired:
                raise
            except TaskPlanningError as exc:
                planning_errors.append(str(exc))

        deterministic = self._build_deterministic_task_spec(text)
        if deterministic is not None:
            logger.log(
                "Planner fallback activated after model planning failures: {}".format(planning_errors[-1]),
                "yellow",
            )
            return deterministic

        raise TaskPlanningError(
            "IntentAgent failed to produce a valid task spec after retries: {}".format(
                " | ".join(planning_errors)
            )
        )

    def _build_planning_prompt(
        self,
        instruction: str,
        previous_error: Optional[str],
        previous_plan: Optional[Dict[str, Any]],
    ) -> str:
        blocks = [
            "You are the Intent Agent for a desktop computer-use system.\n",
            "Convert the user instruction into a valid JSON task plan.\n",
            "Return JSON only. No markdown. No prose.\n",
            "Use only supported operation kinds: {}\n".format(supported_kinds_text()),
            "Required schema:\n",
            "{\n",
            '  "summary": string,\n',
            '  "success_criteria": [string, ...],\n',
            '  "metadata": object,\n',
            '  "operations": [\n',
            "    {\n",
            '      "id": string,\n',
            '      "kind": string,\n',
            '      "description": string,\n',
            '      "arguments": object,\n',
            '      "depends_on": [string, ...],\n',
            '      "risky": boolean\n',
            "    }\n",
            "  ]\n",
            "}\n",
            "Rules:\n",
            "1. Do not output any unsupported kind.\n",
            "2. Use arguments, not params.\n",
            "3. Use depends_on, not dependencies.\n",
            "4. Do not invent paths or business data.\n",
            "5. For spreadsheet.open use file_name when only the file name is known.\n",
            "6. For spreadsheet.write_cell include both cell and text.\n",
            "7. For filesystem.write_text include file_path and text.\n",
            "8. If the previous error says a kind is unsupported, replace it with supported kinds.\n",
            "9. Preserve the user's original language in text fields whenever possible.\n",
            "10. For browser.search, keep the search text semantically faithful to the user's request. Do not translate it unless the user asked for translation.\n",
            "Example A:\n",
            json.dumps(
                {
                    "summary": "打开浏览器并搜索杭州天气",
                    "success_criteria": ["浏览器成功打开并完成杭州天气搜索"],
                    "metadata": {"source": "model_plan"},
                    "operations": [
                        {
                            "id": "open_browser",
                            "kind": "browser.open",
                            "description": "打开浏览器首页",
                            "arguments": {"url": "https://www.baidu.com"},
                            "depends_on": [],
                            "risky": False,
                        },
                        {
                            "id": "search_weather",
                            "kind": "browser.search",
                            "description": "搜索杭州天气",
                            "arguments": {"text": "杭州天气"},
                            "depends_on": ["open_browser"],
                            "risky": False,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            "\nExample B:\n",
            json.dumps(
                {
                    "summary": "Create a text file named 1.txt on the desktop",
                    "success_criteria": ["The text file is created successfully"],
                    "metadata": {"source": "model_plan"},
                    "operations": [
                        {
                            "id": "create_text_file",
                            "kind": "filesystem.write_text",
                            "description": "Create the requested text file",
                            "arguments": {"file_path": "~/Desktop/1.txt", "text": ""},
                            "depends_on": [],
                            "risky": False,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            "\n",
        ]
        if previous_plan:
            blocks.append("Previous plan:\n")
            blocks.append(json.dumps(previous_plan, ensure_ascii=False))
            blocks.append("\n")
        if previous_error:
            blocks.append("Previous error:\n")
            blocks.append(previous_error)
            blocks.append("\n")
        blocks.append("User instruction:\n")
        blocks.append(instruction)
        return "".join(blocks)

    def _parse_and_normalize_plan(self, instruction: str, raw_response: str) -> TaskSpec:
        parsed = self._extract_json_value(raw_response)
        normalized = self._normalize_model_payload(instruction, parsed)
        try:
            return TaskSpec.from_dict(normalized)
        except TaskModelError as exc:
            raise TaskPlanningError("Normalized plan is invalid: {}".format(exc))

    def _extract_json_value(self, raw: str) -> Any:
        decoder = json.JSONDecoder()
        for index, char in enumerate(raw):
            if char not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(raw, index)
            except json.JSONDecodeError:
                continue
            if isinstance(value, (dict, list)):
                return value
        raise TaskPlanningError("Failed to parse JSON task spec from model output: {}".format(raw[:500]))

    def _normalize_model_payload(self, instruction: str, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, list):
            payload = {
                "summary": instruction,
                "success_criteria": ["Complete the user request"],
                "metadata": {"source": "model_plan"},
                "operations": payload,
            }
        if not isinstance(payload, dict):
            raise TaskPlanningError("Planner output must be a JSON object or array.")

        operations = payload.get("operations", payload.get("tasks", payload.get("steps", [])))
        if not isinstance(operations, list):
            raise TaskPlanningError("Planner output is missing a valid operations list.")

        normalized_operations = []
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise TaskPlanningError("Operation at index {} is not an object.".format(index))
            normalized_operations.append(self._normalize_operation(instruction, operation, index))

        metadata = dict(payload.get("metadata", {"source": "model_plan"}))
        metadata.setdefault("original_instruction", instruction)

        return {
            "summary": str(payload.get("summary", instruction)).strip(),
            "success_criteria": self._normalize_success_criteria(payload.get("success_criteria")),
            "metadata": metadata,
            "operations": normalized_operations,
        }

    def _normalize_operation(self, instruction: str, operation: Dict[str, Any], index: int) -> Dict[str, Any]:
        operation_id = operation.get("id", operation.get("node_id", "step_{}".format(index + 1)))
        kind = operation.get("kind", operation.get("action", ""))
        arguments = operation.get("arguments", operation.get("params", {}))
        depends_on = operation.get("depends_on", operation.get("dependencies", []))
        description = operation.get("description", operation.get("title", kind or operation_id))
        risky = bool(operation.get("risky", False))

        kind = self._normalize_operation_kind(str(kind))
        if not is_supported_operation_kind(kind):
            raise TaskPlanningError("Unsupported operation kind in plan: {}".format(kind))

        arguments = self._normalize_operation_arguments(instruction, kind, arguments)
        kind, arguments, description = self._rewrite_open_operation_by_suffix(
            instruction=instruction,
            kind=kind,
            arguments=arguments,
            description=str(description),
        )
        kind, arguments, description = self._rewrite_text_file_operations(
            instruction=instruction,
            kind=kind,
            arguments=arguments,
            description=str(description),
        )
        self._validate_operation_arguments_for_safety(instruction, kind, arguments)
        depends_on = [str(item) for item in depends_on] if isinstance(depends_on, list) else []

        return {
            "id": str(operation_id),
            "kind": kind,
            "description": str(description),
            "arguments": arguments,
            "depends_on": depends_on,
            "risky": risky,
        }

    def _normalize_operation_kind(self, kind: str) -> str:
        aliases = {
            "open_browser": "browser.open",
            "browser_search": "browser.search",
            "send_email": "browser.send",
            "browser_send": "browser.send",
            "open_wps_file": "spreadsheet.open",
            "wps_input_cell": "spreadsheet.write_cell",
            "input_cell": "spreadsheet.write_cell",
            "run_command": "command.run",
            "run_shell": "command.run",
            "open_path": "filesystem.open_path",
            "create_folder": "filesystem.create_folder",
            "mkdir": "filesystem.create_folder",
            "copy_file": "filesystem.copy",
            "create_text_file": "filesystem.write_text",
            "write_text_file": "filesystem.write_text",
            "document.create": "filesystem.write_text",
            "delete_file": "filesystem.delete",
            "remove_file": "filesystem.delete",
        }
        return aliases.get(kind, kind)

    def _normalize_operation_arguments(self, instruction: str, kind: str, arguments: Any) -> Dict[str, Any]:
        normalized = dict(arguments) if isinstance(arguments, dict) else {}

        if kind == "browser.open":
            candidate_url = ""
            for key in ("url", "link", "href", "address", "target"):
                if normalized.get(key):
                    candidate_url = str(normalized[key])
                    break
            extracted_url = self._extract_first_url(candidate_url) or self._extract_first_url(instruction)
            if extracted_url:
                normalized["url"] = extracted_url

        if kind == "meeting.extract_actions":
            candidate_url = ""
            for key in ("source_url", "url", "link", "href"):
                if normalized.get(key):
                    candidate_url = str(normalized[key])
                    break
            extracted_url = self._extract_first_url(candidate_url) or self._extract_first_url(instruction)
            if extracted_url:
                normalized["source_url"] = extracted_url

        if kind == "browser.search" and "text" not in normalized:
            for key in ("query", "keyword", "search_text"):
                if normalized.get(key):
                    normalized["text"] = str(normalized[key])
                    break
        if kind == "browser.search":
            inferred_search = self._infer_browser_search_query(instruction)
            if inferred_search:
                normalized["text"] = inferred_search

        if kind == "browser.send":
            if "to" not in normalized:
                for key in ("recipient", "email", "receiver"):
                    if normalized.get(key):
                        normalized["to"] = str(normalized[key])
                        break
            if "subject" not in normalized and normalized.get("title"):
                normalized["subject"] = str(normalized["title"])
            if "body" not in normalized and normalized.get("content"):
                normalized["body"] = str(normalized["content"])
            attachments = normalized.get("attachments")
            if attachments is None and normalized.get("attachment"):
                normalized["attachments"] = [str(normalized["attachment"])]

        if kind == "spreadsheet.open":
            if "file_path" not in normalized and "file_name" not in normalized:
                for key in ("path", "filename", "name"):
                    if normalized.get(key):
                        normalized["file_name"] = str(normalized[key])
                        break
            inferred = self._infer_spreadsheet_write_from_instruction(instruction)
            if "file_name" not in normalized and inferred.get("file_name"):
                normalized["file_name"] = inferred["file_name"]

        if kind == "spreadsheet.write_cell":
            if "cell" not in normalized:
                for key in ("cell_address", "target_cell", "address"):
                    if normalized.get(key):
                        normalized["cell"] = str(normalized[key]).upper()
                        break
            if "text" not in normalized and normalized.get("value") is not None:
                normalized["text"] = str(normalized["value"])
            if "file_path" not in normalized and "file_name" not in normalized:
                for key in ("path", "filename", "name"):
                    if normalized.get(key):
                        normalized["file_name"] = str(normalized[key])
                        break
            inferred = self._infer_spreadsheet_write_from_instruction(instruction)
            if "cell" not in normalized and inferred.get("cell"):
                normalized["cell"] = inferred["cell"]
            if "text" not in normalized and inferred.get("text") is not None:
                normalized["text"] = inferred["text"]
            if "file_name" not in normalized and inferred.get("file_name"):
                normalized["file_name"] = inferred["file_name"]

        if kind == "filesystem.write_text":
            inferred = self._infer_text_file_from_instruction(instruction)
            if "file_path" not in normalized:
                for key in ("path", "file_name", "filename", "name"):
                    if normalized.get(key):
                        normalized["file_path"] = self._normalize_text_file_path(str(normalized[key]))
                        break
            if "file_path" not in normalized and inferred.get("file_path"):
                normalized["file_path"] = inferred["file_path"]
            if "text" not in normalized:
                normalized["text"] = inferred.get("text", "")

        if kind == "filesystem.create_folder":
            inferred = self._infer_folder_creation_from_instruction(instruction)
            if "path" not in normalized:
                for key in ("folder_path", "directory", "dir", "name"):
                    if normalized.get(key):
                        normalized["path"] = self._normalize_desktop_path_value(str(normalized[key]))
                        break
            if "path" not in normalized and inferred.get("folder_name"):
                normalized["path"] = self._normalize_desktop_path_value(inferred["folder_name"])

        if kind in {"filesystem.copy", "filesystem.move"}:
            if "src" not in normalized:
                for key in ("source", "file_path", "path", "name"):
                    if normalized.get(key):
                        normalized["src"] = self._normalize_desktop_path_value(str(normalized[key]))
                        break
            if "dst_dir" not in normalized:
                for key in ("destination", "dst", "target_dir", "directory"):
                    if normalized.get(key):
                        normalized["dst_dir"] = self._normalize_desktop_path_value(str(normalized[key]))
                        break

        if kind == "filesystem.rename":
            if "src" not in normalized:
                for key in ("old_path", "source", "source_path", "file_path", "path", "old_name", "name"):
                    if normalized.get(key):
                        normalized["src"] = self._normalize_desktop_path_value(str(normalized[key]))
                        break
            if "new_name" not in normalized:
                for key in ("new_path", "target_path", "destination_path", "target_name", "rename_to"):
                    if normalized.get(key):
                        value = str(normalized[key]).strip()
                        if "/" in value or "\\" in value or value.startswith(("~", ".")):
                            normalized["new_name"] = os.path.basename(value)
                        else:
                            normalized["new_name"] = value
                        break
            inferred = self._infer_rename_from_instruction(instruction)
            if "src" not in normalized and inferred.get("src"):
                normalized["src"] = inferred["src"]
            if "new_name" not in normalized and inferred.get("new_name"):
                normalized["new_name"] = inferred["new_name"]

        if kind == "filesystem.delete":
            if "path" not in normalized:
                for key in ("target", "file_path", "src", "name"):
                    if normalized.get(key):
                        normalized["path"] = self._normalize_desktop_path_value(str(normalized[key]))
                        break

        if kind == "filesystem.open_path":
            if "path" not in normalized:
                for key in ("target", "directory", "dir", "name"):
                    if normalized.get(key):
                        normalized["path"] = self._normalize_desktop_path_value(str(normalized[key]))
                        break

        return normalized

    def _rewrite_text_file_operations(
        self,
        *,
        instruction: str,
        kind: str,
        arguments: Dict[str, Any],
        description: str,
    ) -> (str, Dict[str, Any], str):
        candidate = str(
            arguments.get("file_path")
            or arguments.get("file_name")
            or arguments.get("path")
            or arguments.get("name")
            or ""
        ).strip()
        lowered_instruction = str(instruction or "").lower()
        is_text_file = candidate.lower().endswith(".txt") or ".txt" in lowered_instruction
        if not is_text_file:
            return kind, arguments, description

        inferred_text = self._infer_text_file_from_instruction(instruction)
        wants_write = any(token in instruction for token in ["输入", "写入", "内容", "保存"]) or any(
            token in lowered_instruction for token in ["input", "write", "save", "edit"]
        )

        if kind in {"spreadsheet.open", "spreadsheet.write_cell"}:
            if wants_write:
                file_path = candidate or inferred_text.get("file_path", "")
                if file_path:
                    file_path = self._normalize_text_file_path(file_path)
                rewritten = {
                    "file_path": file_path or inferred_text.get("file_path", ""),
                    "text": str(arguments.get("text") or inferred_text.get("text", "") or ""),
                }
                return "filesystem.write_text", rewritten, "Create or overwrite the requested text file."

            file_path = candidate or inferred_text.get("file_path", "")
            if file_path:
                file_path = self._normalize_text_file_path(file_path)
            return (
                "filesystem.open_path",
                {"path": file_path or inferred_text.get("file_path", "")},
                "Open the requested text file.",
            )

        return kind, arguments, description

    def _rewrite_open_operation_by_suffix(
        self,
        *,
        instruction: str,
        kind: str,
        arguments: Dict[str, Any],
        description: str,
    ) -> (str, Dict[str, Any], str):
        candidate = str(
            arguments.get("file_path")
            or arguments.get("file_name")
            or arguments.get("path")
            or arguments.get("name")
            or ""
        ).strip()
        inferred_open = self._infer_open_path_from_instruction(instruction)
        if not candidate and inferred_open.get("path"):
            candidate = str(inferred_open["path"]).strip()
        if not candidate:
            return kind, arguments, description

        suffix = self._path_suffix(candidate)
        if not suffix:
            return kind, arguments, description

        lowered_instruction = str(instruction or "").lower()
        wants_write = any(token in instruction for token in ["输入", "写入", "保存", "内容"]) or any(
            token in lowered_instruction for token in ["input", "write", "save", "edit"]
        )

        if suffix in self.SPREADSHEET_EXTENSIONS:
            if kind == "filesystem.open_path":
                rewritten = {"file_path": candidate}
                return "spreadsheet.open", rewritten, "Open the requested spreadsheet."
            return kind, arguments, description

        if suffix in self.TEXT_EXTENSIONS:
            if wants_write and kind in {"filesystem.open_path", "spreadsheet.open", "spreadsheet.write_cell"}:
                inferred_text = self._infer_text_file_from_instruction(instruction)
                rewritten = {
                    "file_path": self._normalize_text_file_path(candidate),
                    "text": str(arguments.get("text") or inferred_text.get("text", "") or ""),
                }
                return "filesystem.write_text", rewritten, "Create or overwrite the requested text file."
            if kind == "spreadsheet.open":
                return (
                    "filesystem.open_path",
                    {"path": self._normalize_text_file_path(candidate)},
                    "Open the requested text file.",
                )

        return kind, arguments, description

    @classmethod
    def _path_suffix(cls, path_value: str) -> str:
        _, suffix = os.path.splitext(str(path_value or "").strip())
        return suffix.lower()

    def _validate_operation_arguments_for_safety(self, instruction: str, kind: str, arguments: Dict[str, Any]) -> None:
        missing = self._find_missing_required_arguments(kind, arguments)
        if missing:
            raise TaskClarificationRequired(
                self._build_missing_argument_question(instruction, kind, missing, arguments)
            )
        return
        supplement = ""
        if "补充说明：" in instruction:
            supplement = instruction.split("补充说明：", 1)[-1].strip()
        if kind == "filesystem.create_folder" and not arguments.get("path"):
            if not supplement:
                raise TaskClarificationRequired("你想创建的新文件夹叫什么名字，放在哪个位置？")
        if kind == "filesystem.copy":
            if not arguments.get("src"):
                raise TaskClarificationRequired("你想复制哪个文件或文件夹？")
            if not arguments.get("dst_dir"):
                if not supplement:
                    raise TaskClarificationRequired("你想把它复制到哪里？")
        if kind == "filesystem.move":
            if not arguments.get("src"):
                raise TaskClarificationRequired("你想移动哪个文件或文件夹？")
            if not arguments.get("dst_dir"):
                if not supplement:
                    raise TaskClarificationRequired("你想把它移动到哪里？")
        if kind == "filesystem.rename":
            if not arguments.get("src"):
                raise TaskClarificationRequired("你想重命名哪个文件或文件夹？")
            if not arguments.get("new_name"):
                if not supplement:
                    raise TaskClarificationRequired("你想把它重命名成什么名字？")
        if kind == "filesystem.delete" and not arguments.get("path"):
            if not supplement:
                raise TaskClarificationRequired("你想删除哪个具体文件或文件夹？")
        if kind == "filesystem.open_path" and not arguments.get("path"):
            raise TaskClarificationRequired("你想打开哪个文件或文件夹？")
        if kind == "browser.send":
            if not arguments.get("to"):
                raise TaskClarificationRequired("你想发送给谁？请补充收件人。")
            if not arguments.get("body"):
                raise TaskClarificationRequired("邮件正文内容是什么？")

    def _find_missing_required_arguments(self, kind: str, arguments: Dict[str, Any]) -> List[str]:
        required_by_kind = {
            "filesystem.create_folder": ["path"],
            "filesystem.copy": ["src", "dst_dir"],
            "filesystem.move": ["src", "dst_dir"],
            "filesystem.rename": ["src", "new_name"],
            "filesystem.delete": ["path"],
            "filesystem.open_path": ["path"],
            "filesystem.write_text": ["file_path"],
            "browser.open": ["url"],
            "browser.search": ["text"],
            "browser.send": ["to", "body"],
            "spreadsheet.open": [],
            "spreadsheet.write_cell": ["cell", "text"],
            "command.run": ["command"],
        }
        missing: List[str] = []
        for field in required_by_kind.get(kind, []):
            if kind == "spreadsheet.open" and field in {"file_path", "file_name"}:
                if arguments.get("file_path") or arguments.get("file_name"):
                    continue
            value = arguments.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(field)
        return missing

    def _build_missing_argument_question(
        self,
        instruction: str,
        kind: str,
        missing: List[str],
        arguments: Dict[str, Any],
    ) -> str:
        if self.provider is not None:
            prompt = (
                "You are the Intent Agent for a desktop computer-use system.\n"
                "A draft operation is missing required arguments.\n"
                "Write exactly one short clarification question for the user.\n"
                "Return JSON only with this schema:\n"
                '{{ "question": string }}\n'
                "Rules:\n"
                "1. Ask only about the missing information.\n"
                "2. Keep the question concise and specific.\n"
                "3. Do not ask for information already present.\n"
                "User instruction:\n{instruction}\n"
                "Operation kind:\n{kind}\n"
                "Current arguments:\n{arguments}\n"
                "Missing fields:\n{missing}\n"
            ).format(
                instruction=instruction,
                kind=kind,
                arguments=json.dumps(arguments, ensure_ascii=False),
                missing=json.dumps(missing, ensure_ascii=False),
            )
            for _ in range(2):
                response = self.provider.call([{"role": "user", "content": prompt}])
                try:
                    parsed = self._extract_json_value(response)
                except TaskPlanningError:
                    continue
                if isinstance(parsed, dict):
                    question = str(parsed.get("question", "") or "").strip()
                    if question:
                        return question

        field_names = {
            "path": "目标路径",
            "src": "源文件或文件夹",
            "dst_dir": "目标文件夹",
            "new_name": "新名字",
            "file_path": "文件路径",
            "file_name": "文件名",
            "to": "收件人",
            "body": "正文内容",
            "url": "网址",
            "text": "要输入的内容",
            "cell": "单元格位置",
            "command": "命令内容",
        }
        readable = "、".join(field_names.get(item, item) for item in missing)
        return "还缺少这些关键信息：{}。请补充后我再继续。".format(readable)

    def _infer_browser_search_query(self, instruction: str) -> Optional[str]:
        text = (instruction or "").strip()
        if not text:
            return None

        for marker in ["\u641c\u7d22", "\u67e5\u8be2", "\u67e5\u627e"]:
            idx = text.find(marker)
            if idx >= 0:
                query = text[idx + len(marker):].strip()
                if query.startswith("\uff1a") or query.startswith(":"):
                    query = query[1:].strip()
                query = re.sub(r"^[\s:\uff1a]+", "", query)
                query = re.sub(r"[,\uff0c\u3002.!?\uff1f]+$", "", query).strip()
                if query:
                    return query

        patterns = [
            "(?:\u6253\u5f00\u6d4f\u89c8\u5668\\s*)?(?:\u641c\u7d22|\u67e5\u8be2|\u67e5\u627e)\\s*(?P<query>.+)$",
            r"(?:open\s+(?:the\s+)?.*browser.*search(?:\s+for)?)\s*(?P<query>.+)$",
            r"(?:search\s+for|search)\s*(?P<query>.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            query = str(match.group("query") or "").strip(" \t\r\n,，。.!?？")
            if query:
                return query
        return None

    def _infer_browser_search_from_instruction(self, instruction: str) -> Optional[str]:
        text = (instruction or "").strip()
        if not text:
            return None

        patterns = [
            r"(?:打开浏览器)?(?:搜索|查询|查找)\s*(?P<query>.+)$",
            r"(?:open .* browser .* search(?: for)?)\s*(?P<query>.+)$",
            r"(?:search for|search)\s*(?P<query>.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            query = str(match.group("query") or "").strip(" \t\r\n,，。.!?？")
            if query:
                return query
        return None

    def _infer_spreadsheet_write_from_instruction(self, instruction: str) -> Dict[str, str]:
        inferred: Dict[str, str] = {}
        file_match = re.search(r"([^\s]+\.et)", instruction, flags=re.IGNORECASE)
        if file_match:
            inferred["file_name"] = file_match.group(1).strip()
        write_match = re.search(
            r"(?:\u8f93\u5165|\u5199\u5165)\s*(?P<text>.+?)\s*\u5230\s*(?P<cell>[A-Za-z]+\d+)\u683c",
            instruction,
            flags=re.IGNORECASE,
        )
        if write_match:
            inferred["text"] = write_match.group("text").strip()
            inferred["cell"] = write_match.group("cell").upper().strip()
        return inferred

    def _infer_text_file_from_instruction(self, instruction: str) -> Dict[str, str]:
        inferred: Dict[str, str] = {}
        explicit_path = self._extract_explicit_path(instruction, extensions=("txt", "md", "log"))
        if explicit_path:
            inferred["file_path"] = self._normalize_text_file_path(explicit_path)
        else:
            file_match = re.search(
                r"(?:\u540d\u4e3a|\u53eb|named)\s*([^\s]+\.txt)",
                instruction,
                flags=re.IGNORECASE,
            )
            if not file_match:
                file_match = re.search(r"([^\s]+\.txt)", instruction, flags=re.IGNORECASE)
            if file_match:
                inferred["file_path"] = self._normalize_text_file_path(file_match.group(1).strip())
        content_match = re.search(
            r"(?:\u5185\u5bb9\u4e3a|\u5199\u5165\u5185\u5bb9|\u5185\u5bb9\u662f)\s*[\"']?(.+?)[\"']?$",
            instruction,
        )
        if not content_match:
            content_match = re.search(
                "(?:\u5199\u5165|\u8f93\u5165)\s*[\"']?(.+?)[\"']?(?:\s*(?:\u7136\u540e|\u518d|\u63a5\u7740|\u5e76|\u5e76\u4e14)|[,.，。]|$)",
                instruction,
            )
        if content_match:
            inferred["text"] = content_match.group(1).strip()
        return inferred

    def _infer_folder_creation_from_instruction(self, instruction: str) -> Dict[str, str]:
        inferred: Dict[str, str] = {}
        patterns = [
            r"new folder named\s+([^\s.,]+)",
            r"folder named\s+([^\s.,]+)",
            r"名为\s*([^\s，。]+)\s*的文件夹",
            r"创建一个名为\s*([^\s，。]+)\s*的文件夹",
            r"创建一个新文件夹\s*([^\s，。]+)",
            r"创建新文件夹\s*([^\s，。]+)",
            r"新建一个文件夹\s*([^\s，。]+)",
            r"新建文件夹\s*([^\s，。]+)",
            r"创建文件夹\s*([^\s，。]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match:
                inferred["folder_name"] = match.group(1).strip().strip("\"'")
                break
        return inferred

    def _infer_open_path_from_instruction(self, instruction: str) -> Dict[str, str]:
        inferred: Dict[str, str] = {}
        explicit_url = self._extract_first_url(instruction)
        if explicit_url:
            inferred["path"] = explicit_url
            return inferred
        explicit_path = self._extract_explicit_path(instruction)
        if explicit_path:
            inferred["path"] = self._normalize_desktop_path_value(explicit_path)
            return inferred
        patterns = [
            r"(?:打开|open)\s*([^\s，。]+(?:/[^，。\s]+)*)",
            r"(~/[^\s，。]+)",
            r"(/[^，。\s]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match:
                inferred["path"] = self._normalize_desktop_path_value(match.group(1).strip())
                break
        return inferred

    def _extract_explicit_path(
        self,
        instruction: str,
        extensions: Optional[tuple[str, ...]] = None,
    ) -> str:
        text = str(instruction or "").strip()
        if not text:
            return ""

        ext_suffix = r"(?:\.[A-Za-z0-9_]+)"
        if extensions:
            ext_suffix = r"(?:\.(?:{}))".format("|".join(re.escape(item) for item in extensions))

        verb_boundary = (
            r"(?:\u5e76|\u7136\u540e|\u518d|\u63a5\u7740|"
            r"\u8f93\u5165|\u5199\u5165|\u4fdd\u5b58|\u4fee\u6539|\u7f16\u8f91|"
            r"input|write|save|edit)"
        )
        patterns = [
            r"((?:/|~/)[^\s,\uFF0C\u3002\uFF1B;\"']+?{})(?=(?:{}|\s|$))".format(ext_suffix, verb_boundary),
            r"((?:/|~/)[^\s,\uFF0C\u3002\uFF1B;\"']+)(?=(?:{}|\s|$))".format(verb_boundary),
        ]
        if extensions:
            patterns.append(
                r"([^\s,\uFF0C\u3002\uFF1B;\"']+{})(?=(?:{}|\s|$))".format(ext_suffix, verb_boundary)
            )

        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip().rstrip("。，“”\"'")
        return ""

    @staticmethod
    def _extract_first_url(text: str) -> str:
        payload = str(text or "").strip()
        if not payload:
            return ""
        match = re.search(r"https?://[^\s\u3000,\uFF0C\u3002\uFF1B;\"'<>]+", payload, flags=re.IGNORECASE)
        if not match:
            return ""
        return match.group(0).rstrip("，。；;\"'》〉】）)")

    def _infer_delete_target_from_instruction(self, instruction: str) -> Dict[str, str]:
        inferred: Dict[str, str] = {}
        patterns = [
            r"(?:删除|移除|清空|delete)\s*([^\s，。]+(?:\.[^\s，。]+)?)",
            r"补充说明：\s*([^\s，。]+(?:\.[^\s，。]+)?)",
            r"([~/./\\\\][^\s，。]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match:
                inferred["path"] = self._normalize_desktop_path_value(match.group(1).strip())
                break
        return inferred

    def _infer_rename_from_instruction(self, instruction: str) -> Dict[str, str]:
        inferred: Dict[str, str] = {}
        source_match = re.search(
            r"(?:重命名|rename)\s*([^\s，。]+(?:\.[^\s，。]+)?)",
            instruction,
            flags=re.IGNORECASE,
        )
        if source_match:
            inferred["src"] = self._normalize_desktop_path_value(source_match.group(1).strip())
        rename_match = re.search(
            r"(?:改成|命名为|为|rename .* to|叫)\s*([^\s，。]+(?:\.[^\s，。]+)?)",
            instruction,
            flags=re.IGNORECASE,
        )
        if rename_match:
            inferred["new_name"] = rename_match.group(1).strip()
        if not inferred.get("new_name") and "补充说明：" in instruction:
            inferred["new_name"] = instruction.split("补充说明：", 1)[-1].strip().split()[0]
        return inferred

    def _normalize_text_file_path(self, path_value: str) -> str:
        path_value = path_value.strip()
        if path_value.startswith(("~", "/", ".")):
            return path_value
        return "~/\u684c\u9762/{}".format(path_value)

    def _normalize_desktop_path_value(self, path_value: str) -> str:
        path_value = (path_value or "").strip().strip("\"'")
        if path_value.startswith(("~", "/", ".")):
            return path_value
        return "~/\u684c\u9762/{}".format(path_value)

    def _normalize_success_criteria(self, value: Any) -> List[str]:
        if isinstance(value, list):
            criteria = [str(item) for item in value if str(item).strip()]
            if criteria:
                return criteria
        return ["Complete the user request"]

    def _build_deterministic_task_spec(self, instruction: str) -> Optional[TaskSpec]:
        text = instruction.strip()
        lowered = text.lower()
        operations: List[OperationSpec] = []

        url = self._extract_first_url(text)
        if url:
            operations.append(
                OperationSpec(
                    id="open_browser",
                    kind="browser.open",
                    description="Open the requested URL in the browser.",
                    arguments={"url": url},
                )
            )
            return TaskSpec(
                summary=text,
                success_criteria=["Open the requested web page."],
                operations=operations,
                metadata={"source": "deterministic", "original_instruction": instruction},
            )

        search_patterns = [
            r"\u641c\u7d22(?P<query>.+)",
            r"\u67e5\u627e(?P<query>.+)",
            r"\u67e5\u8be2(?P<query>.+)",
            r"search for (?P<query>.+)",
            r"search (?P<query>.+)",
        ]
        search_query = None
        for pattern in search_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                search_query = match.group("query").strip(" ,.?")
                break

        browser_keywords = ["\u6d4f\u89c8\u5668", "browser", "\u7f51\u9875", "\u7f51\u7ad9", "web"]
        if search_query and any(
            keyword in lowered
            for keyword in browser_keywords + ["\u641c\u7d22", "search", "\u67e5\u8be2", "\u67e5\u627e"]
        ):
            operations.append(
                OperationSpec(
                    id="open_browser",
                    kind="browser.open",
                    description="Open the browser home page for web search.",
                    arguments={"url": "https://www.baidu.com"},
                )
            )
            operations.append(
                OperationSpec(
                    id="search_browser",
                    kind="browser.search",
                    description="Search the requested query in the browser.",
                    arguments={"text": search_query},
                    depends_on=["open_browser"],
                )
            )
            return TaskSpec(
                summary=text,
                success_criteria=["Open the browser and perform the requested search."],
                operations=operations,
                metadata={"source": "deterministic", "original_instruction": instruction},
            )

        refresh_desktop = any(
            phrase in lowered
            for phrase in ["刷新桌面", "刷新一下桌面", "刷新当前桌面", "refresh desktop", "desktop refresh"]
        ) or (lowered.strip() == "refresh" and any(token in lowered for token in ["桌面", "desktop"]))
        if refresh_desktop and any(token in lowered for token in ["桌面", "desktop", "refresh"]):
            operations.append(
                OperationSpec(
                    id="refresh_desktop",
                    kind="command.run",
                    description="Refresh the desktop shell view.",
                    arguments={"command": "xdotool key F5"},
                    risky=True,
                )
            )
            return TaskSpec(
                summary=text,
                success_criteria=["Refresh the current desktop successfully."],
                operations=operations,
                metadata={"source": "deterministic", "original_instruction": instruction},
            )

        command_match = re.search(
            r"(?:\u8fd0\u884c\u547d\u4ee4|\u6267\u884c\u547d\u4ee4|run command)\s*(?P<command>.+)",
            text,
            flags=re.IGNORECASE,
        )
        if command_match:
            operations.append(
                OperationSpec(
                    id="run_command",
                    kind="command.run",
                    description="Run the requested shell command.",
                    arguments={"command": command_match.group("command").strip()},
                )
            )
            return TaskSpec(
                summary=text,
                success_criteria=["Run the requested command successfully."],
                operations=operations,
                metadata={"source": "deterministic", "original_instruction": instruction},
            )

        spreadsheet_match = re.search(
            r"\u6253\u5f00(?P<file>[^\s]+\.et).*?(?:\u8f93\u5165|\u5199\u5165)(?P<value>.+?)\u5230(?P<cell>[A-Za-z]+\d+)\u683c",
            text,
            flags=re.IGNORECASE,
        )
        if spreadsheet_match:
            file_name = spreadsheet_match.group("file").strip()
            cell = spreadsheet_match.group("cell").upper().strip()
            value = spreadsheet_match.group("value").strip()
            operations.append(
                OperationSpec(
                    id="open_spreadsheet",
                    kind="spreadsheet.open",
                    description="Open the requested spreadsheet.",
                    arguments={"file_name": file_name},
                )
            )
            operations.append(
                OperationSpec(
                    id="write_spreadsheet_cell",
                    kind="spreadsheet.write_cell",
                    description="Write the requested text into the target cell.",
                    arguments={"file_name": file_name, "cell": cell, "text": value},
                    depends_on=["open_spreadsheet"],
                )
            )
            return TaskSpec(
                summary=text,
                success_criteria=["Open the spreadsheet and write the requested cell value."],
                operations=operations,
                metadata={"source": "deterministic", "original_instruction": instruction},
            )

        spreadsheet_path_match = re.search(r"([^\s]+?\.(?:et|xlsx|xls))", text, flags=re.IGNORECASE)
        if spreadsheet_path_match and any(token in lowered for token in ["打开", "open"]):
            spreadsheet_path = spreadsheet_path_match.group(1).strip()
            operations.append(
                OperationSpec(
                    id="open_spreadsheet",
                    kind="spreadsheet.open",
                    description="Open the requested spreadsheet.",
                    arguments={"file_path": spreadsheet_path},
                )
            )
            return TaskSpec(
                summary=text,
                success_criteria=["Open the requested spreadsheet."],
                operations=operations,
                metadata={"source": "deterministic", "original_instruction": instruction},
            )

        text_file = self._infer_text_file_from_instruction(text)
        if text_file.get("file_path") and (
            ".txt" in text.lower()
            or "记事本" in text
            or "文本文件" in text
        ):
            write_requested = any(token in text for token in ["输入", "写入", "保存", "内容"]) or any(
                token in lowered for token in ["input", "write", "save", "edit"]
            )
            if write_requested:
                operations.append(
                    OperationSpec(
                        id="write_text_file",
                        kind="filesystem.write_text",
                        description="Create or overwrite the requested text file.",
                        arguments={"file_path": text_file["file_path"], "text": text_file.get("text", "")},
                    )
                )
                return TaskSpec(
                    summary=text,
                    success_criteria=["Create or overwrite the requested text file with the requested content."],
                    operations=operations,
                    metadata={"source": "deterministic", "original_instruction": instruction},
                )
            operations.append(
                OperationSpec(
                    id="open_text_file",
                    kind="filesystem.open_path",
                    description="Open the requested text file.",
                    arguments={"path": text_file["file_path"]},
                )
            )
            return TaskSpec(
                summary=text,
                success_criteria=["Open the requested text file."],
                operations=operations,
                metadata={"source": "deterministic", "original_instruction": instruction},
            )

        folder = self._infer_folder_creation_from_instruction(text)
        if folder.get("folder_name"):
            operations.append(
                OperationSpec(
                    id="create_folder",
                    kind="filesystem.create_folder",
                    description="Create the requested folder.",
                    arguments={"path": self._normalize_desktop_path_value(folder["folder_name"])},
                )
            )
            return TaskSpec(
                summary=text,
                success_criteria=["Create the requested folder."],
                operations=operations,
                metadata={"source": "deterministic", "original_instruction": instruction},
            )

        open_path = self._infer_open_path_from_instruction(text)
        if open_path.get("path") and any(token in lowered for token in ["打开", "open", "文件夹", "目录", "folder"]):
            path_value = str(open_path["path"])
            suffix = self._path_suffix(path_value)
            if suffix in self.SPREADSHEET_EXTENSIONS:
                operations.append(
                    OperationSpec(
                        id="open_spreadsheet",
                        kind="spreadsheet.open",
                        description="Open the requested spreadsheet.",
                        arguments={"file_path": path_value},
                    )
                )
                return TaskSpec(
                    summary=text,
                    success_criteria=["Open the requested spreadsheet."],
                    operations=operations,
                    metadata={"source": "deterministic", "original_instruction": instruction},
                )
            operations.append(
                OperationSpec(
                    id="open_path",
                    kind="filesystem.open_path",
                    description="Open the requested file or folder path.",
                    arguments={"path": open_path["path"]},
                )
            )
            return TaskSpec(
                summary=text,
                success_criteria=["Open the requested file or folder."],
                operations=operations,
                metadata={"source": "deterministic", "original_instruction": instruction},
            )

        rename_info = self._infer_rename_from_instruction(text)
        if rename_info.get("src") and rename_info.get("new_name"):
            operations.append(
                OperationSpec(
                    id="rename_file",
                    kind="filesystem.rename",
                    description="Rename the requested file or folder.",
                    arguments={"src": rename_info["src"], "new_name": rename_info["new_name"]},
                )
            )
            return TaskSpec(
                summary=text,
                success_criteria=["Rename the requested file or folder."],
                operations=operations,
                metadata={"source": "deterministic", "original_instruction": instruction},
            )

        delete_target = self._infer_delete_target_from_instruction(text)
        if delete_target.get("path") and any(token in lowered for token in ["删除", "delete", "移除", "清空"]):
            operations.append(
                OperationSpec(
                    id="delete_path",
                    kind="filesystem.delete",
                    description="Delete the requested file or folder.",
                    arguments={"path": delete_target["path"]},
                    risky=True,
                )
            )
            return TaskSpec(
                summary=text,
                success_criteria=["Delete the requested file or folder."],
                operations=operations,
                metadata={"source": "deterministic", "original_instruction": instruction},
            )

        return None

    def _deterministic_visual_instruction(self, instruction: str) -> Optional[str]:
        folder = self._infer_folder_creation_from_instruction(instruction)
        folder_name = str(folder.get("folder_name", "") or "").strip()
        if folder_name:
            return "在当前打开的文件管理器里创建一个名为 {} 的文件夹".format(folder_name)
        return None

    def _task_spec_to_dict(self, task_spec: TaskSpec) -> Dict[str, Any]:
        return {
            "summary": task_spec.summary,
            "success_criteria": task_spec.success_criteria,
            "metadata": task_spec.metadata,
            "operations": [
                {
                    "id": operation.id,
                    "kind": operation.kind,
                    "description": operation.description,
                    "arguments": operation.arguments,
                    "depends_on": operation.depends_on,
                    "risky": operation.risky,
                }
                for operation in task_spec.operations
            ],
        }

    def _truncate_text(self, text: str, max_length: int = 400) -> str:
        if len(text) <= max_length:
            return text
        return text[:max_length] + "..."
