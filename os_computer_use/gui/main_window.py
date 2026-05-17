from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

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

# 内联 HTML 片段标记：与连续正文合并时区分命令条（Qt RichText 不显示该注释）。
STREAM_CMD_HTML_MARK = "<!--ocu-cmd-->"


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


def _authorization_should_collapse_details(action_type: str, details: str) -> bool:
    raw = str(details or "").strip()
    if not raw:
        return False
    if str(action_type or "").strip() == "browser.send" and "{" in raw:
        return len(raw) > 80
    if "\n" in raw or "\r" in raw:
        return len(raw) > 60
    return len(raw) > 200


def _authorization_summary_text(action_type: str, details: str) -> str:
    """授权卡片折叠态：突出收件人、主题等，便于录屏与扫读。"""
    raw = str(details or "").strip()
    if not raw:
        return "\uff08\u65e0\u8be6\u60c5\uff09"
    if str(action_type or "").strip() == "browser.send":
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                lines = []
                to = str(payload.get("to", "") or "").strip()
                subj = str(payload.get("subject", "") or "").strip()
                if to:
                    lines.append("\u6536\u4ef6\u4eba\uff1a{}".format(to))
                if subj:
                    if len(subj) > 140:
                        subj = subj[:140] + "\u2026"
                    lines.append("\u4e3b\u9898\uff1a{}".format(subj))
                att = payload.get("attachment_count")
                if att is not None:
                    lines.append("\u9644\u4ef6\u6570\uff1a{}".format(att))
                if lines:
                    return "\n".join(lines)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    first = raw.replace("\r\n", "\n").split("\n", 1)[0].strip()
    if len(first) > 160:
        first = first[:160] + "\u2026"
    return first


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
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(4)

        title = QLabel("\u7528\u6237")
        title.setObjectName("userTitle")
        title.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        body = QLabel(text)
        body.setObjectName("userBody")
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(title)
        layout.addWidget(body)


