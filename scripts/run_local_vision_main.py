from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_SERVER_LOG = "output/llama_cpp_vision_server.log"
DEFAULT_PID_FILE = "output/llama_cpp_vision_server.pid"
TEST_163_USERNAME = "test_meeting2026@163.com"
TEST_163_PASSWORD = "Haha1234"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start or reuse a local llama.cpp vision server, then run main.py."
    )
    parser.add_argument("--prompt", type=str, help="Single-shot task, same as main.py --prompt.")
    parser.add_argument(
        "--visual-only",
        action="store_true",
        help="Bypass planner and run tasks directly through VisualExecutor.",
    )
    parser.add_argument(
        "--pure-visual",
        action="store_true",
        help="When used with --visual-only, allow only keyboard/mouse style actions.",
    )
    parser.add_argument(
        "--server-command",
        default="llama-server",
        help="Command used to start llama.cpp server.",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Path to the Qwen3-VL GGUF model file.",
    )
    parser.add_argument(
        "--mmproj",
        required=True,
        help="Path to the multimodal projector GGUF file.",
    )
    parser.add_argument(
        "--alias",
        default="qwen3-vl-2b-instruct",
        help="Model alias exposed to the local OpenAI-compatible server.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="llama.cpp server host.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="llama.cpp server port.")
    parser.add_argument(
        "--ctx-size",
        type=int,
        default=4096,
        help="Context size passed to llama-server.",
    )
    parser.add_argument(
        "--gpu-layers",
        type=int,
        default=0,
        help="GPU layers for llama.cpp. Use 0 on VM/CPU-only setups.",
    )
    parser.add_argument(
        "--skip-server-start",
        action="store_true",
        help="Assume the local llama.cpp server is already running.",
    )
    parser.add_argument(
        "--server-log",
        default=DEFAULT_SERVER_LOG,
        help="Log file for the background llama.cpp server process.",
    )
    parser.add_argument(
        "--pid-file",
        default=DEFAULT_PID_FILE,
        help="PID file for the background llama.cpp server process.",
    )
    parser.add_argument(
        "--startup-timeout",
        type=int,
        default=120,
        help="Seconds to wait for llama.cpp server startup.",
    )
    return parser


def apply_test_163_mail_credentials() -> None:
    os.environ.setdefault("OCU_163_USERNAME", TEST_163_USERNAME)
    os.environ.setdefault("OCU_163_PASSWORD", TEST_163_PASSWORD)


def ensure_file(path_str: str, label: str) -> Path:
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError("{} does not exist: {}".format(label, path))
    return path


def resolve_server_command(command: str) -> str:
    candidate = str(command or "").strip()
    if not candidate:
        raise FileNotFoundError("llama-server command is empty.")

    direct = Path(candidate).expanduser()
    if direct.exists():
        return str(direct.resolve())

    discovered = shutil.which(candidate)
    if discovered:
        return discovered

    common_candidates = [
        Path.home() / "llama.cpp" / "build" / "bin" / "llama-server",
        Path.home() / "llama.cpp" / "build" / "bin" / "Release" / "llama-server",
        Path("/usr/local/bin/llama-server"),
        Path("/usr/bin/llama-server"),
        Path("/usr/local/bin/llama-server.exe"),
        Path("/usr/bin/llama-server.exe"),
    ]
    for item in common_candidates:
        if item.exists():
            return str(item.resolve())

    raise FileNotFoundError(
        "Could not find llama-server. Install/build llama.cpp first, or pass "
        "--server-command /path/to/llama-server, or set LLAMA_SERVER_CMD."
    )


def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def server_ready(base_url: str) -> bool:
    normalized = base_url.rstrip("/")
    root_url = normalized[:-3] if normalized.endswith("/v1") else normalized
    candidates = [
        normalized + "/models",
        root_url + "/health",
    ]
    for url in candidates:
        try:
            response = requests.get(url, timeout=2)
            if response.status_code < 500:
                return True
        except requests.RequestException:
            continue
    return False


