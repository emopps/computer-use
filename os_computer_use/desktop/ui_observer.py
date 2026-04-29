from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

try:
    from os_computer_use.desktop.atspi_provider import ATSPIProvider
except Exception:
    ATSPIProvider = None


class UIObserver:
    def __init__(self, desktop: Any, atspi_provider: Optional[ATSPIProvider] = None):
        self.desktop = desktop
        self.atspi_provider = atspi_provider or (ATSPIProvider() if ATSPIProvider else None)

    async def capture(self) -> Dict[str, Any]:
        return {
            "dom": await self._dom_snapshot(),
            "atspi": self._atspi_snapshot(),
        }

    async def _dom_snapshot(self) -> Dict[str, Any]:
        page = getattr(self.desktop, "_page", None)
        if page is None:
            return {
                "available": False,
                "url": "",
                "title": "",
                "elements": [],
            }

        url = ""
        title = ""
        try:
            url = str(page.url or "")
        except Exception:
            url = ""

        elements = await self._extract_dom_elements(page)
        return {
            "available": True,
            "url": url,
            "title": title,
            "elements": elements,
        }

    async def _extract_dom_elements(self, page: Any) -> List[Dict[str, str]]:
        """Extract interactive elements from the current Playwright page via DOM evaluation."""
        return await self._async_extract_dom(page)

    async def _async_extract_dom(self, page: Any) -> List[Dict[str, str]]:
        """Run JavaScript in the browser page to extract interactive DOM elements."""
        js_script = """
        () => {
            const results = [];
            const seen = new Set();
            function add(name, role, selector, tag, href) {
                const key = (href || '') + '|' + name;
                if (seen.has(key)) return;
                seen.add(key);
                results.push({name: name, role: role, selector: selector, tag: tag, href: href || ''});
            }
            // Priority 1: Search result links (common patterns for Baidu/Google/Bing)
            document.querySelectorAll('h3 a, div.result a, div.c-container a, .t a, article a').forEach(el => {
                const text = (el.textContent || '').trim().slice(0, 80);
                const href = el.href || '';
                if (text && href && !href.startsWith('javascript')) {
                    add(text, 'search_result', 'h3 a, .result a', 'a', href);
                }
            });
            // Priority 2: Inputs and textareas
            document.querySelectorAll('input, textarea, select').forEach(el => {
                const inputType = el.type || el.tagName.toLowerCase();
                const name = el.name || el.id || el.placeholder || inputType;
                const role = inputType === 'submit' || inputType === 'button' ? 'button'
                    : inputType === 'checkbox' || inputType === 'radio' ? inputType
                    : 'input';
                add(name, role, el.id ? '#' + el.id : el.name ? '[name="' + el.name + '"]' : inputType, el.tagName.toLowerCase(), '');
            });
            // Priority 3: Buttons
            document.querySelectorAll('button, [role="button"]').forEach(el => {
                const text = (el.textContent || '').trim().slice(0, 60);
                add(text || el.id || 'button', 'button', el.id ? '#' + el.id : 'button', 'button', '');
            });
            // Priority 4: Other links (nav etc.)
            document.querySelectorAll('a[href]').forEach(el => {
                const text = (el.textContent || '').trim().slice(0, 80);
                const href = el.href || '';
                if (text || href) {
                    add(text || href, 'link', 'a[href]', 'a', href);
                }
            });
            return results.slice(0, 50);
        }
        """
        try:
            elements = await page.evaluate(js_script)
            return elements if isinstance(elements, list) else []
        except Exception:
            return []

    def _atspi_snapshot(self) -> Dict[str, Any]:
        if self.atspi_provider is None:
            return {
                "available": False,
                "summary": "AT-SPI provider unavailable.",
                "applications": [],
            }
        try:
            tree = self.atspi_provider.get_accessibility_tree()
        except Exception as exc:
            return {
                "available": False,
                "summary": "AT-SPI unavailable: {}".format(exc),
                "applications": [],
            }

        # Extract app-level info
        applications = []
        for item in (tree if isinstance(tree, list) else []):
            if not isinstance(item, dict):
                continue
            applications.append(
                {
                    "name": str(item.get("name", "") or ""),
                    "role": str(item.get("role", "") or ""),
                }
            )

        # Extract deep interactive elements for richer context
        interactive_roles = {
            "push button", "toggle button", "menu item", "check menu item",
            "radio menu item", "combo box", "entry", "password text",
            "text", "spin button", "page tab", "check box", "radio button",
            "link", "table cell", "column header", "row header",
        }
        elements = []
        def _collect_interactive(nodes, depth=0):
            for node in (nodes or []):
                if not isinstance(node, dict):
                    continue
                role = (node.get("role") or "").lower()
                name = str(node.get("name") or "")
                if role in interactive_roles and name:
                    elements.append({
                        "name": name[:80],
                        "role": role,
                        "x": node.get("x", 0),
                        "y": node.get("y", 0),
                        "states": node.get("states", []),
                    })
                if depth < 6:
                    _collect_interactive(node.get("children", []), depth + 1)

        _collect_interactive(tree)

        summary_parts = ["Apps: " + ", ".join(a["name"] for a in applications if a["name"])]
        if elements:
            summary_parts.append("Interactive elements ({}): {}".format(
                len(elements),
                ", ".join(
                    "{}[{}]".format(e["name"][:30], e["role"]) for e in elements[:30]
                ),
            ))

        return {
            "available": True,
            "summary": "; ".join(summary_parts),
            "applications": applications,
            "elements": elements[:50],
        }
