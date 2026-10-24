# Runtime provider selection (decision checkpoint)

Which model provider, and which model, Agent Factory's **own agents** should
use first at runtime. It is a decision document: no provider was
implemented, configured, or called to produce it.

- **Status:** a recommendation awaiting the owner's decision.
- **Research date:** 2026-10-06.
- **Method:** read-only access to official provider documentation and
  pricing pages (authorized for this comparison only). No account,
  credential, AWS configuration, package installation, or model call.

## 0. Two separate decisions

| | Development-time AI | Runtime AI |
| --- | --- | --- |
| What | The coding assistant that helps build this repository (currently Claude Code) | The `ModelProvider` that Agent Factory's planner, developer, and reviewer call when a run executes |
| Chosen by | The owner's tooling preference | Product requirements: cost, safety, AWS fit, operations |
| Effect on the other | None | None |

**Disclosure.** This document was researched and written by the
development-time assistant: a Claude model, made by Anthropic, one of the
providers compared. The conclusions rest on the cited official facts below
(API compatibility with Bedrock, a provider-side hard spend limit, price,
model lifecycle), each of which the owner can check, and not on which
assistant wrote them. Where a fact favours another provider (OpenAI's
guaranteed structured outputs, Ollama's offline use), it is stated.

## 1. Official sources checked (accessed 2026-10-06)

| Provider | Source (official) | Used for |
| --- | --- | --- |
| Anthropic | `platform.claude.com/docs/en/about-claude/pricing` | Per-model prices, cache, batch, data residency, Bedrock regional premium |
| Anthropic | `platform.claude.com/docs/en/models/overview` | Current models, IDs on every platform, context, output, lifecycle |
| Anthropic | `platform.claude.com/docs/en/models/sonnet-5-5/overview`, `.../whats-new-sonnet-5-5` | Claude Sonnet 5.5 specification and **breaking changes** |
| Anthropic | `platform.claude.com/docs/en/models/opus-5-5/overview`, `.../models/sonnet-5/overview` | Alternatives' restrictions |
| Anthropic | `platform.claude.com/docs/en/build-with-claude/structured-outputs` | Structured outputs, schema limits, Bedrock availability |
| Anthropic | `platform.claude.com/docs/en/agents-and-tools/tool-use/overview` | Tool use, `tool_choice` |
| Anthropic | `platform.claude.com/docs/en/build-with-claude/claude-in-amazon-bedrock` | Claude in Amazon Bedrock: Messages API endpoint, authentication, features, quotas, regions |
| Anthropic | `platform.claude.com/docs/en/api/rate-limits` | Tiers, spend limits, 429/400 behaviour, headers |
| Anthropic | `privacy.claude.com` articles 7996866 (retention) and 7996868 (training) | Data handling for the commercial API |
| AWS | `docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html` | Bedrock model card: endpoints, APIs, features, service tiers, inference profiles |
| AWS | `docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html` | Bedrock API keys (short-term, long-term) |
| AWS | `docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html` | Data protection, the model-deployment-account isolation |
| AWS | `aws.amazon.com/bedrock/pricing/` | Attempted. **Current Claude prices could not be read** (the page renders them dynamically); only legacy Claude 3.5 Sonnet extended-access prices were visible |
| OpenAI | `developers.openai.com/api/docs/pricing`, `.../docs/models` | Current models and prices (`openai.com/api/pricing` refused the request with 403) |
| OpenAI | `developers.openai.com/api/docs/guides/structured-outputs`, `.../guides/your-data` | Structured outputs, data controls |
| Ollama | `docs.ollama.com/capabilities/structured-outputs` | Local structured output |

Everything below is limited to what these pages say. Where a page was
silent or unreadable, the gap is stated, not filled.

## 2. Pricing (USD per million tokens, standard tier, as published 2026-10-06)

| Provider / model | Input | Output | Cached input / cache read | Notes |
| --- | --- | --- | --- | --- |
| **Anthropic Claude Sonnet 5.5** (`claude-sonnet-5-5`) | **$2** | **$10** | $0.20 read; $2.50 (5 min) / $4 (1 h) write | Current ("latest"), fast; 1M context; 128K max output |
| Anthropic Claude Opus 5.5 | $4 | $20 | $0.20 read | Thinking always on, cannot be disabled |
| Anthropic Claude Sonnet 5 | $2 | $10 | $0.20 read | Legacy |
| Anthropic Claude Haiku 4.5 | $1 | $5 | $0.10 read | **Retirement not sooner than 2026-10-15**, nine days after this research |
| Anthropic, any model | | | | Batch −50%. `inference_geo: "us"` ×1.1 (4.6+). Small free credits for new users. Monthly spend caps per tier (Start $500) and a settable lower limit |
| **Amazon Bedrock, Claude** | **not verified** | **not verified** | | Billed through AWS Marketplace. Anthropic's pricing page: on Bedrock, for Claude 4.5 and later, **global endpoints carry no premium and regional endpoints +10%**. AWS's own per-model figures were not readable; **verify in the AWS console or pricing calculator before use**. (Visible example of AWS extended-access pricing: legacy Claude 3.5 Sonnet at $6 / $30.) |
| OpenAI GPT-6.1 Sol ("recommended for most tasks") | $2 | $10 | $0.10 | 1.05M context; 128K output |
| OpenAI GPT-6 Astra (flagship) | $10 | $50 | $1.00 | |
| OpenAI GPT-6 Luna (efficient) | $0.10 | $0.50 | $0.01 | |
| OpenAI, any model | | | | Regional processing +10% for models released on or after 2026-03-05 |
| Ollama (local) | $0 per token | $0 | | Hardware and electricity; no hosted cost |

No minimum charge is stated for on-demand inference by Anthropic, OpenAI,
or (for the legacy models visible) Bedrock.

## 3. First live acceptance run: the maximum spend

**The run.** The live suite is two tests (`tests/test_model_live.py`) on the
tiny calculator template. Each is capped by `LIVE_BUDGET`:

- 10 calls;
- 60,000 input and 16,000 output tokens per run;
- per call, 20,000 input and 4,000 output tokens.

**Output is capped exactly**, because `max_tokens` is sent with every call
and the run's output budget is checked before each call. **Input** is
checked against an estimate before each call and against the actual count
after it. The estimate (one token per three characters) may undercount:

- current Claude models' tokenizer produces "approximately 30% more tokens
  for the same text";
- the tool-use system prompt adds 286 tokens per call on Sonnet 5.5.

So input can overshoot the run cap by part of one call. A **10% margin** on
input covers this, and section 7 tightens the estimate.

| Model | Max input (2 × 66k) | Max output (2 × 16k) | **Maximum for the whole live suite** |
| --- | --- | --- | --- |
| **Claude Sonnet 5.5, Claude API** | 132k × $2 = $0.264 | 32k × $10 = $0.320 | **≈ $0.59** |
| Claude Sonnet 5.5 on Bedrock (global) | assuming parity: $0.264 | $0.320 | ≈ $0.59 (**price unverified**) |
| Claude Opus 5.5, Claude API | $0.528 | $0.640 | ≈ $1.17 |
| OpenAI GPT-6.1 Sol | $0.264 | $0.320 | ≈ $0.59 |
| OpenAI GPT-6 Luna | $0.013 | $0.016 | ≈ $0.03 |

**After the compatibility fix**, the live suite also carries a **dollar
ceiling of $0.50 for the whole suite** ($0.25 per run), checked before every
call against that call's worst case. The suite cannot spend more than $0.50
even if the token-based maximum above were reached (§12).

A realistic run of the calculator task uses far less: one plan, one or
two implementations, and one review, at a few thousand tokens each. The
figures above are the **hard ceiling** the budgets enforce. Failed calls
(timeouts) are counted at their reservation and stay inside the same caps.

## 4. Model quality and Agent Factory fit

| | Claude Sonnet 5.5 (direct) | Claude Sonnet 5.5 (Bedrock) | OpenAI GPT-6.1 Sol | Local (Ollama) |
| --- | --- | --- | --- | --- |
| Coding, planning, reviewing | Described as "the best combination of speed and intelligence"; Bedrock's card: "a clear leap forward from Sonnet 5 on coding" | Same model | "Near-Astra performance for complex work at a lower cost" | Depends on the local model and GPU; well below frontier on ordinary hardware |
| Provider-enforced schema | Structured outputs (`output_config.format`, strict tools) are **GA on the Claude API**, but cannot express `minLength`/`maxLength`, `maxItems`, `propertyNames`, or `additionalProperties` other than `false` | **Not supported** on Claude in Amazon Bedrock | `strict: true` guarantees schema adherence; the documented keyword limits are less explicit | `format` with a JSON schema; adherence **not guaranteed** |
| Forced tool use (the adapter's current method) | **Returns HTTP 400 on Sonnet 5.5** (and Opus 5.5) | Same model | n/a | n/a |
| Non-default `temperature` (the adapter sends 0.2) | **Returns HTTP 400** on Sonnet 5.5 and Sonnet 5 | Same | Model-dependent | Recommended 0 |
| Thinking | Adaptive, on by default (default effort `high`); `thinking: {"type": "between_tools"}` turns off up-front thinking; thinking tokens are output tokens | Same; `between_tools` "available on every platform" | Model-dependent | n/a |
| Errors and retries | 429 + `retry-after`; spend cap: 429 without `retry-after` (`enforced_spend_limit_reached`); own limit: 400 "You have reached your specified API usage limits"; refusals: 200 with `stop_reason: "refusal"` | AWS errors; CloudTrail logs | 429 | Local |
| Token accounting | `usage` per response, including cache reads and writes | Same Messages API shape | `usage` per response | Local |
| Context | 1M input, 128K output | Same | 1.05M / 128K | Model-dependent |
| Version stability | Pinned dateless IDs; Sonnet 5.5 retirement not sooner than 2027-09-28 | Bedrock sets its own lifecycle (card: EOL no sooner than 2027-09-28) | Named model versions | You pin the weights |

**Agent Factory does not rely on provider-side schema enforcement.** It
validates every answer against its own stricter schema (lengths, path
patterns, no unexpected keys) and re-asks once. A provider that guarantees
schemas reduces re-asks; it does not replace local validation. This is
also why Bedrock's lack of structured outputs is acceptable.

## 5. Authentication

| | Claude API (direct) | Claude in Amazon Bedrock | OpenAI | Ollama |
| --- | --- | --- | --- | --- |
| Credential | API key (`x-api-key`) | IAM (SigV4), a Bedrock service role, assumed roles (12 h), or Bedrock API keys: **short-term** (up to 12 h, inherit the caller's IAM permissions, "recommended for production") or **long-term** ("for exploration only") | API key | None |
| Temporary credentials | No | **Yes** (STS, short-term keys, roles on ECS/EC2) | No | n/a |
| Agent Factory today | A key **file** (`AGENT_FACTORY_MODEL_API_KEY_FILE`), header only, never logged, refused by the sandbox and the request guard | Would need: a bearer token header (no new package), or SigV4 signing (`botocore`/`boto3`, or an AWS-provided SDK) | Similar to the key file | Loopback HTTP |
| Least privilege | Workspace-scoped keys and limits | IAM policies on model ARNs (`bedrock-mantle:CreateInference`, `bedrock:InvokeModel`); `bedrock:CallWithBearerToken` can be denied or limited to short-term keys | Project keys | n/a |

## 6. AWS integration

- **Bedrock is a model inference service.** It is the natural home of
  Agent Factory's model calls once Agent Factory runs on AWS:
  - IAM roles instead of keys;
  - CloudWatch and CloudTrail logs;
  - AWS billing;
  - data handled under Bedrock's terms.
- **Claude in Amazon Bedrock speaks the same Messages API.** It is served
  at `https://bedrock-runtime.{region}.amazonaws.com/anthropic` (AWS's
  recommended endpoint) and at
  `https://bedrock-mantle.{region}.api.aws/anthropic/v1/messages`, with
  "the same request body shape as Anthropic's first-party API".
  - Model IDs gain a prefix: `anthropic.claude-sonnet-5-5`, or the
    inference profiles `global.anthropic.claude-sonnet-5-5` and
    `us.anthropic.claude-sonnet-5-5`.
  - The adapter's request and response format therefore carries over. What
    changes is authentication, the base URL, the model ID, and features
    absent on Bedrock (structured outputs, token counting on one
    endpoint, Message Batches).
- **Quotas.** The default is 2M input tokens per minute (TPM). Up to 5M input
  and 500k output TPM are available without Anthropic approval. Requests per
  minute are enforced by AWS.
- **Access.** Sonnet 5 and Haiku 4.5 are open to all Bedrock customers.
  Sonnet 5.5 and Opus 5.5 show per-model access criteria in the console.
- **Regions.** Global endpoints have no premium; regional endpoints carry a
  10% premium. Sonnet 5.5's in-region availability depends on the endpoint
  (geo and global profiles across US and EU regions).
- **Observability.** CloudWatch and CloudTrail. AWS states that model
  providers have no access to Bedrock's model-deployment accounts, logs,
  prompts, or completions.

**The AWS Agent Toolkit is not a model provider.** Do not confuse the two:

```text
                    Agent Factory
                         |
           +-------------+--------------+
           |                            |
           v                            v
     ModelProvider                Capability Registry
   (inference only)               (actions, by policy)
           |                            |
   Claude API / Bedrock           Tool Gateway
           |                     (policy, approval, ActionExecution, sandbox, audit)
     Claude Sonnet 5.5                  |
                                  AWS Agent Toolkit (future) ── AWS services
```

- **Bedrock** answers questions. It holds no authority over AWS resources,
  and the model never receives AWS credentials.
- **The AWS Agent Toolkit** (future) would let agents *act* on AWS. Every
  action would pass the Tool Gateway with IAM least privilege, approvals for
  dangerous categories (`spend_money`, `production_deploy`,
  `credential_change` are never auto-allowed), ActionExecution, and audit.
- **The two stay separate even when both are AWS.** The inference
  credentials (for example a role allowed only `bedrock-mantle:CreateInference`
  on one model) must never be the toolkit's credentials, and neither ever
  reaches the model or the sandbox.

## 7. Privacy and data handling (official statements only)

| | Training on API data | Retention | Notes |
| --- | --- | --- | --- |
| Anthropic API (commercial) | "By default, we will not use your inputs or outputs from our commercial products (e.g. … Anthropic API …) to train our models", except explicit feedback or opt-in | Deleted "within 30 days of receipt or generation"; policy violations up to 2 years; feedback 5 years; zero-retention agreements available | Consumer chat terms are different and do not apply to the API |
| Claude in Amazon Bedrock | Governed by Bedrock | Governed by Bedrock's data retention page | "Model providers don't have any access to those accounts… they don't have access to Amazon Bedrock logs or to customer prompts and completions"; Anthropic: "zero operator access" |
| OpenAI API | "Not used to train or improve OpenAI models (unless you explicitly opt in)" | Abuse-monitoring logs "up to 30 days"; zero retention for eligible endpoints | Data residency in several regions (+10% on newer models) |
| Ollama (local) | n/a | Nothing leaves the machine | |

What Agent Factory sends is unchanged by the choice: the minimum context per
role, secrets withheld, host paths redacted
([model-providers.md](../model-providers.md#11-privacy-what-may-leave-this-machine)).

## 8. Direct Anthropic vs. Bedrock → Claude

| | Direct (Claude API) | Bedrock → Claude |
| --- | --- | --- |
| Same model | `claude-sonnet-5-5` | `anthropic.claude-sonnet-5-5` |
| Request format | Messages API | **The same** Messages API shape (`/anthropic` endpoints) |
| Adapter work before the first call | The Sonnet 5.5 compatibility fixes (§11); nothing else | The same fixes, **plus** Bedrock authentication (bearer token, or SigV4 signing with a new dependency), region and model-ID configuration, and Bedrock error mapping |
| Account setup | A Console account, a workspace, a key | An AWS account, Bedrock model access (Sonnet 5.5 has per-model criteria), an IAM identity and policy, a region, and a short-term key or role |
| **Hard spend stop** | **Yes:** a settable monthly spend limit; requests stop (HTTP 400) when reached | AWS Budgets alerts (and budget actions); no equivalent per-model hard stop was found in the pages read (not verified) |
| Credentials | A long-lived key in a file (rotatable) | Temporary credentials: IAM, short-term keys, roles |
| Future AWS deployment | An external dependency (a key in a secret manager) | **Native** (task role, VPC endpoints, CloudTrail) |
| Pricing | Verified | Not verified (§2) |
| Features | Structured outputs, token counting, batches | No structured outputs; fewer endpoints |

**Conclusion: B, Bedrock second.** For the *first* proof, direct is lower
risk and faster:

- a verified price;
- a hard provider-side spend stop;
- no AWS setup or new dependency;
- a compatibility fix that both paths need anyway.

For *production on AWS*, Bedrock is preferable, and moving there changes
authentication and endpoint, not prompts, schemas, budgets, or tests.

## 9. Recommendation

1. **First provider: the Claude API, called directly**, through the
   existing adapter **after the compatibility fix in §11**. The fix is
   small and testable offline.
2. **First model: `claude-sonnet-5-5`, one model for the planner, the
   developer, and the reviewer.**
   - **It is current:** retirement not sooner than 2027-09-28, and it is
     available on Bedrock under the same name.
   - **It is fast, and priced at $2 / $10.**
   - **It has the capability the three roles need:** planning a small
     change, writing complete files, and reviewing against tests.
   - **Rejected:** Opus 5.5 costs twice as much and has thinking always on.
     Haiku 4.5 retires within days. Sonnet 5 is legacy, at the same price.
   - **No routing yet.** One model keeps the first evaluation
     interpretable.
3. **Second provider: Claude in Amazon Bedrock, the same model.** Use the
   Messages API at `bedrock-runtime…/anthropic`:
   - short-term Bedrock API keys for local testing (no new package);
   - an IAM role (SigV4) when Agent Factory runs on AWS.
4. **Not first:**
   - **OpenAI** (a strong alternative: same price tier, guaranteed schemas)
     needs a new adapter now, and offers no path to the same model on
     Bedrock.
   - **A local model** suits free, offline regression runs later, not the
     first quality proof.

### Migration path (direct → Bedrock)

1. One adapter class, parameterized by:
   - base URL (`https://api.anthropic.com` or
     `https://bedrock-runtime.{region}.amazonaws.com/anthropic`);
   - model ID (`claude-sonnet-5-5` or
     `global.anthropic.claude-sonnet-5-5`);
   - **an authentication strategy**: an API key file, a Bedrock bearer
     token, or later SigV4.

   The new strategy is the only Bedrock-specific code.
2. `AGENT_FACTORY_MODEL_PROVIDER=bedrock` with `AGENT_FACTORY_MODEL_REGION`.
   Explicit activation, budgets, the guard, and the key-file rules are
   unchanged. A Bedrock bearer token, like any key, is read from a file,
   never from `AWS_BEARER_TOKEN_BEDROCK` merely being present.
3. Run the same live suite on Bedrock and compare outcomes, tokens, and cost.
4. On AWS, an IAM role (ECS task role) allowed only to invoke the chosen
   model in the chosen region, with no key on disk. The dispatcher and the
   toolkit hold separate credentials.

## 10. Credentials and packages

**For the first call (direct):**

- **Credential:** one Claude API key, for a **dedicated workspace** with a
  **low monthly spend limit** (for example $5), saved to a file outside the
  repository (for example `%USERPROFILE%\.agent-factory\anthropic.key`). It
  is never pasted into a terminal command, a document, the repository, or
  `.local/`.
- **Packages:** **none.** The adapter uses Python's standard library.
- **Network:** HTTPS POST to `https://api.anthropic.com/v1/messages`, from
  the test process only. The sandbox keeps no network.

**For Bedrock later:**

- an AWS account;
- Bedrock model access to `anthropic.claude-sonnet-5-5` (per-model
  criteria), in a chosen region;
- an IAM identity that may only invoke that model, with long-term Bedrock
  keys denied;
- a short-term Bedrock API key per session (from the console, or the
  `aws-bedrock-token-generator` package, a pinned install).

SigV4 for a deployed role needs `botocore`/`boto3` (a pinned install) or an
AWS-provided signer. **None of this is configured or installed now.**

## 11. Required before the first live call: adapter compatibility

> **Status: implemented** (owner-approved, 2026-10-06), and tested offline only
> (`tests/test_anthropic_compat.py`). One refinement: the token estimate is
> stricter than item 6 proposed. It is one token per UTF-8 byte plus 1,024,
> a true upper bound, rather than one per 2.5 characters. The live budget's
> input limits were raised to suit it (30,000 per call, 100,000 per run),
> and the $0.50 ceiling governs.

The current adapter would **fail on the very first call** to Claude Sonnet
5.5. It sends `temperature: 0.2` (HTTP 400) and forced
`tool_choice: {"type": "tool"}` (HTTP 400). These changes need the owner's
authorization (a code change), and all can be tested offline against the
loopback fake server:

1. **Remove `temperature`** (the default only).
2. **`tool_choice: {"type": "auto"}`** with the single `submit_result`
   tool, and the instruction to call it. An answer without exactly one
   `submit_result` call is already treated as malformed: one corrective
   re-ask, then the node fails.
   - First-party structured outputs (`output_config.format`) are **not**
     used in v1: Bedrock lacks them, and they cannot express Agent
     Factory's open `files` map (`additionalProperties` must be `false`).
   - A future prompt version could switch the developer schema to an array
     of `{path, content}` and use them on the Claude API.
3. **`thinking: {"type": "between_tools"}`**: no up-front thinking, so
   output stays predictable and within the per-call cap. Thinking tokens
   are output tokens; thinking blocks are ignored and never stored.
4. **Spend limits are not errors to retry.**
   - The tier spend cap (429 with `error_code: enforced_spend_limit_reached`
     and no `retry-after`) and a user-set limit (400 "You have reached your
     specified … usage limits") become `ProviderConfigError`: the run
     **blocks**, with no retries.
5. **Refusals.** `stop_reason: "refusal"` becomes a non-retryable failure of
   the node.
6. **A more conservative token estimate before each call:** one token per
   2.5 characters, plus the tool-use system prompt. This keeps the input
   overshoot well inside the 10% margin of §3.
7. **A new test against the loopback server for each change.** The prompt
   versions are unchanged: the templates' text and schemas do not change.

## 12. Hard limits for the first live run

| Limit | Value | Where |
| --- | --- | --- |
| Provider-side monthly spend limit | **$5** on a dedicated workspace (owner sets it in the Console) | Claude Console |
| Per call | 30,000 input (upper-bound estimate) / 4,000 output tokens | `LIVE_BUDGET` |
| Per agent visit | 3 calls | `LIVE_BUDGET` |
| Per run | 10 calls, 100,000 input (upper-bound estimate), 16,000 output tokens | `LIVE_BUDGET` |
| Dollar ceiling (checked before each call against its worst case; the live suite refuses to run without prices) | **$0.50 for the whole live suite**: $0.25 per run (two runs). For `demo --real-model`, the configured ceiling below: `AGENT_FACTORY_MODEL_PRICE_INPUT_PER_MTOK=2`, `..._OUTPUT_PER_MTOK=10`, `AGENT_FACTORY_MODEL_MAX_USD_PER_RUN=0.50` | environment |
| Timeout | 120 s per call | `AGENT_FACTORY_MODEL_TIMEOUT_S` |
| Transport retries | 2 per call (bounded), all within the call caps | `RetryPolicy` |
| Whole live suite | **≤ $0.50** (the ceiling; the token caps alone would allow about $0.36 per run) | |

The existing budget controls are not weakened; these are tighter.

## 13. The next step's exact configuration (after authorization)

```bat
:: 1. Owner: Console workspace "agent-factory-live", monthly spend limit $5, API key saved to:
::    %USERPROFILE%\.agent-factory\anthropic.key   (never pasted anywhere else)
set AGENT_FACTORY_MODEL_PROVIDER=anthropic
set AGENT_FACTORY_MODEL_MODE=live
set AGENT_FACTORY_MODEL_NAME=claude-sonnet-5-5
set AGENT_FACTORY_MODEL_API_KEY_FILE=%USERPROFILE%\.agent-factory\anthropic.key
set AGENT_FACTORY_MODEL_PRICE_INPUT_PER_MTOK=2
set AGENT_FACTORY_MODEL_PRICE_OUTPUT_PER_MTOK=10
set AGENT_FACTORY_MODEL_MAX_USD_PER_RUN=0.50
.venv\Scripts\python -m agent_factory model check
.venv\Scripts\python -m pytest -m "model_live and docker_live" --run-model-live --run-docker-live -v -s
```

The live tests use `LIVE_BUDGET`, which is stricter than the environment
budget.

## 14. Remaining risks

- **Bedrock prices are unverified** (AWS's page could not be read), so the
  Bedrock estimate assumes parity with the first-party price.
- **Model behaviour is unproven** for Agent Factory's prompts. A schema-valid
  but poor plan or implementation is caught only by the tests and the
  reviewer; the first live run is the evaluation.
- **Tool use with `auto` may sometimes answer in prose.** That costs one
  corrective re-ask, counted in the budget.
- **The adapter changes in §11 are required**, and only offline-tested until
  the first call.
- **Provider terms, prices, and model lifecycles change.** Re-check §1's
  sources before each new spend decision.
- **A long-lived API key exists on the developer's machine** for the
  direct path. Mitigations:
  - a dedicated workspace with a low limit;
  - a file outside the repository;
  - the existing guards;
  - rotation after the experiment;
  - Bedrock's temporary credentials remove it in production.
