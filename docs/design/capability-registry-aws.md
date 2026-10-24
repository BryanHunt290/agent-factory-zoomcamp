# Capability Registry and AWS: Phase A design

> **Status: Phase A approved. Stages B, C, D and E1 are implemented, offline**
> ([capabilities.md](../capabilities.md), [aws-capabilities.md](../aws-capabilities.md)).
> - E1 added `cloud_read`, contract-first, with the owner's approval, and the
>   real `BotoAwsBackend`, tested only against stubs.
> - No AWS package was installed, no AWS configuration was changed, and no
>   AWS API was called.
> - Official sources were read on **2026-10-06**. Every requirement below
>   comes from them, or from read-only checks of this machine.
>
> **Where the implementation differs from this design** (each one is the
> stricter choice, and each is explained in the docs linked above):
> 1. **Categories.** Only `cloud_read` was added (E1, owner-approved,
>    through the contract and migration 0009). `cloud_write` and
>    `iam_change` weren't: cloud mutations use the existing dangerous
>    categories, and IAM changes are a denied class of capability.
> 2. **Effect of a read.** `cloud_read` reads are declared read-only. Stage D
>    had used `network_access`, idempotent, until the category existed.
> 3. **Capability IDs.** These follow the owner's naming
>    (`core.files.read`, `core.tests.run`, `aws.identity.inspect`,
>    `aws.s3.metadata.read`), not the illustrative `code.*` names in
>    section 5.
> 4. **Approval levels.** The `approval` values are `policy` / `always` /
>    `denied`; section 5's `never` isn't offered.
> 5. **Out-of-scope profiles.** `default` and `internal-dev` can't be bound
>    at all.

Agent Factory is about to gain capabilities beyond its own workspace, AWS
first and then GitHub, Upwork, and others. This document settles how that
happens without weakening the model boundary:
- the model has no authority;
- actions go only through the Tool Gateway;
- approvals bind an exact action;
- ActionExecution records every approved effect;
- no credential reaches the model or the sandbox.

Four questions stay separate throughout:

| Question | Answered by |
| --- | --- |
| What can this installation, and this agent, actually do? | **Capability Registry** (new) |
| May this particular action run now? | **Tool Gateway** (policy, validation, ActionExecution) |
| Does this action need the owner's judgment? | **Human approval** (exact action, fingerprint, expiry) |
| What is Agent Factory's AWS identity allowed to do? | **IAM** (in AWS, outside Agent Factory) |

None of these may stand in for another. In particular, IAM allowing an
action never removes Agent Factory's approval requirement, and an approval
never widens IAM.

---

## 1. Local environment review (2026-10-06)

A read-only authoring-environment review informed this design. Private host
profile, credential-type and cache inventory is omitted from the public copy.
Existing account configuration was left untouched, and no AWS API call was
made. Agent Factory requires its separately authorized temporary identity;
the implemented blocked-profile rules remain documented in the runtime guide.

## 2. Official AWS tooling (sources read 2026-10-06)

"AWS Agent Toolkit" is, precisely, the **Agent Toolkit for AWS**. It was
announced on 2026-05-06 and is generally available.

