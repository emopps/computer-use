from os_computer_use.desktop.atspi_provider import ATSPIProvider
from os_computer_use.desktop.grounding import draw_big_dot
from os_computer_use.llm.config import vision_model, action_model, grounding_model
from os_computer_use.llm.llm_provider import Message
from os_computer_use.logging import logger

import shlex
import os
import tempfile
from PIL import Image
import json
import asyncio
import time

TYPING_DELAY_MS = 12
TYPING_GROUP_SIZE = 50

tools = {
    "stop": {
        "description": "指示任务已完成。",
        "params": {},
    }
}


class SandboxAgent:

    def __init__(self, sandbox, output_dir=".", save_logs=True):
        super().__init__()
        self.messages = []  # 代理记忆
        self.sandbox = sandbox  # E2B 沙箱或本地桌面
        self.atspi = ATSPIProvider()
        self.latest_screenshot = None  # 最近的屏幕截图 PNG
        self.image_counter = 0  # 当前截图编号
        
        # 在项目根目录下创建 screenshots 文件夹
        self.screenshots_dir = os.path.join(os.getcwd(), "screenshots")
        os.makedirs(self.screenshots_dir, exist_ok=True)
        self.tmp_dir = self.screenshots_dir # 使用项目内文件夹存储截图

        # 设置日志文件位置
        if save_logs:
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
                logger.log_file = os.path.join(output_dir, "log.html")
            else:
                logger.log_file = "log.html"

        print("代理将使用以下操作：")
        for action, details in tools.items():
            param_str = ", ".join(details.get("params").keys())
            print(f"- {action}({param_str})")

    async def call_function(self, name, arguments):

        func_impl = getattr(self, name.lower()) if name.lower() in tools else None
        if func_impl:
            try:
                if asyncio.iscoroutinefunction(func_impl):
                    result = await func_impl(**arguments) if arguments else await func_impl()
                else:
                    result = func_impl(**arguments) if arguments else func_impl()
                return result
            except Exception as e:
                return f"执行函数时出错: {str(e)}"
        else:
            return "函数未实现。"

    def tool(description, params):
        def decorator(func):
            tools[func.__name__] = {"description": description, "params": params}
            return func

        return decorator

    def save_image(self, image, prefix="image"):
        self.image_counter += 1
        filename = f"{prefix}_{self.image_counter}.png"
        filepath = os.path.join(self.tmp_dir, filename)
        if isinstance(image, Image.Image):
            image.save(filepath)
        else:
            with open(filepath, "wb") as f:
                f.write(image)
        return filepath

    def screenshot(self):
        file = self.sandbox.screenshot()
        filename = self.save_image(file, "screenshot")
        logger.log(f"截图 {filename}", "gray")
        self.latest_screenshot = filename
        with open(filename, "rb") as image_file:
            return image_file.read()

    @tool(
        description="运行 shell 命令并返回结果。",
        params={"command": "要同步运行的 Shell 命令"},
    )
    async def run_command(self, command):
        result = await self.sandbox.commands.run(command, timeout=15)
        stdout, stderr = result.stdout, result.stderr
        if stdout and stderr:
            return stdout + "\n" + stderr
        elif stdout or stderr:
            return stdout + stderr
        else:
            return "命令运行完成。"

    @tool(
        description="在后台运行 shell 命令。",
        params={"command": "要异步运行的 Shell 命令"},
    )
    def run_background_command(self, command):
        self.sandbox.commands.run(command, background=True)
        return "命令已启动。"

    @tool(
        description="向系统发送按键或组合键。",
        params={"name": "按键或组合键（例如 'Return', 'Ctl-C'）"},
    )
    def send_key(self, name):
        self.sandbox.press(name)
        return "按键已按下。"

    @tool(
        description="向系统输入指定的文本。",
        params={"text": "要输入的文本"},
    )
    def type_text(self, text):
        self.sandbox.write(text, chunk_size=TYPING_GROUP_SIZE, delay_in_ms=TYPING_DELAY_MS)
        return "文本已输入。"

    def click_element(self, query, click_command, action_name="点击"):
        """所有点击操作的基础方法"""
        self.screenshot()
        
        # 优先尝试 ATSPI
        elements = self.atspi.find_element_by_query(query)
        if elements:
            # 为了简单起见，选取第一个匹配项
            el = elements[0]
            # 计算元素中心
            x = el["x"] + el["width"] / 2
            y = el["y"] + el["height"] / 2
            logger.log(f"通过 AT-SPI 找到元素: {el['name']} ({el['role']}) 位于 ({x}, {y})", "gray")
            position = (x, y)
        else:
            # 回退到视觉模型定位
            logger.log(f"未通过 AT-SPI 找到元素 '{query}'，正在回退到视觉定位", "gray")
            position = grounding_model.call(query, self.latest_screenshot)
            
        dot_image = draw_big_dot(Image.open(self.latest_screenshot), position)
        filepath = self.save_image(dot_image, "location")
        logger.log(f"{action_name} {filepath})", "gray")

        x, y = position
        self.sandbox.move_mouse(x, y)
        click_command()
        return f"鼠标已执行{action_name}。"

    @tool(
        description="打开浏览器并访问指定的 URL，如果未指定则打开百度。",
        params={"url": "要访问的网址"},
    )
    async def open_browser(self, url="https://www.baidu.com"):
        await self.sandbox.open_browser(url)
        return f"已打开浏览器并访问 {url}"

    @tool(
        description="在已打开的浏览器中进行百度搜索。",
        params={"text": "要搜索的内容"},
    )
    async def browser_search(self, text):
        result = await self.sandbox.browser_search(text)
        return result

    @tool(
        description="打开指定的 WPS 表格文件 (.et)。",
        params={"file_path": "文件的绝对路径"},
    )
    async def open_wps_file(self, file_path):
        success = await self.sandbox.open_wps_file(file_path)
        if success:
            return f"已尝试打开 WPS 文件: {file_path}"
        return f"打开文件失败，请确认路径是否正确: {file_path}"

    @tool(
        description="在当前活动的 WPS 表格中，向指定的单元格输入文本。",
        params={
            "cell": "目标单元格，例如 'A1', 'B5'",
            "text": "要输入的文本"
        },
    )
    def wps_input_cell(self, cell, text):
        success = self.sandbox.wps_input_cell(cell, text)
        if success:
            return f"已向 WPS {cell} 单元格输入: {text}"
        return "未能执行单元格输入，请确保 WPS 窗口已打开且可见。"

    @tool(
        description="创建或覆盖写入一个文本文件。",
        params={
            "file_path": "文件的绝对路径或 ~ 开头路径",
            "content": "要写入的文本内容",
        },
    )
    def write_text_file(self, file_path, content):
        ok = self.sandbox.write_text_file(file_path, content)
        return "写入成功" if ok else "写入失败"

    @tool(
        description="向文本文件追加内容（如果文件不存在则创建）。",
        params={
            "file_path": "文件的绝对路径或 ~ 开头路径",
            "content": "要追加的文本内容",
        },
    )
    def append_text_file(self, file_path, content):
        ok = self.sandbox.append_text_file(file_path, content)
        return "追加成功" if ok else "追加失败"

    @tool(
        description="用系统文本编辑器打开指定文本文件。",
        params={
            "file_path": "文件的绝对路径或 ~ 开头路径",
        },
    )
    async def open_text_editor(self, file_path):
        ok = await self.sandbox.open_text_editor(file_path)
        return "已打开编辑器" if ok else "未找到可用编辑器"

    def append_screenshot(self):
        return vision_model.call(
            [
                *self.messages,
                Message(
                    [
                        self.screenshot(),
                        "这张图片显示了电脑的当前显示内容。请按照以下格式回答：\n"
                        "目标是：[在此处填写目标]\n"
                        "在屏幕上，我看到：[列出与目标相关的所有内容，包括窗口、图标、菜单、应用和 UI 元素]\n"
                        "这意味着目标：[已完成|未完成]\n\n"
                        "（仅当目标未完成时继续。）\n"
                        "下一步是 [点击|输入|运行 shell 命令] [在此处填写具体步骤]，以便 [在此处填写预期的效果]。",
                    ],
                    role="user",
                ),
            ]
        )

    async def run(self, instruction, max_steps=10):
        import re
        # 0. 快速通道：优先通过原子动作/DOM/AT-SPI2 解决，不经过视觉模型
        lower_instr = instruction.lower()
        
        # 场景 A: 浏览器操作 (Playwright 原生 DOM 识别)
        if any(k in lower_instr for k in ["浏览器", "搜索", "baidu", "百度"]):
            print(">>> 触发浏览器原生快速通道 (DOM 识别)...")
            search_text = ""
            search_match = re.search(r'搜索\s*[:：]?\s*(.+)', instruction)
            if search_match:
                search_text = search_match.group(1).strip()
            elif "搜索" in lower_instr:
                parts = re.split(r'搜索', instruction)
                if len(parts) > 1:
                    search_text = parts[-1].strip()
            
            if search_text:
                print(f"正在原生搜索关键词: {search_text}")
                await self.sandbox.open_browser()
                result = await self.sandbox.browser_search(search_text)
                logger.log(f"原生搜索结果: {result}", "green")
            else:
                await self.sandbox.open_browser()
                logger.log("已打开浏览器", "green")
            return

        # 场景 B: WPS 操作 (指令级自动化)
        if any(k in lower_instr for k in ["wps", "表格", "et"]):
            print(">>> 触发 WPS 原生快速通道 (指令级)...")
            file_path = None
            path_match = re.search(r'(/[^\s]+?\.et)', instruction)
            if path_match:
                file_path = path_match.group(1).strip()
            
            cell = "A1"
            cell_match = re.search(r'([a-zA-Z][0-9]{1,2})', instruction)
            if cell_match:
                cell = cell_match.group(1).upper()
                
            text = None
            if "输入" in lower_instr:
                text = lower_instr.split("输入")[-1].strip()
            elif "hello" in lower_instr:
                text = "hello"

            print(f"执行参数: 文件={file_path}, 格子={cell}, 文本={text}")
            await self.sandbox.open_wps_file(file_path)
            if text:
                self.sandbox.wps_input_cell(cell, text)
                logger.log(f"已在 WPS {cell} 输入: {text}", "green")
            return

        # 场景 C: 终端操作
        if any(k in lower_instr for k in ["终端", "terminal", "命令行"]):
            print(">>> 触发终端原生快速通道...")

            # 提取要执行的命令
            cmd_text = ""
            cmd_match = re.search(r'输入\s*[:：]?\s*(.+)', instruction)
            if cmd_match:
                cmd_text = cmd_match.group(1).strip()
            else:
                # 兼容“打开终端输入ls / 新打开终端输入ls”这种格式
                parts = re.split(r'输入', instruction, maxsplit=1)
                if len(parts) == 2:
                    cmd_text = parts[1].strip()

            force_new = "新打开" in instruction

            if cmd_text:
                print(f"将在新终端窗口执行命令: {cmd_text}")
                # 核心：新开终端时直接执行命令，避免焦点问题
                if hasattr(self.sandbox, "open_terminal_with_command"):
                    await self.sandbox.open_terminal_with_command(cmd_text)
                else:
                    # 兜底：至少打开终端（旧实现）
                    await self.sandbox.open_terminal()
                logger.log(f"已在新终端执行命令: {cmd_text}", "green")
            else:
                # 无具体命令时，仅打开新终端窗口
                await self.sandbox.open_terminal()
                logger.log("已打开终端", "green")
            return

        # 场景 D: 文本编辑/文件写入（纯原生，不依赖 UI 焦点）
        # 支持示例：
        # - 创建/home/kylin/桌面/a.txt 写入hello
        # - 往/home/kylin/桌面/a.txt 追加\nworld
        # - 打开/home/kylin/桌面/a.txt
        if any(k in instruction for k in ["文本", "文本文件", "记事本", "编辑器", ".txt"]):
            # 1) 提取路径（支持 .txt/.md/.log）
            path_match = re.search(r'(/[^\s]+?\.(txt|md|log))', instruction)
            file_path = path_match.group(1) if path_match else None

            # 处理内容提取：避免抓取到末尾的“并打开”等指令词
            def extract_and_clean_content(raw_match):
                if not raw_match: return None
                c = raw_match.group(1).strip()
                # 移除末尾可能的“并打开”、“然后打开”等
                c = re.split(r'\s*(?:并|然后)?打开$', c)[0].strip()
                # 处理转义字符（如 \n），仅针对字面量 \n 进行替换，避免破坏 UTF-8 编码
                c = c.replace("\\n", "\n").replace("\\t", "\t")
                return c

            # 2) 写入
            write_match = re.search(r'写入\s*[:：]?\s*(.+)', instruction)
            content = extract_and_clean_content(write_match)
            if file_path and content is not None:
                self.sandbox.write_text_file(file_path, content)
                print(f">>> 已写入文件: {file_path}")
                if "打开" in instruction:
                    await self.sandbox.open_text_editor(file_path)
                return

            # 3) 追加
            append_match = re.search(r'追加\s*[:：]?\s*(.+)', instruction)
            content = extract_and_clean_content(append_match)
            if file_path and content is not None:
                self.sandbox.append_text_file(file_path, content)
                print(f">>> 已追加文件: {file_path}")
                if "打开" in instruction:
                    await self.sandbox.open_text_editor(file_path)
                return

            # 4) 仅打开
            if file_path and "打开" in instruction:
                ok = await self.sandbox.open_text_editor(file_path)
                print(f">>> 打开编辑器: {ok}")
                return

        # 场景 E: 点击图标或菜单 (AT-SPI2 桌面树识别)
        click_match = re.search(r'点击\s*[:：]?\s*(.+)', instruction)
        if click_match:
            query = click_match.group(1).strip()
            print(f">>> 尝试通过 AT-SPI2 定位并点击: {query}")
            elements = self.atspi.find_element_by_query(query)
            if elements:
                el = elements[0]
                x = el["x"] + el["width"] / 2
                y = el["y"] + el["height"] / 2
                print(f"AT-SPI2 命中: {el['name']} ({el['role']}) @ ({x}, {y})")
                self.sandbox.move_mouse(x, y)
                self.sandbox.left_click()
                logger.log(f"已通过 AT-SPI2 完成点击: {query}", "green")
                return
            else:
                print(f"AT-SPI2 未找到元素: {query}，将进入常规 Agent 循环")

        # 1. 常规 Agent 循环
        self.messages.append(Message(f"目标: {instruction}"))
        logger.log(f"用户: {instruction}", print=True)

        step_count = 0
        should_continue = True
        while should_continue and step_count < max_steps:
            step_count += 1
            self.sandbox.set_timeout(60)

            print(f"\n--- 步骤 {step_count} ---")
            
            # 强化系统提示词，引导模型优先使用命令行启动软件
            system_prompt = (
                "你是一个具有电脑使用能力的 AI 助手。你可以操作鼠标、键盘和运行命令。\n"
                "### 重要操作指南 ###\n"
                "1. **优先使用命令行**：如果用户要求打开软件（如浏览器、WPS、终端、记事本等），请直接使用 `run_command` 或 `run_background_command` 工具。例如，要打开浏览器，直接运行 `浏览器` 或 `google-chrome`。\n"
                "2. **减少截图依赖**：在执行第一步启动操作时，不需要等待截图。命令行启动比视觉识别更快、更稳。\n"
                "3. **DOM/AT-SPI 优先**：如果需要点击，我会为你提供 UI 树信息，请优先根据元素名称定位。\n"
                "4. **完成任务**：目标达成后，务必调用 `stop`。"
            )

            # 只有在第一步且包含启动关键词时，尝试跳过截图以加快响应
            skip_initial_screenshot = step_count == 1 and any(k in instruction for k in ["打开", "启动", "运行", "open", "run"])
            
            if skip_initial_screenshot:
                print("检测到启动指令，正在尝试直接引导模型执行命令行启动...")
                obs_message = Message("请直接使用工具启动软件，无需等待截图。", role="user")
            else:
                obs_message = Message(logger.log(f"思考: {self.append_screenshot()}", "green"))

            content, tool_calls = action_model.call(
                [
                    Message(system_prompt, role="system"),
                    *self.messages,
                    obs_message,
                ],
                tools,
            )

            if content:
                print(f"思考内容: {content}")
                self.messages.append(Message(logger.log(f"思考: {content}", "blue")))

            if not tool_calls:
                print("模型未提供工具调用，可能正在思考或等待。")
                # 兜底：如果模型只说话没调工具，尝试引导
                self.messages.append(Message("请通过工具调用来执行具体的操作步奏。"))
                continue

            should_continue = False
            for tool_call in tool_calls:
                name, parameters = tool_call.get("name"), tool_call.get("parameters")
                print(f"尝试执行操作: {name} 参数: {parameters}")
                
                should_continue = name != "stop"
                if not should_continue:
                    print("检测到 stop 命令，任务完成。")
                    break
                    
                # 以易读格式打印工具调用
                logger.log(f"操作: {name} {str(parameters)}", "red")
                # 使用模型使用的相同格式将工具调用写入消息历史
                self.messages.append(Message(json.dumps(tool_call)))
                
                try:
                    result = await self.call_function(name, parameters)
                    print(f"操作结果: {result}")
                    self.messages.append(
                        Message(logger.log(f"观察: {result}", "yellow"))
                    )
                except Exception as e:
                    error_msg = f"执行工具时出错: {e}"
                    print(error_msg)
                    self.messages.append(Message(logger.log(error_msg, "red")))
                    should_continue = True # 出错也尝试继续下一步
