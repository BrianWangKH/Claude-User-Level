# Claude Code Dev Flow

A Claude Code home configuration (`~/.claude`) that turns Claude Code into a small, test-first development team, with the rules that must always hold enforced by hooks instead of prompts.

[Traditional Chinese](README.zh-TW.md)

## Features

- **Separate planning and building.** You agree on a plan in an interactive session; a headless worker builds it in its own git worktree.
- **Test-first subagent team.** A test planner, spec reviewer, QA, developer and a plan-blind code reviewer, each limited to its own job.
- **Hook-enforced rules.** Guards block out-of-role edits, quality gates check complexity, coverage and mutation results, and budgets stop runaway runs.
- **Real-data verification.** A finished branch is checked by scripted commands and an independent verifier agent before it can merge.
- **Hands-off local mode.** Finished topics are verified, merged into your local `main` and cleaned up automatically; queued topics start in turn.
- **Worker pane.** A side panel showing each worker's progress, spend and pending decisions, with launch / resume / stop controls.

## How it works

```
/repo-plan (you + planning session)
    │  writes the plan, estimate and real-data checks
    ▼
wt launch / wt queue
    │
worker (headless claude, own worktree)
    │  qa-planner → spec-reviewer → qa → developer → code-reviewer
    │  hooks: guards, quality gates, budgets
    ▼
STATUS: done
    │
verify.py (scripted checks) → verifier agent (judgement on real data)
    │
auto-merge into local main → wrap-up → next queued topic
```

When a decision needs a person, the worker stops and notifies you; you answer in the planning session and resume it.

## Requirements

- Linux or WSL2
- [Claude Code](https://docs.claude.com/en/docs/claude-code) with a logged-in account
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- git
- bubblewrap (`bwrap`), used to run quality gates without network access
- Go toolchain, only for Go repositories

## Installation

1. Back up your existing `~/.claude`.
2. Clone this repository as `~/.claude`. The included `settings.json` registers every hook, loads the worker pane plugin and sets the sandbox; review its `sandbox` and `permissions` sections for your machine.
3. Add your personal values to `~/.claude/config/local.json` (see [Configuration](#configuration)) and your personal rules, such as reply language, to a file under `~/.claude/rules/`. Both are git-ignored.
4. Check the configuration:

   ```bash
   python3 ~/.claude/config/homeconfig.py check
   ```

## Quick start

1. Create a repository with the starter kit:

   ```bash
   bash ~/.claude/templates/new-repo.sh my-app --lang python
   ```

   It creates the main checkout and a sibling `worktrees/` directory, commits a skeleton and enables local mode.
2. Open Claude Code in the main checkout and type `/repo-plan`. Discuss the first topic until the plan is settled.
3. Approve and run it with `wt queue <topic>` (or `wt launch <topic>`).
4. Watch progress with `wt pane` or `wt status`. The topic merges into `main` on its own when every check passes.

## Usage

`wt` commands are handled by a hook and cost no model turn.

| Command | What it does |
|---|---|
| `/repo-plan` | Plan topics, start a repository, check progress |
| `wt queue [<topic> ...]` | Queue topics and start those that can run; no argument shows the queue |
| `wt queue drop <topic>` | Remove a topic that has not started |
| `wt launch <topic>` | Start a worker for a topic without the queue |
| `wt resume <topic>` | Continue a topic after a decision or a stop |
| `wt stop <topic>` | Stop a worker |
| `wt status [<topic>]` | Show what each run is doing |
| `wt merge <topic>` | Approve a merge that was held for a person |
| `wt wrapup <topic>` | Finish a wrap-up that did not complete |
| `wt reconcile [--dry-run]` | Find and finish owed wrap-ups and follow-up runs |
| `wt pane` | Open or close the worker pane |
| `wt auto [on\|off]` | Launch or resume workers automatically after planning (this session only) |
| `wt local [on\|off]` | Switch between local mode and PR mode |
| `/pr-conflict <topic>` | PR mode: hand a conflicting PR to the worker to merge |
| `/finish-worktree <topic>` | PR mode: clean up after a PR merges |
| `/feature-flow <topic>` | Run the build flow by hand in a worktree |

### Local mode and PR mode

| | Local mode (default) | PR mode |
|---|---|---|
| Source of truth | local `main` | `origin`'s default branch |
| Merge | automatic, when every condition holds | you push and open a PR |
| Clean-up | automatic | `/finish-worktree` after the PR merges |

A topic merges automatically only when no guard alert is open, the branch contains the latest `main`, no protected path changed (settings, CI, dependency files), the code review approved, all tests and checks pass, and both real-data layers passed. Otherwise you are notified and decide with `wt merge`.

## Configuration

All settings are read through `config/homeconfig.py`.

| File | Purpose |
|---|---|
| `config/home.json` | Defaults for every value, with a note beside each key |
| `config/local.json` | Your overrides (git-ignored); invalid values fall back to the default with a warning |
| `<repo>/.claude/quality-gates.json` | Per-repo test, coverage and lint commands; defaults come from the language profile |
| `<repo>/.claude/flow.json` | Per-repo queue limits |
| `<repo>/.claude/guard-paths.json` | Which paths count as tests |

Example override:

```json
{ "budget": { "budget_headroom_factor": 1.5 } }
```

Read one effective value with `python3 ~/.claude/config/homeconfig.py get <section> <key>`.

## Project structure

```
~/.claude/
├── CLAUDE.md          global rules loaded by every session
├── settings.json      hook registrations, sandbox, permissions
├── agents/            subagent definitions
├── skills/            flow skills (planning, building, routing, review, wrap-up)
├── hooks/
│   ├── hook-entry.py  single entry point for every hook
│   ├── guards/        allow or block tool calls
│   ├── gates/         quality gates and real-data checks
│   ├── budget/        time, cost and quota tracking
│   ├── worker/        worker lifecycle and wt commands
│   ├── lib/           shared modules
│   ├── tools/         helper scripts (hook entry listing, new-repo setup)
│   └── tests/         test suite
├── config/            settings reader and defaults
├── templates/         new-repository kit and settings templates
└── mods/wt-pane/      worker pane plugin
```

## Testing

```bash
uv run --no-project --with pytest --with pytest-cov --with pytest-xdist \
  python -m pytest ~/.claude/hooks/tests -n auto
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `wt ...` does nothing | The hook is not registered; check `UserPromptSubmit` in `settings.json` |
| A worker cannot reach a domain | Add the domain to the repository's `.claude/settings.json` sandbox allow list, then `wt resume` |
| A worker cannot find a tool | Set the tool's path in `config/local.json` under `toolchains.tools` |
| A run shows as orphan | `wt status <topic>`, then `wt resume <topic>` |
| A gate says `bwrap unavailable` | Run `bwrap --ro-bind / / --unshare-net true` to see why |
| A change to `quality-gates.json` has no effect | Gates read it from `main`; merge it first |
