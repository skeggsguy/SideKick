"""Browser click/select/type approvals name the element and its site, not a
bare [eN] handle (2026-11-01: a browser_delegate click approval read "e94").
The label comes from the parked run's own live tab, found by run id — no
Chrome here: the manager's session is a stub."""

from __future__ import annotations

import pytest

from harness.gateway.approval_text import approval_summary
from harness.gateway.preview import ApprovalPreviews, PreviewError
from harness.tools._browser_manager import BrowserSessionManager, manager_for_run


class StubSession:
    def __init__(self, labels, host="lens.google.com"):
        self.labels, self.host = labels, host

    def label_for_handle(self, handle):
        return self.labels.get(handle)

    def host_for_handle(self, handle):
        return self.host if handle in self.labels else None


def _manager(run_id, labels, host="lens.google.com"):
    m = BrowserSessionManager()
    m.run_id_fn = lambda: run_id
    m._session = StubSession(labels, host)
    return m


def test_manager_for_run_finds_the_run_driving_the_tab():
    a, b = _manager("run-a", {}), _manager("run-b", {})
    assert manager_for_run("run-b") is b
    assert manager_for_run("run-a") is a
    assert manager_for_run("nope") is None
    assert manager_for_run(None) is None


async def test_click_preview_names_the_element_and_site():
    keep = _manager("run-c", {"e94": 'button "Upload image"'})  # noqa: F841 (weakref)
    out = await ApprovalPreviews().resolve("browser_click", {"handle": "e94"}, run_id="run-c")
    assert out == {"will click": 'button "Upload image"', "on": "lens.google.com"}


async def test_select_preview_uses_its_own_verb():
    keep = _manager("run-s", {"e3": 'combobox "Country"'})  # noqa: F841
    out = await ApprovalPreviews().resolve(
        "browser_select", {"handle": "e3", "value": "UK"}, run_id="run-s"
    )
    assert out["will select"] == 'combobox "Country"'


async def test_stale_or_unknown_run_is_404_so_the_card_falls_back_to_args():
    keep = _manager("run-d", {"e1": 'link "Home"'})  # noqa: F841
    for args, run_id in (({"handle": "e7"}, "run-d"), ({"handle": "e1"}, "other"),
                         ({"handle": "e1"}, None)):
        with pytest.raises(PreviewError) as e:
            await ApprovalPreviews().resolve("browser_click", args, run_id=run_id)
        assert e.value.status == 404


async def test_type_preview_adds_the_field_it_types_into():
    keep = _manager("run-t", {"e2": 'textbox "Search"'})  # noqa: F841
    out = await ApprovalPreviews().resolve(
        "browser_type", {"handle": "e2", "text": "cats"}, run_id="run-t"
    )
    assert out == {"into": 'textbox "Search"', "on": "lens.google.com", "will type": "cats"}


def test_push_names_the_element_and_site():
    r = {"will click": 'button "Upload image"', "on": "lens.google.com"}
    assert approval_summary("browser_click", {"handle": "e94"}, r) == (
        'Click button "Upload image" on lens.google.com'
    )
    assert approval_summary("browser_select", {"handle": "e3"}, {"will select": 'combobox "C"'}) == (
        'Select combobox "C"'
    )


def test_push_without_a_preview_still_says_what_kind_of_action():
    assert approval_summary("browser_click", {"handle": "e94"}) == (
        "Click element e94 in the browser"
    )


async def test_delegate_binds_its_tab_to_the_parent_run_only_while_running():
    """The child parks approvals via the PARENT's approver, so the row carries
    the parent's run id — that is what the tab must answer to, and only while
    the child runs (a left-open blocked tab must not answer a later one)."""
    from harness.composition import _browser_child_factory
    from harness.core.types import LLMResponse
    from harness.tools.browser_delegate import BrowserDelegateTool
    from test_agent_loop import ScriptedProvider

    real = _browser_child_factory(
        ScriptedProvider([LLMResponse(text="outcome: data\ndone")]),
        ScriptedProvider([]), None, None, None, lambda: None,
    )
    seen = {}

    def factory(envelope):
        child, manager = real(envelope)
        run_turn = child.run_turn

        async def spy(*a, **kw):
            seen["during"] = manager_for_run("parent-1") is manager
            return await run_turn(*a, **kw)

        child.run_turn = spy
        seen["manager"] = manager
        return child, manager

    tool = BrowserDelegateTool(factory, lambda: "parent-1")
    await tool.run({"origin": "example.com", "task": "look something up"})
    assert seen["during"] is True
    assert manager_for_run("parent-1") is None


def test_two_delegates_in_one_run_are_ambiguous_so_no_label():
    keep = (_manager("run-p", {"e1": 'link "A"'}), _manager("run-p", {"e1": 'link "B"'}))  # noqa: F841
    assert manager_for_run("run-p") is None
