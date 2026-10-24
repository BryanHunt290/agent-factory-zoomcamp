# Stage E2: setting up the first live AWS identity (owner guide)

**Status: preparation only.** Nothing in this guide has been done. Agent Factory has made **zero** AWS API
calls, and the assistant preparing this guide did not authenticate, call STS, or create anything.

The first live AWS test will prove exactly one path and nothing else:

```text
Agent Factory → Capability Registry → aws.identity.inspect → Tool Gateway (cloud_read, development)
              → BotoAwsBackend → the temporary identity "agent-factory-dev" → STS GetCallerIdentity
```

**What this guide sets up:**
- one new IAM Identity Center user (you);
- one permission set, `AgentFactoryIdentity`, which denies everything;
- one local profile, `agent-factory-dev`, whose credentials are temporary and come from a browser sign-in.

**What it never touches:** your existing `default` and `internal-dev` profiles. Agent Factory refuses them
by name, and nothing here reads, shows, rotates, or deletes their keys.

**You never paste a secret anywhere.** No access key, secret key, session token, or SSO token goes into
Claude Code, ChatGPT, a document, or a command line. Sign-in happens in your browser.

Sources (official, read 2026-10-06) are listed at the end.

---

## 0. Two checks before you click anything

1. **Is your AWS account on the Free plan?** Open the AWS console, then **Billing and Cost Management**, and
   look at your account plan.
   - AWS: "If you use a free tier account, creating an AWS organization automatically upgrades your account
     to a paid plan with pay-as-you-go pricing. **Your free tier credits expire immediately.**"
   - Step 2 creates an organization. **If you're on the Free plan and want to keep its credits, stop here**
     and tell me: the alternative (`aws login`) needs a dependency you haven't approved.
   - **This is an unresolved owner decision** (re-checked against the official documentation on 2026-10-07;
     see "The Free plan and AWS Organizations" below). Nothing here has been clicked.
2. **Is the account already in an AWS Organization, or does it already have IAM Identity Center?**
   - Check the AWS Organizations and IAM Identity Center consoles.
   - If either exists already, stop and tell me what you see. The steps below assume a standalone account.

### The Free plan and AWS Organizations (unresolved; for the owner to decide)

