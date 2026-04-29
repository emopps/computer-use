import pyatspi
import json

class ATSPIProvider:
    def __init__(self):
        try:
            self.registry = pyatspi.Registry
        except Exception as e:
            print(f"Error initializing AT-SPI: {e}")
            self.registry = None

    def get_accessibility_tree(self):
        if not self.registry:
            return []
        
        desktop = self.registry.getDesktop(0)
        tree = []
        for app in desktop:
            if app:
                parsed = self._parse_element(app)
                if parsed is not None:
                    tree.append(parsed)
        return tree

    # AT-SPI uses INT_MIN (-2147483648) for offscreen/hidden elements
    _INT_MIN = -2147483648

    def _parse_element(self, element, depth=0, max_depth=8):
        if depth > max_depth:
            return None
            
        try:
            name = element.name or ""
            role = element.getRoleName() or ""
            try:
                bbox = element.get_extents(pyatspi.DESKTOP_COORDS)
                # Filter out INT_MIN sentinel values (offscreen/hidden)
                has_valid_pos = (
                    bbox.x > self._INT_MIN + 1000
                    and bbox.y > self._INT_MIN + 1000
                )
                visible = has_valid_pos and bbox.width > 0 and bbox.height > 0
            except Exception:
                bbox = None
                has_valid_pos = False
                visible = False

            # Application/root elements may have invalid bbox but still have
            # visible children — always traverse their subtree.
            # For deeper elements with invalid bbox, still traverse children
            # (e.g. a panel container might report 0-size but hold buttons).
            # Named interactive elements (menu items, buttons, etc.) are kept
            # even if offscreen (collapsed menus report INT_MIN coordinates).
            _interactive_roles = {
                "push button", "toggle button", "menu item", "check menu item",
                "radio menu item", "combo box", "entry", "password text",
                "text", "spin button", "page tab", "check box", "radio button",
                "link", "table cell", "column header", "row header",
            }
            if not visible and depth > 1:
                is_interactive = role in _interactive_roles and name
                if not is_interactive and element.childCount == 0:
                    return None

            # Extract element states for richer context
            states = []
            try:
                state_set = element.getState()
                state_names = {
                    pyatspi.STATE_FOCUSED: "focused",
                    pyatspi.STATE_EDITABLE: "editable",
                    pyatspi.STATE_SELECTED: "selected",
                    pyatspi.STATE_CHECKED: "checked",
                    pyatspi.STATE_ENABLED: "enabled",
                    pyatspi.STATE_VISIBLE: "visible",
                    pyatspi.STATE_SHOWING: "showing",
                }
                for state_enum, state_name in state_names.items():
                    if state_set.contains(state_enum):
                        states.append(state_name)
            except Exception:
                pass

            data = {
                "name": name,
                "role": role,
                "x": bbox.x if bbox and visible else 0,
                "y": bbox.y if bbox and visible else 0,
                "width": bbox.width if bbox and visible else 0,
                "height": bbox.height if bbox and visible else 0,
                "states": states,
                "children": []
            }
            
            if depth < max_depth:
                for i in range(element.childCount):
                    try:
                        child = element.getChildAtIndex(i)
                        if child:
                            parsed_child = self._parse_element(child, depth + 1, max_depth)
                            if parsed_child:
                                data["children"].append(parsed_child)
                    except Exception:
                        continue
            return data
        except Exception:
            return None

    def find_element_by_query(self, query):
        """
        增强的 UI 元素查询，支持模糊匹配和关键字提取。
        """
        tree = self.get_accessibility_tree()
        query = query.lower()
        
        matches = []
        def search(nodes):
            for node in nodes:
                name = (node.get("name") or "").lower()
                role = (node.get("role") or "").lower()
                
                # 匹配逻辑：名称包含查询词，或者角色包含查询词
                if query in name or query in role:
                    matches.append(node)
                
                if node.get("children"):
                    search(node["children"])
        
        search(tree)
        # 按面积从小到大排序，优先返回更具体的子元素
        matches.sort(key=lambda x: x["width"] * x["height"])
        return matches