def _stream_resolution_after_authorization(stream: List[Dict[str, Any]], auth_index: int) -> Optional[bool]:
    """在流式块序列中，authorization 块之后若出现「已允许/已拒绝」的 html，则返回 True/False；否则 None（仍待处理）。"""
    allowed_marker = "\u5df2\u5141\u8bb8\u7ee7\u7eed\u3002"
    rejected_marker = "\u5df2\u62d2\u7edd\u8fd9\u4e00\u6b65\u3002"
    for j in range(auth_index + 1, len(stream)):
        typ = str(stream[j].get("type", "") or "")
        if typ == "authorization":
            return None
        if typ == "html":
            blob = str(stream[j].get("html", "") or "")
            if allowed_marker in blob:
                return True
            if rejected_marker in blob:
                return False
    return None


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
        self._stream_merge_label: Optional[QLabel] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(4)

        self.name_label = QLabel("\u684c\u9762\u667a\u80fd\u4f53")
        self.name_label.setObjectName("assistantName")
        self.stream_wrap = QWidget()
        self.stream_wrap.setObjectName("assistantStreamHost")
        self.stream_layout = QVBoxLayout(self.stream_wrap)
        self.stream_layout.setContentsMargins(0, 0, 0, 0)
        self.stream_layout.setSpacing(0)
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

    def _break_stream_body_merge(self) -> None:
        """下一块非连续正文（命令条、操作行、授权卡等）前结束合并。"""
        self._stream_merge_label = None

    @staticmethod
    def _is_command_html_fragment(html_fragment: str) -> bool:
        if STREAM_CMD_HTML_MARK in html_fragment:
            return True
        lf = html_fragment.lower()
        return "display:inline-block" in lf and "border-radius:999px" in lf

    @staticmethod
    def _body_fragment_to_flow_span(html_fragment: str) -> str:
        """把正文块改为行内 span，避免多个块级 div 在 Qt RichText 里产生额外段距。"""
        raw = str(html_fragment or "").strip()
        if not raw:
            return raw
        if raw.startswith("<span") and raw.endswith("</span>"):
            return raw
        m = re.match(r'^<div\s+style="([^"]*)"\s*>(.*)</div>\s*$', raw, re.DOTALL)
        if not m:
            return raw
        style, inner = m.group(1), m.group(2)
        kept: List[str] = []
        for p in (x.strip() for x in style.split(";") if x.strip()):
            pl = p.lower()
            if pl.startswith("color:") or pl.startswith("font-weight:"):
                kept.append(p)
        if not any(x.lower().startswith("color:") for x in kept):
            kept.insert(0, "color:#26251e")
        return '<span style="{}">{}</span>'.format(";".join(kept), inner)

    def _append_body_html_fragment(self, html_fragment: str) -> None:
        """连续 append_text 合并到同一 QLabel；正文用 span + <br/>，行距与用户单段一致。"""
        display_html = (
            html_fragment.replace(STREAM_CMD_HTML_MARK, "", 1)
            if STREAM_CMD_HTML_MARK in html_fragment
            else html_fragment
        )
        if self._is_command_html_fragment(html_fragment):
            self._break_stream_body_merge()
            label = QLabel()
            label.setObjectName("assistantBody")
            label.setWordWrap(True)
            label.setTextFormat(Qt.RichText)
            label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setContentsMargins(0, 0, 0, 0)
            label.setText(display_html)
            self._append_block(label)
            return
        piece = self._body_fragment_to_flow_span(html_fragment)
        if (
            self._stream_merge_label is not None
            and self._stream_blocks
            and self._stream_blocks[-1] is self._stream_merge_label
        ):
            cur = self._stream_merge_label.text()
            self._stream_merge_label.setText(
                (cur + "<br/>" + piece) if cur.strip() else piece
            )
            return
        label = QLabel()
        label.setObjectName("assistantBody")
        label.setWordWrap(True)
        label.setTextFormat(Qt.RichText)
        label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setContentsMargins(0, 0, 0, 0)
        label.setText(piece)
        self._stream_merge_label = label
        self._append_block(label)

    def _append_block(self, widget: QWidget) -> None:
        widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._stream_blocks.append(widget)
        self.stream_layout.addWidget(widget)

    def append_text(self, text: str, color: str = "default") -> None:
        text = text.strip()
        if not text:
            return
        palette = {
            "default": "#26251e",
            "muted": "#807d72",
            "cyan": "#3d5a80",
            "green": "#1f8a65",
            "yellow": "#8a6328",
            "red": "#cf2d56",
            "gray": "#a09c92",
            "blue": "#3d5a80",
            "magenta": "#6b5089",
        }
        safe = html.escape(text)
        html_text = (
            f'<span style="color:{palette.get(color, "#26251e")}; font-weight:400;">{safe}</span>'
        )
        self._lines.append(html_text)
        self._lines = self._lines[-40:]
        self._append_body_html_fragment(html_text)
        self._stream_snapshots.append({"type": "html", "html": html_text})

    def add_command(self, text: str, tone: str = "run") -> None:
        key = f"{tone}:{text}"
        if key in self._command_keys:
            return
        self._command_keys.add(key)
        self._commands.append((text, tone))
        palette = {
            "run": ("#425466", "#f5f7fb", "#dbe3ef"),
            "done": ("#0f6b4b", "#eef8f3", "#b9e1cd"),
            "warn": ("#b42318", "#fff5f4", "#f3c4bf"),
        }
        fg, bg, bd = palette.get(tone, palette["run"])
        safe = html.escape(text.strip())
        html_text = (
            STREAM_CMD_HTML_MARK
            + '<div style="margin:6px 0 4px 0;">'
            f'<span style="display:inline-block; color:{fg}; background:{bg}; '
            f'border:1px solid {bd}; border-radius:10px; padding:8px 12px; font-weight:500; font-size:15px; line-height:1.45;">{safe}</span>'
            "</div>"
        )
        self._lines.append(html_text)
        self._lines = self._lines[-60:]
        self._append_body_html_fragment(html_text)
        self._stream_snapshots.append({"type": "html", "html": html_text})

    def upsert_operation(self, operation_id: str, label: str, status: str) -> None:
        row = self._operation_rows.get(operation_id)
        if row is None:
            self._break_stream_body_merge()
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
        self._break_stream_body_merge()
        self._append_block(widget)

    def record_authorization_pending(self, action_type: str, details: str) -> None:
        """与内联 AuthorizationCard 同步写入流快照，避免切换会话/重绘时仅存文本而丢失授权块。"""
        self._stream_snapshots.append(
            {
                "type": "authorization",
                "action_type": str(action_type or ""),
                "details": str(details or ""),
            }
        )

    def pending_authorization_from_stream(self) -> Optional[Dict[str, str]]:
        """从流快照推断尚未在流中出现「已允许/已拒绝」跟进的最后一次授权请求。"""
        auth_idx: Optional[int] = None
        action_type = ""
        detail_text = ""
        for i, item in enumerate(self._stream_snapshots):
            if str(item.get("type", "") or "") == "authorization":
                auth_idx = i
                action_type = str(item.get("action_type", "") or "")
                detail_text = str(item.get("details", "") or "")
        if auth_idx is None:
            return None
        if _stream_resolution_after_authorization(self._stream_snapshots, auth_idx) is not None:
            return None
        return {"action_type": action_type, "details": detail_text}

    def restore_lines(self, lines: List[str]) -> None:
        self._lines = list(lines)
        self._break_stream_body_merge()
        for html_text in self._lines:
            self._append_body_html_fragment(html_text)

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
        self._break_stream_body_merge()
        for i, item in enumerate(stream):
            item_type = str(item.get("type", "") or "")
            if item_type == "html":
                html_text = str(item.get("html", "") or "")
                if not html_text:
                    continue
                self._lines.append(html_text)
                self._append_body_html_fragment(html_text)
                self._stream_snapshots.append({"type": "html", "html": html_text})
            elif item_type == "operation":
                self._break_stream_body_merge()
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
            elif item_type == "authorization":
                action_type = str(item.get("action_type", "") or "")
                detail_text = str(item.get("details", "") or "")
                self._stream_snapshots.append(
                    {"type": "authorization", "action_type": action_type, "details": detail_text}
                )
                resolved = _stream_resolution_after_authorization(stream, i)
                if resolved is True or resolved is False:
                    self._break_stream_body_merge()
                    self._append_block(AuthorizationCard(action_type, detail_text, None, resolved=resolved))


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
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(12)

        self.rail = QFrame()
        self.rail.setObjectName("operationRail")
        self.rail.setFixedWidth(3)

        self.dot = QLabel()
        self.dot.setObjectName("operationDot")
        self.dot.setFixedSize(10, 10)

        self.title = QLabel(label)
        self.title.setObjectName("operationTitle")
        self.title.setWordWrap(True)

        self.status_label = QLabel()
        self.status_label.setObjectName("operationStatus")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setMinimumWidth(112)
        self.status_label.setMinimumHeight(38)

        leading_wrap = QHBoxLayout()
        leading_wrap.setContentsMargins(0, 0, 0, 0)
        leading_wrap.setSpacing(10)
        leading_wrap.addWidget(self.dot, 0, Qt.AlignVCenter)
        leading_wrap.addWidget(self.title, 1, Qt.AlignVCenter)

        layout.addWidget(self.rail, 0)
        layout.addLayout(leading_wrap, 1)
        layout.addWidget(self.status_label, 0, Qt.AlignVCenter)

    def _tick(self) -> None:
        if self._running_since <= 0:
            return
        self._last_elapsed = max(0, int(time.monotonic() - self._running_since))
        self.status_label.setText("\u6267\u884c\u4e2d {}s".format(self._last_elapsed))

    def set_status(self, status: str) -> None:
        if status == "running":
            if self._running_since <= 0:
                self._running_since = time.monotonic()
                self._last_elapsed = 0
            if not self._timer.isActive():
                self._timer.start()
            self.status_label.setText("\u6267\u884c\u4e2d 0s")
            self.setProperty("state", "running")
        elif status == "completed":
            elapsed = max(0, int(time.monotonic() - self._running_since)) if self._running_since > 0 else self._last_elapsed
            self._last_elapsed = elapsed
            self._timer.stop()
            self._running_since = 0.0
            self.status_label.setText("\u5df2\u5b8c\u6210 {}s".format(elapsed))
            self.setProperty("state", "completed")
        else:
            self._timer.stop()
            elapsed = max(0, int(time.monotonic() - self._running_since)) if self._running_since > 0 else self._last_elapsed
            self._last_elapsed = elapsed
            self._running_since = 0.0
            self.status_label.setText("\u5931\u8d25 {}s".format(elapsed) if elapsed else "\u5931\u8d25")
            self.setProperty("state", "failed")
        self.style().unpolish(self)
        self.style().polish(self)


