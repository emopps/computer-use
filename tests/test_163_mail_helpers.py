from types import SimpleNamespace
import sys

sys.modules.setdefault("pyautogui", SimpleNamespace(PAUSE=0.0))
sys.modules.setdefault("mss", SimpleNamespace(mss=lambda: SimpleNamespace(monitors=[None, {}])))

from os_computer_use.desktop.local_desktop import LocalDesktop


def test_looks_like_163_mail_page_detects_mail_context():
    assert LocalDesktop._looks_like_163_mail_page(
        "https://mail.163.com/js6/main.jsp?sid=abc",
        "",
    )
    assert LocalDesktop._looks_like_163_mail_page(
        "",
        "收件箱 写 信 主　题： 收件人：",
    )
    assert not LocalDesktop._looks_like_163_mail_page(
        "https://example.com",
        "random page",
    )


def test_build_163_editor_html_preserves_unicode_lines():
    html = LocalDesktop._build_163_editor_html("测试主题\n这是一封中文邮件")
    assert "测试主题" in html
    assert "这是一封中文邮件" in html
    assert "<div>" in html
    assert "?" not in html
