"""Command-line interface.

    python -m agent_factory demo [--scenario repair] [--workspace DIR] [--fresh] [--database]
    python -m agent_factory demo --database --project-id PRJ [--workspace-base DIR]   (an API-created project)
    python -m agent_factory status  --workspace DIR --run-id ID
    python -m agent_factory log     --workspace DIR --run-id ID
    python -m agent_factory approve --workspace DIR --run-id ID --request-id APR [--deny] [--note TEXT] [--database]
    python -m agent_factory resume  --workspace DIR --run-id ID [--database]
    python -m agent_factory reconcile-action --workspace DIR --run-id ID --execution-id AXE --attempt N
            --fingerprint SHA --choice it-happened|did-not-happen|retry [--note TEXT] [--database]
    python -m agent_factory db check | migrate | verify | reconcile --workspace DIR [--run-id ID]
    python -m agent_factory api serve [--host 127.0.0.1] [--port 8000]   (loopback only)
    python -m agent_factory auth principal add --issuer URL --subject SUB [--grant PERM ...] [--grant-all]
    python -m agent_factory auth principal list | disable --id PRN | enable --id PRN
    python -m agent_factory auth principal grant|revoke --id PRN PERM [PERM ...]
    python -m agent_factory auth dev-issuer init | token --subject SUB --out FILE   (DEVELOPMENT ONLY)
    python -m agent_factory worker enqueue --project-id PRJ [--run-id ID] [--scenario NAME] [--workspace-base DIR]
    python -m agent_factory worker run [--workspace-base DIR] [--lease S] [--heartbeat S] [--step-budget N]
            [--max-runs N] [--idle-exit S]

``--database`` records product data (projects, runs, milestones, approvals) in
the PostgreSQL database configured by AGENT_FACTORY_DATABASE_URL or
AGENT_FACTORY_DATABASE_URL_FILE (docs/database.md). Without it nothing is
stored outside the workspace.

With ``--database``, a command never races a worker on the same run
(docs/workers.md, "operator commands"): ``approve`` and ``reconcile-action``
only record the person's decision and wake the run, and a worker applies it;
``resume``, ``demo``, and ``db reconcile`` claim the run first, exactly as a
worker does, and refuse while a worker holds it. Without ``--database`` they
act on the workspace directly, as before.

``api serve`` runs the HTTP API (docs/backend.md) on 127.0.0.1, and refuses
non-loopback addresses. Every product request needs a valid access token from
the configured identity provider (docs/authentication.md); without an
authentication configuration the server does not start.

``auth principal`` enrolls the identities that may use the API and grants
their permissions, in the configured database. No HTTP route can do this.
``auth dev-issuer`` is a local token issuer for development only: it works
only with AGENT_FACTORY_AUTH_MODE=development, and production refuses it.

``worker run`` runs ONE worker process in the foreground (docs/workers.md): it
claims runs from the configured database, advances them under a lease renewed
by heartbeats, and gives them back. Every write it makes to a run is fenced:
refused once another worker owns the run. It prints its worker ID and one
JSON line per event. Ctrl+C stops it after the current step; a second Ctrl+C
stops it at once (the lease then expires, and another worker recovers the
run). Run the command again in another terminal for a second worker; nothing
starts workers on its own. ``worker enqueue`` creates a run for an existing
project, or takes an existing run, and queues it for a worker.

``reconcile-action`` records a person's determination of an approved action
whose outcome is uncertain (docs/design/action-execution.md). "retry" is a
new authorization for one more attempt, valid for 15 minutes. The actor is an
unauthenticated local placeholder until authentication exists.

Exit codes: 0 completed or awaiting approval; 1 failed or exhausted; 2 usage
or configuration error; 3 blocked; 4 database schema not current; 5 awaiting
reconciliation (a person must say whether an action happened).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from .demo import SCENARIOS, scenario_provider
from .domain.claims import OwnershipLost
from .domain.errors import DomainError, StorageError
from .domain.executions import ReconciliationChoice
from .domain.identity import Actor
from .domain.ids import new_principal_id
from .domain.principals import ALL_PERMISSIONS, Permission, Principal, PrincipalStatus
from .domain.repositories import ProductStore
from .engine import CheckpointStore, ExecutionLog, Executor, ExecutorError
from .factory import build_executor, workspace_for_project
from .persistence.config import DatabaseConfig, DatabaseConfigError
from .providers import ModelProvider
from .safety.execution import ExecutionLedgerUnavailable
from .state import ProjectState, RunStatus
from .templates import TEMPLATES
from .util import timestamp, utc_now
from .workspace import Workspace, WorkspaceError, workspace_root_for

DEV_ISSUER_DIR = Path(".local") / "dev-issuer"


def _template_for(workspace: Workspace):
    template_name = workspace.info.get("template")
    template = TEMPLATES.get(template_name)
    if template is None:
        raise SystemExit(f"unknown template {template_name!r} recorded in workspace")
    return template


def _executor_for(
    workspace: Workspace, run_id: str, product_store: ProductStore | None = None, ownership=None
) -> tuple[Executor, ProjectState]:
    """Rebuild the executor for an existing run from what was persisted."""
    template = _template_for(workspace)
    probe = build_executor(template, _NoProvider(), workspace)
    state = probe.store.load(run_id)
    provider: ModelProvider
    scenario = state.metadata.get("scenario")
    if state.metadata.get("provider") == "fake" and scenario:
        provider = scenario_provider(scenario)
    else:
        raise SystemExit("v0.1 can only resume runs that use the fake provider")
    return build_executor(template, provider, workspace, product_store=product_store, ownership=ownership), state


def _operator_claim(project_id: str, run_id: str):
    """The run's claim, held for a database-mode operator command (worker/operator.py)."""
    config = DatabaseConfig.from_environment()
    from .persistence.claims import open_claim_repository, open_fenced_store
    from .worker.operator import claimed_run

    return claimed_run(lambda: open_claim_repository(config), lambda fence: open_fenced_store(config, fence), project_id, run_id)


def _known_to_database(executor: Executor, run_id: str) -> None:
    """Record a run started without --database before any database-mode command changes it.

    Until it is recorded no worker can know it (a claim needs the run's record),
    so this one unclaimed write cannot race anyone.
    """
    recorder = executor.recorder
    if recorder is not None and recorder.store.runs.get(recorder.project.id, run_id) is None:
        recorder.sync(executor.store.load(run_id))


def _database_store() -> ProductStore:
    """The configured PostgreSQL product store. Never migrates; refuses an unmigrated schema."""
    config = DatabaseConfig.from_environment()
    try:
        from .persistence.postgres import open_postgres_store
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"error: --database needs the PostgreSQL driver ({exc.name} is not installed): "
            "pip install -r requirements-postgres.txt (docs/database.md)"
        ) from None
    return open_postgres_store(config)


