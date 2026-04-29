from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable, List


DEFAULT_PATTERNS = [
    "*.json",
    "*.txt",
    "*.model",
    "*.tiktoken",
    "*.safetensors",
    "*.bin",
    "*.py",
]

SMALL_VLM_PRESETS = {
    "vm_8gb_safe": {
        "repo_id": "HuggingFaceTB/SmolVLM-500M-Instruct",
        "patterns": DEFAULT_PATTERNS,
        "note": "Recommended starting point for CPU-only or low-memory VMs.",
    },
    "qwen2_vl_2b": {
        "repo_id": "Qwen/Qwen2-VL-2B-Instruct",
        "patterns": DEFAULT_PATTERNS,
        "note": "Stronger, but usually too heavy for an 8GB VM without GPU acceleration.",
    },
}


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def ensure_hf_deps():
    try:
        from huggingface_hub import HfApi, snapshot_download  # noqa: F401
        return True
    except Exception:
        eprint("Missing dependency: huggingface_hub")
        eprint("Install it first with:")
        eprint("  python3 -m pip install -U huggingface_hub")
        return False


def resolve_patterns(raw_patterns: Iterable[str]) -> List[str]:
    patterns: List[str] = []
    for item in raw_patterns:
        value = str(item or "").strip()
        if value:
            patterns.append(value)
    return patterns or list(DEFAULT_PATTERNS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or download a Hugging Face model snapshot with resume support."
    )
    parser.add_argument(
        "--repo-id",
        help="Hugging Face model repo id, e.g. HuggingFaceTB/SmolVLM-500M-Instruct",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(SMALL_VLM_PRESETS.keys()),
        help="Use a predefined model recommendation.",
    )
    parser.add_argument(
        "--target-dir",
        required=True,
        help="Directory where the model snapshot will be stored.",
    )
    parser.add_argument(
        "--pattern",
        action="append",
        default=[],
        help="Allow pattern to restrict downloaded files. Repeatable.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("HF_TOKEN", ""),
        help="Optional Hugging Face token. Defaults to HF_TOKEN.",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Model branch / revision. Defaults to main.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview files only. Do not download anything.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the final confirmation prompt and start downloading immediately.",
    )
    return parser


def pick_repo_and_patterns(args) -> tuple[str, List[str], str]:
    if args.preset:
        preset = SMALL_VLM_PRESETS[args.preset]
        repo_id = preset["repo_id"]
        patterns = resolve_patterns(args.pattern) if args.pattern else list(preset["patterns"])
        note = str(preset["note"])
        if args.repo_id and args.repo_id != repo_id:
            eprint("Warning: --repo-id overrides the preset repo.")
            repo_id = args.repo_id
    else:
        repo_id = str(args.repo_id or "").strip()
        patterns = resolve_patterns(args.pattern)
        note = ""

    if not repo_id:
        raise SystemExit("You must provide --repo-id or --preset.")
    return repo_id, patterns, note


def preview_repo(repo_id: str, revision: str, token: str, patterns: List[str]) -> List[str]:
    from huggingface_hub import HfApi

    api = HfApi(token=token or None)
    info = api.model_info(repo_id=repo_id, revision=revision, files_metadata=False)
    matched: List[str] = []
    for sibling in info.siblings:
        name = str(getattr(sibling, "rfilename", "") or "")
        if _matches_any(name, patterns):
            matched.append(name)
    matched.sort()
    return matched


def _matches_any(path: str, patterns: List[str]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(path, pattern) for pattern in patterns)


def confirm() -> bool:
    try:
        answer = input("Start download? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def download(repo_id: str, revision: str, token: str, target_dir: str, patterns: List[str]) -> str:
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=target_dir,
        local_dir_use_symlinks=False,
        token=token or None,
        allow_patterns=patterns,
        resume_download=True,
    )
    return str(path)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not ensure_hf_deps():
        return 1

    repo_id, patterns, note = pick_repo_and_patterns(args)
    target_dir = str(Path(args.target_dir).expanduser().resolve())

    print("Repo:      ", repo_id)
    print("Revision:  ", args.branch)
    print("Target:    ", target_dir)
    print("Patterns:  ", ", ".join(patterns))
    if note:
        print("Note:      ", note)
    print()

    try:
        files = preview_repo(repo_id, args.branch, args.token, patterns)
    except Exception as exc:
        eprint("Failed to query model metadata:", exc)
        return 2

    if not files:
        eprint("No files matched the requested patterns.")
        return 3

    print("Matched files:")
    for item in files:
        print(" -", item)
    print()

    if args.dry_run:
        print("Dry run only. No download started.")
        return 0

    if not args.yes and not confirm():
        print("Cancelled.")
        return 0

    try:
        downloaded_path = download(repo_id, args.branch, args.token, target_dir, patterns)
    except Exception as exc:
        eprint("Download failed:", exc)
        return 4

    print("Download finished.")
    print("Saved to:", downloaded_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
