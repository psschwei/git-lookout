# Proactive Cross-PR Conflict Detection

## The Problem

GitHub PRs only compare a branch against its base (usually `main`). Two PRs can each be clean against `main` individually, but will conflict with each other when both try to merge. Nobody finds out until one merges and the other is suddenly broken.

This gets worse with AI agents — agents work fast, in parallel, and frequently touch the same files. By the time a conflict surfaces, one PR is already merged and the other developer/agent is stuck resolving it after the fact.

## The Idea

A service that proactively scans all open PRs against each other and warns developers **before** either PR merges. The goal: "heads up, PR #42 and PR #57 are going to collide — both modify `processOrder()`."

## How It Works

### Trigger

- A new PR is opened against `main`
- An existing PR is updated (new commits pushed)
- On a cron schedule (e.g., every 15 minutes) as a background sweep

### Detection

For each updated PR, simulate merging it after every other open PR:

```bash
# For each pair of open PRs (A, B) targeting main:
# 1. Merge A into main (in memory)
result=$(git merge-tree --write-tree main pr-A-head)

# 2. Try merging B into the result
git merge-tree --write-tree $result pr-B-head

# 3. If exit code != 0, A and B will conflict
```

This requires a cloned repo on an Action runner or external compute — `git merge-tree` needs actual git objects, can't be done purely via the GitHub API.

### Context Gathering

When a conflict is detected between two PRs, gather context via the GitHub API:

- PR descriptions (`GET /pulls/{n}` → `body`)
- Commit messages (`GET /pulls/{n}/commits`)
- Changed files (`GET /pulls/{n}/files`)
- Linked issues (parse PR body for `#123` / `Fixes #123`)
- Review comments (`GET /pulls/{n}/comments`)

### Reporting

Post a comment on both PRs:

```markdown
⚠️ **Conflict detected with PR #57**

This PR and #57 both modify `processOrder()` in `src/api/orders.ts`.

- **This PR (#42)**: Add input validation to order processing
- **PR #57**: Add audit logging to order processing

These changes appear **complementary** (validation + logging), but will
produce a merge conflict because both modify the same function body.

**Suggestion**: Coordinate merge order. If #42 merges first, #57 will need
to integrate the validation logic. Consider merging #42 first since it's
smaller and already approved.
```

Optionally, create a GitHub Check Run to surface the warning in the PR's checks tab.

### Agentic Enhancement (future)

Beyond just detecting conflicts, an agent could:

1. **Classify the conflict**: Are the changes complementary (both add different things), contradictory (both change the same thing differently), or duplicative (both implement the same feature)?
2. **Suggest merge order**: Based on PR size, approval status, and dependency analysis, recommend which should merge first.
3. **Pre-generate the resolution**: Show what the merged code would look like if both PRs are combined, so the second developer doesn't have to figure it out from scratch.
4. **Detect silent conflicts**: Two PRs merge cleanly in Git but produce broken code (e.g., one renames a function, the other adds a call to the old name in a different file). Use dependency graph analysis or test execution to catch these.

## Architecture

```
GitHub webhook (pull_request: opened/synchronize)
       │
       ▼
GitHub Action or external service
       │
       ▼
1. Fetch all open PRs targeting the same base branch
   GET /repos/{owner}/{repo}/pulls?base=main&state=open
       │
       ▼
2. For each pair, run git merge-tree simulation
   (requires cloned repo on the runner)
       │
       ▼
3. If conflict detected:
   a. Gather context from both PRs via GitHub API
   b. Optionally: run entity-level analysis (Weave) to
      classify false vs real conflicts
   c. Optionally: pass context to LLM for intent analysis
       │
       ▼
4. Post results:
   - PR comment on both PRs
   - GitHub Check Run (warning status)
```

## Complexity & Scaling

- **Pairwise comparisons**: For n open PRs, worst case is n-1 comparisons per PR update. For most repos (< 50 open PRs) this is trivial.
- **Clone cost**: Need a cached checkout of the repo. For large repos, use shallow clone or a persistent runner with an up-to-date clone.
- **`git merge-tree` is fast**: It works entirely in memory (no working directory writes). Hundreds of comparisons can run in seconds.
- **Caching**: Only re-check pairs where at least one PR has been updated since the last check. Store last-checked SHAs to skip unchanged pairs.

## Implementation Options

### Option A: GitHub Action

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  conflict-scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Fetch all open PR branches
        run: |
          # fetch all open PR heads
          gh pr list --base main --state open --json number,headRefName \
            | jq -r '.[].headRefName' \
            | xargs -I {} git fetch origin {}
      - name: Run pairwise conflict detection
        run: ./scripts/detect-conflicts.sh
```

Pros: Free for public repos, easy to set up.
Cons: GitHub Actions minutes for private repos, cold clone on every run.

### Option B: GitHub App + external compute

A lightweight web service (like Weave's `weave-github` pattern):
- Receives `pull_request` webhooks
- Maintains a warm clone of the repo
- Runs conflict detection on every PR update
- Posts results via the Checks API

Pros: Faster (warm clone), more control, can run on cheap compute.
Cons: More infrastructure to manage.

## What Exists Today

- **GitHub merge queue**: Only detects conflicts when PRs enter the queue (too late).
- **Graphite merge queue**: Stack-aware, catches conflicts between stacked PRs, but only within managed stacks — not across unrelated PRs.
- **Weave (weave-github)**: Analyzes individual PRs for entity-level conflicts with their base branch. Does not compare PRs against each other.
- **Mergify**: Merge automation with speculative checks, but no cross-PR conflict detection before merge time.

Nobody does proactive, pairwise, cross-PR conflict scanning with context-aware reporting.

## Open Questions

- Should the agent suggest a specific resolution, or just warn? (Warning is safer and simpler to start.)
- How to handle the n² problem for repos with many open PRs? (Priority: only check PRs that touch overlapping files.)
- Should silent conflicts (clean merge but broken code) be in scope for v1? (Probably not — start with textual conflicts, add semantic analysis later.)
- What's the right balance between noise and helpfulness? (Only warn on actual entity-level conflicts, not every file overlap.)