**Why an organization is needed at all.** The profile E2 uses (`sso_account_id` plus `sso_role_name`) signs in
to an *AWS account* through a *permission set*. In IAM Identity Center, only an **organization instance**
can do that:
- AWS's instance comparison: "AWS access portal for single-sign on access to your AWS accounts" and
  "Multi-account permissions" are **Yes** for an instance in the AWS Organizations management account, and
  **No** for an instance in a standalone account or a member account. Permission sets ("manage AWS account
  access for users and groups") belong to organization instances.
- An organization instance is "enabled in the AWS Organizations management account". Enabling it from a
  standalone account shows **Enable IAM Identity Center with AWS Organizations**: "This option creates a new
  organization with your account as the management account."

**Can Identity Center be enabled without an organization?** Yes, as an **account instance** (the console's
"enable an account instance of IAM Identity Center" link). But an account instance offers only users,
the access portal for *applications*, and OIDC applications. It cannot give access to AWS accounts, so it
cannot produce the role credentials this profile needs. **For this design, an organization is
required.**

**What the official documentation says about the Free plan** (the sources do not fully agree):

| Source | Says |
| --- | --- |
| [Enable IAM Identity Center](https://docs.aws.amazon.com/singlesignon/latest/userguide/enable-identity-center.html) | "If you use a free tier account, creating an AWS organization automatically upgrades your account to a paid plan with pay-as-you-go pricing. Your free tier credits expire immediately." |
| [AWS Free Tier Terms](https://aws.amazon.com/free/terms/) | "If you are using an AWS Free Plan account and enroll in AWS Organizations, your account will be upgraded to a Paid Plan account, and you will no longer be able to use or earn credits offered under the Free Tier." |
| [Choosing a plan (Billing guide)](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-plans.html) | "Free account plans will automatically upgrade to paid plan if you join AWS Organizations ..." (on credits it says only that, when a Free plan expires and you upgrade, "Any remaining credits ... are automatically applied to future AWS bills") |
| [AWS Free Tier FAQs](https://aws.amazon.com/free/free-tier-faqs/) | "The activities will expire immediately, and you will become ineligible to earn more credits when you join an AWS Organization"; and, for an upgrade in general, "your remaining Free Tier credits will automatically apply to future AWS bills until they expire" |

All four agree that **creating an organization moves a Free-plan account to the Paid plan** (pay-as-you-go,
the account no longer closes when credits run out, and charges beyond credits become possible). They
**disagree on the remaining credits**: two say they expire or can no longer be used; the FAQ suggests credits
already earned carry over and only the earning activities end. Treat the stricter reading (credits lost) as
the planning assumption until AWS confirms otherwise.

**What to verify before clicking anything** (each is a read-only look in the console):
1. **Billing and Cost Management** home: the account plan (Free or Paid), the credit balance, and the plan's
   end date. If it already says **Paid**, this concern does not apply (credits already apply to bills).
2. **AWS Organizations** console: whether the account is already in an organization (then the account may
   already be Paid, and step 2 below differs: stop and report what you see).
3. **IAM Identity Center** console, in `us-west-2`: whether an instance already exists, and which kind.
4. If the account is on the Free plan: decide whether losing the remaining credits (the stricter reading)
   is acceptable, or ask AWS Support which reading applies to this account. If it is not acceptable, the
   only temporary-credential alternative in the design is `aws login`, which needs the unapproved `awscrt`
   dependency (a separate owner decision); a role chain from static keys stays refused.

Nothing in E2a depends on this decision: the harness is built and tested offline either way.

## A. AWS console steps (about 15 minutes)

Sign in as an administrator of the account. Use root only if you have no other administrator.

### A1. Pick the Region first: US West (Oregon), `us-west-2`

Use the Region selector at the top right **before** opening IAM Identity Center. AWS: an organization can
have Identity Center "enabled only in a single AWS Region", and moving it later means deleting it and
starting over.

### A2. Enable IAM Identity Center

1. Open **IAM Identity Center**.
2. If you see **Instance configuration**, choose **Single-Region instance**. Not Multi-Region: that creates a
   KMS key, and "AWS KMS charges apply".
3. Leave **Enable multi-account permissions** on. Permission sets need it.
4. Choose **Enable**. On the **Enable IAM Identity Center with AWS Organizations** page, choose **Enable**
   again.

**What this does:**
- It creates an AWS Organization, with your account as its management account.
- It creates an Identity Center "organization instance": a user directory and a sign-in portal.
- Both are free: Identity Center "is offered at no extra charge", and Organizations "at no additional
  charge".

### A3. Require MFA

1. Go to **Settings**, then the **Authentication** tab.
2. Under **Multi-factor authentication**, choose **Configure**.
3. Under **Prompt users for MFA**, choose **Every time they sign in (always-on)**. That's the default; confirm
   it.
4. If a setting asks what to do when a user has no MFA device, choose to **require them to register one at
   sign-in**.
5. **Save changes.**

### A4. Note your AWS access portal URL

On the IAM Identity Center **Dashboard**, copy the **AWS access portal URL**. It looks like
`https://d-xxxxxxxxxx.awsapps.com/start`. `aws configure sso` needs it.

It's an address, not a secret, but don't paste it into chat. You don't need to.

### A5. Create your Identity Center user

1. Choose **Users**, then **Add user**.
   - **Username:** for example `af-owner`. It can't be changed later.
   - **Password:** "Send an email to this user with password setup instructions" (the default).
   - **Email:** an address you can open now. First name, last name, display name: yours.
2. Choose **Next**, skip groups with **Next**, then **Add user**.
3. Open the email "Invitation to join AWS IAM Identity Center". It comes from `no-reply@signin.aws.com` or
   `no-reply@login.awsapps.com`, and expires in seven days.
4. Accept it, set a password, and register an MFA device (an authenticator app or a passkey).

**What this does:** creates a person in the Identity Center directory. This is not an IAM user, and it
**has no access keys**.

### A6. Create the `AgentFactoryIdentity` permission set

1. Under **Multi-account permissions**, choose **Permission sets**, then **Create permission set**.
2. Choose **Custom permission set**, then **Next**.
3. Leave **AWS managed policies** and **Customer managed policies** empty. Do **not** pick
   `PowerUserAccess`, which AWS's CLI guide suggests, nor `ReadOnlyAccess` or `AdministratorAccess`.
4. Expand **Inline policy** and replace its contents with exactly:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       { "Sid": "AgentFactoryE2DenyEverything", "Effect": "Deny", "Action": "*", "Resource": "*" }
     ]
   }
   ```
5. Choose **Next**.
   - **Permission set name:** `AgentFactoryIdentity`.
   - **Description:** `Agent Factory E2: proves a temporary identity only. Denies all actions.`
   - **Session duration:** **1 hour**, the minimum (and the default).
   - **Relay state:** empty.
6. Choose **Next**, then **Create**.

**Why a deny-everything policy is enough:**
- AWS's STS reference says of `GetCallerIdentity`: "No permissions are required to perform this operation.
  If an administrator attaches a policy to your identity that explicitly denies access to the
  `sts:GetCallerIdentity` action, you can still perform this operation. Permissions are not required
  because the same information is returned when access is denied."
- So this identity can prove who it is, and do **nothing else**: no S3, EC2, Lambda, Glue, Athena,
  Redshift, Step Functions, CloudFormation, Bedrock, IAM, deployment, or console actions.
- Signing in to this role is governed by the Identity Center assignment (step A7), not by the role's
  permissions.

**One thing is unconfirmed:** whether the explicit deny could interfere with obtaining the role's
credentials. I believe it can't, but I didn't find an AWS statement saying so. E2 itself will show it. If it
does interfere, E2 fails closed with nothing done, and the fix is to delete the inline policy, leaving the
permission set with no policy at all, which also grants nothing.

### A7. Assign the permission set to your account

1. Under **Multi-account permissions**, choose **AWS accounts**.
2. Tick your account (the management account), then **Assign users or groups**.
3. On the **Users** tab, tick `af-owner`, then **Next**.
4. Tick `AgentFactoryIdentity`, then **Next**, then **Submit**. Leave the page open until it reports success
   (a few minutes).
   - AWS notes that assigning in the management account needs `IAMFullAccess`-equivalent permissions for
     **you**, the administrator doing it. It doesn't give that to the new identity.

**What this does:** Identity Center creates an IAM role in your account (named
`AWSReservedSSO_AgentFactoryIdentity_…`), with the deny-all policy attached, that only Identity Center users
can assume.

### A8. Look, but don't take keys

1. Open the portal URL from A4 and sign in as `af-owner`, with MFA.
2. You should see your account and the role **AgentFactoryIdentity**. That's all you need to confirm.
3. **Don't** open "Access keys" there: it displays temporary keys, and they must never be copied anywhere.

## B. Local Windows PowerShell steps

### B1. Check the CLI version (no AWS call)

```powershell
aws --version
```

It must be 2.22.0 or newer; 2.36.8 is installed. 2.22.0 and newer use browser sign-in with PKCE by default.

### B2. Create the `agent-factory-dev` profile

```powershell
aws configure sso
```

**What it asks, and what to answer:**

| Prompt | Answer |
| --- | --- |
| `SSO session name (Recommended):` | `agent-factory` |
| `SSO start URL [None]:` | your portal URL from A4 |
| `SSO region [None]:` | `us-west-2` |
| `SSO registration scopes [None]:` | `sso:account:access` |
| (your browser opens) | Sign in as `af-owner` with MFA. Allow the request from the client named `botocore-client-…`. Return to PowerShell. |
| account choice | Skipped automatically when there is only one account |
| role choice | Skipped automatically: `AgentFactoryIdentity` is the only role |
| `Default client Region [None]:` | `us-west-2` |
| `CLI default output format (json if not specified) [None]:` | `json` |
| `Profile name [...]:` | `agent-factory-dev`. **Never `default`**: that name would make it the default profile. |

**What this does:**
- It signs you in once in the browser. That is your own sign-in, not an Agent Factory call. It also lists
  your accounts and roles through the SSO portal. **It does not call STS.**
- It writes two sections to `%USERPROFILE%\.aws\config`: `[profile agent-factory-dev]` (`sso_session`,
  `sso_account_id`, `sso_role_name`, `region`, `output`) and `[sso-session agent-factory]` (`sso_start_url`,
  `sso_region`, `sso_registration_scopes`).
- **It writes no access key.** It doesn't touch `default` or `internal-dev`.

### B3. Check the new profile without showing anything sensitive

This prints **key names only** for the new sections. It never prints values, and never prints the other
profiles.

```powershell
$cfg = Get-Content "$env:USERPROFILE\.aws\config" -Raw
foreach ($name in 'profile agent-factory-dev', 'sso-session agent-factory') {
  $m = [regex]::Match($cfg, '(?ms)^\[' + [regex]::Escape($name) + '\]\s*$(.*?)(?=^\[|\z)')
  if (-not $m.Success) { "[$name] MISSING"; continue }
  $keys = ($m.Groups[1].Value -split "`n" | Where-Object { $_ -match '^\s*([a-z_]+)\s*=' } | ForEach-Object { $Matches[1] })
  "[$name] keys: $($keys -join ', ')"
  if ($keys | Where-Object { $_ -match 'aws_access_key_id|aws_secret_access_key|aws_session_token|credential_process|role_arn|source_profile' }) { "  STOP: a static or chained credential is configured here" }
}
```

**Expected:**
```
[profile agent-factory-dev] keys: sso_session, sso_account_id, sso_role_name, region, output
[sso-session agent-factory] keys: sso_start_url, sso_region, sso_registration_scopes
```

Tell me only whether it matched. Don't paste the file.

### B4. Sign in, when you're about to authorize E2

The sign-in from B2 already counts. Later, or after it expires:

```powershell
aws sso login --profile agent-factory-dev
```

**What it does:**
- It opens the browser for your Identity Center sign-in, with MFA.
- It caches an SSO **access token** (not AWS keys) in `%USERPROFILE%\.aws\sso\cache\`, in a file named
  after the session `agent-factory`.
- Agent Factory's backend later exchanges that token for **role credentials lasting 1 hour** (the permission
  set's session duration), through the SSO portal, and keeps them **in memory only**.

### B5. Check the sign-in is temporary, without showing any token

This prints only the expiry time of the cached SSO sign-in. It never prints the token.

```powershell
$sha1 = [System.Security.Cryptography.SHA1]::Create()
$key  = -join ($sha1.ComputeHash([Text.Encoding]::UTF8.GetBytes('agent-factory')) | ForEach-Object { $_.ToString('x2') })
$file = "$env:USERPROFILE\.aws\sso\cache\$key.json"
if (Test-Path $file) { "SSO sign-in expires at: " + (Get-Content $file -Raw | ConvertFrom-Json).expiresAt } else { "No SSO sign-in cached: run aws sso login --profile agent-factory-dev" }
```

**What it proves:**
- The sign-in is a token with an expiry, not a key.
- The role credentials derived from it expire after 1 hour, and Agent Factory checks that again itself: it
  refuses a credential with no known expiry, or one expiring within 60 seconds.

**Don't run these to check:**
- `aws configure export-credentials`: it prints keys.
- `aws sts get-caller-identity`: that's the call E2 makes through Agent Factory.

### B6. When you're done

```powershell
aws sso logout
```

It deletes the cached sign-in. Agent Factory then refuses the identity until you sign in again.

## C. When the slot gets bound

`aws.dev.identity → agent-factory-dev` is **not** bound anywhere today, and nothing should be changed to bind
it:
- No configuration file or environment variable holds a slot binding.
- The product never constructs the real backend (a test enforces that).
- No agent has `aws_identity_inspect` on its allowlist.

The binding happens **only inside the authorized E2 verification run**, as constructor arguments that exist
for that process's lifetime:

```python
BotoAwsBackend(environment="dev", allowed_profiles=frozenset({"agent-factory-dev"}),
               allowed_regions=frozenset({"us-west-2"}))
AwsPackSettings(enabled=True, backend=..., slots=CredentialSlots({"aws.dev.identity": "agent-factory-dev"}),
                region="us-west-2", real_backend_authorized=True)
```

A persistent binding (for workers to use AWS capabilities in ordinary runs) is a later, separately approved
design, never part of E2.

## D. Existing account configuration

Review and maintain any pre-existing AWS credentials separately with the
account owner. This guide does not inspect, rotate or remove them. Agent
Factory still refuses its documented blocked profiles.

## Sources (official, read 2026-10-06)

- [Enable IAM Identity Center](https://docs.aws.amazon.com/singlesignon/latest/userguide/get-set-up-for-idc.html): instance types, the Region, Organizations, the Free-plan upgrade, single- vs. multi-Region
- [Add users to your Identity Center directory](https://docs.aws.amazon.com/singlesignon/latest/userguide/addusers.html)
- [Prompt users for MFA](https://docs.aws.amazon.com/singlesignon/latest/userguide/mfa-getting-started.html)
- [Create a permission set](https://docs.aws.amazon.com/singlesignon/latest/userguide/howtocreatepermissionset.html), [Set session duration](https://docs.aws.amazon.com/singlesignon/latest/userguide/howtosessionduration.html) (default 1 hour, range 1 to 12 hours)
- [Assign user or group access to AWS accounts](https://docs.aws.amazon.com/singlesignon/latest/userguide/assignusers.html)
- [Configuring IAM Identity Center authentication with the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html): `aws configure sso`, PKCE since 2.22.0, `aws sso login`, the token cache, `aws sso logout`
- [STS GetCallerIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html): no permissions needed; an explicit deny doesn't block it
- [AWS STS Regional endpoints](https://docs.aws.amazon.com/sdkref/latest/guide/feature-sts-regionalized-endpoints.html)
- [IAM Identity Center FAQs](https://aws.amazon.com/iam/identity-center/faqs/) ("no extra charge"), [Organizations pricing](https://docs.aws.amazon.com/organizations/latest/userguide/pricing.html), [IAM FAQs](https://aws.amazon.com/iam/faqs/) (STS at no additional charge)