def _optional_store(args: argparse.Namespace) -> ProductStore | None:
    return _database_store() if getattr(args, "database", False) else None


def _close(store: ProductStore | None) -> None:
    if store is not None:
        store.close()


class _NoProvider(ModelProvider):
    name = "none"

    def complete(self, request):  # pragma: no cover - never called
        raise RuntimeError("no provider configured")


def _print_state(state: ProjectState, workspace: Workspace) -> None:
    print(f"\nrun {state.run_id}: {state.status.value.upper()}  (graph {state.graph_name}, {state.step} steps)")
    print("route:")
    print("  START")
    for h in state.history:
        print(f"  -> {h['node']:<9} [{h['outcome']}]  {h['summary']}")
    if state.status is RunStatus.COMPLETED:
        print(f"  -> {state.current_node.upper()}")
    if state.test_report:
        r = state.test_report
        print(f"tests:  {r['tests_run']} run, {'passed' if r['passed'] else 'FAILED'} {r['failures'] or ''}".rstrip())
    if state.review:
        print(f"review: {state.review['verdict']} - {state.review['summary']}")
    if state.error:
        print(f"stopped: {state.error['type']}: {state.error['message']}")
    if state.blocked:
        b = state.blocked
        print("\nBLOCKED (no code was executed; no retry consumed)")
        print(f"  at:      {b['node']} (step {b['step']})")
        print(f"  reason:  {b['type']}: {b['message']}")
        print("  fix the sandbox backend, then:")
        print(f"  resume:  python -m agent_factory resume --workspace {workspace.root} --run-id {state.run_id}")
    if state.pending_approval:
        p = state.pending_approval
        print("\nAPPROVAL REQUIRED")
        print(f"  request:  {p['request_id']}")
        print(f"  action:   {p['agent']} -> {p['tool']} {p['params']}  ({p['category']})")
        print("  approve:  python -m agent_factory approve "
              f"--workspace {workspace.root} --run-id {state.run_id} --request-id {p['request_id']}")
        print("  reject:   (same command) --deny")
        print(f"  then:     python -m agent_factory resume --workspace {workspace.root} --run-id {state.run_id}")
    if state.pending_reconciliation:
        r = state.pending_reconciliation
        print("\nRECONCILIATION REQUIRED (an approved action may already have happened)")
        print(f"  action:   {r['agent']} -> {r['tool']} {r['params']}  ({r['category']})")
        print(f"  attempt:  {r['attempt']}, started {r['dispatched_at']}")
        print(f"  why:      {r['reason']}")
        print(f"  execution {r['execution_id']}, fingerprint {r['action_fingerprint']}")
        print("  check whether it happened, then record what you found:")
        print(
            "  python -m agent_factory reconcile-action "
            f"--workspace {workspace.root} --run-id {state.run_id} --execution-id {r['execution_id']} "
            f"--attempt {r['attempt']} --fingerprint {r['action_fingerprint']} "
            "--choice it-happened|did-not-happen|retry"
        )
    print(f"\nworkspace:  {workspace.project_dir}")
    print(f"checkpoint: {workspace.runs_dir / state.run_id / 'checkpoint.json'}")
    print(f"log:        {workspace.runs_dir / state.run_id / 'execution_log.jsonl'}")


def _exit_code(state: ProjectState) -> int:
    if state.status in (RunStatus.COMPLETED, RunStatus.AWAITING_APPROVAL):
        return 0
    if state.status is RunStatus.AWAITING_RECONCILIATION:
        return 5
    return 3 if state.status is RunStatus.BLOCKED else 1


def cmd_demo(args: argparse.Namespace) -> int:
    template = TEMPLATES["calculator"]
    if args.project_id:
        # A project that already exists in the database, e.g. created through the API. Its
        # workspace directory is derived from its ID alone; its name is never part of a path.
        if not args.database:
            print("error: --project-id needs --database (the project lives in the database)", file=sys.stderr)
            return 2
        if args.workspace:
            print(
                "error: with --project-id the workspace is <workspace-base>/<project-id>; "
                "use --workspace-base instead of --workspace",
                file=sys.stderr,
            )
            return 2
        root = workspace_root_for(args.workspace_base, args.project_id)
    else:
        root = Path(args.workspace or Path("workspaces") / f"demo-{args.scenario}")
    if root.exists() and not args.fresh:
        print(f"{root} already exists; pass --fresh to recreate it", file=sys.stderr)
        return 2
    store = _optional_store(args)  # before touching the workspace: fail fast on bad configuration
    try:
        if root.exists():
            Workspace.remove(root)
        git = args.git and shutil.which("git") is not None
        if args.project_id:
            project = store.projects.get(args.project_id)
            if project is None:
                raise WorkspaceError(f"project {args.project_id} does not exist in the database")
            if project.template != template.name:
                raise WorkspaceError(
                    f"the demo runs the {template.name!r} template, but project {args.project_id} "
                    f"uses {project.template!r}"
                )
            _, workspace = workspace_for_project(store, args.project_id, args.workspace_base, git=git)
        else:
            workspace = template.create_workspace(root, git=git)
        description = SCENARIOS[args.scenario][0]
        provider, budget = _demo_provider(args)
        if args.real_model:
            print(f"Agent Factory demo with a REAL model: {provider.name} / {provider.model}")
            print(f"PRIVACY: the task, this project's file names, and the files each step needs (never secrets) are sent to "
                  f"{provider.name} and processed there. Budget: {budget.max_calls_per_run} calls, "
                  f"{budget.max_input_tokens_per_run} input / {budget.max_output_tokens_per_run} output tokens per run.")
            print("tests: only in the Docker sandbox (a real model's code is untrusted); the run blocks without it")
            metadata = {"template": template.name, "provider": provider.name, "model": provider.model}
        else:
            print(f"Agent Factory demo: scenario '{args.scenario}' - {description}")
            print("provider: fake (deterministic, no network; trusted fixture)")
            print("tests: real unittest via LocalProcessBackend - NOT a sandbox; untrusted code would be blocked")
            metadata = {"template": template.name, "provider": "fake", "scenario": args.scenario}
        executor = build_executor(template, provider, workspace, product_store=store, model_budget=budget)
        if store is None:
            state = executor.start(template.task, run_id=args.run_id, metadata=metadata)
        else:
            # Create the run, then advance it under its claim, exactly as a worker would.
            created = executor.create(template.task, run_id=args.run_id, metadata=metadata)
            with _operator_claim(workspace.ensure_project_id(), created.run_id) as held:
                claimed = build_executor(
                    template, provider, workspace, product_store=held.store, ownership=held.ownership, model_budget=budget
                )
                state = held.state = claimed.advance(created.run_id)
        _print_state(state, workspace)
        print(f"log entries: {executor.log_for(state.run_id).verify()} (hash chain verified)")
        _print_records(store, workspace, state.run_id)
        return _exit_code(state)
    finally:
        _close(store)


