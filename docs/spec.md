# git-lookout: Proactive Cross-PR Conflict Detection

## Overview

A service that detects merge conflicts between branches before they become a problem. The core use case: scan open pull requests against each other and warn developers before either PR merges. A secondary use case: let developers and coding agents check a branch for conflicts before even opening a PR.

Three entry points share the same detection engine:

- **GitHub App (webhook)** — automatically scans PRs on every push, posts conflict comments
- **API endpoint** — on-demand check for a branch against all open PRs, returns JSON
- **CLI** — local check against open PRs, no server needed

## Architecture

A lightweight service running on a small VM or Kubernetes pod.

```
GitHub ──webhook──▶ [FastAPI server]
                         │
Agent/User ──POST──▶ [/api/check]
                         │
                         ▼
                    [Core engine]
                    ├─▶ git fetch (bare clone on disk)
                    ├─▶ file overlap pre-filter (SQLite)
                    ├─▶ git merge-tree for overlapping pairs
                    ├─▶ analysis pipeline (empty in v1)
                         │
                         ▼
                    [Output]
                    ├─▶ Webhook path: post/update PR comments via GitHub API
                    └─▶ API path: return JSON response

CLI (standalone, no server):
    local repo ──▶ [core engine] ──▶ terminal output

                    [Persistent disk]
                         ├─ /data/repos/{owner}/{repo}.git  (bare clone)
                         └─ /data/git-lookout.db  (SQLite)
```

### Components

The app is structured as two layers: a **platform-agnostic core** and **platform adapters**.

**Core** (pure git, no GitHub dependency):

1. **Git manager** — maintains bare clones, runs `git fetch` + `git merge-tree`
2. **Conflict detector** — pre-filters branch pairs by file overlap, runs pairwise merge simulation, returns structured conflict results

**Platform adapter** (GitHub for v1, others later):

3. **Webhook receiver** — handles `pull_request` events (opened, synchronize, reopened, closed)
4. **Reporter** — posts/updates/removes conflict comments on PRs via the GitHub API
5. **State store** — SQLite database tracking open PRs, changed files, and check results

The core only needs a repo path and a list of branch refs to compare. It knows nothing about PRs, comments, or webhooks. The platform adapter translates between the hosting platform's concepts (PRs, MRs, webhooks) and the core's inputs/outputs.

This separation is intentional but lightweight for v1 — keep the git operations in their own module, keep the GitHub API calls in theirs. No formal adapter interface yet. The seam will be obvious when it's time to add another platform (GitLab, Bitbucket).

Note: even the CLI mode needs the GitHub API to fetch the list of open PRs and their branches. "Platform-agnostic" refers to the core git operations (merge-tree, file diffing), not the full workflow. Every entry point currently depends on GitHub for PR metadata.

### GitHub App Permissions

- `pull_requests: write` — post and update comments
- `contents: read` — clone and fetch repo contents
- Subscribe to `pull_request` webhook events

## Entry Points

The core conflict detection logic has three entry points. All three use the same core: file overlap pre-filter → merge-tree → analysis pipeline. They differ in trigger and output.

### 1. Webhook (PR lifecycle)

The primary mode. Runs automatically when:

- A new PR is opened against a tracked base branch
- An existing PR is updated (new commits pushed)
- An existing PR is reopened

When a PR is closed or merged, the app cleans up: removes the PR from tracking and updates/removes any conflict comments on other PRs.

**Output**: Comments posted on both PRs.

### 2. API endpoint (pre-PR check)

On-demand check for a branch that doesn't have a PR yet. A developer or agent pushes a branch and calls the API before opening a PR.

```
POST /api/check
{
  "repo": "owner/repo",
  "ref": "my-feature-branch"
}

→ Response:
{
  "conflicts": [
    {
      "pr_number": 42,
      "title": "Add input validation to order processing",
      "conflicting_files": ["src/api/orders.ts"],
      "conflict_regions": [...]
    }
  ]
}
```

