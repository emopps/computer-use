from __future__ import annotations

from typing import Any, Dict, List


class InteractionResolver:
    def build_attempt_plan(
        self,
        *,
        operation_kind: str,
        instruction: str,
        args: Dict[str, Any],
        observation: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        del instruction
        del args

        dom = observation.get("dom", {})
        atspi = observation.get("atspi", {})
        attempts: List[Dict[str, Any]] = []

        if operation_kind in {"browser.open", "browser.search", "scholar.baidu_search", "browser.send"}:
            if dom.get("available"):
                # Check if DOM has search results or interactive elements
                elements = dom.get("elements", [])
                has_search_results = any(
                    e.get("role") == "search_result" for e in elements
                )
                attempts.append(
                    {
                        "mode": "dom",
                        "reason": "Browser page is available through Playwright DOM access."
                        + (" Search results found in DOM." if has_search_results else ""),
                    }
                )
            else:
                attempts.append(
                    {
                        "mode": "dom",
                        "reason": "Browser DOM is the primary channel for web tasks.",
                    }
                )
            attempts.append(
                {
                    "mode": "visual",
                    "reason": "Escalate to screenshots and vision only after DOM attempts fail.",
                }
            )
            return attempts

        if operation_kind == "spreadsheet.write_cell":
            # 单元格写入：仅 direct + visual。禁止在 direct 之后插入 atspi 再跑一遍 primary，
            # 否则 _execute_ui_operation 会在 atspi 返回 None 后再次 await primary_executor，
            # 造成同格二次粘贴（用户可见两次「正在向 Bx 粘贴」）并与 WPS 校验/保存竞态。
            attempts.append(
                {
                    "mode": "direct",
                    "reason": "WPS Office does not expose AT-SPI; use xdotool directly.",
                }
            )
            attempts.append(
                {
                    "mode": "visual",
                    "reason": "Escalate to screenshots and vision only after direct attempt fails.",
                }
            )
            return attempts

        if operation_kind == "spreadsheet.open":
            # 打开表格：保留 direct → atspi → visual，atspi 对多数 WPS 无效但不会重复打开同一文件。
            attempts.append(
                {
                    "mode": "direct",
                    "reason": "WPS Office does not expose AT-SPI; use xdotool directly.",
                }
            )
            if atspi.get("available"):
                atspi_elements = atspi.get("elements", [])
                if atspi_elements:
                    attempts.append(
                        {
                            "mode": "atspi",
                            "reason": "AT-SPI elements available for apps that support it.",
                        }
                    )
            attempts.append(
                {
                    "mode": "visual",
                    "reason": "Escalate to screenshots and vision only after direct and AT-SPI attempts fail.",
                }
            )
            return attempts

        # For filesystem and other operations, direct mode suffices
        attempts.append(
            {
                "mode": "direct",
                "reason": "No UI resolution strategy required for this operation kind.",
            }
        )
        return attempts