class AuthorizationCard(QFrame):
    """高风险操作确认卡片；支持交互确认与历史只读展示（恢复会话时保留）。"""

    def __init__(
        self,
        action_type: str,
        details: str,
        on_decide: Optional[Callable[[bool], None]],
        *,
        resolved: Optional[bool] = None,
    ) -> None:
        super().__init__()
        self.setObjectName("authorizationCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(8)

        self._title_label = QLabel()
        self._title_label.setObjectName("assistantName")

        body = QLabel("\u68c0\u6d4b\u5230\u9ad8\u98ce\u9669\u64cd\u4f5c\uff0c\u8bf7\u786e\u8ba4\u662f\u5426\u7ee7\u7eed\u3002")
        if resolved is not None:
            body.setText("\u5df2\u8bb0\u5f55\u7684\u9ad8\u98ce\u9669\u64cd\u4f5c\uff08\u5386\u53f2\u786e\u8ba4\u72b6\u6001\uff09\u3002")
        body.setObjectName("assistantBody")
        body.setWordWrap(True)

        type_heading = QLabel("\u64cd\u4f5c\u7c7b\u578b\uff1a{}".format(action_type or ""))
        type_heading.setObjectName("monoDetail")

        self._toggle_btn: Optional[QPushButton] = None
        self._full_detail_label: Optional[QLabel] = None
        self._simple_detail_label: Optional[QLabel] = None
        self._expanded = False

        raw_details = str(details or "").strip()
        if _authorization_should_collapse_details(action_type, details):
            summary = QLabel(_authorization_summary_text(action_type, details))
            summary.setObjectName("assistantBody")
            summary.setWordWrap(True)
            self._full_detail_label = QLabel(
                "\u8be6\u60c5\uff08\u5168\u6587\uff09\uff1a\n{}".format(details if raw_details else "\uff08\u65e0\uff09")
            )
            self._full_detail_label.setObjectName("monoDetail")
            self._full_detail_label.setWordWrap(True)
            self._full_detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._full_detail_label.hide()
            self._toggle_btn = QPushButton("\u5c55\u5f00\u8be6\u60c5")
            self._toggle_btn.setObjectName("expandDetailButton")
            self._toggle_btn.setFlat(True)
            self._toggle_btn.setCursor(Qt.PointingHandCursor)
            self._toggle_btn.clicked.connect(self._toggle_detail_expanded)
        else:
            single = QLabel(
                "\u8be6\u60c5\uff1a{}".format(details if raw_details else "\uff08\u65e0\uff09")
            )
            single.setObjectName("monoDetail")
            single.setWordWrap(True)
            single.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._simple_detail_label = single

        self._button_bar = QWidget()
        bl = QHBoxLayout(self._button_bar)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        reject = QPushButton("\u62d2\u7edd")
        reject.setObjectName("minorButton")
        reject.setMinimumHeight(34)
        approve = QPushButton("\u5141\u8bb8")
        approve.setObjectName("sendButton")
        approve.setMinimumHeight(34)
        if resolved is None and on_decide is not None:
            reject.clicked.connect(lambda: on_decide(False))
            approve.clicked.connect(lambda: on_decide(True))
        bl.addWidget(reject)
        bl.addWidget(approve)
        bl.addStretch(1)

        self._result_label = QLabel()
        self._result_label.setObjectName("assistantBody")
        self._result_label.setWordWrap(True)
        self._result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._result_label.hide()

        layout.addWidget(self._title_label)
        layout.addWidget(body)
        layout.addWidget(type_heading)
        if self._toggle_btn is not None:
            layout.addWidget(summary)
            layout.addWidget(self._toggle_btn)
            layout.addWidget(self._full_detail_label)
        else:
            layout.addWidget(self._simple_detail_label)
        layout.addWidget(self._button_bar)
        layout.addWidget(self._result_label)

        if resolved is True:
            self._title_label.setText("\u5df2\u5141\u8bb8")
            self._apply_resolved_state(True, update_title=False)
        elif resolved is False:
            self._title_label.setText("\u5df2\u62d2\u7edd")
            self._apply_resolved_state(False, update_title=False)
        else:
            self._title_label.setText("\u9700\u8981\u786e\u8ba4")

    def apply_resolved(self, allowed: bool) -> None:
        """用户点击允许/拒绝后的即时视觉反馈。"""
        self._apply_resolved_state(allowed, update_title=True)

    def _apply_resolved_state(self, allowed: bool, *, update_title: bool) -> None:
        self._button_bar.hide()
        text = "\u5df2\u5141\u8bb8\u7ee7\u7eed\u3002" if allowed else "\u5df2\u62d2\u7edd\u8fd9\u4e00\u6b65\u3002"
        self._result_label.setText(text)
        self._result_label.show()
        self.setProperty("state", "resolved_allow" if allowed else "resolved_deny")
        if update_title:
            self._title_label.setText("\u5df2\u5141\u8bb8" if allowed else "\u5df2\u62d2\u7edd")
        self.style().unpolish(self)
        self.style().polish(self)

    def _toggle_detail_expanded(self) -> None:
        if self._full_detail_label is None or self._toggle_btn is None:
            return
        self._expanded = not self._expanded
        self._full_detail_label.setVisible(self._expanded)
        self._toggle_btn.setText(
            "\u6536\u8d77\u8be6\u60c5" if self._expanded else "\u5c55\u5f00\u8be6\u60c5"
        )


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
        self._worker_conversation_button: Optional[QPushButton] = None
        self._running = False
        self._warmup_ready = False
        self._clarification_resume_active = False
        self._seen_operation_states: Set[Tuple[str, str]] = set()
        self._plan_cycle = 0
        # 主聊天区：用户离开底部则暂停自动滚到底，回到底部附近恢复（类似 Claude 网页）。
        self._chat_follow_tail = True
        self._chat_scroll_programmatic = False
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
        root.setObjectName("appRoot")
        self.setCentralWidget(root)

        page = QHBoxLayout(root)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(176)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(14, 20, 14, 18)
        sidebar_layout.setSpacing(12)

        self.new_chat_button = QPushButton("新建对话")
        self.new_chat_button.setObjectName("newChatButton")
        sidebar_layout.addWidget(self.new_chat_button)

        current_label = QLabel("当前")
        current_label.setObjectName("sidebarSectionTitle")
        sidebar_layout.addWidget(current_label)

        self.current_conversation_host = QWidget()
        self.current_conversation_host.setObjectName("sidebarStackHost")
        self.current_conversation_host.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.current_conversation_layout = QVBoxLayout(self.current_conversation_host)
        self.current_conversation_layout.setContentsMargins(0, 0, 0, 0)
        self.current_conversation_layout.setSpacing(10)
        sidebar_layout.addWidget(self.current_conversation_host)

        history_label = QLabel("历史")
        history_label.setObjectName("sidebarSectionTitle")
        sidebar_layout.addWidget(history_label)

        self.history_scroll = QScrollArea()
        self.history_scroll.setFrameShape(QFrame.NoFrame)
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_scroll.setObjectName("historyScroll")
        self.conversation_list = QWidget()
        self.conversation_list.setObjectName("sidebarStackHost")
        self.conversation_list_layout = QVBoxLayout(self.conversation_list)
        self.conversation_list_layout.setContentsMargins(0, 0, 0, 0)
        self.conversation_list_layout.setSpacing(10)
        self.conversation_list_layout.setAlignment(Qt.AlignTop)
        self.history_scroll.setWidget(self.conversation_list)
        sidebar_layout.addWidget(self.history_scroll, 1)
        page.addWidget(sidebar)

        main = QWidget()
        main.setObjectName("mainPanel")
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(24, 20, 24, 20)
        main_layout.setSpacing(16)
        page.addWidget(main, 1)

        hero = QLabel("Open Computer Use")
        hero.setObjectName("heroTitle")
        hero.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hero_sub = QLabel("\u672c\u5730\u684c\u9762\u81ea\u52a8\u5316 \u00b7 \u81ea\u7136\u8bed\u8a00\u4ea4\u4e92")
        hero_sub.setObjectName("heroSubtitle")
        hero_sub.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        main_layout.addWidget(hero)
        main_layout.addWidget(hero_sub)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setFrameShape(QFrame.NoFrame)
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setObjectName("chatScroll")
        self.chat_host = QWidget()
        self.chat_host.setObjectName("chatHost")
        self.chat_layout = QVBoxLayout(self.chat_host)
        self.chat_layout.setContentsMargins(0, 4, 0, 8)
        self.chat_layout.setSpacing(20)
        self.chat_layout.setAlignment(Qt.AlignTop)
        self.chat_scroll.setWidget(self.chat_host)
        chat_bar = self.chat_scroll.verticalScrollBar()
        chat_bar.valueChanged.connect(self._on_chat_scroll_value_changed)
        chat_bar.rangeChanged.connect(self._on_chat_scroll_range_changed)
        main_layout.addWidget(self.chat_scroll, 1)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        self.prompt_edit = QLineEdit()
        self.prompt_edit.setObjectName("promptEdit")
        self.prompt_edit.setPlaceholderText("请输入您的问题或任务...")
        self.prompt_edit.setMinimumHeight(44)
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

        font = QFont()
        font.setStyleStrategy(QFont.PreferAntialias)
        self.setFont(font)

        self.setStyleSheet(
            """
            QMainWindow {
                background: #f7f7f4;
            }
            QWidget {
                color: #26251e;
                font-family: "Inter", "IBM Plex Sans", system-ui, "Segoe UI",
                             "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                             "Noto Sans CJK SC", sans-serif;
                font-size: 16px;
            }
            #appRoot, #mainPanel, #sidebar, #chatHost {
                background: #f7f7f4;
            }
            QLabel {
                background: transparent;
            }
            #assistantStreamHost {
                background: transparent;
            }
            #monoDetail {
                font-family: "JetBrains Mono", "Cascadia Code", "Consolas", monospace;
                font-size: 13px;
                font-weight: 400;
                color: #5a5852;
                line-height: 1.5;
            }
            #sidebar {
                background: #f7f7f4;
                border-right: 1px solid #e6e5e0;
            }
            #sidebarStackHost {
                background: transparent;
            }
            #sidebarSectionTitle {
                color: #807d72;
                font-size: 13px;
                font-weight: 600;
                padding: 8px 6px 2px 6px;
            }
            #newChatButton {
                min-height: 40px;
                border: 1px solid #cfcdc4;
                border-radius: 8px;
                background: #ffffff;
                color: #26251e;
                font-size: 14px;
                font-weight: 500;
            }
            #newChatButton:hover {
                background: #fafaf7;
                border-color: #807d72;
            }
            #newChatButton:pressed {
                background: #efeee8;
            }
            QPushButton[conversation="true"] {
                min-height: 40px;
                max-height: 40px;
                text-align: left;
                padding: 0 12px;
                border: 1px solid #e6e5e0;
                border-radius: 8px;
                background: #fafaf7;
                color: #5a5852;
                font-size: 14px;
                font-weight: 500;
            }
            QPushButton[conversation="true"]:hover {
                background: #efeee8;
                color: #26251e;
            }
            QPushButton[conversation="true"][active="true"] {
                background: #26251e;
                color: #f7f7f4;
                border: 1px solid #26251e;
            }
            QPushButton[conversation="true"][pinned="true"] {
                border: 1px solid #cfcdc4;
            }
            #historyScroll {
                border: none;
                background: transparent;
            }
            #heroTitle {
                margin-top: 4px;
                font-size: 22px;
                font-weight: 400;
                color: #26251e;
            }
            #heroSubtitle {
                margin-top: -2px;
                margin-bottom: 8px;
                font-size: 16px;
                font-weight: 400;
                color: #5a5852;
            }
            #chatScroll {
                border: none;
                background: #f7f7f4;
            }
            #chatScroll > QWidget > QWidget {
                background: #f7f7f4;
            }
            #chatHost {
                background: #f7f7f4;
            }
            #userBubble {
                background: #ffffff;
                border-radius: 12px;
                border: 1px solid #e6e5e0;
            }
            #userTitle {
                color: #807d72;
                font-size: 16px;
                font-weight: 600;
            }
            #userBody {
                color: #5a5852;
                font-size: 16px;
                font-weight: 400;
                line-height: 1.45;
                margin: 0px;
                padding: 0px;
            }
            #assistantCard {
                background: #ffffff;
                border-radius: 12px;
                border: 1px solid #e6e5e0;
            }
            #authorizationCard {
                background: #fafaf7;
                border-radius: 12px;
                border: 1px solid #e6e5e0;
            }
            #authorizationCard[state="resolved_allow"] {
                background: #fafaf7;
                border: 1px solid #1f8a65;
            }
            #authorizationCard[state="resolved_deny"] {
                background: #fafaf7;
                border: 1px solid #cf2d56;
            }
            #assistantName {
                color: #26251e;
                font-size: 16px;
                font-weight: 600;
            }
            #assistantBody {
                color: #5a5852;
                font-size: 16px;
                font-weight: 400;
                line-height: 1.45;
                margin: 0px;
                padding: 0px;
            }
            #assistantMeta {
                color: #807d72;
                font-size: 13px;
                font-weight: 400;
            }
            #operationRow {
                background: #fcfcfa;
                border: 1px solid #e6e9ef;
                border-radius: 10px;
            }
            #operationRow[state="running"] {
                background: #f7faff;
                border: 1px solid #d5e2f2;
            }
            #operationRow[state="completed"] {
                background: #f7faf7;
                border: 1px solid #d8e8de;
            }
            #operationRow[state="failed"] {
                background: #fff7f6;
                border: 1px solid #f0d0cb;
            }
            #operationRail {
                background: #cfd7e6;
                border-radius: 1px;
            }
            #operationRow[state="running"] #operationRail {
                background: #7c9ecf;
            }
            #operationRow[state="completed"] #operationRail {
                background: #67a37f;
            }
            #operationRow[state="failed"] #operationRail {
                background: #d96d63;
            }
            #operationDot {
                background: #7c9ecf;
                border: 2px solid #eef4fb;
                border-radius: 5px;
            }
            #operationRow[state="completed"] #operationDot {
                background: #67a37f;
                border: 2px solid #edf7f1;
            }
            #operationRow[state="failed"] #operationDot {
                background: #d96d63;
                border: 2px solid #fff1ef;
            }
            #operationTitle {
                color: #202939;
                font-size: 16px;
                font-weight: 600;
                line-height: 1.35;
            }
            #operationKind {
                color: #7d8898;
                font-size: 13px;
                font-weight: 500;
            }
            #operationStatus {
                color: #445469;
                background: #edf3fb;
                border: 1px solid #d5e2f2;
                border-radius: 999px;
                padding: 0 12px;
                font-size: 14px;
                font-weight: 600;
                min-width: 104px;
            }
            #operationRow[state="running"] #operationStatus {
                color: #35527f;
                background: #edf3fb;
                border: 1px solid #d5e2f2;
            }
            #operationRow[state="completed"] #operationStatus {
                color: #1f6a46;
                background: #edf7f1;
                border: 1px solid #cfe3d7;
            }
            #operationRow[state="failed"] #operationStatus {
                color: #b42318;
                background: #fff1ef;
                border: 1px solid #f0d0cb;
            }
            QAbstractScrollArea {
                background: transparent;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 4px 2px 4px 2px;
            }
            QScrollBar::handle:vertical {
                background: #cfcdc4;
                min-height: 40px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background: #a09c92;
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
                height: 8px;
                margin: 2px 4px 2px 4px;
            }
            QScrollBar::handle:horizontal {
                background: #cfcdc4;
                min-width: 40px;
                border-radius: 4px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #a09c92;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
            QScrollBar::left-arrow:horizontal, QScrollBar::right-arrow:horizontal,
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: transparent;
                border: none;
                width: 0px;
            }
            #promptEdit {
                background: #ffffff;
                border: 1px solid #cfcdc4;
                border-radius: 8px;
                padding: 0 16px;
                color: #26251e;
                font-size: 16px;
                font-weight: 400;
                selection-background-color: #efeee8;
                selection-color: #26251e;
            }
            #promptEdit:focus {
                border: 1px solid #26251e;
            }
            #sendButton {
                min-width: 88px;
                min-height: 40px;
                border: none;
                border-radius: 8px;
                background: #f54e00;
                color: #ffffff;
                font-size: 14px;
                font-weight: 500;
            }
            #sendButton:hover {
                background: #d04200;
            }
            #sendButton:pressed {
                background: #d04200;
            }
            #sendButton:disabled {
                background: #e6e5e0;
                color: #a09c92;
            }
            #minorButton {
                min-width: 72px;
                min-height: 40px;
                border: 1px solid #cfcdc4;
                border-radius: 8px;
                background: #ffffff;
                color: #26251e;
                font-size: 14px;
                font-weight: 500;
            }
            #minorButton:hover {
                background: #fafaf7;
            }
            #warnButton {
                min-width: 72px;
                min-height: 40px;
                border: 1px solid #cf2d56;
                border-radius: 8px;
                background: #ffffff;
                color: #cf2d56;
                font-size: 14px;
                font-weight: 500;
            }
            #warnButton:hover {
                background: #fafaf7;
            }
            #expandDetailButton {
                border: none;
                background: transparent;
                color: #26251e;
                font-size: 14px;
                font-weight: 500;
                padding: 2px 0;
                text-align: left;
            }
            #expandDetailButton:hover {
                color: #f54e00;
                text-decoration: underline;
            }
            QMenu {
                background: #ffffff;
                color: #26251e;
                border: 1px solid #e6e5e0;
                padding: 4px 0;
            }
            QMenu::item {
                padding: 8px 28px 8px 16px;
            }
            QMenu::item:selected {
                background: #fafaf7;
            }
            QStatusBar {
                background: #f7f7f4;
                color: #807d72;
                border-top: 1px solid #e6e5e0;
                padding: 4px 12px;
                font-size: 13px;
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
        from_archive: bool = False,
    ) -> QPushButton:
        button = QPushButton(title)
        button.setProperty("conversation", True)
        button.setProperty("active", False)
        button.setProperty("pinned", bool(pinned))
        if not conversation_id:
            conversation_id = "conv_{}".format(int(time.time() * 1000))
        is_history = bool(from_archive) or str(conversation_id).startswith("session::")
        button.setProperty("is_history", is_history)
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
        self._chat_follow_tail = True
        self.prompt_edit.clear()
        self.prompt_edit.setFocus()

    def _ensure_assistant_card(self) -> AssistantCard:
        if self._assistant_card is None:
            self._assistant_card = AssistantCard()
            self._assistant_card.set_meta("")
            self._insert_message(self._assistant_card, False)
        return self._assistant_card

    def _insert_message(self, widget: QWidget, align_right: bool, *, suppress_chat_scroll: bool = False) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 4, 0)
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
        self._update_message_widths(scroll_if_following=not suppress_chat_scroll)
        if not suppress_chat_scroll:
            self._scroll_chat_to_bottom_force()

    def _update_message_widths(self, *, scroll_if_following: bool = True) -> None:
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
        if scroll_if_following:
            self._scroll_chat_to_bottom_if_following()

    def _on_chat_scroll_value_changed(self, value: int) -> None:
        if self._chat_scroll_programmatic:
            return
        bar = self.chat_scroll.verticalScrollBar()
        maximum = bar.maximum()
        if maximum <= 0:
            self._chat_follow_tail = True
            return
        margin = 72
        self._chat_follow_tail = value >= maximum - margin

    def _on_chat_scroll_range_changed(self, _minimum: int, maximum: int) -> None:
        """内容高度变化后：若用户本就在底部，保持贴底（布局完成后再滚）。"""
        if self._chat_scroll_programmatic or not self._chat_follow_tail:
            return
        if maximum <= 0:
            return
        QTimer.singleShot(0, self._scroll_chat_to_bottom_programmatic)

    def _scroll_chat_to_bottom_programmatic(self) -> None:
        bar = self.chat_scroll.verticalScrollBar()
        self._chat_scroll_programmatic = True
        try:
            bar.setValue(bar.maximum())
        finally:
            self._chat_scroll_programmatic = False

    def _scroll_chat_to_bottom_if_following(self) -> None:
        if not self._chat_follow_tail:
            return
        QTimer.singleShot(0, self._scroll_chat_to_bottom_programmatic)

    def _scroll_chat_to_bottom_force(self) -> None:
        """新消息插入或用户发送后：重新锁定到底部并滚过去。"""
        self._chat_follow_tail = True
        QTimer.singleShot(0, self._scroll_chat_to_bottom_programmatic)

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
        self._worker_conversation_button = self._active_conversation_button

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
                self._scroll_chat_to_bottom_if_following()
            return

        if re.search(r"^Executing\s+.+?\s+with args\s+.+", text):
            return

        if re.search(r"^Completed\s+.+?\s+->\s+.+", text):
            if self._active_conversation_button is not None:
                self._save_current_conversation_state(self._active_conversation_button)
            self._scroll_chat_to_bottom_if_following()
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
        self._scroll_chat_to_bottom_if_following()

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
        self._scroll_chat_to_bottom_if_following()

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
        self._scroll_chat_to_bottom_if_following()

    def _operation_label(self, kind: str) -> str:
        mapping = {
            "command.run": "执行命令",
            "browser.open": "打开浏览器",
            "browser.search": "浏览器搜索",
            "scholar.baidu_search": "百度学术检索",
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
            "检测到百度安全验证",
            "请在浏览器中手动完成验证",
            "验证已通过，正在继续执行搜索任务",
            "百度安全验证已通过，正在继续执行搜索任务",
        ]
        if any(marker in localized for marker in verification_markers):
            card = self._ensure_assistant_card()
            card.append_text(localized, "yellow")
            if self._active_conversation_button is not None:
                self._save_current_conversation_state(self._active_conversation_button)
            self._scroll_chat_to_bottom_if_following()
        self._set_status_message(message, timed=self._running)

    def _ask_clarification(self, question: str) -> None:
        if self._worker_conversation_button is not None and self._active_conversation_button is not self._worker_conversation_button:
            self._activate_conversation(self._worker_conversation_button)
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
        self._scroll_chat_to_bottom_if_following()

    def _clarification_consumed(self) -> None:
        if self._worker_conversation_button is not None and self._active_conversation_button is not self._worker_conversation_button:
            self._activate_conversation(self._worker_conversation_button)
        self._assistant_card = AssistantCard()
        self._assistant_card.set_meta("")
        self._seen_operation_states.clear()
        self._insert_message(self._assistant_card, False)
        self._assistant_card.append_text("\u5df2\u6536\u5230\u4f60\u7684\u8865\u5145\u4fe1\u606f\uff0c\u6b63\u5728\u7ee7\u7eed\u89c4\u5212\u3002", "cyan")
        self._set_status_message("\u5df2\u6536\u5230\u4f60\u7684\u8865\u5145\u4fe1\u606f\uff0c\u6b63\u5728\u7ee7\u7eed\u89c4\u5212")
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        self._scroll_chat_to_bottom_if_following()

    def _ask_authorization(self, action_type: str, details: str) -> None:
        if self._worker_conversation_button is not None and self._active_conversation_button is not self._worker_conversation_button:
            self._activate_conversation(self._worker_conversation_button)
        self._awaiting_authorization = True
        self._pending_authorization = {"action_type": action_type, "details": details}
        self.prompt_edit.setEnabled(False)
        self.run_button.setEnabled(False)
        card = self._ensure_assistant_card()
        card.append_text("\u8fd9\u4e00\u6b65\u6709\u98ce\u9669\uff0c\u9700\u8981\u4f60\u786e\u8ba4\u540e\u6211\u518d\u7ee7\u7eed\u3002", "yellow")
        card.record_authorization_pending(action_type, details)

        def decide(allowed: bool) -> None:
            auth_card = self._active_authorization_card
            if auth_card is not None:
                auth_card.apply_resolved(allowed)
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
            self._awaiting_authorization = False
            self._pending_input_mode = ""
            self._pending_authorization = None
            self.prompt_edit.setEnabled(True)
            self.run_button.setEnabled(True)
            self._worker.submit_authorization(allowed)
            if self._assistant_card is not None:
                self._assistant_card.append_text("\u5df2\u5141\u8bb8\u7ee7\u7eed\u3002" if allowed else "\u5df2\u62d2\u7edd\u8fd9\u4e00\u6b65\u3002", "yellow")
            if self._active_conversation_button is not None:
                self._save_current_conversation_state(self._active_conversation_button)
            self._scroll_chat_to_bottom_if_following()

        self._mount_authorization_card(card, action_type, details, decide)
        self._set_status_message("\u8bf7\u786e\u8ba4\u662f\u5426\u5141\u8bb8\u6267\u884c\u8be5\u6b65\u9aa4")

    def _mount_authorization_card(self, card: AssistantCard, action_type: str, details: str, on_decide) -> None:
        auth_widget = AuthorizationCard(action_type, details, on_decide)
        self._active_authorization_card = auth_widget
        card.add_inline_widget(auth_widget)
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
        self._scroll_chat_to_bottom_if_following()

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
        self.prompt_edit.setEnabled(False)
        self.run_button.setEnabled(False)
        self._set_status_message("\u8bf7\u786e\u8ba4\u662f\u5426\u5141\u8bb8\u6267\u884c\u8be5\u6b65\u9aa4")

        def decide(allowed: bool) -> None:
            auth_card = self._active_authorization_card
            if auth_card is not None:
                auth_card.apply_resolved(allowed)
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
            self._awaiting_authorization = False
            self._pending_input_mode = ""
            self._pending_authorization = None
            self.prompt_edit.setEnabled(True)
            self.run_button.setEnabled(True)
            self._worker.submit_authorization(allowed)
            if self._assistant_card is not None:
                self._assistant_card.append_text("\u5df2\u5141\u8bb8\u7ee7\u7eed\u3002" if allowed else "\u5df2\u62d2\u7edd\u8fd9\u4e00\u6b65\u3002", "yellow")
            if self._active_conversation_button is not None:
                self._save_current_conversation_state(self._active_conversation_button)
            self._scroll_chat_to_bottom_if_following()

        self._mount_authorization_card(self._assistant_card, action_type, details, decide)

    def _task_finished(self, payload: dict) -> None:
        if self._worker_conversation_button is not None and self._active_conversation_button is not self._worker_conversation_button:
            self._activate_conversation(self._worker_conversation_button)
        self._awaiting_clarification = False
        self._awaiting_authorization = False
        self._pending_input_mode = ""
        self._clarification_resume_active = False
        self._active_authorization_card = None
        self._pending_authorization = None
        self._worker_conversation_button = None
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
        self._scroll_chat_to_bottom_if_following()

    def _task_failed(self, error: str) -> None:
        error = self._localize_runtime_text(error)
        if self._worker_conversation_button is not None and self._active_conversation_button is not self._worker_conversation_button:
            self._activate_conversation(self._worker_conversation_button)
        self._awaiting_clarification = False
        self._awaiting_authorization = False
        self._pending_input_mode = ""
        self._clarification_resume_active = False
        self._active_authorization_card = None
        self._pending_authorization = None
        self._worker_conversation_button = None
        if self._assistant_card is not None:
            message = "\u4efb\u52a1\u5df2\u53d6\u6d88\u3002" if "\u53d6\u6d88" in str(error) else f"\u4efb\u52a1\u5931\u8d25\uff1a{error}"
            self._assistant_card.append_text(message, "red" if "\u53d6\u6d88" not in str(error) else "yellow")
            self._assistant_card.add_command("\u4efb\u52a1\u4e2d\u65ad", "warn")
        if self._active_conversation_button is not None:
            self._save_current_conversation_state(self._active_conversation_button)
            self._persist_conversation_history()
        self._set_status_message("\u4efb\u52a1\u5df2\u53d6\u6d88" if "\u53d6\u6d88" in str(error) else "\u4efb\u52a1\u5931\u8d25")
        self._scroll_chat_to_bottom_if_following()

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
                self._insert_message(box, True, suppress_chat_scroll=True)
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
                self._insert_message(card, False, suppress_chat_scroll=True)
        if self._assistant_card is not None and self._awaiting_authorization and not self._pending_authorization:
            recovered = self._assistant_card.pending_authorization_from_stream()
            if recovered:
                self._pending_authorization = recovered
        if self._awaiting_authorization and self._pending_authorization and self._assistant_card is not None:
            self._restore_authorization_card(
                self._pending_authorization.get("action_type", ""),
                self._pending_authorization.get("details", ""),
            )
        elif self._awaiting_clarification:
            self.prompt_edit.setEnabled(True)
            self.run_button.setEnabled(True)
            self._set_status_message("\u8bf7\u5728\u4e0b\u65b9\u8f93\u5165\u8865\u5145\u4fe1\u606f\u540e\u53d1\u9001")
        else:
            self.prompt_edit.setEnabled(True)
            self.run_button.setEnabled(not self._running)
        self.prompt_edit.setFocus()
        self._scroll_chat_to_bottom_force()

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
                from_archive=True,
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
        return '<div style="color:{}; margin-bottom:2px; background:transparent; line-height:1.45;">{}</div>'.format(
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
        if kind == "scholar.baidu_search":
            return "在百度学术检索 {}".format(args.get("text", ""))
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