The server fetches the ref into its bare clone, runs merge-tree against all open PRs, and returns structured results. The branch must be pushed to the remote — the server can only see refs that exist on the remote.

**Authentication**: The endpoint must verify the caller has access to the requested repo. Options: require a GitHub token in the request and validate it against the repo, or use API keys scoped to specific repos/installations. Without auth, anyone who can reach the server could probe conflict status for any installed repo.

**Output**: JSON response to the caller.

**As an MCP tool**: This endpoint can be exposed as an MCP tool so coding agents can call it directly:

```json
{
  "name": "check_conflicts",
  "description": "Check if a branch will conflict with any open PRs",
  "parameters": {
    "repo": "owner/repo",
    "ref": "my-feature-branch"
  }
}
```

Agent workflow: write code → push branch → call `check_conflicts` → if conflicts, adjust before opening PR.

### 3. CLI (local check)

A standalone command-line tool that runs entirely on the developer's machine. No server needed.

```bash
git-lookout check --repo owner/repo
```

The CLI:
1. Fetches all open PR branches into the local repo
2. Runs merge-tree against the current HEAD
3. Prints results to the terminal

Does not require the branch to be pushed — runs against local commits. Uses the same core detection logic but doesn't need the server infrastructure.

**Output**: Terminal output.

### Summary

| Entry point | Trigger | Needs push? | Output |
|---|---|---|---|
| Webhook | PR event | Yes (PR exists) | Comment on PR |
| API endpoint | Explicit call | Yes (ref on remote) | JSON response |
| CLI | Manual | No | Terminal output |

## Detection Flow

### 1. Pre-filter by file overlap

Before running any git operations, compare changed file lists to find PR pairs that touch at least one common file. This reduces the number of pairwise `merge-tree` checks from n-1 (against every other open PR) to just the handful that could actually conflict.

Changed file lists are cached in SQLite and refreshed on each PR update via the GitHub API (`GET /pulls/{n}/files`).

### 2. Pairwise merge simulation

For each overlapping pair, simulate merging both PRs using `git merge-tree`:

```bash
# Merge PR A into the base branch (in memory, no working tree)
result=$(git merge-tree --write-tree base pr-A-head)

# Try merging PR B into the result
git merge-tree --write-tree $result pr-B-head

# Non-zero exit code = conflict
```

`git merge-tree` operates entirely in memory — no working directory writes, no checkout. Hundreds of pairwise checks can run in seconds.

### 3. SHA-based skip

If both PRs in a pair are at the same SHAs as the last check, skip the pair. Only re-check when at least one PR has new commits.

## Analysis Pipeline

The flow from detection to reporting passes through an **analysis pipeline**: an ordered list of analyzers that enrich the conflict result before it gets reported.

```
detect → [analyzer 1] → [analyzer 2] → ... → report
```

In v1, the pipeline is empty — detection feeds directly into reporting. Future AI/agentic capabilities are added by appending analyzers to the pipeline. No changes to detection, reporting, or the trigger/webhook layer.

### Conflict result data structure

The conflict result is a single data structure that accumulates information as it passes through the pipeline. v1 fields are always present. Future fields start as `None` and get populated by analyzers that are added later.

```python
@dataclass
class ConflictResult:
    # v1 — always present
    pr_a: PRInfo
    pr_b: PRInfo
    conflicting_files: list[str]
    conflict_regions: list[ConflictRegion]  # file + line ranges from merge-tree

    # future — populated by analyzers, None until then
    classification: str | None = None        # "complementary" | "contradictory" | "duplicative"
    suggested_merge_order: MergeOrder | None = None
    proposed_resolution: str | None = None
    confidence: float | None = None
```

The reporter renders whatever fields are present. If a field is `None`, the corresponding section is omitted from the comment. No version-gated conditional logic — just "render what's there."

### Pipeline execution

