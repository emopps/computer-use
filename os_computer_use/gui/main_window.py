from __future__ import annotations

import html
import json
import os
import re
import sys
from typing import Optional

from PyQt5.QtCore import QObject, QPoint, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

Signal = pyqtSignal

from os_computer_use.app_runtime import ensure_supported_python
from os_computer_use.gui.worker import AgentWorker


def configure_linux_input_method() -> None:
    if not sys.platform.startswith("linux"):
        return

    has_sogou = (
        os.path.exists("/opt/sogouimebs/files/bin/sogouImeService")
        or os.path.exists("/opt/sogouimebs/files/bin/sogouimebs-session")
    )
    session_type = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    current_qt_im = os.environ.get("QT_IM_MODULE", "").strip().lower()

    # Kylin 上现成可用的是 Qt5 fcitx 前端；X11 + 搜狗场景优先走 fcitx。
    if current_qt_im in {"fcitx", "fcitx5", "xim"}:
        qt_im = current_qt_im
    elif has_sogou and session_type == "x11":
        qt_im = "fcitx"
    elif session_type == "x11":
        qt_im = "xim"
    else:
        qt_im = "fcitx" if has_sogou else "xim"

    gtk_im = os.environ.get("GTK_IM_MODULE", "fcitx" if has_sogou else qt_im) or (
        "fcitx" if has_sogou else qt_im
    )
    os.environ["QT_IM_MODULE"] = qt_im
    os.environ["GTK_IM_MODULE"] = gtk_im
    os.environ["QT4_IM_MODULE"] = qt_im
    os.environ["XMODIFIERS"] = os.environ.get("XMODIFIERS", "@im=fcitx") or "@im=fcitx"


class WorkerBridge(QObject):
    warmup_requested = Signal()
    process_prompt = Signal(str)
    cancel_requested = Signal()
    clarification_submitted = Signal(str)
    authorization_submitted = Signal(bool)


class UserBubble(QFrame):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.setObjectName("userBubble")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(6)

        title = QLabel("\u7528\u6237")
        title.setObjectName("userTitle")
        title.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        body = QLabel(text)
        body.setObjectName("userBody")
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout.addWidget(title)
        layout.addWidget(body)


