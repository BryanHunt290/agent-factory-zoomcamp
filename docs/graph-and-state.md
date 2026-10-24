# Graph and state model

## Graph definition

A `GraphDefinition` (`agent_factory/graph/definition.py`) is immutable data:

- **`Node(name, agent=None, terminal=False, max_visits=None)`**: a non-terminal
  node names the agent that runs it. `max_visits` caps how often it may be
  entered in one run.
- **`Edge(source, outcome, target, max_traversals=None)`**: when `source`
  reports `outcome`, the executor moves to `target`. `max_traversals` caps how
  often this edge may be taken in one run.
- **`start`**, **`max_steps`**: the entry node and a global ceiling on the
  number of agent steps.

Graphs are validated on construction. A graph is rejected if any of these hold:

- it has duplicate or unknown nodes, or a missing or terminal start node
- it has no terminal node
- a terminal node has an agent or outgoing edges
- a non-terminal node has no agent or no outgoing edges
- two edges share the same `(source, outcome)`
- a node is unreachable from the start, or can never reach a terminal node
- **a cycle has no budget**: every cycle must pass through an edge with
  `max_traversals` or a node with `max_visits`

Graphs serialize with `to_dict()` / `from_dict()`, so a template can ship one
as JSON.

### Routing

Agents report an outcome string. `graph.transition(node, outcome)` returns the
edge or raises `InvalidTransitionError`, which fails the run. The rejected
node's state updates are **not** committed.

### The standard graph (`build_software_dev_graph`)

| Edge | Budget (default) |
| --- | --- |
| `planner:planned -> developer` | - |
| `developer:implemented -> tester` | - |
| `tester:pass -> reviewer` | - |
| `tester:fail -> developer` | `max_test_repairs = 3` |
| `reviewer:approved -> done` | - |
| `reviewer:changes_required -> developer` | `max_review_repairs = 2` |

`planner.max_visits = 1` and `developer.max_visits = 1 + 3 + 2`. `max_steps` is
set to the exact worst-case path length (16 by default), so it acts as a
backstop that never cuts a legal run short. Budgets apply run-wide, not per
review round.

### Bounded retries: three layers

1. **Edge budgets.** If taking an edge would exceed `max_traversals`, the run
   ends as `exhausted` and the log shows a `transition` with
   `accepted: false` plus a `run_exhausted` event.
2. **Node visit limits.** Entering a node beyond `max_visits` ends the run the
   same way.
3. **`max_steps`.** A global ceiling.

Validation guarantees that layer 1 or 2 covers every cycle. Layer 3 covers
everything. An unlimited repair loop is therefore impossible.

## Project state

`ProjectState` (`agent_factory/state.py`) is the only channel between agents.
There is no transcript.

| Field | Owner | Contents |
| --- | --- | --- |
| `task` | template | title, goal, requirements |
| `plan` | planner | summary, steps, files, acceptance_criteria |
| `implementation` | developer | iteration, summary, responding_to, files_written, files_deleted, deletions_rejected, commit |
| `test_report` | tester | passed, returncode, tests_run, failures, output (tail), iteration |
| `review` | reviewer | iteration, verdict, summary, comments |
| `current_node`, `step`, `status` | executor | position and lifecycle |
| `node_visits`, `edge_traversals` | executor | retry accounting |
| `history` | executor | one compact line per completed step: node, visit, outcome, summary |
| `pending_approval`, `approvals` | executor | approval gate state |
| `error`, `metadata`, timestamps | executor | |

Agents receive a deep-copied snapshot, so they cannot mutate the live state.
They return `AgentResult(outcome, updates, summary)`.

Feedback is structured. When the developer is re-entered, `feedback_for(state)`
builds `{"source": "tester", "failures": [...], "output": "..."}` or
`{"source": "reviewer", "comments": [...]}` from the owning fields. The
developer's context therefore stays bounded no matter how many iterations run.

## Persistence

```
<workspace>/.agent_factory/runs/<run_id>/
  checkpoint.json        latest ProjectState (atomic write: temp + fsync + rename)
  execution_log.jsonl    append-only event log
  run.lock               held (an OS lock) by the one process changing the run
  executions.json        ledger of the run's approved actions, if it has any (atomic write)
  executions.lock        held while the ledger file changes
```

On Windows, another process (an antivirus scanner, the search indexer) can
briefly stop a just-written file from being replaced. The atomic write retries
that denial for up to 0.4 s, then raises.

A checkpoint is written after every completed node, on pause, on failure, on
exhaustion, and on completion. JSON is pretty-printed with sorted keys and LF
line endings, so it diffs cleanly. `schema_version` guards against loading
incompatible state.

**Resume semantics.** `Executor.resume(run_id)` loads the checkpoint and
continues at `current_node`. A node that was interrupted is re-run from the
start (at-least-once execution per node). Its approved actions are not: the
action ledger replays a recorded success, and never resends an action whose
outcome is unknown ([safety.md](safety.md#durable-execution-approval-is-not-execution)).

## Execution log

Each line is a JSON object with `seq`, `timestamp` (UTC, `Z`), `event`,
`run_id`, event fields, `prev_hash`, and `hash`. Each `hash` is
SHA-256 over the entry, chained to the previous one, and
`ExecutionLog.verify()` detects edits, deletions, and reordering.

| Event | Key fields |
| --- | --- |
| `run_started` / `run_resumed` | graph, node, task, provider |
| `node_entered` | step, node, agent, visit |
| `action` (kind `tool`) | tool, category, decision, params (long strings hashed), result, summary, approval_id |
| `action` (kind `model_call`) | provider, model, purpose, attempt, result, usage |
| `node_result` | outcome, summary, updated_fields |
| `transition` | source, outcome, target, edge, traversal, max_traversals, accepted, reason |
| `approval_requested` / `approval_decision` | request / request_id, approved, approver, note |
| `run_paused` / `run_completed` / `run_failed` / `run_exhausted` | reason / error |

Inspect a log with `python -m agent_factory log --workspace W --run-id R`.
