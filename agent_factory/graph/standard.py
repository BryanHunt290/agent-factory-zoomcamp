"""The first Agent Factory graph: plan -> develop -> test -> review.

    START -> planner -> developer -> tester --pass--> reviewer --approved--> done
                           ^            |                 |
                           +----fail----+                 |
                           +-------changes_required-------+

Both repair edges carry a traversal budget, so the loop always terminates.
"""

from __future__ import annotations

from .definition import Edge, GraphDefinition, Node


class Outcome:
    PLANNED = "planned"
    IMPLEMENTED = "implemented"
    PASS = "pass"
    FAIL = "fail"
    APPROVED = "approved"
    CHANGES_REQUIRED = "changes_required"


def build_software_dev_graph(
    *,
    max_test_repairs: int = 3,
    max_review_repairs: int = 2,
    max_steps: int | None = None,
) -> GraphDefinition:
    """Build the standard software-development graph.

    ``max_test_repairs`` and ``max_review_repairs`` are run-wide budgets for
    the tester->developer and reviewer->developer edges respectively.
    """
    developer_visits = 1 + max_test_repairs + max_review_repairs
    if max_steps is None:
        # Exact upper bound on agent steps: one plan, every developer visit
        # followed by a test, and every permitted review.
        max_steps = 1 + 2 * developer_visits + (1 + max_review_repairs)

    return GraphDefinition(
        name="software-dev-v1",
        start="planner",
        max_steps=max_steps,
        nodes=(
            Node("planner", agent="planner", max_visits=1),
            Node("developer", agent="developer", max_visits=developer_visits),
            Node("tester", agent="tester"),
            Node("reviewer", agent="reviewer"),
            Node("done", terminal=True),
        ),
        edges=(
            Edge("planner", Outcome.PLANNED, "developer"),
            Edge("developer", Outcome.IMPLEMENTED, "tester"),
            Edge("tester", Outcome.PASS, "reviewer"),
            Edge("tester", Outcome.FAIL, "developer", max_traversals=max_test_repairs),
            Edge("reviewer", Outcome.APPROVED, "done"),
            Edge(
                "reviewer",
                Outcome.CHANGES_REQUIRED,
                "developer",
                max_traversals=max_review_repairs,
            ),
        ),
    )
