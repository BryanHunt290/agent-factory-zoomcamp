# AWS capabilities

> **Status:** Stages D and E1 of [design/capability-registry-aws.md](design/capability-registry-aws.md).
> **No AWS API has ever been called by Agent Factory.**
> - The real backend (`BotoAwsBackend`) exists and is tested **offline only**, against botocore's
>   Stubber and synthetic configuration files.
> - No AWS profile of yours, credential file, or `AWS_*` value is read by any code path or test.
> - The first live call (Stage E2) needs your authorization and a new temporary identity
>   ([section 10](#10-stage-e2-the-new-identity-and-the-first-call)).

`AwsToolPack` (`agent_factory/aws/`) puts typed AWS capabilities behind the Capability Registry and the
Tool Gateway ([capabilities.md](capabilities.md)). An AWS capability never means "the model has my AWS
credentials":

```
Claude ─► structured intent ─► Agent Factory ─► Capability Registry ─► Tool Gateway
            (data only)                          (exists? available?)   (policy · approval ·
                                                                          ActionExecution · audit)
                                                                               │
                                                     typed AWS tool (one fixed operation)
                                                                               │
                                     AwsBackend: FakeAwsBackend (default) │ BotoAwsBackend (explicit only)
                                                                               │
                                                          one approved temporary identity ─► IAM ─► AWS
```

## 1. Capabilities

| Capability | Category | Wraps only | Output (after filtering) | IAM actions (documented) | Slot |
| --- | --- | --- | --- | --- | --- |
| `aws.identity.inspect` | `cloud_read` | STS `GetCallerIdentity` | principal type and name, `********` + last 4 digits of the account, region, backend kind | `sts:GetCallerIdentity` (needs no permission, can't be denied) | `aws.dev.identity` |
| `aws.s3.metadata.read` | `cloud_read` | S3 `HeadObject` on a designated bucket and prefix | size, type, ETag, date, encryption, cleaned user metadata | `s3:GetObject`, `s3:ListBucket` (both scoped) | `aws.dev.readonly` |
| `aws.deploy.execute` | `production_deploy` | **nothing** (a definition only) | — | `cloudformation:DescribeChangeSet`, `cloudformation:ExecuteChangeSet`, `cloudformation:DescribeStacks` | `aws.dev.deploy` |

- **The fake backend implements both reads. The real backend implements only `GetCallerIdentity`.**
  S3 waits for Stage F, and with the real backend `aws.s3.metadata.read` is `not_configured`.
- **`aws.deploy.execute`** is never executable. It's classified as a mutation: approval always,
  recurring cost, non-idempotent (so ActionExecution with human reconciliation).

**No arbitrary execution path:**
- `AwsBackend` has exactly three methods: credential metadata, `get_caller_identity`, `head_object`.
- No tool takes an operation, method, service, command, or script.
- No module imports `boto3`. Only `aws/boto.py` imports `botocore`.
- `boto.py` calls exactly one client method (`get_caller_identity`) on exactly one client (`sts`), with no
  dynamic dispatch (a static test checks the source).

## 2. `cloud_read`

`cloud_read` is read-only access to an external cloud system through a typed, trusted adapter
([api-contract.md](api-contract.md#action-categories)). It was added contract-first:
- `openapi.yaml`;
- the API schemas;
- `ActionCategory`;
- the generated frontend types;
- migration `0009` for the database constraints.

| Environment (`PermissionPolicy.for_environment`) | `cloud_read` |
| --- | --- |
| development (the default policy) | allowed, for an agent with the tool on its allowlist and an available capability |
| staging | configurable; approval by default |
| production | approval required; a production policy that allows it is refused |

- **Cloud mutations** use the dangerous categories (`production_deploy`, `spend_money`, …): approval
  always. A capability that mutates through the network can't follow the policy's "allow".
- **Identity, credential, and organization changes** (`iam:`, `organizations:`, `sts:AssumeRole`, …)
  are a **denied class**, never approvable.
- **No agent** has an AWS tool on its allowlist by default.

**Invariants, enforced when a capability is registered:**
- read-only effect, and `mutates: false`;
- cost `none` or `low`;
- the trusted adapter only, never the sandbox;
- an approved credential slot;
- documented IAM actions only of read verbs (`Get`, `List`, `Describe`, `Head`, `Lookup`);
- every input string and list bounded, and a closed, bounded output.

A tool in `cloud_read` must also declare read-only semantics, or the tool registry refuses it. So a
genuinely mutating tool can't pretend to be a cloud read. A cloud read needs no side-effect replay,
because it changes nothing; under a production policy it still goes through approval and the
ActionGuard.

## 3. `BotoAwsBackend` (`aws/boto.py`)

**Activation is explicit, twice.** The pack needs both:
- `AwsPackSettings(enabled=True, backend=BotoAwsBackend(...))`;
- `real_backend_authorized=True`.

Without the second, the pack is `not_configured` and the backend is never even consulted. Nothing in
Agent Factory sets it: no factory, CLI, worker, or API constructs `BotoAwsBackend` (a test checks the
source). Neither `boto3` being installed nor credentials existing changes anything.

**The constructor requires:**
- an environment (`dev`, `staging`, `prod`), which must match the slot's;
- an explicit allowlist of profile names (never `default` or `internal-dev`);
- an explicit set of allowed regions.

**No default session, no default chain.** It never calls:
- `boto3.client`, `boto3.Session()`;
- `get_credentials`, `create_credential_resolver`.

The default chain would try, in order: environment keys, static keys in `~/.aws/credentials`, a credential
process, the container endpoint, and the instance metadata service (`169.254.169.254`). It would quietly
use whichever answers first. Instead:

1. **The profile:** the slot's binding names one profile. It must be on the allowlist, and in the
   backend's environment. A missing profile is `AWS_NOT_CONFIGURED`, never "fall back to default".
2. **The session:** `botocore.session.Session(profile=…)`. Naming a profile disables environment
   credentials. The session's shared-credentials-file setting points at a path that can't exist, so
   **the file of long-term keys is never opened** (a test checks that it isn't).
3. **Classification, from configuration keys only (never values):**
   - accepted: `sso` (IAM Identity Center) and `login` (`aws login`);
   - refused before any provider runs:
     - `static`: keys in the config;
     - `process`: `credential_process`;
     - `assume-role-chain`: a role whose source profile could be static keys (botocore checks those
       first);
     - `workload-assume-role` and `web-identity`: they belong to the deployment stage;
     - anything else: unsupported credential type.
4. **Exactly one provider.** botocore's `ProfileProviderBuilder` builds the profile providers, with
   environment variables disabled. Only the one whose `METHOD` equals the approved kind is kept. No other
   provider exists in the resolution, so there's nothing to fall back to: a test fails if environment,
   file, process, container, or instance-metadata providers are even consulted.
5. **The credential** must report the same method, carry a session token, and expire more than a minute
   from now.
6. **The STS client** is created with the resolved credential passed explicitly, the explicit region,
   connect and read timeouts of 5 and 10 seconds, at most two attempts, and configured endpoint overrides
   ignored (`AWS_ENDPOINT_URL*`). Its endpoint must be `https://sts.<region>.amazonaws.com`.
7. **The call:** exactly `GetCallerIdentity`. The answer is reduced to `Account`, `Arn` and `UserId`
   before it leaves the backend (`ResponseMetadata` never does). The tool reduces it further
   (section 5).

**Credential labels**, verified in botocore 1.43.57's source rather than guessed:

| Label | Mechanism | Accepted? |
| --- | --- | --- |
| `sso` | IAM Identity Center | **Yes** |
| `login` | `aws login` | Yes, but botocore needs `awscrt` (`botocore[crt]`), which isn't installed or approved; resolution fails closed as `AWS_NOT_CONFIGURED` |
| `assume-role`, `assume-role-with-web-identity` | roles | In the design; refused in E1 |
| `iam-role`, `container-role` | a workload's own role | **Never directly**; only as the source of an explicit AssumeRole, in the deployment stage |
| `env`, `shared-credentials-file`, `config-file`, `explicit`, `static`, `custom-process`, anything unknown | static keys, processes | **Refused** |

## 4. Errors (fixed categories)

Every failure becomes one of:
- `AWS_NOT_CONFIGURED`
- `AWS_IDENTITY_UNAVAILABLE`
- `AWS_PERMISSION_DENIED`
- `AWS_EXPIRED_CREDENTIAL`
- `AWS_UNSUPPORTED_REGION`
- `AWS_THROTTLED`
- `AWS_SERVICE_UNAVAILABLE`
- `AWS_RESPONSE_INVALID`
- `AWS_NOT_FOUND`, for S3

**Mapping:**
- AWS error codes: `AccessDenied` → permission denied; `ExpiredToken` → expired; `InvalidClientTokenId`
  and `SignatureDoesNotMatch` → identity unavailable; `Throttling` → throttled;
  `RegionDisabledException` → unsupported region; `InternalFailure` or 5xx → service unavailable; an
  unknown 4xx → response invalid.
- SDK exceptions: an SSO token unauthorized → expired; missing credentials or token → identity
  unavailable; an unknown profile or a missing dependency → not configured; connection errors and
  timeouts → service unavailable; anything else → service unavailable.

**Never surfaced:** the tool's message is exactly `aws: <CATEGORY>`. The SDK's message (which may hold an
ARN or a user name) never reaches a tool result, the API, a model, or a log. The trusted log gets the
exception class and AWS error code only.

## 5. Result filtering

**The identity summary:**
```json
{"principal_type": "assumed-role", "principal_name": "AWSReservedSSO_AgentFactoryIdentity_0123456789abcdef",
 "account_hint": "********9012", "region": "us-west-2", "backend": "boto"}
```
- **Dropped:** the full account ID, the ARN, the user ID, and the role session name. For IAM Identity
  Center the session name is the person's user name or email address.
- **Shape:** fixed fields only, a principal name of at most 64 characters, and a closed output schema.

**The S3 summary (fake backend):**
- projected onto known fields;
- credential-like keys and values removed;
- account numbers masked;
- strings, maps and the total size bounded.

The fake plants canaries to prove all of this.

## 6. Availability probe

configured and authorized → approved credential type → unexpired temporary identity → allowed region →
`GetCallerIdentity` → safe result → `available`. Any doubt is unavailable, and nothing falls back:

| State | Status (detail) |
| --- | --- |
| Not enabled, no backend, or a real backend without authorization | `not_configured` |
| Slot unbound, profile missing or not allowlisted, wrong environment | `not_configured` |
| Static keys, unknown or unsupported credential type | `no_identity` (`AWS_IDENTITY_UNAVAILABLE: …`) |
| Expired, about to expire, or no known expiry | `no_identity` (`AWS_EXPIRED_CREDENTIAL`) |
| `AccessDenied` | `permission_denied` |
| Region not allowed | `unsupported_region` |
| Throttling, service errors, invalid answers | `probe_failed` |
| Everything holds | `available` (detail: the backend kind) |

**Available is not live-tested.** `AVAILABLE_NOW` says the probe passed now. `LIVE_TESTED` says a
recorded, authorized live verification of this exact version passed in this environment (section 7).
Before E2, the identity capability can be `IMPLEMENTED` and `TESTED`, but it is never `LIVE_TESTED`.

## 7. Live-tested evidence

`capabilities/live_evidence.py` holds the only source of `LIVE_TESTED`:
`evidence/live-capabilities.jsonl`, in the repository, where it's reviewed and committed.

**Each record holds:**
- the capability ID and version;
- the backend kind (never `fake`, `stub`, `loopback`, `test`);
- the verification ID and suite;
- the environment;
- the commit;
- the time;
- a SHA-256 digest of the safe result (never the result);
- who authorized it;
- the previous record's hash.

**Rules:**
- **The only writer** is `append_live_verification`, and only with an authorization naming the same
  capability, version, and environment.
- **Packs can't supply evidence.**
- **A record counts only** for its exact version, in its environment, from a backend kind the pack
  declares real (`live_backends`; for AWS, `boto`).
- **A malformed or broken chain** makes the whole file untrusted, so no `LIVE_TESTED` at all.
- **Nothing recorded yet.** The file doesn't exist; a test checks that, and that the default suite
  never writes it.

## 8. Sandbox and model

- **The sandbox:** generated code still has no network. The sandbox refuses every `AWS_*` name, even if
  allowlisted, including:
  - `AWS_CONFIG_FILE`;
  - `AWS_SHARED_CREDENTIALS_FILE`;
  - `AWS_WEB_IDENTITY_TOKEN_FILE`;
  - `AWS_CONTAINER_*`;
  - `AWS_LOGIN_CACHE_DIRECTORY`.

  Its environment is built, never inherited, and the context builder withholds `.aws/` (credentials,
  config, SSO and login caches).
- **The model:** receives descriptions only, and Claude's one callable tool is still `submit_result`.
  With the real backend enabled (stubbed) and the identity capability available to an agent, the
  manifest and views contain none of: the profile, the slot, the account, an ARN, a key, a token, or
  IAM action names.

## 9. `requirements-aws.txt`

The same set as before, verified installed and unchanged on 2026-10-06; nothing was installed:

| Package | Version |
| --- | --- |
| `boto3` | 1.43.57 |
| `botocore` | 1.43.57 |
| `s3transfer` | 0.19.2 |
| `jmespath` | 1.1.0 |
| `python-dateutil` | 2.9.0.post0 |
| `urllib3` | 2.7.0 |
| `six` | 1.17.0 |

`BotoAwsBackend` uses only `botocore`. `awscrt` is **not** in the set, so `aws login` sessions can't be
used by Agent Factory unless you approve that dependency.

## 10. Stage E2: the new identity and the first call

**Recommendation: IAM Identity Center (B), not `aws login` (A).**

| | A. `aws login` | B. IAM Identity Center |
| --- | --- | --- |
| Works with the pinned SDK set | **No**: needs `awscrt`, a new dependency | **Yes**: botocore's `sso` provider |
| Identity behind it | Your console identity (an IAM user, or root) with `SignInLocalDevelopmentAccess` | A dedicated permission set, assigned to one account |
| Least privilege for Agent Factory | Hard: the session is your console identity's permissions | Natural: a permission set made only for Agent Factory |
| Dev / staging / prod later | One account per sign-in | Multiple accounts and permission sets in one portal |
| Cost | None | None for Identity Center, Organizations, or STS; **but on a Free-plan account, creating the organization upgrades it to a paid plan and the free credits expire immediately** (AWS); a Multi-Region instance adds KMS charges, so choose Single-Region |

**Your one-time setup** is in [aws-e2-setup.md](aws-e2-setup.md): the AWS console steps and the Windows
PowerShell steps, each explained, re-checked against the official AWS documentation on 2026-10-06. It adds
two things the first draft here lacked:
- a check of the account's plan (creating the organization ends Free-plan credits);
- choosing a Single-Region instance (Multi-Region adds KMS charges).

None of it has been done.

### E2 readiness (prepared 2026-10-06; E2a built 2026-10-07; nothing live has run)

**E2a: the live verification harness exists, tested offline only.** E1 built every part the call needs (the
real backend, the explicit authorization flag, the Tool Gateway path `call_capability`, and the evidence
writer `append_live_verification`). E2a wires them together for one real call, in `tests/`, not the
product: a test still pins that nothing in `agent_factory/` constructs `BotoAwsBackend` or passes
`real_backend_authorized=True`, and that the harness is the only file in `tests/` that does.

| File | Role |
| --- | --- |
| `tests/aws_live_support.py` | The harness: the network guard, the pre-flight, and `run_identity_verification` |
| `tests/test_aws_live.py` | The one live test, marker `aws_live`, option `--run-aws-live`. **Never run yet**: it is E2b |
| `tests/test_aws_live_harness.py` | 79 offline tests of the harness, in the default suite (synthetic AWS home, botocore's Stubber, fake resolver and connector) |
| `tests/conftest.py` | The `aws_live` opt-in suite; its process and network guards |

Like the other opt-in suites, the live test fails rather than skips when requested and unconfigured. It
verifies exactly one capability: there is no parameter for the capability, the operation, or the backend,
and the plan (profile `agent-factory-dev`, slot `aws.dev.identity`, `us-west-2`, `dev`) has exactly one
accepted value. In order, it:

1. **Refuses to start** without `AGENT_FACTORY_AWS_LIVE_AUTHORIZED_BY` (a short plain-text note of who
   authorized it, for the evidence; anything that looks like an account ID, ARN, key, or token is refused),
   or with uncommitted changes (the evidence records the commit).
2. **Pre-flight, offline:**
   - the plan is the approved one, and the network allowlist matches its Region;
   - `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN` are not set (presence only;
     a value is never read);
   - `default` and `internal-dev` are refused by the backend and the slots;
   - the `agent-factory-dev` profile exists, and its configuration *keys* classify as `sso` (no value is
     inspected, the shared credentials file is not opened, nothing is resolved);
   - the evidence file verifies, and holds no record for `aws.identity.inspect` version 1 in `dev`;
   - the sandbox environment carries no `AWS_*` name;
   - the AWS pack's only executable capabilities are the two `cloud_read` reads.

   The live test also checks that the pre-flight touched no network at all.
3. **Opens the network to three hosts only**, on port 443:
   - `sts.us-west-2.amazonaws.com`;
   - `portal.sso.us-west-2.amazonaws.com`;
   - `oidc.us-west-2.amazonaws.com`.

   Any other name is never resolved, so no DNS query leaves for it. A connection is allowed only to an
   address one of these names resolved to in this run, so `169.254.169.254`, `169.254.170.2`,
   `fd00:ec2::254`, and any literal IP are refused. An allowed name that resolves to a non-public address
   (DNS rebinding) is refused. Every refusal is recorded, and any refusal fails the verification, even if a
   library caught the error. No process may start except two read-only git commands on this repository
   (`rev-parse --verify HEAD`, `status --porcelain`): never the AWS CLI, a credential process, or Docker.
4. **Builds:**
   - `BotoAwsBackend(environment="dev", allowed_profiles={"agent-factory-dev"}, allowed_regions={"us-west-2"})`;
   - `AwsPackSettings(enabled=True, backend=…, slots=CredentialSlots({"aws.dev.identity": "agent-factory-dev"}), region="us-west-2", real_backend_authorized=True)`;
   - a `CapabilityRegistry(environment="dev")` with the Core, Git and AWS packs.

   Building the registry runs the probe: **STS call 1**, after one SSO `GetRoleCredentials` exchange.
   The registered capability must be version 1, `cloud_read`, implemented by `aws_identity_inspect`, and
   its availability must be `available`.
5. **Calls the capability through the gateway:**
   - policy: `PermissionPolicy.for_environment("development").with_agent_tools("verifier", {"aws_identity_inspect"})`;
   - call: `ToolGateway(…, agent="verifier", capabilities=registry).call_capability("aws.identity.inspect")`:
     **STS call 2**;
   - the gateway must record exactly one action: `aws_identity_inspect`, `cloud_read`, decision `allow`,
     no parameters, result `ok`.
6. **Checks the result is exactly the safe summary:** five keys; `principal_type == "assumed-role"`;
   `principal_name` is `AWSReservedSSO_AgentFactoryIdentity_` plus a hex suffix; `account_hint` is
   `********` plus 4 digits; `region == "us-west-2"`; `backend == "boto"`. No 12-digit number, ARN, key or
   principal ID (`AKIA`, `ASIA`, `AROA`, ...), or token-looking text appears in the result, the gateway's
   records, or the captured logs.
7. **Writes one evidence record**, only now: it copies the evidence file to
   `live-capabilities.jsonl.staged`, appends with `append_live_verification`, re-reads the staged copy with a
   registry whose AWS pack is *not* enabled (so no third AWS call), requires `LIVE_TESTED` for exactly
   this verification and no identifying material, and only then replaces the real file in one atomic
   rename. A crash, or a failed check, before the rename leaves the real file exactly as it was.

The owner reviews and commits `evidence/live-capabilities.jsonl`.

**Offline proof that a stubbed run can never produce real evidence:** with any test hook (credential loader
or client hook) the harness refuses the repository's evidence file before doing anything. The registry
trusts only that file, and a default-suite test fails if it exists.

**The E2b command** (only after the owner authorizes E2b and has signed in; E2a is built and tested offline):

```powershell
aws sso login --profile agent-factory-dev
$env:AGENT_FACTORY_AWS_LIVE_AUTHORIZED_BY = "owner, <date>, Stage E2"
.venv\Scripts\python -m pytest -m aws_live --run-aws-live tests/test_aws_live.py -v -s
```

**A manual `aws sts get-caller-identity` is never the proof.** It is allowed only as a diagnostic after a
failed run, and only with the owner's go-ahead.

**What E2b sends over the network:**
- one SSO portal call (`GetRoleCredentials`, which turns the cached sign-in into 1-hour role credentials);
- two STS `GetCallerIdentity` calls (the probe, then the capability);
- possibly one OIDC token refresh, if the cached sign-in needs it.

All are free, and all are in `us-west-2`.

**The `LIVE_TESTED` record E2b would append** (one line; the values here are illustrative):

```json
{"authorized_by": "owner, 2026-10-07, Stage E2", "backend": "boto", "capability_id": "aws.identity.inspect",
 "commit": "<the commit E2b ran at>", "environment": "dev", "hash": "<sha256 of this record>",
 "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
 "recorded_at": "2026-10-07T18:04:11Z", "result_digest": "<sha256 of the canonical safe summary>",
 "suite": "tests/test_aws_live.py::test_identity_inspection_live", "verification_id": "e2-identity-20261007-1",
 "version": 1}
```

- **Never in it:** the summary itself, an account ID, an ARN, a user ID, a profile name, a slot, a key, or a
  token.
- **A unit test can never write it:** the writer refuses `fake`/`stub` backends, and the registry trusts
  only `boto` records for this pack.

**Pre-live checklist** (every item must hold before the owner authorizes E2b):

| Check | How it is verified |
| --- | --- |
| `agent-factory-dev` exists | The owner, with the key-names-only check in [aws-e2-setup.md](aws-e2-setup.md#b3-check-the-new-profile-without-showing-anything-sensitive) |
| Authentication is temporary, and the session has an expiry | The owner (the expiry-only check, B5); the runner refuses a credential without a known expiry, or one expiring within 60 s |
| No static access key is involved | The owner (B3 prints `STOP` otherwise); the backend classifies config keys and refuses static, process, and chained kinds before resolving anything |
| Region is `us-west-2` | The profile (B3); the backend's region allowlist; the STS endpoint must equal `https://sts.us-west-2.amazonaws.com` |
| The slot maps only to `agent-factory-dev` | The runner's `CredentialSlots`; the backend's one-profile allowlist |
| `default` and `internal-dev` cannot be selected | Refused at construction (backend) and binding (slots); tested offline |
| `BotoAwsBackend` is explicitly authorized | `real_backend_authorized=True` in the runner only; nothing in the product sets it (tested) |
| Only `aws.identity.inspect` is exercised, in `cloud_read` | The runner calls that one capability; its category is pinned by tests |
| The environment is development | `BotoAwsBackend(environment="dev")`, a `dev` registry, the development policy |
| No AWS mutation capability is enabled | `aws.deploy.execute` is non-executable; the deny-all permission set grants nothing |
| No sandbox network; no AWS credential in generated code | Unchanged E1 guarantees (sandbox env allowlist, `AWS_*` refused); the runner re-checks |
| No AWS credential reaches Claude or any model context | No model is involved in E2 at all; E1 tests prove the manifest carries no profile, slot, account, or key |
| No `LIVE_TESTED` evidence exists before the call | `evidence/live-capabilities.jsonl` is absent (a test checks this) |
| E2a runner built and tested offline; tree committed | Done 2026-10-07 (the owner reviews the E2a commit); the runner refuses a dirty tree |

**Failure handling** (every case fails closed, with no evidence written and no fallback to another
profile):

| Condition | What Agent Factory reports | What the owner does |
| --- | --- | --- |
| SSO sign-in missing | `AWS_IDENTITY_UNAVAILABLE`, availability `no_identity` | `aws sso login --profile agent-factory-dev` |
| SSO sign-in or role credential expired, or expiring within 60 s | `AWS_EXPIRED_CREDENTIAL`, `no_identity` | Sign in again |
| Wrong, missing, or non-allowlisted profile | `AWS_NOT_CONFIGURED` (never another profile) | Fix the profile name |
| `agent-factory-dev` holds static keys | `AWS_IDENTITY_UNAVAILABLE` ("long-term access keys are refused"), before any provider runs | Remove the keys; re-run `aws configure sso` |
| Unsupported credential type (process, role chain, web identity, `aws login` without `awscrt`, unknown) | `AWS_IDENTITY_UNAVAILABLE` ("unsupported credential type") or `AWS_NOT_CONFIGURED` | Use the Identity Center profile |
| Wrong Region, or a global/overridden STS endpoint | `AWS_UNSUPPORTED_REGION`, or `AWS_NOT_CONFIGURED` ("unexpected STS endpoint") | Set `region = us-west-2`; remove endpoint overrides |
| STS or SSO unavailable, timing out, or throttling | `AWS_SERVICE_UNAVAILABLE` / `AWS_THROTTLED`, after at most 2 STS attempts | Retry later |
| STS `AccessDenied` (not expected for this call) | `AWS_PERMISSION_DENIED` | Report it; don't widen the policy |
| SSO portal refusal (the assignment is missing or not yet provisioned) | Fails closed, but **its label is imprecise**: SSO error codes are not in the mapping table, so a 4xx maps to `AWS_RESPONSE_INVALID` | Check step A7; a later fix can map these codes precisely |
| Malformed STS response | `AWS_RESPONSE_INVALID` | Report it |
| Any network attempt outside the three hosts (or port 443), or an allowed name resolving to a private or metadata address | The runner's guard refuses and records it, and the test fails, even if a library swallowed the error | Report it |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, or `AWS_SESSION_TOKEN` set in the environment | The pre-flight fails before any credential is resolved (names only) | Unset them |
| A capability version other than 1, or an evidence record for it already present | The run fails: a new version needs a new authorization; a version is verified once | Report it |

**Endpoints E2 would contact, all resolved offline from botocore 1.43.57:**
- `https://sts.us-west-2.amazonaws.com`: **GetCallerIdentity**;
- `https://portal.sso.us-west-2.amazonaws.com`: SSO GetRoleCredentials, which turns your cached
  sign-in into short-lived role credentials. This is part of credential resolution; it isn't a
  capability call;
- `https://oidc.us-west-2.amazonaws.com`: only if the cached SSO token needs a refresh.

Nothing else. No S3, no IAM, no metadata service.

**What E2 returns** (and nothing more):
```json
{"principal_type": "assumed-role", "principal_name": "AWSReservedSSO_AgentFactoryIdentity_<suffix>",
 "account_hint": "********<last 4>", "region": "us-west-2", "backend": "boto"}
```
**What it never returns:**
- the full account ID;
- the ARN;
- the user ID;
- your user name or email (the role session name);
- a key or a token.

Sources (official, read 2026-10-06):
- [Configuring IAM Identity Center authentication with the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html)
- [Getting started with IAM Identity Center](https://docs.aws.amazon.com/singlesignon/latest/userguide/getting-started.html)
- [Create a permission set](https://docs.aws.amazon.com/singlesignon/latest/userguide/howtocreatepermissionset.html)
- [Login for AWS local development](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)
- [STS GetCallerIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html): "No
  permissions are required … If an administrator attaches a policy … that explicitly denies access to the
  `sts:GetCallerIdentity` action, you can still perform this operation."