```python
# v1: no analyzers
analyzers = []

# future: add stages as needed
# analyzers = [
#     ClassifyConflict(),
#     SuggestMergeOrder(),
#     GenerateResolution(),
# ]

conflict = detect_conflict(repo, pr_a, pr_b)
for analyzer in analyzers:
    conflict = analyzer.enrich(conflict)
report(conflict)
```

Each analyzer takes a `ConflictResult`, populates its fields, and returns it. New capability = new analyzer class appended to the list. Everything else is unchanged.

### Planned analyzers (future)

- **ClassifyConflict**: Determines whether the changes are complementary (both add different things), contradictory (both change the same thing differently), or duplicative (both implement the same feature). Could use heuristics or an LLM.
- **SuggestMergeOrder**: Recommends which PR should merge first based on size, approval status, priority labels, and dependency analysis.
- **GenerateResolution**: Produces the merged code showing what the combined changes would look like, so the second developer doesn't start from scratch.

LLM integration is an implementation detail inside individual analyzers. The rest of the system doesn't know or care whether an analyzer uses an LLM, a heuristic, or static analysis.

### What this means for v1 code

Minimal overhead:

1. Define `ConflictResult` as a dataclass with optional fields
2. Include the pipeline loop in the flow (runs zero iterations in v1)
3. Have the reporter check for optional fields before rendering

This avoids rewrites when adding analysis capabilities later. New analyzers are additive — a new file, a new class, appended to the list.

## Reporting

The output format depends on the entry point. The webhook path posts PR comments (below). The API endpoint returns the same information as structured JSON (see the Entry Points section for the response format). Future entry points (CLI, MCP tool) will render the same `ConflictResult` data in their own format.

### Webhook: conflict detected

Post a comment on both PRs:

```markdown
⚠️ **Potential conflict with PR #57**

This PR and #57 both modify the following files:
- `src/api/orders.ts`

**This PR (#42)**: Add input validation to order processing
**PR #57**: Add audit logging to order processing

Consider coordinating merge order to avoid conflicts.
```

### Webhook: conflict resolved

When a previously conflicting pair becomes clean (one PR is rebased, updated, or closed), update the existing comment:

```markdown
~~⚠️ **Potential conflict with PR #57**~~

✅ **Resolved** — this PR no longer conflicts with #57.
```

### Idempotency

The app stores the GitHub comment ID for each conflict comment it posts. On subsequent checks, it updates the existing comment rather than posting a new one. This prevents duplicate comments when a PR is pushed to multiple times.

## Data Model

SQLite database with four tables.

### repositories

Tracks repos the app is installed on.

```sql
CREATE TABLE repositories (
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    installation_id INTEGER NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    UNIQUE(owner, name)
);
```

### pull_requests

Open PRs currently being tracked.

```sql
CREATE TABLE pull_requests (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repositories(id),
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    title TEXT,
    author TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(repo_id, pr_number)
);
```

### pr_files

Changed files per PR, used for the overlap pre-filter.

```sql
CREATE TABLE pr_files (
    pr_id INTEGER NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    PRIMARY KEY (pr_id, file_path)
);

CREATE INDEX idx_pr_files_path ON pr_files(file_path);
```

The index on `file_path` makes the overlap query fast:

```sql
SELECT DISTINCT pf2.pr_id
FROM pr_files pf1
JOIN pr_files pf2 ON pf1.file_path = pf2.file_path
WHERE pf1.pr_id = :pr_id
  AND pf2.pr_id != :pr_id;
```

### conflict_checks

Results of pairwise conflict checks.

```sql
CREATE TABLE conflict_checks (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repositories(id),
    pr_a_number INTEGER NOT NULL,
    pr_b_number INTEGER NOT NULL,
    pr_a_sha TEXT NOT NULL,
    pr_b_sha TEXT NOT NULL,
    status TEXT NOT NULL,       -- 'conflict' | 'clean'
    conflicting_files TEXT,     -- JSON array, e.g. '["src/api/orders.ts"]'
    comment_id_a INTEGER,       -- GitHub comment ID on PR A
    comment_id_b INTEGER,       -- GitHub comment ID on PR B
    checked_at TEXT NOT NULL,
    UNIQUE(repo_id, pr_a_number, pr_b_number)
);
```

