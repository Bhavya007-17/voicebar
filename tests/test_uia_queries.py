"""Role-filtered UIA queries use in-process fakes, never desktop controls."""

from types import SimpleNamespace

import pytest

from voicebar.executor import uia
from voicebar.executor.common import ExecutionError

MODULE = SimpleNamespace(UIA_ButtonControlTypeId=50000, UIA_EditControlTypeId=50004,
                         UIA_ControlTypePropertyId=30003, TreeScope_Subtree=7)


class Element:
    def __init__(self, name, *, role=50000, automation_id="", enabled=True):
        self.CurrentName = name
        self.CurrentControlType = role
        self.CurrentLocalizedControlType = "localized role"
        self.CurrentAutomationId = automation_id
        self.CurrentIsEnabled = enabled

    def GetCurrentPropertyValue(self, _property):
        raise AssertionError("candidate enumeration must not query action patterns")


class Collection:
    def __init__(self, elements):
        self.elements = elements
        self.read = []

    @property
    def Length(self):
        return len(self.elements)

    def GetElement(self, index):
        self.read.append(index)
        return self.elements[index]


class Root:
    def __init__(self, elements):
        self.collection = Collection(elements)
        self.queries = []

    def FindAll(self, scope, condition):
        self.queries.append((scope, condition))
        return self.collection


class Client:
    ControlViewCondition = ("control-view",)

    def __init__(self):
        self.properties = []

    def CreatePropertyCondition(self, property_id, value):
        self.properties.append((property_id, value))
        return ("property", property_id, value)

    def CreateAndCondition(self, first, second):
        return ("and", first, second)


def query(elements, selector, *, check=lambda: None):
    automation = object.__new__(uia.Automation)
    automation.timeout = 8
    root, client = Root(elements), Client()
    rows, returned = automation._controls(root, client, MODULE, selector, check)
    return rows, returned, root, client


def test_query_combines_control_view_with_canonical_role_and_subtree():
    element = Element("Create", automation_id="create")
    rows, elements, root, client = query([element], {"role": "  BUTTON  "})

    assert client.properties == [(MODULE.UIA_ControlTypePropertyId, 50000)]
    assert root.queries == [(MODULE.TreeScope_Subtree, (
        "and", client.ControlViewCondition, ("property", 30003, 50000)))]
    assert rows == [{"name": "Create", "role": "button", "automation_id": "create",
                     "enabled": True}]
    assert elements == [element]


def test_name_and_id_are_not_native_filters_and_preserve_duplicate_matches():
    elements = [Element("  New   CHAT ", automation_id=" Prompt "),
                Element("unrelated", automation_id="other"),
                Element("new chat", automation_id="prompt")]
    selector = {"name": "new chat", "automation_id": "prompt"}
    rows, returned, root, client = query(elements, selector)

    assert client.properties == []
    assert root.queries == [(MODULE.TreeScope_Subtree, client.ControlViewCondition)]
    assert returned == elements
    assert [row["name"] for row in rows] == [element.CurrentName for element in elements]
    assert root.collection.read == [0, 1, 2]
    with pytest.raises(uia.ControlMatchError) as caught:
        uia.unique(rows, selector)
    assert len(caught.value.candidates) == 2


def test_enumeration_finishes_before_a_unique_match_can_be_selected():
    elements = [Element("target"), Element("other"), Element("last")]
    rows, returned, root, _client = query(elements, {"name": "target"})

    assert root.collection.read == [0, 1, 2]
    assert uia.unique(rows, {"name": "TARGET"}) is rows[0]
    assert returned[rows.index(rows[0])] is elements[0]


@pytest.mark.parametrize("selector", [{"name": "target"}, {"role": "unknown role"}])
def test_name_only_and_unknown_role_fall_back_to_complete_control_view(selector):
    rows, _elements, root, client = query([Element("target")], selector)

    assert client.properties == []
    assert root.queries == [(MODULE.TreeScope_Subtree, client.ControlViewCondition)]
    assert len(rows) == 1
    if "role" in selector:
        with pytest.raises(uia.ControlMatchError):
            uia.unique(rows, selector)


def test_query_rejects_oversized_collection_before_reading_elements(monkeypatch):
    monkeypatch.setattr(uia, "MAX_CONTROLS", 2)
    automation = object.__new__(uia.Automation)
    automation.timeout = 8
    root = Root([Element("one"), Element("two"), Element("three")])

    with pytest.raises(ExecutionError, match="too large"):
        automation._controls(root, Client(), MODULE, {}, lambda: None)
    assert root.collection.read == []


def test_query_accepts_collection_at_control_bound(monkeypatch):
    monkeypatch.setattr(uia, "MAX_CONTROLS", 2)
    rows, _elements, root, _client = query([Element("one"), Element("two")], {})

    assert len(rows) == 2
    assert root.collection.read == [0, 1]


def test_revocation_before_query_prevents_provider_call():
    automation = object.__new__(uia.Automation)
    automation.timeout = 8
    root = Root([Element("target")])

    def revoked():
        raise ExecutionError("request revoked")

    with pytest.raises(ExecutionError, match="revoked"):
        automation._controls(root, Client(), MODULE, {}, revoked)
    assert root.queries == []


def test_revocation_during_enumeration_discards_partial_result():
    automation = object.__new__(uia.Automation)
    automation.timeout = 8
    root = Root([Element("target"), Element("duplicate")])

    def revoke_after_first():
        if root.collection.read:
            raise ExecutionError("request revoked")

    with pytest.raises(ExecutionError, match="revoked"):
        automation._controls(root, Client(), MODULE, {}, revoke_after_first)
    assert root.collection.read == [0]


def test_deadline_after_provider_query_prevents_enumeration(monkeypatch):
    clock = iter([0, 0, 8])
    monkeypatch.setattr(uia.time, "monotonic", lambda: next(clock))
    automation = object.__new__(uia.Automation)
    automation.timeout = 8
    root = Root([Element("target")])

    with pytest.raises(ExecutionError, match="time limit"):
        automation._controls(root, Client(), MODULE, {}, lambda: None)
    assert len(root.queries) == 1
    assert root.collection.read == []