| Offering | What it is | Relevance to Agent Factory |
| --- | --- | --- |
| **Agent Toolkit for AWS** | Tools, knowledge and guardrails for **AI coding agents** (Claude Code, Codex, Cursor, Kiro, other MCP clients). Four components: the AWS MCP Server, skills, plugins, and rules files. No extra charge; AWS resources used are billed normally. | A **development-time** aid for coding assistants. **Not** the runtime execution path for Agent Factory's agents (section 7). |
| **AWS MCP Server** (part of the toolkit) | A managed remote MCP server at `https://aws-mcp.{region}.api.aws/mcp` (us-east-1, **us-west-2**, ap-southeast-1/-2, ap-northeast-1, eu-central-1, eu-west-1/-2). Tools: knowledge (`aws___search_documentation`, `aws___read_documentation`, `aws___retrieve_skill`, `aws___list_regions`, `aws___get_regional_availability`) and API (`aws___run_script`, `aws___get_presigned_url`, `aws___get_tasks`). | The knowledge tools need no authentication. **`aws___run_script` runs model-written Python against AWS with the caller's IAM identity**, which is incompatible with exact-action approval (section 7). |
| **MCP Proxy for AWS** (`mcp-proxy-for-aws`, a library; `mcp-proxy-for-aws-cli`, a pinned CLI) | A local stdio proxy that signs MCP requests with SigV4 from the standard boto3 credential chain. Python **3.10+**; run with `uvx` or `pip`. Has `--read-only`, `--profile`, `--region`, `--metadata`. Exposes `aws_iam_streamablehttp_client` for custom agents. | Only needed if Agent Factory ever calls the MCP server with SigV4. Not proposed before Stage F. |
| **Toolkit CLI integration** | `aws configure agent-toolkit` (wizard; installs default skills and configures the MCP connection for detected coding agents) and `aws agent-toolkit list-installed-skills / search-skills / add-skill / update-skill / remove-skill`. Needs **AWS CLI 2.35.0+**. | Writes coding-agent configuration. Not for Agent Factory's runtime. Do not run it in this repository without a separate decision. |
| **AWS SDK for Python (boto3)** | The general SDK. Typed calls, one per API operation, using the standard credential chain. | **The proposed execution path** for AWS capabilities: one adapter per typed capability, behind the Tool Gateway. |
| **IAM** | Authorizes every AWS call, whatever the client. Calls through managed MCP servers carry the condition keys `aws:ViaAWSMCPService` (bool) and `aws:CalledViaAWSMCP` (string, such as `aws-mcp.amazonaws.com`). The older `aws-mcp:InvokeMcp`-style actions are being retired. | Least-privilege roles per capability family (section 8). |
| **`aws login`** (CLI sign-in) | Browser sign-in that issues **temporary** credentials, refreshed every 15 minutes, for sessions up to 12 hours. Cached in `%USERPROFILE%\.aws\login\cache`, usable by the CLI and SDKs. Needs **CLI 2.32.0+** and the `SignInLocalDevelopmentAccess` managed policy (or root). | One of two local options (section 9). |
| **IAM Identity Center** (`aws configure sso`) | Federated short-term credentials. AWS's recommended path for organizations with several accounts. | The other local option; preferred once there are dev, staging and production accounts. |
| **Amazon Bedrock AgentCore** (Runtime, Gateway, Identity, Memory) and **Strands Agents** | AWS's managed agent-hosting platform (Gateway turns APIs and Lambda functions into agent tools; Identity holds a token vault), and AWS's open-source agent SDK. | Alternative agent *frameworks*. Agent Factory already is the orchestrator, and adopting either would move authority into a framework this project doesn't control. **Not proposed.** Revisit only for cloud hosting (section 10). |

Requirements, per component:

| | AWS MCP Server, OAuth | AWS MCP Server, SigV4 | `aws configure agent-toolkit` | boto3 adapters (proposed) |
| --- | --- | --- | --- | --- |
| AWS CLI | Not needed | 2.32.0+ (for `aws login`) | **2.35.0+** | Not needed (CLI 2.32.0+ only for `aws login` credentials) |
| Python | Not needed | 3.10+ (proxy) | Not stated | 3.12 (the project's) |
| uv/uvx | No | Yes | Not stated | No |
| Node.js | No | No | No | No |
| Docker | No | No (an optional proxy image exists) | No | No (and AWS calls never run in the sandbox) |
| WSL/Linux | No | No | No | No |
| Windows | Yes (client side) | Yes (`uv` has a Windows installer) | Not stated explicitly | Yes |
| AWS account / identity | Yes, plus `AWSMCPSignInOAuthAccessPolicy` | Yes (any SigV4 credentials) | Yes | Yes (a least-privilege role) |

Sources (all read 2026-10-06):
- [What is the Agent Toolkit for AWS?](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/what-is-agent-toolkit.html)
- [Agent Toolkit components](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/components.html)
- [Setting up the AWS MCP Server](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/getting-started-aws-mcp-server.html)
- [Understanding the MCP Server tools](https://docs.aws.amazon.com/aws-mcp/latest/userguide/understanding-mcp-server-tools.html)
- [AWS MCP Server overview](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/mcp-server.html)
- [Announcement, 2026-05-06](https://aws.amazon.com/about-aws/whats-new/2026/05/agent-toolkit/)
- [Get started with the Agent Toolkit in the AWS CLI](https://aws.amazon.com/blogs/developer/get-started-with-the-agent-toolkit-for-aws-in-the-aws-cli/) (dated 2026-08-26)
- [Understanding IAM for managed AWS MCP servers](https://aws.amazon.com/blogs/security/understanding-iam-for-managed-aws-mcp-servers/) (dated 2026-03-02)
- [MCP Proxy for AWS](https://github.com/aws/mcp-proxy-for-aws) (no current release number stated on the page; Docker images tagged `1.1.6` given as an example)
- [Login for AWS local development](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)
- [Amazon Bedrock AgentCore overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)

Not verified: an operating-system support matrix for `aws configure
agent-toolkit` (none was found), and the proxy's exact current version.

## 3. AWS CLI decision

**The installed CLI is sufficient: leave it alone.**

| Requirement | Minimum | Installed | Result |
| --- | --- | --- | --- |
| `aws login` (temporary local credentials) | 2.32.0 | 2.36.8 | Met |
| `aws configure agent-toolkit` / `aws agent-toolkit …` | 2.35.0 | 2.36.8 | Met |
| The proposed boto3 adapters | none | — | Not required |

The CLI is a **human** tool here, for signing in and inspecting. It is
never an agent tool: no capability runs `aws <model-generated arguments>`
(section 7).

## 4. Capability Registry architecture

```
Real Claude ──► Agent ──► (structured answer: data, never an action)
                              │
                              ▼
                    Capability Registry ── "what exists, is it available, with what evidence?"
                              │  (read by the manifest builder, the planner's context, assessments)
                              ▼
                        Tool Gateway ── policy · validation · approval · ActionExecution · audit
                              │
            ┌─────────────────┼──────────────────┐
            ▼                 ▼                  ▼
   CoreToolPack         GitToolPack          AwsToolPack           (future: GitHub, Upwork, …)
   filesystem, tests    workspace git        typed boto3 adapters
   (sandbox for code)                         │
                                              ▼
                                     IAM (least-privilege role) ──► AWS
```

- **The registry is provider-neutral.**
  - Its core knows nothing about AWS: no regions, ARNs or IAM terms.
  - Anything AWS-specific lives in `AwsToolPack` and its availability probe.
  - A pack contributes capabilities as data, against one typed schema.
- **The registry describes; it never authorizes.** A capability being
  *available* only means the Gateway *can* be asked. Every call is still
  decided by the Gateway.
- **The manifest is derived from the registry.** Today's
  `CapabilityManifest` (built from policy and the tool registry) becomes a
  view over the Capability Registry, filtered by the agent's permissions.
  It's still description only, still built from trusted sources, and
  project content still can't add to it.
- **A capability is not a model tool.** This is the lesson of the prompt
  `/2` correction ([model-providers.md §4a](../model-providers.md#4a-model-provider-tools-versus-agent-factory-capabilities)).
  - The model gets capability *descriptions*, and exactly one provider
    tool (`submit_result`).
  - To ask for AWS work, it expresses typed intent in its structured
    answer, for example a `requested_actions` entry naming `aws.s3.read`
    with typed parameters. That needs new prompt and schema versions.
  - The orchestrator validates the intent and turns it into Gateway calls.
  - No capability becomes a provider-callable function without its own
    design review.

## 5. Capability schema

One frozen, typed record per capability **version**:

| Field | Type | Meaning |
| --- | --- | --- |
| `capability_id` | dotted string: `code.read`, `tests.run`, `git.commit`, `aws.s3.read`, `aws.logs.read`, `aws.deploy.execute`, `github.pull_request.create` | Stable name. Namespaces belong to packs. |
| `version` | int | Changes when inputs, outputs, effects or permissions change. Recorded with every call. |
| `pack` | string (`core`, `git`, `aws`, …) | The pack that registered it. One owner per namespace. |
| `description` | ≤ 200 characters, maintainer-written | What the model may read. No endpoints, ARNs, account IDs or paths. |
| `implemented_by` | tool name in the `ToolRegistry` | The one implementation the Gateway dispatches to. |
| `category` | existing `ActionCategory` (plus new ones such as `cloud_read`, `cloud_write`, `iam_change`) | Feeds the existing permission policy. |
| `effect` | existing `ExecutionSemantics` (`read_only` / `idempotent` / `non_idempotent`, idempotency natural or keyed) | Feeds ActionExecution, unchanged. |
| `approval` | `never` / `policy` / `always` | A pack can only *tighten*, never loosen, the category's policy floor (dangerous categories are always approval). |
| `cost_impact` | `none` / `low` / `unknown` / `recurring` | See section 13. `unknown` and `recurring` force approval. |
| `risk` | `low` / `medium` / `high` / `critical` | For people and assessments. Never lowers a policy decision. |
| `network` | `none` / `trusted_adapter` | `trusted_adapter`: network used by Agent Factory's own adapter, never by the sandbox. |
| `credential` | `none` / a named credential **slot** (such as `aws:dev-read`) | A slot names *which* identity, never holds it. |
| `sandbox_compatible` | bool | Whether it may run inside the generated-code sandbox (AWS: always false). |
| `input_schema`, `output_schema` | strict JSON schemas (the existing `agents/schema.py` subset) | Typed parameters only. No free-text commands. |
| `required_permissions` | list of strings, pack-defined (for AWS, IAM actions such as `s3:GetObject`) | Documentation for the least-privilege policy, and an assessment input. Never used to authorize. |
| `tags` | short strings | Search and grouping. |
| `evidence` | see below | What is *proven*, not claimed. |

**Evidence.** Each item is computed, never asserted:

| State | Set by |
| --- | --- |
| `IMPLEMENTED` | The pack registered an implementation. |
| `TESTED` | A named offline test exists and is in the default suite (checked by a registry test). |
| `LIVE_TESTED` | A recorded live acceptance result (date, suite, commit) exists for this capability version. |
| `AVAILABLE_NOW` | The availability probe passed within its time limit (section 14). |
| `REQUIRES_APPROVAL` | Policy plus `approval` plus `cost_impact`. |
| `UNAVAILABLE` + `reason` | A probe failed or the pack is disabled. The reason is one of a fixed set: `not_configured`, `no_identity`, `permission_denied`, `unsupported_region`, `dependency_missing`, `probe_failed`. |

Example (illustrative, not implemented):
```json
{"capability_id": "aws.s3.read", "version": 1, "pack": "aws", "implemented_by": "aws_s3_get_object",
 "category": "cloud_read", "effect": {"class": "read_only"}, "approval": "policy", "cost_impact": "low",
 "network": "trusted_adapter", "credential": "aws:dev-read", "sandbox_compatible": false,
 "required_permissions": ["s3:GetObject", "s3:ListBucket"],
 "evidence": {"implemented": true, "tested": true, "live_tested": false,
              "available_now": false, "unavailable_reason": "no_identity"}}
```

**Registration rules**, each a test:
- duplicate `capability_id@version` is refused;
- a namespace owned by another pack is refused;
- a missing `ExecutionSemantics` on a dangerous category is refused (as today);
- `approval` may never be below the policy floor;
- `cost_impact` `unknown` or `recurring` with `approval: never` is refused;
- `sandbox_compatible: true` together with a `credential` slot is refused.

## 6. Tool packs

A pack is one unit of registration:
- its capabilities (section 5);
- their tool implementations (`Tool` subclasses, registered in the `ToolRegistry`);
- a permission profile (the categories it uses and the IAM actions it documents);
- an availability probe;
- its offline tests and fakes.

| Pack | Capabilities | Status |
| --- | --- | --- |
| `CoreToolPack` | `code.read`, `code.list`, `code.write`, `code.delete`, `tests.run` | Exists today as loose tools; Stage C migrates it, behaviour unchanged |
| `GitToolPack` | `git.commit` (workspace only, through `Workspace._git`) | Exists today; migrated with Core |
| `AwsToolPack` | `aws.identity.read`, then `aws.logs.read`, `aws.s3.read`, … | Stages D to G |
| `GithubToolPack` | `github.repo.read`, `github.pull_request.create` | Future; separate design |
| `UpworkToolPack` | none: assessment only (section 17) | Future; separate design |

A pack is enabled by explicit configuration, as the model provider is
today (an `AGENT_FACTORY_PACKS`-style setting). It's never enabled because
a credential happens to be in the environment. A disabled pack's
capabilities are listed as `UNAVAILABLE` (`not_configured`).

## 7. AWS adapter boundary

**Proposed: typed boto3 adapters behind the Tool Gateway. Not the AWS MCP
Server, and not the CLI.**

- One `Tool` per typed capability.
  - Example: `aws_logs_filter_events(log_group, start, end, pattern ≤ 200 chars)`.
  - Each makes **one** boto3 call (or a bounded, paginated series) of
    **one** fixed operation. The operation name is in the code; it never
    comes from model output.
- The adapter runs in the trusted Agent Factory process (the worker),
  **outside** the sandbox.
- It gets its boto3 session from a credential slot (section 9), and the
  session never leaves the adapter.
- Results are bounded, summarized and redacted before they enter state, the
  log, or any later model context: no ARNs with account IDs, no
  credentials, sizes capped.
- Errors map to fixed categories:
  - `AccessDenied` → `permission_denied`;
  - throttling → transient, bounded retry;
  - `ExpiredToken` → `no_identity`;
  - everything else → `aws_error`, with a fixed message.

  Raw exception text never reaches the API or the model.

Rejected, with reasons:

| Option | Why not |
| --- | --- |
| Give the model `aws <args>` (the CLI as a shell tool) | Arbitrary operations, arbitrary arguments, the user's full identity. No exact-action approval is meaningful. |
| AWS MCP Server `aws___run_script` as an agent capability | Executes **model-written Python** against AWS under the caller's identity, in AWS's sandbox. One approval would cover an unbounded program. It can't be bound to a typed fingerprint, its idempotency can't be declared, and ActionExecution can't record its effects one by one. |
| Expose AWS MCP tools as provider-callable tools | Repeats the capability-as-tool confusion fixed by prompt `/2`. Moves action selection into the model. |
| Bedrock AgentCore Gateway / Strands as the tool layer | Moves authority into a framework outside this project's guarantees (approvals, fencing, ActionExecution). |

Possible later use, read-only:
- The MCP Server's **unauthenticated knowledge tools**
  (`aws___search_documentation`, `aws___read_documentation`) could become a
  `docs.aws.search` capability: read-only, no credential, network only in
  the adapter.
- That is a small, separate decision (Stage F+). It isn't needed for any
  AWS action.

For people (the owner, or a coding assistant working on *this*
repository), the Agent Toolkit plugin for Claude Code is a reasonable
development aid. That's a decision about the owner's own tools, not about
Agent Factory, and it's outside this design.

## 8. IAM architecture (least privilege)

Never `AdministratorAccess`, `PowerUserAccess`, or `*:*` for Agent Factory.

- **One IAM role per capability family and environment**, assumed only
  for the capabilities that need it:

  | Role (illustrative) | Allows | Notes |
  | --- | --- | --- |
  | `af-dev-identity` | `sts:GetCallerIdentity` | Needs no policy at all (it can't be denied); used by the probe |
  | `af-dev-read` | `logs:FilterLogEvents`, `logs:DescribeLogGroups` on named log-group ARNs; `s3:GetObject`/`s3:ListBucket` on one bucket and prefix; `cloudformation:Describe*` on named stacks | No `*` resources where the service supports resource ARNs |
  | `af-dev-deploy` | Only the actions one deployment method needs, on named stacks | Approval always; separate role |
  | `af-staging-*`, `af-prod-*` | The same families, separate accounts | Production: no long-running sessions, MFA on the human approver's side, stricter conditions |

- **Guardrails above the roles:**
  - a permissions boundary on every `af-*` role;
  - in an organization, an SCP that denies `iam:*`, `organizations:*` and
    `account:*` to `af-*` roles;
  - explicit denies of deletion of protected resources, by tag condition;
  - an explicit deny of anything arriving through a managed MCP server
    (`aws:ViaAWSMCPService = true`), since Agent Factory never uses that
    path.
- **IAM changes are never a capability** for agents. `iam_change` is a
  category whose policy floor is **deny** for every agent. It can be raised
  only to "approval" by explicit owner configuration, and no pack ships it.
- **Accounts:** separate `development`, `staging` and `production` accounts
  in the end. Phase A assumes one development account. Production
  mutations come last, behind the strongest policy.

## 9. Local credential strategy

**Agent Factory never reads `~/.aws/credentials` long-term keys, and never
uses the `default` profile implicitly.**

- **A credential slot** is a configuration entry that names a profile,
  such as `AGENT_FACTORY_AWS_PROFILE_DEV_READ=af-dev-read`.
  - boto3 resolves it through the standard chain inside the adapter.
  - The adapter **refuses a profile that resolves to long-term keys**: the
    probe checks the provider method (for example `shared-credentials-file`
    versus `sso` or `login` / `assume-role`) and reports `not_configured`.
    That is fail closed.
- **The profile itself** should be one of two kinds:
  - **IAM Identity Center** (`aws configure sso`), with a permission set per
    family. Preferred once there's an organization.
  - **`aws login`** for a development IAM user or role, with `af-dev-*`
    roles assumed from it (`role_arn` plus `source_profile`). Temporary,
    refreshed every 15 minutes, 12-hour maximum.
- **What the owner must decide:** retiring or keeping the existing
  long-term keys. This design neither uses nor changes them.
- **Separation:**
  - AWS credentials and the Anthropic key are separate files and slots;
    neither can be read by the other's code.
  - Verified in today's code:
    - the sandbox rejects `AWS_` names in its environment
      (`sandbox/validation.py`);
    - the context builder withholds `.aws/` files (`providers/context.py`);
    - the request guard refuses the values of environment variables whose
      names look secret, which covers `AWS_ACCESS_KEY_ID`,
      `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN`
      (`providers/guard.py`).
  - Stage D adds value patterns that don't depend on the environment:
    `AKIA…`/`ASIA…` key IDs, `aws_secret_access_key =` lines, and session
    tokens.
  - The API and the dashboard never show a profile's identity beyond the
    slot name and its availability.

## 10. Cloud credential strategy

When Agent Factory runs in AWS (not before a deployment design):
- **The workload's IAM role**: an ECS task role, or an EC2 instance
  profile. On EKS, pod identity.
- **No access keys anywhere:** not in images, environment variables, or
  configuration.
- **The worker role** may only `sts:AssumeRole` into the `af-{env}-*`
  family roles, with session tags (run ID, capability) so that CloudTrail
  ties every AWS call to an Agent Factory run.
- **The API process gets no AWS role at all.** The API records intent;
  workers act.
- **Bedrock AgentCore Identity** (a token vault) could later hold
  non-AWS credentials (GitHub, Upwork). That's a separate evaluation.

## 11. Approval policy

| Class | Examples | Default |
| --- | --- | --- |
| Identity and read metadata | `aws.identity.read`, describe resources, deployment status | allow (still logged) |
| Read data | `aws.logs.read`, `aws.s3.read` (designated bucket and prefix) | allow in development; approval in production |
| Mutate, low cost | write an object under the designated prefix | approval |
| Create resources, `unknown` or `recurring` cost | create a stack or service | approval, always |
| Deploy | `aws.deploy.execute` (`production_deploy`) | approval, always (dangerous category) |
| Delete | any AWS deletion (`destructive_delete`) | approval, always |
| Spend | anything with a direct charge (`spend_money`) | approval, always |
| IAM, credentials, organizations | `iam:*`, keys, policies | **deny** (no pack offers them) |

These follow the existing rule: dangerous categories are never
auto-allowed. Approvals bind the exact action:
- capability ID and version;
- every typed parameter;
- the target account and region (as fingerprint parameters);
- the credential slot.

They expire after 12 hours, as today.

## 12. ActionExecution integration

AWS mutations use the existing machinery unchanged:
```
model intent ─► validate (schema) ─► Gateway: policy ─► approval (exact fingerprint) ─► notification
     ─► owner approves in the dashboard ─► worker re-checks ─► ActionExecution write-ahead record
     ─► adapter: one typed AWS call ─► outcome recorded (succeeded / failed / unknown)
```
- **Execution semantics, declared per capability:**
  - read-only for reads;
  - `keyed` idempotent where the AWS API takes a client token (for
    example CloudFormation `ClientRequestToken`, or `ClientToken` on
    EC2/ECS creates). The token is the existing ActionExecution
    idempotency key;
  - otherwise non-idempotent.
- **An unknown outcome of a non-idempotent mutation is never retried.**
  The run waits for human reconciliation, as today.
- **Fencing:** the worker's claim is proved before dispatch, as today.

## 13. Cost and risk metadata

- `cost_impact`: `none` (reads of metadata), `low` (bounded reads,
  requests), `unknown`, or `recurring` (creates something that bills while
  it exists).
- `unknown` or `recurring` → approval always. The approval screen shows
  the label; no live estimate in Phase A.
- **Later:** AWS Budgets alerts on the development account (the owner's
  configuration), and a per-run AWS action ceiling in Agent Factory
  (number of mutating calls), similar to the model budget. A dollar
  estimate is not attempted.

## 14. Availability probes

A probe answers "is this capability usable right now?" safely. Rules:
- **Read-only and free.**
  - The identity probe is `sts:GetCallerIdentity`, which can't be denied
    and changes nothing.
  - Family probes use a cheap describe or list on the designated resource
    (for example `logs:DescribeLogGroups` with a name prefix), or are
    skipped and marked `untested_permission`.
- **No secrets in output.** A probe reports the slot, the credential
  *kind* (`sso` / `login` / `assume-role` / `refused:long-term-keys`), the
  region, and a fixed status. It never reports key IDs, tokens, or the
  account ID; at most a hash, if the owner asks.
- **Configuration and permission are kept apart:**
  - `not_configured`: no slot, or a refused credential kind;
  - `no_identity`: expired or invalid credentials;
  - `permission_denied`: `AccessDenied` on the family probe;
  - `unsupported_region`;
  - `dependency_missing`: boto3 absent;
  - `probe_failed`: network or timeout.
- **Cached briefly** (minutes), with the time of the check shown. A
  probe's result is evidence; it never authorizes.
- **On demand:** the CLI (`capabilities check`), not on every run.

## 15. Failure behaviour (fail closed)

| Failure | Result |
| --- | --- |
| boto3 missing, pack disabled, no slot | Capability `UNAVAILABLE`; the run never sees it as offered |
| Identity missing or expired | `UNAVAILABLE` (`no_identity`); a mutating step already approved **blocks** (the approval is not consumed) |
| Permission denied at call time | Tool error `permission_denied`. The node fails or blocks; never a retry under another identity |
| Unsupported region | `UNAVAILABLE` |
| Throttling or 5xx on a read | Bounded retry (the existing transient policy) |
| Unknown outcome of a non-idempotent mutation | Reconciliation by a person; never resent |
| Anything uncertain | Do not act |

There is never a fallback to the CLI, to a shell, to another profile, to
`aws___run_script`, or to the sandbox with network.

## 16. Test strategy (for Stages B to G)

| Area | Tests |
| --- | --- |
| Registry | registration; duplicate ID/version refused; foreign namespace refused; versions recorded; unavailable capability listed with its reason; permission metadata present; effect semantics required; evidence computed (`TESTED` checks a named test exists) |
| Model boundary | the manifest from the registry holds descriptions only; no credential, ARN or account ID in any request (canaries); project text cannot add or enable a capability; an `UNAVAILABLE` capability named in model intent is refused by the Gateway; still one provider tool (`submit_result`) |
| Tool Gateway | unregistered capability denied; permission denied; approval-required pauses with an exact fingerprint; a side effect without an ActionExecution record is impossible (static and runtime) |
| AWS (offline: a fake boto3 client via `botocore.stub.Stubber`, or an injected fake session) | credentials never enter a model request, the sandbox env, logs, the API or the dashboard (canaries); an "arbitrary CLI command" intent is rejected by schema; read/write classification enforced per capability; a mutating capability requires approval; long-term-key profiles are refused by the probe; error mapping per category; client tokens reused on retry |
| Fail closed | toolkit or boto3 missing; identity missing; permission missing; unsupported region; API failure; expired credentials mid-run |
| Live (opt-in `--run-aws-live`, owner authorization, development account only) | Stage E: identity probe only. Stage F: one read capability against a designated resource. Stage G: one approval-gated mutation of a designated, disposable, zero-cost resource, then cleanup verified |
| Mutation checks | every guard above, broken on purpose |

No live AWS mutation test before Stage G, and none without its own
authorization.

## 17. Future Upwork capability assessment (boundary only)

```
Upwork job ─► requirements (data, untrusted) ─► Agent Factory assessment
                                                    │
         per requirement: supported (evidence) │ missing │ needs human work │ needs approval
                                                    ▼
                                     a confidence derived from evidence
```
- **A requirement maps to capability IDs.**
  - The mapping may be proposed by the model.
  - Its *answer* comes from the registry: `IMPLEMENTED` + `TESTED` +
    `LIVE_TESTED` + `AVAILABLE_NOW`, and whether approval is needed.
- **The model's opinion is one input,** and it is labelled as such. "Claude
  thinks it can" is never reported as "Agent Factory can".
- **The assessment is a read-only report.** It submits nothing to Upwork,
  bids nothing, and promises nothing. Any external communication is
  `external_communication`, which always needs approval.

## 18. Proposed implementation stages

Ordered so that each stage is useful on its own and nothing touches AWS
before the boundary is proven offline.

| Stage | Scope | Needs from the owner |
| --- | --- | --- |
| **A** (this) | Discovery and design | **Done** (approved) |
| **B** | Provider-neutral Capability Registry: schema, registration rules, evidence computation, the manifest as a registry view. Offline only, no new dependency. | **Done** ([capabilities.md](../capabilities.md)) |
| **C** | `CoreToolPack` and `GitToolPack` migration with **no behaviour change** (the same tools, policy and prompts), proven by the full existing regression | **Done** |
| **D** | `AwsToolPack` foundation with a **fake** AWS backend: credential slots, the probe logic, error mapping, `aws.identity.inspect` and `aws.s3.metadata.read`, a non-executable `aws.deploy.execute`, offline tests and canaries | **Done** ([aws-capabilities.md](../aws-capabilities.md)); `requirements-aws.txt` pins the installed set and is not imported by any code yet |
| **E1** | `cloud_read` contract-first; the real `BotoAwsBackend` (STS GetCallerIdentity only), explicitly activated, with no default chain; the live-evidence mechanism; offline only | **Done** ([aws-capabilities.md](../aws-capabilities.md)) |
| **E2** | Live identity check on a development account. Prepared 2026-10-06 ([aws-e2-setup.md](../aws-e2-setup.md)); split into **E2a**, an opt-in live-verification runner in `tests/` built and tested offline (**built 2026-10-07**: `tests/aws_live_support.py`, `tests/test_aws_live.py`, 79 offline tests; never run live), and **E2b**, the authorized call through Registry → Tool Gateway → `BotoAwsBackend` → STS | **The owner sets up IAM Identity Center** and a new `agent-factory-dev` profile ([aws-e2-setup.md](../aws-e2-setup.md); first checking whether the account is on the Free plan, whose credits creating an organization ends); decides the Free-plan question (unresolved; the organization is required for permission sets); **then authorizes E2b** (free: one SSO credential exchange and two `GetCallerIdentity` calls; Agent Factory keeps only the principal type and name and the account's last four digits) |
| **F** | First controlled live read (for example `aws.logs.read` on one designated log group), behind the Gateway, with evidence recorded | The owner creates `af-dev-read` (a policy is proposed for review, not applied by Agent Factory), names the resource, and authorizes the live read suite |
| **G** | First approval-gated mutation (for example writing one object under a designated, disposable prefix), through approval, notification, ActionExecution and outcome, then cleanup | The owner creates `af-dev-write-sandbox`, authorizes the live mutation test, and sets a cost ceiling |
| Later | `docs.aws.search` (MCP knowledge tools, unauthenticated); GitHub pack; Upwork assessment; staging and production accounts; cloud workload identity | Separate designs |

**Exact installs, configuration and network access needed after Phase A:**
- **B, C:** none.
- **D:** pin `boto3==1.43.57` (and `botocore`) in a new
  `requirements-aws.txt`. They're already present globally, so no network
  is needed unless the owner wants a clean venv install.
- **E:** the owner configures a temporary-credential profile (Agent Factory
  doesn't). Then one authorized `sts:GetCallerIdentity` call to
  `sts.us-west-2.amazonaws.com`.
- **F, G:** owner-created IAM roles and policies, and named resources.
  Live calls only to the specific service endpoints, under a live-suite
  authorization each time.
- **Never needed:**
  - an AWS CLI update;
  - `uv`, `uvx`, `mcp-proxy-for-aws`;
  - `aws configure agent-toolkit`;
  - Node.js;
  - Docker changes;
  - WSL;
  - sandbox network access.
