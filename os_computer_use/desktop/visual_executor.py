from __future__ import annotations

import asyncio
import datetime
import io
import json
import os
import re
import subprocess
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from os_computer_use.logging import logger

try:
    from os_computer_use.desktop.atspi_provider import ATSPIProvider
except Exception:
    ATSPIProvider = None

if TYPE_CHECKING:
    from os_computer_use.desktop.local_desktop import LocalDesktop


class VisualExecutionError(RuntimeError):
    pass


def parse_first_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    # Remove markdown code blocks if present
    cleaned = re.sub(r"```json\s*(.*?)\s*```", r"\1", text, flags=re.DOTALL)
    cleaned = re.sub(r"```\s*(.*?)\s*```", r"\1", cleaned, flags=re.DOTALL)
    cleaned = cleaned.strip()

    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


class VisualExecutor:
    ACTION_MENU = {
        "vision": "Observe the current desktop state with the vision model.",
        "activate_window": "Activate a visible desktop window by title pattern.",
        "click": "Click screen coordinates x,y.",
        "type_text": "Type text into the focused input.",
        "press_key": "Press a keyboard key or hotkey like enter or ctrl-l.",
        "open_browser": "Open the browser with a URL.",
        "browser_search": "Search text in the active browser page.",
        "open_app": "Open a desktop app or document.",
        "input_cell": "Write text into a spreadsheet cell.",
        "finish": "Finish the task.",
    }
    PURE_VISUAL_ACTION_MENU = {
        "click": "Click screen coordinates x,y with left or right button.",
        "type_text": "Type text into the focused input.",
        "press_key": "Press a keyboard key or hotkey like enter or ctrl-l.",
        "finish": "Finish the task.",
    }

    def __init__(
        self,
        desktop: LocalDesktop,
        reasoning_model: Any,
        vision_model: Any,
        atspi_provider: Optional[ATSPIProvider] = None,
        pure_visual_actions: bool = False,
    ):
        self.desktop = desktop
        self.reasoning_model = reasoning_model
        self.vision_model = vision_model
        self.atspi_provider = atspi_provider or (ATSPIProvider() if ATSPIProvider else None)
        self.pure_visual_actions = bool(pure_visual_actions)
        self.context_history: List[str] = []
        self.last_observation: Dict[str, Any] = {}
        self.max_steps = 8
        self.screenshots_dir = os.path.join(os.getcwd(), "screenshots")
        os.makedirs(self.screenshots_dir, exist_ok=True)
        self.repetitive_action_count = 0
        self.last_action_signature = ""
        self._folder_creation_target = ""
        self._folder_creation_preexisting = False

    async def run(self, instruction: str, failure_context: str = "") -> Dict[str, Any]:
        text = (instruction or "").strip()
        if not text:
            raise VisualExecutionError("Visual fallback requires a non-empty instruction.")
        self._folder_creation_target = self._extract_folder_creation_target(text)
        self._folder_creation_preexisting = self._folder_exists(self._folder_creation_target)
        initial_screenshot = self.desktop.capture_visual_screenshot()
        initial_screenshot_path = self._save_screenshot(initial_screenshot)

        logger.log("Switching to visual fallback execution.", "magenta")
        logger.log("Visual fallback captured screenshot: {}".format(initial_screenshot_path), "gray")

        deterministic = self._extract_deterministic_actions(text)
        if self.pure_visual_actions:
            deterministic = [
                action
                for action in deterministic
                if action.get("action") in self._available_action_menu()
            ]
        for action in deterministic:
            result = await self._execute_action(action)
            self.context_history.append(
                "{}({}) -> {}".format(
                    action["action"],
                    json.dumps(action.get("params", {}), ensure_ascii=False),
                    result,
                )
            )

        if deterministic and self._is_deterministic_complete(text):
            return {
                "mode": "visual_fallback",
                "completed": True,
                "history": list(self.context_history),
                "initial_screenshot_path": initial_screenshot_path,
            }

        for _ in range(self.max_steps):
            self.last_observation = await self._observe(text, failure_context)
            
            # Use the decision from the vision model observation if available
            action_name = self.last_observation.get("action", "vision")
            params = self._hydrate_action_params(
                action_name,
                self.last_observation.get("params", {}),
                self.last_observation,
            )
            is_complete = self.last_observation.get("is_complete", False)
            
            if is_complete or action_name == "finish":
                return {
                    "mode": "visual_fallback",
                    "completed": True,
                    "history": list(self.context_history),
                    "observation": self.last_observation,
                    "answer": self.last_observation.get("summary", "Visual fallback completed."),
                    "initial_screenshot_path": initial_screenshot_path,
                }
            
            # If vision model didn't provide a clear action, use reasoning model as fallback
            if action_name == "vision" or not action_name:
                if self.last_observation.get("source") == "atspi":
                    logger.log("AT-SPI 观察未给出可执行动作，升级为截图视觉分析。", "yellow")
                    screenshot_image = self.desktop.capture_visual_screenshot()
                    screenshot_path = self._save_screenshot(screenshot_image)
                    self.last_observation = await self._observe_from_screenshot(
                        instruction=text,
                        failure_context=failure_context,
                        screenshot_image=screenshot_image,
                        screenshot_path=screenshot_path,
                        system_state=self._system_state_text(),
                        accessibility=self._accessibility_snapshot(),
                    )
                if self._folder_creation_target:
                    decision = self._decide_folder_creation_next(text)
                else:
                    decision = self._decide_next(text, failure_context)
                action_name = decision.get("action", "finish")
                params = self._hydrate_action_params(
                    action_name,
                    decision.get("params", {}),
                    self.last_observation,
                )

            if not self._is_action_actionable(action_name, params):
                if self.last_observation.get("source") == "atspi":
                    logger.log("AT-SPI 动作信息不足，升级为截图视觉分析。", "yellow")
                    screenshot_image = self.desktop.capture_visual_screenshot()
                    screenshot_path = self._save_screenshot(screenshot_image)
                    self.last_observation = await self._observe_from_screenshot(
                        instruction=text,
                        failure_context=failure_context,
                        screenshot_image=screenshot_image,
                        screenshot_path=screenshot_path,
                        system_state=self._system_state_text(),
                        accessibility=self._accessibility_snapshot(),
                    )
                if self._folder_creation_target:
                    decision = self._decide_folder_creation_next(text)
                else:
                    decision = self._decide_next(text, failure_context)
                action_name = decision.get("action", "finish")
                params = self._hydrate_action_params(
                    action_name,
                    decision.get("params", {}),
                    self.last_observation,
                )
                if not self._is_action_actionable(action_name, params):
                    action_name = "finish"
                    params = {}
            
            # Detect repetitive actions to prevent infinite loops
            action_signature = "{}({})".format(action_name, json.dumps(params, sort_keys=True))
            if action_signature == self.last_action_signature:
                self.repetitive_action_count += 1
            else:
                self.repetitive_action_count = 0
                self.last_action_signature = action_signature
            
            if self.repetitive_action_count >= 2:
                logger.log("Repetitive action detected, forcing finish to avoid loop.", "yellow")
                action_name = "finish"

            if action_name == "finish":
                return {
                    "mode": "visual_fallback",
                    "completed": True,
                    "history": list(self.context_history),
                    "observation": self.last_observation,
                    "answer": self.last_observation.get("summary", "Visual fallback completed."),
                    "initial_screenshot_path": initial_screenshot_path,
                }

            result = await self._execute_action({"action": action_name, "params": params})
            self.context_history.append(
                "{}({}) -> {}".format(
                    action_name,
                    json.dumps(params, ensure_ascii=False),
                    result,
                )
            )

        raise VisualExecutionError(
            "Visual fallback reached the step limit without finishing the task."
        )

    def _extract_deterministic_actions(self, instruction: str) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []

        url_match = re.search(r"(https?://[^\s]+)", instruction)
        if url_match:
            actions.append({"action": "open_browser", "params": {"url": url_match.group(1)}})
            return actions

        search_match = re.search(
            r"(?:\u641c\u7d22|\u67e5\u627e|\u67e5\u8be2|search)\s+(.+)",
            instruction,
            flags=re.IGNORECASE,
        )
        if search_match and any(
            token in instruction.lower()
            for token in ("\u6d4f\u89c8\u5668", "browser", "\u7f51\u9875", "\u7f51\u7ad9", "weather")
        ):
            actions.append({"action": "open_browser", "params": {"url": "https://www.baidu.com"}})
            actions.append(
                {"action": "browser_search", "params": {"text": search_match.group(1).strip()}}
            )
            return actions

        sheet_match = re.search(
            r"\u6253\u5f00(?P<file>[^\s]+\.(?:et|xlsx?|xls)).*?(?:\u8f93\u5165|\u5199\u5165|\u586b\u5165)(?P<value>.+?)(?:\u5230|\u7684)(?P<cell>[A-Za-z]+\d+)\u683c",
            instruction,
            flags=re.IGNORECASE,
        )
        if not sheet_match:
            sheet_match = re.search(
                r"open\s+(?P<file>[^\s]+\.(?:et|xlsx?|xls)).*?(?:write|input|fill)\s+(?P<value>.+?)\s+(?:to|into)\s+(?P<cell>[A-Za-z]+\d+)",
                instruction,
                flags=re.IGNORECASE,
            )
        if sheet_match:
            actions.append(
                {
                    "action": "open_app",
                    "params": {"name": "wps", "file_path": sheet_match.group("file").strip()},
                }
            )
            actions.append(
                {
                    "action": "input_cell",
                    "params": {
                        "cell": sheet_match.group("cell").upper().strip(),
                        "text": sheet_match.group("value").strip(),
                    },
                }
            )
            return actions

        return actions

    def _is_deterministic_complete(self, instruction: str) -> bool:
        lowered = instruction.lower()
        return any(
            token in lowered
            for token in ("\u641c\u7d22", "search", ".et", ".xlsx", ".xls", "\u683c", "\u5199\u5165")
        )

    def _extract_folder_creation_target(self, instruction: str) -> str:
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
            r"在当前打开的文件管理器里创建一个新文件夹\s*([^\s，。]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip().strip("\"'")
        return ""

    async def _observe(self, instruction: str, failure_context: str) -> Dict[str, Any]:
        screenshot_image = self.desktop.capture_visual_screenshot()
        screenshot_path = self._save_screenshot(screenshot_image)
        logger.log("视觉兜底已截取当前桌面截图：{}".format(screenshot_path), "gray")

        system_state = self._system_state_text()
        accessibility = self._accessibility_snapshot()
        structured_accessibility = self._structured_accessibility_observation(
            instruction=instruction,
            system_state=system_state,
        )
        if structured_accessibility is not None:
            structured_accessibility["screenshot_path"] = screenshot_path
            return self._normalize_observation(structured_accessibility)

        return await self._observe_from_screenshot(
            instruction=instruction,
            failure_context=failure_context,
            screenshot_image=screenshot_image,
            screenshot_path=screenshot_path,
            system_state=system_state,
            accessibility=accessibility,
        )

    async def _observe_from_screenshot(
        self,
        *,
        instruction: str,
        failure_context: str,
        screenshot_image: Any,
        screenshot_path: str,
        system_state: str,
        accessibility: str,
    ) -> Dict[str, Any]:
        image_buffer = io.BytesIO()
        screenshot_image.save(image_buffer, format="PNG")

        prompt = (
            "你是一个 AI 助手，具有计算机视觉和操作能力。当前显示的是计算机的屏幕截图。\n"
            "请按照以下结构和规则进行回复：\n\n"
            "1. 任务目标: [在此处写下当前的任务目标]\n"
            "2. 屏幕观察: [详细列出屏幕上与目标相关的所有内容，包括窗口、图标、菜单、应用和 UI 元素]\n"
            "3. 状态判断: [目标已完成 | 目标未完成]\n"
            "4. 下一步计划: [如果目标未完成，说明下一步要做什么：点击|输入|按键，并说明预期结果]\n"
            "5. JSON 输出: 必须以纯 JSON 格式返回最终决策，禁止 Markdown 代码块标签。\n\n"
            "JSON 结构：\n"
            "{{"
            '"summary": "简要总结当前观察和判断",'
            '"screen_type": "界面类型",'
            '"focused_target": "当前焦点",'
            '"elements": [{{"name": "元素名", "x": X坐标, "y": Y坐标}}],'
            '"suggested_text": "建议输入的文本",'
            '"action": "下一步动作名",'
            '"params": {{"动作参数": "值"}},'
            '"is_complete": 布尔值'
            "}}\n"
            "用户任务：\n{}\n"
            "失败上下文：\n{}\n"
            "系统状态：\n{}\n"
            "无障碍快照：\n{}\n"
            "截图路径：\n{}\n".format(
                instruction,
                failure_context or "none",
                system_state,
                accessibility,
                screenshot_path,
            )
        )

        last_error = None
        for attempt in range(3):
            try:
                logger.log(
                    "Visual observation attempt {} with screenshot {}".format(
                        attempt + 1, screenshot_path
                    ),
                    "gray",
                )
                raw_output = self.vision_model.call(
                    [{"role": "user", "content": [prompt, image_buffer.getvalue()]}]
                )
                parsed = parse_first_json(raw_output or "")
                if parsed:
                    parsed["screenshot_path"] = screenshot_path
                    return self._normalize_observation(parsed)
                return {
                    "summary": "本地视觉模型未返回可解析的 JSON 结果。",
                    "screen_type": "unknown",
                    "focused_target": "",
                    "elements": [],
                    "suggested_text": "",
                    "raw_output": raw_output or "",
                    "screenshot_path": screenshot_path,
                }
            except Exception as exc:
                last_error = str(exc)
                logger.log(
                    "Visual observation attempt {} failed: {}".format(attempt + 1, last_error),
                    "red",
                )
                if attempt < 2:
                    await asyncio.sleep(attempt + 1)

        raise VisualExecutionError(
            "Vision model failed after 3 attempts. Last screenshot: {}. Last error: {}".format(
                screenshot_path,
                last_error or "unknown error",
            )
        )

    def _structured_accessibility_observation(
        self,
        *,
        instruction: str,
        system_state: str,
    ) -> Optional[Dict[str, Any]]:
        if self.atspi_provider is None:
            return None
        try:
            tree = self.atspi_provider.get_accessibility_tree()
        except Exception:
            return None

        flattened = self._flatten_accessibility_nodes(tree)
        if not flattened:
            return None

        ranked = self._rank_accessibility_elements(flattened, instruction)
        if not ranked:
            return None

        elements = [
            {
                "name": item["name"],
                "x": item["x"] + item["width"] // 2,
                "y": item["y"] + item["height"] // 2,
            }
            for item in ranked[:8]
        ]
        return {
            "summary": "通过 AT-SPI 识别到了当前桌面中的可操作元素。",
            "screen_type": "desktop_accessibility",
            "focused_target": system_state,
            "elements": elements,
            "suggested_text": (
                "如果当前任务是创建文件夹，优先点击新建文件夹相关按钮，然后输入目标名称。"
                if self._folder_creation_target
                else ""
            ),
            "source": "atspi",
        }

    def _flatten_accessibility_nodes(self, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        flattened: List[Dict[str, Any]] = []

        def visit(node: Dict[str, Any]) -> None:
            if not isinstance(node, dict):
                return
            name = str(node.get("name", "") or "").strip()
            role = str(node.get("role", "") or "").strip().lower()
            width = int(node.get("width", 0) or 0)
            height = int(node.get("height", 0) or 0)
            if name and width > 0 and height > 0:
                flattened.append(
                    {
                        "name": name,
                        "role": role,
                        "x": int(node.get("x", 0) or 0),
                        "y": int(node.get("y", 0) or 0),
                        "width": width,
                        "height": height,
                    }
                )
            for child in list(node.get("children", []) or []):
                visit(child)

        for item in nodes:
            visit(item)
        return flattened

    def _rank_accessibility_elements(
        self,
        elements: List[Dict[str, Any]],
        instruction: str,
    ) -> List[Dict[str, Any]]:
        keywords = self._instruction_keywords(instruction)
        preferred_roles = {
            "push button",
            "button",
            "icon",
            "menu item",
            "entry",
            "text",
            "label",
            "table cell",
            "frame",
            "window",
            "application",
        }

        scored: List[Dict[str, Any]] = []
        for element in elements:
            name = str(element.get("name", "") or "")
            role = str(element.get("role", "") or "")
            lowered_name = name.lower()
            score = 0
            if role in preferred_roles:
                score += 2
            for keyword in keywords:
                if keyword and keyword in lowered_name:
                    score += 5
            if any(token in lowered_name for token in ["新建", "文件夹", "folder", "desktop", "桌面"]):
                score += 4
            if score > 0:
                enriched = dict(element)
                enriched["score"] = score
                scored.append(enriched)

        scored.sort(key=lambda item: (item["score"], -item["y"], -item["x"]), reverse=True)
        return scored

    def _instruction_keywords(self, instruction: str) -> List[str]:
        lowered = (instruction or "").lower()
        ascii_tokens = re.findall(r"[a-z0-9_./:-]+", lowered)
        cjk_tokens = re.findall(r"[\u4e00-\u9fff]{1,6}", lowered)
        combined = ascii_tokens + cjk_tokens
        unique: List[str] = []
        for token in combined:
            value = token.strip()
            if value and value not in unique:
                unique.append(value)
        return unique

    def _decide_next(self, instruction: str, failure_context: str) -> Dict[str, Any]:
        available_actions = self._available_action_menu()
        prompt = (
            "You are the decision core for a desktop visual executor.\n"
            "Choose exactly one action from: {}\n"
            "Return JSON only with schema: "
            '{{"action": string, "params": object}}\n'
            "Task:\n{}\n"
            "Failure context:\n{}\n"
            "Recent history:\n{}\n"
            "Current observation:\n{}\n".format(
                ", ".join(available_actions.keys()),
                instruction,
                failure_context or "none",
                "\n".join(self.context_history[-5:]) or "none",
                json.dumps(self.last_observation, ensure_ascii=False),
            )
        )
        raw_output = self.reasoning_model.call([{"role": "user", "content": prompt}])
        parsed = parse_first_json(raw_output or "")
        if parsed and parsed.get("action") in available_actions:
            return parsed
        return {"action": "finish", "params": {"answer": "No valid visual action was produced."}}

    def _decide_folder_creation_next(self, instruction: str) -> Dict[str, Any]:
        if self._folder_creation_succeeded():
            return {
                "action": "finish",
                "params": {"answer": "Folder creation was verified."},
            }

        elements = list(self.last_observation.get("elements", []) or [])
        input_element = self._find_element(
            elements,
            ["文件名输入框", "folder name input", "name input", "新建文件夹名称", "输入框"],
        )
        new_folder_element = self._find_element(
            elements,
            ["新建文件夹", "new folder"],
        )
        focus_element = self._find_element(
            elements,
            ["桌面", "desktop", "文件管理器", "file manager", "home"],
        )

        typed_name = self._history_contains(self._folder_creation_target)
        pressed_enter = self._history_contains("Pressed key enter.")
        shortcut_used = self._history_contains("Pressed key ctrl+shift+n.")
        focused_file_manager = self._history_contains("Focused file manager target.")

        if self.pure_visual_actions and not focused_file_manager and focus_element:
            return {
                "action": "click",
                "params": {
                    "target": focus_element.get("name", "file manager"),
                    "x": focus_element.get("x"),
                    "y": focus_element.get("y"),
                    "button": "left",
                    "note": "focus_file_manager",
                },
            }

        if not self.pure_visual_actions and not self._history_contains("Activated window for folder creation."):
            return {
                "action": "activate_window",
                "params": {"patterns": ["桌面", "Desktop", "文件管理器", "File Manager"]},
            }

        if self.pure_visual_actions and not shortcut_used and not typed_name:
            return {
                "action": "press_key",
                "params": {"key": "ctrl-shift-n"},
            }

        if not self.pure_visual_actions and new_folder_element and not typed_name:
            return {
                "action": "click",
                "params": {
                    "target": new_folder_element.get("name", "new folder"),
                    "x": new_folder_element.get("x"),
                    "y": new_folder_element.get("y"),
                },
            }

        if typed_name and not pressed_enter:
            return {
                "action": "press_key",
                "params": {"key": "enter"},
            }

        if self.pure_visual_actions and shortcut_used and not typed_name:
            return {
                "action": "type_text",
                "params": {"text": self._folder_creation_target},
            }

        if input_element and not typed_name:
            return {
                "action": "type_text",
                "params": {
                    "text": self._folder_creation_target,
                    "target": input_element.get("name", "folder name input"),
                    "x": input_element.get("x"),
                    "y": input_element.get("y"),
                },
            }

        if not input_element and not shortcut_used:
            return {
                "action": "press_key",
                "params": {"key": "ctrl-shift-n"},
            }

        if (input_element and typed_name and not pressed_enter) or (
            typed_name and not pressed_enter and not new_folder_element
        ):
            return {
                "action": "press_key",
                "params": {"key": "enter"},
            }

        raise VisualExecutionError(
            "Folder creation task is missing actionable UI elements in observation. "
            "Refusing to fall back to unrelated generic actions. "
            "Observation: {}".format(json.dumps(self.last_observation, ensure_ascii=False))
        )

    @staticmethod
    def _find_element(elements: List[Dict[str, Any]], names: List[str]) -> Optional[Dict[str, Any]]:
        lowered_names = [name.lower() for name in names]
        for element in elements:
            element_name = str(element.get("name", "") or "").lower()
            if any(bad in element_name for bad in ["open-computer-use", "kylin@kylin-pc", "终端", "terminal"]):
                continue
            if any(name in element_name for name in lowered_names):
                return element
        return None

    def _folder_creation_succeeded(self) -> bool:
        if not self._folder_creation_target:
            return False

        if self._folder_exists(self._folder_creation_target) and not self._folder_creation_preexisting:
            return True

        observation_text = json.dumps(self.last_observation, ensure_ascii=False)
        if (
            self._folder_creation_target in observation_text
            and self._history_contains(self._folder_creation_target)
            and self._history_contains("Pressed key enter.")
        ):
            return True
        return False

    @staticmethod
    def _folder_exists(folder_name: str) -> bool:
        if not folder_name:
            return False
        for candidate in ("~/Desktop", "~/桌面"):
            path = os.path.abspath(os.path.join(os.path.expanduser(candidate), folder_name))
            if os.path.isdir(path):
                return True
        return False

    def _history_contains(self, text: str) -> bool:
        if not text:
            return False
        return any(text in item for item in self.context_history)

    def _available_action_menu(self) -> Dict[str, str]:
        if self.pure_visual_actions:
            return self.PURE_VISUAL_ACTION_MENU
        return self.ACTION_MENU

    @staticmethod
    def _normalize_observation(observation: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(observation or {})
        normalized.setdefault("summary", "")
        normalized.setdefault("screen_type", "unknown")
        normalized.setdefault("focused_target", "")
        elements = normalized.get("elements")
        normalized["elements"] = elements if isinstance(elements, list) else []
        normalized.setdefault("suggested_text", "")
        params = normalized.get("params")
        normalized["params"] = params if isinstance(params, dict) else {}
        return normalized

    def _hydrate_action_params(
        self,
        action_name: str,
        params: Dict[str, Any],
        observation: Dict[str, Any],
    ) -> Dict[str, Any]:
        hydrated = dict(params or {})
        elements = list(observation.get("elements", []) or [])
        target = hydrated.get("target") or observation.get("focused_target")
        if action_name in {"click", "type_text"} and (hydrated.get("x") is None or hydrated.get("y") is None):
            element = None
            if target:
                element = self._match_element_from_target(elements, str(target))
            if element is None and len(elements) == 1:
                element = elements[0]
            if element is not None:
                hydrated.setdefault("target", element.get("name", target or ""))
                hydrated["x"] = element.get("x")
                hydrated["y"] = element.get("y")
        if action_name == "type_text" and not hydrated.get("text") and observation.get("suggested_text"):
            hydrated["text"] = observation.get("suggested_text")
        return hydrated

    @staticmethod
    def _is_action_actionable(action_name: str, params: Dict[str, Any]) -> bool:
        if not action_name or action_name in {"vision", "finish"}:
            return action_name == "finish"
        if action_name == "click":
            return (params.get("x") is not None and params.get("y") is not None) or bool(params.get("target"))
        if action_name == "type_text":
            return bool(params.get("text"))
        if action_name == "press_key":
            return bool(params.get("key"))
        if action_name == "browser_search":
            return bool(params.get("text"))
        if action_name == "open_browser":
            return True
        if action_name == "activate_window":
            return bool(params.get("patterns") or params.get("target"))
        if action_name == "open_app":
            return bool(params.get("name") or params.get("file_path") or params.get("app_name"))
        if action_name == "input_cell":
            return bool(params.get("cell") or params.get("target") or params.get("cell_pos") or params.get("cell_address")) and (
                params.get("text") is not None or params.get("value") is not None or params.get("content") is not None
            )
        return True

    @staticmethod
    def _match_element_from_target(elements: List[Dict[str, Any]], target: str) -> Optional[Dict[str, Any]]:
        query = str(target or "").strip().lower()
        if not query:
            return elements[0] if len(elements) == 1 else None
        exact = VisualExecutor._find_element(elements, [query])
        if exact is not None:
            return exact
        query_tokens = [token for token in re.split(r"[\s_\-:/]+", query) if token]
        best = None
        best_score = 0
        for element in elements:
            name = str(element.get("name", "") or "").lower()
            if not name:
                continue
            score = 0
            if query in name or name in query:
                score += 4
            for token in query_tokens:
                if token and token in name:
                    score += 1
            if score > best_score:
                best_score = score
                best = element
        if best_score > 0:
            return best
        return elements[0] if len(elements) == 1 else None

    async def _execute_action(self, action: Dict[str, Any]) -> str:
        act = action.get("action", "")
        params = action.get("params", {})

        if act not in self._available_action_menu():
            raise VisualExecutionError(
                "Action {} is not allowed in the current visual mode.".format(act)
            )

        if act == "open_browser":
            url = params.get("url", "https://www.baidu.com")
            await self.desktop.open_browser(url)
            return "Opened browser: {}".format(url)
        if act == "browser_search":
            text = params.get("text", "")
            if not text:
                raise VisualExecutionError("browser_search requires text.")
            await self.desktop.browser_search(text)
            return "Searched: {}".format(text)
        if act == "open_app":
            app_name = str(params.get("name") or params.get("app_name") or "").lower()
            file_path = params.get("file_path")
            is_spreadsheet = (
                "wps" in app_name or 
                "excel" in app_name or 
                "spreadsheet" in app_name or 
                (file_path and str(file_path).lower().endswith((".et", ".xlsx", ".xls")))
            )
            if is_spreadsheet:
                resolved = self._resolve_path(file_path) if file_path else None
                success = await self.desktop.open_wps_file(resolved)
                if not success:
                    raise VisualExecutionError("Failed to open spreadsheet app.")
                return "Opened app for {}".format(resolved or "spreadsheet")
            if file_path and str(file_path).lower().endswith(".txt"):
                resolved = self._resolve_path(file_path)
                success = await self.desktop.open_text_editor(resolved)
                if not success:
                    raise VisualExecutionError("Failed to open text editor.")
                return "Opened text editor: {}".format(resolved)
            raise VisualExecutionError("Unsupported open_app target: {}".format(params))
        if act == "input_cell":
            cell = params.get("cell") or params.get("target") or params.get("cell_pos") or params.get("cell_address")
            text = params.get("text") or params.get("value") or params.get("content")
            if not cell or text is None:
                raise VisualExecutionError("input_cell requires cell and text. Got: {}".format(params))
            success = self.desktop.wps_input_cell(str(cell), str(text))
            if not success:
                raise VisualExecutionError("Failed to input spreadsheet cell.")
            return "Wrote {} to {}".format(text, cell)
        if act == "click":
            x = params.get("x")
            y = params.get("y")
            button = str(params.get("button", "left") or "left").lower()
            if (x is None or y is None) and params.get("target"):
                element = self._match_element_from_target(
                    list(self.last_observation.get("elements", []) or []),
                    str(params.get("target")),
                )
                if element:
                    x = element.get("x")
                    y = element.get("y")
            if x is None or y is None:
                raise VisualExecutionError("click requires x and y.")
            self.desktop.move_mouse(int(x), int(y))
            await asyncio.sleep(0.3)
            if button == "right":
                self.desktop.right_click()
            else:
                self.desktop.left_click()
            if params.get("note") == "focus_file_manager":
                return "Focused file manager target."
            return "Clicked at ({}, {}) with {} button.".format(x, y, button)
        if act == "activate_window":
            patterns = params.get("patterns") or []
            if not patterns and params.get("target"):
                patterns = [str(params.get("target"))]
            if not patterns:
                raise VisualExecutionError("activate_window requires patterns.")
            success = self.desktop.activate_window_matching(patterns)
            if not success:
                raise VisualExecutionError(
                    "Failed to activate any window matching patterns: {}".format(patterns)
                )
            return "Activated window for folder creation."
        if act == "type_text":
            text = params.get("text", "")
            if not text:
                raise VisualExecutionError("type_text requires text.")
            x = params.get("x")
            y = params.get("y")
            if (x is None or y is None) and params.get("target"):
                element = self._find_element(
                    list(self.last_observation.get("elements", []) or []),
                    [str(params.get("target"))],
                )
                if element:
                    x = element.get("x")
                    y = element.get("y")
            if x is not None and y is not None:
                self.desktop.move_mouse(int(x), int(y))
                await asyncio.sleep(0.2)
                self.desktop.left_click()
                await asyncio.sleep(0.2)
            self.desktop.write(str(text))
            return "Typed text."
        if act == "press_key":
            key = params.get("key")
            if not key:
                raise VisualExecutionError("press_key requires key.")
            self.desktop.press(str(key))
            return "Pressed key {}.".format(key)
        if act == "vision":
            return "Observation step acknowledged."

        raise VisualExecutionError("Unsupported visual action: {}".format(act))

    def _take_screenshot(self) -> str:
        """Capture a screenshot and save it to the screenshots directory."""
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        screenshot_name = "visual_{}.png".format(now)
        screenshot_path = os.path.join(self.screenshots_dir, screenshot_name)
        self.desktop.capture_visual_screenshot().save(screenshot_path)
        return screenshot_path

    def _accessibility_snapshot(self) -> str:
        try:
            if self.atspi_provider is None:
                return "Accessibility provider unavailable."
            tree = self.atspi_provider.get_accessibility_tree()
            return json.dumps(tree[:2], ensure_ascii=False)
        except Exception as exc:
            return "Accessibility unavailable: {}".format(exc)

    def _system_state_text(self) -> str:
        windows = []
        try:
            proc = subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--name", ".*"],
                capture_output=True,
                text=True,
                check=False,
            )
            for wid in [item for item in proc.stdout.splitlines() if item.strip()]:
                name_proc = subprocess.run(
                    ["xdotool", "getwindowname", wid.strip()],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                title = name_proc.stdout.strip()
                if title:
                    windows.append(title)
        except Exception as exc:
            return "System state unavailable: {}".format(exc)
        return "Visible windows: {}".format(", ".join(windows) if windows else "none")

    def _resolve_path(self, path: str) -> str:
        resolved = os.path.abspath(os.path.expanduser(path))
        if os.path.exists(resolved):
            return resolved
        
        name_no_ext, _ = os.path.splitext(os.path.basename(path))
        possible_names = [os.path.basename(path), f"{name_no_ext}.et", f"{name_no_ext}.xlsx", f"{name_no_ext}.xls"]
        
        for candidate in ("~/Desktop", "~/桌面", "~"):
            base_dir = os.path.expanduser(candidate)
            for name in possible_names:
                merged = os.path.abspath(os.path.join(base_dir, name))
                if os.path.exists(merged):
                    return merged
        return resolved

    def _save_screenshot(self, image: Any) -> str:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(self.screenshots_dir, "visual_{}.png".format(timestamp))
        image.save(path, format="PNG")
        return path