PR pairs are stored in canonical order: lower PR number in `pr_a_number`, higher in `pr_b_number`. This ensures each pair has exactly one row.

**Note**: This table only tracks PR-to-PR checks from the webhook path. API endpoint checks (branch vs. open PRs) are **stateless** — results are computed on demand and returned to the caller without being stored. There are no comments to track and no lifecycle to manage. If the caller wants to check again, they just call the endpoint again.

The API endpoint is a read-only consumer of the other tables. It reads `pull_requests` and `pr_files` (maintained by the webhook path) to know which open PRs exist and which files they touch, then runs `merge-tree` against the overlapping ones. It doesn't write to any table.

## Lifecycle

### PR opened or updated

1. Upsert into `pull_requests` with current head SHA
2. Refresh `pr_files` (delete old entries, insert current changed files)
3. Query for overlapping PRs via `pr_files` join
4. For each overlapping pair:
   - Skip if both SHAs match the last check in `conflict_checks`
   - Run `git merge-tree`
   - Upsert into `conflict_checks`
   - If status changed: post, update, or remove comments accordingly

### PR closed or merged

1. Delete from `pull_requests` (cascades to `pr_files`)
2. For each `conflict_checks` row involving this PR:
   - If a conflict comment exists on the other PR, update it to "resolved"
   - Delete the `conflict_checks` row

## Infrastructure

### Deployment model

A single app instance serves all installed repos. This is the natural model for a GitHub App — one webhook endpoint receives events for every repo the app is installed on.

**v1: single instance, co-located storage.** The app, SQLite database, and bare clones all live on one machine (VM or K8s pod with a persistent volume). This is simple and sufficient for moderate scale (hundreds of repos, thousands of open PRs). SQLite handles the read/write volume easily, and the bare clones already tie the app to a specific disk.

**Future scaling considerations.** If horizontal scaling is needed later:

- **Database**: Move from SQLite to Postgres. The schema works as-is — just swap the driver. This decouples the DB from the app instance and allows multiple workers.
- **Bare clones**: These are the harder problem. `git merge-tree` needs local git objects. Options:
  - Shared filesystem (NFS/EFS) — works but adds latency to git operations
  - Worker-per-repo with local clones — cleaner isolation, more operational complexity
  - Clone on demand — loses the warm clone advantage, only viable for small repos
- **Webhook routing**: A coordinator receives webhooks and dispatches to the worker that owns the relevant repo's clone.

Don't design for this now. The single-instance model goes far, and the data model already supports multi-repo via the `repositories` table and `repo_id` foreign keys.

### Data freshness

Webhooks handle the happy path, but events can be missed (app downtime, network issues, GitHub delivery failures). A **periodic reconciliation sweep** (every 5-15 minutes) syncs state against the GitHub API:

1. Fetch all open PRs from `GET /repos/{owner}/{repo}/pulls?state=open`
2. Add any PRs missing from the database (missed open events)
3. Update any PRs with stale head SHAs (missed update events)
4. Remove any PRs that are no longer open (missed close events)
5. Re-run conflict checks for any pairs that changed

This also catches **base branch moves** — when a PR merges into main, the conflict landscape changes for all remaining open PRs, but only the merged PR fires a webhook. The sweep picks up the rest.

The sweep is cheap: one API call to list open PRs per repo, plus a handful of file-list fetches for any that are out of sync. Well within GitHub API rate limits.

### Data durability

Almost all data is **reconstructable from GitHub**:

| Table | Reconstructable? | How |
|---|---|---|
| `repositories` | Yes | GitHub App installation events |
| `pull_requests` | Yes | `GET /pulls?state=open` |
| `pr_files` | Yes | `GET /pulls/{n}/files` |
| `conflict_checks` | Mostly | Re-run merge-tree for all pairs |
| `conflict_checks.comment_id_*` | Partially | Search for comments by bot user |

