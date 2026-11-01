"""Pure unit tests for format_ax_tree — Accessibility.getFullAXTree node lists
to model-facing text + a handle map. No I/O, no browser."""

from __future__ import annotations

from harness.tools._axtree import _AX_MAX_CHARS, format_ax_tree


def _node(role, name="", *, backend=None, ignored=False, properties=None):
    node: dict = {"role": {"value": role}, "name": {"value": name}, "ignored": ignored}
    if backend is not None:
        node["backendDOMNodeId"] = backend
    if properties is not None:
        node["properties"] = properties
    return node


def test_interactive_role_gets_handle_and_backend_mapping():
    text, handles, _ = format_ax_tree(
        [
            _node("button", "Reserve now", backend=42),
            _node("link", "Home", backend=7),
        ]
    )
    assert handles == {"e1": 42, "e2": 7}
    assert '[e1] button "Reserve now"' in text
    assert '[e2] link "Home"' in text


def test_ignored_nodes_skipped():
    text, handles, _ = format_ax_tree(
        [
            _node("button", "Visible", backend=1),
            _node("button", "Hidden", backend=2, ignored=True),
        ]
    )
    assert handles == {"e1": 1}
    assert "Hidden" not in text
    assert "Visible" in text


def test_structural_roles_rendered_without_handles():
    text, handles, _ = format_ax_tree(
        [
            _node("heading", "Choose a time"),
            _node("navigation", "Main"),
            _node("button", "Go", backend=9),
        ]
    )
    assert handles == {"e1": 9}  # only the button
    assert 'heading "Choose a time"' in text
    assert 'navigation "Main"' in text
    # structural lines carry no [eN] handle
    assert "[e1] heading" not in text
    assert "[e" not in text.split("\n")[0]  # heading line first, no handle


def test_missing_backend_dom_node_id_skipped_from_handles():
    text, handles, _ = format_ax_tree(
        [
            _node("button", "Has id", backend=5),
            _node("button", "No id"),  # no backendDOMNodeId
        ]
    )
    assert handles == {"e1": 5}
    assert "No id" not in text  # skipped entirely (not actionable)


def test_state_suffix_rendered():
    text, handles, _ = format_ax_tree(
        [
            _node(
                "button",
                "Submit",
                backend=1,
                properties=[{"name": "disabled", "value": {"value": True}}],
            ),
            _node(
                "checkbox",
                "Agree",
                backend=2,
                properties=[{"name": "checked", "value": {"value": True}}],
            ),
            _node(
                "checkbox",
                "Partial",
                backend=3,
                properties=[{"name": "checked", "value": {"value": "mixed"}}],
            ),
        ]
    )
    assert '[e1] button "Submit" (disabled)' in text
    assert '[e2] checkbox "Agree" (checked)' in text
    assert '[e3] checkbox "Partial" (checked: mixed)' in text


def test_false_state_props_not_rendered():
    text, _, _ = format_ax_tree(
        [
            _node(
                "button",
                "Enabled",
                backend=1,
                properties=[{"name": "disabled", "value": {"value": False}}],
            ),
        ]
    )
    assert "(disabled)" not in text


def test_handle_numbering_stable_in_input_order():
    text, handles, _ = format_ax_tree(
        [
            _node("button", "First", backend=100),
            _node("heading", "Section"),  # no handle, does not consume a number
            _node("link", "Second", backend=200),
            _node("textbox", "Third", backend=300),
        ]
    )
    assert handles == {"e1": 100, "e2": 200, "e3": 300}
    assert '[e1] button "First"' in text
    assert '[e2] link "Second"' in text
    assert '[e3] textbox "Third"' in text


def test_char_cap_appends_truncation_note():
    # Many interactive nodes with long names to blow past the cap.
    nodes = [_node("button", "X" * 200, backend=i) for i in range(200)]
    text, handles, _ = format_ax_tree(nodes)
    assert "[tree truncated]" in text
    assert len(text) <= _AX_MAX_CHARS + len("[tree truncated]") + 2
    # Only the rendered handles are mapped (truncation stops mid-list).
    assert len(handles) < 200


