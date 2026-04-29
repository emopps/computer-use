from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from os_computer_use.desktop.file_tool import FileTool
from os_computer_use.desktop.interaction_resolver import InteractionResolver
from os_computer_use.desktop.ui_observer import UIObserver
from os_computer_use.desktop.visual_executor import VisualExecutor
from os_computer_use.runtime.capabilities import SUPPORTED_OPERATION_KINDS
from os_computer_use.runtime.task_models import ExecutionContext, OperationStatus

if TYPE_CHECKING:
    from os_computer_use.desktop.local_desktop import LocalDesktop


class TaskExecutionError(RuntimeError):
    pass


class UnsupportedOperationError(TaskExecutionError):
    pass


class ActionAgent:
    def __init__(
        self,
        desktop: "LocalDesktop",
        file_tool: FileTool,
        reasoning_model: Any = None,
        vision_model: Any = None,
    ):
        self.desktop = desktop
        self.file_tool = file_tool
        self.reasoning_model = reasoning_model
        self.vision_model = vision_model
        self.supported_kinds = set(SUPPORTED_OPERATION_KINDS)
        self._visual_executor = None
        self.ui_observer = UIObserver(desktop=self.desktop)
        self.interaction_resolver = InteractionResolver()
        self._browser_ui_lock = None
        # 失败计数器：同一操作类型连续失败次数，达到阈值后退回视觉
        self._failure_counts: Dict[str, int] = {}
        self._visual_fallback_threshold = 3  # 连续失败3次才退回视觉

    async def execute(self, operation, context: ExecutionContext) -> Any:
        kind = operation.kind
        args = dict(operation.arguments)
        before_hash = self._screenshot_hash()

        if kind == "filesystem.list":
            return self.file_tool.list_files(args["directory"], args.get("extensions"))
        if kind == "filesystem.open_path":
            path = self._resolve_file_manager_path(args, primary_key="path")
            success = await self.desktop.open_path(path)
            if not success:
                raise TaskExecutionError("Failed to open path: {}".format(path))
            output = {"path": path}
            self._validate_execution(kind, before_hash, output, args)
            return output
        if kind == "filesystem.create_folder":
            folder_path = self._resolve_file_manager_path(args, primary_key="path")
            created = self.file_tool.create_folder(folder_path)
            output = {"path": created}
            self._validate_execution(kind, before_hash, output, args)
            return output
        if kind == "filesystem.copy":
            src = self._resolve_file_manager_path(args, primary_key="src")
            dst_dir = self._resolve_file_manager_path(args, primary_key="dst_dir")
            copied = self.file_tool.copy_file(src, dst_dir, args.get("new_name"))
            output = {"src": src, "dst_path": copied}
            self._validate_execution(kind, before_hash, output, args)
            return output
        if kind == "filesystem.move":
            src = self._resolve_file_manager_path(args, primary_key="src")
            dst_dir = self._resolve_file_manager_path(args, primary_key="dst_dir")
            moved = self.file_tool.move_file(src, dst_dir, args.get("new_name"))
            output = {"src": src, "dst_path": moved}
            self._validate_execution(kind, before_hash, output, args)
            return output
        if kind == "filesystem.rename":
            src = self._resolve_file_manager_path(args, primary_key="src")
            renamed = self.file_tool.rename_file(src, args["new_name"])
            output = {"src": src, "dst_path": renamed}
            self._validate_execution(kind, before_hash, output, args)
            return output
        if kind == "filesystem.delete":
            target_path = self._resolve_file_manager_path(args, primary_key="path")
            deleted = self.file_tool.delete_path(target_path)
            output = {"path": deleted}
            self._validate_execution(kind, before_hash, output, args)
            return output
        if kind == "filesystem.write_text":
            file_path = self._resolve_text_file_path(args)
            text = self._resolve_text_from_args_or_operation(args, context)
            self.desktop.write_text_file(file_path, str(text))
            editor_synced = await self.desktop.sync_text_editor_content(file_path, str(text))
            if not editor_synced:
                raise TaskExecutionError("Text file was written on disk but editor view did not update: {}".format(file_path))
            output = {"file_path": file_path, "text": str(text)}
            self._validate_execution(kind, before_hash, output, args)
            return output
        if kind == "document.extract_text":
            return self.file_tool.parse_pdf(args["file_path"])
        if kind == "document.extract_structured":
            source_text = args.get("text")
            if source_text is None:
                source_operation_id = args.get("from_operation")
                if not source_operation_id:
                    raise TaskExecutionError(
                        "Operation {} requires either 'text' or 'from_operation'.".format(operation.id)
                    )
                source_result = context.results.get(source_operation_id)
                if not source_result or source_result.status != OperationStatus.COMPLETED:
                    raise TaskExecutionError(
                        "Operation {} could not access completed result from {}.".format(
                            operation.id, source_operation_id
                        )
                    )
                source_text = source_result.output
            return self.file_tool.extract_contract_elements(source_text)
        if kind == "browser.open":
            page_key = self._browser_page_key(operation, context)
            return await self._run_browser_operation(
                page_key=page_key,
                create_page=True,
                runner=lambda: self._execute_ui_operation(
                    kind=kind,
                    args=args,
                    context=context,
                    before_hash=before_hash,
                    primary_executor=lambda: self.desktop.open_browser(args["url"], page_key=page_key),
                ),
            )
        if kind == "browser.search":
            page_key = self._browser_page_key(operation, context)
            raw_result = await self._run_browser_operation(
                page_key=page_key,
                create_page=True,
                runner=lambda: self._execute_ui_operation(
                    kind=kind,
                    args=args,
                    context=context,
                    before_hash=before_hash,
                    primary_executor=lambda: self.desktop.browser_search(args["text"], page_key=page_key),
                ),
            )
            return self._normalize_browser_search_output(args["text"], raw_result)
        if kind == "browser.send":
            page_key = self._browser_page_key(operation, context)
            return await self._run_browser_operation(
                page_key=page_key,
                create_page=True,
                runner=lambda: self._execute_ui_operation(
                    kind=kind,
                    args=args,
                    context=context,
                    before_hash=before_hash,
                    primary_executor=lambda: self._send_browser_message(args, page_key=page_key),
                ),
            )
        if kind == "spreadsheet.open":
            file_path = self._resolve_spreadsheet_path(args, require_exists=False)
            return await self._execute_ui_operation(
                kind=kind,
                args=args,
                context=context,
                before_hash=before_hash,
                primary_executor=lambda: self._open_spreadsheet(file_path),
            )
        if kind == "spreadsheet.write_cell":
            # 统一解析 text / from_operation，避免把引用对象或 JSON 字符串原样写入单元格
            args["text"] = self._resolve_text_from_args_or_operation(args, context)
            file_path = self._resolve_spreadsheet_path(args, require_exists=False) if (args.get("file_path") or args.get("file_name")) else ""
            return await self._execute_ui_operation(
                kind=kind,
                args=args,
                context=context,
                before_hash=before_hash,
                primary_executor=lambda: self._write_spreadsheet_cell(file_path, args["cell"], args["text"]),
            )
        if kind == "command.run":
            command = self._normalize_platform_command(args["command"])
            background = command.startswith("xdg-open ")
            result = await self.desktop.commands.run(
                command,
                timeout=args.get("timeout", 15),
                background=background,
            )
            if self._is_command_failure(command, result):
                raise TaskExecutionError((result.stderr or "").strip())
            output = {
                "command": command,
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "background": background,
            }
            self._validate_execution(kind, before_hash, output, args)
            return output

        raise UnsupportedOperationError("Unsupported operation kind: {}".format(kind))

    async def _execute_ui_operation(
        self,
        *,
        kind: str,
        args: Dict[str, Any],
        context: ExecutionContext,
        before_hash: str,
        primary_executor,
    ) -> Any:
        observation = await self.ui_observer.capture()
        attempt_plan = self.interaction_resolver.build_attempt_plan(
            operation_kind=kind,
            instruction=context.instruction,
            args=args,
            observation=observation,
        )
        failures: List[str] = []

        # 检查是否已达到视觉退回阈值
        fail_count = self._failure_counts.get(kind, 0)
        should_try_visual = fail_count >= self._visual_fallback_threshold

        for attempt in attempt_plan:
            mode = attempt.get("mode", "direct")

            # 如果未达到视觉退回阈值，跳过视觉模式
            if mode == "visual" and not should_try_visual:
                continue
            # 如果已达到视觉退回阈值，跳过 DOM/ATSPI（已证明无效）
            if mode != "visual" and should_try_visual:
                continue

            try:
                # DOM 驱动交互：使用 Playwright 配合 DOM 元素信息
                if mode == "dom" and observation.get("dom", {}).get("available"):
                    output = await self._execute_dom_driven(kind, args, observation)
                    if output is not None:
                        self._validate_execution(kind, before_hash, output, args)
                        self._failure_counts[kind] = 0  # 成功则重置计数
                        return output

                # ATSPI 驱动交互：使用无障碍元素
                if mode == "atspi" and observation.get("atspi", {}).get("available"):
                    output = await self._execute_atspi_driven(kind, args, observation)
                    if output is not None:
                        self._validate_execution(kind, before_hash, output, args)
                        self._failure_counts[kind] = 0  # 成功则重置计数
                        return output

                # 直接执行（xdotool/Playwright API 调用）
                output = await primary_executor()
                self._validate_execution(kind, before_hash, output, args)
                return output
            except Exception as exc:
                failures.append(
                    "{} attempt failed: {} ({})".format(
                        mode,
                        exc,
                        attempt.get("reason", "no reason"),
                    )
                )

        # 视觉降级：仅在所有 DOM/ATSPI/直接策略都失败时使用
        if any(item.get("mode") == "visual" for item in attempt_plan):
            if self.reasoning_model is not None and self.vision_model is not None:
                fallback_instruction = self._build_visual_operation_instruction(
                    kind=kind,
                    args=args,
                    context=context,
                )
                output = await self.execute_visual_fallback(
                    instruction=fallback_instruction,
                    context=context,
                    failure_message=" | ".join(failures) if failures else "non-visual strategies exhausted",
                )
                self._validate_execution(kind, before_hash, output, args)
                return output

        raise TaskExecutionError(
            "Operation {} exhausted non-visual strategies before visual escalation: {}".format(
                kind,
                " | ".join(failures) if failures else "no usable strategies",
            )
        )

    async def _execute_dom_driven(
        self, kind: str, args: Dict[str, Any], observation: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """使用观察中的 DOM 元素引导 Playwright 交互。
        如果 DOM 驱动方式不适用于此操作，返回 None。"""
        dom = observation.get("dom", {})
        elements = dom.get("elements", [])
        page = getattr(self.desktop, "_page", None)
        if not page:
            return None

        if kind == "browser.search":
            return None

        if kind == "browser.search":
            text = args.get("text", "")
            if not text:
                return None
            # 从 DOM 元素中查找搜索输入框
            search_input = None
            for el in elements:
                if el.get("role") == "input" and el.get("tag") in ("input", "textarea"):
                    selector = el.get("selector", "")
                    if selector:
                        search_input = selector
                        break
            if not search_input:
                # 降级到已知选择器
                for sel in ["#kw", "#chat-textarea", "input[name='wd']", "textarea[name='wd']", "input[type='text']"]:
                    try:
                        loc = page.locator(sel).first
                        if await loc.is_visible(timeout=1000):
                            search_input = sel
                            break
                    except Exception:
                        continue

            if search_input:
                try:
                    loc = page.locator(search_input).first
                    await loc.click()
                    await loc.fill("")
                    await loc.type(text, delay=30)
                    # 尝试点击搜索按钮
                    search_btn = None
                    for el in elements:
                        if el.get("role") == "button" and any(
                            kw in (el.get("name") or "").lower()
                            for kw in ["搜索", "search", "baidu", "su", "提交"]
                        ):
                            btn_sel = el.get("selector", "")
                            if btn_sel:
                                search_btn = btn_sel
                                break
                    if search_btn:
                        await page.locator(search_btn).first.click(timeout=3000)
                    else:
                        await page.keyboard.press("Enter")
                    await asyncio.sleep(2)
                    return {"text": text, "method": "dom_driven"}
                except Exception:
                    pass

        elif kind == "browser.open":
            url = args.get("url", "")
            if url and page:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    return {"url": url, "method": "dom_driven"}
                except Exception:
                    pass

        return None

    async def _execute_atspi_driven(
        self, kind: str, args: Dict[str, Any], observation: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """使用观察中的 ATSPI 元素引导桌面交互。
        如果 ATSPI 驱动方式不适用，返回 None。"""
        atspi = observation.get("atspi", {})
        elements = atspi.get("elements", [])

        # 表格操作不支持 ATSPI（WPS 不暴露无障碍树）
        # 但其他桌面应用可能支持
        if kind.startswith("spreadsheet"):
            return None

        # 对于有 ATSPI 元素的桌面应用，尝试按坐标点击
        if kind == "command.run" and elements:
            # 可使用 ATSPI 元素坐标点击 UI 元素
            pass

        return None

    async def _open_spreadsheet(self, file_path: str) -> Dict[str, Any]:
        success = await self.desktop.open_wps_file(file_path)
        if not success:
            raise TaskExecutionError("Failed to open spreadsheet: {}".format(file_path))
        return {"opened": file_path}

    async def _write_spreadsheet_cell(self, file_path: str, cell: str, text: str) -> Dict[str, Any]:
        success = self.desktop.wps_input_cell(cell, text)
        if not success:
            raise TaskExecutionError(
                "Failed to write to spreadsheet cell {} with current desktop state.".format(cell)
            )
        return {"file_path": file_path, "cell": cell, "text": text}

    def _ensure_browser_ui_lock(self):
        if self._browser_ui_lock is None:
            self._browser_ui_lock = asyncio.Lock()
        return self._browser_ui_lock

    async def _run_browser_operation(self, page_key: str, create_page: bool, runner):
        async with self._ensure_browser_ui_lock():
            await self.desktop.bind_browser_page(page_key=page_key, create=create_page)
            return await runner()

    def _browser_page_key(self, operation, context: ExecutionContext) -> str:
        cache = context.artifacts.setdefault("browser_page_keys", {})
        if operation.id in cache:
            return str(cache[operation.id])

        operations = {item.id: item for item in context.task_spec.operations}
        visiting = set()

        def resolve(op_id: str) -> str:
            if op_id in cache:
                return str(cache[op_id])
            if op_id in visiting:
                return op_id
            visiting.add(op_id)
            current = operations.get(op_id)
            if current is None or not str(current.kind).startswith("browser."):
                visiting.discard(op_id)
                return op_id
            browser_deps = []
            for dep_id in current.depends_on:
                dep = operations.get(dep_id)
                if dep is not None and str(dep.kind).startswith("browser."):
                    browser_deps.append(dep_id)
            if not browser_deps:
                cache[op_id] = op_id
                visiting.discard(op_id)
                return op_id
            root = resolve(browser_deps[0])
            cache[op_id] = root
            visiting.discard(op_id)
            return root

        return resolve(operation.id)

    async def _send_browser_message(self, args: Dict[str, Any], page_key: str = "") -> Dict[str, Any]:
        to = str(args.get("to", "") or "")
        subject = str(args.get("subject", "") or "")
        body = str(args.get("body", "") or "")
        attachments = list(args.get("attachments", []) or [])
        success = await self.desktop.browser_send(
            to,
            subject=subject,
            body=body,
            attachments=attachments,
            page_key=page_key,
        )
        if not success:
            raise TaskExecutionError("Failed to send browser message with current page state.")
        return {
            "to": to,
            "subject": subject,
            "body": body,
            "attachments": attachments,
        }

    def _normalize_browser_search_output(self, query: str, raw_result: Any) -> Dict[str, Any]:
        raw_text = str(raw_result or "").strip()
        filtered_text = raw_text
        useful = bool(raw_text)
        reason = ""

        if self.reasoning_model is not None and raw_text:
            distilled = self._distill_browser_search_result(query, raw_text)
            if distilled:
                candidate = str(distilled.get("answer", "") or "").strip()
                if candidate:
                    filtered_text = candidate
                useful = bool(distilled.get("useful", candidate))
                reason = str(distilled.get("reason", "") or "").strip()

        return {
            "query": query,
            "text": filtered_text,
            "raw_text": raw_text,
            "useful": useful,
            "reason": reason,
        }

    def _distill_browser_search_result(self, query: str, raw_text: str) -> Dict[str, Any]:
        prompt = (
            "You are filtering browser search output for a desktop computer-use agent.\n"
            "Decide whether the extracted text contains a direct, task-relevant answer to the user's query.\n"
            "Produce a concise, natural-language answer in the same language as the user's query.\n"
            "Remove noise, duplicate fragments, navigation labels, timestamps, hourly breakdowns, SEO fragments, and unrelated details unless the user explicitly asked for them.\n"
            "Return strict JSON only with keys useful, answer, reason.\n\n"
            "Rules:\n"
            "1. Preserve only information that directly helps answer the query.\n"
            "2. If the query is broad, provide a concise summary instead of verbatim snippets.\n"
            "3. For weather queries, summarize location, condition, current temperature, and high/low when available. Drop hourly timelines unless explicitly requested.\n"
            "4. Keep the answer readable as file content. Do not output keyword soup or compressed fragments.\n"
            "5. If the text is insufficient or mostly noise, set useful=false and explain why.\n\n"
            'JSON schema: {"useful": true, "answer": "...", "reason": "..."}\n\n'
            "User query:\n"
            f"{query}\n\n"
            "Extracted text:\n"
            f"{raw_text}"
        )
        try:
            response = self.reasoning_model.call([{"role": "user", "content": prompt}])
            parsed = self._extract_json_object(response)
            if isinstance(parsed, dict):
                return parsed
            fallback_answer = self._extract_answer_text(response)
            if fallback_answer:
                return {
                    "useful": True,
                    "answer": fallback_answer,
                    "reason": "model_returned_plain_text_summary",
                }
        except Exception:
            return {}
        return {}

    @staticmethod
    def _extract_json_object(text: Any) -> Dict[str, Any]:
        payload = str(text or "").strip()
        if not payload:
            return {}
        try:
            parsed = json.loads(payload)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", payload, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                pass
        match = re.search(r"\{.*\}", payload, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                pass
        return {}

    @staticmethod
    def _extract_answer_text(text: Any) -> str:
        payload = str(text or "").strip()
        if not payload:
            return ""
        payload = re.sub(r"^```(?:json)?", "", payload).strip()
        payload = re.sub(r"```$", "", payload).strip()
        if payload.startswith("{") and payload.endswith("}"):
            return ""
        lines = [line.strip() for line in payload.splitlines() if line.strip()]
        if not lines:
            return ""
        compact = " ".join(lines)
        compact = re.sub(r"\s+", " ", compact).strip()
        return compact

    @staticmethod
    def _build_visual_operation_instruction(
        *,
        kind: str,
        args: Dict[str, Any],
        context: ExecutionContext,
    ) -> str:
        if kind == "browser.open":
            return "Open browser {}".format(args.get("url", "https://www.baidu.com"))
        if kind == "browser.search":
            return "Search browser for {}".format(args.get("text", ""))
        if kind == "browser.send":
            return "Send browser message to {}".format(args.get("to", "")).strip()
        if kind == "spreadsheet.open":
            target = args.get("file_path") or args.get("file_name") or ""
            return "Open spreadsheet {}".format(target).strip()
        if kind == "spreadsheet.write_cell":
            target = args.get("file_path") or args.get("file_name") or ""
            return "Open {} and write {} to {} cell".format(
                target,
                args.get("text", ""),
                args.get("cell", ""),
            ).strip()
        return context.instruction

    async def execute_visual_fallback(
        self,
        instruction: str,
        context: ExecutionContext,
        failure_message: str,
    ) -> Any:
        if self.reasoning_model is None or self.vision_model is None:
            raise TaskExecutionError(
                "Visual fallback is unavailable because the required models were not configured."
            )
        if self._visual_executor is None:
            self._visual_executor = VisualExecutor(
                desktop=self.desktop,
                reasoning_model=self.reasoning_model,
                vision_model=self.vision_model,
            )
        return await self._visual_executor.run(
            instruction=instruction,
            failure_context=failure_message,
        )

    def _resolve_text_from_args_or_operation(self, args: Dict[str, Any], context: ExecutionContext) -> str:
        """从 args['text'] 或 from_operation 前序操作结果获取文本。
        支持 browser.search → filesystem.write_cell / spreadsheet.write_cell 的结果传递。"""
        text = args.get("text")
        source_op_id = self._extract_from_operation_reference(text) or args.get("from_operation")
        if source_op_id and context:
            source_result = context.results.get(source_op_id)
            if source_result and source_result.status == OperationStatus.COMPLETED:
                output = source_result.output
                # output 可能是字符串或字典
                if isinstance(output, str):
                    return output
                if isinstance(output, dict):
                    # 优先取 text 字段，否则取整个 output 的字符串
                    return output.get("text", str(output))
                return str(output)
        if text is not None:
            return str(text)
        return str(args.get("text", ""))

    @staticmethod
    def _extract_from_operation_reference(value: Any) -> str:
        if isinstance(value, dict):
            source_id = value.get("from_operation") or value.get("source_operation")
            return str(source_id).strip() if source_id else ""

        if not isinstance(value, str):
            return ""

        text = value.strip()
        if not text:
            return ""

        plain_match = re.match(r"^\[?from_operation[:\s]+([A-Za-z0-9_\-]+)\]?$", text)
        if plain_match:
            return plain_match.group(1).strip()

        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
            except Exception:
                return ""
            if isinstance(parsed, dict):
                source_id = parsed.get("from_operation") or parsed.get("source_operation")
                return str(source_id).strip() if source_id else ""

        return ""

    def _resolve_spreadsheet_path(self, args: Dict[str, Any], *, require_exists: bool = True) -> str:
        file_path = (args.get("file_path") or "").strip()
        file_name = (args.get("file_name") or "").strip()
        desktop_dirs = self._desktop_directories()

        def _get_possible_names(name: str) -> List[str]:
            if not name:
                return []
            # 规范化：去掉模型错误添加的"桌面/"或"Desktop/"前缀
            clean_name = re.sub(r"^(桌面|Desktop)/", "", name)
            basename = os.path.basename(clean_name)
            name_no_ext, _ = os.path.splitext(basename)
            # 同时去掉 basename 开头的"桌面"
            # （模型在指令为"桌面 工作簿1.et"时会幻觉出"桌面工作簿1.et"）
            stripped_basename = re.sub(r"^(桌面|Desktop)", "", basename)
            stripped_no_ext = re.sub(r"^(桌面|Desktop)", "", name_no_ext)
            candidates = [
                basename,
                stripped_basename if stripped_basename != basename else "",
                f"{name_no_ext}.et",
                f"{name_no_ext}.xlsx",
                f"{name_no_ext}.xls",
                f"{stripped_no_ext}.et" if stripped_no_ext != name_no_ext else "",
                f"{stripped_no_ext}.xlsx" if stripped_no_ext != name_no_ext else "",
                f"{stripped_no_ext}.xls" if stripped_no_ext != name_no_ext else "",
                clean_name,
            ]
            return list(dict.fromkeys(c for c in candidates if c))

        if file_path:
            # 1. Try absolute path
            resolved = os.path.abspath(os.path.expanduser(file_path))
            if os.path.exists(resolved):
                return resolved
            
            # 2. Try variations in Desktop directories
            possible_names = _get_possible_names(file_path)
            candidates = [resolved]
            for desktop_dir in desktop_dirs:
                for name in possible_names:
                    candidates.append(os.path.abspath(os.path.join(desktop_dir, name)))
            
            for candidate in candidates:
                if os.path.exists(candidate):
                    return candidate
            
            if not require_exists:
                return resolved
                
            raise TaskExecutionError(
                "Spreadsheet file not found. Checked variations of: {}".format(file_path)
            )

        if file_name:
            possible_names = _get_possible_names(file_name)
            candidates = []
            for name in possible_names:
                candidates.append(os.path.abspath(os.path.expanduser(name)))
                candidates.append(os.path.abspath(os.path.join(os.getcwd(), name)))
                for desktop_dir in desktop_dirs:
                    candidates.append(os.path.abspath(os.path.join(desktop_dir, name)))
            
            for candidate in candidates:
                if os.path.exists(candidate):
                    return candidate
            raise TaskExecutionError(
                "Spreadsheet file not found. Checked: {}".format(", ".join(possible_names))
            )

        raise TaskExecutionError("Spreadsheet operation requires file_path or file_name.")

    def _resolve_text_file_path(self, args: Dict[str, Any]) -> str:
        file_path = args.get("file_path")
        if not file_path:
            raise TaskExecutionError("filesystem.write_text requires file_path.")
        return self._normalize_desktop_path(file_path)

    def _resolve_file_manager_path(self, args: Dict[str, Any], primary_key: str = "path") -> str:
        path_value = args.get(primary_key)
        if not path_value:
            raise TaskExecutionError("{} requires {}.".format(primary_key, primary_key))
        return self._normalize_desktop_path(str(path_value))

    def _normalize_platform_command(self, command: str) -> str:
        normalized = (command or "").strip()
        if not normalized:
            raise TaskExecutionError("command.run requires a non-empty command.")

        url_match = re.search(r"(https?://[^\s'\"]+)", normalized)
        if normalized.lower().startswith("open -a ") and url_match:
            return "xdg-open {}".format(url_match.group(1))
        if normalized.lower().startswith("start ") and url_match:
            return "xdg-open {}".format(url_match.group(1))

        open_match = re.match(r"^(open|start)\s+(.+)$", normalized, flags=re.IGNORECASE)
        if open_match:
            tail = open_match.group(2).strip()
            tail_url_match = re.search(r"(https?://[^\s'\"]+)", tail)
            if tail_url_match:
                return "xdg-open {}".format(tail_url_match.group(1))
            return "xdg-open {}".format(tail)
        return normalized

    def _is_command_failure(self, command: str, result: Any) -> bool:
        stderr = (result.stderr or "").strip()
        if not stderr:
            return False
        if command.startswith("xdg-open "):
            lower_stderr = stderr.lower()
            if "gtk-message" in lower_stderr or "failed to load module" in lower_stderr:
                return False
            if "超时" in stderr or "timeout" in lower_stderr or ("执行" in stderr and "超时" in stderr):
                return False
        return True

    def _validate_execution(self, kind: str, before_hash: str, output: Any, args: Dict[str, Any]) -> None:
        wait_seconds = 0.0
        if kind in ("browser.open", "browser.search", "command.run"):
            wait_seconds = 1.2
        elif kind in ("spreadsheet.open",):
            wait_seconds = 0.5
        after_hash = self._screenshot_hash(wait_seconds=wait_seconds)
        screen_changed = before_hash != after_hash

        if kind == "filesystem.write_text":
            path = self._resolve_text_file_path(args)
            if not os.path.exists(path):
                raise TaskExecutionError("Text file was not created: {}".format(path))
            if isinstance(output, dict):
                expected_text = str(output.get("text", "") or "")
            else:
                expected_text = str(args.get("text", "") or "")
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    actual_text = fh.read()
            except Exception as exc:
                raise TaskExecutionError("Text file could not be read back: {}".format(exc))
            if self._normalize_text_content(actual_text) != self._normalize_text_content(expected_text):
                raise TaskExecutionError(
                    "Text file content mismatch for {}: expected {!r}, got {!r}".format(
                        path, expected_text, actual_text
                    )
                )
            return

        if kind == "filesystem.open_path":
            if not screen_changed:
                raise TaskExecutionError("Open path did not change the desktop state.")
            return

        if kind == "filesystem.create_folder":
            path = self._resolve_file_manager_path(args, primary_key="path")
            if not os.path.isdir(path):
                raise TaskExecutionError("Folder was not created: {}".format(path))
            return

        if kind == "filesystem.copy":
            dst_dir = self._resolve_file_manager_path(args, primary_key="dst_dir")
            new_name = args.get("new_name") or os.path.basename(str(args.get("src", "") or ""))
            dst_path = os.path.join(dst_dir, str(new_name))
            if not os.path.exists(dst_path):
                raise TaskExecutionError("Copy target was not created: {}".format(dst_path))
            return

        if kind == "filesystem.move":
            dst_dir = self._resolve_file_manager_path(args, primary_key="dst_dir")
            new_name = args.get("new_name") or os.path.basename(str(args.get("src", "") or ""))
            dst_path = os.path.join(dst_dir, str(new_name))
            if not os.path.exists(dst_path):
                raise TaskExecutionError("Move target was not created: {}".format(dst_path))
            return

        if kind == "filesystem.rename":
            src = self._resolve_file_manager_path(args, primary_key="src")
            dst_path = os.path.join(os.path.dirname(src), str(args.get("new_name", "") or ""))
            if not os.path.exists(dst_path):
                raise TaskExecutionError("Rename target was not created: {}".format(dst_path))
            return

        if kind == "filesystem.delete":
            path = self._resolve_file_manager_path(args, primary_key="path")
            if os.path.exists(path):
                raise TaskExecutionError("Path was not deleted: {}".format(path))
            return

        if kind == "spreadsheet.open":
            if not screen_changed:
                raise TaskExecutionError("Spreadsheet open did not change the desktop state.")
            return

        if kind == "spreadsheet.write_cell":
            return

        if kind == "browser.open":
            if not screen_changed and not getattr(output, "url", None):
                raise TaskExecutionError("Browser open did not produce a detectable state change.")
            return

        if kind == "browser.search":
            if not screen_changed and str(output).strip() != "搜索完成":
                raise TaskExecutionError("Browser search did not produce a detectable state change.")
            return

        if kind == "browser.send":
            if not screen_changed and not isinstance(output, dict):
                raise TaskExecutionError("Browser send did not produce a detectable state change.")
            return

        if kind == "command.run":
            stdout = ""
            stderr = ""
            if isinstance(output, dict):
                stdout = str(output.get("stdout", "") or "")
                stderr = str(output.get("stderr", "") or "")
            if not screen_changed and not stdout and stderr:
                raise TaskExecutionError("Command execution did not produce a detectable state change.")
            return

    def _normalize_desktop_path(self, file_path: str) -> str:
        expanded = os.path.abspath(os.path.expanduser(file_path))
        desktop_dir = self._preferred_desktop_directory()
        home_dir = os.path.abspath(os.path.expanduser("~"))
        desktop_aliases = [
            os.path.join(home_dir, "Desktop"),
            os.path.join(home_dir, "桌面"),
        ]

        for alias in desktop_aliases:
            alias = os.path.abspath(alias)
            if expanded == alias or expanded.startswith(alias + os.sep):
                suffix = expanded[len(alias):].lstrip(os.sep)
                return os.path.abspath(os.path.join(desktop_dir, suffix))
        return expanded

    def _preferred_desktop_directory(self) -> str:
        directories = self._desktop_directories()
        if directories:
            return directories[0]
        return os.path.abspath(os.path.expanduser("~/桌面"))

    @staticmethod
    def _normalize_text_content(text: str) -> str:
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        return normalized.rstrip("\n")

    def _screenshot_hash(self, wait_seconds: float = 0.0) -> str:
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        try:
            image = self.desktop.screenshot()
            if hasattr(image, "tobytes"):
                payload = image.tobytes()
            else:
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                payload = buffer.getvalue()
            return hashlib.md5(payload).hexdigest()
        except Exception:
            return "screenshot-unavailable"

    def _desktop_directories(self) -> List[str]:
        directories = []
        try:
            result = subprocess.run(
                ["xdg-user-dir", "DESKTOP"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            desktop_dir = (result.stdout or "").strip()
            if desktop_dir:
                directories.append(os.path.expanduser(desktop_dir))
        except Exception:
            pass

        for candidate in ["~/Desktop", "~/桌面"]:
            directories.append(os.path.expanduser(candidate))

        seen = set()
        ordered = []
        for directory in directories:
            normalized = os.path.abspath(directory)
            if normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return ordered
