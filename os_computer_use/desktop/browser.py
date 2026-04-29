import time
import threading
from multiprocessing import Process, Queue
try:
    import webview
except ImportError:
    webview = None

class Browser:
    def __init__(self):
        self.width = 1024
        self.height = 768
        self.window_frame_height = 29  # Additional px for window border
        self.command_queue = Queue()
        self.webview_process = None
        self.is_running = False

    def open(self, url, width=None, height=None):
        """
        打开一个带有指定 URL 的浏览器窗口

        参数：
            url (str): 要打开的 URL
            width (int, 可选): 窗口宽度
            height (int, 可选): 窗口高度
        """
        if not webview:
            print(f"警告: 未安装 pywebview，无法打开窗口。请手动访问: {url}")
            return

        if self.is_running:
            print("浏览器窗口已在运行")
            return

        self.width = width or self.width
        self.height = height or self.height

        print(f"URL: {url}")

        # Start webview in separate process
        self.webview_process = Process(
            target=self._create_window,
            args=(url, self.width, self.height, self.command_queue),
        )
        self.webview_process.start()
        self.is_running = True

    def close(self):
        """关闭浏览器窗口"""
        if not webview:
            return

        if not self.is_running:
            print("没有正在运行的浏览器窗口")
            return

        self.command_queue.put("close")
        if self.webview_process:
            self.webview_process.join()
            self.webview_process = None
        self.is_running = False

    @staticmethod
    def _create_window(url, width, height, command_queue):
        """Create a webview window in a separate process"""

        def check_queue():
            while True:
                if not command_queue.empty():
                    command = command_queue.get()
                    if command == "close":
                        window.destroy()
                        break
                time.sleep(1)  # Check every second

        window_frame_height = 29
        window = webview.create_window(
            "Browser Window", url, width=width, height=height + window_frame_height
        )

        # Start queue checking in a separate thread
        t = threading.Thread(target=check_queue)
        t.daemon = True
        t.start()

        webview.start()
