"""Graph definition: the static shape of a workflow.

A graph is a set of named nodes connected by edges labelled with *outcomes*.
An agent node finishes by reporting an outcome; the executor follows the edge
carrying that label. Terminal nodes end the run.

Definitions are immutable and validated on construction, so a graph that
references unknown nodes, cannot reach a terminal node, or contains a cycle
without a retry budget is rejected before anything runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


class GraphValidationError(ValueError):
    """The graph definition is structurally invalid."""


class InvalidTransitionError(RuntimeError):
    """A node reported an outcome that has no outgoing edge."""

    def __init__(self, node: str, outcome: str, allowed: Iterable[str]) -> None:
        self.node = node
        self.outcome = outcome
        self.allowed = tuple(sorted(allowed))
        super().__init__(
            f"node {node!r} reported outcome {outcome!r}; allowed outcomes: {list(self.allowed)}"
        )


@dataclass(frozen=True)
class Node:
    """A step in the graph.

    Non-terminal nodes name the agent that runs them. ``max_visits`` caps how
    often the node may be entered during one run.
    """

    name: str
    agent: str | None = None
    terminal: bool = False
    max_visits: int | None = None


@dataclass(frozen=True)
class Edge:
    """A transition taken when ``source`` reports ``outcome``.

    ``max_traversals`` bounds how often the edge may be followed in one run;
    repair edges (tester -> developer, reviewer -> developer) use it.
    """

    source: str
    outcome: str
    target: str
    max_traversals: int | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.outcome}->{self.target}"


@dataclass(frozen=True)
class GraphDefinition:
    name: str
    start: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    max_steps: int = 50

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        self._validate()
        object.__setattr__(self, "_by_name", {n.name: n for n in self.nodes})
        out: dict[str, dict[str, Edge]] = {n.name: {} for n in self.nodes}
        for e in self.edges:
            out[e.source][e.outcome] = e
        object.__setattr__(self, "_out", out)

    # ------------------------------------------------------------------ lookups

    def node(self, name: str) -> Node:
        try:
            return self._by_name[name]  # type: ignore[attr-defined]
        except KeyError:
            raise KeyError(f"graph {self.name!r} has no node {name!r}") from None

    def outgoing(self, node: str) -> Mapping[str, Edge]:
        return dict(self._out[node])  # type: ignore[attr-defined]

    def transition(self, node: str, outcome: str) -> Edge:
        """Return the edge for ``outcome`` or raise :class:`InvalidTransitionError`."""
        edges = self._out.get(node, {})  # type: ignore[attr-defined]
        if outcome not in edges:
            raise InvalidTransitionError(node, outcome, edges)
        return edges[outcome]

    @property
    def agent_names(self) -> set[str]:
        return {n.agent for n in self.nodes if n.agent}

    # ------------------------------------------------------------ serialization

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start": self.start,
            "max_steps": self.max_steps,
            "nodes": [
                {"name": n.name, "agent": n.agent, "terminal": n.terminal, "max_visits": n.max_visits}
                for n in self.nodes
            ],
            "edges": [
                {
                    "source": e.source,
                    "outcome": e.outcome,
                    "target": e.target,
                    "max_traversals": e.max_traversals,
                }
                for e in self.edges
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GraphDefinition:
        return cls(
            name=data["name"],
            start=data["start"],
            max_steps=data.get("max_steps", 50),
            nodes=tuple(Node(**n) for n in data["nodes"]),
            edges=tuple(Edge(**e) for e in data["edges"]),
        )

    # --------------------------------------------------------------- validation

    def _validate(self) -> None:
        errors: list[str] = []
        if not self.name:
            errors.append("graph name must be non-empty")
        if self.max_steps < 1:
            errors.append("max_steps must be >= 1")

        names = [n.name for n in self.nodes]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            errors.append(f"duplicate node names: {dupes}")
        by_name = {n.name: n for n in self.nodes}

        for n in self.nodes:
            if not n.name:
                errors.append("node names must be non-empty")
            if n.terminal and n.agent:
                errors.append(f"terminal node {n.name!r} must not have an agent")
            if not n.terminal and not n.agent:
                errors.append(f"non-terminal node {n.name!r} must name an agent")
            if n.max_visits is not None and n.max_visits < 1:
                errors.append(f"node {n.name!r}: max_visits must be >= 1")

        if self.start not in by_name:
            errors.append(f"start node {self.start!r} is not defined")
        elif by_name[self.start].terminal:
            errors.append("start node must not be terminal")
        if not any(n.terminal for n in self.nodes):
            errors.append("graph needs at least one terminal node")

        seen: set[tuple[str, str]] = set()
        for e in self.edges:
            if e.source not in by_name:
                errors.append(f"edge {e.key}: unknown source node {e.source!r}")
                continue
            if e.target not in by_name:
                errors.append(f"edge {e.key}: unknown target node {e.target!r}")
                continue
            if not e.outcome:
                errors.append(f"edge from {e.source!r} has an empty outcome label")
            if by_name[e.source].terminal:
                errors.append(f"terminal node {e.source!r} must not have outgoing edges")
            if (e.source, e.outcome) in seen:
                errors.append(f"node {e.source!r} has two edges for outcome {e.outcome!r}")
            seen.add((e.source, e.outcome))
            if e.max_traversals is not None and e.max_traversals < 1:
                errors.append(f"edge {e.key}: max_traversals must be >= 1")

        if errors:
            raise GraphValidationError("; ".join(errors))

        # Topology checks (only meaningful once the structure is sound).
        out: dict[str, list[Edge]] = {n: [] for n in by_name}
        for e in self.edges:
            out[e.source].append(e)

        for n in self.nodes:
            if not n.terminal and not out[n.name]:
                errors.append(f"non-terminal node {n.name!r} has no outgoing edges")

        reachable = _reachable(self.start, lambda x: (e.target for e in out[x]))
        unreachable = sorted(set(by_name) - reachable)
        if unreachable:
            errors.append(f"nodes unreachable from start: {unreachable}")

        incoming: dict[str, list[str]] = {n: [] for n in by_name}
        for e in self.edges:
            incoming[e.target].append(e.source)
        can_finish: set[str] = set()
        for t in (n.name for n in self.nodes if n.terminal):
            can_finish |= _reachable(t, lambda x: incoming[x])
        stuck = sorted(set(by_name) - can_finish)
        if stuck:
            errors.append(f"nodes that can never reach a terminal node: {stuck}")

        # Every cycle must pass through a budgeted edge or node, otherwise the
        # executor could loop until max_steps without any per-loop limit.
        bounded_nodes = {n.name for n in self.nodes if n.max_visits is not None}
        unbounded_out = {
            n: [
                e.target
                for e in out[n]
                if e.max_traversals is None and e.target not in bounded_nodes
            ]
            for n in by_name
            if n not in bounded_nodes
        }
        cycle = _find_cycle(unbounded_out)
        if cycle:
            errors.append(
                "cycle without a retry budget (set max_traversals on an edge or "
                f"max_visits on a node): {' -> '.join(cycle)}"
            )

        if errors:
            raise GraphValidationError("; ".join(errors))


def _reachable(start: str, neighbours) -> set[str]:
    seen = {start}
    stack = [start]
    while stack:
        for nxt in neighbours(stack.pop()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _find_cycle(adj: Mapping[str, list[str]]) -> list[str] | None:
    """Return one cycle in ``adj`` as a node path, or None."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {n: WHITE for n in adj}
    for root in sorted(adj):
        if colour[root] != WHITE:
            continue
        path: list[str] = []
        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            node, idx = stack.pop()
            if idx == 0:
                colour[node] = GREY
                path.append(node)
            succ = [s for s in adj.get(node, []) if s in adj]
            if idx < len(succ):
                stack.append((node, idx + 1))
                nxt = succ[idx]
                if colour[nxt] == GREY:
                    return path[path.index(nxt):] + [nxt]
                if colour[nxt] == WHITE:
                    stack.append((nxt, 0))
            else:
                colour[node] = BLACK
                path.pop()
    return None