The only unique data is **comment IDs** — pointers to comments already posted. If lost, the app may post duplicate comments until it re-discovers existing ones.

**Recovery from total data loss:**

1. App starts with empty database
2. Reconciliation sweep runs immediately on startup
3. Fetches all open PRs, populates `pull_requests` and `pr_files`
4. Runs conflict checks for all overlapping pairs
5. Posts comments for detected conflicts (may duplicate existing comments)

Full recovery in under a minute for most repos.

**Backup options (v1):** Litestream for continuous SQLite replication to object storage (S3/GCS) is low-effort and gives point-in-time recovery. Alternatively, skip backups entirely and rely on reconstruction — acceptable for a v1 given the low cost of duplicate comments.

### Requirements

- Small VM (e.g., $5-10/mo) or a Kubernetes pod with a persistent volume
- Persistent disk for bare clones and SQLite database
- TLS termination for webhook endpoint (reverse proxy with Let's Encrypt, or platform-provided)

### Resource footprint

- **CPU**: Minimal. `merge-tree` is fast; the app is idle between webhooks.
- **Memory**: Bare clone loaded by git as needed. A few hundred MB for large repos.
- **Disk**: Bare clone size + SQLite (small). A few GB covers most repos.

## Tech Stack

**Language: Python**

Chosen for broad familiarity and contributor accessibility.

### Core dependencies

- **FastAPI** — webhook receiver. Lightweight, async-capable, good ecosystem.
- **PyGithub** or **githubkit** — GitHub API client. Handles REST calls for PR metadata, posting comments, etc.
- **sqlite3** (standard library) — database driver. No ORM; raw SQL for four simple tables.
- **git CLI** (via `subprocess.run`) — for `git fetch`, `git merge-tree`, and bare clone management. No Python git libraries needed.

### Supporting

- **uvicorn** — ASGI server to run FastAPI
- **pydantic** — request/response validation (comes with FastAPI)
- **cryptography** or **PyJWT** — GitHub App JWT generation for authentication

### What we're not using

- **Heavy ORMs (SQLAlchemy, Django ORM)** — overkill for four tables with simple queries
- **Python git libraries (GitPython, pygit2)** — add complexity without benefit; `merge-tree` support is uneven. The git CLI is simpler and guaranteed to support what we need.
- **Full frameworks (Django, etc.)** — more structure than this app needs

## Scope

### In scope (v1)

- Textual conflict detection via `git merge-tree`
- File overlap pre-filtering
- Webhook entry point: PR comments with conflict details, comment lifecycle (post / update / resolve)
- API entry point: on-demand branch check, JSON response
- Reconciliation sweep for data freshness
- Multi-repo support

### Out of scope (future)

- **CLI entry point**: Standalone local check tool. Depends on the same core engine but requires its own packaging and distribution. Not needed for the initial service.
- **Silent conflict detection**: PRs that merge cleanly but produce broken code (e.g., function rename + new call to old name). Requires dependency graph analysis or test execution.
- **LLM-powered analysis**: Classify conflicts as complementary/contradictory/duplicative, suggest merge order, pre-generate resolutions.
- **GitHub Check Runs**: Surface conflicts in the PR checks tab in addition to comments.
- **MCP tool**: Wrapping the API endpoint as an MCP tool for coding agents. The API endpoint is the prerequisite; MCP packaging comes later.
- **PR priority**: Allow certain PRs to be marked as higher priority (e.g., critical bugfixes). In v1, conflict comments are symmetric — both PRs get the same warning and humans decide who yields. With priority, the system could make directional recommendations: "PR #500 is marked critical — consider rebasing this PR to accommodate it." Priority becomes load-bearing when the system starts suggesting merge order or auto-resolving conflicts. Likely signal sources: GitHub labels (`critical`, `priority:high`), branch naming conventions (`hotfix/*`), or repo-level config (`.git-lookout.yml`).