def _demo_provider(args: argparse.Namespace):
    """The fake scenario provider, or (only with --real-model and the full explicit configuration) a real one."""
    from .providers.config import ModelConfig, ModelConfigError, build_provider

    try:
        config = ModelConfig.from_environment()
    except ModelConfigError as exc:
        raise SystemExit(f"error: {exc}") from None
    if not args.real_model:
        return scenario_provider(args.scenario), config.budget
    if not config.real:
        raise SystemExit(
            "error: --real-model needs AGENT_FACTORY_MODEL_PROVIDER, AGENT_FACTORY_MODEL_MODE=live, "
            "AGENT_FACTORY_MODEL_NAME, and AGENT_FACTORY_MODEL_API_KEY_FILE (docs/model-providers.md)"
        )
    return build_provider(config), config.budget


def cmd_model_check(args: argparse.Namespace) -> int:
    """Validate the model configuration and print it (never the key). Makes no model call."""
    from .providers.config import ModelConfig, ModelConfigError

    try:
        config = ModelConfig.from_environment()
    except ModelConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(config.describe(), indent=2, sort_keys=True))
    if config.real:
        print(f"Real model calls are ENABLED: project context may be sent to {config.provider}. No call was made by this check.")
    else:
        print("The deterministic fake provider is in use: no model calls leave this machine.")
    return 0


def _print_records(store: ProductStore | None, workspace: Workspace, run_id: str) -> None:
    if store is None:
        return
    from .domain.repositories import MilestoneQuery

    project_id = workspace.project_id
    milestones = store.milestones.query(MilestoneQuery(project_id=project_id, run_id=run_id))
    print(f"product records (PostgreSQL): project {project_id}, run {run_id}: {len(milestones)} milestones")


def cmd_status(args: argparse.Namespace) -> int:
    # Read-only: needs no provider, so it works for any run.
    workspace = Workspace.open(args.workspace)
    state = CheckpointStore(workspace.runs_dir).load(args.run_id)
    _print_state(state, workspace)
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    workspace = Workspace.open(args.workspace)
    store = CheckpointStore(workspace.runs_dir)
    log = ExecutionLog(store.run_dir(args.run_id) / ExecutionLog.FILENAME)
    for e in log.entries():
        detail = {k: v for k, v in e.items() if k not in ("seq", "timestamp", "event", "hash", "prev_hash", "run_id")}
        print(f"{e['seq']:>4} {e['timestamp']} {e['event']:<18} {detail}")
    print(f"\n{log.verify()} entries, hash chain verified")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    workspace = Workspace.open(args.workspace)
    verdict = "REJECTED" if args.deny else "APPROVED"
    if not args.database:
        executor, _ = _executor_for(workspace, args.run_id)
        state = executor.decide_approval(
            args.run_id, args.request_id, approved=not args.deny, approver=args.approver, note=args.note
        )
        print(f"{verdict} {args.request_id}; run {state.run_id} is ready to resume")
        return 0
    # Database mode: record the decision (and wake the run) only; a worker applies it under the run's claim.
    with _database_store() as store:
        executor, state = _executor_for(workspace, args.run_id, store)
        executor.check_pending_approval(state, args.request_id)
        _known_to_database(executor, args.run_id)
        executor.recorder.record_decision(  # type: ignore[union-attr]
            state, args.request_id, approved=not args.deny, approver=args.approver, note=args.note,
            decided_at=timestamp(executor.clock),
        )  # fmt: skip
    print(f"{verdict} {args.request_id} recorded; a worker applies it to run {args.run_id}")
    print(f"or now:  python -m agent_factory resume --workspace {workspace.root} --run-id {args.run_id} --database")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    workspace = Workspace.open(args.workspace)
    if not args.database:
        executor, _ = _executor_for(workspace, args.run_id)
        state = executor.resume(args.run_id)
        _print_state(state, workspace)
        return _exit_code(state)
    # Database mode: claim the run like a worker, apply any stored decision through the same checks, continue.
    with _database_store() as store:
        executor, _ = _executor_for(workspace, args.run_id, store)
        _known_to_database(executor, args.run_id)
    with _operator_claim(workspace.ensure_project_id(), args.run_id) as held:
        executor, _ = _executor_for(workspace, args.run_id, held.store, held.ownership)
        state = held.state = executor.advance(args.run_id, unblock=True)
        _print_state(state, workspace)
        _print_records(held.store, workspace, state.run_id)
    return _exit_code(state)


#: CLI spelling -> the recorded choice.
RECONCILIATION_CHOICES = {
    "it-happened": ReconciliationChoice.IT_HAPPENED,
    "did-not-happen": ReconciliationChoice.DID_NOT_HAPPEN,
    "retry": ReconciliationChoice.AUTHORIZE_RETRY,
}


def cmd_reconcile_action(args: argparse.Namespace) -> int:
    workspace = Workspace.open(args.workspace)
    if args.database:
        # Record the person's decision (and wake the run) only; a worker applies it to the ledger under the claim.
        from .domain.reconciliation import ReconciliationService

        with _database_store() as store, store.transaction():
            ReconciliationService(store).record(
                project_id=workspace.ensure_project_id(), run_id=args.run_id, execution_id=args.execution_id,
                attempt=args.attempt, action_fingerprint=args.fingerprint, choice=RECONCILIATION_CHOICES[args.choice],
                actor=Actor.unauthenticated(args.approver), comment=args.note,
            )  # fmt: skip
        print(f"recorded '{args.choice}' for {args.execution_id} (attempt {args.attempt}); a worker applies it")
        if args.choice == "retry":
            print("one more attempt is authorized for 15 minutes from now, once a worker applies the decision")
        print(f"or now:  python -m agent_factory resume --workspace {workspace.root} --run-id {args.run_id} --database")
        return 0
    store = None
    try:
        executor, _ = _executor_for(workspace, args.run_id, store)
        state = executor.reconcile_action(
            args.run_id,
            args.execution_id,
            attempt=args.attempt,
            action_fingerprint=args.fingerprint,
            choice=RECONCILIATION_CHOICES[args.choice],
            approver=args.approver,
            comment=args.note,
        )
    finally:
        _close(store)
    print(f"recorded '{args.choice}' for {args.execution_id} (attempt {args.attempt}); run {state.run_id}: {state.status.value}")
    if args.choice == "retry":
        print("one more attempt is authorized for 15 minutes; resume the run to make it")
    if state.status is RunStatus.RUNNING:
        print(f"resume:  python -m agent_factory resume --workspace {workspace.root} --run-id {state.run_id}")
    return 0


