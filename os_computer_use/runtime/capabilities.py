from __future__ import annotations

from typing import Iterable, List


SUPPORTED_OPERATION_KINDS = (
    "meeting.extract_actions",
    "meeting.send_assignments",
    "research.collect_literature",
    "browser.open",
    "browser.search",
    "scholar.baidu_search",
    "browser.send",
    "spreadsheet.open",
    "spreadsheet.write_cell",
    "command.run",
    "filesystem.list",
    "filesystem.open_path",
    "filesystem.create_folder",
    "filesystem.copy",
    "filesystem.move",
    "filesystem.rename",
    "filesystem.delete",
    "filesystem.write_text",
    "document.extract_text",
    "document.extract_structured",
)


def is_supported_operation_kind(kind: str) -> bool:
    return kind in SUPPORTED_OPERATION_KINDS


def supported_kinds_text(extra_kinds: Iterable[str] = ()) -> str:
    values: List[str] = list(SUPPORTED_OPERATION_KINDS)
    for kind in extra_kinds:
        if kind not in values:
            values.append(kind)
    return ", ".join(values)