class AssistantCard(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("assistantCard")
        self._lines: list[str] = []
        self._commands: list[tuple[str, str]] = []
        self._command_keys: set[str] = set()
        self._stream_blocks: list[QWidget] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        self.name_label = QLabel("\u684c\u9762\u667a\u80fd\u4f53")
        self.name_label.setObjectName("assistantName")
        self.stream_wrap = QWidget()
        self.stream_layout = QVBoxLayout(self.stream_wrap)
        self.stream_layout.setContentsMargins(0, 0, 0, 0)
        self.stream_layout.setSpacing(8)
        self.stream_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self.meta_label = QLabel("\u6267\u884c\u52a8\u4f5c\u4f1a\u9ad8\u4eae\u663e\u793a")
        self.meta_label.setObjectName("assistantMeta")
        self.meta_label.setWordWrap(True)
        self.meta_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.meta_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.meta_label.hide()

        layout.addWidget(self.name_label)
        layout.addWidget(self.stream_wrap)
        layout.addWidget(self.meta_label)

    def _append_block(self, widget: QWidget) -> None:
        widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._stream_blocks.append(widget)
        self.stream_layout.addWidget(widget)

    def _append_html_line(self, html_text: str) -> None:
        label = QLabel()
        label.setObjectName("assistantBody")
        label.setWordWrap(True)
        label.setTextFormat(Qt.RichText)
        label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setText(html_text)
        self._append_block(label)

    def append_text(self, text: str, color: str = "default") -> None:
        text = text.strip()
        if not text:
            return
        palette = {
            "default": "#364152",
            "cyan": "#2d86eb",
            "green": "#1f8f5f",
            "yellow": "#a36a00",
            "red": "#d04f4f",
            "gray": "#7b8596",
            "blue": "#3f7cff",
            "magenta": "#7b58d0",
        }
        safe = html.escape(text)
        html_text = (
            f'<div style="color:{palette.get(color, "#364152")}; margin-bottom:6px; background:transparent;">{safe}</div>'
        )
        self._lines.append(html_text)
        self._lines = self._lines[-40:]
        self._append_html_line(html_text)

    def add_command(self, text: str, tone: str = "run") -> None:
        key = f"{tone}:{text}"
        if key in self._command_keys:
            return
        self._command_keys.add(key)
        self._commands.append((text, tone))
        palette = {
            "run": ("#0d7a5f", "#eefaf5", "#cbeee1"),
            "done": ("#1d64d6", "#f1f6ff", "#d5e4ff"),
            "warn": ("#a45a00", "#fff6e6", "#f2ddb3"),
        }
        fg, bg, border = palette.get(tone, palette["run"])
        safe = html.escape(text.strip())
        html_text = (
            '<div style="margin:8px 0 10px 0;">'
            f'<span style="display:inline-block; color:{fg}; background:{bg}; '
            f'border:1px solid {border}; border-radius:14px; padding:10px 14px; font-weight:700;">{safe}</span>'
            "</div>"
        )
        self._lines.append(html_text)
        self._lines = self._lines[-60:]
        self._append_html_line(html_text)

    def set_meta(self, text: str) -> None:
        self.meta_label.setText(text)
        self.meta_label.setVisible(bool(str(text).strip()))

    def add_inline_widget(self, widget: QWidget) -> None:
        self._append_block(widget)

    def restore_lines(self, lines: list[str]) -> None:
        self._lines = list(lines)
        for html_text in self._lines:
            self._append_html_line(html_text)


class AuthorizationCard(QFrame):
    def __init__(self, action_type: str, details: str, on_decide) -> None:
        super().__init__()
        self.setObjectName("authorizationCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(6)

        title = QLabel("\u9700\u8981\u786e\u8ba4")
        title.setObjectName("assistantName")

        body = QLabel("\u68c0\u6d4b\u5230\u9ad8\u98ce\u9669\u64cd\u4f5c\uff0c\u8bf7\u786e\u8ba4\u662f\u5426\u7ee7\u7eed\u3002")
        body.setObjectName("assistantBody")
        body.setWordWrap(True)

        detail = QLabel("\u64cd\u4f5c\u7c7b\u578b\uff1a{}\n\u8be6\u60c5\uff1a{}".format(action_type, details))
        detail.setObjectName("assistantMeta")
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextSelectableByMouse)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        reject = QPushButton("\u62d2\u7edd")
        reject.setObjectName("minorButton")
        reject.setMinimumHeight(34)
        approve = QPushButton("\u5141\u8bb8")
        approve.setObjectName("sendButton")
        approve.setMinimumHeight(34)
        reject.clicked.connect(lambda: on_decide(False))
        approve.clicked.connect(lambda: on_decide(True))
        buttons.addWidget(reject)
        buttons.addWidget(approve)
        buttons.addStretch(1)

        layout.addWidget(title)
        layout.addWidget(body)
        layout.addWidget(detail)
        layout.addLayout(buttons)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        window_title = "\u5f00\u653e\u5f0f\u684c\u9762\u667a\u80fd\u4f53"
        self.setWindowTitle(window_title)
        os.environ["OPEN_DESKTOP_GUI_WINDOW_TITLE"] = window_title
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowCloseButtonHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowStaysOnTopHint
        )
        self.resize(800, 580)

        self._bridge = WorkerBridge()
        self._worker_thread = QThread(self)
        self._worker = AgentWorker()
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.start()

        self._bridge.warmup_requested.connect(self._worker.warmup_models)
        self._bridge.process_prompt.connect(self._worker.process_prompt)

        self._worker.warmup_status.connect(self._show_status)
        self._worker.warmup_finished.connect(self._warmup_finished)
        self._worker.busy_changed.connect(self._set_busy_state)
        self._worker.log_message.connect(self._append_log)
        self._worker.task_message.connect(self._show_status)
        self._worker.plan_ready.connect(self._load_plan)
        self._worker.operation_update.connect(self._update_operation)
        self._worker.clarification_requested.connect(self._ask_clarification)
        self._worker.clarification_consumed.connect(self._clarification_consumed)
        self._worker.authorization_requested.connect(self._ask_authorization)
        self._worker.task_finished.connect(self._task_finished)
        self._worker.task_failed.connect(self._task_failed)

        self._assistant_card: Optional[AssistantCard] = None
        self._conversation_count = 0
        self._conversation_buttons: list[QPushButton] = []
        self._active_conversation_button: Optional[QPushButton] = None
        self._conversation_data: dict[QPushButton, dict] = {}
        self._message_widgets: list[QWidget] = []
        self._awaiting_clarification = False
        self._awaiting_authorization = False
        self._pending_input_mode = ""
        self._active_authorization_card: Optional[AuthorizationCard] = None
        self._pending_authorization: Optional[dict[str, str]] = None
        self._running = False
        self._warmup_ready = False
        self._clarification_resume_active = False
        self._seen_operation_states: set[tuple[str, str]] = set()

        self._build_ui()
        self._create_conversation("当前任务")
        QTimer.singleShot(0, self._place_window)
        QTimer.singleShot(0, self._start_warmup)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._running:
            self._worker.cancel_task()
        self._worker_thread.quit()
        self._worker_thread.wait(5000)
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        QTimer.singleShot(80, self.prompt_edit.setFocus)

    def _place_window(self) -> None:
        app_instance = QApplication.instance()
        if app_instance is None:
            return
        desktop = app_instance.desktop()
        if desktop is None:
            return
        area = desktop.availableGeometry(self)
        x = area.x() + area.width() - self.width() - 20
        y = area.y() + 20
        self.move(max(area.x(), x), max(area.y(), y))

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)

        page = QHBoxLayout(root)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(156)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(18, 22, 18, 18)
        sidebar_layout.setSpacing(14)

        self.new_chat_button = QPushButton("新建对话")
        self.new_chat_button.setObjectName("newChatButton")
        sidebar_layout.addWidget(self.new_chat_button)

        self.conversation_list = QWidget()
        self.conversation_list_layout = QVBoxLayout(self.conversation_list)
        self.conversation_list_layout.setContentsMargins(0, 0, 0, 0)
        self.conversation_list_layout.setSpacing(10)
        sidebar_layout.addWidget(self.conversation_list)
        sidebar_layout.addStretch(1)
        page.addWidget(sidebar)

        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(20, 18, 20, 18)
        main_layout.setSpacing(14)
        page.addWidget(main, 1)

        hero = QLabel("欢迎使用开放式桌面智能体")
        hero.setObjectName("heroTitle")
        hero.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(hero)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setObjectName("chatScroll")
        self.chat_host = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_host)
        self.chat_layout.setContentsMargins(10, 8, 10, 8)
        self.chat_layout.setSpacing(18)
        self.chat_layout.setAlignment(Qt.AlignTop)
        self.chat_scroll.setWidget(self.chat_host)
        main_layout.addWidget(self.chat_scroll, 1)

        input_row = QHBoxLayout()
        input_row.setSpacing(12)
        self.prompt_edit = QLineEdit()
        self.prompt_edit.setObjectName("promptEdit")
        self.prompt_edit.setPlaceholderText("请输入您的问题或任务...")
        self.prompt_edit.setMinimumHeight(54)
        self.prompt_edit.setAttribute(Qt.WA_InputMethodEnabled, True)
        self.prompt_edit.setFocusPolicy(Qt.StrongFocus)
        self.prompt_edit.setInputMethodHints(Qt.ImhNone)
        self.prompt_edit.returnPressed.connect(self._run_task)

        self.clear_button = QPushButton("清空")
        self.clear_button.setObjectName("minorButton")
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("warnButton")
        self.stop_button.setVisible(False)
        self.run_button = QPushButton("发送")
        self.run_button.setObjectName("sendButton")

        input_row.addWidget(self.prompt_edit, 1)
        input_row.addWidget(self.clear_button)
        input_row.addWidget(self.stop_button)
        input_row.addWidget(self.run_button)
        main_layout.addLayout(input_row)

        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("正在启动并预加载模型...")

        self.new_chat_button.clicked.connect(self._handle_new_chat)
        self.clear_button.clicked.connect(self.prompt_edit.clear)
        self.stop_button.clicked.connect(self._cancel_task)
        self.run_button.clicked.connect(self._run_task)

        font = QFont("Microsoft YaHei UI")
        font.setStyleStrategy(QFont.PreferAntialias)
        self.setFont(font)

        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #f7f9fc;
                color: #303133;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                             "Noto Sans CJK SC", "Source Han Sans SC", sans-serif;
                font-size: 14px;
            }
            QLabel {
                background: transparent;
            }
            #sidebar {
                background: #fbfcfe;
                border-right: 1px solid #e7ebf2;
            }
            #newChatButton {
                min-height: 40px;
                border: none;
                border-radius: 20px;
                background: #3396f4;
                color: white;
                font-size: 15px;
                font-weight: 700;
            }
            QPushButton[conversation="true"] {
                min-height: 38px;
                text-align: left;
                padding: 0 14px;
                border: none;
                border-radius: 10px;
                background: #eef4fb;
                color: #4c596b;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton[conversation="true"][active="true"] {
                background: #d9ebff;
                color: #1f75d8;
            }
            QPushButton[conversation="true"][pinned="true"] {
                border: 1px solid #9fcbff;
            }
            #heroTitle {
                margin-top: 8px;
                font-size: 21px;
                font-weight: 800;
                color: #2b2f36;
            }
            #chatScroll {
                border: none;
                background: transparent;
            }
            #userBubble {
                background: #eef6ff;
                border-radius: 22px;
                border: 1px solid #d6e9ff;
            }
            #userTitle {
                color: #2f9d46;
                font-size: 14px;
                font-weight: 800;
            }
            #userBody {
                color: #2f3540;
                font-size: 15px;
                line-height: 1.55;
            }
            #assistantCard {
                background: white;
                border-radius: 22px;
                border: 1px solid #edf1f6;
            }
            #authorizationCard {
                background: #fff9ee;
                border-radius: 18px;
                border: 1px solid #f3ddab;
            }
            #assistantName {
                color: #1773d1;
                font-size: 15px;
                font-weight: 800;
            }
            #assistantBody {
                color: #364152;
                font-size: 15px;
                line-height: 1.6;
            }
            #assistantMeta {
                color: #8d96a5;
                font-size: 13px;
            }
            QAbstractScrollArea {
                background: transparent;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 10px;
                margin: 6px 2px 6px 2px;
            }
            QScrollBar::handle:vertical {
                background: #c8d3e1;
                min-height: 48px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical:hover {
                background: #aab8cb;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
            QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical,
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
                border: none;
                height: 0px;
            }
            QScrollBar:horizontal {
                background: transparent;
                height: 10px;
                margin: 2px 6px 2px 6px;
            }
            QScrollBar::handle:horizontal {
                background: #c8d3e1;
                min-width: 48px;
                border-radius: 5px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #aab8cb;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
            QScrollBar::left-arrow:horizontal, QScrollBar::right-arrow:horizontal,
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: transparent;
                border: none;
                width: 0px;
            }
            #promptEdit {
                background: white;
                border: 1.5px solid #4ca2f6;
                border-radius: 22px;
                padding: 0 16px;
                color: #2f3540;
                font-size: 15px;
                selection-background-color: #d9ebff;
                selection-color: #2f3540;
            }
            #sendButton {
                min-width: 90px;
                min-height: 44px;
                border: none;
                border-radius: 22px;
                background: #3396f4;
                color: white;
                font-size: 15px;
                font-weight: 800;
            }
            #minorButton {
                min-width: 76px;
                min-height: 44px;
                border: 1px solid #dfe5ef;
                border-radius: 22px;
                background: white;
                color: #5c6573;
                font-size: 14px;
                font-weight: 700;
            }
            #warnButton {
                min-width: 76px;
                min-height: 44px;
                border: 1px solid #f0d4d4;
                border-radius: 22px;
                background: #fff4f4;
                color: #c44a4a;
                font-size: 14px;
                font-weight: 700;
            }
            QStatusBar {
                background: #f7f9fc;
                color: #8d96a5;
            }
            """
        )

    def _set_busy_state(self, busy: bool) -> None:
        self._running = bool(busy)
        can_submit = self._warmup_ready and (not busy or self._awaiting_clarification)
        self.run_button.setEnabled(can_submit)
        self.clear_button.setEnabled(self._warmup_ready and not busy)
        self.prompt_edit.setEnabled(self._warmup_ready and (not self._awaiting_authorization))
        self.stop_button.setVisible(busy)
        self.stop_button.setEnabled(busy)
        if busy:
            self.status_bar.showMessage("执行中...")
        elif self._warmup_ready:
            self.status_bar.showMessage("就绪")
        else:
            self.status_bar.showMessage("正在启动并预加载模型...")

    def _start_warmup(self) -> None:
        self.run_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.prompt_edit.setEnabled(False)
        self._bridge.warmup_requested.emit()

    def _warmup_finished(self, ok: bool, message: str) -> None:
        self._warmup_ready = True
        self.run_button.setEnabled(True)
        self.clear_button.setEnabled(True)
        self.prompt_edit.setEnabled(True)
        self.status_bar.showMessage(message if ok else "预加载失败，发送任务时会继续尝试")

    def _cancel_task(self) -> None:
        if not self._running:
            return
        self.status_bar.showMessage("正在停止任务...")
        self._worker.cancel_task()

    def _handle_new_chat(self) -> None:
        self._conversation_count += 1
        self._create_conversation(f"新对话 {self._conversation_count}")
        self._reset_chat_content()

    def _create_conversation(self, title: str) -> None:
        button = QPushButton(title)
        button.setProperty("conversation", True)
        button.setProperty("active", False)
        button.setProperty("pinned", False)
        button.setCursor(Qt.PointingHandCursor)
        button.setContextMenuPolicy(Qt.CustomContextMenu)
        button.clicked.connect(lambda: self._activate_conversation(button))
        button.customContextMenuRequested.connect(
            lambda pos, target=button: self._show_conversation_menu(target, pos)
        )
        self.conversation_list_layout.addWidget(button)
        self._conversation_buttons.append(button)
        self._conversation_data[button] = {
            "messages": [],
            "awaiting_clarification": False,
            "awaiting_authorization": False,
            "pending_input_mode": "",
        }
        self._activate_conversation(button)

    def _activate_conversation(self, button: QPushButton) -> None:
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        for item in self._conversation_buttons:
            item.setProperty("active", item is button)
            item.style().unpolish(item)
            item.style().polish(item)
        self._active_conversation_button = button
        self._load_conversation_state(button)

    def _show_conversation_menu(self, button: QPushButton, pos: QPoint) -> None:
        menu = QMenu(self)
        rename_action = menu.addAction("重命名")
        pin_action = menu.addAction("取消置顶" if bool(button.property("pinned")) else "置顶")
        delete_action = menu.addAction("删除")
        chosen = menu.exec_(button.mapToGlobal(pos))
        if chosen is rename_action:
            self._rename_conversation(button)
        elif chosen is pin_action:
            self._toggle_pin_conversation(button)
        elif chosen is delete_action:
            self._delete_conversation(button)

    def _rename_conversation(self, button: QPushButton) -> None:
        text, ok = QInputDialog.getText(self, "重命名对话", "新的对话名称：", text=button.text())
        if ok and text.strip():
            button.setText(text.strip())

    def _toggle_pin_conversation(self, button: QPushButton) -> None:
        button.setProperty("pinned", not bool(button.property("pinned")))
        button.style().unpolish(button)
        button.style().polish(button)
        self._reorder_conversations()

    def _delete_conversation(self, button: QPushButton) -> None:
        if len(self._conversation_buttons) <= 1:
            QMessageBox.information(self, "提示", "至少保留一个对话。")
            return
        self._conversation_buttons.remove(button)
        self._conversation_data.pop(button, None)
        if self._active_conversation_button is button:
            self._active_conversation_button = None
        button.deleteLater()
        self._reorder_conversations()
        if self._conversation_buttons:
            self._activate_conversation(self._conversation_buttons[0])

    def _reorder_conversations(self) -> None:
        while self.conversation_list_layout.count():
            self.conversation_list_layout.takeAt(0)
        pinned = [b for b in self._conversation_buttons if bool(b.property("pinned"))]
        normal = [b for b in self._conversation_buttons if not bool(b.property("pinned"))]
        self._conversation_buttons = pinned + normal
        for button in self._conversation_buttons:
            self.conversation_list_layout.addWidget(button)

    def _reset_chat_content(self) -> None:
        while self.chat_layout.count() > 0:
            item = self.chat_layout.takeAt(0)
            if item is None:
                continue
            child_layout = item.layout()
            if child_layout is not None:
                while child_layout.count():
                    child = child_layout.takeAt(0)
                    widget = child.widget()
                    if widget is not None:
                        widget.deleteLater()
                child_layout.deleteLater()
        self._assistant_card = None
        self._message_widgets = []
        self._awaiting_clarification = False
        self._awaiting_authorization = False
        self._pending_input_mode = ""
        self._clarification_resume_active = False
        self._active_authorization_card = None
        self._pending_authorization = None
        self._seen_operation_states.clear()
        self.prompt_edit.clear()
        self.prompt_edit.setFocus()

    def _ensure_assistant_card(self) -> AssistantCard:
        if self._assistant_card is None:
            self._assistant_card = AssistantCard()
            self._assistant_card.set_meta("")
            self._insert_message(self._assistant_card, False)
        return self._assistant_card

    def _insert_message(self, widget: QWidget, align_right: bool) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        if align_right:
            row.addStretch(1)
            row.addWidget(widget, 0, Qt.AlignTop | Qt.AlignRight)
        else:
            row.addWidget(widget, 0, Qt.AlignTop | Qt.AlignLeft)
            row.addStretch(1)
        index = self.chat_layout.count()
        self.chat_layout.insertLayout(index, row)
        if widget not in self._message_widgets:
            self._message_widgets.append(widget)
        self._update_message_widths()
        self._scroll_bottom()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_message_widths()

    def _update_message_widths(self) -> None:
        viewport = self.chat_scroll.viewport()
        if viewport is None:
            return
        width = max(320, viewport.width())
        user_max = int(width * 0.94)
        assistant_max = int(width * 0.94)
        for widget in self._message_widgets:
            if isinstance(widget, UserBubble):
                widget.setFixedWidth(user_max)
            elif isinstance(widget, (AssistantCard, AuthorizationCard)):
                widget.setFixedWidth(assistant_max)

    def _scroll_bottom(self) -> None:
        bar = self.chat_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
    def _run_task(self) -> None:
        prompt = self.prompt_edit.text().strip()
        if not prompt:
            QMessageBox.information(self, "\u63d0\u793a", "\u8bf7\u8f93\u5165\u4efb\u52a1\u3002")
            return

        user_box = UserBubble(html.escape(prompt))
        self._insert_message(user_box, True)

        if self._pending_input_mode == "clarification" or self._awaiting_clarification:
            self._awaiting_clarification = False
            self._pending_input_mode = ""
            self._clarification_resume_active = True
            self.prompt_edit.clear()
            if self._active_conversation_button is not None:
                self._save_current_conversation_state(self._active_conversation_button)
            self._worker.submit_clarification(prompt)
            return

        self._assistant_card = AssistantCard()
        self._assistant_card.set_meta("")
        self._seen_operation_states.clear()
        self._insert_message(self._assistant_card, False)

        if self._active_conversation_button is not None:
            text = prompt[:16] + ("..." if len(prompt) > 16 else "")
            self._active_conversation_button.setText(text)
            self._save_current_conversation_state(self._active_conversation_button)

        self.prompt_edit.clear()
        self._bridge.process_prompt.emit(prompt)

    def _append_log(self, text: str, color: str) -> None:
        text = self._localize_runtime_text(text)
        hidden_markers = [
            "Checking local vision server",
            "Vision server is running:",
            "Vision server not reachable",
            "attempting to start",
            "Vision server started successfully",
            "Loading local vision model",
            "Local vision model is ready",
            "createPlatformDialogHelper",
            "Planned operations:",
            "Task summary:",
            "Decision evaluate:",
            "Task finished.",
        ]
        if any(marker in text for marker in hidden_markers):
            return

        card = self._ensure_assistant_card()

        if "Planner raw output" in text:
            summary_match = re.search(r'"summary"\s*:\s*"([^"]+)"', text)
            if summary_match:
                card.append_text(
                    "\u6a21\u578b\u5df2\u751f\u6210\u89c4\u5212\u8349\u6848\uff1a{}".format(summary_match.group(1).strip()),
                    "gray",
                )
                if self._active_conversation_button is not None:
                    self._save_current_conversation_state(self._active_conversation_button)
                self._scroll_bottom()
            return

        if re.search(r"^Executing\s+.+?\s+with args\s+.+", text):
            return

        if re.search(r"^Completed\s+.+?\s+->\s+.+", text):
            if self._active_conversation_button is not None:
                self._save_current_conversation_state(self._active_conversation_button)
            self._scroll_bottom()
            return

        if "Planning task" in text:
            if self._clarification_resume_active:
                return
            card.append_text("\u6211\u5148\u7406\u89e3\u4f60\u7684\u4efb\u52a1\uff0c\u518d\u62c6\u89e3\u6267\u884c\u987a\u5e8f\u3002", "cyan")
        elif re.match(r"^Operation\s+\S+\s+failed:", text):
            return
        elif "failed" in text.lower():
            card.append_text(text, "red")
        else:
            card.append_text(text, color)
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        self._scroll_bottom()

    def _format_args(self, raw_args: str) -> str:
        try:
            parsed = json.loads(raw_args)
        except Exception:
            return raw_args[:120]
        parts = []
        for key, value in list(parsed.items())[:3]:
            parts.append(f"{key}={value}")
        return " | ".join(parts)

    def _load_plan(self, summary: str, operations: list) -> None:
        self._clarification_resume_active = False
        card = self._ensure_assistant_card()
        card.set_meta("")
        card.append_text("\u6211\u5df2\u7ecf\u7406\u89e3\u4efb\u52a1\uff0c\u8ba1\u5212\u8fd9\u6837\u6267\u884c\uff1a", "blue")
        if operations:
            for index, operation in enumerate(operations, 1):
                card.append_text("{}. {}".format(index, self._describe_operation(operation)), "default")
        elif summary:
            card.append_text(summary, "default")
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        self._scroll_bottom()

    def _update_operation(self, operation_id: str, kind: str, status: str, error: str) -> None:
        if self._assistant_card is None:
            return
        state_key = (operation_id, status)
        if state_key in self._seen_operation_states:
            return
        self._seen_operation_states.add(state_key)
        label = self._operation_label(kind or operation_id)
        if status == "running":
            self._assistant_card.add_command(f"{label} \u8fdb\u884c\u4e2d", "run")
        elif status == "completed":
            self._assistant_card.add_command(f"{label} \u5df2\u5b8c\u6210", "done")
        elif error:
            error = self._localize_runtime_text(error)
            self._assistant_card.append_text(f"\u6b65\u9aa4 {operation_id} \u5931\u8d25\uff1a{error}", "red")
            self._assistant_card.add_command(f"{operation_id} \u5931\u8d25", "warn")
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)

    def _operation_label(self, kind: str) -> str:
        mapping = {
            "command.run": "执行命令",
            "browser.open": "打开浏览器",
            "browser.search": "浏览器搜索",
            "filesystem.open_path": "打开文件",
            "filesystem.write_text": "写入文本文件",
            "spreadsheet.open": "打开表格",
            "spreadsheet.write_cell": "写入单元格",
            "filesystem.copy": "复制文件",
            "filesystem.move": "移动文件",
            "filesystem.rename": "重命名文件",
            "filesystem.delete": "删除文件",
            "filesystem.create_folder": "创建文件夹",
        }
        return mapping.get(str(kind or ""), str(kind or "执行步骤"))

    def _show_status(self, message: str) -> None:
        self.status_bar.showMessage(message)

    def _ask_clarification(self, question: str) -> None:
        self._awaiting_clarification = True
        self._pending_input_mode = "clarification"
        self.prompt_edit.setEnabled(True)
        self.run_button.setEnabled(True)
        card = self._ensure_assistant_card()
        card.set_meta("")
        card.append_text("\u8fd8\u9700\u8981\u4f60\u8865\u5145\u4e00\u70b9\u4fe1\u606f\uff1a", "yellow")
        card.append_text(question, "yellow")
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        self.status_bar.showMessage("\u8bf7\u5728\u4e0b\u65b9\u8f93\u5165\u8865\u5145\u4fe1\u606f\u540e\u53d1\u9001")

    def _clarification_consumed(self) -> None:
        self._assistant_card = AssistantCard()
        self._assistant_card.set_meta("")
        self._seen_operation_states.clear()
        self._insert_message(self._assistant_card, False)
        self._assistant_card.append_text("\u5df2\u6536\u5230\u4f60\u7684\u8865\u5145\u4fe1\u606f\uff0c\u6b63\u5728\u7ee7\u7eed\u89c4\u5212\u3002", "cyan")
        self.status_bar.showMessage("\u5df2\u6536\u5230\u4f60\u7684\u8865\u5145\u4fe1\u606f\uff0c\u6b63\u5728\u7ee7\u7eed\u89c4\u5212")
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        self._scroll_bottom()

    def _ask_authorization(self, action_type: str, details: str) -> None:
        self._awaiting_authorization = True
        self._pending_authorization = {"action_type": action_type, "details": details}
        self.prompt_edit.setEnabled(False)
        card = self._ensure_assistant_card()
        card.append_text("\u8fd9\u4e00\u6b65\u6709\u98ce\u9669\uff0c\u9700\u8981\u4f60\u786e\u8ba4\u540e\u6211\u518d\u7ee7\u7eed\u3002", "yellow")

        def decide(allowed: bool) -> None:
            self._awaiting_authorization = False
            self._pending_input_mode = ""
            self._pending_authorization = None
            self.prompt_edit.setEnabled(True)
            self.run_button.setEnabled(True)
            if self._active_authorization_card is not None:
                self._active_authorization_card.setEnabled(False)
            self._worker.submit_authorization(allowed)
            if self._assistant_card is not None:
                self._assistant_card.append_text("\u5df2\u5141\u8bb8\u7ee7\u7eed\u3002" if allowed else "\u5df2\u62d2\u7edd\u8fd9\u4e00\u6b65\u3002", "yellow")
            if self._active_conversation_button is not None:
                self._save_current_conversation_state(self._active_conversation_button)

        self._mount_authorization_card(card, action_type, details, decide)
        self.status_bar.showMessage("\u8bf7\u786e\u8ba4\u662f\u5426\u5141\u8bb8\u6267\u884c\u8be5\u6b65\u9aa4")

    def _mount_authorization_card(self, card: AssistantCard, action_type: str, details: str, on_decide) -> None:
        auth_widget = AuthorizationCard(action_type, details, on_decide)
        self._active_authorization_card = auth_widget
        card.add_inline_widget(auth_widget)
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        self._scroll_bottom()

    def _localize_runtime_text(self, text: str) -> str:
        value = str(text or "")
        replacements = [
            ("Planning task...", "正在规划任务..."),
            ("Planner retrying with failure context...", "规划失败，正在结合错误上下文重试..."),
            ("Planner switching to visual fallback...", "规划执行失败，正在切换到视觉兜底..."),
            ("Switching to visual fallback execution.", "已切换到视觉兜底执行。"),
            ("Visual fallback finished.", "视觉兜底执行完成。"),
            ("AT-SPI 观察未给出可执行动作，升级为截图视觉分析。", "AT-SPI 未给出可执行动作，正在升级为截图视觉分析。"),
            ("AT-SPI 动作信息不足，升级为截图视觉分析。", "AT-SPI 动作信息不足，正在升级为截图视觉分析。"),
            ("Repetitive action detected, forcing finish to avoid loop.", "检测到重复动作，为避免循环已停止视觉兜底。"),
            ("Visual fallback reached the step limit without finishing the task.", "视觉兜底达到步骤上限，任务仍未完成。"),
            ("click requires x and y.", "点击缺少坐标 x 和 y。"),
            ("type_text requires text.", "输入动作缺少文本内容。"),
            ("press_key requires key.", "按键动作缺少按键参数。"),
            ("browser_search requires text.", "浏览器搜索缺少搜索词。"),
            ("Visual fallback requires a non-empty instruction.", "视觉兜底需要非空任务描述。"),
        ]
        for source, target in replacements:
            value = value.replace(source, target)
        value = value.replace("Visual fallback captured screenshot:", "视觉兜底已保存截图：")
        value = re.sub(
            r"Visual observation attempt (\d+) with screenshot (.+)",
            r"视觉观察第 \1 次，截图路径：\2",
            value,
        )
        value = re.sub(
            r"Visual observation attempt (\d+) failed: (.+)",
            r"视觉观察第 \1 次失败：\2",
            value,
        )
        value = re.sub(
            r"Decision retry limit reached, task incomplete: (.+)",
            r"达到决策重试上限，任务仍未完成：\1",
            value,
        )
        value = re.sub(
            r"Decision not satisfied, replanning \(attempt (\d+)/(\d+)\)\.\.\.",
            r"结果未满足要求，正在重新规划（第 \1/\2 次）...",
            value,
        )
        value = re.sub(
            r"Decision evaluate: satisfied=(True|False), reason=(.+)",
            lambda m: "结果评估：{}，原因：{}".format("已满足" if m.group(1) == "True" else "未满足", m.group(2)),
            value,
        )
        return value

    def _restore_authorization_card(self, action_type: str, details: str) -> None:
        if self._assistant_card is None or self._active_authorization_card is not None:
            return

        def decide(allowed: bool) -> None:
            self._awaiting_authorization = False
            self._pending_input_mode = ""
            self._pending_authorization = None
            self.prompt_edit.setEnabled(True)
            self.run_button.setEnabled(True)
            if self._active_authorization_card is not None:
                self._active_authorization_card.setEnabled(False)
            self._worker.submit_authorization(allowed)
            if self._assistant_card is not None:
                self._assistant_card.append_text("\u5df2\u5141\u8bb8\u7ee7\u7eed\u3002" if allowed else "\u5df2\u62d2\u7edd\u8fd9\u4e00\u6b65\u3002", "yellow")
            if self._active_conversation_button is not None:
                self._save_current_conversation_state(self._active_conversation_button)

        self._mount_authorization_card(self._assistant_card, action_type, details, decide)

    def _task_finished(self, payload: dict) -> None:
        self._awaiting_clarification = False
        self._awaiting_authorization = False
        self._pending_input_mode = ""
        self._clarification_resume_active = False
        self._active_authorization_card = None
        self._pending_authorization = None
        evaluation = payload.get("evaluation", {})
        if self._assistant_card is not None:
            satisfied = bool(evaluation.get("satisfied", True))
            reason = self._localize_runtime_text(str(evaluation.get("reason", "") or ""))
            if satisfied:
                self._assistant_card.append_text("\u6574\u4e2a\u4efb\u52a1\u5df2\u7ecf\u5b8c\u6210\u3002", "green")
                if reason and reason not in {"not executed", ""}:
                    self._assistant_card.append_text(f"\u7ed3\u679c\u8bc4\u4f30\uff1a{reason}", "gray")
            else:
                self._assistant_card.append_text("\u6267\u884c\u5df2\u7ed3\u675f\uff0c\u4f46\u7ed3\u679c\u6821\u9a8c\u672a\u901a\u8fc7\u3002", "yellow")
                if reason and reason not in {"not executed", ""}:
                    self._assistant_card.append_text(f"\u672a\u901a\u8fc7\u539f\u56e0\uff1a{reason}", "yellow")
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)

    def _task_failed(self, error: str) -> None:
        error = self._localize_runtime_text(error)
        self._awaiting_clarification = False
        self._awaiting_authorization = False
        self._pending_input_mode = ""
        self._clarification_resume_active = False
        self._active_authorization_card = None
        self._pending_authorization = None
        if self._assistant_card is not None:
            message = "\u4efb\u52a1\u5df2\u53d6\u6d88\u3002" if "\u53d6\u6d88" in str(error) else f"\u4efb\u52a1\u5931\u8d25\uff1a{error}"
            self._assistant_card.append_text(message, "red" if "\u53d6\u6d88" not in str(error) else "yellow")
            self._assistant_card.add_command("\u4efb\u52a1\u4e2d\u65ad", "warn")
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        self.status_bar.showMessage("\u4efb\u52a1\u5df2\u53d6\u6d88" if "\u53d6\u6d88" in str(error) else "\u4efb\u52a1\u5931\u8d25")

    def _save_current_conversation_state(self, button: QPushButton) -> None:
        messages = []
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item is None:
                continue
            row_layout = item.layout()
            if row_layout is None:
                continue
            for j in range(row_layout.count()):
                widget = row_layout.itemAt(j).widget()
                if isinstance(widget, UserBubble):
                    title = widget.layout().itemAt(0).widget().text()
                    body = widget.layout().itemAt(1).widget().text()
                    messages.append({"type": "user", "title": title, "body": body})
                elif isinstance(widget, AssistantCard):
                    messages.append(
                        {
                            "type": "assistant",
                            "lines": list(widget._lines),
                            "meta": widget.meta_label.text(),
                        }
                    )
        self._conversation_data[button] = {
            "messages": messages,
            "awaiting_clarification": self._awaiting_clarification,
            "awaiting_authorization": self._awaiting_authorization,
            "pending_input_mode": self._pending_input_mode,
            "pending_authorization": dict(self._pending_authorization or {}),
        }

    def _load_conversation_state(self, button: QPushButton) -> None:
        data = self._conversation_data.get(button, {})
        self._reset_chat_content()
        self._awaiting_clarification = bool(data.get("awaiting_clarification", False))
        self._awaiting_authorization = bool(data.get("awaiting_authorization", False))
        self._pending_input_mode = str(data.get("pending_input_mode", "") or "")
        pending_authorization = dict(data.get("pending_authorization", {}) or {})
        self._pending_authorization = pending_authorization or None
        messages = data.get("messages", [])
        for message in messages:
            if message.get("type") == "user":
                box = UserBubble(message.get("body", ""))
                self._insert_message(box, True)
            elif message.get("type") == "assistant":
                card = AssistantCard()
                card.restore_lines(list(message.get("lines", [])))
                card.set_meta("")
                self._assistant_card = card
                self._insert_message(card, False)
        if self._awaiting_authorization and self._pending_authorization and self._assistant_card is not None:
            self._restore_authorization_card(
                self._pending_authorization.get("action_type", ""),
                self._pending_authorization.get("details", ""),
            )
        self.prompt_edit.setFocus()

    def _describe_operation(self, operation: dict) -> str:
        kind = str(operation.get("kind", "") or "")
        args = dict(operation.get("arguments", {}) or {})
        if kind == "filesystem.open_path":
            return "打开文件 {}".format(args.get("path", ""))
        if kind == "filesystem.write_text":
            return "编辑文本文件 {} 并写入内容".format(args.get("file_path", ""))
        if kind == "spreadsheet.open":
            return "打开表格 {}".format(args.get("file_path") or args.get("file_name") or "")
        if kind == "spreadsheet.write_cell":
            return "在 {} 的 {} 单元格写入内容".format(
                args.get("file_path") or args.get("file_name") or "表格",
                args.get("cell", ""),
            )
        if kind == "browser.open":
            return "打开浏览器并进入 {}".format(args.get("url", ""))
        if kind == "browser.search":
            return "在浏览器中搜索 {}".format(args.get("text", ""))
        if kind == "command.run":
            return "执行命令 {}".format(args.get("command", ""))
        return str(operation.get("description", kind) or kind)


def launch() -> int:
    configure_linux_input_method()
    ensure_supported_python()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return int(app.exec_())
