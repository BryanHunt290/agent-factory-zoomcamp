"""A tiny demo project: a calculator module validated by protected acceptance tests."""

from __future__ import annotations

from ..state import TaskSpec
from .base import ProjectTemplate

ACCEPTANCE_TESTS = '''\
import unittest

from calculator import add, divide, multiply, subtract


class CalculatorTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_subtract(self):
        self.assertEqual(subtract(5, 3), 2)

    def test_multiply(self):
        self.assertEqual(multiply(4, 3), 12)

    def test_divide(self):
        self.assertEqual(divide(8, 2), 4)

    def test_divide_by_zero(self):
        with self.assertRaises(ValueError):
            divide(1, 0)


if __name__ == "__main__":
    unittest.main()
'''

LEGACY_MODULE = '''\
# Deprecated predecessor of calculator.py. Scheduled for removal.
def plus(a, b):
    return a + b
'''

CALCULATOR_TEMPLATE = ProjectTemplate(
    name="calculator",
    description="Implement a four-function calculator module that passes the acceptance tests.",
    task=TaskSpec(
        title="Calculator module",
        goal="Provide calculator.py with add, subtract, multiply and divide.",
        requirements=[
            "Functions take two numbers and return a number.",
            "divide raises ValueError on division by zero.",
            "All acceptance tests in tests/ pass.",
        ],
    ),
    scaffold={
        "README.md": "# Calculator\n\nBuilt by Agent Factory.\n",
        "legacy_calc.py": LEGACY_MODULE,
        "tests/__init__.py": "",
        "tests/test_calculator.py": ACCEPTANCE_TESTS,
    },
    protected_paths=("tests",),
)
