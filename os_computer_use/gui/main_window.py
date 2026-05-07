from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from PyQt5.QtCore import QObject, QPoint, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QFontMetrics
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
        self._lines: List[str] = []
        self._commands: List[Tuple[str, str]] = []
        self._command_keys: Set[str] = set()
        self._stream_blocks: List[QWidget] = []
        self._operation_rows: Dict[str, OperationStatusRow] = {}
        self._operation_snapshots: List[Dict[str, str]] = []
        self._stream_snapshots: List[Dict[str, str]] = []

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
            "muted": "#5d6b7c",
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
            f'<div style="color:{palette.get(color, "#364152")}; margin-bottom:6px; background:transparent; font-weight:500;">{safe}</div>'
        )
        self._lines.append(html_text)
        self._lines = self._lines[-40:]
        self._append_html_line(html_text)
        self._stream_snapshots.append({"type": "html", "html": html_text})

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
        self._stream_snapshots.append({"type": "html", "html": html_text})

    def upsert_operation(self, operation_id: str, label: str, status: str) -> None:
        row = self._operation_rows.get(operation_id)
        if row is None:
            row = OperationStatusRow(label)
            self._operation_rows[operation_id] = row
            self._append_block(row)
            self._operation_snapshots.append({"id": operation_id, "label": label, "status": status})
            self._stream_snapshots.append(
                {"type": "operation", "id": operation_id, "label": label, "status": status}
            )
        row.set_status(status)
        for snapshot in self._operation_snapshots:
            if snapshot.get("id") == operation_id:
                snapshot["label"] = label
                snapshot["status"] = status
                break
        for snapshot in self._stream_snapshots:
            if snapshot.get("type") == "operation" and snapshot.get("id") == operation_id:
                snapshot["label"] = label
                snapshot["status"] = status
                break

    def set_meta(self, text: str) -> None:
        self.meta_label.setText(text)
        self.meta_label.setVisible(bool(str(text).strip()))

    def add_inline_widget(self, widget: QWidget) -> None:
        self._append_block(widget)

    def restore_lines(self, lines: List[str]) -> None:
        self._lines = list(lines)
        for html_text in self._lines:
            self._append_html_line(html_text)

    def restore_operations(self, operations: List[Dict[str, str]]) -> None:
        for item in operations:
            op_id = str(item.get("id", "") or "")
            label = str(item.get("label", "") or "")
            status = str(item.get("status", "") or "")
            if not op_id or not label:
                continue
            self.upsert_operation(op_id, label, status)

    def operation_snapshots(self) -> List[Dict[str, str]]:
        return [dict(item) for item in self._operation_snapshots]

    def stream_snapshots(self) -> List[Dict[str, str]]:
        return [dict(item) for item in self._stream_snapshots]

    def restore_stream(self, stream: List[Dict[str, str]]) -> None:
        self._stream_snapshots = []
        self._lines = []
        self._operation_snapshots = []
        self._operation_rows = {}
        for item in stream:
            item_type = str(item.get("type", "") or "")
            if item_type == "html":
                html_text = str(item.get("html", "") or "")
                if not html_text:
                    continue
                self._lines.append(html_text)
                self._append_html_line(html_text)
                self._stream_snapshots.append({"type": "html", "html": html_text})
            elif item_type == "operation":
                op_id = str(item.get("id", "") or "")
                label = str(item.get("label", "") or "")
                status = str(item.get("status", "") or "")
                if not op_id or not label:
                    continue
                row = OperationStatusRow(label)
                self._operation_rows[op_id] = row
                self._append_block(row)
                row.set_status(status)
                snapshot = {"id": op_id, "label": label, "status": status}
                self._operation_snapshots.append(snapshot)
                self._stream_snapshots.append({"type": "operation", **snapshot})


