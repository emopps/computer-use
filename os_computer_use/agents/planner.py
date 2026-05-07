from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from os_computer_use.agents.action import ActionAgent, TaskExecutionError
from os_computer_use.agents.audit import AuditAgent
from os_computer_use.agents.intent import TaskClarificationRequired, TaskPlanningError
from os_computer_use.agents.memory import MemoryAgent
from os_computer_use.logging import logger
from os_computer_use.runtime.event_schema import build_execution_event
from os_computer_use.runtime.scheduler import DAGScheduler
from os_computer_use.runtime.scheduler import SchedulerNode
from os_computer_use.runtime.task_models import ExecutionContext, OperationResult, OperationStatus, TaskModelError, TaskSpec


class PlannerAgent:
    def __init__(self, provider: Optional[Any] = None):
        self.provider = provider

    @staticmethod
    def _extract_explicit_path(instruction: str, extensions: Optional[tuple[str, ...]] = None) -> str:
        text = str(instruction or "").strip()
        if not text:
            return ""
        ext_suffix = r"(?:\.[A-Za-z0-9_]+)"
        if extensions:
            ext_suffix = r"(?:\.(?:{}))".format("|".join(re.escape(item) for item in extensions))
        verb_boundary = (
            r"(?:并|然后|再|接着|"
            r"输入|写入|保存|修改|编辑|"
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
                return match.group(1).strip().rstrip(" ,.;:'\"")
        return ""

    @staticmethod
    def _extract_explicit_spreadsheet_target(instruction: str) -> str:
        text = str(instruction or "").strip()
        if not text:
            return ""
        patterns = [
            r"((?:/|~/)[^\s,\uFF0C\u3002\uFF1B;\"']+\.(?:et|xlsx|xls))",
            r"([^\s,\uFF0C\u3002\uFF1B;\"']+\.(?:et|xlsx|xls))",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip().rstrip(" ,.;:'\"")
        return ""

    def _backfill_path_args(self, instruction: str, kind: str, args: Dict[str, Any]) -> None:
        explicit_text_path = self._extract_explicit_path(instruction, extensions=("txt", "md", "log"))
        explicit_any_path = self._extract_explicit_path(instruction)
        explicit_sheet_target = self._extract_explicit_spreadsheet_target(instruction)
        lowered_instruction = str(instruction or "").lower()
        if kind == "filesystem.write_text" and explicit_text_path:
            args["file_path"] = explicit_text_path
        elif kind == "filesystem.open_path" and explicit_any_path and not args.get("path"):
            args["path"] = explicit_any_path
        elif kind in {"spreadsheet.open", "spreadsheet.write_cell"} and explicit_sheet_target:
            args["file_path"] = explicit_sheet_target
            args.pop("file_name", None)
        elif kind in {"filesystem.copy", "filesystem.move", "filesystem.rename"} and explicit_any_path and not args.get("src"):
            args["src"] = explicit_any_path
        elif kind == "command.run" and not args.get("command"):
            if "刷新桌面" in str(instruction or "") or (
                "refresh" in lowered_instruction and ("desktop" in lowered_instruction or "桌面" in str(instruction or ""))
            ):
                args["command"] = "xdotool key F5"

    def plan(
        self,
        instruction: str,
        previous_error: Optional[str] = None,
        previous_plan: Optional[TaskSpec] = None,
        previous_results: Optional[Any] = None,
    ) -> TaskSpec:
        if self.provider is None:
            raise TaskPlanningError("PlannerAgent requires a configured language model provider.")

        planning_errors: List[str] = []
        for attempt in range(3):
            prompt = self._build_planning_prompt(
                instruction,
                previous_error=planning_errors[-1] if planning_errors else previous_error,
                previous_plan=self._task_spec_to_dict(previous_plan) if previous_plan else None,
                previous_results=previous_results,
            )
            response = self.provider.call([{"role": "user", "content": prompt}])
            logger.log(
                "Planner raw output (attempt {}): {}".format(attempt + 1, self._truncate_text(response)),
                "gray",
            )
            try:
                return self._parse_and_normalize_plan(
                    instruction,
                    response,
                    previous_results=previous_results,
                )
            except TaskClarificationRequired:
                raise
            except TaskPlanningError as exc:
                planning_errors.append(str(exc))

        raise TaskPlanningError(
            "PlannerAgent failed to produce a valid task spec after retries: {}".format(
                " | ".join(planning_errors)
            )
        )

    def replan(
        self,
        instruction: str,
        failed_task_spec: TaskSpec,
        failure_message: str,
        previous_results: Optional[Any] = None,
    ) -> TaskSpec:
        """重新规划：基于失败的 task_spec 和失败原因，重新生成计划。"""
        return self.plan(
            instruction=instruction,
            previous_error=failure_message,
            previous_plan=failed_task_spec,
            previous_results=previous_results,
        )

    def _build_planning_prompt(
        self,
        instruction: str,
        previous_error: Optional[str],
        previous_plan: Optional[Dict[str, Any]],
        previous_results: Optional[Any],
    ) -> str:
        from os_computer_use.runtime.capabilities import supported_kinds_text
        blocks = [
            "You are the Planner Agent for a desktop computer-use system.\n",
            "Convert the user instruction into the next valid JSON task step for a desktop automation loop.\n",
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
            "0. 这是单步循环模式。每次只规划当前最应该执行的一个原子步骤。除非任务已经完成，否则 operations 里只保留一个当前可执行步骤。\n",
            "0a. 如果结合已完成结果判断任务已经完成，返回空 operations，并在 metadata 中设置 {\"task_completed\": true}。\n",
            "0b. 已经成功执行过的步骤不要再次规划。尤其是 spreadsheet.open / browser.open / filesystem.open_path 这类打开动作，完成后下一步应该前进到写入、搜索、复制结果等后续动作。\n",
            "0c. 如果用户要求把搜索结果写入表格/单元格/Excel/WPS，在真正出现成功的 spreadsheet.write_cell 之前，绝不能返回 task_completed。仅完成 browser.search 仍然未完成任务。\n",
            "1. 不要输出不支持的操作类型。\n",
            "2. 使用 arguments，不是 params。\n",
            "3. 使用 depends_on，不是 dependencies。\n",
            "4. 不要编造路径或业务数据。\n",
            "5. spreadsheet.open 用 'file_path' 指定文档路径。麒麟系统WPS表格支持 '.et' 或 '.xlsx' 后缀。文件不需要预先存在，会自动创建。新文件优先用 '.xlsx'。\n",
            "5a. Never use spreadsheet.open or spreadsheet.write_cell for .txt files. Use filesystem.write_text for text editing and filesystem.open_path for plain open requests.\n",
            "6. spreadsheet.write_cell 需包含 'cell'（如 'A1'）和 'text'。\n",
            "6a. 重要：spreadsheet.write_cell 必须在 depends_on 中依赖 spreadsheet.open。不能在未打开文件的情况下写入单元格。始终生成 spreadsheet.open 操作并让 write_cell 依赖它。\n",
            "7. filesystem.write_text 需包含 'file_path' 和 'text'。\n",
            "8. browser.open 用 'url'。仅用于打开特定网站。默认搜索引擎是百度（https://www.baidu.com），不是 Google。\n",
            "9. browser.search 用 'text'，保持查询语义忠实。重要：搜索任务始终用 browser.search（不是 browser.open），不要手动构造搜索URL。\n",
            "10. browser.send 用 'to' 和 'body'。\n",
            "11. 如果之前的错误说某操作类型不支持，替换为支持的类型。\n",
            "12. 尽量保留用户原始语言填写文本字段。\n",
            "13. 重要：不要用 'command.run' 执行已有专门操作类型的任务（如用 'filesystem.rename' 而非通过 'command.run' 运行 'mv'）。\n",
            "14. filesystem.rename 用 'src' 指定旧路径，'new_name' 指定新文件名（不是完整路径）。\n",
            "15. 重要：不要用 browser.open 打开WPS/表格文件。用 spreadsheet.open 打开WPS文件，WPS是桌面应用，不是网站。\n",
            "16. 当后续操作需要前序操作的结果文本时（如搜索结果），用 'from_operation' 指定源操作id，不要编造占位符文本如 '[search result]'。例如：browser.search 的 id 为 'search1'，则 filesystem.write_text 可用 {\"from_operation\": \"search1\"} 代替猜测文本。系统会自动填入实际结果。\n",
            ]
        if previous_plan:
            blocks.append("Previous plan:\n")
            blocks.append(json.dumps(previous_plan, ensure_ascii=False))
            blocks.append("\n")
        if previous_results:
            blocks.append("Completed execution results so far:\n")
            blocks.append(self._results_for_prompt(previous_results))
            blocks.append("\n")
        if previous_error:
            blocks.append("Previous error:\n")
            blocks.append(previous_error)
            blocks.append("\n")
        blocks.append("User instruction:\n")
        blocks.append(instruction)
        return "".join(blocks)

    @staticmethod
    def _results_for_prompt(previous_results: Any) -> str:
        if isinstance(previous_results, list):
            lines = []
            for item in previous_results[-12:]:
                if not isinstance(item, dict):
                    lines.append(str(item))
                    continue
                lines.append(
                    "- {kind}({operation_id}): {output}".format(
                        kind=str(item.get("kind", "") or "operation"),
                        operation_id=str(item.get("operation_id", "") or "step"),
                        output=PlannerAgent._short_output(item.get("output")),
                    )
                )
            return "\n".join(lines)
        if isinstance(previous_results, dict):
            return json.dumps(PlannerAgent._safe_output(previous_results), ensure_ascii=False)
        return str(previous_results)

    # 安全网：必填参数，不允许模型编造
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
        "command.run": ["command"],
        "spreadsheet.open": [],  # file_path 自动生成，不需要模型提供
        "spreadsheet.write_cell": ["cell", "text"],
    }

    def _validate_plan_params(self, instruction: str, task_spec: TaskSpec) -> None:
        """安全网：检测编造的必填参数，确保参数有指令依据。"""
        for operation in task_spec.operations:
            kind = operation.kind
            args = operation.arguments
            text_value = args.get("text")
            if isinstance(text_value, dict):
                source_id = text_value.get("from_operation") or text_value.get("source_operation")
                if source_id:
                    args["from_operation"] = str(source_id)
                    args.pop("text", None)
            self._backfill_path_args(instruction, kind, args)
            required = self._REQUIRED_PARAMS.get(kind, [])

            # spreadsheet.open：自动生成默认文件路径
            if kind == "spreadsheet.open" and not args.get("file_path") and not args.get("file_name"):
                args["file_path"] = "工作簿1.xlsx"

            for field in required:
                value = args.get(field)
                # from_operation 可以替代 text 字段
                if field == "text" and args.get("from_operation"):
                    continue
                # 检测占位符文本（如 [search result]、[结果] 等）
                if field == "text" and isinstance(value, str):
                    placeholder_patterns = ["[search result]", "[result]", "[结果]", "[搜索结果]", "[待填充]", "[TBD]"]
                    if any(p in value for p in placeholder_patterns):
                        raise TaskClarificationRequired(
                            "模型生成了占位符文本 '{}'，请使用 from_operation 引用前序操作结果，或提供实际内容。".format(value)
                        )
                    if re.match(r"^@[A-Za-z0-9_\-]+\.(?:text|output)$", value.strip()) or re.match(
                        r"^\$\{\{?\s*[A-Za-z0-9_\-]+\.(?:text|output)\s*\}?\}$",
                        value.strip(),
                    ) or re.match(
                        r"^\$\{\{?\s*[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+(?:\[\d+\])?\s*\}?\}$",
                        value.strip(),
                    ):
                        raise TaskClarificationRequired(
                            "模型生成了未解析的引用文本 '{}'，请改用 from_operation 引用前序操作结果。".format(value)
                        )
                if value is None or (isinstance(value, str) and not value.strip()):
                    # 缺少必填参数——模型跳过了
                    field_display = {
                        "src": "源文件路径", "dst_dir": "目标文件夹", "new_name": "新文件名",
                        "path": "目标路径", "file_path": "文件路径", "text": "内容",
                        "url": "网址", "to": "收件人", "body": "正文内容",
                        "cell": "单元格位置(如A1)",
                    }
                    readable = field_display.get(field, field)
                    raise TaskClarificationRequired(
                        "还缺少关键信息：{}。请补充后我再继续。".format(readable)
                    )

                # 路径校验安全网
                if field in ["src", "path", "file_path"] and isinstance(value, str):
                    # 路径规范化：
                    # 如果模型猜测了'桌面/桌面工作簿1.et'但指令是'桌面 工作簿1.et'
                    # 模型错误地把'桌面'同时当作目录前缀和文件名前缀
                    if "/" in value or "\\" in value:
                        parts = re.split(r'[/\\\\]', value)
                        if parts[0] in ["桌面", "Desktop"] and len(parts) > 1:
                            filename_part = parts[-1]
                            # 同时去掉文件名开头的'桌面'
                            # （模型在指令为'桌面 工作簿1.et'时会幻觉出'桌面工作簿1.et'）
                            clean_filename = re.sub(r'^(桌面|Desktop)', '', filename_part)
                            # 检查清理后的文件名是否在指令中
                            if clean_filename in instruction or filename_part in instruction:
                                # 自纠正：使用不含编造目录前缀的文件名
                                args[field] = clean_filename if clean_filename else filename_part
                                value = args[field]

                    basename = os.path.basename(value)
                    # 同时去掉 basename 开头的'桌面'用于依据检查
                    clean_basename = re.sub(r'^(桌面|Desktop)', '', basename)
                    grounded = (value in instruction) or (basename in instruction) or (clean_basename in instruction)
                    
                    if not grounded:
                        # 最终检查：模型可能在指令中用空格组合了'桌面'和'工作簿1.et'
                        # 但在计划中用了斜杠
                        if "/" in value:
                            potential_text = value.replace("/", " ")
                            if potential_text in instruction:
                                grounded = True
                    
                    if not grounded:
                        # 跳过浏览器URL和表格路径的依据检查
                        # （表格文件不存在时会自动创建）
                        if kind.startswith("browser") or kind == "spreadsheet.open":
                            continue
                        
                        raise TaskClarificationRequired(
                            "模型似乎猜测了一个文件路径 '{}'，请提供确切的完整文件路径。".format(value)
                        )

                # 检测编造的重命名名称（如 _renamed 后缀）
                if kind == "filesystem.rename" and field == "new_name" and isinstance(value, str):
                    base = os.path.basename(value)
                    # 如果 new_name 包含 _renamed 或 _copy 等，很可能是编造的
                    fabricated_patterns = ["_renamed", "_copy", "_new", "_moved", "_备份"]
                    for pattern in fabricated_patterns:
                        if pattern in base.lower() and pattern not in instruction.lower():
                            raise TaskClarificationRequired(
                                "你想把文件重命名为什么名字？"
                            )

    def _parse_and_normalize_plan(
        self,
        instruction: str,
        raw_response: str,
        previous_results: Optional[Any] = None,
    ) -> TaskSpec:
        parsed = self._extract_json_value(raw_response)
        
        # 确保操作存在且为列表
        if not isinstance(parsed, dict) or "operations" not in parsed:
            raise TaskPlanningError("Model output missing 'operations' list.")
            
        ops = parsed.get("operations", [])
        if not isinstance(ops, list):
            raise TaskPlanningError("'operations' must be a list.")
        parsed.setdefault("metadata", {})
            
        # 基本参数映射修正
        for op in ops:
            kind = op.get("kind", "")
            args = op.get("arguments", {})
            if not isinstance(args, dict):
                op["arguments"] = {}
                args = op["arguments"]
            self._normalize_reference_arguments(args)
                
            # 重命名修正：new_name 应为文件名，src 应为完整路径
            if kind == "filesystem.rename":
                if "new_path" in args:
                    args["new_name"] = os.path.basename(args["new_path"])
                if "name" in args and "new_name" not in args:
                    args["new_name"] = args["name"]

            # 自动修正：去除路径中的 file:// 前缀
            for path_key in ["file_path", "src", "path", "dst_dir"]:
                val = args.get(path_key, "")
                if isinstance(val, str) and val.startswith("file://"):
                    args[path_key] = re.sub(r'^file://+', '', val)

            text_val = args.get("text", "")

            # 自动修正：browser.open 带搜索URL → browser.search
            # LLM 有时生成 browser.open + google.com/search?q=... 而非 browser.search
            if kind == "browser.open" and args.get("url"):
                url = args["url"]
                search_match = re.match(
                    r'https?://(?:www\.)?(?:google|bing|yahoo|duckduckgo)\.[a-z]+/search\?(?:.*&)?q=([^&]+)',
                    url, re.IGNORECASE
                )
                if search_match:
                    from urllib.parse import unquote
                    query_text = unquote(search_match.group(1)).replace('+', ' ').strip()
                    if query_text:
                        op["kind"] = "browser.search"
                        args["text"] = query_text
                        if "url" in args:
                            del args["url"]

            # 自动修正：browser.open 通用打开默认使用 baidu.com
            if kind == "browser.open" and args.get("url"):
                url = args.get("url", "")
                if re.match(r'https?://(?:www\.)?google\.[a-z]+/?$', url, re.IGNORECASE):
                    args["url"] = "https://www.baidu.com"

            # 自动修正：[from_operation:xxx] 模式转为正确的 from_operation 参数
            text_val = args.get("text", "")
            if isinstance(text_val, str):
                from_op_match = re.match(r'^\[from_operation[:\s]+(\w+)\]$', text_val.strip())
                if from_op_match:
                    args["from_operation"] = from_op_match.group(1)
                    del args["text"]

            # 自动修正：[search_result] 占位符 → from_operation 引用 browser.search
            if isinstance(text_val, str) and text_val.strip() in ["[search_result]", "[search result]", "[结果]", "[搜索结果]", "[待填充]", "[TBD]"]:
                # 查找 depends_on 中的 browser.search 操作 id
                depends = op.get("depends_on", [])
                search_ops = [o for o in ops if o.get("kind") == "browser.search"]
                if search_ops:
                    # 优先使用 depends_on 中的 search op，否则取最后一个
                    dep_search = [s for s in search_ops if s.get("id") in depends]
                    source_id = dep_search[0].get("id") if dep_search else search_ops[-1].get("id")
                    if source_id:
                        args["from_operation"] = source_id
                        del args["text"]

        # 自动清理：移除不必要的 document.extract_text 中间操作
        # 3B 模型经常在 browser.search 后生成 document.extract_text 来"读取"搜索结果，
        # 但搜索结果已经作为 browser.search 的输出返回，不需要额外提取
        ops = self._remove_unnecessary_extract_text(ops, parsed)

        # 如果用户明确给了唯一的表格目标文件，所有表格操作都必须落到这个文件
        # 不能让模型在多步写入时幻觉出按城市拆分的新文件名
        ops = self._coalesce_spreadsheet_targets(instruction, ops)

        # 结构保证：spreadsheet.write_cell 之前必须有 spreadsheet.open
        ops = self._ensure_spreadsheet_open_before_write(ops, parsed)
        ops = self._ensure_from_operation_dependencies(ops)
        ops = self._resolve_completed_external_dependencies(ops, previous_results)
        ops = self._serialize_spreadsheet_writes(ops)
        ops = self._prune_completed_operation_dicts(ops, previous_results)
        ops = self._reduce_to_next_step(ops)
        parsed["operations"] = ops
        summary = str(parsed.get("summary", "") or "").strip()
        if not summary:
            if parsed["operations"]:
                first_op = parsed["operations"][0]
                summary = str(first_op.get("description", "") or first_op.get("kind", "") or "").strip()
            elif previous_results:
                summary = "Task completed."
            else:
                summary = str(instruction or "").strip()
            parsed["summary"] = summary
        if not parsed["operations"]:
            parsed.setdefault("metadata", {})
            parsed["metadata"]["task_completed"] = True
            if not str(parsed.get("summary", "") or "").strip():
                parsed["summary"] = "Task completed."

        try:
            spec = TaskSpec.from_dict(parsed)
        except (TaskModelError, KeyError, TypeError) as exc:
            raise TaskPlanningError("Plan is invalid: {}".format(exc))

        # 安全网：验证必填参数未被编造
        self._validate_plan_params(instruction, spec)
        return spec

    @staticmethod
    def _normalize_reference_arguments(args: Dict[str, Any]) -> None:
        text_val = args.get("text", "")
        if isinstance(text_val, dict):
            source_id = text_val.get("from_operation") or text_val.get("source_operation")
            if source_id:
                args["from_operation"] = str(source_id)
                field_path = text_val.get("from_field") or text_val.get("field_path")
                if field_path:
                    args["from_field"] = str(field_path).strip()
                index_value = text_val.get("from_index")
                if index_value is not None and str(index_value).strip() != "":
                    try:
                        args["from_index"] = int(index_value)
                    except Exception:
                        pass
                args.pop("text", None)
                return

        if not isinstance(text_val, str):
            return

        stripped_text = text_val.strip()
        if not stripped_text:
            return

        matchers = [
            re.match(r'^from_operation[:\s]+([A-Za-z0-9_\-]+)$', stripped_text),
            re.match(r'^\[from_operation[:\s]+([A-Za-z0-9_\-]+)\]$', stripped_text),
            re.match(r'^@([A-Za-z0-9_\-]+)\.(?:text|output)$', stripped_text),
            re.match(r'^\$\{\{\s*([A-Za-z0-9_\-]+)\.(?:text|output)\s*\}\}$', stripped_text),
            re.match(r'^\$\{\s*([A-Za-z0-9_\-]+)\.(?:text|output)\s*\}$', stripped_text),
        ]
        for matched in matchers:
            if matched:
                args["from_operation"] = matched.group(1)
                args.pop("text", None)
                return

        indexed_match = re.match(
            r'^\$\{\{?\s*([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\[(\d+)\]\s*\}?\}$',
            stripped_text,
        )
        if indexed_match:
            args["from_operation"] = indexed_match.group(1)
            args["from_field"] = indexed_match.group(2)
            args["from_index"] = int(indexed_match.group(3))
            args.pop("text", None)
            return

        field_match = re.match(
            r'^\$\{\{?\s*([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\s*\}?\}$',
            stripped_text,
        )
        if field_match:
            args["from_operation"] = field_match.group(1)
            args["from_field"] = field_match.group(2)
            args.pop("text", None)
            return

        if stripped_text.startswith("{") and stripped_text.endswith("}"):
            try:
                parsed_text = json.loads(stripped_text)
            except Exception:
                parsed_text = None
            if isinstance(parsed_text, dict):
                source_id = parsed_text.get("from_operation") or parsed_text.get("source_operation")
                if source_id:
                    args["from_operation"] = str(source_id)
                    field_path = parsed_text.get("from_field") or parsed_text.get("field_path")
                    if field_path:
                        args["from_field"] = str(field_path).strip()
                    index_value = parsed_text.get("from_index")
                    if index_value is not None and str(index_value).strip() != "":
                        try:
                            args["from_index"] = int(index_value)
                        except Exception:
                            pass
                    args.pop("text", None)

    def _resolve_completed_external_dependencies(self, ops: list, previous_results: Optional[Any]) -> list:
        if not ops or not isinstance(previous_results, list):
            return ops

        completed_by_operation_id = defaultdict(list)
        for item in previous_results:
            if not isinstance(item, dict):
                continue
            operation_id = str(item.get("operation_id", "") or "")
            if operation_id:
                completed_by_operation_id[operation_id].append(item)
        completed_spreadsheet_targets = {
            str((item.get("arguments", {}) or {}).get("file_path") or (item.get("arguments", {}) or {}).get("file_name") or "").strip()
            for item in previous_results
            if isinstance(item, dict) and str(item.get("kind", "") or "") == "spreadsheet.open"
        }
        completed_spreadsheet_targets.discard("")
        current_ids = {str(op.get("id", "") or "") for op in ops}

        for op in ops:
            normalized_deps = []
            for dep in list(op.get("depends_on", []) or []):
                dep_id = str(dep or "")
                if not dep_id:
                    continue
                if dep_id in current_ids:
                    normalized_deps.append(dep_id)
                    continue
                completed = self._select_previous_result(op, completed_by_operation_id.get(dep_id, []))
                if completed:
                    if str(completed.get("kind", "") or "") == "spreadsheet.open":
                        target_ref = str(
                            (completed.get("arguments", {}) or {}).get("file_path")
                            or (completed.get("arguments", {}) or {}).get("file_name")
                            or ""
                        ).strip()
                        if target_ref:
                            completed_spreadsheet_targets.add(target_ref)
                    continue
                normalized_deps.append(dep_id)
            op["depends_on"] = normalized_deps

            args = op.get("arguments", {}) or {}
            source_id = str(args.get("from_operation", "") or "").strip()
            if source_id and source_id not in current_ids:
                completed = self._select_previous_result(op, completed_by_operation_id.get(source_id, []))
                if completed:
                    resolved_text = self._coerce_result_text(completed.get("output"))
                    if resolved_text:
                        args["text"] = resolved_text
                    args.pop("from_operation", None)

            if op.get("kind") == "spreadsheet.write_cell":
                target_ref = str(args.get("file_path") or args.get("file_name") or "").strip()
                if target_ref and target_ref in completed_spreadsheet_targets:
                    op["depends_on"] = [dep for dep in list(op.get("depends_on", []) or []) if dep in current_ids]

        return ops

    def _reduce_to_next_step(self, ops: list) -> list:
        if not ops:
            return ops
        if len(ops) == 1:
            return ops
        op_ids = {str(op.get("id", "")) for op in ops}
        for op in ops:
            deps = [str(dep) for dep in op.get("depends_on", []) if str(dep) in op_ids]
            if not deps:
                op["depends_on"] = []
                return [op]
        first = dict(ops[0])
        first["depends_on"] = []
        return [first]

    def _prune_completed_operation_dicts(self, ops: list, previous_results: Optional[Any]) -> list:
        if not previous_results or not ops:
            return ops
        previous_result_items = [item for item in previous_results if isinstance(item, dict)]
        completed_signatures = {
            self._operation_signature_from_result(item)
            for item in previous_result_items
        }
        completed_signatures.discard("")
        if not completed_signatures:
            return ops
        completed_results_by_op_id = defaultdict(list)
        for item in previous_result_items:
            operation_id = str(item.get("operation_id", "") or "")
            if operation_id:
                completed_results_by_op_id[operation_id].append(item)
        completed_spreadsheet_targets = {
            str((item.get("arguments", {}) or {}).get("file_path") or (item.get("arguments", {}) or {}).get("file_name") or "").strip()
            for item in previous_result_items
            if str(item.get("kind", "") or "") == "spreadsheet.open"
        }
        completed_spreadsheet_targets.discard("")

        removed_ids = set()
        remaining = []
        for operation in ops:
            signature = self._operation_signature(
                str(operation.get("kind", "") or ""),
                dict(operation.get("arguments", {}) or {}),
            )
            if signature in completed_signatures:
                removed_ids.add(str(operation.get("id", "") or ""))
                continue
            remaining.append(operation)

        if not removed_ids:
            return ops

        for operation in remaining:
            operation["depends_on"] = [
                dep for dep in list(operation.get("depends_on", []) or [])
                if dep not in removed_ids
            ]
            args = operation.get("arguments", {})
            if isinstance(args, dict):
                source_id = str(args.get("from_operation", "") or "")
                if source_id in removed_ids:
                    source_result = self._select_previous_result(operation, completed_results_by_op_id.get(source_id, []))
                    source_output = source_result.get("output")
                    resolved_text = self._coerce_result_text(source_output)
                    if resolved_text:
                        args["text"] = resolved_text
                    args.pop("from_operation", None)
        remaining = self._ensure_spreadsheet_open_before_write(
            remaining,
            {"operations": remaining},
            satisfied_targets=completed_spreadsheet_targets,
        )
        remaining = self._ensure_from_operation_dependencies(remaining)
        remaining = self._serialize_spreadsheet_writes(remaining)
        return remaining

    @staticmethod
    def _coerce_result_text(output: Any) -> str:
        if output is None:
            return ""
        if isinstance(output, str):
            return output.strip()
        if isinstance(output, dict):
            candidate = output.get("text")
            if candidate is not None:
                return str(candidate).strip()
        return str(output).strip()

    def _select_previous_result(self, operation: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not candidates:
            return {}
        if len(candidates) == 1:
            return candidates[0]

        operation_context = " ".join(
            [
                str(operation.get("id", "") or ""),
                str(operation.get("description", "") or ""),
                json.dumps(operation.get("arguments", {}) or {}, ensure_ascii=False),
            ]
        ).lower()
        city_tokens = [
            "beijing", "北京",
            "shanghai", "上海",
            "nanjing", "南京",
        ]

        best_item = candidates[-1]
        best_score = -1
        for index, item in enumerate(candidates):
            score = index
            candidate_blob = " ".join(
                [
                    str(item.get("description", "") or ""),
                    json.dumps(item.get("arguments", {}) or {}, ensure_ascii=False),
                    json.dumps(item.get("output", {}) or {}, ensure_ascii=False),
                ]
            ).lower()
            for token in city_tokens:
                if token.lower() in operation_context and token.lower() in candidate_blob:
                    score += 20
            if str(item.get("kind", "") or "") == "browser.search":
                score += 3
            if score > best_score:
                best_score = score
                best_item = item
        return best_item

    @staticmethod
    def _operation_signature(kind: str, arguments: Dict[str, Any]) -> str:
        normalized = PlannerAgent._safe_output(dict(arguments or {}))
        return "{}|{}".format(str(kind or ""), json.dumps(normalized, ensure_ascii=False, sort_keys=True))

    def _operation_signature_from_result(self, item: Dict[str, Any]) -> str:
        return self._operation_signature(
            str(item.get("kind", "") or ""),
            dict(item.get("arguments", {}) or {}),
        )

    def _remove_unnecessary_extract_text(self, ops: list, parsed: dict) -> list:
        """自动清理：移除 browser.search 后不必要的 document.extract_text 中间操作。
        3B 模型经常在 browser.search 后生成 document.extract_text 来"读取"搜索结果，
        但搜索结果已经作为 browser.search 的输出返回，不需要额外提取。
        同时修正依赖链：依赖被移除操作的后续操作，改为依赖 browser.search。"""
        extract_ops = [op for op in ops if op.get("kind") == "document.extract_text"]
        search_ops = [op for op in ops if op.get("kind") == "browser.search"]
        if not extract_ops or not search_ops:
            return ops

        search_ids = {op.get("id") for op in search_ops}
        removed_ids = set()

        for ext_op in extract_ops:
            ext_deps = ext_op.get("depends_on", []) or []
            # 如果 document.extract_text 依赖 browser.search，说明是多余的
            if any(d in search_ids for d in ext_deps):
                ext_id = ext_op.get("id", "")
                removed_ids.add(ext_id)
                # 修正后续操作的依赖：把对 ext_id 的依赖替换为 search op
                for other_op in ops:
                    other_deps = other_op.get("depends_on", []) or []
                    if ext_id in other_deps:
                        # 替换为 extract_text 依赖的 search op
                        new_deps = []
                        for d in other_deps:
                            if d == ext_id:
                                for sd in ext_deps:
                                    if sd in search_ids:
                                        new_deps.append(sd)
                                        break
                            else:
                                new_deps.append(d)
                        other_op["depends_on"] = new_deps
                    # 修正 from_operation 引用
                    args = other_op.get("arguments", {})
                    if args.get("from_operation") == ext_id:
                        for sd in ext_deps:
                            if sd in search_ids:
                                args["from_operation"] = sd
                                break

        if removed_ids:
            ops = [op for op in ops if op.get("id", "") not in removed_ids]
            parsed["operations"] = ops

        return ops

    def _ensure_spreadsheet_open_before_write(
        self,
        ops: list,
        parsed: dict,
        satisfied_targets: Optional[set] = None,
    ) -> list:
        """结构保证：每个 spreadsheet.write_cell 必须依赖 spreadsheet.open。
        如果模型忘记包含，自动插入。"""
        satisfied_targets = satisfied_targets or set()
        existing_ids = {op.get("id", "") for op in ops}
        existing_kinds = {op.get("kind", "") for op in ops}
        has_spreadsheet_open = "spreadsheet.open" in existing_kinds

        # 查找 spreadsheet.open 的 id（如果存在）
        open_op_id = None
        for op in ops:
            if op.get("kind") == "spreadsheet.open":
                open_op_id = op.get("id", "open_spreadsheet")
                break

        # 从 write_cell 操作收集 file_path/file_name，传播到 open 操作
        write_ops = [op for op in ops if op.get("kind") == "spreadsheet.write_cell"]
        if not write_ops:
            return ops

        # 如果没有 spreadsheet.open，创建一个
        if not has_spreadsheet_open:
            # 从第一个 write_cell 推导文件引用
            first_write_args = write_ops[0].get("arguments", {}) or {}
            file_path = first_write_args.get("file_path", "")
            file_name = first_write_args.get("file_name", "")
            target_ref = str(file_path or file_name or "").strip()
            if target_ref and target_ref in satisfied_targets:
                return ops

            open_op_id = "open_spreadsheet"
            open_op = {
                "id": open_op_id,
                "kind": "spreadsheet.open",
                "description": "Open spreadsheet file",
                "arguments": {},
                "depends_on": [],
                "risky": False,
            }
            if file_path:
                open_op["arguments"]["file_path"] = file_path
            elif file_name:
                open_op["arguments"]["file_name"] = file_name

            ops.insert(0, open_op)
            parsed["operations"] = ops

        # Ensure every write_cell depends on the open op
        for op in write_ops:
            depends_on = list(op.get("depends_on", []) or [])
            if open_op_id not in depends_on:
                depends_on.append(open_op_id)
            op["depends_on"] = depends_on

        return ops

    def _coalesce_spreadsheet_targets(self, instruction: str, ops: list) -> list:
        explicit_target = self._extract_explicit_spreadsheet_target(instruction)
        spreadsheet_ops = [op for op in ops if op.get("kind") in {"spreadsheet.open", "spreadsheet.write_cell"}]
        if not spreadsheet_ops:
            return ops

        canonical_target = explicit_target
        if not canonical_target:
            for op in spreadsheet_ops:
                args = op.get("arguments", {}) or {}
                candidate = str(args.get("file_path") or args.get("file_name") or "").strip()
                if candidate:
                    canonical_target = candidate
                    break
        if not canonical_target:
            return ops

        target_key = "file_path"
        if not explicit_target and not any(sep in canonical_target for sep in ("/", "\\")) and not canonical_target.startswith("~"):
            target_key = "file_name"

        primary_open_id = None
        removed_open_ids = set()

        for op in ops:
            if op.get("kind") not in {"spreadsheet.open", "spreadsheet.write_cell"}:
                continue
            args = op.setdefault("arguments", {})
            args[target_key] = canonical_target
            if target_key == "file_path":
                args.pop("file_name", None)
            else:
                args.pop("file_path", None)

            if op.get("kind") == "spreadsheet.open":
                if primary_open_id is None:
                    primary_open_id = str(op.get("id", "open_spreadsheet"))
                else:
                    removed_open_ids.add(str(op.get("id", "")))

        if removed_open_ids and primary_open_id:
            normalized_ops = []
            for op in ops:
                op_id = str(op.get("id", ""))
                if op_id in removed_open_ids and op.get("kind") == "spreadsheet.open":
                    continue

                depends_on = list(op.get("depends_on", []) or [])
                if depends_on:
                    rewritten = []
                    for dep_id in depends_on:
                        dep_text = str(dep_id)
                        if dep_text in removed_open_ids:
                            dep_text = primary_open_id
                        if dep_text not in rewritten:
                            rewritten.append(dep_text)
                    op["depends_on"] = rewritten
                normalized_ops.append(op)
            ops = normalized_ops
        return ops

    def _ensure_from_operation_dependencies(self, ops: list) -> list:
        for op in ops:
            args = op.get("arguments", {}) or {}
            source_id = str(args.get("from_operation") or "").strip()
            if not source_id:
                continue
            depends_on = list(op.get("depends_on", []) or [])
            if source_id not in depends_on:
                depends_on.append(source_id)
            op["depends_on"] = depends_on
        return ops

    def _serialize_spreadsheet_writes(self, ops: list) -> list:
        writes_by_target: Dict[str, List[dict]] = {}

        for op in ops:
            if op.get("kind") != "spreadsheet.write_cell":
                continue
            args = op.get("arguments", {}) or {}
            target = str(args.get("file_path") or args.get("file_name") or "").strip()
            if not target:
                target = "__default_spreadsheet__"
            writes_by_target.setdefault(target, []).append(op)

        for write_ops in writes_by_target.values():
            previous_id = ""
            for op in write_ops:
                if previous_id:
                    depends_on = list(op.get("depends_on", []) or [])
                    if previous_id not in depends_on:
                        depends_on.append(previous_id)
                    op["depends_on"] = depends_on
                previous_id = str(op.get("id", "") or previous_id)

        return ops

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
        raise TaskPlanningError("Failed to parse JSON from model output")

    def _truncate_text(self, text: str, max_len: int = 200) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len] + "..."

    def _task_spec_to_dict(self, spec: TaskSpec) -> Dict[str, Any]:
        return {
            "summary": spec.summary,
            "success_criteria": spec.success_criteria,
            "operations": [
                {
                    "id": op.id,
                    "kind": op.kind,
                    "arguments": op.arguments,
                    "depends_on": op.depends_on,
                }
                for op in spec.operations
            ]
        }

    def _get_scheduler_nodes(self, task_spec: TaskSpec) -> List[SchedulerNode]:
        nodes = []
        for operation in task_spec.operations:
            nodes.append(
                SchedulerNode(
                    node_id=operation.id,
                    agent_type=self._agent_type_for(operation.kind),
                    action=operation.kind,
                    params=operation.arguments,
                    description=operation.description,
                    dependencies=operation.depends_on,
                    metadata={"risky": operation.risky},
                )
            )
        return nodes

    @staticmethod
    def _agent_type_for(kind: str) -> str:
        if kind.startswith("approval.") or kind.startswith("audit."):
            return "AuditAgent"
        if kind.startswith("decision."):
            return "DecisionAgent"
        if kind.startswith("memory."):
            return "MemoryAgent"
        return "ActionAgent"

    async def execute_task(
        self,
        instruction: str,
        task_spec: TaskSpec,
        action_agent: ActionAgent,
        audit_agent: AuditAgent,
        memory_agent: MemoryAgent,
        max_replans: int,
        replan_callback,
        should_cancel=None,
    ) -> ExecutionContext:
        current_task_spec = task_spec
        last_error = None

        def emit_progress(event_type: str, **payload: Any) -> None:
            if replan_callback is None:
                return
            try:
                replan_callback({"event": event_type, **payload})
            except Exception:
                return

        for attempt in range(max_replans + 1):
            if callable(should_cancel) and should_cancel():
                raise TaskExecutionError("Task cancelled by user.")
            scheduler = DAGScheduler()
            context = ExecutionContext(instruction=instruction, task_spec=current_task_spec)
            memory_agent.record_task_state(
                "planning",
                {
                    "attempt": attempt + 1,
                    "summary": current_task_spec.summary,
                    "operation_count": len(current_task_spec.operations),
                },
            )
            memory_agent.record(
                "task_lifecycle",
                self._task_lifecycle_event_payload(
                    instruction=instruction,
                    task_spec=current_task_spec,
                    attempt=attempt + 1,
                    lifecycle_status="planning",
                ),
            )

            logger.log("Task summary: {}".format(current_task_spec.summary), "blue")
            logger.log(
                "Planned operations: {}".format(
                    json.dumps(
                        [
                            {
                                "id": operation.id,
                                "kind": operation.kind,
                                "arguments": operation.arguments,
                                "depends_on": operation.depends_on,
                            }
                            for operation in current_task_spec.operations
                        ],
                        ensure_ascii=False,
                    )
                ),
                "gray",
            )
            emit_progress(
                "plan",
                attempt=attempt + 1,
                summary=current_task_spec.summary,
                operations=[
                    {
                        "id": operation.id,
                        "kind": operation.kind,
                        "description": operation.description,
                        "arguments": dict(operation.arguments),
                        "depends_on": operation.depends_on,
                        "risky": operation.risky,
                    }
                    for operation in current_task_spec.operations
                ],
            )
            memory_agent.record("task_spec", self._task_spec_payload(current_task_spec))
            memory_agent.record(
                "task_decision",
                self._task_decision_event_payload(
                    instruction=instruction,
                    task_spec=current_task_spec,
                    attempt=attempt + 1,
                ),
            )
            audit_agent.log_action(
                "task.decision",
                json.dumps(
                    {
                        "instruction": instruction,
                        "summary": current_task_spec.summary,
                        "decision_summary": current_task_spec.metadata.get("decision_summary", {}),
                        "operation_decisions": current_task_spec.metadata.get("operation_decisions", []),
                    },
                    ensure_ascii=False,
                ),
                "PLANNED",
            )

            for node in self._get_scheduler_nodes(current_task_spec):
                scheduler.add_node(node)

            async def executor(node: SchedulerNode) -> Any:
                if callable(should_cancel) and should_cancel():
                    raise TaskExecutionError("Task cancelled by user.")
                operation = next(item for item in current_task_spec.operations if item.id == node.node_id)
                operation_decision = self._operation_decision(current_task_spec, operation.id)
                decision_outcome = operation_decision.get("outcome", "register")
                logger.log(
                    "Executing {} with args {}".format(
                        operation.kind, json.dumps(operation.arguments, ensure_ascii=False)
                    ),
                    "yellow",
                )
                emit_progress(
                    "operation",
                    attempt=attempt + 1,
                    operation_id=operation.id,
                    kind=operation.kind,
                    description=operation.description,
                    status="running",
                )
                memory_agent.record_task_state(
                    "running_operation",
                    {
                        "attempt": attempt + 1,
                        "operation_id": operation.id,
                        "kind": operation.kind,
                        "decision_outcome": decision_outcome,
                    },
                )
                memory_agent.record(
                    "operation_lifecycle",
                    self._operation_lifecycle_event_payload(
                        instruction=instruction,
                        task_spec=current_task_spec,
                        operation=operation,
                        attempt=attempt + 1,
                        lifecycle_status="running",
                        decision_outcome=decision_outcome,
                    ),
                )
                details = json.dumps(operation.arguments, ensure_ascii=False)
                memory_agent.record(
                    "operation_decision",
                    self._operation_decision_event_payload(
                        instruction=instruction,
                        task_spec=current_task_spec,
                        operation=operation,
                        operation_decision=operation_decision,
                        attempt=attempt + 1,
                    ),
                )
                audit_agent.log_action(
                    "operation.decision",
                    json.dumps(
                        {
                            "operation_id": operation.id,
                            "kind": operation.kind,
                            "arguments": operation.arguments,
                            "decision": operation_decision,
                        },
                        ensure_ascii=False,
                    ),
                    "EXECUTING",
                )
                if decision_outcome == "risk":
                    audit_agent.log_action(operation.kind, details, "BLOCKED_BY_DECISION")
                    raise TaskExecutionError(
                        "Decision blocked operation {} as risk.".format(operation.id)
                    )
                if decision_outcome == "track_only":
                    memory_agent.record(
                        "decision_track_only",
                        {
                            "operation_id": operation.id,
                            "kind": operation.kind,
                            "details": operation.arguments,
                        },
                    )
                if decision_outcome == "confirm" or operation.risky:
                    authorized = audit_agent.request_authorization(operation.kind, details)
                    if not authorized:
                        raise TaskExecutionError("User rejected risky operation: {}".format(operation.id))

                if callable(should_cancel) and should_cancel():
                    raise TaskExecutionError("Task cancelled by user.")
                output = await action_agent.execute(operation, context)
                logger.log(
                    "Completed {} -> {}".format(operation.kind, self._short_output(output)),
                    "green",
                )
                emit_progress(
                    "operation",
                    attempt=attempt + 1,
                    operation_id=operation.id,
                    kind=operation.kind,
                    description=operation.description,
                    status="completed",
                    output=self._safe_output(output),
                )
                context.results[operation.id] = OperationResult(
                    operation_id=operation.id,
                    status=OperationStatus.COMPLETED,
                    output=output,
                )
                memory_agent.record_task_state(
                    "operation_completed",
                    {
                        "attempt": attempt + 1,
                        "operation_id": operation.id,
                        "kind": operation.kind,
                        "decision_outcome": decision_outcome,
                    },
                )
                memory_agent.record(
                    "operation_lifecycle",
                    self._operation_lifecycle_event_payload(
                        instruction=instruction,
                        task_spec=current_task_spec,
                        operation=operation,
                        attempt=attempt + 1,
                        lifecycle_status="completed",
                        decision_outcome=decision_outcome,
                    ),
                )
                audit_agent.log_action(
                    operation.kind,
                    json.dumps(
                        {
                            "operation_id": operation.id,
                            "decision": operation_decision,
                            "output": self._safe_output(output),
                        },
                        ensure_ascii=False,
                    ),
                    "COMPLETED",
                )
                return output

            results = await scheduler.execute_all(executor)
            for result in results.values():
                context.results[result.operation_id] = result
                if result.status != OperationStatus.COMPLETED:
                    emit_progress(
                        "operation",
                        attempt=attempt + 1,
                        operation_id=result.operation_id,
                        status=result.status.value,
                        error=result.error,
                    )
                operation = next(
                    (item for item in current_task_spec.operations if item.id == result.operation_id),
                    None,
                )
                memory_agent.record(
                    "operation_result",
                    self._operation_result_event_payload(
                        instruction=instruction,
                        task_spec=current_task_spec,
                        operation=operation,
                        result=result,
                        attempt=attempt + 1,
                    ),
                )

            failed_results = [
                result for result in results.values() if result.status == OperationStatus.FAILED
            ]
            if not failed_results:
                decision_summary = current_task_spec.metadata.get("decision_summary", {})
                memory_agent.record_task_state(
                    "completed",
                    {
                        "attempt": attempt + 1,
                        "summary": current_task_spec.summary,
                    },
                )
                memory_agent.record(
                    "task_lifecycle",
                    self._task_lifecycle_event_payload(
                        instruction=instruction,
                        task_spec=current_task_spec,
                        attempt=attempt + 1,
                        lifecycle_status="completed",
                        decision_summary=decision_summary,
                    ),
                )
                memory_agent.append_history(
                    {
                        "instruction": instruction,
                        "summary": current_task_spec.summary,
                        "status": "completed",
                        "operation_kinds": [operation.kind for operation in current_task_spec.operations],
                        "decision_outcome": decision_summary.get("dominant_outcome"),
                        "task_risk_level": decision_summary.get("task_risk_level", "none"),
                        "matched_rule_ids": decision_summary.get("matched_rule_ids", []),
                    }
                )
                memory_agent.record(
                    "task_execution",
                    self._task_execution_event_payload(
                        instruction=instruction,
                        task_spec=current_task_spec,
                        attempt=attempt + 1,
                        execution_status="completed",
                        decision_summary=decision_summary,
                    ),
                )
                audit_agent.log_action(
                    "task.execution",
                    json.dumps(
                        {
                            "instruction": instruction,
                            "summary": current_task_spec.summary,
                            "decision_summary": decision_summary,
                            "status": "completed",
                        },
                        ensure_ascii=False,
                    ),
                    "COMPLETED",
                )
                logger.log("Task finished.", "cyan")
                emit_progress(
                    "task",
                    attempt=attempt + 1,
                    status="completed",
                    summary=current_task_spec.summary,
                )
                # 返回执行结果字典供 DecisionAgent 评估
                return {
                    op_id: result.output for op_id, result in context.results.items()
                }

            first_failure = failed_results[0]
            last_error = "Operation {} failed: {}".format(
                first_failure.operation_id,
                first_failure.error or "unknown error",
            )
            logger.log(last_error, "red")
            emit_progress(
                "task",
                attempt=attempt + 1,
                status="failed_attempt",
                error=last_error,
            )
            memory_agent.record(
                "task_execution",
                self._task_execution_event_payload(
                    instruction=instruction,
                    task_spec=current_task_spec,
                    attempt=attempt + 1,
                    execution_status="failed_attempt",
                    decision_summary=current_task_spec.metadata.get("decision_summary", {}),
                    error=last_error,
                ),
            )
            audit_agent.log_action(
                "task.execution",
                json.dumps(
                    {
                        "instruction": instruction,
                        "summary": current_task_spec.summary,
                        "error": last_error,
                    },
                    ensure_ascii=False,
                ),
                "FAILED_ATTEMPT",
            )
            memory_agent.record_task_state(
                "failed_attempt",
                {
                    "attempt": attempt + 1,
                    "error": last_error,
                },
            )
            memory_agent.record(
                "task_lifecycle",
                self._task_lifecycle_event_payload(
                    instruction=instruction,
                    task_spec=current_task_spec,
                    attempt=attempt + 1,
                    lifecycle_status="failed_attempt",
                    decision_summary=current_task_spec.metadata.get("decision_summary", {}),
                    error=last_error,
                ),
            )

            if "Decision blocked operation" in last_error:
                decision_summary = current_task_spec.metadata.get("decision_summary", {})
                memory_agent.record_task_state(
                    "blocked_by_decision",
                    {
                        "attempt": attempt + 1,
                        "error": last_error,
                    },
                )
                memory_agent.record(
                    "task_lifecycle",
                    self._task_lifecycle_event_payload(
                        instruction=instruction,
                        task_spec=current_task_spec,
                        attempt=attempt + 1,
                        lifecycle_status="blocked_by_decision",
                        decision_summary=decision_summary,
                        error=last_error,
                    ),
                )
                memory_agent.append_history(
                    {
                        "instruction": instruction,
                        "summary": current_task_spec.summary,
                        "status": "blocked_by_decision",
                        "operation_kinds": [operation.kind for operation in current_task_spec.operations],
                        "error": last_error,
                        "decision_outcome": decision_summary.get("dominant_outcome"),
                        "task_risk_level": decision_summary.get("task_risk_level", "none"),
                        "matched_rule_ids": decision_summary.get("matched_rule_ids", []),
                    }
                )
                memory_agent.record(
                    "task_execution",
                    self._task_execution_event_payload(
                        instruction=instruction,
                        task_spec=current_task_spec,
                        attempt=attempt + 1,
                        execution_status="blocked_by_decision",
                        decision_summary=decision_summary,
                        error=last_error,
                    ),
                )
                audit_agent.log_action(
                    "task.execution",
                    json.dumps(
                        {
                            "instruction": instruction,
                            "summary": current_task_spec.summary,
                            "decision_summary": decision_summary,
                            "error": last_error,
                        },
                        ensure_ascii=False,
                    ),
                    "BLOCKED_BY_DECISION",
                )
                raise TaskExecutionError(last_error)

            # Only truly unrecoverable errors should skip replan and go to visual.
            # Path resolution errors ("not found", "does not exist") should be
            # handled by replanning with corrected paths, NOT by visual fallback.
            _UNRECOVERABLE_ERROR_PATTERNS = [
                "Failed to establish a new connection",
                "Connection refused",
                "unsupported operation",
            ]
            if any(pat in last_error for pat in _UNRECOVERABLE_ERROR_PATTERNS):
                if attempt < max_replans:
                    logger.log(
                        "Unrecoverable error detected, skipping replan and going to visual fallback.",
                        "magenta",
                    )

            if attempt >= max_replans or any(pat in last_error for pat in _UNRECOVERABLE_ERROR_PATTERNS):
                decision_summary = current_task_spec.metadata.get("decision_summary", {})
                logger.log("Planner switching to visual fallback...", "magenta")
                fallback_output = await action_agent.execute_visual_fallback(
                    instruction=instruction,
                    context=context,
                    failure_message=last_error,
                )
                memory_agent.record(
                    "visual_fallback_result",
                    {
                        "status": "completed",
                        "failure_message": last_error,
                        "output": self._safe_output(fallback_output),
                    },
                )
                memory_agent.record_task_state(
                    "completed_with_visual_fallback",
                    {
                        "attempt": attempt + 1,
                        "error": last_error,
                    },
                )
                memory_agent.record(
                    "task_lifecycle",
                    self._task_lifecycle_event_payload(
                        instruction=instruction,
                        task_spec=current_task_spec,
                        attempt=attempt + 1,
                        lifecycle_status="completed_with_visual_fallback",
                        decision_summary=decision_summary,
                        error=last_error,
                        fallback="visual",
                    ),
                )
                memory_agent.append_history(
                    {
                        "instruction": instruction,
                        "summary": current_task_spec.summary,
                        "status": "completed_with_visual_fallback",
                        "operation_kinds": [operation.kind for operation in current_task_spec.operations],
                        "error": last_error,
                        "decision_outcome": decision_summary.get("dominant_outcome"),
                        "task_risk_level": decision_summary.get("task_risk_level", "none"),
                        "matched_rule_ids": decision_summary.get("matched_rule_ids", []),
                    }
                )
                memory_agent.record(
                    "task_execution",
                    self._task_execution_event_payload(
                        instruction=instruction,
                        task_spec=current_task_spec,
                        attempt=attempt + 1,
                        execution_status="completed_with_visual_fallback",
                        decision_summary=decision_summary,
                        error=last_error,
                        fallback="visual",
                    ),
                )
                audit_agent.log_action(
                    "task.execution",
                    json.dumps(
                        {
                            "instruction": instruction,
                            "summary": current_task_spec.summary,
                            "decision_summary": decision_summary,
                            "error": last_error,
                            "fallback": "visual",
                        },
                        ensure_ascii=False,
                    ),
                    "COMPLETED_WITH_VISUAL_FALLBACK",
                )
                logger.log("Visual fallback finished.", "cyan")
                fallback_results = {
                    op_id: result.output
                    for op_id, result in context.results.items()
                    if result.status == OperationStatus.COMPLETED
                }
                fallback_results["__visual_fallback__"] = self._safe_output(fallback_output)
                return fallback_results

            logger.log("Planner retrying with failure context...", "magenta")
            current_task_spec = self.plan(
                instruction, previous_error=last_error, previous_plan=current_task_spec
            )

        raise TaskExecutionError(last_error or "Task execution failed.")

    @staticmethod
    def _short_output(output: Any) -> str:
        text = str(output)
        if len(text) > 200:
            return text[:200] + "..."
        return text

    @staticmethod
    def _safe_output(output: Any) -> Any:
        if output is None or isinstance(output, (str, int, float, bool)):
            return output
        if isinstance(output, dict):
            return {str(key): PlannerAgent._safe_output(value) for key, value in output.items()}
        if isinstance(output, (list, tuple, set)):
            return [PlannerAgent._safe_output(item) for item in output]
        return str(output)

    @staticmethod
    def _task_spec_payload(task_spec: TaskSpec) -> Dict[str, Any]:
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

    @staticmethod
    def _operation_decision(task_spec: TaskSpec, operation_id: str) -> Dict[str, Any]:
        for item in task_spec.metadata.get("operation_decisions", []):
            if item.get("operation_id") == operation_id:
                return item
        return {"operation_id": operation_id, "outcome": "register"}

    @staticmethod
    def _task_decision_event_payload(
        instruction: str,
        task_spec: TaskSpec,
        attempt: int,
    ) -> Dict[str, Any]:
        decision_summary = task_spec.metadata.get("decision_summary", {})
        return build_execution_event(
            event_name="task_decision",
            phase="planning",
            attempt=attempt,
            subject_type="task",
            subject_id=task_spec.summary,
            instruction=instruction,
            task_summary=task_spec.summary,
            outcome=decision_summary.get("dominant_outcome", "register"),
            risk_level=decision_summary.get("task_risk_level", "none"),
            decision_summary=decision_summary,
            operation_decisions=task_spec.metadata.get("operation_decisions", []),
        )

    @staticmethod
    def _operation_decision_event_payload(
        instruction: str,
        task_spec: TaskSpec,
        operation,
        operation_decision: Dict[str, Any],
        attempt: int,
    ) -> Dict[str, Any]:
        return build_execution_event(
            event_name="operation_decision",
            phase="execution",
            attempt=attempt,
            subject_type="operation",
            subject_id=operation.id,
            instruction=instruction,
            task_summary=task_spec.summary,
            outcome=operation_decision.get("outcome", "register"),
            risk_level=operation_decision.get("risk_level", "none"),
            operation_id=operation.id,
            operation_kind=operation.kind,
            arguments=operation.arguments,
            decision=operation_decision,
        )

    @staticmethod
    def _task_execution_event_payload(
        instruction: str,
        task_spec: TaskSpec,
        attempt: int,
        execution_status: str,
        decision_summary: Dict[str, Any],
        error: str = None,
        fallback: str = None,
    ) -> Dict[str, Any]:
        return build_execution_event(
            event_name="task_execution",
            phase="execution",
            attempt=attempt,
            subject_type="task",
            subject_id=task_spec.summary,
            instruction=instruction,
            task_summary=task_spec.summary,
            outcome=decision_summary.get("dominant_outcome", "register"),
            risk_level=decision_summary.get("task_risk_level", "none"),
            execution_status=execution_status,
            decision_summary=decision_summary,
            error=error,
            fallback=fallback,
        )

    @staticmethod
    def _operation_result_event_payload(
        instruction: str,
        task_spec: TaskSpec,
        operation,
        result: OperationResult,
        attempt: int,
    ) -> Dict[str, Any]:
        operation_kind = operation.kind if operation is not None else ""
        return build_execution_event(
            event_name="operation_result",
            phase="execution",
            attempt=attempt,
            subject_type="operation",
            subject_id=result.operation_id,
            instruction=instruction,
            task_summary=task_spec.summary,
            outcome="completed" if result.status == OperationStatus.COMPLETED else result.status.value,
            risk_level="none",
            operation_id=result.operation_id,
            operation_kind=operation_kind,
            execution_status=result.status.value,
            output=PlannerAgent._safe_output(result.output),
            error=result.error,
        )

    @staticmethod
    def _task_lifecycle_event_payload(
        instruction: str,
        task_spec: TaskSpec,
        attempt: int,
        lifecycle_status: str,
        decision_summary: Dict[str, Any] = None,
        error: str = None,
        fallback: str = None,
    ) -> Dict[str, Any]:
        summary = decision_summary or {}
        return build_execution_event(
            event_name="task_lifecycle",
            phase="execution" if lifecycle_status != "planning" else "planning",
            attempt=attempt,
            subject_type="task",
            subject_id=task_spec.summary,
            instruction=instruction,
            task_summary=task_spec.summary,
            outcome=summary.get("dominant_outcome", "register"),
            risk_level=summary.get("task_risk_level", "none"),
            lifecycle_status=lifecycle_status,
            decision_summary=summary or None,
            error=error,
            fallback=fallback,
        )

    @staticmethod
    def _operation_lifecycle_event_payload(
        instruction: str,
        task_spec: TaskSpec,
        operation,
        attempt: int,
        lifecycle_status: str,
        decision_outcome: str,
    ) -> Dict[str, Any]:
        return build_execution_event(
            event_name="operation_lifecycle",
            phase="execution",
            attempt=attempt,
            subject_type="operation",
            subject_id=operation.id,
            instruction=instruction,
            task_summary=task_spec.summary,
            outcome=decision_outcome,
            risk_level="none",
            operation_id=operation.id,
            operation_kind=operation.kind,
            lifecycle_status=lifecycle_status,
        )
