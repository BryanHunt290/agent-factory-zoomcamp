"""Deterministic demonstration scenarios for the calculator template.

Each scenario is a FakeProvider script: the "model" answers are fixed, but
everything else is real. Files are written to a real workspace, the real
acceptance tests run in a subprocess, and routing follows the real results.
"""

from __future__ import annotations

from .graph import Outcome
from .providers import FakeProvider

PLAN = {
    "summary": "Implement calculator.py with four arithmetic functions.",
    "steps": [
        "Create calculator.py exposing add, subtract, multiply, divide.",
        "Raise ValueError from divide when the divisor is zero.",
        "Run the acceptance tests in tests/.",
    ],
    "files": ["calculator.py"],
    "acceptance_criteria": [
        "tests/test_calculator.py passes",
        "divide(1, 0) raises ValueError",
    ],
}

CALC_BUGGY = '''\
def add(a, b):
    return a + b


def subtract(a, b):
    return a + b  # BUG: wrong operator


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b
'''

CALC_CORRECT = CALC_BUGGY.replace("return a + b  # BUG: wrong operator", "return a - b")

CALC_POLISHED = '''\
"""Four-function calculator."""

Number = int | float


def add(a: Number, b: Number) -> Number:
    """Return a + b."""
    return a + b


def subtract(a: Number, b: Number) -> Number:
    """Return a - b."""
    return a - b


def multiply(a: Number, b: Number) -> Number:
    """Return a * b."""
    return a * b


def divide(a: Number, b: Number) -> float:
    """Return a / b; raise ValueError when b is zero."""
    if b == 0:
        raise ValueError("division by zero")
    return a / b
'''


def _dev(summary: str, code: str, delete: list[str] | None = None) -> dict:
    out = {"summary": summary, "files": {"calculator.py": code}}
    if delete:
        out["delete"] = delete
    return out


DEV_BUGGY = _dev("Initial implementation of calculator.py.", CALC_BUGGY)
DEV_FIX = _dev("Fix subtract: use '-' instead of '+'.", CALC_POLISHED)
DEV_CORRECT_PLAIN = _dev("Implementation of calculator.py.", CALC_CORRECT)
DEV_FIX_PLAIN = _dev("Fix subtract: use '-' instead of '+'.", CALC_CORRECT)
DEV_POLISH = _dev("Add module docstring, docstrings and type hints.", CALC_POLISHED)
DEV_WITH_DELETE = _dev(
    "Implement calculator.py and remove the deprecated legacy module.",
    CALC_POLISHED,
    delete=["legacy_calc.py"],
)

APPROVE = {
    "verdict": Outcome.APPROVED,
    "summary": "Correct, tested, documented.",
    "comments": [],
}
REQUEST_CHANGES = {
    "verdict": Outcome.CHANGES_REQUIRED,
    "summary": "Works, but public functions are undocumented.",
    "comments": ["Add a module docstring.", "Add docstrings and type hints to each function."],
}

#: scenario name -> (description, script, repeat_last)
SCENARIOS: dict[str, tuple[str, dict[str, list], bool]] = {
    "repair": (
        "planner -> developer (bug) -> tester FAIL -> developer repair -> tester PASS -> reviewer APPROVE -> done",
        {"planner": [PLAN], "developer": [DEV_BUGGY, DEV_FIX], "reviewer": [APPROVE]},
        False,
    ),
    "review": (
        "tests pass first time; reviewer requests changes; developer polishes; reviewer approves",
        {"planner": [PLAN], "developer": [DEV_CORRECT_PLAIN, DEV_POLISH], "reviewer": [REQUEST_CHANGES, APPROVE]},
        False,
    ),
    "full": (
        "test failure repair, then reviewer rejection repair, then approval",
        {
            "planner": [PLAN],
            "developer": [DEV_BUGGY, DEV_FIX_PLAIN, DEV_POLISH],
            "reviewer": [REQUEST_CHANGES, APPROVE],
        },
        False,
    ),
    "exhaust": (
        "developer never fixes the bug; the tester->developer retry budget runs out",
        {"planner": [PLAN], "developer": [DEV_BUGGY], "reviewer": [APPROVE]},
        True,
    ),
    "approval": (
        "developer asks to delete a file; the run pauses for human approval",
        {"planner": [PLAN], "developer": [DEV_WITH_DELETE], "reviewer": [APPROVE]},
        False,
    ),
}


def scenario_provider(name: str) -> FakeProvider:
    try:
        _, script, repeat_last = SCENARIOS[name]
    except KeyError:
        raise ValueError(f"unknown scenario {name!r}; choose from {sorted(SCENARIOS)}") from None
    return FakeProvider(script, repeat_last=repeat_last)
