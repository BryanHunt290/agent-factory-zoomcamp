> Historical snapshot: this older draft is preserved for provenance, not current scoring.
> Use [the current mapping](RUBRIC_MAPPING.md) and its official source link.

# AI Dev Tools Zoomcamp — official final-project requirements (snapshot)

This file preserves the official requirements Agent Factory is being mapped
against, exactly as published, so later documents can cite them without
reinterpreting them. Our interpretations live in
[RUBRIC_MAPPING.md](RUBRIC_MAPPING.md), not here.

## Source and version

| Item | Value |
| --- | --- |
| Course | AI Dev Tools Zoomcamp (DataTalks.Club) |
| Official repository | <https://github.com/DataTalksClub/ai-dev-tools-zoomcamp> |
| Branch / commit | `main` @ `49b7a73b013fb82e5e93eb189345a940b1ed24b1` |
| Commit date / subject | 2026-09-26T22:46:05+02:00, "Publish Homework 4 incident response exercise" |
| Retrieved | 2026-10-02 (13:28 UTC), read-only: `git ls-remote` + shallow clone |
| Requirements file | [`project/README.md`](https://github.com/DataTalksClub/ai-dev-tools-zoomcamp/blob/49b7a73b013fb82e5e93eb189345a940b1ed24b1/project/README.md) |
| Supporting files | `README.md` (Final Project and Certificate sections), `course.yaml`, `cohorts/README.md`, `cohorts/2026/cohort.yaml`, `cohorts/2026/README.md` |
| Context only (not requirements) | `docs/2026-syllabus-proposal.md`, `04-devops/01-devops-and-observability-for-ai-built-apps.md` |

**Why this repository is the official source.**
- It is owned by the DataTalksClub GitHub organization.
- `course.yaml` names it as the course repository and names
  `https://datatalks.club/docs/courses/ai-dev-tools-zoomcamp/` as the docs site.
- `cohorts/README.md` states that the website is generated from this
  repository ("The website's ingestion parser is the final authority").

So the repository is upstream of the website, and the website was not
consulted.

**Which cohort's rules apply.**
- `course.yaml` sets `current_cohort: "2026"`, and that content lives at the
  repository root, so the root `project/README.md` holds the current
  requirements.
- `cohorts/2025/` is a frozen archive of the previous cohort. It was **not**
  used.
- LLM Zoomcamp material found elsewhere on this machine belongs to a different
  course and was **not** used.

## Status of the requirements: DRAFT

The requirements page opens with this note (verbatim):

> The 2026 project requirements are currently a draft. You can use this page to
> see what we are preparing, but the final project rules, scoring, deadlines,
> and peer-review requirements may change before the cohort starts.

Also recorded at this commit:
- `cohorts/2026/cohort.yaml` gives the cohort dates as `start_date: 2026-08-31`
  and `end_date: 2026-09-28`.
- The repository publishes **no** project deadline, passing score, or
  submission form.
- Peer-review rules are "still being prepared".

**Re-check this file against the repository before any submission.**

## What the final project is

From `project/README.md` (verbatim):

> The final project applies the full course workflow to an end-to-end
> application of your own.
>
> You will:
>
> - build and deploy a full-stack app with AI assistance
> - document how AI tools were used and reviewed
> - add an agent extension pack around the project
> - audit and harden the project with open-source AI security, audit, and DevOps tools
> - review peer projects

The course `README.md` (Final Project section) describes the same project as
a list (verbatim):

> - Choose a problem and describe the expected system behavior
> - Build a frontend
> - Build a backend
> - Define frontend/backend communication with an OpenAPI contract
> - Add database or persistent storage
> - Test the application
> - Containerize the application
> - Deploy it so others can try it
> - Document how AI tools, prompts, workflows, agent instructions, MCP, or automation were used

## Expected repository contents

"Your project repo should include:"

```text
README.md
product-spec.md
AGENTS.md or equivalent project instructions
frontend/
backend/
openapi.yaml
docker-compose.yml
.github/workflows/
docs/
security/
ops/
```

"If you complete Module 5, it should also include an agent extension pack:"

```text
agent-capabilities/
agent-hooks/
mcp-server/
plugins/ or custom-agent/
docs/agent-extension-pack.md
docs/permissions.md
```

Note from `cohorts/README.md`: Module 5 (`05-agent-capabilities/`) has content
but "is not part of `cohorts/2026/`'s curriculum yet".

## Peer review and certificate

- "To evaluate projects, we use peer review. To get points for your project,
  you need to evaluate projects from your peers. Final peer-review requirements
  for the 2026 cohort are still being prepared."
- Course `README.md`: "Learners must pass the final project and complete the
  required peer reviews." Certificates are tied to "the final project and the
  required peer reviews".

## Scored criteria (draft)

There are 14 criteria. The maximum total is **30 points**: criteria 4 and 6
are scored 0–3 and all others 0–2. Each level's text is verbatim.

| # | Criterion | Levels |
| --- | --- | --- |
| 1 | Problem Description | 0: The problem is not described. · 1: The problem is described briefly, but it is unclear what the system does or what functionality is expected. · 2: The README clearly describes the problem, the system functionality, and expected behavior. |
| 2 | AI-Assisted Development Workflow | 0: No description of how AI tools were used. · 1: The project describes how AI tools were used to build the system. · 2: The project clearly documents the AI workflow, including prompts or task delegation, context files, manual review, and verification. |
| 3 | Technologies and System Architecture | 0: Technologies are not described or are unclear. · 1: The main technologies are mentioned, but their roles are not explained. · 2: The project clearly describes the frontend, backend, database, containerization, CI/CD, and how they fit together. |
| 4 | Frontend Implementation | 0: No frontend or non-functional frontend. · 1: A functional frontend exists, but structure is unclear or backend calls are scattered. · 2: The frontend is functional and well structured, with centralized backend communication. · 3: The frontend is functional, well structured, and includes tests covering core logic, with clear instructions for running them. |
| 5 | API Contract | 0: No OpenAPI specification. · 1: OpenAPI specification exists but is incomplete or loosely aligned with frontend needs. · 2: OpenAPI specification reflects frontend requirements and is used as the contract for backend development. |
| 6 | Backend Implementation | 0: No backend or backend does not follow the API contract. · 1: Backend implements required endpoints but has limited structure or documentation. · 2: Backend is well structured and follows the OpenAPI specification. · 3: Backend is well structured, follows the OpenAPI specification, and includes tests covering core functionality. |
| 7 | Database Integration | 0: No database or persistent storage. · 1: Database is integrated, but configuration or usage is minimal or poorly documented. · 2: Database layer is properly integrated, supports different environments, and is documented. |
| 8 | Containerization | 0: No containerization. · 1: Dockerfiles exist, but running the full system requires manual steps. · 2: The full system runs via Docker or Docker Compose with clear instructions. |
| 9 | Integration Testing | 0: No integration tests. · 1: Integration tests exist but are limited or not clearly separated from unit tests. · 2: Integration tests are clearly separated, cover key workflows, and are documented. |
| 10 | Deployment | 0: Application is not deployed. · 1: Deployment steps are described, but no working deployment is shown. · 2: Application is deployed to the cloud with a working URL or clear proof of deployment. |
| 11 | CI/CD Pipeline | 0: No CI/CD pipeline. · 1: CI pipeline runs tests automatically. · 2: CI/CD pipeline runs tests and deploys the application when tests pass. |
| 12 | Agent Extension Pack | 0: No agent extension work is included. · 1: The project includes basic project instructions or reusable prompts. · 2: The project includes a documented extension pack with project instructions, a reusable workflow, a subagent/specialist, an MCP tool/server, a hook or guardrail, and permission notes. |
| 13 | Security, Audit, and DevOps Hardening | 0: No security/audit/DevOps review artifacts. · 1: The project includes at least one audit or security scan artifact. · 2: The project includes PR audit output, deterministic security scan findings, agent/extension security notes, operational diagnosis output, and an AI tool/data policy. |
| 14 | Reproducibility | 0: Project cannot be run with the provided instructions. · 1: Project can be run, but setup or run instructions are incomplete. · 2: Clear instructions exist to set up, run, test, and deploy the system end to end. |

## Mandatory vs optional

The official material does **not** use the words mandatory, optional, or
bonus. What it does say:

- **Scored criteria.** All 14 are listed under "Draft Criteria". None is
  marked optional, and no passing threshold is published.
- **Repository contents.** The expected contents are phrased as "should
  include".
- **Extension pack.** The directory list is conditional: "If you complete
  Module 5". Criterion 12 nevertheless scores agent extension work. The
  material does not say how criterion 12 is treated for learners who skip
  Module 5, which is not yet in the 2026 curriculum.
- **Peer review.** It is required "to get points" and for the certificate.
  Its details are not yet published.

## Course context used for interpretation (not requirements)

These help explain terms in criteria 12–13. They are proposals or lesson
content, not scoring rules.

- **`docs/2026-syllabus-proposal.md`.** Describes the security/DevOps
  package as "PR audit output, Semgrep MCP findings, Agent Scan report,
  K8sGPT diagnosis, optional HolmesGPT incident summary, and an AI tool/data
  policy". It describes the extension pack as "project instructions, a custom
  skill, a subagent, a hook, an MCP tool/server, and either a small plugin or
  a custom agent".
- **Module 4 lesson.** Recurring security audits combine "a deterministic
  scanner (Semgrep), model review, and human validation". HolmesGPT and
  K8sGPT appear "without making Kubernetes a prerequisite for this course".
