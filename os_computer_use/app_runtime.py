from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from os_computer_use.logging import logger

load_dotenv()


def visual_fallback_enabled() -> bool:
    value = str(os.getenv("OCU_ENABLE_VISUAL_FALLBACK", "0") or "0").strip().lower()
    return value not in {"0", "false", "no", "off", "disable", "disabled"}


def ensure_supported_python() -> None:
    if sys.version_info < (3, 8):
        version = ".".join(str(part) for part in sys.version_info[:3])
        raise RuntimeError(
            f"Python 3.8+ is required, but the current interpreter is {version}. "
            "Please run this project with python3."
        )


def initialize_run_directories():
    run_id = 1
    while os.path.exists("./output/run_{}".format(run_id)) or os.path.exists(
        "./memory/sessions/run_{}".format(run_id)
    ):
        run_id += 1

    output_dir = "./output/run_{}".format(run_id)
    memory_dir = "./memory"
    session_memory_dir = "./memory/sessions/run_{}".format(run_id)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(session_memory_dir, exist_ok=True)
    os.makedirs("./memory/persistent/history", exist_ok=True)
    os.makedirs("./memory/persistent/preferences", exist_ok=True)
    os.makedirs("./memory/persistent/index", exist_ok=True)
    return output_dir, memory_dir, session_memory_dir


async def build_agent(output_dir, memory_dir, session_memory_dir):
    from os_computer_use.agents.action import ActionAgent
    from os_computer_use.agents.audit import AuditAgent
    from os_computer_use.agents.decision import DecisionAgent
    from os_computer_use.agents.intent import IntentAgent
    from os_computer_use.agents.memory import MemoryAgent
    from os_computer_use.agents.planner import PlannerAgent
    from os_computer_use.desktop.file_tool import FileTool
    from os_computer_use.desktop.local_desktop import LocalDesktop
    from os_computer_use.llm.config import action_model, vision_model

    desktop = LocalDesktop()
    enabled = visual_fallback_enabled()
    effective_vision_model = vision_model if enabled else None
    await prepare_visual_fallback(effective_vision_model)
    return {
        "intent": IntentAgent(action_model),
        "planner": PlannerAgent(action_model),
        "decision": DecisionAgent(provider=action_model),
        "action": ActionAgent(
            desktop,
            FileTool(),
            reasoning_model=action_model,
            vision_model=effective_vision_model,
            artifact_dir=output_dir,
        ),
        "memory": MemoryAgent(memory_root=memory_dir, session_dir=session_memory_dir),
        "audit": AuditAgent(os.path.join(output_dir, "audit.log")),
    }


async def warmup_runtime():
    from os_computer_use.llm.config import action_model, vision_model

    _ = action_model
    effective_vision_model = vision_model if visual_fallback_enabled() else None
    await prepare_visual_fallback(effective_vision_model)
    return {
        "reasoning_ready": True,
        "vision_ready": effective_vision_model is not None,
    }


async def prepare_visual_fallback(vision_model):
    if vision_model is None:
        return
    try:
        if hasattr(vision_model, "_ensure_loaded"):
            logger.log("Loading local vision model...", "cyan")
            vision_model._ensure_loaded()
            logger.log("Local vision model is ready.", "green")
            return
        if hasattr(vision_model, "_candidate_endpoints"):
            logger.log("Checking local vision server...", "cyan")
            endpoints = vision_model._candidate_endpoints()
            base_url = getattr(vision_model, "base_url", endpoints[0] if endpoints else "")
            from scripts.run_local_vision_main import ensure_file, server_ready, start_server
            import argparse

            if server_ready(base_url):
                logger.log("Vision server is running: {}".format(base_url), "green")
                return

            logger.log(
                "Vision server not reachable at {}, attempting to start...".format(base_url),
                "yellow",
            )
            model_path_str = os.getenv(
                "LOCAL_LLAMA_CPP_VISION_MODEL_PATH",
                "/data/usershare/models/Qwen3-VL-2B-Instruct-GGUF/Qwen3VL-2B-Instruct-Q4_K_M.gguf",
            )
            mmproj_path_str = os.getenv(
                "LOCAL_LLAMA_CPP_VISION_MMPROJ_PATH",
                "/data/usershare/models/Qwen3-VL-2B-Instruct-GGUF/mmproj-Qwen3VL-2B-Instruct-F16.gguf",
            )
            if not os.path.isfile(model_path_str) or not os.path.isfile(mmproj_path_str):
                logger.log(
                    "Vision model files not found. Model: {}, mmproj: {}".format(
                        model_path_str, mmproj_path_str
                    ),
                    "red",
                )
                logger.log(
                    "Run: python3 scripts/download_qwen3_vl_gguf.py && bash scripts/run_local_vision_main.sh",
                    "yellow",
                )
                return
            server_args = argparse.Namespace(
                skip_server_start=False,
                server_command=os.getenv("LLAMA_SERVER_CMD", "llama-server"),
                model=model_path_str,
                mmproj=mmproj_path_str,
                alias=os.getenv("LOCAL_LLAMA_CPP_VISION_MODEL", "qwen3-vl-2b-instruct").strip(),
                host="127.0.0.1",
                port=8080,
                ctx_size=4096,
                gpu_layers=int(os.getenv("LLAMA_SERVER_NGL", "0")),
                server_log="output/llama_cpp_vision_server.log",
                pid_file="output/llama_cpp_vision_server.pid",
                startup_timeout=120,
            )
            try:
                model_path = ensure_file(server_args.model, "Model")
                mmproj_path = ensure_file(server_args.mmproj, "mmproj")
                start_server(server_args, model_path, mmproj_path)
                logger.log("Vision server started successfully.", "green")
            except Exception as start_exc:
                logger.log("Failed to start vision server: {}".format(start_exc), "red")
                logger.log("Start it manually: bash scripts/run_local_vision_main.sh", "yellow")
            return
    except ImportError:
        logger.log("run_local_vision_main module not available for server startup.", "yellow")
    except Exception as exc:
        logger.log("Vision fallback warmup failed: {}".format(exc), "yellow")
