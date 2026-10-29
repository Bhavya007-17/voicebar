"""Browser chrome ancestry queries use fake COM elements, never real windows."""

from types import SimpleNamespace

import pytest

from voicebar.executor import uia
from voicebar.executor.common import ExecutionError

DOCUMENT = 50030
PANE = 50033


def element(key, parent=None, control_type=PANE):
    return SimpleNamespace(key=key, parent=parent, CurrentControlType=control_type)


def row(name):
    return {"name": name, "role": "tabitem", "automation_id": "", "enabled": True}


def query(monkeypatch, elements, *, root=None, get_parent=None, check=lambda: None, timeout=8):
    root = root or element("root")
    walker = SimpleNamespace(GetParentElement=get_parent or (lambda node: node.parent))
    client = SimpleNamespace(ControlViewWalker=walker,
                             CompareElements=lambda left, right: left.key == right.key)
    module = SimpleNamespace(UIA_DocumentControlTypeId=DOCUMENT)
    automation = object.__new__(uia.Automation)
    automation.timeout = timeout

    def controls(actual_root, actual_client, actual_module, selector, bounded):
        assert actual_root is root and actual_client is client and actual_module is module
        assert selector == {"role": "tabitem"}
        bounded()
        return [row(node.key) for node in elements], elements

    monkeypatch.setattr(automation, "_controls", controls)
    return automation._browser_tab_controls(root, client, module, check)


def test_only_tab_strip_items_survive_document_ancestry(monkeypatch):
    root = element("root")
    strip = element("strip", root)
    document = element("document", root, DOCUMENT)
    page_group = element("page-group", document)
    real = element("Real browser tab", strip)
    aria = element("Page ARIA tab", page_group)
    assert query(monkeypatch, [real, aria], root=root) == [row("Real browser tab")]


def test_com_element_comparison_accepts_a_distinct_proxy_for_same_root(monkeypatch):
    root = element("root")
    other_proxy = element("root")
    assert other_proxy is not root
    assert query(monkeypatch, [element("Tab", other_proxy)], root=root) == [row("Tab")]


def test_matching_document_root_is_still_excluded(monkeypatch):
    root = element("root", control_type=DOCUMENT)
    assert query(monkeypatch, [element("ARIA tab", root)], root=root) == []


def test_detached_candidate_rejects_entire_query_not_partial_inventory(monkeypatch):
    root = element("root")
    with pytest.raises(uia.ControlTreeChanged, match="original window"):
        query(monkeypatch, [element("Good tab", root), element("Detached")], root=root)


def test_candidate_moved_under_other_window_is_not_a_browser_tab(monkeypatch):
    other_root = element("other-window")
    with pytest.raises(uia.ControlTreeChanged, match="original window"):
        query(monkeypatch, [element("Moved tab", other_root)])


def test_cyclic_ancestry_is_bounded(monkeypatch):
    cycle = element("cycle")
    cycle.parent = cycle
    calls = []
    def parent(node):
        calls.append(node.key)
        return node.parent
    with pytest.raises(ExecutionError, match="deep or cyclic"):
        query(monkeypatch, [element("Tab", cycle)], get_parent=parent)
    assert len(calls) == uia.MAX_BROWSER_ANCESTORS


def test_revocation_during_parent_query_prevents_returning_any_rows(monkeypatch):
    revoked = [False]
    root = element("root")
    def parent(node):
        revoked[0] = True
        return node.parent
    def check():
        if revoked[0]:
            raise ExecutionError("revoked")
    with pytest.raises(ExecutionError, match="revoked"):
        query(monkeypatch, [element("Tab", root)], root=root, get_parent=parent, check=check)


def test_slow_parent_query_exceeding_budget_rejects_results(monkeypatch):
    now = [0]
    monkeypatch.setattr(uia.time, "monotonic", lambda: now[0])
    root = element("root")
    def parent(node):
        now[0] = 8
        return node.parent
    with pytest.raises(ExecutionError, match="time limit"):
        query(monkeypatch, [element("Tab", root)], root=root, get_parent=parent)


def test_browser_tabs_public_api_has_its_own_worker_request(monkeypatch):
    automation = object.__new__(uia.Automation)
    calls = []
    monkeypatch.setattr(automation, "_request", lambda *args: calls.append(args) or [])
    assert automation.browser_tabs(123) == []
    assert calls == [("browser_tabs", 123)]
