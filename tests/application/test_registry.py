import pytest
from arkod.application.registry import Registry, UnknownOperationError


class FakeTool:
    def __init__(self, name):
        self.name = name


def test_duplicate_keeps_original_tool():
    registry = Registry()
    first, second = FakeTool("t"), FakeTool("t")
    registry.register(first)

    with pytest.raises(ValueError):
        registry.register(second)

    assert registry.get("t") is first


def test_unknown_operation_carries_name():
    with pytest.raises(UnknownOperationError) as exc:
        Registry().get("nope")
    assert exc.value.code == "unknown_operation"
    assert exc.value.operation == "nope"


def test_empty_registry_rejects_everything():
    registry = Registry()
    for name in ["", " ", "compare_teams"]:
        with pytest.raises(UnknownOperationError):
            registry.get(name)


def test_lookup_is_case_sensitive():
    registry = Registry()
    registry.register(FakeTool("compare_teams"))
    with pytest.raises(UnknownOperationError):
        registry.get("Compare_Teams")


def test_none_operation_is_unknown():
    with pytest.raises(UnknownOperationError):
        Registry().get(None)


def test_registries_are_isolated():
    a, b = Registry(), Registry()
    a.register(FakeTool("t"))
    with pytest.raises(UnknownOperationError):
        b.get("t")


def test_two_tools_registered_independently():
    registry = Registry()
    t1, t2 = FakeTool("a"), FakeTool("b")
    registry.register(t1)
    registry.register(t2)
    assert registry.get("a") is t1
    assert registry.get("b") is t2