import os

from os_computer_use.llm import providers


DEFAULT_QWEN3_VL_GGUF_DIR = "/data/usershare/models/Qwen3-VL-2B-Instruct-GGUF"
DEFAULT_QWEN3_VL_GGUF_MODEL = DEFAULT_QWEN3_VL_GGUF_DIR + "/Qwen3VL-2B-Instruct-Q4_K_M.gguf"

TEXT_PROVIDER = str(os.getenv("OCU_TEXT_PROVIDER", "openrouter") or "openrouter").strip().lower()
VISION_PROVIDER = str(os.getenv("OCU_VISION_PROVIDER", "local") or "local").strip().lower()

LOCAL_REASONING_MODEL_PATH = os.getenv(
    "LOCAL_REASONING_MODEL_PATH",
    "/data/usershare/models/qwen2.5-3b-instruct-q4_k_m.gguf",
).strip()
OPENROUTER_REASONING_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openai/gpt-oss-20b:free",
).strip()
OPENROUTER_VISION_MODEL = os.getenv(
    "OPENROUTER_VISION_MODEL",
    "nvidia/nemotron-nano-12b-v2-vl:free",
).strip()

LOCAL_VISION_MODEL_PATH = os.getenv(
    "LOCAL_VISION_MODEL_PATH",
    DEFAULT_QWEN3_VL_GGUF_DIR,
).strip()
LOCAL_VISION_BACKEND = str(os.getenv("LOCAL_VISION_BACKEND", "llama_cpp") or "llama_cpp").strip().lower()
LOCAL_LLAMA_CPP_VISION_BASE_URL = os.getenv(
    "LOCAL_LLAMA_CPP_VISION_BASE_URL",
    "http://127.0.0.1:8080/v1",
).strip()
LOCAL_LLAMA_CPP_VISION_MODEL = os.getenv(
    "LOCAL_LLAMA_CPP_VISION_MODEL",
    "qwen3-vl-2b-instruct",
).strip()


def _build_local_vision_provider():
    backend = LOCAL_VISION_BACKEND
    if backend == "auto":
        backend = "llama_cpp" if os.path.exists(DEFAULT_QWEN3_VL_GGUF_MODEL) else "hf"

    if backend == "llama_cpp":
        return providers.LocalLlamaCppVisionProvider(
            base_url=LOCAL_LLAMA_CPP_VISION_BASE_URL,
            model=LOCAL_LLAMA_CPP_VISION_MODEL or None,
            max_new_tokens=160,
            timeout=180,
        )

    return providers.LocalHFVisionProvider(
        LOCAL_VISION_MODEL_PATH,
        cpu_only=True,
        longest_edge=512,
        max_new_tokens=160,
    )


def _build_openrouter_text_provider():
    return providers.OpenRouterProvider(OPENROUTER_REASONING_MODEL)


def _build_local_text_provider():
    return providers.OpenRouterProvider(LOCAL_REASONING_MODEL_PATH)


def _build_openrouter_vision_provider():
    return providers.OpenRouterProvider(OPENROUTER_VISION_MODEL)


def _build_text_provider():
    if TEXT_PROVIDER == "local":
        return _build_local_text_provider()
    return _build_openrouter_text_provider()


def _build_vision_provider():
    if VISION_PROVIDER == "openrouter":
        return _build_openrouter_vision_provider()
    return _build_local_vision_provider()


action_model = _build_text_provider()
vision_model = _build_vision_provider()
grounding_model = vision_model
