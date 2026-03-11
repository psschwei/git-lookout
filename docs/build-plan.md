# git-lookout: Build Plan

The goal is to get to a working API endpoint where a developer or coding agent can check a branch for conflicts against all open PRs before opening a PR. The webhook/comment path comes after.

## Build order

```
Phase 1 ──▶ Phase 2 ──▶ Phase 3 ──▶ Phase 4
 (core)      (data)      (API)      (webhooks + comments)
                          ▲
                     first useful
                      milestone
```

Each phase depends on the previous one. Phase 3 is the first "useful to a real user" milestone.

---

## Phase 1: Core engine

The foundation. No server, no GitHub API, no database. Just git operations and conflict detection that can be run from a test script.

### Deliverables

- **Bare clone manager**: initialize and fetch into a bare clone on disk
- **merge-tree wrapper**: run `git merge-tree --write-tree` between two refs, parse the output into structured results (conflicting files, conflict regions)
- **ConflictResult dataclass**: with v1 fields always present and optional fields for future analyzers (classification, suggested merge order, proposed resolution, confidence)
- **Analysis pipeline loop**: the `for analyzer in analyzers` pass-through, wired in but empty
- **File overlap check**: given two lists of changed files, return the intersection

### Validation

Create a local test repo with two branches that conflict. Run the core engine. Verify it:
- Detects the conflict
- Correctly identifies the conflicting files
- Correctly extracts conflict regions (file + line ranges)
- Returns clean results for branches that don't conflict

### Risk

This is the riskiest phase. It validates that `merge-tree` output is parseable and gives us what we need. Prototype this first before building anything on top.

---

## Phase 2: Data layer + GitHub integration

Connect to GitHub and start tracking open PRs in the database.

### Deliverables

- **GitHub App registration**: register the app on GitHub (permissions: `contents: read`, `pull_requests: write`; events: `pull_request`)
- **GitHub API client**: authenticate as the GitHub App (JWT + installation token), list open PRs, get changed files per PR
- **SQLite schema**: all four tables (`repositories`, `pull_requests`, `pr_files`, `conflict_checks`) with indexes
- **Reconciliation sweep**: poll GitHub for open PRs targeting the default branch, then:
  - Add PRs missing from the database
  - Update PRs with stale head SHAs (refresh `pr_files`)
  - Remove PRs that are no longer open
  - Run on a configurable interval (default: every 5 minutes)

### Validation

Point the sweep at a real repo with open PRs. Run it. Verify:
- `pull_requests` table has the right PRs with correct SHAs
- `pr_files` table has the right changed files for each PR
- Run again with no changes — verify it no-ops (no unnecessary writes)
- Push a commit to a PR, run again — verify it updates the SHA and refreshes files
- Close a PR, run again — verify it removes the PR and its files

---

## Phase 3: API endpoint

The first useful milestone. A server that accepts a branch ref and returns conflicts against all open PRs.

### Deliverables

- **FastAPI server**: with uvicorn
- **`POST /api/check` endpoint**: accepts `{ "repo": "owner/repo", "ref": "branch-name" }`, returns conflict results as JSON
- **Auth middleware**: validate that the caller has access to the requested repo (GitHub token validation or scoped API keys)
- **Request flow**:
  1. Fetch the ref into the bare clone (`git fetch origin <ref>`)
  2. Get the ref's changed files (diff against base branch)
  3. Query `pr_files` for overlapping open PRs
  4. Run `merge-tree` for each overlapping PR
  5. Run the (empty) analysis pipeline
  6. Return `ConflictResult` list as JSON
- **Background sweep**: run the reconciliation sweep on a timer to keep PR data fresh

### Validation

Using a real repo:
- Push a branch that conflicts with an open PR, call the endpoint, verify it returns the correct conflict
- Push a branch that doesn't conflict with anything, verify it returns an empty list
- Push a branch that overlaps files with a PR but doesn't actually conflict (e.g., changes different functions in the same file), verify it returns clean
- Call with an invalid repo or ref, verify it returns appropriate errors
- Call without auth, verify it rejects the request

---

## Phase 4: Webhook + PR comments

Add the passive monitoring mode. PRs are automatically scanned on every push, and conflict comments are posted without anyone having to ask.

### Deliverables

- **Webhook receiver**: handle `pull_request` events (opened, synchronize, reopened, closed) with signature verification
- **PR-to-PR conflict detection**: on each event, run the triggering PR against all overlapping open PRs, populate `conflict_checks` table
- **Comment templates**: conflict detected, conflict resolved
- **Comment lifecycle**:
  - Post a comment on both PRs when a new conflict is detected
  - Update the comment when the conflict details change (new files, etc.)
  - Update the comment to "resolved" when the conflict is resolved
  - Store comment IDs in `conflict_checks` for idempotent updates
- **SHA-based skip**: don't re-check pairs where both SHAs are unchanged since the last check
- **Cleanup on PR close/merge**: remove PR from tracking, update conflict comments on other PRs to "resolved"

### Validation

Using a real repo:
- Open two PRs that conflict — verify comments appear on both
- Push to one PR to resolve the conflict — verify comments update to "resolved"
- Push to one PR to introduce a new conflict — verify new comment appears
- Close a PR — verify its conflict comments on other PRs update to "resolved"
- Push to a PR multiple times — verify no duplicate comments (idempotency)