def test_unknown_roles_dropped():
    text, handles, _ = format_ax_tree(
        [
            _node("generic", "wrapper", backend=1),
            _node("StaticText", "some text", backend=2),
            _node("button", "Real", backend=3),
        ]
    )
    assert handles == {"e1": 3}
    assert "wrapper" not in text
    assert "some text" not in text


def test_start_continues_numbering_and_returns_next():
    text, handles, nxt = format_ax_tree(
        [_node("button", "A", backend=1), _node("link", "B", backend=2)],
        start=5,
    )
    assert handles == {"e5": 1, "e6": 2}
    assert "[e5]" in text and "[e6]" in text
    assert nxt == 7


def test_max_chars_param_truncates():
    nodes = [_node("button", "Y" * 50, backend=i) for i in range(20)]
    text, handles, _ = format_ax_tree(nodes, max_chars=200)
    assert "[tree truncated]" in text
    assert len(handles) < 20


# --- find_select_context: option → owning select, enabled-index --------------


def _sel_node(node_id, role, *, backend=None, parent=None, children=(),
              disabled=False, multiselectable=False, ignored=False):
    n: dict = {
        "nodeId": node_id,
        "role": {"value": role},
        "ignored": ignored,
        "childIds": list(children),
    }
    if backend is not None:
        n["backendDOMNodeId"] = backend
    if parent is not None:
        n["parentId"] = parent
    props = []
    if disabled:
        props.append({"name": "disabled", "value": {"value": True}})
    if multiselectable:
        props.append({"name": "multiselectable", "value": {"value": True}})
    if props:
        n["properties"] = props
    return n


def _native_select(*, multiselectable=False, popup=True):
    """combobox(100) → MenuListPopup → options 1(sel) 2 3(disabled) 4."""
    mid = ["popup"] if popup else ["o1", "o2", "o3", "o4"]
    nodes = [
        _sel_node("root", "RootWebArea", children=["cb"]),
        _sel_node("cb", "combobox", backend=100, parent="root",
                  children=mid if popup else ["o1", "o2", "o3", "o4"],
                  multiselectable=multiselectable),
    ]
    if popup:
        nodes.append(_sel_node("popup", "MenuListPopup", backend=101,
                               parent="cb", children=["o1", "o2", "o3", "o4"]))
    parent = "popup" if popup else "cb"
    nodes += [
        _sel_node("o1", "option", backend=201, parent=parent),
        _sel_node("o2", "option", backend=202, parent=parent),
        _sel_node("o3", "option", backend=203, parent=parent, disabled=True),
        _sel_node("o4", "option", backend=204, parent=parent),
    ]
    return nodes


def test_select_context_enabled_index_skips_disabled():
    from harness.tools._axtree import find_select_context

    # 204 is the 4th option but the 3rd ENABLED one (203 disabled) → index 2.
    assert find_select_context(_native_select(), 204) == (100, 2)
    assert find_select_context(_native_select(), 201) == (100, 0)


def test_select_context_without_popup_layer():
    from harness.tools._axtree import find_select_context

    # ARIA listbox shape: options directly under the container.
    assert find_select_context(_native_select(popup=False), 202) == (100, 1)


def test_select_context_refusal_codes():
    from harness.tools._axtree import find_select_context

    nodes = _native_select()
    assert find_select_context(nodes, 999) == "option-not-found"
    assert find_select_context(nodes, 100) == "not-an-option"  # the combobox
    assert find_select_context(nodes, 203) == "option-disabled"
    assert (
        find_select_context(_native_select(multiselectable=True), 202)
        == "multiselect"
    )
    # An option with no combobox/listbox ancestor.
    orphan = [
        _sel_node("root", "RootWebArea", children=["o1"]),
        _sel_node("o1", "option", backend=301, parent="root"),
    ]
    assert find_select_context(orphan, 301) == "not-a-dropdown"


def test_select_context_ignored_options_excluded():
    from harness.tools._axtree import find_select_context

    nodes = _native_select()
    # Mark option 202 ignored: it drops out of the enabled ordering, shifting
    # 204 to enabled index 1.
    next(n for n in nodes if n.get("backendDOMNodeId") == 202)["ignored"] = True
    assert find_select_context(nodes, 204) == (100, 1)