class OperationStatusRow(QFrame):
    def __init__(self, label: str) -> None:
        super().__init__()
        self.setObjectName("operationRow")
        self._running_since = 0.0
        self._last_elapsed = 0
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        self.dot = QLabel()
        self.dot.setObjectName("operationDot")
        self.dot.setFixedSize(8, 8)

        self.title = QLabel(label)
        self.title.setObjectName("operationTitle")

        self.status_label = QLabel()
        self.status_label.setObjectName("operationStatus")
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        layout.addWidget(self.dot, 0, Qt.AlignTop)
        layout.addWidget(self.title, 1)
        layout.addWidget(self.status_label, 0, Qt.AlignRight)

    def _tick(self) -> None:
        if self._running_since <= 0:
            return
        self._last_elapsed = max(0, int(time.monotonic() - self._running_since))
        self.status_label.setText("Running {}s".format(self._last_elapsed))

    def set_status(self, status: str) -> None:
        if status == "running":
            if self._running_since <= 0:
                self._running_since = time.monotonic()
                self._last_elapsed = 0
            if not self._timer.isActive():
                self._timer.start()
            self.status_label.setText("Running 0s")
            self.setProperty("state", "running")
        elif status == "completed":
            elapsed = max(0, int(time.monotonic() - self._running_since)) if self._running_since > 0 else self._last_elapsed
            self._last_elapsed = elapsed
            self._timer.stop()
            self._running_since = 0.0
            self.status_label.setText("Finished {}s".format(elapsed))
            self.setProperty("state", "completed")
        else:
            self._timer.stop()
            elapsed = max(0, int(time.monotonic() - self._running_since)) if self._running_since > 0 else self._last_elapsed
            self._last_elapsed = elapsed
            self._running_since = 0.0
            self.status_label.setText("Failed {}s".format(elapsed) if elapsed else "Failed")
            self.setProperty("state", "failed")
        self.style().unpolish(self)
        self.style().polish(self)


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
        self._conversation_buttons: List[QPushButton] = []
        self._active_conversation_button: Optional[QPushButton] = None
        self._conversation_data: Dict[QPushButton, dict] = {}
        self._message_widgets: List[QWidget] = []
        self._awaiting_clarification = False
        self._awaiting_authorization = False
        self._pending_input_mode = ""
        self._active_authorization_card: Optional[AuthorizationCard] = None
        self._pending_authorization: Optional[Dict[str, str]] = None
        self._running = False
        self._warmup_ready = False
        self._clarification_resume_active = False
        self._seen_operation_states: Set[Tuple[str, str]] = set()
        self._plan_cycle = 0
        self._status_base_message = "正在启动并预加载模型..."
        self._status_started_at = 0.0
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(250)
        self._status_timer.timeout.connect(self._refresh_status_bar)
        self._history_store_path = Path("./history/gui_conversations.json")
        self._legacy_history_store_path = Path("./memory/gui_conversations.json")

        self._build_ui()
        self._load_conversation_history()
        QTimer.singleShot(0, self._place_window)
        QTimer.singleShot(0, self._start_warmup)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._running:
            self._worker.cancel_task()
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        self._persist_conversation_history()
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

        current_label = QLabel("当前")
        current_label.setObjectName("sidebarSectionTitle")
        sidebar_layout.addWidget(current_label)

        self.current_conversation_host = QWidget()
        self.current_conversation_host.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.current_conversation_layout = QVBoxLayout(self.current_conversation_host)
        self.current_conversation_layout.setContentsMargins(0, 0, 0, 0)
        self.current_conversation_layout.setSpacing(10)
        sidebar_layout.addWidget(self.current_conversation_host)

        history_label = QLabel("历史")
        history_label.setObjectName("sidebarSectionTitle")
        sidebar_layout.addWidget(history_label)

        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_scroll.setObjectName("historyScroll")
        self.conversation_list = QWidget()
        self.conversation_list_layout = QVBoxLayout(self.conversation_list)
        self.conversation_list_layout.setContentsMargins(0, 0, 0, 0)
        self.conversation_list_layout.setSpacing(10)
        self.conversation_list_layout.setAlignment(Qt.AlignTop)
        self.history_scroll.setWidget(self.conversation_list)
        sidebar_layout.addWidget(self.history_scroll, 1)
        page.addWidget(sidebar)

        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(20, 18, 20, 18)
        main_layout.setSpacing(14)
        page.addWidget(main, 1)

        hero = QLabel("Open Computer Use")
        hero.setObjectName("heroTitle")
        hero.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
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
        self._set_status_message("正在启动并预加载模型...")

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
            #sidebarSectionTitle {
                color: #8a94a6;
                font-size: 12px;
                font-weight: 700;
                padding: 4px 4px 0 4px;
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
                max-height: 38px;
                text-align: left;
                padding: 0 14px;
                border: none;
                border-radius: 10px;
                background: #eef4fb;
                color: #4c596b;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton[conversation="true"][active="true"] {
                background: #d9ebff;
                color: #1f75d8;
            }
            QPushButton[conversation="true"][pinned="true"] {
                border: 1px solid #9fcbff;
            }
            #historyScroll {
                border: none;
                background: transparent;
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
                color: #6f7c8f;
                font-size: 13px;
            }
            #operationRow {
                background: #f7fbff;
                border: 1px solid #dce8f7;
                border-radius: 10px;
            }
            #operationRow[state="running"] {
                border: 1px solid #b9dafc;
                background: #eef7ff;
            }
            #operationRow[state="completed"] {
                border: 1px solid #cfe9da;
                background: #f1fbf5;
            }
            #operationRow[state="failed"] {
                border: 1px solid #f0d4d4;
                background: #fff4f4;
            }
            #operationDot {
                background: #3b82f6;
                border-radius: 4px;
            }
            #operationTitle {
                color: #364152;
                font-size: 13px;
                font-weight: 600;
            }
            #operationStatus {
                color: #2d86eb;
                font-size: 12px;
                font-weight: 600;
                min-width: 96px;
            }
            #operationRow[state="completed"] #operationStatus {
                color: #1f8f5f;
            }
            #operationRow[state="failed"] #operationStatus {
                color: #d04f4f;
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
            self._set_status_message("执行中...", timed=True)
        elif self._warmup_ready:
            self._set_status_message("就绪")
        else:
            self._set_status_message("正在启动并预加载模型...")

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
        self._set_status_message(message if ok else "预加载失败，发送任务时会继续尝试")

    def _cancel_task(self) -> None:
        if not self._running:
            return
        self._set_status_message("正在停止任务...")
        self._worker.cancel_task()

    def _handle_new_chat(self) -> None:
        self._conversation_count += 1
        self._create_conversation(f"新对话 {self._conversation_count}")
        self._reset_chat_content()
        self._persist_conversation_history()

    def _create_conversation(
        self,
        title: str,
        *,
        conversation_id: str = "",
        pinned: bool = False,
        state: Optional[dict] = None,
        activate: bool = True,
    ) -> QPushButton:
        button = QPushButton(title)
        button.setProperty("conversation", True)
        button.setProperty("active", False)
        button.setProperty("pinned", bool(pinned))
        button.setProperty("is_history", conversation_id.startswith("session::"))
        if not conversation_id:
            conversation_id = "conv_{}".format(int(time.time() * 1000))
        button.setProperty("conversation_id", conversation_id)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(title)
        button.setContextMenuPolicy(Qt.CustomContextMenu)
        button.clicked.connect(lambda: self._activate_conversation(button))
        button.customContextMenuRequested.connect(
            lambda pos, target=button: self._show_conversation_menu(target, pos)
        )
        self._conversation_buttons.append(button)
        self._conversation_data[button] = state or {
            "messages": [],
            "awaiting_clarification": False,
            "awaiting_authorization": False,
            "pending_input_mode": "",
            "pending_authorization": None,
            "session_memory_dir": "",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._sync_conversation_button_text(button, title)
        self._attach_conversation_button(button)
        if activate:
            self._activate_conversation(button)
        return button

    def _activate_conversation(self, button: QPushButton) -> None:
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        for item in self._conversation_buttons:
            item.setProperty("active", item is button)
            item.style().unpolish(item)
            item.style().polish(item)
        self._active_conversation_button = button
        self._load_conversation_state(button)
        self._persist_conversation_history()

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
            self._sync_conversation_button_text(button, text.strip())
            self._persist_conversation_history()

    def _toggle_pin_conversation(self, button: QPushButton) -> None:
        button.setProperty("pinned", not bool(button.property("pinned")))
        button.style().unpolish(button)
        button.style().polish(button)
        self._reorder_conversations()
        self._persist_conversation_history()

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
        self._persist_conversation_history()

    def _reorder_conversations(self) -> None:
        while self.current_conversation_layout.count():
            self.current_conversation_layout.takeAt(0)
        while self.conversation_list_layout.count():
            self.conversation_list_layout.takeAt(0)
        current_buttons = [b for b in self._conversation_buttons if not bool(b.property("is_history"))]
        history_buttons = [b for b in self._conversation_buttons if bool(b.property("is_history"))]
        pinned = [b for b in history_buttons if bool(b.property("pinned"))]
        normal = [b for b in history_buttons if not bool(b.property("pinned"))]
        history_buttons = pinned + normal
        self._conversation_buttons = current_buttons + history_buttons
        for button in current_buttons:
            self.current_conversation_layout.addWidget(button)
        for button in history_buttons:
            self.conversation_list_layout.addWidget(button)
        self.conversation_list_layout.addStretch(1)
        self._persist_conversation_history()

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
            self._sync_conversation_button_text(self._active_conversation_button, text)
            self._save_current_conversation_state(self._active_conversation_button)
            self._persist_conversation_history()

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
            card.append_text("\u6211\u6b63\u5728\u5224\u65ad\u5f53\u524d\u6700\u5408\u9002\u7684\u4e0b\u4e00\u6b65\u3002", "cyan")
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
        self._plan_cycle += 1
        self._seen_operation_states.clear()
        card = self._ensure_assistant_card()
        card.set_meta("")
        card.append_text("\u6211\u5df2\u7ecf\u7406\u89e3\u4efb\u52a1\u3002", "blue")
        if operations:
            card.append_text(
                "\u63a5\u4e0b\u6765\u5148\u505a\u8fd9\u4e00\u6b65\uff1a{}".format(self._describe_operation(operations[0])),
                "muted",
            )
        elif summary:
            card.append_text(summary, "green")
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        self._scroll_bottom()

    def _update_operation(self, operation_id: str, kind: str, status: str, error: str) -> None:
        if self._assistant_card is None:
            return
        display_operation_id = "{}:{}".format(self._plan_cycle, operation_id)
        state_key = (display_operation_id, status)
        if state_key in self._seen_operation_states:
            return
        self._seen_operation_states.add(state_key)
        label = self._operation_label(kind or operation_id)
        if status == "running":
            self._assistant_card.upsert_operation(display_operation_id, label, "running")
        elif status == "completed":
            self._assistant_card.upsert_operation(display_operation_id, label, "completed")
        elif error:
            error = self._localize_runtime_text(error)
            self._assistant_card.append_text(f"\u6b65\u9aa4 {operation_id} \u5931\u8d25\uff1a{error}", "red")
            self._assistant_card.upsert_operation(display_operation_id, label, "failed")
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
        localized = self._localize_runtime_text(message)
        verification_markers = [
            "检测到搜索验证",
            "请在浏览器中手动完成验证",
            "验证已通过，正在继续执行搜索任务",
        ]
        if any(marker in localized for marker in verification_markers):
            card = self._ensure_assistant_card()
            card.append_text(localized, "yellow")
            if self._active_conversation_button is not None:
                self._save_current_conversation_state(self._active_conversation_button)
            self._scroll_bottom()
        self._set_status_message(message, timed=self._running)

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
        self._set_status_message("\u8bf7\u5728\u4e0b\u65b9\u8f93\u5165\u8865\u5145\u4fe1\u606f\u540e\u53d1\u9001")

    def _clarification_consumed(self) -> None:
        self._assistant_card = AssistantCard()
        self._assistant_card.set_meta("")
        self._seen_operation_states.clear()
        self._insert_message(self._assistant_card, False)
        self._assistant_card.append_text("\u5df2\u6536\u5230\u4f60\u7684\u8865\u5145\u4fe1\u606f\uff0c\u6b63\u5728\u7ee7\u7eed\u89c4\u5212\u3002", "cyan")
        self._set_status_message("\u5df2\u6536\u5230\u4f60\u7684\u8865\u5145\u4fe1\u606f\uff0c\u6b63\u5728\u7ee7\u7eed\u89c4\u5212")
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
        self._set_status_message("\u8bf7\u786e\u8ba4\u662f\u5426\u5141\u8bb8\u6267\u884c\u8be5\u6b65\u9aa4")

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
        if self._active_conversation_button is not None:
            conversation_state = dict(self._conversation_data.get(self._active_conversation_button, {}) or {})
            conversation_state["session_memory_dir"] = str(payload.get("session_memory_dir", "") or "")
            conversation_state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._conversation_data[self._active_conversation_button] = conversation_state
        evaluation = payload.get("evaluation", {})
        if self._assistant_card is not None:
            satisfied = bool(evaluation.get("satisfied", True))
            reason = self._localize_runtime_text(str(evaluation.get("reason", "") or ""))
            if satisfied:
                self._assistant_card.append_text("\u6574\u4e2a\u4efb\u52a1\u5df2\u7ecf\u5b8c\u6210\u3002", "green")
            else:
                self._assistant_card.append_text("\u6267\u884c\u5df2\u7ed3\u675f\uff0c\u4f46\u7ed3\u679c\u6821\u9a8c\u672a\u901a\u8fc7\u3002", "yellow")
                if reason and reason not in {"not executed", ""}:
                    self._assistant_card.append_text(f"\u672a\u901a\u8fc7\u539f\u56e0\uff1a{reason}", "yellow")
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
            self._persist_conversation_history()

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
            self._persist_conversation_history()
        self._set_status_message("\u4efb\u52a1\u5df2\u53d6\u6d88" if "\u53d6\u6d88" in str(error) else "\u4efb\u52a1\u5931\u8d25")

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
                            "stream": widget.stream_snapshots(),
                            "lines": list(widget._lines),
                            "meta": widget.meta_label.text(),
                            "operations": widget.operation_snapshots(),
                        }
                    )
        self._conversation_data[button] = {
            "messages": messages,
            "awaiting_clarification": self._awaiting_clarification,
            "awaiting_authorization": self._awaiting_authorization,
            "pending_input_mode": self._pending_input_mode,
            "pending_authorization": dict(self._pending_authorization or {}),
            "session_memory_dir": str(
                self._conversation_data.get(button, {}).get("session_memory_dir", "") or ""
            ),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
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
                stream = list(message.get("stream", []) or [])
                if stream:
                    card.restore_stream(stream)
                else:
                    card.restore_lines(list(message.get("lines", [])))
                    card.restore_operations(list(message.get("operations", [])))
                card.set_meta("")
                self._assistant_card = card
                self._insert_message(card, False)
        if self._awaiting_authorization and self._pending_authorization and self._assistant_card is not None:
            self._restore_authorization_card(
                self._pending_authorization.get("action_type", ""),
                self._pending_authorization.get("details", ""),
            )
        self.prompt_edit.setFocus()

    def _load_conversation_history(self) -> None:
        archive = self._read_conversation_archive()
        conversations = archive.get("conversations", []) or []
        self._conversation_count += 1
        target_button = self._create_conversation("新对话 {}".format(self._conversation_count), activate=False)
        if not conversations:
            self._activate_conversation(target_button)
            self._persist_conversation_history()
            return

        conversations.sort(key=lambda item: str(item.get("updated_at", "") or ""), reverse=True)
        for entry in conversations:
            state = dict(entry.get("state", {}) or {})
            self._create_conversation(
                str(entry.get("title", "历史会话") or "历史会话"),
                conversation_id=str(entry.get("id", "") or ""),
                pinned=bool(entry.get("pinned", False)),
                state=state,
                activate=False,
            )

        self._reorder_conversations()
        self._activate_conversation(target_button)

    def _read_conversation_archive(self) -> dict:
        source_path = self._history_store_path
        if not source_path.exists() and self._legacy_history_store_path.exists():
            source_path = self._legacy_history_store_path
        if not source_path.exists():
            return {"conversations": [], "last_active_id": "", "version": 1}
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"conversations": [], "last_active_id": "", "version": 1}
        if not isinstance(payload, dict):
            return {"conversations": [], "last_active_id": "", "version": 1}
        payload.setdefault("conversations", [])
        payload.setdefault("last_active_id", "")
        payload["version"] = 1
        payload["conversations"] = [
            item
            for item in payload.get("conversations", [])
            if isinstance(item, dict) and not str(item.get("id", "") or "").startswith("session::")
        ]
        return payload

    def _persist_conversation_history(self) -> None:
        try:
            self._history_store_path.parent.mkdir(parents=True, exist_ok=True)
            conversations = []
            for button in self._conversation_buttons:
                if not bool(button.property("is_history")) and not self._conversation_data.get(button, {}).get("messages"):
                    continue
                state = dict(self._conversation_data.get(button, {}) or {})
                conversations.append(
                    {
                        "id": str(button.property("conversation_id") or ""),
                        "title": button.text(),
                        "pinned": bool(button.property("pinned")),
                        "updated_at": str(state.get("updated_at", "") or ""),
                        "state": state,
                    }
                )
            payload = {
                "version": 1,
                "last_active_id": str(
                    self._active_conversation_button.property("conversation_id")
                    if self._active_conversation_button is not None
                    else ""
                ),
                "conversations": conversations,
            }
            self._history_store_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    @staticmethod
    def _build_history_line_html(text: str, color: str = "default") -> str:
        palette = {
            "default": "#364152",
            "muted": "#5d6b7c",
            "cyan": "#2d86eb",
            "green": "#1f8f5f",
            "yellow": "#a36a00",
            "red": "#d04f4f",
            "gray": "#7b8596",
            "blue": "#3f7cff",
            "magenta": "#7b58d0",
        }
        safe = html.escape(str(text or "").strip())
        return '<div style="color:{}; margin-bottom:6px; background:transparent;">{}</div>'.format(
            palette.get(color, "#364152"),
            safe,
        )

    def _sync_conversation_button_text(self, button: QPushButton, title: str) -> None:
        button.setToolTip(title)
        metrics = QFontMetrics(button.font())
        available_width = max(72, button.width() - 28) if button.width() > 0 else 100
        button.setText(metrics.elidedText(title, Qt.ElideRight, available_width))

    def _attach_conversation_button(self, button: QPushButton) -> None:
        if bool(button.property("is_history")):
            self.conversation_list_layout.addWidget(button)
        else:
            self.current_conversation_layout.addWidget(button)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        for button in self._conversation_buttons:
            self._sync_conversation_button_text(button, button.toolTip() or button.text())
        self._update_message_widths()

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

    def _set_status_message(self, message: str, timed: bool = False) -> None:
        self._status_base_message = str(message or "")
        if timed:
            if self._status_started_at <= 0:
                self._status_started_at = time.monotonic()
            if not self._status_timer.isActive():
                self._status_timer.start()
            self._refresh_status_bar()
            return
        self._status_started_at = 0.0
        self._status_timer.stop()
        self.status_bar.showMessage(self._status_base_message)

    def _refresh_status_bar(self) -> None:
        if self._status_started_at <= 0:
            self.status_bar.showMessage(self._status_base_message)
            return
        elapsed = max(0, int(time.monotonic() - self._status_started_at))
        self.status_bar.showMessage("{} {}s".format(self._status_base_message, elapsed))


def launch() -> int:
    configure_linux_input_method()
    ensure_supported_python()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return int(app.exec_())
