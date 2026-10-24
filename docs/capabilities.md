# Capabilities: the registry, tool packs, evidence, and assessment

> **Status:** Stages B, C and E1 of [design/capability-registry-aws.md](design/capability-registry-aws.md),
> implemented and tested offline. E1 added the `cloud_read` category and the live-evidence file. The AWS
> pack (fake backend, plus a real backend tested only offline) is in [aws-capabilities.md](aws-capabilities.md).

Agent Factory can now answer, from facts, **what this installation can actually do**. It doesn't need to
ask a language model.

Four questions stay separate, each answered by exactly one component:

| Question | Answered by | Code |
| --- | --- | --- |
| What exists, is it available now, and what proves it? | **Capability Registry** | `agent_factory/capabilities/` |
| May this particular action run now? | **Tool Gateway** (policy, approval, ActionExecution, audit) | `tools/gateway.py` |
| Does this exact action need the owner's judgment? | **Human approval** (exact fingerprint, expiry) | `safety/`, `domain/approvals.py` |
| What may a cloud identity do? | **IAM**, in the cloud, outside Agent Factory | — |

```
Agent ──(structured answer)──► Capability Registry ──► Tool Gateway ──► tool implementation
                                "what exists, proven?"   "may it run now?"
                                                          policy · approval · ActionExecution · audit
```

## 1. A capability

A capability is one immutable `Capability` (`capabilities/model.py`). Its fields:

| Field | Meaning |
| --- | --- |
| `capability_id` | Namespaced and stable: `core.files.read`, `git.commit`, `aws.identity.inspect`. Lower-case, dotted, 2 to 6 segments. The first segment is the owning pack. |
| `version` | A positive integer, recorded with the definition. |
| `pack` | The owning tool pack. A pack may only register its own namespace. |
| `description` | At most 200 characters, maintainer-written. ARNs, 12-digit numbers, key-like strings, URLs, host paths and credential words are refused. |
| `implemented_by` | The one tool that performs it, or `None` for a definition with no implementation (never available). |
| `category` | The existing `ActionCategory`. It must equal the tool's. |
| `effect` | The existing execution class (`read_only` / `idempotent` / `non_idempotent`). It must equal the tool's declared semantics; an undeclared tool counts as `non_idempotent`. |
| `approval` | `policy` (follow the permission policy), `always` (a person, whatever the policy says), or `denied` (never; not even approval enables it). |
| `cost` | `none` / `low` / `unknown` / `recurring`. |
| `risk` | `low` / `medium` / `high` / `critical`. |
| `mutates` | Whether it changes state beyond the run's own records. |
| `network` | `none`, or `trusted_adapter`: network used by Agent Factory's own adapter, **never** the sandbox. |
| `credential` | An abstract slot such as `aws.dev.readonly`, or `None`. Never a profile name, never a credential. |
| `sandbox_compatible` | May it run inside the generated-code sandbox? Never true together with a credential or the network. |
| `input_schema`, `output_schema` | Strict schemas in the subset `agents/schema.py` validates. The input is always a closed object: typed parameters only, never a free-text command. |
| `required_permissions` | Exactly `tool:<name>` and `category:<category>`, checked against the tool. |
| `iam_actions` | For cloud capabilities: the exact IAM actions, documented. Never a wildcard, never used to authorize. |
| `tests` | Named offline tests (`tests/<file>.py::test_<name>`). |
| `documentation` | Documentation references. **Never counted as evidence.** |
| `tags` | Grouping. |

Definitions are frozen: the fields can't be changed, and schemas become read-only mappings and tuples.

### Validation (fails closed)

A definition or registration is **refused**, never repaired, when:
- the ID is malformed or in another pack's namespace, or the version isn't a positive integer;
- the ID is a duplicate: one installation has one version of each ID;
- the implementing tool is missing, implements another capability, or is in the pack without being
  claimed by any capability;
- the category differs from the tool's;
- **the effect is weaker than the tool's semantics.** A capability can't make a tool look harmless;
- the input schema doesn't name exactly the tool's parameters;
- a schema uses an unsupported keyword or type, or `required` names an undefined property;
- read-only is combined with `mutates`; or a recurring cost with no mutation;
- a mutation with `unknown` or `recurring` cost isn't `always` approval;
- a dangerous category (deletion, spend, deployment, credentials, external communication, network) is
  declared with `approval: policy`;
- an IAM action is a wildcard;
- an action changes identity, credentials or the organization (`iam:`, `organizations:`, `account:`,
  `sso:`, `sts:AssumeRole`, …) or is in `credential_change`, and the capability isn't `denied`;
- a sandbox-compatible capability needs a credential or the network;
- a network-using mutation follows the policy's allow (it must be `always` or `denied`);
- a `cloud_read` capability breaks a cloud-read invariant (section 1a).

### 1a. `cloud_read`