def cmd_db_check(args: argparse.Namespace) -> int:
    """Connectivity, role, and migration status. Changes nothing."""
    from .persistence.checks import check_connection

    config = DatabaseConfig.from_environment()
    report = check_connection(config)
    print(f"database:   {config.redacted()}")
    print(f"configured: {config.source}")
    print(f"server:     PostgreSQL {report.server_version}")
    role_note = "  WARNING: superuser; use a least-privilege role (docs/database.md)" if report.superuser else ""
    print(f"role:       {report.user} (superuser: {'yes' if report.superuser else 'no'}){role_note}")
    print(f"migrations: {report.migrations.describe()}")
    return 0 if report.migrations.up_to_date else 4


def cmd_db_migrate(args: argparse.Namespace) -> int:
    from .persistence.migrator import migrate, migration_status
    from .persistence.postgres import connect

    config = DatabaseConfig.from_environment()
    conn = connect(config)
    try:
        applied = migrate(conn, config.schema)
        status = migration_status(conn, config.schema)
    finally:
        conn.close()
    for m in applied:
        print(f"applied {m.version:04d}_{m.name} ({m.checksum[:19]}...)")
    if not applied:
        print("nothing to apply")
    print(status.describe())
    return 0 if status.up_to_date else 4


def cmd_db_verify(args: argparse.Namespace) -> int:
    from .persistence.checks import verify_round_trip

    config = DatabaseConfig.from_environment()
    for step in verify_round_trip(config):
        print(f"ok: {step}")
    print(f"persistence verified on {config.redacted()}")
    return 0


def cmd_db_reconcile(args: argparse.Namespace) -> int:
    """Re-derive product records from checkpoints, and apply stored decisions the engine missed."""
    workspace = Workspace.open(args.workspace)
    run_ids = [args.run_id] if args.run_id else CheckpointStore(workspace.runs_dir).list_runs()
    from .worker.loop import Release
    from .worker.operator import RunOwnedElsewhere

    template = _template_for(workspace)
    with _database_store() as store:
        plain = build_executor(template, _NoProvider(), workspace, product_store=store)
        for run_id in run_ids:
            _known_to_database(plain, run_id)
    for run_id in run_ids:
        try:
            with _operator_claim(workspace.ensure_project_id(), run_id) as held:
                executor = build_executor(template, _NoProvider(), workspace, product_store=held.store, ownership=held.ownership)
                before = executor.store.load(run_id).status
                state = executor.reconcile(run_id)  # never runs an agent: no provider needed
                held.release = Release(None, "operator_done")  # whether a worker wants the run is unchanged
        except RunOwnedElsewhere as exc:
            print(f"{run_id}: skipped: {exc}")
            continue
        change = f" (stored decision applied: {before.value} -> {state.status.value})" if state.status != before else ""
        print(f"{run_id}: {state.status.value}; product records reconciled{change}")
    return 0


def cmd_api_serve(args: argparse.Namespace) -> int:
    """Run the HTTP API on loopback. Every product request is authenticated and authorized."""
    try:
        from .api.auth import AuthConfigError, AuthMode, AuthSettings
        from .api.auth.development import DevelopmentIssuerError
        from .api.config import ApiSettings, UnsafeBindError
        from .api.server import serve
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"error: the API needs its dependencies ({exc.name} is not installed): "
            "pip install -r requirements-api.txt (docs/backend.md)"
        ) from None
    try:
        settings = ApiSettings.from_environment(
            host=args.host,
            port=args.port,
            pool_max_size=args.pool_max_size,
            pool_timeout_s=args.pool_timeout,
        )
        warnings = settings.bind_warnings()
    except UnsafeBindError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for warning in warnings:
        print(warning, file=sys.stderr)
    try:
        auth = AuthSettings.from_environment()
    except AuthConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    database = DatabaseConfig.from_environment()
    if auth.mode is AuthMode.DEVELOPMENT:
        print("DEVELOPMENT AUTHENTICATION: tokens from the local development issuer are accepted.", file=sys.stderr)
    print(
        f"Agent Factory API: http://{settings.host}:{settings.port}/api/v1  "
        f"(loopback only; tokens from {auth.policy.issuer}; stop with Ctrl+C)",
        file=sys.stderr,
    )
    try:
        serve(settings, database, auth, log_level=args.log_level)
    except DevelopmentIssuerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------- auth


def _permissions(names: list[str] | None) -> frozenset[Permission]:
    return frozenset(Permission(name) for name in names or [])


def _describe(principal: Principal, permissions: frozenset[Permission]) -> str:
    name = f" {principal.display_name!r}" if principal.display_name else ""
    granted = ", ".join(sorted(permissions)) or "none"
    return f"{principal.id} [{principal.status.value}]{name}  {principal.issuer} / {principal.subject}  permissions: {granted}"


def cmd_auth_principal_add(args: argparse.Namespace) -> int:
    """Enroll an identity (issuer + subject). Without grants it can authenticate but do nothing."""
    grants = ALL_PERMISSIONS if args.grant_all else _permissions(args.grant)
    now = timestamp(utc_now)
    principal = Principal(
        id=new_principal_id(),
        issuer=args.issuer,
        subject=args.subject,
        status=PrincipalStatus.ACTIVE,
        created_at=now,
        updated_at=now,
        display_name=args.display_name,
        email=args.email,
    )
    with _database_store() as store, store.transaction():
        store.principals.add(principal)
        if grants:
            store.principals.grant(principal.id, grants, at=now)
    print(f"enrolled {_describe(principal, grants)}")
    return 0


def cmd_auth_principal_list(args: argparse.Namespace) -> int:
    with _database_store() as store:
        principals = store.principals.list()
        for principal in principals:
            print(_describe(principal, store.principals.permissions(principal.id)))
    if not principals:
        print("no principals enrolled")
    return 0


def _set_status(principal_id: str, status: PrincipalStatus) -> int:
    with _database_store() as store, store.transaction():
        principal = store.principals.get(principal_id)
        if principal is None:
            print(f"error: no principal {principal_id}", file=sys.stderr)
            return 2
        if principal.status is not status:
            principal = principal.with_status(status, timestamp(utc_now))
            store.principals.update(principal)
        print(_describe(principal, store.principals.permissions(principal.id)))
    return 0


def cmd_auth_principal_disable(args: argparse.Namespace) -> int:
    """A disabled principal cannot authenticate, whatever it was granted. Takes effect on its next request."""
    return _set_status(args.id, PrincipalStatus.DISABLED)


def cmd_auth_principal_enable(args: argparse.Namespace) -> int:
    return _set_status(args.id, PrincipalStatus.ACTIVE)


