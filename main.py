from __future__ import annotations

import argparse
import os
import sys


def _desired_linux_im_env() -> dict[str, str]:
    if not sys.platform.startswith("linux"):
        return {}

    has_sogou = (
        os.path.exists("/opt/sogouimebs/files/bin/sogouImeService")
        or os.path.exists("/opt/sogouimebs/files/bin/sogouimebs-session")
    )
    session_type = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    current_qt_im = os.environ.get("QT_IM_MODULE", "").strip().lower()

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
    return {
        "QT_IM_MODULE": qt_im,
        "GTK_IM_MODULE": gtk_im,
        "QT4_IM_MODULE": qt_im,
        "XMODIFIERS": os.environ.get("XMODIFIERS", "@im=fcitx") or "@im=fcitx",
    }


def _bootstrap_input_method() -> None:
    if not sys.platform.startswith("linux"):
        return

    desired = _desired_linux_im_env()
    changed = False
    for key, value in desired.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
            changed = True

    marker = "_OCU_IM_BOOTSTRAPPED"
    if changed and os.environ.get(marker) != "1":
        os.environ[marker] = "1"
        os.execvpe(sys.executable, [sys.executable] + sys.argv, os.environ)


_bootstrap_input_method()


def _apply_runtime_model_overrides(args) -> None:
    if args.text_provider:
        os.environ["OCU_TEXT_PROVIDER"] = args.text_provider
    if args.vision_provider:
        os.environ["OCU_VISION_PROVIDER"] = args.vision_provider
    if args.text_model:
        os.environ["OPENROUTER_MODEL"] = args.text_model
    if args.vision_model:
        os.environ["OPENROUTER_VISION_MODEL"] = args.vision_model
    if args.local_reasoning_model:
        os.environ["LOCAL_REASONING_MODEL_PATH"] = args.local_reasoning_model
    if args.local_vision_model:
        os.environ["LOCAL_VISION_MODEL_PATH"] = args.local_vision_model
    if args.local_vision_backend:
        os.environ["LOCAL_VISION_BACKEND"] = args.local_vision_backend
    if args.llama_cpp_vision_url:
        os.environ["LOCAL_LLAMA_CPP_VISION_BASE_URL"] = args.llama_cpp_vision_url
    if args.llama_cpp_vision_model:
        os.environ["LOCAL_LLAMA_CPP_VISION_MODEL"] = args.llama_cpp_vision_model


def main() -> int:
    parser = argparse.ArgumentParser(description="Open Computer Use GUI")
    parser.add_argument("--text-provider", choices=["local", "openrouter"], help="文本模型来源")
    parser.add_argument("--vision-provider", choices=["local", "openrouter"], help="视觉模型来源")
    parser.add_argument("--text-model", help="OpenRouter 文本模型名，建议使用免费模型")
    parser.add_argument("--vision-model", help="OpenRouter 视觉模型名，建议使用免费模型")
    parser.add_argument("--local-reasoning-model", help="本地文本模型路径")
    parser.add_argument("--local-vision-model", help="本地 Hugging Face 视觉模型目录")
    parser.add_argument("--local-vision-backend", choices=["auto", "llama_cpp", "hf"], help="本地视觉后端")
    parser.add_argument("--llama-cpp-vision-url", help="llama.cpp 视觉服务地址")
    parser.add_argument("--llama-cpp-vision-model", help="llama.cpp 视觉服务模型名")
    args = parser.parse_args()

    _apply_runtime_model_overrides(args)
    from os_computer_use.gui.main_window import launch

    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