`cloud_read` (contract-first in `openapi.yaml`, [api-contract.md](api-contract.md#action-categories))
is read-only access to an external cloud system through a typed, trusted adapter. **It isn't a local
read.** It uses the network and a cloud identity, and may disclose cloud data.

**Its rule depends on the environment** (`PermissionPolicy.for_environment`):

| Environment | Rule |
| --- | --- |
| development (the default policy) | allow |
| staging | configurable; approval by default |
| production | approval; a production policy that allows it is refused at construction |

**Invariants, refused at registration if broken:**
- read-only effect, and `mutates: false`;
- cost `none` or `low`;
- `trusted_adapter` network, never the sandbox;
- an approved credential slot;
- IAM actions documented, and only read verbs (`Get`, `List`, `Describe`, `Head`, `Lookup`);
- every input string and list bounded;
- a closed, bounded output.

The tool registry also refuses a `cloud_read` tool that doesn't declare read-only semantics, so a
mutating tool can't masquerade as one. A capability can still tighten it (`always`, `denied`).

## 2. Evidence

`CapabilityRegistry.evidence(id)` answers **why** Agent Factory believes something. Each item has a kind
and a provenance:

| Kind | Granted only when | Source |
| --- | --- | --- |
| `IMPLEMENTED` | A registered tool implements it | `tool:<name>` |
| `TESTED` | A named test **exists** in `tests/`, found by parsing the test file, never by running or trusting it | the test ID |
| `LIVE_TESTED` | An authorized live verification of **this version, in this environment, by a real backend the pack declares**, recorded in the reviewed evidence file | `verification:<id>`, the backend, suite, commit, time |
| `AVAILABLE_NOW` | The pack's probe (and the capability's own probe, if any) passed | `probe:<pack>`, the time |
| `REQUIRES_APPROVAL` | The policy floor or the capability's own rule needs a person | `policy:<category>` |
| `UNAVAILABLE` | Otherwise, with a fixed reason | `probe:<pack>` |

Documentation never counts, and nothing holds a secret.

### Live-tested evidence (`capabilities/live_evidence.py`)

**The source:** the registry reads `LIVE_TESTED` only from `evidence/live-capabilities.jsonl`, in the
repository, where it's reviewed and committed. Packs can't supply it, and unit tests can't create it.

**The writer:** `append_live_verification` is the only writer.
- It requires an authorization naming the same capability, version and environment.
- It refuses a `fake`, `stub`, `loopback` or `test` backend.

**Each record holds:**
- the capability ID and version;
- the backend kind;
- the verification ID and suite;
- the environment;
- the commit and time;
- a SHA-256 digest of the safe result, never the result;
- who authorized it;
- the previous record's hash.

**Validity:**
- A malformed record, a broken hash, or a broken chain (a removed record, for example) makes the
  **whole file untrusted**, so no `LIVE_TESTED` at all.
- A record counts only for its exact version, in the registry's environment (`dev`, `staging` or
  `prod`), from a backend kind in the pack's `live_backends`.
- **Live-tested is not available:** a record never makes a capability `AVAILABLE_NOW`. Only a probe
  does.

The file doesn't exist yet: no capability is `LIVE_TESTED`, and a test checks that.

## 3. Availability

**Implemented and available are different questions.** `availability(id)` returns one fixed status:

| Status | Meaning |
| --- | --- |
| `available` | Usable now |
| `not_configured` | The pack isn't enabled, or a slot or backend isn't configured |
| `no_identity` | Configured, but no valid identity (missing, expired, or a refused credential kind) |
| `permission_denied` | The identity exists but may not do it |
| `unsupported_region` | — |
| `dependency_missing` | — |
| `probe_failed` | The probe raised an error or returned nothing usable |
| `not_implemented` | A definition with no implementation |

- **Probes** must be read-only, free and secret-free. A probe that fails or misbehaves makes the
  capability unavailable, never available.
- **When probes run:** when the registry is built (once per executor) and on `refresh()`.
- **The local packs** are always available. Docker health is still checked at run time by the sandbox
  manager, as before; it isn't a probe yet.

## 4. Tool packs

A `ToolPack` (`capabilities/packs.py`) owns one namespace and provides:
- its capability definitions;
- the tools that implement them;
- a probe, and optional per-capability probes;
- `live_backends`: which real backend kinds may count as live evidence for it (none for the local packs;
  `boto` for AWS).

| Pack | Capabilities | Enabled |
| --- | --- | --- |
| `CoreToolPack` | `core.files.list`, `core.files.read`, `core.files.write`, `core.files.delete`, `core.tests.run` | Always (local) |
| `GitToolPack` | `git.commit` (workspace git only, through `Workspace._git`) | Always (local) |
| `AwsToolPack` | `aws.identity.inspect`, `aws.s3.metadata.read`, `aws.deploy.execute` (definition only) | Only with explicit settings ([aws-capabilities.md](aws-capabilities.md)) |