def cmd_auth_principal_grant(args: argparse.Namespace) -> int:
    with _database_store() as store, store.transaction():
        store.principals.grant(args.id, _permissions(args.permissions), at=timestamp(utc_now))
        principal = store.principals.get(args.id)
        assert principal is not None  # grant() refuses an unknown principal
        print(_describe(principal, store.principals.permissions(args.id)))
    return 0


def cmd_auth_principal_revoke(args: argparse.Namespace) -> int:
    with _database_store() as store, store.transaction():
        store.principals.revoke(args.id, _permissions(args.permissions))
        principal = store.principals.get(args.id)
        assert principal is not None  # revoke() refuses an unknown principal
        print(_describe(principal, store.principals.permissions(args.id)))
    return 0


def _development_issuer_module():
    """The development issuer, only in AGENT_FACTORY_AUTH_MODE=development (it is not production authentication)."""
    if os.environ.get("AGENT_FACTORY_AUTH_MODE", "").strip().lower() != "development":
        raise SystemExit(
            "error: the development issuer is DEVELOPMENT ONLY; set AGENT_FACTORY_AUTH_MODE=development to use it"
        )
    try:
        from .api.auth import development
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"error: the development issuer needs the API dependencies ({exc.name} is not installed): "
            "pip install -r requirements-api.txt (docs/authentication.md)"
        ) from None
    return development


def cmd_auth_dev_issuer_init(args: argparse.Namespace) -> int:
    development = _development_issuer_module()
    try:
        issuer = development.DevelopmentIssuer.create(args.dir)
    except development.DevelopmentIssuerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"DEVELOPMENT ONLY: created a local token issuer in {args.dir} (key id {issuer.public_jwk['kid']}).")
    print(f"Serve with AGENT_FACTORY_AUTH_MODE=development and AGENT_FACTORY_AUTH_DEV_ISSUER_DIR={args.dir}.")
    print(f"Enroll yourself: auth principal add --issuer {development.DEV_ISSUER} --subject <you> --grant-all")
    return 0


def cmd_auth_dev_issuer_token(args: argparse.Namespace) -> int:
    """Write a short-lived development token to a file. A token is a credential, so it is never printed."""
    development = _development_issuer_module()
    try:
        issuer = development.DevelopmentIssuer.load(args.dir)
        token = issuer.mint(
            args.subject, lifetime=timedelta(minutes=args.minutes), display_name=args.name, email=args.email
        )
    except development.DevelopmentIssuerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(token + "\n")
    print(f"DEVELOPMENT ONLY: wrote a {args.minutes}-minute token for subject {args.subject!r} to {out}")
    return 0


def cmd_sandbox_inspect_base(args: argparse.Namespace) -> int:
    from .sandbox.image import ImageBuilder, ImageBuildError

    try:
        info = ImageBuilder().inspect_base(args.base)
    except ImageBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for key, value in info.__dict__.items():
        print(f"{key:>15}: {value}")
    return 0


def cmd_sandbox_build_image(args: argparse.Namespace) -> int:
    """HUMAN-INITIATED: builds the sandbox image. Never invoked by agent runs."""
    from .sandbox.image import ImageBuilder, ImageBuildError

    try:
        result = ImageBuilder().build(args.base, confirm=args.confirm)
    except ImageBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(result.log)
    for key, value in result.record.__dict__.items():
        print(f"{key:>22}: {value}")
    print("recorded in sandbox_image/image.lock.json")
    return 0


def cmd_sandbox_check(args: argparse.Namespace) -> int:
    """Probe + isolation self-test of the recorded image (starts one short-lived container)."""
    from .sandbox.config import docker_config_from_lock
    from .sandbox.docker import DockerBackend

    backend = DockerBackend(docker_config_from_lock())
    status = backend.self_test()
    if not status.available:
        print("UNAVAILABLE: " + "; ".join(status.reasons))
        return 3
    print(f"OK: docker sandbox available {status.info.to_dict()}")
    return 0


def _stop_on_signals(worker) -> Callable[[], None]:
    """First Ctrl+C (SIGINT, SIGTERM, SIGBREAK): stop after the current step. Second: stop at once."""

    def handler(signum, frame):
        if worker.stopping:
            raise KeyboardInterrupt
        worker.request_stop()

    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            sig = getattr(signal, name)
            previous[sig] = signal.signal(sig, handler)

    def restore() -> None:
        for sig, old in previous.items():
            signal.signal(sig, old)

    return restore


def _claims_opener(config: DatabaseConfig):
    try:
        from .persistence.claims import open_claim_repository
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"error: workers need the PostgreSQL driver ({exc.name} is not installed): "
            "pip install -r requirements-postgres.txt (docs/database.md)"
        ) from None
    return lambda: open_claim_repository(config)


