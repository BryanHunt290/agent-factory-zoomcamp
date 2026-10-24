# Authentication and authorization

| | |
| --- | --- |
| Status | **Implemented on `feature/auth-worker` (unreleased):** token verification, principals, permissions, and their enforcement on every API operation. **Not yet:** a chosen and configured production identity provider, and browser or phone sign-in (it comes with the frontend). The API still listens on 127.0.0.1 only. |
| Code | [`agent_factory/api/auth/`](../agent_factory/api/auth/), [`agent_factory/domain/principals.py`](../agent_factory/domain/principals.py), migration [`0004_principals.sql`](../agent_factory/persistence/migrations/0004_principals.sql) |
| Contract | [`openapi.yaml`](../openapi.yaml): the `bearerAuth` scheme, and `x-permission` on every operation |
| Tests | `test_auth_tokens.py`, `test_auth_settings.py`, `test_auth_http.py`, `test_auth_principals.py`, `test_auth_boundary.py` ([testing.md](testing.md#authentication-and-authorization-tests-unreleased)) |

## Contents

1. [Two questions](#two-questions)
2. [The architecture](#the-architecture)
3. [Principals: who someone is](#principals-who-someone-is)
4. [Permissions: what someone may do](#permissions-what-someone-may-do)
5. [Verifying an access token](#verifying-an-access-token)
6. [What a client sees: 401, 403, 429, 503](#what-a-client-sees-401-403-429-503)
7. [Approval decisions: who and what](#approval-decisions-who-and-what)
8. [Rate limits](#rate-limits)
9. [Logs and audit](#logs-and-audit)
10. [Development and test authentication](#development-and-test-authentication)
11. [Configuration](#configuration)
12. [The security boundary](#the-security-boundary)
13. [Workers are not principals](#workers-are-not-principals)
14. [What is not guaranteed, and what remains](#what-is-not-guaranteed-and-what-remains)

## Two questions

Every API request answers two separate questions, in this order:

| Question | Name | How Agent Factory answers it | When it fails |
| --- | --- | --- | --- |
| *Who is making this request?* | **Authentication** | A signed access token from a trusted identity provider, verified on every request, names an enrolled principal | **401**: no credential, or one that was refused |
| *Is that identity allowed to do this?* | **Authorization** | The operation requires one permission; the principal either holds it or does not | **403**: a known identity, without the permission |

Knowing who someone is never implies what they may do, and the two failures
are reported differently, so a client can tell "sign in again" from "you may
not do this".

## The architecture

```
Browser / phone
      |  sign-in: Authorization Code + PKCE          (with the frontend; not built yet)
      v
OpenID Connect identity provider                     (chosen per deployment; none yet)
      |  a signed JWT access token
      v
Agent Factory API: an OAuth 2.0 resource server
      |  1. verify the token: signature, issuer, audience, times      -> 401
      |  2. issuer + subject -> an enrolled, active principal          -> 401
      |  3. the operation's permission                                 -> 403
      v
services and domain (an approval decision records the principal)
```

The API never sees a password and never issues tokens. It accepts
`Authorization: Bearer <token>` (RFC 6750) and verifies the token with the
public keys the provider publishes.

### Why this architecture

The owner chose it on 2026-10-04 from these options:

| Option | Assessment |
| --- | --- |
| **OpenID Connect with an external identity provider** (chosen) | Established standards: OAuth 2.0 (RFC 6749, 6750), OpenID Connect Core, JWT (RFC 7519) and its best current practices (RFC 8725). The provider handles passwords, passkeys, multi-factor sign-in, and account recovery; Agent Factory only verifies signatures and stores no credentials. It serves one owner today and many users later, from a browser or a phone, and fits a later AWS deployment (Amazon Cognito is the natural candidate) without tying the code to one provider. |
| **Locally issued sessions** (Agent Factory runs its own sign-in) | It would make Agent Factory an identity provider, with credential storage, multi-factor sign-in, recovery, and brute-force protection to build and secure. Rejected. |
| **A homegrown token scheme** | Rejected: custom protocols and custom cryptography are where security bugs live. |

The owner also approved the one new dependency: PyJWT with `cryptography`,
pinned in `requirements-api.txt`. Verifying an asymmetric signature (RS256,
ES256) needs a maintained cryptography library; Python's standard library has
none, and writing one was not an option.

### What is production-ready and what depends on the provider

| Part | State |
| --- | --- |
| Token verification: signature, algorithm allowlist, key IDs, issuer, audience, expiry, not-before, issue time, subject, maximum lifetime | **Implemented**, tested with real signatures (EC and RSA keys) |
| The provider's key set: HTTPS fetch, bounded cache, refresh for key rotation, fail closed | **Implemented**, tested with an injected fetcher; never run against a real provider |
| Principals, permissions, enforcement on every operation, approval identity | **Implemented**, tested in memory and on PostgreSQL |
| A production identity provider | **Not chosen and not configured.** Nothing was contacted. Choosing one needs an account and credentials: the owner's decision |
| Browser and phone sign-in: Authorization Code + PKCE, a server-side session so tokens never live in page scripts, CSRF protection | **Not built.** It comes with the frontend |
| Step-up sign-in before dangerous approvals | Not built |
| Public exposure | Refused: the API listens on 127.0.0.1 only |

## Principals: who someone is

A **principal** is an identity enrolled in Agent Factory (`principals` table).

- **Its ID** is Agent Factory's own: `prn-` and 20 random hex digits.
- **Its identity is the pair (issuer, subject):** the token's `iss` (which
  provider) and `sub` (which account at that provider). An OpenID Connect
  provider promises never to reassign a subject, so the pair is stable. The
  same subject from another issuer is a different person.
- **An email address is never an identity.** People change email addresses,
  and providers can reassign them. A display name is not unique. Both are
  stored only to show people who a principal is, and both follow the token's
  `name` and `email` claims whenever those are present.
- **Enrollment is explicit.** The owner enrolls principals with the local
  CLI. A token with a valid signature, for anyone not enrolled, is refused
  with 401: having an account at the provider grants nothing. No HTTP route
  can create a principal or change a permission.
- **Active or disabled.** A disabled principal cannot authenticate, whatever
  it was granted. Status and permissions are read on every request, so
  disabling or revoking takes effect at once. Principals are disabled, never
  deleted, so the decisions they made keep naming them.
- **The initial owner** is an ordinary principal holding every permission.
  Nothing in the code treats a particular user or name specially, so more
  users later need no new identity model.

```bat
rem Enroll the owner (the configured database; see database.md):
.venv\Scripts\python -m agent_factory auth principal add --issuer https://issuer.example.com --subject <provider subject> --display-name "Owner" --grant-all
rem Someone who may read approvals and nothing else:
.venv\Scripts\python -m agent_factory auth principal add --issuer https://issuer.example.com --subject <subject> --grant approvals:read
.venv\Scripts\python -m agent_factory auth principal list
.venv\Scripts\python -m agent_factory auth principal grant --id prn-... approvals:decide
.venv\Scripts\python -m agent_factory auth principal revoke --id prn-... approvals:decide
.venv\Scripts\python -m agent_factory auth principal disable --id prn-...
```

## Permissions: what someone may do

Every operation requires exactly one permission. The contract names it
(`x-permission` in `openapi.yaml`), and one `require_permission` check
enforces it on the server (`api/auth/policy.py`). A test compares the two for
every route.

| Permission | Operations | Notes |
| --- | --- | --- |
| `projects:read` | `listProjects`, `getProject`, `listTemplates` | Project views include summary counts (pending approvals, recent activity) and the latest run's status, but not the runs, milestones, or approvals themselves |
| `projects:create` | `createProject` | Does not include reading projects |
| `runs:read` | `listRuns`, `getRun` | |
| `milestones:read` | `listProjectMilestones`, `listRunMilestones` | |
| `approvals:read` | `listPendingApprovals`, `getApproval`, `listAttention` | Does not include deciding or reconciling |
| `approvals:decide` | `decideApproval` | The response shows the request just decided. Does not include reconciling |
| `actions:reconcile` | `reconcileAction` | Saying whether an approved action with an uncertain outcome happened (design/action-execution.md). Does not include deciding, or reading the inbox: the response shows the approval just answered |

Decisions behind the list:

- **No permission implies another.** Reading approvals does not allow
  deciding; deciding does not allow reconciling, and reconciling does not
  allow deciding; creating projects does not allow reading them. Each is
  tested, on the real routes
  (`test_api_human_decisions.py::test_approvals_decide_does_not_allow_reconciling`,
  `::test_actions_reconcile_does_not_allow_deciding_or_reading`,
  `test_auth_http.py::test_every_operation_requires_exactly_its_documented_permission`).
- **Why reconciling is separate.** Approving says "this action may run".
  Reconciling says "this is what happened in the outside world", and choosing
  "try once more" authorizes a new dispatch. The people who can check what
  happened (an operator with access to the mail log, the payment provider, the
  deployment) are not necessarily the people who approve.
- **The template catalog belongs to `projects:read`.** It describes what
  kinds of project exist and holds no project data, so a `templates:read`
  permission would add a grant to manage without protecting anything.
- **No roles yet.** Permissions are granted directly; `--grant-all` makes an
  owner. Roles, and organizations or teams, can be added for multiple users
  without changing how identity works.
- **Only Agent Factory's grants count.** `scope`, `roles`, or `permissions`
  claims in a token are ignored, as are request fields and headers: a
  provider authenticates, Agent Factory authorizes (tested).
- **Hiding a button is not authorization.** A frontend may hide what a user
  cannot do, but the server checks every request whatever the client shows,
  sends, or claims; a request crafted by hand meets the same check.

## Verifying an access token

`TokenVerifier` (`api/auth/tokens.py`) accepts a token only if all of these
hold:

1. It is a compact JWS (three base64url parts), at most 8 KiB. An unsigned or
   encrypted token is refused.
2. Its `alg` is on the configured allowlist, which can contain only
   asymmetric algorithms (RS, PS, ES, EdDSA). `none` and the HMAC family are
   never accepted, whatever the token says.
3. Its `kid` names a key the provider published **for that algorithm**. The
   key, not the token, decides how the signature is checked: a token cannot
   ask for the RSA public key to be used as an HMAC secret (the classic
   algorithm-confusion attack, tested).
4. The signature verifies, and the key meets the minimum length (2048-bit
   RSA).
5. `iss` equals the configured issuer exactly; `aud` contains the configured
   audience.
6. `exp`, `iat`, `iss`, `aud`, and `sub` are present. It has not expired, it
   is not used before `nbf`, and it was not issued in the future; a 30-second
   leeway allows for clock skew. `exp - iat` is at most the maximum lifetime
   (24 hours by default).
7. `sub` is a valid OpenID Connect subject (1 to 255 printable characters).
8. No `crit` header extension is present (none is understood). If a token
   type is configured (`at+jwt` under RFC 9068), the `typ` header must match.

**The provider's keys** (`api/auth/keys.py`) come from its JWKS document over
HTTPS:

- Only public signature keys (RSA, EC, OKP) are accepted. A symmetric key, or
  one carrying private parts, makes the whole document invalid.
- The set is cached for 10 minutes. A token naming an unknown `kid` triggers
  one refresh, at most once a minute, so a provider's key rotation works and a
  flood of invented key IDs cannot make the API hammer the provider.
- If the keys cannot be fetched, the last good set serves for at most an hour
  past its expiry. After that nothing verifies: 503
  `authentication_unavailable`, never a guess.

**Provider notes, to settle when one is chosen.** Amazon Cognito's access
tokens carry `client_id` instead of `aud`, and some providers mark access
tokens `typ: at+jwt`; either needs one configuration change, not a new design.

## What a client sees: 401, 403, 429, 503

| Status | `code` | When | Headers |
| --- | --- | --- | --- |
| 401 | `authentication_required` | No `Authorization` header | `WWW-Authenticate: Bearer realm="agent-factory"` |
| 401 | `invalid_token` | A credential was sent and refused, for any reason: malformed, bad signature, expired, wrong issuer or audience, unknown or disabled principal | `WWW-Authenticate: Bearer realm="agent-factory", error="invalid_token"` |
| 403 | `forbidden` | Authenticated, without the operation's permission. `required_permission` names it | |
| 429 | `too_many_requests` | Too many refused credentials from this client, or too many decisions by this principal | `Retry-After` |
| 503 | `authentication_unavailable` | The provider's keys cannot be obtained | `Retry-After` |

- **Every refused credential gets the same 401**, with the same text. The
  response never says whether an account exists, is disabled, or which check
  failed; the server log records the category.
- **The order** is authentication, then authorization, then everything else.
  A request without a valid credential never learns whether a project
  exists, and a principal without `approvals:read` gets 403 even for an
  approval that does not exist.
- `/healthz` and `/readyz` need no credential: they answer a fixed word and
  expose no data.

## Approval decisions: who and what

```
Authenticated principal
        |
        v
Holds approvals:decide? -- no --> 403
        | yes
        v
Fingerprint matches the request? -- no --> 409 action_fingerprint_mismatch
        | yes
        v
Record the decision with the trusted principal and the server's time,
and wake the run (one transaction)
        |
        v
A worker, holding the run's claim, re-checks the stored request against the
engine's exact pending action, the expiry, and that the principal is still
enrolled, active, and holding approvals:decide; then it applies the decision.
Approval is still not execution (design/action-execution.md).
```

- **Authentication proves who decided; the fingerprint proves what they
  decided.** Both are required.
- **The server controls** the actor (the authenticated principal, `verified`),
  and the decision time. The request body is a closed schema: `actor`,
  `decided_by`, `verified`, `user_id`, or `decided_at` in it is a 422 and
  nothing is recorded.
- **An identical retry** (same outcome, same fingerprint) by the same
  principal answers 200 with the recorded decision. The same request from
  another principal is 409 `approval_already_decided`: nobody can claim
  another person's decision.
- **The database enforces it too.** A decision by a `user` actor carries
  `principal_id`, a foreign key to an enrolled principal that must equal the
  actor ID; no other decision carries one (database.md#principals-and-permissions).
  Reconciliation answers and applied reconciliations follow the same rule
  (migration 0006).
- **Re-checked when applied.** A decision is applied by a worker, possibly
  minutes later. If the person was disabled, or lost `approvals:decide`, in
  between, the approval is applied as refused and the action never runs; a
  rejection is applied regardless. An answer by someone who lost
  `actions:reconcile` is not applied at all, and the run waits for a person
  ([workers.md](workers.md#human-decisions-wake-ups-and-consumption)).

Reconciliation follows the same shape: `actions:reconcile`, then the
per-principal limit, then the exact binding (execution, attempt,
fingerprint), then the answer recorded with the trusted principal and the
server's time, and the run woken. The request body is closed: no actor, time,
or retry window can be sent.

The audit question "who approved what, when, for which project and run" is a
query:

```sql
SELECT d.decided_at, d.outcome, p.id AS principal, p.issuer, p.subject,
       r.project_id, r.run_id, r.action_tool, r.action_category, d.action_fingerprint
FROM approval_decisions d
JOIN principals p ON p.id = d.principal_id
JOIN approval_requests r ON r.project_id = d.project_id AND r.id = d.request_id
ORDER BY d.decided_at;
```

## Rate limits

| Limit | Key | Default | Beyond it |
| --- | --- | --- | --- |
| Refused credentials | the client's address | 10 per minute | 429 for any request carrying a credential, before any verification work, until the window passes |
| Approval decisions and reconciliation answers (one shared budget) | the principal | 30 per minute | 429 for that principal; others are unaffected |

**What is guaranteed:** the limits hold within one API process, and the API
runs as one process on 127.0.0.1, so today that is the whole service. A
client address is not an identity (many people can share one, behind a
proxy), so that limit protects the verifier rather than identifying anyone.
**Not guaranteed:** limits across several processes or hosts. A deployment
must move them to shared state or a gateway; Redis was deliberately not added
for this milestone.

## Logs and audit

- **Logged:** the request ID; after authentication, the principal ID; the
  permission checked; and a safe result category (`allowed`, `forbidden`,
  `expired`, `wrong_issuer`, `unknown_principal`, `rate_limited`, ...). Each
  access line carries them.
- **Never logged:** access tokens, the `Authorization` header, signing keys,
  or any credential payload. The log redactor also removes anything shaped
  like a JWT or a bearer credential from every line, including tracebacks
  (tested with canaries).
- **The approval audit trail is the database**, not the logs: decisions name
  their principal, action fingerprint, time, project, and run (query above).

## Development and test authentication

Neither is production authentication, and neither can be switched on in
production:

| | Test issuer | Development issuer |
| --- | --- | --- |
| Where | `tests/auth_support.py`; never shipped | `agent_factory/api/auth/development.py` and the `auth dev-issuer` CLI |
| Keys | Generated in memory for each test run | One P-256 key pair in `.local/dev-issuer/` (git-ignored). The API reads only the public `jwks.json` |
| Tokens | Really signed; verified by the production code path | Really signed; written to a file, never printed; at most a day long (15 minutes by default) |
| Active when | Only inside tests, which build the application with it | Only with `AGENT_FACTORY_AUTH_MODE=development` |

There is no test header, no bypass flag, and no "insecure" mode: every mode
verifies real signatures, and only the trusted issuer differs.

**Production refuses development authentication** (tested):

- production is the default mode, and an unconfigured production API refuses
  to start;
- production refuses the development key directory and the development issuer
  (`urn:agent-factory:development`, not an https URL), and requires https for
  the issuer and the JWKS URL;
- both kinds of setting at once are refused in either mode;
- even a production configuration whose JWKS URL serves the development
  issuer's keys refuses development tokens, because their issuer does not
  match.

To try the API locally without a provider:

```bat
set AGENT_FACTORY_AUTH_MODE=development
set AGENT_FACTORY_AUTH_DEV_ISSUER_DIR=.local\dev-issuer
set AGENT_FACTORY_DATABASE_URL_FILE=.local\database.url
.venv\Scripts\python -m agent_factory db migrate
.venv\Scripts\python -m agent_factory auth dev-issuer init
.venv\Scripts\python -m agent_factory auth principal add --issuer urn:agent-factory:development --subject me --grant-all
.venv\Scripts\python -m agent_factory auth dev-issuer token --subject me --out .local\dev-token.txt
.venv\Scripts\python -m agent_factory api serve
rem In another window: send the token in the Authorization header, e.g.
rem   curl -H "Authorization: Bearer <contents of .local\dev-token.txt>" http://127.0.0.1:8000/api/v1/projects
```

## Configuration

`AuthSettings.from_environment()` (`api/auth/settings.py`):

| Variable | Mode | Meaning |
| --- | --- | --- |
| `AGENT_FACTORY_AUTH_MODE` | both | `production` (the default) or `development` |
| `AGENT_FACTORY_AUTH_ISSUER` | production | The provider's issuer identifier: an https URL, compared exactly with `iss` |
| `AGENT_FACTORY_AUTH_AUDIENCE` | production | The audience the provider puts in tokens for this API |
| `AGENT_FACTORY_AUTH_JWKS_URL` | production | The provider's JWKS document (https) |
| `AGENT_FACTORY_AUTH_ALGORITHMS` | production, optional | Comma-separated; default `RS256,ES256`; asymmetric only |
| `AGENT_FACTORY_AUTH_TOKEN_TYPE` | production, optional | A required `typ` header, e.g. `at+jwt` |
| `AGENT_FACTORY_AUTH_DEV_ISSUER_DIR` | development | The development issuer's directory |

Rate limits and the maximum token lifetime have the defaults above; they are
constructor arguments, not environment variables.

## The security boundary

Access tokens and authentication settings exist only inside the API process:

- **The sandbox refuses them.** Names matching `AUTH`, `TOKEN`, `KEY`,
  `SECRET`, `SESSION`, `JWT`, `BEARER`, `OIDC`, or `JWK`, and any value shaped
  like a JWT under any name, are never passed into a sandbox request or a
  container (docs/sandbox.md). Trusted local processes inherit only an
  allowlisted environment. Tested with canaries, including a real local
  process.
- **The engine, domain, sandbox, tools, and agents never import
  authentication code** (a test inspects their imports), so a token has no
  path into generated code, execution logs, or milestones.
- **Records hold the principal, never the token** (tested by searching every
  stored record after an authenticated decision).
- **Errors never echo a credential** (tested).

## Workers are not principals

Workers ([workers.md](workers.md)) are internal infrastructure, not people,
and this architecture is unchanged by them:

- **No principal, no bearer token.** A worker never presents a user's access
  token and has no principal or permissions.
- **No endpoint.** No HTTP route claims, renews, or releases runs, and none
  controls workers.
- **What authorizes a worker** is the database role it connects with, plus
  its claim on a run. Fencing makes the claim authoritative: knowing a run's
  ID and an old fencing token gives a worker nothing, because ownership is a
  database row. Today a worker uses the same local database credentials as
  the CLI.
- **Future infrastructure identity** (design note; nothing is implemented,
  and no cloud identity is configured):
  - in a deployment, each worker would run with its own service identity
    (in AWS, an IAM role, with IAM database authentication or a credential
    from a secrets manager);
  - its database role would be allowed to claim runs and write ledger and
    product records, and denied principals and permissions;
  - service-to-service calls, if any appear, would use that identity, never
    user tokens.

## What is not guaranteed, and what remains

- **No production identity provider is configured**, and fetching a real
  provider's keys has not been exercised. That needs the owner's choice of
  provider, and an account.
- **No browser or phone sign-in yet.** Bearer tokens in a header cannot be
  sent by another site, so there is no CSRF exposure today; the session
  cookie of the coming server-side sign-in will need CSRF protection.
- **No token revocation.** A stolen token works until it expires (24 hours at
  most here; many providers default to an hour). Disabling the principal stops
  it at once.
- **Rate limits are per process** (above).
- **No step-up authentication** before approving dangerous categories (money,
  deployment, credentials, deletion, external communication): a candidate for
  the approval interface, using the provider's `auth_time` or `acr` claims.
- **Grants have no audit trail** beyond each grant's time; enrollment and
  permission changes are made with the local CLI, not logged to a table.
- **The local operator is trusted as an operator.** CLI decisions and
  answers (`approve --database`, `reconcile-action --database`) are recorded
  as `unauthenticated` actors (`local:<name>`), never as principals. The
  worker applies them: whoever can run the CLI already has the workspace and
  the database credentials. They are labelled as unverified everywhere they
  are shown.
