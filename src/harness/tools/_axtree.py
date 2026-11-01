"""Lane B's pure a11y-tree layer: Accessibility.getFullAXTree node lists in,
model-facing text + `[eN]` handle maps out. No I/O — everything here is unit-
testable offline (tests/test_browser_ax.py).

The interactive-role handle scheme and the select-context walk were verified
against live Chrome AX shapes (2026-07-19); the docstrings on each function
record what the tree looks like in practice.
"""

from __future__ import annotations

from typing import Any

# a11y-tree text cap (chars); a truncation note is appended past this.
_AX_MAX_CHARS = 15_000

# Roles that become clickable/typable `[eN]` handles.
_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "tab",
        "menuitem",
        "option",
        "listbox",
    }
)
# Roles rendered as context lines (no handle) so the tree reads as a page.
_STRUCTURAL_ROLES = frozenset(
    {
        "heading",
        "dialog",
        "alertdialog",
        "alert",
        "form",
        "navigation",
        "main",
        "banner",
    }
)
# Node properties surfaced as a `(state)` suffix when set.
_STATE_PROPS = ("checked", "disabled", "expanded", "selected")

# Roles that own dropdown options (native <select> renders as
# combobox → MenuListPopup → option*; ARIA listboxes as listbox → option*).
_SELECT_ROLES = frozenset({"combobox", "listbox"})


def _ax_value(node_value: Any) -> Any:
    """Unwrap an AXValue ({"value": ...}) or pass a bare value through."""
    if isinstance(node_value, dict):
        return node_value.get("value")
    return node_value


def _state_suffix(properties: list[dict]) -> str:
    """Render set boolean/tristate properties as ` (disabled) (checked)` etc."""
    parts: list[str] = []
    for prop in properties:
        name = prop.get("name")
        if name not in _STATE_PROPS:
            continue
        value = _ax_value(prop.get("value"))
        if value in (True, "true"):
            parts.append(name)
        elif value == "mixed":
            parts.append(f"{name}: mixed")
    return "".join(f" ({p})" for p in parts)


def format_ax_tree(
    nodes: list[dict], *, start: int = 1, max_chars: int = _AX_MAX_CHARS
) -> tuple[str, dict[str, int], int]:
    """Flatten an Accessibility.getFullAXTree node list to model-facing text +
    a handle map. PURE — no I/O.

    Interactive roles get an `[eN]` handle (N counting from `start`, in input
    order) mapped to the node's backendDOMNodeId; a node missing that id is
    skipped (no durable handle). Structural roles render as context lines
    without handles. Output is capped at `max_chars` with a `[tree truncated]`
    note. Returns (text, handle map, next start) so merged multi-frame
    snapshots continue the numbering across sections.

    NEVER render a node's AX ``value`` (a textbox holds typed identity refs —
    a leak); type-verify likewise reports only empty/non-empty.
    """
    lines: list[str] = []
    handles: dict[str, int] = {}
    counter = start - 1
    total = 0
    truncated = False

    for node in nodes:
        if node.get("ignored"):
            continue
        role = str(_ax_value(node.get("role")) or "")
        name = str(_ax_value(node.get("name")) or "").strip()
        state = _state_suffix(node.get("properties") or [])
        label = f' "{name}"' if name else ""

        if role in _INTERACTIVE_ROLES:
            backend = node.get("backendDOMNodeId")
            if backend is None:
                continue  # no durable handle — not actionable, drop it
            counter += 1
            handle = f"e{counter}"
            line = f"[{handle}] {role}{label}{state}"
        elif role in _STRUCTURAL_ROLES:
            line = f"{role}{label}{state}"
        else:
            continue

        if total + len(line) + 1 > max_chars:
            truncated = True
            break
        if role in _INTERACTIVE_ROLES:
            handles[handle] = int(backend)
        lines.append(line)
        total += len(line) + 1

    if truncated:
        lines.append("[tree truncated]")
    return "\n".join(lines), handles, counter + 1


def _ax_prop(node: dict, name: str) -> Any:
    for prop in node.get("properties") or []:
        if prop.get("name") == name:
            return _ax_value(prop.get("value"))
    return None


def find_select_context(
    nodes: list[dict], option_backend: int
) -> tuple[int, int] | str:
    """Locate a dropdown option inside its owning select. PURE — no I/O.

    Returns (select backendDOMNodeId, the option's index among the select's
    ENABLED options — i.e. the ArrowDown count after Home, since arrow
    navigation skips disabled options) or a refusal code:
    option-not-found / not-an-option / not-a-dropdown / multiselect /
    option-disabled. AX shape verified live 2026-07-19.
    """
    by_id = {n.get("nodeId"): n for n in nodes if n.get("nodeId") is not None}

    def role_of(n: dict) -> str:
        return str(_ax_value(n.get("role")) or "")

    option = next(
        (n for n in nodes if n.get("backendDOMNodeId") == option_backend), None
    )
    if option is None:
        return "option-not-found"
    if role_of(option) != "option":
        return "not-an-option"

    select = None
    node = option
    for _ in range(10):
        parent = by_id.get(node.get("parentId"))
        if parent is None:
            break
        if role_of(parent) in _SELECT_ROLES:
            select = parent
            break
        node = parent
    if select is None or select.get("backendDOMNodeId") is None:
        return "not-a-dropdown"
    if _ax_prop(select, "multiselectable") in (True, "true"):
        return "multiselect"

    # The select's options in document order (DFS through its subtree).
    enabled_backends: list[int] = []
    stack = list(reversed(select.get("childIds") or []))
    for _ in range(5000):
        if not stack:
            break
        child = by_id.get(stack.pop())
        if child is None:
            continue
        if role_of(child) == "option" and not child.get("ignored"):
            if _ax_prop(child, "disabled") not in (True, "true"):
                backend = child.get("backendDOMNodeId")
                if backend is not None:
                    enabled_backends.append(int(backend))
            continue  # options have no option descendants
        stack.extend(reversed(child.get("childIds") or []))

    if option_backend not in enabled_backends:
        return "option-disabled"
    return int(select["backendDOMNodeId"]), enabled_backends.index(option_backend)


def _quad_center(quad: list[float]) -> tuple[float, float]:
    """Center of a content quad [x1,y1,x2,y2,x3,y3,x4,y4] — mean of its corners."""
    x = (quad[0] + quad[2] + quad[4] + quad[6]) / 4
    y = (quad[1] + quad[3] + quad[5] + quad[7]) / 4
    return x, y