def cmd_worker_run(args: argparse.Namespace) -> int:
    from .domain.claims import LeasePolicy
    from .worker import Worker, WorkerEvents, WorkerIdentity, WorkerSettings
    from .worker.sources import WorkspaceRunSource

    try:
        settings = WorkerSettings(
            policy=LeasePolicy(lease=timedelta(seconds=args.lease), heartbeat_interval=timedelta(seconds=args.heartbeat)),
            poll_interval=args.poll,
            step_budget=args.step_budget,
            max_runs=args.max_runs,
            idle_exit=args.idle_exit,
        )
    except (ValueError, DomainError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    config = DatabaseConfig.from_environment()
    open_claims = _claims_opener(config)
    open_claims().close()  # fail fast: reachable, and every migration applied (never migrates)
    identity = WorkerIdentity.new()
    print(
        f"worker {identity.worker_id} (pid {identity.pid} on {identity.host}) started: one worker, one run at a time. "
        "Ctrl+C stops it after the current step; a second Ctrl+C stops it at once.",
        flush=True,
    )
    events = WorkerEvents(identity.worker_id, sink=lambda record: print(json.dumps(record, sort_keys=True), flush=True))
    worker = Worker(identity, settings, open_claims=open_claims, source=WorkspaceRunSource(config, args.workspace_base), events=events)
    restore = _stop_on_signals(worker)
    try:
        report = worker.run()
    finally:
        restore()
    print(
        f"worker {identity.worker_id} stopped ({report.stopped_because}): {report.claims} claimed, "
        f"{report.released} released, {report.lost} lost",
        flush=True,
    )
    return 0


def cmd_worker_enqueue(args: argparse.Namespace) -> int:
    from .persistence.claims import PostgresClaimRepository

    store = _database_store()
    try:
        git = args.git and shutil.which("git") is not None
        template, workspace = workspace_for_project(store, args.project_id, args.workspace_base, git=git)
        if args.run_id and CheckpointStore(workspace.runs_dir).exists(args.run_id):
            run_id, verb = args.run_id, "queued existing run"
        else:
            executor = build_executor(template, scenario_provider(args.scenario), workspace, product_store=store)
            metadata = {"template": template.name, "provider": "fake", "scenario": args.scenario}
            run_id, verb = executor.create(template.task, run_id=args.run_id, metadata=metadata).run_id, "created and queued run"
        PostgresClaimRepository(store.unit_of_work).enqueue(args.project_id, run_id)  # type: ignore[arg-type]
    finally:
        _close(store)
    print(f"{verb} {run_id} of project {args.project_id} for a worker")
    print(f"start a worker:  python -m agent_factory worker run --workspace-base {args.workspace_base}")
    return 0


# ---------------------------------------------------------------- notifications


def _destination_line(d) -> str:
    """A destination as the CLI prints it: never the full address."""
    return f"{d.id} [{d.status.value}] {d.channel.value} {d.redacted}  principal {d.principal_id}  policy {d.policy.value}"


def cmd_notifications_destination_add(args: argparse.Namespace) -> int:
    """Where a principal is told that something needs attention. The address is never printed in full."""
    from .domain.notifications import Destination, DestinationStatus, NotificationChannel, new_destination_id, normalize_address

    channel = NotificationChannel(args.channel)
    address = normalize_address(channel, args.address)
    now = timestamp(utc_now)
    destination = Destination(
        id=new_destination_id(), principal_id=args.principal_id, channel=channel, address=address,
        status=DestinationStatus.ACTIVE, created_at=now, updated_at=now,
    )  # fmt: skip
    with _database_store() as store, store.transaction():
        if store.principals.get(args.principal_id) is None:
            print(f"error: no principal {args.principal_id}", file=sys.stderr)
            return 2
        store.notifications.add_destination(destination)
    print(f"added {_destination_line(destination)}")
    print("Notifications only alert; they never approve anything. The person signs in to the dashboard to decide.")
    return 0


def cmd_notifications_destination_list(args: argparse.Namespace) -> int:
    with _database_store() as store:
        destinations = store.notifications.list_destinations(args.principal_id)
    for d in destinations:
        print(_destination_line(d))
    if not destinations:
        print("no notification destinations")
    return 0


def _set_destination_status(destination_id: str, active: bool) -> int:
    from .domain.notifications import DestinationStatus

    status = DestinationStatus.ACTIVE if active else DestinationStatus.DISABLED
    with _database_store() as store, store.transaction():
        destination = store.notifications.get_destination(destination_id)
        if destination is None:
            print(f"error: no destination {destination_id}", file=sys.stderr)
            return 2
        if destination.status is not status:
            destination = destination.with_status(status, timestamp(utc_now))
            store.notifications.update_destination(destination)
    print(_destination_line(destination))
    return 0


def cmd_notifications_destination_disable(args: argparse.Namespace) -> int:
    """Stop telling this destination. Notifications already queued for it are skipped, not sent."""
    return _set_destination_status(args.id, active=False)


def cmd_notifications_destination_enable(args: argparse.Namespace) -> int:
    return _set_destination_status(args.id, active=True)


def cmd_notifications_status(args: argparse.Namespace) -> int:
    """The outbox for one approval: each notification's state, attempts, and why; never an address."""
    with _database_store() as store:
        rows = store.notifications.list_for_approval(args.project_id, args.approval_id)
        unknown = store.notifications.with_unknown_outcome([n.id for n in rows])
    for n in rows:
        about = n.attention_kind.value + (f" (attempt {n.attention_attempt})" if n.attention_attempt else "")
        why = f" {n.failure_category.value}" if n.failure_category else ""
        when = f" next {n.next_attempt_at}" if n.next_attempt_at else (f" sent {n.sent_at}" if n.sent_at else "")
        flag = "  an attempt's outcome is unknown" if n.id in unknown else ""
        print(f"{n.id} {about} {n.channel.value} {n.status.value} attempts {n.attempts}{why}{when}{flag}")
    if not rows:
        print("no notifications for this approval")
    return 0


def _notification_provider(args: argparse.Namespace):
    """The provider the dispatcher sends through. Only the fake exists, and only for development."""
    from .notifications import FakeNotificationProvider

    if args.provider != "fake":
        raise SystemExit(
            "error: no real notification provider is configured yet (docs/notifications.md#real-providers). "
            "Notifications stay queued in the outbox, and the dashboard still shows every attention item."
        )
    if os.environ.get("AGENT_FACTORY_NOTIFICATIONS_MODE", "").strip().lower() != "development":
        raise SystemExit(
            "error: the fake notification provider is DEVELOPMENT ONLY; set AGENT_FACTORY_NOTIFICATIONS_MODE=development"
        )
    record = Path(args.fake_record) if args.fake_record else None
    if args.fake_script:
        return FakeNotificationProvider.from_script_file(Path(args.fake_script), record=record)
    return FakeNotificationProvider(record=record)


def cmd_notifications_dispatch(args: argparse.Namespace) -> int:
    from .domain.notifications import RetryPolicy, new_dispatcher_id
    from .notifications import DashboardUrlError, DispatcherEvents, DispatcherSettings, NotificationDispatcher

    provider = _notification_provider(args)
    try:
        settings = DispatcherSettings(
            lease=timedelta(seconds=args.lease), send_timeout=args.send_timeout, poll_interval=args.poll,
            sweep_interval=args.sweep, batch=args.batch, dashboard_url=args.dashboard_url, idle_exit=args.idle_exit,
            retry=RetryPolicy(max_attempts=args.max_attempts, base=timedelta(seconds=args.retry_base),
                              cap=timedelta(seconds=max(args.retry_cap, args.retry_base))),
            max_backoff=max(args.poll, args.max_backoff),
        )  # fmt: skip
    except (ValueError, DashboardUrlError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    config = DatabaseConfig.from_environment()
    try:
        from .persistence.notifications import open_notification_outbox
        from .persistence.postgres import open_postgres_store
    except ModuleNotFoundError as exc:
        raise SystemExit(f"error: the dispatcher needs the PostgreSQL driver ({exc.name} is not installed)") from None
    open_notification_outbox(config).close()  # fail fast: reachable, and every migration applied (never migrates)
    dispatcher_id = new_dispatcher_id()
    print(
        f"notification dispatcher {dispatcher_id} (pid {os.getpid()}) started with the {provider.name} provider. "
        "Notifications alert only; they never approve anything. Ctrl+C stops it.",
        flush=True,
    )
    events = DispatcherEvents(dispatcher_id, sink=lambda record: print(json.dumps(record, sort_keys=True), flush=True))
    dispatcher = NotificationDispatcher(
        dispatcher_id, settings, open_outbox=lambda: open_notification_outbox(config), provider=provider,
        open_store=lambda: open_postgres_store(config), events=events,
    )  # fmt: skip
    restore = _stop_on_signals(dispatcher)
    try:
        report = dispatcher.run()
    finally:
        restore()
    print(f"notification dispatcher {dispatcher_id} stopped ({report.stopped_because}): {report.sent} sent, "
          f"{report.failed} failed, {report.skipped} skipped", flush=True)  # fmt: skip
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_factory", description="Agent Factory v0.1")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run a deterministic demonstration")
    demo.add_argument("--scenario", choices=sorted(SCENARIOS), default="repair")
    demo.add_argument("--workspace", help="workspace directory (default: workspaces/demo-<scenario>)")
    demo.add_argument("--run-id", default="demo")
    demo.add_argument("--fresh", action="store_true", help="recreate the workspace if it exists")
    demo.add_argument("--no-git", dest="git", action="store_false", help="do not git-init the project")
    demo.add_argument("--database", action="store_true", help="record product data in the configured PostgreSQL database")
    demo.add_argument(
        "--project-id",
        help="run for this existing project (e.g. one created through the API); its workspace is "
        "<workspace-base>/<project-id>. Needs --database",
    )
    demo.add_argument("--workspace-base", default="workspaces", help="parent directory of project workspaces")
    demo.add_argument(
        "--real-model", action="store_true",
        help="use the configured REAL model provider instead of the fake (needs AGENT_FACTORY_MODEL_MODE=live; may cost money)",
    )
    demo.set_defaults(func=cmd_demo)

    model = sub.add_parser("model", help="model providers (docs/model-providers.md)")
    model_sub = model.add_subparsers(dest="model_command", required=True)
    model_sub.add_parser("check", help="validate and show the model configuration; makes no call").set_defaults(func=cmd_model_check)

    for name, func, help_text in (
        ("status", cmd_status, "show a run's state"),
        ("log", cmd_log, "print and verify a run's execution log"),
        ("resume", cmd_resume, "resume an interrupted or approved run"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--workspace", required=True)
        p.add_argument("--run-id", required=True)
        if name == "resume":
            p.add_argument("--database", action="store_true", help="reconcile with and record to the configured database")
        p.set_defaults(func=func)

    db = sub.add_parser("db", help="PostgreSQL product store: check, migrate, verify, reconcile")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("check", help="connectivity, role, and migration status (changes nothing)").set_defaults(
        func=cmd_db_check
    )
    db_sub.add_parser("migrate", help="apply pending schema migrations").set_defaults(func=cmd_db_migrate)
    db_sub.add_parser("verify", help="write, reconnect, and read back in a throwaway schema").set_defaults(
        func=cmd_db_verify
    )
    reconcile = db_sub.add_parser("reconcile", help="bring product records in line with a workspace's checkpoints")
    reconcile.add_argument("--workspace", required=True)
    reconcile.add_argument("--run-id", help="one run (default: every run in the workspace)")
    reconcile.set_defaults(func=cmd_db_reconcile)

    sandbox = sub.add_parser("sandbox", help="human-run sandbox image operations")
    sandbox_sub = sandbox.add_subparsers(dest="sandbox_command", required=True)
    insp = sandbox_sub.add_parser("inspect-base", help="read-only identification of a local base image")
    insp.add_argument("--base", required=True, help="full sha256:<64 hex> id of a locally present image")
    insp.set_defaults(func=cmd_sandbox_inspect_base)
    build = sandbox_sub.add_parser("build-image", help="HUMAN-INITIATED: build the sandbox image (never pulls)")
    build.add_argument("--base", required=True, help="full sha256:<64 hex> id of the approved local base image")
    build.add_argument("--confirm", action="store_true", help="required: confirm you intend to build an image")
    build.set_defaults(func=cmd_sandbox_build_image)
    check = sandbox_sub.add_parser("check", help="probe + isolation self-test of the recorded image")
    check.set_defaults(func=cmd_sandbox_check)

    api = sub.add_parser("api", help="the HTTP API (loopback only; authenticated)")
    api_sub = api.add_subparsers(dest="api_command", required=True)
    serve = api_sub.add_parser("serve", help="serve the API from the configured PostgreSQL database")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1, loopback only)")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--pool-max-size", type=int, default=10, help="most database connections at once")
    serve.add_argument("--pool-timeout", type=float, default=3.0, help="seconds to wait for a free connection")
    serve.add_argument("--log-level", choices=("debug", "info", "warning", "error"), default="info")
    serve.set_defaults(func=cmd_api_serve)

    permission_names = [p.value for p in Permission]
    auth = sub.add_parser("auth", help="principals and permissions; the development token issuer")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    principal = auth_sub.add_parser("principal", help="enrolled identities and their permissions (in the database)")
    principal_sub = principal.add_subparsers(dest="principal_command", required=True)
    add = principal_sub.add_parser("add", help="enroll an identity: an OpenID Connect issuer and subject")
    add.add_argument("--issuer", required=True, help="the identity provider's issuer, exactly as in its tokens")
    add.add_argument("--subject", required=True, help="the provider's stable subject ID (never an email address)")
    add.add_argument("--display-name", default=None)
    add.add_argument("--email", default=None, help="shown to people only; never identifies anyone")
    grant_group = add.add_mutually_exclusive_group()
    grant_group.add_argument("--grant", action="append", choices=permission_names, help="a permission (repeatable)")
    grant_group.add_argument("--grant-all", action="store_true", help="every permission (the owner)")
    add.set_defaults(func=cmd_auth_principal_add)
    principal_sub.add_parser("list", help="every principal with its status and permissions").set_defaults(
        func=cmd_auth_principal_list
    )
    for name, func, help_text in (
        ("disable", cmd_auth_principal_disable, "a disabled principal cannot authenticate"),
        ("enable", cmd_auth_principal_enable, "allow a disabled principal again"),
    ):
        status_parser = principal_sub.add_parser(name, help=help_text)
        status_parser.add_argument("--id", required=True, help="the principal's prn- ID")
        status_parser.set_defaults(func=func)
    for name, func in (("grant", cmd_auth_principal_grant), ("revoke", cmd_auth_principal_revoke)):
        change = principal_sub.add_parser(name, help=f"{name} permissions")
        change.add_argument("--id", required=True, help="the principal's prn- ID")
        change.add_argument("permissions", nargs="+", choices=permission_names)
        change.set_defaults(func=func)
    dev = auth_sub.add_parser("dev-issuer", help="DEVELOPMENT ONLY: a local token issuer (refused in production)")
    dev_sub = dev.add_subparsers(dest="dev_command", required=True)
    init = dev_sub.add_parser("init", help="create the local signing key and its public key set")
    init.add_argument("--dir", default=str(DEV_ISSUER_DIR))
    init.set_defaults(func=cmd_auth_dev_issuer_init)
    token = dev_sub.add_parser("token", help="write a short-lived development token to a file")
    token.add_argument("--subject", required=True)
    token.add_argument("--out", required=True, help="file to write the token to (it is never printed)")
    token.add_argument("--minutes", type=int, default=15)
    token.add_argument("--name", default=None)
    token.add_argument("--email", default=None)
    token.add_argument("--dir", default=str(DEV_ISSUER_DIR))
    token.set_defaults(func=cmd_auth_dev_issuer_token)

    worker = sub.add_parser("worker", help="workers: claim runs from the database and advance them (docs/workers.md)")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    run = worker_sub.add_parser("run", help="run ONE worker in the foreground until stopped (Ctrl+C)")
    run.add_argument("--workspace-base", default="workspaces", help="parent directory of project workspaces on this host")
    run.add_argument("--lease", type=float, default=60.0, help="lease in seconds, 2 to 300 (default 60)")
    run.add_argument("--heartbeat", type=float, default=20.0, help="seconds between heartbeats, at most a third of the lease (default 20)")
    run.add_argument("--poll", type=float, default=2.0, help="seconds between claim attempts while there is no work (default 2)")
    run.add_argument("--step-budget", type=int, help="hand a run back after this many nodes (default: when it pauses or finishes)")
    run.add_argument("--max-runs", type=int, help="stop after this many claims")
    run.add_argument("--idle-exit", type=float, help="stop after this many seconds without work")
    run.set_defaults(func=cmd_worker_run)
    enqueue = worker_sub.add_parser("enqueue", help="create a run for an existing project (or take an existing run) and queue it")
    enqueue.add_argument("--project-id", required=True, help="a project in the database, e.g. created through the API")
    enqueue.add_argument("--run-id", help="the run to create, or an existing run to queue (default: a new run)")
    enqueue.add_argument("--scenario", choices=sorted(SCENARIOS), default="repair", help="fake-provider scenario for a new run")
    enqueue.add_argument("--workspace-base", default="workspaces", help="parent directory of project workspaces")
    enqueue.add_argument("--no-git", dest="git", action="store_false", help="do not git-init a new workspace")
    enqueue.set_defaults(func=cmd_worker_enqueue)

    notifications = sub.add_parser(
        "notifications", help="attention notifications: destinations, outbox status, the dispatcher (docs/notifications.md)"
    )
    notify_sub = notifications.add_subparsers(dest="notifications_command", required=True)
    destination = notify_sub.add_parser("destination", help="where a principal is told (addresses are shown redacted)")
    destination_sub = destination.add_subparsers(dest="destination_command", required=True)
    dest_add = destination_sub.add_parser("add", help="add an email address or phone number for a principal")
    dest_add.add_argument("--principal-id", required=True)
    dest_add.add_argument("--channel", required=True, choices=["email", "sms"])
    dest_add.add_argument("--address", required=True, help="an email address, or a phone number with its country code (+...)")
    dest_add.set_defaults(func=cmd_notifications_destination_add)
    dest_list = destination_sub.add_parser("list", help="destinations, redacted")
    dest_list.add_argument("--principal-id")
    dest_list.set_defaults(func=cmd_notifications_destination_list)
    for name, func in (("disable", cmd_notifications_destination_disable), ("enable", cmd_notifications_destination_enable)):
        toggle = destination_sub.add_parser(name, help=f"{name} a destination")
        toggle.add_argument("--id", required=True)
        toggle.set_defaults(func=func)
    status = notify_sub.add_parser("status", help="the notifications about one approval")
    status.add_argument("--project-id", required=True)
    status.add_argument("--approval-id", required=True)
    status.set_defaults(func=cmd_notifications_status)
    dispatch = notify_sub.add_parser("dispatch", help="run ONE notification dispatcher in the foreground until stopped")
    dispatch.add_argument("--provider", choices=["fake"], help="only the fake (DEVELOPMENT ONLY) exists so far")
    dispatch.add_argument("--fake-record", help="fake provider: append every call to this JSON-lines file")
    dispatch.add_argument("--fake-script", help="fake provider: scripted outcomes (JSON)")
    dispatch.add_argument("--dashboard-url", help="the dashboard origin notifications link to (no path, query, or credentials)")
    dispatch.add_argument("--lease", type=float, default=60.0, help="seconds a claimed notification is held (max 300)")
    dispatch.add_argument("--send-timeout", type=float, default=20.0)
    dispatch.add_argument("--poll", type=float, default=2.0)
    dispatch.add_argument("--sweep", type=float, default=60.0, help="seconds between attention sweeps")
    dispatch.add_argument("--batch", type=int, default=10)
    dispatch.add_argument("--max-attempts", type=int, default=8)
    dispatch.add_argument("--retry-base", type=float, default=30.0)
    dispatch.add_argument("--retry-cap", type=float, default=1800.0)
    dispatch.add_argument("--max-backoff", type=float, default=30.0)
    dispatch.add_argument("--idle-exit", type=float, help="stop after this many seconds without work (local development)")
    dispatch.set_defaults(func=cmd_notifications_dispatch)

    approve = sub.add_parser("approve", help="approve or reject a pending action")
    approve.add_argument("--workspace", required=True)
    approve.add_argument("--run-id", required=True)
    approve.add_argument("--request-id", required=True)
    approve.add_argument("--deny", action="store_true", help="reject instead of approve")
    approve.add_argument("--approver", default="human")
    approve.add_argument("--note", default="")
    approve.add_argument("--database", action="store_true", help="record the decision in the configured database first")
    approve.set_defaults(func=cmd_approve)

    reconcile = sub.add_parser(
        "reconcile-action", help="say whether an approved action with an uncertain outcome happened"
    )
    reconcile.add_argument("--workspace", required=True)
    reconcile.add_argument("--run-id", required=True)
    reconcile.add_argument("--execution-id", required=True)
    reconcile.add_argument("--attempt", type=int, required=True, help="the uncertain attempt shown by status")
    reconcile.add_argument("--fingerprint", required=True, help="the action fingerprint shown by status")
    reconcile.add_argument("--choice", required=True, choices=sorted(RECONCILIATION_CHOICES))
    reconcile.add_argument("--approver", default="human")
    reconcile.add_argument("--note", default="")
    reconcile.add_argument("--database", action="store_true", help="the run records its actions in the database")
    reconcile.set_defaults(func=cmd_reconcile_action)
    return parser


def main(argv: list[str] | None = None) -> int:
    from .worker.operator import RunOwnedElsewhere

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (
        ExecutorError,
        WorkspaceError,
        FileNotFoundError,
        DatabaseConfigError,
        StorageError,
        DomainError,
        ExecutionLedgerUnavailable,
        RunOwnedElsewhere,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OwnershipLost as exc:
        print(f"error: this command lost the run to a worker and stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
