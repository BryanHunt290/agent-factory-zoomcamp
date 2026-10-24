import pytest

from agent_factory.graph import (
    Edge,
    GraphDefinition,
    GraphValidationError,
    InvalidTransitionError,
    Node,
    Outcome,
    build_software_dev_graph,
)


def _graph(nodes, edges, start="a", **kw):
    return GraphDefinition(name="g", start=start, nodes=tuple(nodes), edges=tuple(edges), **kw)


def test_standard_graph_is_valid_and_routes_as_specified():
    g = build_software_dev_graph()
    assert g.start == "planner"
    assert g.transition("planner", Outcome.PLANNED).target == "developer"
    assert g.transition("developer", Outcome.IMPLEMENTED).target == "tester"
    assert g.transition("tester", Outcome.PASS).target == "reviewer"
    assert g.transition("tester", Outcome.FAIL).target == "developer"
    assert g.transition("reviewer", Outcome.APPROVED).target == "done"
    assert g.transition("reviewer", Outcome.CHANGES_REQUIRED).target == "developer"
    assert g.node("done").terminal


def test_standard_graph_repair_edges_are_budgeted():
    g = build_software_dev_graph(max_test_repairs=4, max_review_repairs=1)
    assert g.transition("tester", Outcome.FAIL).max_traversals == 4
    assert g.transition("reviewer", Outcome.CHANGES_REQUIRED).max_traversals == 1
    assert g.node("developer").max_visits == 6


def test_undeclared_outcome_is_an_invalid_transition():
    g = build_software_dev_graph()
    with pytest.raises(InvalidTransitionError) as exc:
        g.transition("tester", "maybe")
    assert exc.value.node == "tester"
    assert exc.value.allowed == ("fail", "pass")


def test_graph_round_trips_through_dict():
    g = build_software_dev_graph()
    assert GraphDefinition.from_dict(g.to_dict()) == g


@pytest.mark.parametrize(
    "nodes, edges, start, message",
    [
        ([Node("a", agent="x"), Node("z", terminal=True)], [Edge("a", "ok", "nowhere")], "a", "unknown target"),
        ([Node("a", agent="x"), Node("z", terminal=True)], [Edge("a", "ok", "z")], "missing", "start node"),
        ([Node("a", agent="x")], [Edge("a", "ok", "a", max_traversals=1)], "a", "terminal node"),
        ([Node("a"), Node("z", terminal=True)], [Edge("a", "ok", "z")], "a", "must name an agent"),
        (
            [Node("a", agent="x"), Node("z", terminal=True)],
            [Edge("a", "ok", "z"), Edge("a", "ok", "a", max_traversals=1)],
            "a",
            "two edges for outcome",
        ),
        (
            [Node("a", agent="x"), Node("z", terminal=True)],
            [Edge("a", "ok", "z"), Edge("z", "again", "a")],
            "a",
            "must not have outgoing edges",
        ),
        (
            [Node("a", agent="x"), Node("b", agent="y"), Node("z", terminal=True)],
            [Edge("a", "ok", "z")],
            "a",
            "unreachable",
        ),
        (
            [Node("a", agent="x"), Node("b", agent="y"), Node("z", terminal=True)],
            [Edge("a", "ok", "z"), Edge("a", "loop", "b"), Edge("b", "back", "b", max_traversals=2)],
            "a",
            "never reach a terminal",
        ),
    ],
)
def test_invalid_graphs_are_rejected(nodes, edges, start, message):
    with pytest.raises(GraphValidationError, match=message):
        _graph(nodes, edges, start=start)


def test_unbudgeted_cycle_is_rejected():
    nodes = [Node("a", agent="x"), Node("b", agent="y"), Node("z", terminal=True)]
    edges = [Edge("a", "go", "b"), Edge("b", "retry", "a"), Edge("b", "ok", "z")]
    with pytest.raises(GraphValidationError, match="cycle without a retry budget"):
        _graph(nodes, edges)


def test_cycle_bounded_by_edge_or_node_budget_is_accepted():
    nodes = [Node("a", agent="x"), Node("b", agent="y"), Node("z", terminal=True)]
    _graph(nodes, [Edge("a", "go", "b"), Edge("b", "retry", "a", max_traversals=2), Edge("b", "ok", "z")])
    nodes_bounded = [Node("a", agent="x", max_visits=3), Node("b", agent="y"), Node("z", terminal=True)]
    _graph(nodes_bounded, [Edge("a", "go", "b"), Edge("b", "retry", "a"), Edge("b", "ok", "z")])