- **Enabling a pack is explicit,** in code. A credential in the environment or in `~/.aws` never
  enables anything.
- **A disabled pack** still lists its capabilities, as `not_configured`, and contributes **no tool**
  to the tool registry.

**The Core and Git packs wrap the existing tools unchanged:** the same classes, categories, semantics,
descriptions, workspace confinement, protected paths, sandbox routing and git confinement.
- `default_registry()` and `build_executor()` now get their tools from the packs.
- The model's capability manifest is identical, for every agent, to the one before (a test compares
  them).

## 5. The Tool Gateway stays authoritative

`build_executor` builds a `CapabilityRegistry` from the local packs plus any `extra_packs` the caller
enables. It gives that registry to the executor, which passes it to every `ToolGateway`. With a
registry, the gateway:
1. denies a tool that belongs to **no registered capability**;
2. denies a capability that is **unavailable**, without calling it;
3. applies the permission policy as before (agent allowlist, category rule);
4. lets the capability **tighten, never loosen** the decision (`effective_decision`):
   - `denied` → deny;
   - `always` → approval;
   - a mutation with `unknown` or `recurring` cost → approval;
5. takes an approved action through the `ActionGuard` (write-ahead record, replay, reconciliation),
   exactly as before.

`ToolGateway.call_capability(id, **params)` resolves an ID to its tool and then takes exactly that path.
An unregistered, malformed or non-executable ID is denied and logged.

## 6. Agent views and the model boundary

`view_for(agent, policy)` lists the capabilities an agent may use, as safe descriptions:
- ID, version, description, decision, effect, mutation, cost, and availability;
- never the tool, slot, IAM actions, tests or other internals;
- denied capabilities are left out.

| Agent | Capabilities (default policy) |
| --- | --- |
| planner | `core.files.list`, `core.files.read` |
| developer | `core.files.list`, `core.files.read`, `core.files.write`, `git.commit`, and `core.files.delete` (approval) |
| tester | `core.files.list`, `core.files.read`, `core.tests.run` |
| reviewer | `core.files.list`, `core.files.read` |

**A capability is not a model tool.** Claude's only callable tool is still `submit_result`
([model-providers.md §4a](model-providers.md#4a-model-provider-tools-versus-agent-factory-capabilities)).
The registry only supplies descriptive context. Tests check this with the AWS pack enabled:
- every request offers `submit_result` alone;
- no credential, slot, profile, bucket or canary appears in any request.

**Nothing outside code can add a capability.** Project files (for example a `capabilities.json` or an
`AGENTS.md` that claims new powers), files in `.agent_factory/`, and model output all leave the registry
unchanged. A model naming an invented capability gets a denial from the gateway, and `missing` from an
assessment.

## 7. Evidence-based assessment

`assess(registry, requirements)` (`capabilities/assessment.py`) classifies each `Requirement`
(requirement ID, capability ID or none, a description, a `human` flag). The result:
```json
{"supported": [...], "unavailable": [...], "missing": [...], "requires_approval": [...],
 "requires_human": [...], "evidence": [{"capability": "...", "items": [...]}], "basis": "registry"}
```

| Bucket | When |
| --- | --- |
| `requires_human` | The requirement is marked `human` |
| `missing` | No capability, a malformed ID, or an unregistered ID |
| `unavailable` | Registered, but denied by policy or class, or unavailable now (with the status) |
| `requires_approval` | Available, but every action needs a person |
| `supported` | Available, with no approval needed |

- **Deterministic:** the order of the input doesn't change the output.
- **No model, no network:** it imports no model provider and calls nothing.

This is the boundary a future marketplace integration would call ("can you perform requirements X, Y
and Z?"). A model may later help *extract* requirements from a job description, but the comparison is
the registry's. The report submits, applies for and promises nothing. Upwork isn't integrated.

## 8. Tests

`tests/test_capabilities.py` covers:
- registration, validation, duplicates, versions and immutability;
- evidence and its provenance; that every `TESTED` claim names a real test;
- availability and probes;
- agent views, and the manifest being unchanged;
- assessment;
- explicit enablement;
- the gateway: unregistered, unavailable and unpermitted are denied; approval goes through the action
  guard; rules only tighten; a "harmless" label can't weaken a tool that needs ActionExecution;
- hostile project text;
- live evidence: an authorized record of the exact version and environment counts; a fake backend, a
  mismatched authorization, another version, another environment, an undeclared backend, a removed
  record, or an edited record doesn't; the repository holds no record yet.

`tests/test_aws_boto.py` covers the `cloud_read` vocabulary in every layer, the per-environment policy,
the `cloud_read` invariants, and the real AWS backend offline ([aws-capabilities.md](aws-capabilities.md)).

44 mutants were checked, all caught (19 in Stages B to D, 25 in E1). They're listed in
[testing.md](testing.md#capabilities).
