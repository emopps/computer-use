import pyautogui
import mss
from PIL import Image
import os
import re
import time
import random
import json
import subprocess
import shutil
import asyncio
import shlex
import zipfile
import tempfile
import xml.etree.ElementTree as ET
from urllib import parse, request
from typing import List, Optional

class LocalDesktop:
    def __init__(self):
        pyautogui.PAUSE = 0.5
        self.sct = mss.mss()
        if "DISPLAY" not in os.environ:
            os.environ["DISPLAY"] = ":0"
        self._playwright = None
        self._browser = None
        self._page = None
        self._browser_pages = {}
        self._browser_lock = None
        self._wps_window_id = None  # 记住当前操作的WPS窗口，避免多窗口冲突
        self.progress_callback = None

    def _emit_progress(self, event: str, **payload) -> None:
        callback = getattr(self, "progress_callback", None)
        if not callable(callback):
            return
        try:
            callback({"event": event, **payload})
        except Exception:
            return

    def write_text_file(self, file_path, content, encoding="utf-8"):
        file_path = os.path.expanduser(file_path)
        file_path = os.path.abspath(file_path)
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(file_path, "w", encoding=encoding) as f:
            f.write(content)
        return True

    def append_text_file(self, file_path, content, encoding="utf-8"):
        file_path = os.path.expanduser(file_path)
        file_path = os.path.abspath(file_path)
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(file_path, "a", encoding=encoding) as f:
            f.write(content)
        return True

    async def open_text_editor(self, file_path):
        file_path = os.path.expanduser(file_path)
        file_path = os.path.abspath(file_path)
        editors = [
            "ukui-text-editor",
            "mate-text-editor",
            "pluma",
            "gedit",
            "mousepad",
            "xed",
        ]
        editor = None
        for e in editors:
            if shutil.which(e):
                editor = e
                break
        if not editor:
            return False

        cmd = f"export DISPLAY=:0 && nohup {editor} {shlex.quote(file_path)} > /dev/null 2>&1 &"
        subprocess.Popen(cmd, shell=True, start_new_session=True)
        await asyncio.sleep(1.0)
        return True

    async def sync_text_editor_content(self, file_path, content):
        file_path = os.path.expanduser(file_path)
        file_path = os.path.abspath(file_path)
        filename = os.path.basename(file_path)
        patterns = [
            rf"^{re.escape(filename)}",
            re.escape(filename),
            r"文本编辑器",
            r"text editor",
        ]

        activated = self.activate_window_matching(patterns)
        if not activated:
            opened = await self.open_text_editor(file_path)
            if not opened:
                return False
            activated = self.activate_window_matching(patterns)
            if not activated:
                return False

        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.press("backspace")
        time.sleep(0.1)
        if content:
            if not self._copy_text_to_clipboard(str(content)):
                return False
            pyautogui.hotkey("ctrl", "v")
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "s")
        time.sleep(0.2)
        return True

    def _copy_text_to_clipboard(self, text):
        payload = str(text)
        try:
            process = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE)
            process.communicate(input=payload.encode("utf-8"))
            return process.returncode == 0
        except Exception:
            return False

    async def open_path(self, path):
        path = os.path.expanduser(path)
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return False
        cmd = f"export DISPLAY=:0 && nohup xdg-open {shlex.quote(path)} > /dev/null 2>&1 &"
        subprocess.Popen(cmd, shell=True, start_new_session=True)
        await asyncio.sleep(1.2)
        return True

    def screenshot(self):
        monitor = self.sct.monitors[1]
        sct_img = self.sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        return img

    def get_screenshot(self):
        return self.screenshot()

    def capture_visual_screenshot(self):
        hidden_window_ids = self._hide_gui_windows_for_capture()
        try:
            time.sleep(0.15)
            return self.get_screenshot()
        finally:
            self._restore_gui_windows_after_capture(hidden_window_ids)

    def _hide_gui_windows_for_capture(self):
        patterns = []
        env_title = os.environ.get("OPEN_DESKTOP_GUI_WINDOW_TITLE", "").strip()
        if env_title:
            patterns.append(env_title)
        patterns.append("Open Desktop Agent")

        hidden_ids = []
        for pattern in patterns:
            try:
                proc = subprocess.run(
                    ["xdotool", "search", "--onlyvisible", "--name", pattern],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                ids = [item.strip() for item in (proc.stdout or "").splitlines() if item.strip()]
                for wid in ids:
                    if wid in hidden_ids:
                        continue
                    subprocess.run(["xdotool", "windowminimize", wid], check=False)
                    hidden_ids.append(wid)
            except Exception:
                continue
        return hidden_ids

    def _restore_gui_windows_after_capture(self, window_ids):
        for wid in window_ids or []:
            try:
                subprocess.run(["xdotool", "windowmap", wid], check=False)
                subprocess.run(["xdotool", "windowraise", wid], check=False)
            except Exception:
                continue

    def move_mouse(self, x, y):
        pyautogui.moveTo(x, y)

    def left_click(self):
        pyautogui.click()

    def double_click(self):
        pyautogui.doubleClick()

    def right_click(self):
        pyautogui.rightClick()

    def write(self, text, chunk_size=50, delay_in_ms=12):
        pyautogui.write(text, interval=delay_in_ms/1000.0)

    def press(self, key):
        key = key.lower().replace("ctl-", "ctrl-").replace("return", "enter")
        if "-" in key:
            keys = key.split("-")
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(key)

    def set_timeout(self, timeout):
        pass

    def activate_window_matching(self, patterns):
        if isinstance(patterns, str):
            patterns = [patterns]
        for pattern in patterns:
            try:
                proc = subprocess.run(
                    ["xdotool", "search", "--onlyvisible", "--name", pattern],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                ids = [item for item in (proc.stdout or "").splitlines() if item.strip()]
                if not ids:
                    continue
                wid = ids[-1].strip()
                subprocess.run(["xdotool", "windowactivate", "--sync", wid], check=False)
                subprocess.run(["xdotool", "windowraise", wid], check=False)
                subprocess.run(["xdotool", "windowfocus", wid], check=False)
                time.sleep(0.4)
                return True
            except Exception:
                continue
        return False

    @property
    def commands(self):
        return self

    def find_system_browser(self):
        # 针对国产系统优化：增加截图中的“奇安信可信浏览器”等路径
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/browser",
            "/usr/bin/qaxbrowser",
            "/usr/bin/qaxbrowser-safe",
            "/opt/qaxbrowser/qaxbrowser",
            "/opt/google/chrome/google-chrome",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        for name in ["qaxbrowser", "google-chrome", "microsoft-edge", "browser", "chromium"]:
            resolved = shutil.which(name)
            if resolved:
                return resolved
        return None

    async def run(self, command, timeout=15, background=False):
        # 4. 执行命令
        lower_cmd = command.lower()
        if background:
            # 增强浏览器和WPS的指令识别
            if any(k in lower_cmd for k in ["浏览器", "browser", "baidu", "百度", "搜索"]):
                return await self.open_browser()
            elif any(k in lower_cmd for k in ["wps", "表格", "et"]):
                return await self.open_wps_file('')

            bg_cmd = f"export DISPLAY=:0 && nohup {command} > /dev/null 2>&1 &"
            subprocess.Popen(bg_cmd, shell=True, start_new_session=True)
            return type('obj', (object,), {'stdout': f'已通过命令行启动: {command}', 'stderr': ''})
        else:
            try:
                env = os.environ.copy()
                if "DISPLAY" not in env:
                    env["DISPLAY"] = ":0"
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                    return type('obj', (object,), {'stdout': stdout.decode(), 'stderr': stderr.decode()})
                except asyncio.TimeoutError:
                    process.kill()
                    return type('obj', (object,), {'stdout': '', 'stderr': '命令执行超时'})
            except Exception as e:
                return type('obj', (object,), {'stdout': '', 'stderr': str(e)})

    def _ensure_browser_lock(self):
        if self._browser_lock is None:
            self._browser_lock = asyncio.Lock()
        return self._browser_lock

    async def _ensure_browser_runtime(self):
        from playwright.async_api import async_playwright
        if not self._playwright:
            self._playwright = await async_playwright().start()
            browser_path = self.find_system_browser()
            launch_kwargs = {"headless": False, "args": ["--start-maximized"]}
            if browser_path:
                launch_kwargs["executable_path"] = browser_path
            self._browser = await self._browser_type.launch(**launch_kwargs) if hasattr(self, '_browser_type') else await self._playwright.chromium.launch(**launch_kwargs)
        return self._browser

    async def _get_browser_page(self, page_key=None, create=True):
        await self._ensure_browser_runtime()
        key = str(page_key or "default").strip() or "default"
        page = self._browser_pages.get(key)
        try:
            if page is not None and page.is_closed():
                page = None
                self._browser_pages.pop(key, None)
        except Exception:
            page = None
            self._browser_pages.pop(key, None)
        if page is None and create:
            page = await self._browser.new_page(no_viewport=True)
            self._browser_pages[key] = page
        self._page = page
        return page

    async def bind_browser_page(self, page_key=None, create=False):
        lock = self._ensure_browser_lock()
        async with lock:
            return await self._get_browser_page(page_key=page_key, create=create)

    async def _browser_action_sleep(self, minimum: float = 1.0, maximum: float = 2.0) -> None:
        lower = max(0.0, float(minimum))
        upper = max(lower, float(maximum))
        await asyncio.sleep(random.uniform(lower, upper))

    async def open_browser(self, url="https://www.baidu.com", page_key=None):
        lock = self._ensure_browser_lock()
        async with lock:
            page = await self._get_browser_page(page_key=page_key, create=True)
            await self._browser_action_sleep()
            await page.goto(url, wait_until="networkidle")
            self._page = page
        # 将浏览器窗口激活到前台，让用户可见
        await self._browser_action_sleep()
        self._activate_browser_window()
        return page

    def _activate_browser_window(self):
        """用 wmctrl 激活浏览器窗口到前台"""
        try:
            proc = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True)
            if proc.returncode == 0:
                for line in proc.stdout.strip().split("\n"):
                    parts = line.split(None, 4)
                    if len(parts) >= 5:
                        wid_hex, window_name = parts[0], parts[4]
                        # 匹配浏览器窗口名
                        if any(kw in window_name for kw in
                               ["百度", "baidu", "浏览器", "Browser", "Chrome", "Chromium", "奇安信"]):
                            subprocess.run(["wmctrl", "-i", "-a", wid_hex], check=False, capture_output=True)
                            subprocess.run(["xdotool", "windowactivate", "--sync", str(int(wid_hex, 16))],
                                         check=False, capture_output=True)
                            return
        except Exception:
            pass


    async def browser_search(self, text, page_key=None):
        query = str(text or "").strip()
        if not query:
            return ""
        lock = self._ensure_browser_lock()
        async with lock:
            page = await self._get_browser_page(page_key=page_key, create=True)
            current_url = ""
            try:
                current_url = str(page.url or "")
            except Exception:
                current_url = ""
            api_result = self._try_api_priority_search(query)
            if api_result:
                await self._browser_action_sleep()
                search_url = "https://www.baidu.com/s?wd={}".format(parse.quote(query))
                await page.goto(search_url, wait_until="domcontentloaded")
                self._page = page
                return {
                    "query": query,
                    "text": api_result,
                    "source": "api_priority",
                    "useful": True,
                    "reason": "api_priority_answer",
                }
            if "baidu.com" not in current_url or "/s?" in current_url:
                await self._browser_action_sleep()
                await page.goto("https://www.baidu.com", wait_until="networkidle")
            self._page = page
            primary_result = await self._run_browser_search_query(query)
            if self._should_retry_weather_query(primary_result, query):
                for refined_query in self._weather_refined_queries(query):
                    refined_result = await self._run_browser_search_query(refined_query)
                    if not self._should_retry_weather_query(refined_result, query):
                        return refined_result
            return primary_result

    async def _run_browser_search_query(self, query: str) -> str:
        input_box = await self._find_browser_search_input()
        if input_box is None:
            raise RuntimeError("browser_search requires text.")
        await self._set_browser_search_input(input_box, query)
        await self._browser_action_sleep()
        await self._submit_browser_search()
        await self._browser_action_sleep()
        verification_result = await self._wait_for_manual_verification_clear(query)
        if verification_result:
            return verification_result
        return await self._extract_search_results(query)

    def _try_api_priority_search(self, query: str) -> str:
        if self._is_weather_query(query):
            return self._weather_api_answer(query)
        return ""

    def _weather_api_answer(self, query: str) -> str:
        location = str(query or "").replace("天气", "").replace("气温", "").strip(" ，,。")
        if not location:
            return ""
        try:
            geo_url = (
                "https://geocoding-api.open-meteo.com/v1/search?"
                + parse.urlencode(
                    {
                        "name": location,
                        "count": 1,
                        "language": "zh",
                        "format": "json",
                    }
                )
            )
            geo_payload = self._fetch_json(geo_url, timeout=8.0)
            results = geo_payload.get("results") if isinstance(geo_payload, dict) else None
            if not isinstance(results, list) or not results:
                return ""
            first = results[0] or {}
            latitude = first.get("latitude")
            longitude = first.get("longitude")
            resolved_name = str(first.get("name") or location).strip() or location
            admin1 = str(first.get("admin1") or "").strip()
            country = str(first.get("country") or "").strip()
            if latitude is None or longitude is None:
                return ""

            weather_url = (
                "https://api.open-meteo.com/v1/forecast?"
                + parse.urlencode(
                    {
                        "latitude": latitude,
                        "longitude": longitude,
                        "current": "temperature_2m,weather_code,wind_speed_10m",
                        "daily": "temperature_2m_max,temperature_2m_min",
                        "forecast_days": 1,
                        "timezone": "auto",
                    }
                )
            )
            weather_payload = self._fetch_json(weather_url, timeout=8.0)
            if not isinstance(weather_payload, dict):
                return ""
            current = weather_payload.get("current") or {}
            daily = weather_payload.get("daily") or {}
            current_temp = current.get("temperature_2m")
            weather_code = current.get("weather_code")
            wind_speed = current.get("wind_speed_10m")
            max_list = daily.get("temperature_2m_max") or []
            min_list = daily.get("temperature_2m_min") or []
            high = max_list[0] if isinstance(max_list, list) and max_list else None
            low = min_list[0] if isinstance(min_list, list) and min_list else None
            condition = self._map_open_meteo_weather_code(weather_code)

            parts = []
            place_parts = [part for part in [resolved_name, admin1, country] if part]
            if place_parts:
                parts.append(" / ".join(place_parts[:2]))
            summary = []
            if condition:
                summary.append(condition)
            if current_temp is not None:
                summary.append("{}℃".format(self._format_number(current_temp)))
            if high is not None and low is not None:
                summary.append("{}~{}℃".format(self._format_number(low), self._format_number(high)))
            if wind_speed is not None:
                summary.append("风速{}km/h".format(self._format_number(wind_speed)))
            if summary:
                parts.append("，".join(summary))
            return "：".join(parts) if len(parts) >= 2 else (parts[0] if parts else "")
        except Exception:
            return ""

    @staticmethod
    def _fetch_json(url: str, timeout: float = 8.0) -> dict:
        with request.urlopen(url, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _format_number(value) -> str:
        try:
            number = float(value)
        except Exception:
            return str(value)
        if number.is_integer():
            return str(int(number))
        return "{:.1f}".format(number)

    @staticmethod
    def _map_open_meteo_weather_code(code) -> str:
        mapping = {
            0: "晴",
            1: "晴间多云",
            2: "多云",
            3: "阴",
            45: "雾",
            48: "雾",
            51: "小毛雨",
            53: "毛雨",
            55: "大毛雨",
            56: "冻毛雨",
            57: "冻毛雨",
            61: "小雨",
            63: "中雨",
            65: "大雨",
            66: "冻雨",
            67: "冻雨",
            71: "小雪",
            73: "中雪",
            75: "大雪",
            77: "雪粒",
            80: "阵雨",
            81: "阵雨",
            82: "暴雨",
            85: "阵雪",
            86: "大阵雪",
            95: "雷阵雨",
            96: "雷暴夹冰雹",
            99: "强雷暴夹冰雹",
        }
        try:
            return mapping.get(int(code), "")
        except Exception:
            return ""

    async def _capture_search_page_snapshot(self):
        if not self._page:
            return None
        try:
            return await self._page.evaluate(
                """() => ({
                    url: String(window.location.href || ''),
                    title: String(document.title || ''),
                    text: String(document.body?.innerText || '').slice(0, 4000)
                })"""
            )
        except Exception:
            return None

    @staticmethod
    def _is_search_verification_snapshot(snapshot) -> bool:
        if not isinstance(snapshot, dict):
            return False
        current_url = str(snapshot.get("url", "") or "")
        current_title = str(snapshot.get("title", "") or "")
        blob = " ".join(
            [
                current_url,
                current_title,
                str(snapshot.get("text", "") or ""),
            ]
        )
        lower_url = current_url.lower()
        verification_domains = [
            "wappass.baidu.com",
            "passport.baidu.com",
        ]
        verification_paths = [
            "/static/captcha/",
            "/cgi-bin/genimage",
            "/nocaptcha/",
            "captcha",
            "verify",
        ]
        if any(domain in lower_url for domain in verification_domains):
            return True
        if any(token in lower_url for token in verification_paths):
            return True
        tokens = [
            "安全验证",
            "验证码",
            "请输入验证码",
            "异常流量",
            "访问受限",
            "robot",
            "captcha",
            "verify",
            "请完成下列验证",
            "请完成安全验证",
        ]
        return any(token.lower() in blob.lower() for token in tokens)

    async def _wait_for_manual_verification_clear(self, query: str):
        snapshot = await self._capture_search_page_snapshot()
        if not self._is_search_verification_snapshot(snapshot):
            return None

        print("[OCU] 搜索触发验证，请人工完成验证，完成后将自动继续。")
        self._emit_progress(
            "task",
            status="waiting_manual_verification",
            summary="检测到搜索验证，请在浏览器中手动完成验证，系统将自动继续。",
        )
        deadline = time.time() + 300.0
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            snapshot = await self._capture_search_page_snapshot()
            if not self._is_search_verification_snapshot(snapshot):
                self._emit_progress(
                    "task",
                    status="manual_verification_cleared",
                    summary="验证已通过，正在继续执行搜索任务。",
                )
                await self._browser_action_sleep()
                return None

        current_url = str((snapshot or {}).get("url", "") or "")
        current_title = str((snapshot or {}).get("title", "") or "")
        return {
            "query": query,
            "text": "搜索触发验证，请人工完成验证后重试。",
            "source": "browser_verification",
            "useful": False,
            "reason": "search_verification_required",
            "manual_takeover_required": True,
            "verification_url": current_url,
            "verification_title": current_title,
        }

    async def _detect_search_verification_state(self, query: str):
        snapshot = await self._capture_search_page_snapshot()
        if not isinstance(snapshot, dict):
            return None
        if self._is_search_verification_snapshot(snapshot):
            current_url = str(snapshot.get("url", "") or "")
            current_title = str(snapshot.get("title", "") or "")
            return {
                "query": query,
                "text": "搜索触发验证，请人工完成验证后继续。",
                "source": "browser_verification",
                "useful": False,
                "reason": "search_verification_required",
                "manual_takeover_required": True,
                "verification_url": current_url,
                "verification_title": current_title,
            }
        return None

    def _should_retry_weather_query(self, result_text: str, original_query: str) -> bool:
        if not self._is_weather_query(original_query):
            return False
        cleaned = self._clean_extracted_text(result_text, original_query)
        return not self._is_weather_quality_content(cleaned, original_query)

    @staticmethod
    def _weather_refined_queries(query: str) -> List[str]:
        base = str(query or "").strip()
        if not base:
            return []
        candidates = [
            f"{base} 中国天气网",
            f"{base} 实时天气",
            f"{base} 天气预报",
        ]
        return list(dict.fromkeys(item.strip() for item in candidates if item.strip()))

    async def _find_browser_search_input(self):
        selectors = [
            "#chat-textarea",
            "#kw",
            "input[name='wd']",
            "textarea[name='wd']",
            "[contenteditable='true'][role='textbox']",
            "textarea",
            "input[type='search']",
            "input[type='text']",
        ]
        for selector in selectors:
            try:
                element = self._page.locator(selector).first
                if await element.count() > 0 and await element.is_visible(timeout=1500):
                    return element
            except Exception:
                continue
        return None

    async def _set_browser_search_input(self, element, text: str) -> None:
        await self._browser_action_sleep()
        await element.click()
        await self._browser_action_sleep()
        await self._clear_browser_search_input(element)
        await self._browser_action_sleep()
        try:
            await element.fill(text)
        except Exception:
            await self._page.evaluate(
                """({selectors, value}) => {
                    const el = selectors.map((selector) => document.querySelector(selector)).find(Boolean);
                    if (!el) return false;
                    const dispatch = () => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    };
                    if (el.isContentEditable) {
                        el.textContent = value;
                        dispatch();
                        return true;
                    }
                    const proto = Object.getPrototypeOf(el);
                    const descriptor = proto && Object.getOwnPropertyDescriptor(proto, 'value');
                    if (descriptor && descriptor.set) descriptor.set.call(el, value);
                    else el.value = value;
                    dispatch();
                    return true;
                }""",
                {
                    "selectors": [
                        "#chat-textarea",
                        "#kw",
                        "input[name='wd']",
                        "textarea[name='wd']",
                        "[contenteditable='true'][role='textbox']",
                        "textarea",
                        "input[type='search']",
                        "input[type='text']",
                    ],
                    "value": text,
                },
            )
        try:
            current = await element.evaluate(
                """el => (el && el.isContentEditable)
                    ? (el.textContent || '').trim()
                    : ((el && 'value' in el) ? String(el.value || '').trim() : '')"""
            )
        except Exception:
            current = ""
        if str(current).strip() != text.strip():
            await self._page.evaluate(
                """(value) => {
                    const selectors = [
                        "#chat-textarea",
                        "#kw",
                        "input[name='wd']",
                        "textarea[name='wd']",
                        "[contenteditable='true'][role='textbox']",
                        "textarea",
                        "input[type='search']",
                        "input[type='text']",
                    ];
                    const el = selectors.map((selector) => document.querySelector(selector)).find(Boolean);
                    if (!el) return false;
                    const dispatch = () => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    };
                    if (el.isContentEditable) {
                        el.textContent = value;
                        dispatch();
                        return true;
                    }
                    el.value = value;
                    dispatch();
                    return true;
                }""",
                text,
            )

    async def _clear_browser_search_input(self, element) -> None:
        try:
            await element.fill("")
        except Exception:
            pass
        try:
            await self._browser_action_sleep()
            await element.click()
            await self._page.keyboard.press("Control+A")
            await self._browser_action_sleep()
            await self._page.keyboard.press("Backspace")
            await self._browser_action_sleep()
            await self._page.keyboard.press("Delete")
        except Exception:
            pass
        try:
            await element.evaluate(
                """el => {
                    if (!el) return;
                    const dispatch = () => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    };
                    if (el.isContentEditable) {
                        el.innerHTML = '';
                        el.textContent = '';
                        dispatch();
                        return;
                    }
                    if ('value' in el) {
                        el.value = '';
                        dispatch();
                    }
                }"""
            )
        except Exception:
            pass

    async def _submit_browser_search(self) -> None:
        selectors = [
            "#su",
            ".s_btn",
            "input[type='submit']",
            ".chat-input-send-btn",
            "button:has-text('搜索')",
            "button:has-text('百度一下')",
        ]
        for selector in selectors:
            try:
                button = self._page.locator(selector).first
                if await button.count() > 0 and await button.is_visible(timeout=1200):
                    await self._browser_action_sleep()
                    await button.click(timeout=2500, force=True)
                    return
            except Exception:
                continue
        await self._browser_action_sleep()
        await self._page.keyboard.press("Enter")

    async def _extract_search_results(self, query: str) -> str:
        if not self._page:
            return "搜索完成，但当前没有可读取的页面。"
        if self._is_composition_title_query(query):
            composition_titles = self._clean_extracted_text(
                await self._extract_composition_titles_search_answer(query),
                query,
            )
            if composition_titles:
                return composition_titles
        if self._is_weather_query(query):
            weather_answer = self._clean_extracted_text(await self._extract_weather_search_answer(query), query)
            if self._is_weather_quality_content(weather_answer, query) or self._is_weather_homepage_content(weather_answer, query):
                return weather_answer
        instant_answer = self._clean_extracted_text(await self._extract_instant_answer(query), query)
        if self._is_quality_content(instant_answer, query):
            return instant_answer
        snippets = self._clean_extracted_text(await self._extract_search_snippets(query), query)
        if self._is_quality_content(snippets, query):
            return snippets
        deep_content = self._clean_extracted_text(await self._click_and_extract_first_result(query), query)
        if self._is_quality_content(deep_content, query):
            return deep_content
        for fallback in (snippets, instant_answer, deep_content):
            if fallback:
                return fallback
        return "搜索完成，但没有提取到足够可靠的结果。"

    @staticmethod
    def _is_weather_query(query: str) -> bool:
        payload = str(query or "").strip()
        return bool(payload) and "天气" in payload

    @staticmethod
    def _is_composition_title_query(query: str) -> bool:
        payload = str(query or "").strip()
        if not payload:
            return False
        return "作文" in payload

    async def _extract_composition_titles_search_answer(self, query: str) -> str:
        if not self._page:
            return ""
        dom_titles = await self._extract_composition_titles_from_dom(query)
        if dom_titles:
            return "\n".join(dom_titles[:3])
        candidate_texts: List[str] = []
        try:
            candidate_texts.extend(
                await self._page.evaluate(
                    """() => {
                        const selectors = [
                            '.result-op', '.c-container', '.result',
                            '[class*="composition"]', '[class*="zuowen"]',
                            '[class*="write"]', '#content_left'
                        ];
                        const items = [];
                        for (const selector of selectors) {
                            document.querySelectorAll(selector).forEach((el) => {
                                const text = (el.innerText || '').trim();
                                if (text && text.length > 10) items.push(text.slice(0, 4000));
                            });
                        }
                        const bodyText = (document.body?.innerText || '').trim();
                        if (bodyText) items.push(bodyText.slice(0, 12000));
                        return items;
                    }"""
                ) or []
            )
        except Exception:
            candidate_texts = []

        titles = []
        for text in candidate_texts:
            for title in self._extract_composition_titles_from_text(text):
                if title not in titles:
                    titles.append(title)
                if len(titles) >= 3:
                    return "\n".join(titles[:3])
        return "\n".join(titles[:3])

    async def _extract_composition_titles_from_dom(self, query: str) -> List[str]:
        if not self._page:
            return []
        try:
            raw_titles = await self._page.evaluate(
                """() => {
                    const roots = [
                        ...document.querySelectorAll('#content_left .result-op, #content_left .c-container, #content_left .result')
                    ];
                    const candidates = [];
                    const seen = new Set();
                    const push = (text, score) => {
                        const normalized = (text || '').replace(/\\s+/g, ' ').trim();
                        if (!normalized || seen.has(normalized)) return;
                        seen.add(normalized);
                        candidates.push({ text: normalized, score });
                    };

                    for (const root of roots.slice(0, 8)) {
                        const rootText = (root.innerText || '').trim();
                        const rootHtml = root.innerHTML || '';
                        const hasCompositionSignal =
                            /作文|范文|年级|字/.test(rootText) ||
                            /zuowen|composition/i.test(rootHtml);
                        if (!hasCompositionSignal) continue;

                        const selectors = [
                            'h3 a', 'h3', 'a',
                            '[class*="title"]', '[class*="Title"]',
                            '[class*="card"] [class*="name"]',
                            '[class*="card"] span', '[class*="card"] div'
                        ];
                        for (const selector of selectors) {
                            root.querySelectorAll(selector).forEach((el) => {
                                const text = (el.innerText || el.textContent || '').trim();
                                if (!text) return;
                                const rect = el.getBoundingClientRect();
                                if (rect.width <= 0 || rect.height <= 0) return;
                                let score = 0;
                                if (selector.includes('h3')) score += 10;
                                if (/title|Title/.test(selector)) score += 8;
                                if (/作文|小学/.test(text)) score -= 6;
                                if (/字|年级|分|日记|灯会|地球|未来/.test(text)) score += 4;
                                if (text.length >= 2 && text.length <= 16) score += 6;
                                if (/^[\\u4e00-\\u9fffA-Za-z0-9《》“”‘’()（）·—-]+$/.test(text)) score += 2;
                                push(text, score);
                            });
                        }
                    }

                    candidates.sort((a, b) => b.score - a.score || a.text.length - b.text.length);
                    return candidates.map(item => item.text).slice(0, 30);
                }"""
            ) or []
        except Exception:
            return []

        titles: List[str] = []
        for item in raw_titles:
            normalized = self._normalize_composition_title(str(item or ""))
            if not normalized:
                continue
            if normalized not in titles:
                titles.append(normalized)
            if len(titles) >= 3:
                break
        return titles

    def _extract_composition_titles_from_text(self, text: str) -> List[str]:
        payload = str(text or "").replace("\r", "\n")
        lines = [re.sub(r"\s+", " ", line).strip() for line in payload.split("\n")]
        lines = [line for line in lines if line]
        titles: List[str] = []
        blacklist_tokens = [
            "百度", "搜索", "相关", "热搜", "登录", "更多", "作文大全", "精选",
            "字数", "体裁", "年级", "不限", "查看更多", "推荐", "作文题",
        ]

        for index, line in enumerate(lines):
            normalized = line.strip("：:- ").strip()
            if not normalized:
                continue
            if any(token in normalized for token in blacklist_tokens):
                continue
            if len(normalized) < 2 or len(normalized) > 14:
                continue
            if re.search(r"\d", normalized):
                continue
            if normalized.endswith("作文") and len(normalized) > 8:
                continue
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            prev_line = lines[index - 1] if index > 0 else ""
            context_blob = "{} {}".format(prev_line, next_line)
            if not (
                re.search(r"\d+\s*字", context_blob)
                or "分" in context_blob
                or "作文" in context_blob
                or "小学" in context_blob
            ):
                continue
            normalized_title = self._normalize_composition_title(normalized)
            if normalized_title and normalized_title not in titles:
                titles.append(normalized_title)
        return titles

    def _normalize_composition_title(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        normalized = normalized.strip("：:- ").strip()
        normalized = re.sub(r"^[《“\"']+", "", normalized)
        normalized = re.sub(r"[》”\"']+$", "", normalized)
        if not normalized:
            return ""
        blacklist_tokens = [
            "百度", "搜索", "相关", "热搜", "登录", "更多", "作文大全", "精选",
            "字数", "体裁", "年级", "不限", "查看更多", "推荐", "作文题", "小学生作文",
            "百度教育作文", "相关搜索", "未来的地球作文", "小学作文-精选",
        ]
        if any(token == normalized or token in normalized for token in blacklist_tokens):
            return ""
        if len(normalized) < 2 or len(normalized) > 16:
            return ""
        if re.search(r"^\d+$", normalized):
            return ""
        if re.search(r"(第\d+[篇页]|[0-9]{3,}篇)", normalized):
            return ""
        if normalized.endswith("作文") and len(normalized) > 8:
            return ""
        if not re.search(r"[\u4e00-\u9fff]", normalized):
            return ""
        return normalized

    async def _extract_weather_search_answer(self, query: str) -> str:
        if not self._page:
            return ""

        candidate_texts: List[str] = []
        try:
            candidate_texts.extend(
                await self._page.evaluate(
                    """() => {
                        const selectors = [
                            '[class*="weather"]', '[id*="weather"]',
                            '[class*="forecast"]', '[class*="temperature"]',
                            '.op_weather4_twoicon_container', '.op_weather4_twoicon',
                            '.op_weather4_twoicon_today', '.op_weather', '.weather-base',
                            '#content_left', '#content_right', '.result-op', '.c-container'
                        ];
                        const items = [];
                        for (const selector of selectors) {
                            document.querySelectorAll(selector).forEach((el) => {
                                const text = (el.innerText || '').trim();
                                if (text && text.length > 12) items.push(text.slice(0, 1200));
                            });
                        }
                        const bodyText = (document.body?.innerText || '').trim();
                        if (bodyText) items.push(bodyText.slice(0, 12000));
                        return items;
                    }"""
                ) or []
            )
        except Exception:
            candidate_texts = []

        summaries = []
        for text in candidate_texts:
            summary = self._extract_weather_summary_from_text(text, query)
            if summary:
                summaries.append(summary)
        best = self._pick_best_query_text(summaries, query)
        if self._is_weather_quality_content(best, query) or self._is_weather_homepage_content(best, query):
            return best

        # 天气查询严格只解析搜索首页，不再点开任何结果页。
        if best:
            return best
        return ""

    async def _click_and_extract_weather_result(self, query: str) -> str:
        if not self._page:
            return ""
        search_url = self._page.url
        try:
            candidates = []
            locator = self._page.locator(".result h3 a, .c-container h3 a, .t a, .result-op h3 a")
            try:
                count = min(await locator.count(), 10)
            except Exception:
                count = 0
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible(timeout=1000):
                        continue
                    title = (await item.inner_text() or "").strip()
                    href = await item.get_attribute("href")
                    score = self._score_weather_result_title(title, query)
                    if score <= 0:
                        continue
                    candidates.append((score, item, href))
                except Exception:
                    continue
            if not candidates:
                return ""
            candidates.sort(key=lambda row: row[0], reverse=True)
            for _, item, href in candidates[:3]:
                try:
                    if href:
                        await self._page.goto(href, wait_until="domcontentloaded", timeout=15000)
                    else:
                        await item.click(timeout=5000)
                    await asyncio.sleep(2.0)
                    content = await self._page.evaluate(
                        """() => (document.body?.innerText || '').trim().slice(0, 16000)"""
                    )
                    summary = self._extract_weather_summary_from_text(content or "", query)
                    if self._is_weather_quality_content(summary, query):
                        return summary
                except Exception:
                    pass
                finally:
                    try:
                        await self._page.goto(search_url, wait_until="domcontentloaded", timeout=10000)
                        await asyncio.sleep(0.8)
                    except Exception:
                        pass
        except Exception:
            return ""
        return ""

    def _score_weather_result_title(self, title: str, query: str) -> int:
        text = str(title or "").strip()
        if not text:
            return -999
        score = self._score_query_relevance(text, query)
        for token in ("天气", "天气预报", "中国天气网", "中央气象台", "weather"):
            if token.lower() in text.lower():
                score += 8
        for token in self._query_terms(query):
            if token and token in text:
                score += 6
        return score

    def _extract_weather_summary_from_text(self, text: str, query: str) -> str:
        payload = str(text or "").replace("\r", "\n")
        if not payload.strip():
            return ""

        location = ""
        terms = self._query_terms(query)
        if terms:
            location = terms[0]

        weather_pattern = re.compile(
            r"(?:「?(?P<place>[^」\n]{1,12})」?\s*)?"
            r"(?:(?P<date>\d{1,4}[年/-]\d{1,2}(?:月|/-)\d{1,2}(?:日)?|\d{1,2}/\d{1,2}|"
            r"\d{1,2}月\d{1,2}日)\s*)?"
            r"[，,:\s]*"
            r"(?P<condition>晴|多云|阴|小雨|中雨|大雨|暴雨|雷阵雨|阵雨|雨夹雪|小雪|中雪|大雪|雾|霾|扬沙|浮尘)"
            r"[，,:\s]*"
            r"(?P<temp>\d{1,2}\s*[~～\-至]\s*\d{1,2}\s*℃)"
            r"(?P<tail>[^\n]{0,60})"
        )
        candidates = []

        for raw_line in payload.split("\n"):
            line = re.sub(r"\s+", " ", raw_line).strip()
            if len(line) < 6:
                continue
            if location and location not in line and not any(term in line for term in terms):
                continue
            match = weather_pattern.search(line)
            if match:
                candidates.append(self._format_weather_match(match, location))

        if not candidates:
            compact = re.sub(r"\s+", " ", payload)
            for match in weather_pattern.finditer(compact):
                place = (match.group("place") or "").strip()
                if location and place and location not in place and not any(term in place for term in terms):
                    continue
                candidates.append(self._format_weather_match(match, location))

        if not candidates:
            homepage_summary = self._extract_weather_homepage_summary(payload, location, terms)
            if homepage_summary:
                candidates.append(homepage_summary)

        return self._pick_best_query_text(candidates, query)

    def _extract_weather_homepage_summary(self, payload: str, location: str, terms: List[str]) -> str:
        weather_tokens = "晴|多云|阴|小雨|中雨|大雨|暴雨|雷阵雨|阵雨|雨夹雪|小雪|中雪|大雪|雾|霾|扬沙|浮尘"
        lines = [re.sub(r"\s+", " ", line).strip() for line in payload.split("\n")]
        lines = [line for line in lines if len(line) >= 4]

        for index, line in enumerate(lines):
            if location and location not in line and not any(term in line for term in terms):
                continue
            window = " ".join(lines[index:index + 6])
            if not re.search(weather_tokens, window):
                continue
            temp_match = re.search(r"(-?\d{1,2}(?:\s*[~～\-至]\s*-?\d{1,2})?)\s*℃", window)
            if not temp_match:
                temp_match = re.search(r"气温\s*(-?\d{1,2}(?:\s*[~～\-至]\s*-?\d{1,2})?)", window)
            if not temp_match:
                continue
            condition_match = re.search(weather_tokens, window)
            wind_match = re.search(r"(?:[东北西南]{0,2}风\d{1,2}级|风力\d{1,2}级|微风)", window)
            air_match = re.search(r"(空气质量[^\s，。,；;]{1,12})", window)
            detail_parts = []
            if condition_match:
                detail_parts.append(condition_match.group(0))
            detail_parts.append(re.sub(r"\s+", "", temp_match.group(0)))
            if wind_match:
                detail_parts.append(wind_match.group(0))
            if air_match:
                detail_parts.append(air_match.group(1))
            place = location
            if not place:
                place_match = re.search(r"([^\s，。,；;]{1,8})(?:天气|今日天气|今天天气)", window)
                if place_match:
                    place = place_match.group(1)
            if place:
                return "{}：{}".format(place, "，".join(detail_parts))
            return "，".join(detail_parts)
        return ""

    @staticmethod
    def _format_weather_match(match, fallback_location: str) -> str:
        place = (match.group("place") or "").strip()
        date = (match.group("date") or "").strip()
        condition = (match.group("condition") or "").strip()
        temp = re.sub(r"\s+", "", (match.group("temp") or "").strip())
        tail = re.sub(r"\s+", " ", (match.group("tail") or "").strip("，,。;； "))

        parts = []
        if place:
            parts.append(place)
        elif fallback_location:
            parts.append(fallback_location)
        if date:
            parts.append(date)
        summary = " ".join(parts).strip()
        detail = "，".join(item for item in [condition, temp, tail] if item)
        if summary and detail:
            return f"{summary}：{detail}"
        return detail or summary

    def _is_weather_quality_content(self, text: str, query: str = "") -> bool:
        payload = str(text or "").strip()
        if not payload:
            return False
        if self._score_query_relevance(payload, query) <= 0:
            return False
        if not re.search(r"\d{1,2}\s*[~～\-至]\s*\d{1,2}\s*℃", payload):
            return False
        if not re.search(r"晴|多云|阴|小雨|中雨|大雨|暴雨|雷阵雨|阵雨|雨夹雪|小雪|中雪|大雪|雾|霾|扬沙|浮尘", payload):
            return False
        return True

    def _is_weather_homepage_content(self, text: str, query: str = "") -> bool:
        payload = str(text or "").strip()
        if not payload:
            return False
        if self._score_query_relevance(payload, query) <= 0:
            return False
        weather_tokens = ["晴", "多云", "阴", "小雨", "中雨", "大雨", "暴雨", "雷阵雨", "阵雨", "雾", "霾"]
        has_weather = any(token in payload for token in weather_tokens)
        has_temp = bool(re.search(r"\d{1,2}(?:\s*[~～\-至]\s*\d{1,2})?\s*℃", payload))
        has_wind_or_air = any(token in payload for token in ["风", "级", "空气质量", "湿度"])
        return has_weather and (has_temp or has_wind_or_air)

    async def _extract_instant_answer(self, query: str) -> str:
        if not self._page:
            return ""
        try:
            cards = await self._page.evaluate(
                """() => {
                    const selectors = [
                        '.op_weather', '.weather-base', '[class*="weather"]',
                        '.op_realtime', '.op-trip', '[class*="op_weather"]',
                        '#card_weather', '.card-weather', '.result-op',
                        '.op_main', '.c-border', '[class*="card_"]',
                        '[class*="ai-summary"]', '[class*="ai_answer"]',
                        '.c-summary', '.cos-reasoning-content',
                        '[class*="wenda-answer"]', '.op-qa-module'
                    ];
                    const items = [];
                    for (const selector of selectors) {
                        document.querySelectorAll(selector).forEach((el) => {
                            const text = (el.innerText || '').trim();
                            if (text && text.length > 10) items.push(text.slice(0, 800));
                        });
                    }
                    return items;
                }"""
            )
        except Exception:
            return ""
        return self._pick_best_query_text(cards or [], query)

    async def _extract_search_snippets(self, query: str) -> str:
        if not self._page:
            return ""
        try:
            items = await self._page.evaluate(
                """() => {
                    const results = [];
                    const seen = new Set();
                    document.querySelectorAll('.result.c-container, .c-container, .result').forEach((el) => {
                        const title = (el.querySelector('h3 a, .t a')?.innerText || '').trim();
                        const snippet = (
                            el.querySelector(
                                '.c-abstract, .c-span-last, .content-right_8Zs40, ' +
                                '.c-gap-top-xsmall, .c-color-text'
                            )?.innerText || ''
                        ).trim();
                        const text = [title, snippet].filter(Boolean).join('：');
                        if (text && !seen.has(text)) {
                            seen.add(text);
                            results.push(text.slice(0, 500));
                        }
                    });
                    return results.slice(0, 8);
                }"""
            )
        except Exception:
            return ""
        filtered = self._sort_texts_by_query(items or [], query)
        return "\n".join(filtered[:5])

    async def _click_and_extract_first_result(self, query: str) -> str:
        if not self._page:
            return ""
        search_url = self._page.url
        try:
            candidates = []
            selectors = [".result h3 a", ".c-container h3 a", ".t a", ".result-op h3 a"]
            for selector in selectors:
                locator = self._page.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    count = 0
                count = min(count, 6)
                for index in range(count):
                    item = locator.nth(index)
                    try:
                        if not await item.is_visible(timeout=1000):
                            continue
                        title = (await item.inner_text() or "").strip()
                        href = await item.get_attribute("href")
                        candidates.append((self._score_query_relevance(title, query), item, href))
                    except Exception:
                        continue
                if candidates:
                    break
            if not candidates:
                return ""
            candidates.sort(key=lambda row: row[0], reverse=True)
            _, first_link, href = candidates[0]
            if href:
                await self._page.goto(href, wait_until="domcontentloaded", timeout=15000)
            else:
                await first_link.click(timeout=5000)
            await asyncio.sleep(2.0)
            content = await self._page.evaluate(
                """() => {
                    const selectors = [
                        'article', '.article-content', '.post-content',
                        '#article_content', '.content', '#content',
                        '.detail-content', '.news-content', '.weather-info',
                        '.t-day', '.today', 'main', '#main', '.main-content'
                    ];
                    for (const selector of selectors) {
                        const el = document.querySelector(selector);
                        const text = (el?.innerText || '').trim();
                        if (text && text.length > 20) return text.slice(0, 1200);
                    }
                    return (document.body?.innerText || '').trim().slice(0, 1200);
                }"""
            )
            return self._pick_best_query_text([content or ""], query)
        except Exception:
            return ""
        finally:
            try:
                await self._page.goto(search_url, wait_until="domcontentloaded", timeout=10000)
                await asyncio.sleep(0.8)
            except Exception:
                try:
                    await self._page.go_back(wait_until="domcontentloaded", timeout=10000)
                    await asyncio.sleep(0.8)
                except Exception:
                    pass

    @staticmethod
    def _query_terms(query: str):
        text = re.sub(r"\s+", " ", str(query or "")).strip()
        if not text:
            return []
        stopwords = {"打开", "浏览器", "搜索", "查询", "查找", "一下", "请", "帮我", "帮忙", "天气", "如何", "怎么", "是什么", "保存", "到", "并", "和", "与"}
        parts = re.split(r"[\s,，。.;；:：/\\|]+", text)
        terms = []
        for part in parts:
            cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9_-]", "", part).strip()
            if not cleaned or cleaned in stopwords:
                continue
            if cleaned.endswith("天气") and len(cleaned) > 2:
                cleaned = cleaned[:-2]
            if cleaned and cleaned not in terms:
                terms.append(cleaned)
        return terms

    def _score_query_relevance(self, text: str, query: str) -> int:
        payload = str(text or "").strip()
        if not payload:
            return -999
        score = 0
        lowered = payload.lower()
        for term in self._query_terms(query):
            if term.lower() in lowered:
                score += 6
        query_text = str(query or "").strip().lower()
        if query_text and query_text in lowered:
            score += 8
        if "天气" in query and "天气" in payload:
            score += 2
        if any(token in payload for token in ["当前", "温度", "气温", "最高", "最低", "晴", "阴", "多云", "小雨"]):
            score += 2
        if re.search(r"\d", payload):
            score += 1
        return score

    def _sort_texts_by_query(self, texts, query: str):
        ranked = []
        seen = set()
        for item in texts:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            ranked.append((self._score_query_relevance(text, query), text))
        ranked.sort(key=lambda row: row[0], reverse=True)
        return [text for _, text in ranked]

    def _pick_best_query_text(self, texts, query: str) -> str:
        ranked = self._sort_texts_by_query(texts, query)
        if not ranked:
            return ""
        best = ranked[0]
        if self._score_query_relevance(best, query) <= 0 and len(ranked) > 1:
            return ranked[1]
        return best

    def _prefer_query_relevant_lines(self, text: str, query: str):
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        if not lines:
            return []
        scored = [(self._score_query_relevance(line, query), line) for line in lines]
        strong = [line for score, line in scored if score > 0]
        return strong or lines

    def _clean_extracted_text(self, text: str, query: str = "") -> str:
        if not text:
            return ""
        cleaned = str(text).replace("\r\n", "\n").replace("\r", "\n")
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{2,}", "\n", cleaned)
        lines = [line.strip() for line in cleaned.split("\n") if line.strip()]
        lines = self._prefer_query_relevant_lines("\n".join(lines), query)
        compacted = []
        for line in lines:
            digit_ratio = sum(ch.isdigit() for ch in line) / max(len(line), 1)
            if digit_ratio > 0.45 and self._score_query_relevance(line, query) <= 0:
                continue
            if len(line) <= 1:
                continue
            compacted.append(line)
        if not compacted:
            compacted = lines
        return "\n".join(compacted[:6])

    def _is_quality_content(self, text: str, query: str = "") -> bool:
        payload = str(text or "").strip()
        if len(payload) < 12:
            return False
        lines = [line.strip() for line in payload.split("\n") if line.strip()]
        if not lines:
            return False
        unique_lines = list(dict.fromkeys(lines))
        if len(unique_lines) == 1 and len(unique_lines[0]) < 18:
            return False
        if len(unique_lines) / max(len(lines), 1) < 0.5:
            return False
        if query and max(self._score_query_relevance(line, query) for line in unique_lines) <= 0:
            return False
        title_like = [line for line in unique_lines if re.match(r"^[\[\(（【].+[\]\)）】]?$", line) or line.startswith("http")]
        if len(title_like) / max(len(unique_lines), 1) > 0.6:
            return False
        return True

    async def browser_send(self, to, subject="", body="", attachments=None, page_key=None):
        lock = self._ensure_browser_lock()
        async with lock:
            page = await self._get_browser_page(page_key=page_key, create=True)
            self._page = page
            if not self._page:
                return False

            attachments = list(attachments or [])

            compose_selectors = [
                "text=/写邮件|撰写|Compose|New message/i",
                "button:has-text('写邮件')",
                "button:has-text('撰写')",
                "button:has-text('Compose')",
                "a:has-text('写邮件')",
                "a:has-text('Compose')",
            ]
            for selector in compose_selectors:
                try:
                    locator = self._page.locator(selector).first
                    if await locator.is_visible(timeout=1500):
                        await locator.click()
                        await asyncio.sleep(1.0)
                        break
                except Exception:
                    continue

            if not await self._fill_first_visible(
                [
                    "input[type='email']",
                    "input[name='to']",
                    "input[placeholder*='收件']",
                    "input[aria-label*='收件']",
                    "textarea[placeholder*='收件']",
                ],
                to,
            ):
                return False

            if subject:
                await self._fill_first_visible(
                    [
                        "input[name='subject']",
                        "input[placeholder*='主题']",
                        "input[aria-label*='主题']",
                        "input[placeholder*='Subject']",
                    ],
                    subject,
                )

            if body:
                await self._fill_first_visible(
                    [
                        "div[contenteditable='true']",
                        "textarea",
                    ],
                    body,
                )

            if attachments:
                attached = False
                for selector in ["input[type='file']", "input[accept]"]:
                    try:
                        locator = self._page.locator(selector).first
                        if await locator.count() > 0:
                            await locator.set_input_files(attachments)
                            await asyncio.sleep(1.0)
                            attached = True
                            break
                    except Exception:
                        continue
                if not attached:
                    return False

            for selector in [
                "button:has-text('发送')",
                "button:has-text('Send')",
                "[role='button']:has-text('发送')",
                "[role='button']:has-text('Send')",
            ]:
                try:
                    locator = self._page.locator(selector).first
                    if await locator.is_visible(timeout=1500):
                        await locator.click()
                        await asyncio.sleep(2.0)
                        return True
                except Exception:
                    continue
            return False

    async def _fill_first_visible(self, selectors, text):
        for selector in selectors:
            try:
                locator = self._page.locator(selector).first
                if await locator.is_visible(timeout=1500):
                    await locator.click()
                    try:
                        await locator.fill(text)
                    except Exception:
                        await locator.evaluate("el => el.value = ''")
                        await locator.type(text, delay=40)
                    return True
            except Exception:
                continue
        return False

    async def open_wps_file(self, file_path):
        """WPS 文件打开逻辑（针对国产环境优化）
        麒麟系统 WPS 使用 prome_fushion 融合模式：
        - 第一次 'et' 命令启动 WPS 首页（CEF WebView，不接受 xdotool 输入）
        - 第二次 'et filename' 在已有实例中打开文件，窗口变成真正的表格
        因此必须两步启动：先启动 WPS，再打开文件。"""
        try:
            # 清理路径
            create_new = False
            if file_path:
                file_path = file_path.strip("'\"")
                file_path = os.path.expanduser(file_path)
                file_path = os.path.abspath(file_path)
                if not os.path.exists(file_path):
                    if file_path.endswith((".et", ".xlsx", ".xls")):
                        print(f"WPS 文件不存在，将新建: {file_path}")
                        create_new = True
                    else:
                        print(f"WPS 文件不存在: {file_path}")
                        return False

            # Step 1: 检查 WPS 是否已在运行
            existing_wid = self._find_wps_window()
            wps_already_running = existing_wid is not None

            if not wps_already_running:
                # Step 2: 启动 WPS（第一次 et 命令，显示首页）
                cmd = "export DISPLAY=:0 && export GTK_MODULES=gail:atk-bridge && export QT_ACCESSIBILITY=1 && nohup et > /dev/null 2>&1 &"
                print(f"启动 WPS: {cmd}")
                subprocess.Popen(cmd, shell=True, start_new_session=True)

                # 等待 WPS 窗口出现（最多 15 秒）
                wid = None
                for _ in range(30):
                    await asyncio.sleep(0.5)
                    wid = self._find_wps_window()
                    if wid:
                        break

                if not wid:
                    print("WPS 窗口未出现，等待超时")
                    return False

                print(f"WPS 首页已出现: {wid}")
                # 首页是 CEF WebView，无法用 xdotool 操作
                # 需要等待 WPS 完全加载
                await asyncio.sleep(3)

            # Step 3: 用 et 命令在已有实例中打开文件（第二次 et 命令）
            # 这一步会把首页窗口变成真正的表格窗口
            if file_path and not create_new:
                # 打开已有文件
                open_cmd = f"export DISPLAY=:0 && nohup et {shlex.quote(file_path)} > /dev/null 2>&1 &"
                print(f"打开文件: {open_cmd}")
                subprocess.Popen(open_cmd, shell=True, start_new_session=True)
            else:
                # 没有指定文件或文件不存在，创建有效的 .xlsx 模板文件并打开
                # 注意: 不能用空文件或 touch 创建，否则 Ctrl+S 会弹出"另存为"对话框
                if not file_path:
                    file_path = "/tmp/wps_new_spreadsheet.xlsx"
                self._create_blank_spreadsheet(file_path)
                open_cmd = f"export DISPLAY=:0 && nohup et {shlex.quote(file_path)} > /dev/null 2>&1 &"
                print(f"新建表格: {open_cmd}")
                subprocess.Popen(open_cmd, shell=True, start_new_session=True)

            # 等待表格窗口出现（窗口名应包含 .et 或 工作簿）
            for _ in range(20):
                await asyncio.sleep(0.5)
                wid = self._find_wps_window()
                if wid:
                    window_name = subprocess.run(
                        ["xdotool", "getwindowname", wid],
                        capture_output=True, text=True,
                    ).stdout.strip()
                    # 检查是否已变成表格窗口
                    if any(kw in window_name for kw in [".et", ".xlsx", ".xls", "工作簿", "Sheet"]):
                        print(f"表格窗口已打开: {window_name}")
                        self._wps_window_id = wid  # 记住窗口ID
                        return True

            # 即使没检测到表格关键词，也返回 True（可能窗口名格式不同）
            wid = self._find_wps_window()
            if wid:
                window_name = subprocess.run(
                    ["xdotool", "getwindowname", wid],
                    capture_output=True, text=True,
                ).stdout.strip()
                print(f"WPS 窗口: {window_name}")
                self._wps_window_id = wid  # 记住窗口ID
                return True

            print("WPS 窗口未找到")
            return False
        except Exception as e:
            print(f"打开 WPS 失败: {e}")
            return False

    def _create_blank_spreadsheet(self, file_path: str):
        """创建一个有效的空白 xlsx 文件，WPS 可直接打开和保存。
        空文件或 touch 创建的文件会导致 Ctrl+S 弹出"另存为"对话框。"""
        build_dir = tempfile.mkdtemp(prefix="wps_build_")
        try:
            os.makedirs(os.path.join(build_dir, "xl", "worksheets"), exist_ok=True)
            os.makedirs(os.path.join(build_dir, "xl", "_rels"), exist_ok=True)
            os.makedirs(os.path.join(build_dir, "_rels"), exist_ok=True)

            with open(os.path.join(build_dir, "[Content_Types].xml"), "w") as f:
                f.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                '</Types>')

            with open(os.path.join(build_dir, "_rels", ".rels"), "w") as f:
                f.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>')

            with open(os.path.join(build_dir, "xl", "workbook.xml"), "w") as f:
                f.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
                '</workbook>')

            with open(os.path.join(build_dir, "xl", "_rels", "workbook.xml.rels"), "w") as f:
                f.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '</Relationships>')

            with open(os.path.join(build_dir, "xl", "worksheets", "sheet1.xml"), "w") as f:
                f.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData/>'
                '</worksheet>')

            with open(os.path.join(build_dir, "xl", "sharedStrings.xml"), "w") as f:
                f.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="0" uniqueCount="0"/>')

            with zipfile.ZipFile(file_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for root, dirs, files in os.walk(build_dir):
                    for fname in files:
                        full = os.path.join(root, fname)
                        arcname = os.path.relpath(full, build_dir)
                        z.write(full, arcname)
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)

    def wps_input_cell(self, cell, text):
        """WPS 指定单元格输入（从 A1 用方向键导航定位）
        方法：Ctrl+Home 回到 A1，然后按方向键移动到目标单元格，粘贴文本。
        不依赖固定屏幕坐标，纯键盘操作，更可靠。"""
        # 优先使用缓存的窗口ID，避免多窗口冲突
        wid = self._wps_window_id or self._find_wps_window()
        if wid:
            try:
                # 1. 验证并解析单元格格式 (例如 G5, AA10)
                match = re.match(r'^([a-zA-Z]+)([0-9]+)$', cell)
                if not match:
                    print(f"无效的单元格格式: {cell}")
                    return False
                col_str, row_str = match.groups()
                cell_ref = cell.upper()

                # 列字母转数字: A=1, B=2, ..., Z=26, AA=27, ...
                col_num = 0
                for ch in col_str.upper():
                    col_num = col_num * 26 + (ord(ch) - ord('A') + 1)
                row_num = int(row_str)

                # 2. 存入剪贴板
                process = subprocess.Popen(['xclip', '-selection', 'clipboard'], stdin=subprocess.PIPE)
                process.communicate(input=text.encode('utf-8'))
                time.sleep(0.3)

                # 3. 激活窗口（用 wmctrl 而非 xdotool，避免 BadMatch 错误）
                try:
                    wid_hex = hex(int(wid))
                    subprocess.run(["wmctrl", "-i", "-a", wid_hex], check=False, capture_output=True)
                except Exception as focus_exc:
                    print(f"警告：无法激活窗口 {wid}: {focus_exc}")
                time.sleep(0.8)

                # 4. 退出编辑状态
                subprocess.run(["xdotool", "key", "Escape"], check=False)
                time.sleep(0.2)

                # 5. Ctrl+Home 回到 A1
                subprocess.run(["xdotool", "key", "ctrl+Home"], check=False)
                time.sleep(0.3)

                # 6. 用方向键从 A1 导航到目标单元格
                # 右移 (col_num-1) 次，下移 (row_num-1) 次
                right_count = col_num - 1
                down_count = row_num - 1

                if right_count > 0:
                    # 用 xdotool key 重复按键
                    for _ in range(right_count):
                        subprocess.run(["xdotool", "key", "Right"], check=False)
                    time.sleep(0.1)
                if down_count > 0:
                    for _ in range(down_count):
                        subprocess.run(["xdotool", "key", "Down"], check=False)
                    time.sleep(0.1)
                time.sleep(0.3)

                # 7. 粘贴文本
                print(f"正在向 {cell_ref} 粘贴文本: {text}")
                subprocess.run(["xdotool", "key", "ctrl+v"], check=False)
                time.sleep(0.3)

                # 8. 回车确认输入
                subprocess.run(["xdotool", "key", "Return"], check=False)
                time.sleep(0.3)

                # 9. 保存文件（Ctrl+S），确保内容写入磁盘
                subprocess.run(["xdotool", "key", "ctrl+s"], check=False)
                time.sleep(1)

                return True
            except Exception as e:
                print(f"指定单元格输入逻辑出错: {e}")
                return False
        else:
            print("未能找到 WPS 窗口")
        return False

    def read_spreadsheet_cell(self, file_path: str, cell: str) -> str:
        """直接从 xlsx 文件读取指定单元格文本，用于写入后的磁盘校验。"""
        try:
            with zipfile.ZipFile(file_path, "r") as workbook:
                shared_strings = []
                if "xl/sharedStrings.xml" in workbook.namelist():
                    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
                    for item in root.findall(".//{*}si"):
                        text = "".join(node.text or "" for node in item.findall(".//{*}t"))
                        shared_strings.append(text)

                sheet_paths = self._spreadsheet_sheet_paths(workbook)
                for sheet_path in sheet_paths:
                    if sheet_path not in workbook.namelist():
                        continue
                    sheet_root = ET.fromstring(workbook.read(sheet_path))
                    for cell_node in sheet_root.findall(".//{*}c"):
                        if str(cell_node.attrib.get("r", "")).upper() != str(cell).upper():
                            continue
                        return self._read_spreadsheet_cell_node(cell_node, shared_strings)
        except Exception as exc:
            print(f"读取表格单元格失败: {exc}")
        return ""

    def read_spreadsheet_vertical_range(self, file_path: str, start_cell: str, line_count: int) -> list:
        values = []
        if line_count <= 0:
            return values
        col_name, row_num = self._split_spreadsheet_cell_ref(start_cell)
        if not col_name or row_num <= 0:
            return values
        for offset in range(line_count):
            values.append(self.read_spreadsheet_cell(file_path, f"{col_name}{row_num + offset}"))
        return values

    @staticmethod
    def _split_spreadsheet_cell_ref(cell: str) -> tuple:
        match = re.match(r"^([A-Za-z]+)([0-9]+)$", str(cell or "").strip())
        if not match:
            return "", 0
        return match.group(1).upper(), int(match.group(2))

    def _spreadsheet_sheet_paths(self, workbook) -> list:
        default_path = "xl/worksheets/sheet1.xml"
        try:
            if "xl/workbook.xml" not in workbook.namelist():
                return [default_path]

            rel_targets = {}
            if "xl/_rels/workbook.xml.rels" in workbook.namelist():
                rel_root = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
                for rel in rel_root.findall(".//{*}Relationship"):
                    rel_id = str(rel.attrib.get("Id", "")).strip()
                    target = str(rel.attrib.get("Target", "")).strip()
                    if not rel_id or not target:
                        continue
                    normalized = target.lstrip("/")
                    if not normalized.startswith("xl/"):
                        normalized = "xl/" + normalized.lstrip("./")
                    rel_targets[rel_id] = normalized

            workbook_root = ET.fromstring(workbook.read("xl/workbook.xml"))
            paths = []
            for sheet in workbook_root.findall(".//{*}sheet"):
                rel_id = (
                    sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                    or sheet.attrib.get("id")
                    or ""
                )
                sheet_path = rel_targets.get(str(rel_id).strip())
                if sheet_path:
                    paths.append(sheet_path)

            if paths:
                return paths
        except Exception:
            pass
        return [default_path]

    def _read_spreadsheet_cell_node(self, cell_node, shared_strings: list) -> str:
        cell_type = str(cell_node.attrib.get("t", "") or "").strip()
        if cell_type == "inlineStr":
            return "".join(node.text or "" for node in cell_node.findall(".//{*}is//{*}t"))

        value_node = cell_node.find("{*}v")
        raw_value = ""
        if value_node is not None and value_node.text is not None:
            raw_value = str(value_node.text)

        if cell_type == "s":
            try:
                index = int(raw_value)
            except Exception:
                return ""
            return shared_strings[index] if 0 <= index < len(shared_strings) else ""

        if raw_value:
            return raw_value

        formula_text = "".join(node.text or "" for node in cell_node.findall(".//{*}f"))
        if formula_text:
            return formula_text

        return "".join(node.text or "" for node in cell_node.findall(".//{*}t"))

    async def open_terminal(self):
        """打开一个新的终端窗口。

        说明：为了避免“焦点不在新窗口导致输入落到当前终端”的问题，本方法支持在启动新终端时直接执行命令。
        """
        return await self.open_terminal_with_command(None)

    async def open_terminal_with_command(self, command: Optional[str]):
        """新开终端并（可选）执行 command。

        - 如果传入 command，会在新终端启动时执行。
        - 执行后保持终端窗口不退出（exec bash）。
        """
        candidates = [
            "ukui-terminal",
            "mate-terminal",
            "gnome-terminal",
            "konsole",
            "xfce4-terminal",
            "xterm",
        ]
        terminal = None
        for cmd in candidates:
            if shutil.which(cmd):
                terminal = cmd
                break
        if not terminal:
            return False

        if command:
            # 用 bash -lc 兼容别名/环境变量；最后 exec bash 保持窗口
            bash_cmd = f"bash -lc {shlex.quote(command + '; exec bash')}"
            if terminal in ["gnome-terminal", "mate-terminal", "ukui-terminal"]:
                # gnome-terminal 新窗口执行命令
                # 注意：不同终端对 --window 支持不完全一致，但不影响命令执行；失败时用户仍可看到窗口
                launch = f"export DISPLAY=:0 && {terminal} --window -- {bash_cmd}"
            elif terminal == "konsole":
                launch = f"export DISPLAY=:0 && konsole -e {bash_cmd}"
            elif terminal == "xfce4-terminal":
                launch = f"export DISPLAY=:0 && xfce4-terminal --command={shlex.quote(bash_cmd)}"
            else:  # xterm
                launch = f"export DISPLAY=:0 && xterm -e {bash_cmd}"
        else:
            # 仅打开新终端窗口
            if terminal in ["gnome-terminal", "mate-terminal", "ukui-terminal"]:
                launch = f"export DISPLAY=:0 && {terminal} --window"
            else:
                launch = f"export DISPLAY=:0 && {terminal}"

        print(f"将启动终端: {terminal}")
        print(f"启动命令: {launch}")

        # 为了让用户看见窗口：启动后尝试把新窗口激活到前台
        old_wids = set()
        if shutil.which("xdotool"):
            try:
                proc = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", terminal], capture_output=True, text=True)
                if proc.stdout.strip():
                    old_wids = set([w for w in proc.stdout.strip().split("\n") if w])
            except Exception:
                old_wids = set()

        subprocess.Popen(launch, shell=True, start_new_session=True)

        if shutil.which("xdotool"):
            new_wid = None
            for _ in range(20):
                await asyncio.sleep(0.25)
                try:
                    proc = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", terminal], capture_output=True, text=True)
                    if not proc.stdout.strip():
                        continue
                    wids = [w for w in proc.stdout.strip().split("\n") if w]
                    # 尝试找新窗口，否则取最后一个（通常是最新的）
                    diff = [w for w in wids if w not in old_wids]
                    new_wid = (diff[-1] if diff else wids[-1])
                    if new_wid:
                        break
                except Exception:
                    continue

            if new_wid:
                print(f"检测到终端窗口ID: {new_wid}，尝试激活到前台")
                subprocess.run(["xdotool", "windowactivate", "--sync", new_wid], check=False)
                subprocess.run(["xdotool", "windowraise", new_wid], check=False)

        await asyncio.sleep(0.8)
        return True

    def _find_wps_window(self):
        """查找 WPS 表格的可见顶层窗口 ID。
        关键: 必须用 wmctrl -l 找可见的 X11 顶层窗口。
        xdotool search --class wpsoffice 找到的是 Qt 内部不可见窗口，
        对这些窗口执行 windowactivate/focus 会报 BadWindow/BadMatch 错误。
        注意: .et 文件名可能出现在文本编辑器窗口标题中，需要排除。"""
        # 1. 用 wmctrl 找可见的 WPS 窗口（最可靠）
        try:
            proc = subprocess.run(
                ["wmctrl", "-l"], capture_output=True, text=True,
            )
            if proc.returncode == 0:
                for line in proc.stdout.strip().split("\n"):
                    if not line:
                        continue
                    # wmctrl 格式: 0xHEXID DESKTOP HOSTNAME WINDOW_NAME
                    parts = line.split(None, 4)
                    if len(parts) >= 5:
                        wid_hex, window_name = parts[0], parts[4]
                        # 排除文本编辑器窗口（"文本编辑器"、"gedit"、"pluma" 等）
                        if any(kw in window_name for kw in
                               ["文本编辑器", "gedit", "pluma", "kate", "mousepad", "Notepad"]):
                            continue
                        # 匹配 WPS 相关窗口名
                        if any(kw in window_name for kw in
                               ["WPS Office", "工作簿", ".et", ".xlsx", "xls", "Sheet"]):
                            return str(int(wid_hex, 16))
        except Exception:
            pass

        # 2. 降级: 用 xdotool search --name 找（可能找到不可见窗口）
        for pattern in ["WPS Office", "工作簿", r".*\.(et|xlsx)"]:
            proc = subprocess.run(
                ["xdotool", "search", "--name", pattern],
                capture_output=True, text=True,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                ids = [w for w in proc.stdout.strip().split("\n") if w]
                for wid in reversed(ids):
                    try:
                        name = subprocess.run(
                            ["xdotool", "getwindowname", wid],
                            capture_output=True, text=True,
                        ).stdout.strip()
                        # 排除文本编辑器和云服务窗口
                        if name and "wpscloudsvr" not in name.lower() and \
                           not any(kw in name for kw in ["文本编辑器", "gedit", "pluma", "kate", "mousepad"]):
                            return wid
                    except Exception:
                        continue
        return None

