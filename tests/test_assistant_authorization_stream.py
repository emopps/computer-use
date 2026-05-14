"""AssistantCard 授权流快照：切换会话后应能恢复待确认状态（与 main_window 逻辑一致）。"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtWidgets import QApplication

from os_computer_use.gui.main_window import AssistantCard


def test_authorization_snapshot_roundtrip_and_pending_inference() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    card = AssistantCard()
    card.append_text("这一步有风险，需要你确认后我再继续。", "yellow")
    card.record_authorization_pending("browser.send", '{"to":"a@b.com"}')

    stream = card.stream_snapshots()
    assert any(
        item.get("type") == "authorization"
        and item.get("action_type") == "browser.send"
        for item in stream
    )

    restored = AssistantCard()
    restored.restore_stream([dict(x) for x in stream])
    pending = restored.pending_authorization_from_stream()
    assert pending == {"action_type": "browser.send", "details": '{"to":"a@b.com"}'}

    restored.append_text("\u5df2\u5141\u8bb8\u7ee7\u7eed\u3002", "yellow")
    assert restored.pending_authorization_from_stream() is None


def test_pending_inference_rejected_marker() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    card = AssistantCard()
    card.append_text("risk", "yellow")
    card.record_authorization_pending("filesystem.delete", "{}")
    card.append_text("\u5df2\u62d2\u7edd\u8fd9\u4e00\u6b65\u3002", "yellow")
    assert card.pending_authorization_from_stream() is None


def test_restore_stream_inserts_readonly_card_for_resolved_authorization() -> None:
    from os_computer_use.gui.main_window import AuthorizationCard

    app = QApplication.instance() or QApplication([])
    assert app is not None

    stream = [
        {"type": "html", "html": '<div style="color:#a36a00;">warn</div>'},
        {"type": "authorization", "action_type": "browser.send", "details": "{}"},
        {"type": "html", "html": '<div style="color:#a36a00;">\u5df2\u5141\u8bb8\u7ee7\u7eed\u3002</div>'},
    ]
    card = AssistantCard()
    card.restore_stream(stream)
    assert card.pending_authorization_from_stream() is None
    auth_widgets = 0
    for i in range(card.stream_layout.count()):
        item = card.stream_layout.itemAt(i)
        if item is None:
            continue
        w = item.widget()
        if isinstance(w, AuthorizationCard):
            auth_widgets += 1
    assert auth_widgets == 1
