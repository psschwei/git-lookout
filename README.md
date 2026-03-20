# git-lookout

Detect, understand, and resolve merge conflicts — before they happen.

## The Problem

GitHub PRs only compare against their base branch. Two PRs can each look clean individually but will conflict when both try to merge. You only find out after both are done — and one breaks.

This problem is getting worse with AI coding agents that work fast, in parallel, and frequently touch the same files.

## What git-lookout Does

git-lookout watches your repository and proactively detects conflicts between open PRs. Instead of "merge breaks, now you need to fix it," you get an early warning while both branches are still in flight.

**Three ways to use it:**

- **Webhook** — Install the GitHub App and it automatically monitors all open PRs. When a conflict is detected, both PRs get a comment.
- **API** — `POST /api/check` with a repo and branch name. Check for conflicts before you even open a PR. Useful for coding agents via MCP.
- **CLI** — `git-lookout check --repo owner/repo` for a local check with no server needed.

**What you get back:**

- Which files conflict between which PRs
- Conflict regions (file + line ranges)
- *(planned)* Conflict classification: complementary, contradictory, or duplicative
- *(planned)* Suggested merge order
- *(planned)* Pre-generated resolution so the second developer doesn't start from scratch

## Architecture

The system has two layers:

- **Core** — platform-agnostic git operations using `git merge-tree`. No GitHub dependency.
- **Platform adapter** — GitHub App webhook receiver, PR commenter, and state store (SQLite).

Key design choices: raw git CLI (not GitPython), SQLite with no ORM, FastAPI for the webhook/API surface.

## Status

Early development. Currently implemented:

- `BareCloneManager` — maintains bare clones on disk for efficient fetching
- Technical spec and build plan in `docs/`

In progress: merge-tree wrapper, ConflictResult dataclass, analysis pipeline.

## Development

**Requirements:** Python 3.11+

```bash
git clone https://github.com/psschwei/git-lookout.git
cd git-lookout
pip install -e .
pytest tests/
```

See [`docs/spec.md`](docs/spec.md) for the full technical specification and [`docs/build-plan.md`](docs/build-plan.md) for the implementation roadmap.
