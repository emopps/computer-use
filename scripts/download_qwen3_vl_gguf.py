from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional


DEFAULT_ENDPOINT = "https://hf-mirror.com"
DEFAULT_REPO_ID = "Qwen/Qwen3-VL-2B-Instruct-GGUF"


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def ensure_hf_deps() -> bool:
    try:
        from huggingface_hub import HfApi, hf_hub_download  # noqa: F401
        return True
    except Exception:
        eprint("Missing dependency: huggingface_hub")
        eprint("Install it first with:")
        eprint("  python3 -m pip install -U huggingface_hub -i https://mirrors.ustc.edu.cn/pypi/web/simple")
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download the minimum files needed to run Qwen3-VL-2B-Instruct-GGUF with llama.cpp."
    )
    parser.add_argument(
        "--repo-id",
        default=DEFAULT_REPO_ID,
        help="Model repo. Defaults to the official Qwen GGUF repo.",
    )
    parser.add_argument(
        "--target-dir",
        default="/data/usershare/models/Qwen3-VL-2B-Instruct-GGUF",
        help="Directory where model files will be saved.",
    )
    parser.add_argument(
        "--llm-quant",
        choices=["Q4_K_M", "Q8_0", "F16"],
        default="Q4_K_M",
        help="Language model quantization.",
    )
    parser.add_argument(
        "--mmproj-quant",
        choices=["F16", "Q8_0"],
        default="F16",
        help="Vision projector quantization. F16 is the safer default.",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help="Hugging Face endpoint or mirror. Defaults to hf-mirror.com.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("HF_TOKEN", ""),
        help="Optional Hugging Face token. Defaults to HF_TOKEN.",
    )
    parser.add_argument(
        "--revision",
        default="main",
        help="Model revision. Defaults to main.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show matched files. Do not download.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation.",
    )
    return parser


def list_repo_files(repo_id: str, revision: str, token: str, endpoint: str) -> List[str]:
    from huggingface_hub import HfApi

    api = HfApi(endpoint=endpoint, token=token or None)
    info = api.model_info(repo_id=repo_id, revision=revision, files_metadata=False)
    files: List[str] = []
    for sibling in info.siblings:
        name = str(getattr(sibling, "rfilename", "") or "").strip()
        if name:
            files.append(name)
    files.sort()
    return files


def pick_llm_file(files: List[str], quant: str) -> str:
    escaped = re.escape(quant)
    patterns = [
        re.compile(r"(^|/)Qwen3[-_.]?VL[-_.]?2B[-_.]?Instruct[-_.].*{}.*\.gguf$".format(escaped), re.I),
        re.compile(r"(^|/).*{}.*Qwen3[-_.]?VL[-_.]?2B[-_.]?Instruct.*\.gguf$".format(escaped), re.I),
    ]
    for pattern in patterns:
        for name in files:
            lowered = name.lower()
            if "mmproj" in lowered:
                continue
            if pattern.search(name):
                return name
    raise RuntimeError("Could not find LLM GGUF for quant {}".format(quant))


def pick_mmproj_file(files: List[str], quant: str) -> str:
    escaped = re.escape(quant)
    patterns = [
        re.compile(r"(^|/).*mmproj.*{}.*\.gguf$".format(escaped), re.I),
        re.compile(r"(^|/).*{}.*mmproj.*\.gguf$".format(escaped), re.I),
    ]
    for pattern in patterns:
        for name in files:
            if pattern.search(name):
                return name
    raise RuntimeError("Could not find mmproj GGUF for quant {}".format(quant))


def pick_metadata_files(files: List[str]) -> List[str]:
    keep = []
    for name in files:
        if name.endswith(".json") or name.endswith(".txt"):
            keep.append(name)
    return keep


def confirm() -> bool:
    try:
        answer = input("Start download? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def download_files(
    repo_id: str,
    revision: str,
    token: str,
    endpoint: str,
    target_dir: str,
    filenames: List[str],
) -> List[str]:
    from huggingface_hub import hf_hub_download

    saved_paths: List[str] = []
    for filename in filenames:
        path = hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            revision=revision,
            token=token or None,
            endpoint=endpoint,
            local_dir=target_dir,
            local_dir_use_symlinks=False,
            resume_download=True,
        )
        saved_paths.append(str(path))
    return saved_paths


def main() -> int:
    args = build_parser().parse_args()
    if not ensure_hf_deps():
        return 1

    endpoint = str(args.endpoint or "").strip() or DEFAULT_ENDPOINT
    os.environ["HF_ENDPOINT"] = endpoint

    target_dir = str(Path(args.target_dir).expanduser().resolve())
    Path(target_dir).mkdir(parents=True, exist_ok=True)

    print("Repo:         ", args.repo_id)
    print("Revision:     ", args.revision)
    print("Endpoint:     ", endpoint)
    print("Target dir:   ", target_dir)
    print("LLM quant:    ", args.llm_quant)
    print("mmproj quant: ", args.mmproj_quant)
    print()

    try:
        files = list_repo_files(args.repo_id, args.revision, args.token, endpoint)
        llm_file = pick_llm_file(files, args.llm_quant)
        mmproj_file = pick_mmproj_file(files, args.mmproj_quant)
        metadata_files = pick_metadata_files(files)
    except Exception as exc:
        eprint("Failed to resolve repo files:", exc)
        return 2

    wanted = [llm_file, mmproj_file] + metadata_files

    print("Selected files:")
    print(" -", llm_file)
    print(" -", mmproj_file)
    for item in metadata_files:
        print(" -", item)
    print()

    if args.dry_run:
        print("Dry run only. No download started.")
        return 0

    if not args.yes and not confirm():
        print("Cancelled.")
        return 0

    try:
        saved_paths = download_files(
            repo_id=args.repo_id,
            revision=args.revision,
            token=args.token,
            endpoint=endpoint,
            target_dir=target_dir,
            filenames=wanted,
        )
    except Exception as exc:
        eprint("Download failed:", exc)
        return 3

    print("Download finished.")
    print("Saved files:")
    for path in saved_paths:
        print(" -", path)
    print()
    print("Suggested llama-server command:")
    print(
        "  llama-server -m {}/{} --mmproj {}/{} --host 127.0.0.1 --port 8080 -ngl 0".format(
            target_dir,
            Path(llm_file).name,
            target_dir,
            Path(mmproj_file).name,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
