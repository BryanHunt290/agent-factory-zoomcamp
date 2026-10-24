"""Tool execution semantics: every built-in declares its class; dangerous tools must declare."""

from datetime import timedelta

import pytest

from agent_factory.safety import (
    CONSERVATIVE_DEFAULT,
    DANGEROUS_CATEGORIES,
    ActionCategory,
    EffectClass,
    ExecutionSemantics,
    ExecutionSemanticsError,
    Idempotency,
    check_tool_semantics,
    semantics_of,
)
from agent_factory.tools import Tool, ToolRegistry, ToolResult, default_registry

#: The documented classification of every built-in tool (docs/safety.md#execution-semantics).
BUILT_IN = {
    "list_files": (EffectClass.READ_ONLY, None),
    "read_file": (EffectClass.READ_ONLY, None),
    "run_tests": (EffectClass.READ_ONLY, None),
    "write_file": (EffectClass.IDEMPOTENT, Idempotency.NATURAL),
    "delete_file": (EffectClass.IDEMPOTENT, Idempotency.NATURAL),
    "git_commit": (EffectClass.IDEMPOTENT, Idempotency.NATURAL),
}


def test_every_built_in_tool_declares_its_semantics_explicitly():
    registry = default_registry()
    assert set(registry.names()) == set(BUILT_IN)
    for name, (effect, idempotency) in BUILT_IN.items():
        declared = type(registry.get(name)).execution
        assert declared is not None, f"{name} relies on the default"
        assert (declared.effect, declared.idempotency) == (effect, idempotency), name
        assert declared.rationale, f"{name} must say why it has this class"


def test_read_tools_do_not_record_content_for_replay():
    registry = default_registry()
    assert registry.get("read_file").execution.replayable_result is False  # file content is not a ledger record
    assert registry.get("run_tests").execution.replayable_result is False


def _tool(category, execution=None):
    class Fixture(Tool):
        name = "fixture"

        def run(self, workspace, **params):  # pragma: no cover - not run
            return ToolResult()

    Fixture.category = category
    Fixture.execution = execution
    return Fixture()


@pytest.mark.parametrize("category", sorted(DANGEROUS_CATEGORIES))
def test_undeclared_dangerous_tools_are_refused(category):
    with pytest.raises(ExecutionSemanticsError, match="must declare"):
        check_tool_semantics(_tool(category))
    with pytest.raises(ExecutionSemanticsError):
        ToolRegistry([_tool(category)])


@pytest.mark.parametrize("category", sorted(DANGEROUS_CATEGORIES))
def test_a_dangerous_tool_cannot_be_declared_read_only(category):
    with pytest.raises(ExecutionSemanticsError, match="read-only"):
        ToolRegistry([_tool(category, ExecutionSemantics(effect=EffectClass.READ_ONLY))])


def test_undeclared_ordinary_tools_get_the_most_conservative_class():
    tool = _tool(ActionCategory.WRITE_WORKSPACE)
    ToolRegistry([tool])  # allowed, but treated as class C
    assert semantics_of(tool) is CONSERVATIVE_DEFAULT
    assert CONSERVATIVE_DEFAULT.effect is EffectClass.NON_IDEMPOTENT and CONSERVATIVE_DEFAULT.max_attempts == 1


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"effect": EffectClass.NON_IDEMPOTENT, "max_attempts": 2}, "exactly one"),
        ({"effect": EffectClass.IDEMPOTENT}, "natural or keyed"),
        ({"effect": EffectClass.READ_ONLY, "idempotency": Idempotency.NATURAL}, "natural or keyed"),
        ({"effect": EffectClass.IDEMPOTENT, "idempotency": Idempotency.KEYED}, "key_retention"),
        (
            {"effect": EffectClass.IDEMPOTENT, "idempotency": Idempotency.NATURAL, "key_retention": timedelta(hours=1)},
            "key_retention",
        ),
        ({"effect": EffectClass.READ_ONLY, "max_attempts": 0}, "between 1"),
        ({"effect": "maybe"}, "unknown effect"),
    ],
)
def test_inconsistent_declarations_are_refused(kwargs, match):
    with pytest.raises(ExecutionSemanticsError, match=match):
        ExecutionSemantics(**kwargs)


def test_provider_windows_are_declared_per_tool():
    keyed = ExecutionSemantics(
        effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.KEYED, key_retention=timedelta(hours=24), max_attempts=3
    )
    assert keyed.key_retention_s == 86_400 and keyed.describe() == "idempotent (keyed)"