def start_server(args, model_path: Path, mmproj_path: Path) -> Optional[subprocess.Popen]:
    if args.skip_server_start:
        return None

    base_url = "http://{}:{}/v1".format(args.host, args.port)
    if server_ready(base_url):
        print("Using existing llama.cpp server at {}".format(base_url))
        return None

    log_path = Path(args.server_log).resolve()
    pid_path = Path(args.pid_file).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    server_command = resolve_server_command(args.server_command)
    command = [
        server_command,
        "-m",
        str(model_path),
        "--mmproj",
        str(mmproj_path),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "-c",
        str(args.ctx_size),
        "-ngl",
        str(args.gpu_layers),
        "--alias",
        args.alias,
    ]

    print("Starting llama.cpp vision server...")
    print("Command: {}".format(" ".join(command)))
    with open(log_path, "ab") as log_file:
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    pid_path.write_text(str(process.pid), encoding="utf-8")

    deadline = time.time() + args.startup_timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "llama.cpp server exited early. Check log: {}".format(log_path)
            )
        if server_ready(base_url):
            print("llama.cpp server is ready at {}".format(base_url))
            return process
        time.sleep(1.0)

    raise TimeoutError(
        "Timed out waiting for llama.cpp server startup. Check log: {}".format(log_path)
    )


def run_main(args) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["LOCAL_VISION_BACKEND"] = "llama_cpp"
    env["LOCAL_LLAMA_CPP_VISION_BASE_URL"] = "http://{}:{}/v1".format(args.host, args.port)
    env["LOCAL_LLAMA_CPP_VISION_MODEL"] = args.alias
    env.setdefault("TOKENIZERS_PARALLELISM", "false")

    command = [sys.executable, "main.py"]
    if args.prompt:
        command.extend(["--prompt", args.prompt])

    return subprocess.call(command, cwd=str(repo_root), env=env)


def _normalize_visual_instruction(raw_instruction: str, args) -> str:
    text = str(raw_instruction or "").strip()
    if not text:
        return text

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    os.environ["LOCAL_VISION_BACKEND"] = "llama_cpp"
    os.environ["LOCAL_LLAMA_CPP_VISION_BASE_URL"] = "http://{}:{}/v1".format(args.host, args.port)
    os.environ["LOCAL_LLAMA_CPP_VISION_MODEL"] = args.alias

    from os_computer_use.agents.intent import IntentAgent
    from os_computer_use.llm.config import action_model

    try:
        return IntentAgent(action_model).normalize_visual_instruction(text)
    except Exception:
        pass
    return text


async def _run_visual_only_session(args) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    os.environ["LOCAL_VISION_BACKEND"] = "llama_cpp"
    os.environ["LOCAL_LLAMA_CPP_VISION_BASE_URL"] = "http://{}:{}/v1".format(args.host, args.port)
    os.environ["LOCAL_LLAMA_CPP_VISION_MODEL"] = args.alias
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    from os_computer_use.desktop.local_desktop import LocalDesktop
    from os_computer_use.desktop.visual_executor import VisualExecutor
    from os_computer_use.llm.config import action_model, vision_model

    desktop = LocalDesktop()
    executor = VisualExecutor(
        desktop=desktop,
        reasoning_model=action_model,
        vision_model=vision_model,
        pure_visual_actions=bool(args.pure_visual),
    )

    one_shot = bool(args.prompt)
    current_prompt = args.prompt

    while True:
        if not current_prompt:
            try:
                current_prompt = input("Visual User: ").strip()
            except KeyboardInterrupt:
                print()
                return 0
        if not current_prompt:
            if one_shot:
                return 0
            current_prompt = None
            continue

        try:
            normalized_instruction = _normalize_visual_instruction(current_prompt, args)
            print("Visual normalized instruction: {}".format(normalized_instruction))
            result = await executor.run(
                instruction=normalized_instruction,
                failure_context="forced visual-only mode",
            )
            print(result)
        except KeyboardInterrupt:
            print()
            if one_shot:
                return 0
        except Exception as exc:
            print("Visual-only execution failed: {}".format(exc), file=sys.stderr)
            if one_shot:
                return 1

        if one_shot:
            return 0
        current_prompt = None


def run_visual_only(args) -> int:
    return asyncio.run(_run_visual_only_session(args))


def main() -> int:
    args = build_parser().parse_args()
    apply_test_163_mail_credentials()
    model_path = ensure_file(args.model, "Model")
    mmproj_path = ensure_file(args.mmproj, "mmproj")

    try:
        start_server(args, model_path, mmproj_path)
    except FileNotFoundError as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1
    if args.visual_only:
        return run_visual_only(args)
    return run_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
