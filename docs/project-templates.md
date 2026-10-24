# Plugging a project into Agent Factory

Agent Factory is the platform. A concrete project plugs in through a
`ProjectTemplate` (`agent_factory/templates/base.py`) and touches nothing in
the engine.

```python
@dataclass(frozen=True)
class ProjectTemplate:
    name: str
    description: str
    task: TaskSpec                                   # goal + requirements
    scaffold: Mapping[str, str] = {}                 # files seeded into project/
    protected_paths: tuple[str, ...] = ()            # read-only to agents (e.g. acceptance tests)
    test_command: tuple[str, ...] = UNITTEST_COMMAND # fixed, backend-neutral argv: ("python", "-m", ...)
    sandbox_limits: ResourceLimits = ResourceLimits() # time/cpu/memory/disk/pids/output per execution
    graph_factory: Callable[[], GraphDefinition] = build_software_dev_graph
    agent_factory: Callable[[], Mapping[str, Agent]] = default_agents
    policy: PermissionPolicy = PermissionPolicy()
    milestone_policy: MilestonePolicy = standard_milestone_policy()  # what "progress" means for this project
```

## Adding a project

1. **Create a template module**, for example
   `agent_factory/templates/my_service.py` (or a separate package that imports
   `agent_factory`):

   ```python
   from agent_factory.state import TaskSpec
   from agent_factory.templates import ProjectTemplate

   MY_SERVICE = ProjectTemplate(
       name="my-service",
       description="Small HTTP-free domain library",
       task=TaskSpec(title="Pricing rules", goal="...", requirements=["...", "..."]),
       scaffold={"tests/__init__.py": "", "tests/test_pricing.py": ACCEPTANCE_TESTS},
       protected_paths=("tests",),
       test_command=("python", "-m", "pytest", "-q", "tests"),
   )
   ```

   Register it in `TEMPLATES` if the CLI should know about it.

2. **Create a workspace and run.**

   ```python
   from agent_factory.factory import build_executor

   ws = MY_SERVICE.create_workspace("workspaces/my-service", git=True)
   executor = build_executor(MY_SERVICE, provider, ws)
   state = executor.start(MY_SERVICE.task, metadata={"template": MY_SERVICE.name})
   ```

3. **Optionally customise**, from least to most invasive:
   - **Budgets**: `graph_factory=lambda: build_software_dev_graph(max_test_repairs=5)`.
   - **Policy**: `PermissionPolicy().with_rules(execute_sandboxed="deny")`, or
     `.with_agent_tools("reviewer", {...})`. Dangerous categories cannot be
     loosened below `require_approval`.
   - **Agents**: subclass an agent, or write a new one (`name`, `writes`,
     `run(ctx) -> AgentResult`). Return only outcomes your graph declares.
   - **Graph**: any `GraphDefinition`, for example adding a `security_review`
     node between tester and reviewer. Validation rejects unbudgeted cycles.
   - **Milestones**: list the project's own milestone types, such as
     `ingestion_working` or `api_implemented`, and the node outcomes that
     reach them:
     `milestone_policy=milestone_policy(definitions, rules)`. The engine
     records them when a product store is attached. See
     [product-domain.md](product-domain.md#project-defined-meaning).
   - **Tools**: subclass `Tool`, choose an honest `ActionCategory`, register it
     in the registry passed to `Executor`, and add it to the allowlist of the
     agents that need it. A deploy tool should be `production_deploy`, so it is
     always approval-gated.

Keep existing projects out of the Agent Factory repo. A template references a
project by scaffolding or (in a future version) cloning it into a fresh
workspace, never by editing it in place.

## Adding a real model provider

Implement `agent_factory.providers.ModelProvider`:

```python
class ClaudeProvider(ModelProvider):
    name = "claude"

    def complete(self, request: ModelRequest) -> ModelResponse:
        # 1. Render request.instructions + JSON(request.inputs) into a prompt.
        # 2. Ask for JSON matching request.output_schema (tool-use / structured output).
        # 3. Parse the reply into a dict and return ModelResponse(content=..., provider=..., model=..., usage=...).
        ...
```

`OpenAIProvider`, `CodexProvider`, and `OllamaProvider` follow the same
pattern. Graph, executor, agents, and state stay unchanged. Each agent
validates the returned dict against its schema.

Because a real provider makes network calls and needs a credential, adding one
is a deliberate step outside v0.1's guarantees. Before doing so:

- read the API key from an explicit, user-provided source, never from
  repository files
- add a per-run token/cost budget
- prepare the Docker sandbox image (`agent-factory sandbox build-image`, see
  [sandbox.md](sandbox.md)). A real provider's code is untrusted, so it runs
  only in that sandbox by default; without it, runs move to `blocked`
  instead of executing generated code

Determinism note: real providers are not deterministic. Resume still works,
because state is checkpointed between nodes, but a re-run node may produce a
different answer than the interrupted attempt would have.
