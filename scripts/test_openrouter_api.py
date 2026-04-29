from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from os_computer_use.llm.llm_provider import Message, OpenRouterProvider


def _mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return value[:6] + "..." + value[-4:]


def main() -> int:
    load_dotenv(".env")
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    model = os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-20b:free").strip()

    if not api_key:
        print("未检测到 OPENROUTER_API_KEY，无法测试。")
        return 1

    print("开始测试 OpenRouter API")
    print("模型:", model)
    print("Key:", _mask_key(api_key))
    print("OPENROUTER_TRUST_ENV:", os.getenv("OPENROUTER_TRUST_ENV", "false"))

    provider = OpenRouterProvider(model, api_key=api_key)
    result = provider.call(
        [Message("Reply with exactly: OPENROUTER_OK", role="user")]
    )
    print(json.dumps({"ok": True, "model": model, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
