# git-lookout

**Detect, understand, and resolve merge conflicts — before they happen.**

## The problem

GitHub tells you about merge conflicts at the worst possible time — after the work is done. Two developers modify the same code in parallel, both PRs look clean, and nobody finds out they collide until one merges and the other breaks.

This is getting dramatically worse. AI coding agents work fast, in parallel, and across many files. A team running multiple agents on the same repo will hit this constantly. The rate of parallel changes is increasing, but the tooling for coordinating those changes hasn't kept up. The coordination problem that was once occasional is becoming constant — and nobody is solving it.

## The vision

git-lookout is an intelligent coordination layer for parallel development. It does three things:

1. **Detect** — proactively find conflicts between open PRs and in-progress branches, before anyone tries to merge
2. **Understand** — classify conflicts as complementary, contradictory, or duplicative, and recommend which changes should land first
3. **Resolve** — generate the merged code automatically, so the second developer or agent doesn't start from scratch

Today's merge tooling is reactive: it tells you about a conflict after both sides have done the work. git-lookout is proactive: it watches the whole repo, understands what's happening across branches, and helps coordinate — whether the contributors are humans, agents, or both.

## How it works

Pairwise merge simulation across all open branches, entirely in memory. Completes in milliseconds. A file overlap pre-filter keeps it fast even with many open PRs. An analysis pipeline classifies and resolves conflicts using heuristics and LLMs.

## How people use it

- **GitHub App**: installs on a repo, automatically monitors open PRs, and comments when two will conflict — with context on why and what to do about it. Passive — just works for every PR, every contributor.
- **Developer tools** (API, MCP, CLI): developers and coding agents check a branch for conflicts before opening a PR. Available as an MCP tool so any compatible agent can use it natively. Find out while you can still adjust, not after the work is done.

## Architecture

Lightweight. A single small server maintains bare git clones and a SQLite database. Detection runs in milliseconds. The analysis pipeline is pluggable — each capability (classification, merge order, resolution) slots in as a new stage without changing the layers around it.

The whole thing fits on a $5/month VM. Built in Python, open source, designed for contributors.
