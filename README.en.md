# ~/.claude user guide

This directory is my personal Claude Code setup: a flow where a planning session plans, headless workers develop in worktrees, subagents split the work, and hooks enforce the rules. This document is written for myself: the architecture, how to use it, where things are, and what to do when stuck. The rules for Claude live in `CLAUDE.md` and the skills, not here.

## Architecture

### Roles

| Who | Where | What |
|---|---|---|
| Me | planning session, external terminal | discuss, decide, approve (queueing, merges that need a person), change `~/.claude`, push backups whenever I think of it |
| Planning session | the repo's main checkout | `/repo-plan`: discuss, write the plan and estimate, queue or open a worktree. Writes no code |
| Worker (orchestrator session) | `../worktrees/<topic>`, `claude -p` in the background | `/feature-flow <topic>`: dispatch subagents, verify, commit, write `PR_DESCRIPTION.md`. Never pushes |
| Subagent | under the worker | qa-planner, spec-reviewer, qa, developer, code-reviewer, each doing one part |
| Verifier | after the worker ends, started separately by a hook | real-data layer 2: judges the result against the approved plan (see "Real-data checks") |
| Hook | outside Claude Code (outside the sandbox) | enforce rules, record, start workers, merge, wrap up; agents can't change its records (`.hook-only/`) |

### One topic from planning to merge (local mode, the default)

```
planning session ─ /repo-plan ─ plans/queue/<topic>.md, .json (+ plans/verify/<topic>.json)
      │
I approve ─ the pane's queue all / queue, or wt queue <topic> …
      │
wt_queue ─ when its turn comes (after merged, within worker count / USD / quota limits) create the worktree ─ wt launch
      │
worker ─ /feature-flow: qa-planner → spec-reviewer → qa → developer → code-reviewer (hooks handle budget, quality gates, mutation)
      │                     budget too small to dispatch → automatic resume; STATUS: needs-decision / blocked → call me
STATUS: done
      │
verify.py (layer 1) → verifier.py (layer 2) ─ fails → automatic fix run
      │
auto-merge.py ─ all conditions pass → merge into local main; branch behind main → automatic merge run; needs a person → call me (wt merge)
      │
auto-finish.py ─ archive records, import cost, queue friction into plans/friction.md, remove worktree ─ queue opens the next one
```

How I get called: the pane's toasts and `notices` list, and events attached the next time I send a message in the main checkout (the ones I need to handle are marked `!`).

### Situation routing (skill tree)

Every main session loads `route` first (also when it starts with a `/` or `wt` command: it loads, then runs that command as typed). Its "interacting with the user" section applies for the whole session (answer first, complete proposals, consent by risk, long work goes to a background subagent, state the evidence for any status). Subagents and headless workers don't load it, so these rules are not in the global `CLAUDE.md`. A normal request is matched against the table, Claude says one line `情境：… → …`, then loads that situation's playbook; a playbook loads the operation files it uses only when it reaches that step. I can also type `/route <request>` myself.

```
route (root: picks the situation, does no work)
├─ develop a planned topic ────────── estimate file exists: tell me to type wt launch / wt resume; missing: /repo-plan hands off first
├─ plan / discuss / start a repo / progress ── tell me to type /repo-plan ── new-worktree ── worker /feature-flow
│                                                                   ├─ test-first (agents.md, test-first.md)
│                                                                   ├─ small-fix, presentation, docs-investigation
│                                                                   └─ merge.md
├─ PR conflict ─────────────────────── tell me to type /pr-conflict
├─ wrap-up after merge ─────────────── tell me to type /finish-worktree
├─ change ~/.claude ────────────────── playbooks/home-config.md ── ops/evidence, ops/cite, ops/home-draft
├─ debugging (used to work / why) ──── playbooks/debug.md ──────── ops/evidence
├─ run it / manual verification ────── playbooks/verify.md
├─ look up an external fact ────────── playbooks/research.md ───── ops/cite
├─ understand code (no change) ─────── playbooks/ask.md
├─ explicit request to change in place ── playbooks/direct.md
├─ small talk, one-line answer ─────── answer directly
└─ other (none of the above) ───────── playbooks/other.md ──────── log one line to the repo's plans/route-misses.md
```

- The skills of the first four situations set `disable-model-invocation`, so `route` can't invoke them (official docs: Claude calling them through the Skill tool is blocked). It only tells me which command to type, and never follows their steps another way, so planning and orchestration stay separate
- Skills can only live at `~/.claude/skills/<name>/SKILL.md` (the only structure the official docs describe), so the tree is logical: the second and third levels are files under `route/playbooks/` and `route/ops/`, loaded with `cat` (Read can't reach `~/.claude`)
- After a compaction only invoked skills are re-attached (most recently invoked first, within a shared token limit; with many skills invoked even `route` can drop out). Playbooks and operation files loaded with `cat` are always gone, so Claude has to `cat` the current playbook again
- A project can add a `## 情境補充` table to its own `CLAUDE.md` (e.g. data analysis); `route` checks that table first, and project rows win
- Adding a situation: one row in the table in `route/SKILL.md` plus a playbook; repeated steps become `ops/` files. Candidates come from "other": every time "other" is taken, one line goes into the repo's `plans/route-misses.md` (kind of request, why the closest situation didn't fit, the steps actually taken). When the same kind shows up a third time Claude mentions it once, and I decide whether to add it

### Output brevity and code quality

- **Output** (global `CLAUDE.md` 「輸出」, borrowing caveman's approach): replies to me use Lite (answer first, no pleasantries, full sentences and term explanations kept); subagent reports and the worker's orchestration text use Full (fragments are fine, because the orchestrator re-reads reports on every request, so shorter is cheaper); commits, PRs, the plan's decision table and documents are not compressed. Code, paths, commands, error messages and numbers are never altered; risk, irreversible actions and decisions for me go back to full sentences. Measure the saving by comparing `cost/<id>.jsonl` before and after
- **Code quality**: numeric limits (CCN, CRAP, unexecuted lines) are enforced by crap-gate, formatting by ruff / gofmt; the parts that need judgement (names, single responsibility, duplication, dead code, comments, not swallowing errors, pure-function core, interface types and units, changing only what is needed) are C1–C9 in skill `clean-code`. developer and code-reviewer both preload it: developer writes to it, the reviewer reviews against it and tags findings with the rule id; C6, which produces wrong numbers, counts as a correctness problem, and the orchestrator decides the rest by "must-fix vs TODO"

## How to use it

### Daily flow (local mode)

| Step | What I do | What happens |
|---|---|---|
| 1. Plan | open Claude in the main checkout, `/repo-plan` | discuss scope, decisions, class (logic / wiring / docs…); external facts the plan relies on are checked in official docs and cited on the spot, never guessed or left to the worker |
| 2. Hand off | agree to the plan | the planning session writes `plans/queue/<topic>.md` (the plan), `.json` (order, `after`, resource tags, estimate), and `plans/verify/<topic>.json` when there are real-data checks; the estimate uses `cost-history.py estimate branch …` (fixed overhead + proportional part, plus historical gate waits, no floor) |
| 3. Approve | press `queue all` in the pane's Planned block (or one topic's `queue`, each pressed twice), or type `wt queue a b c` | the queue starts what it can within its limits; the rest show what they wait for |
| 4. Wait | nothing; open `wt pane` to look, or ask 「進度」 | a finished worker runs both check layers, merges, wraps up, then opens the next one |
| 5. When called back | read the toast / `notices` / events attached to my message, answer Claude's questions | once the decision is in the worktree plan, `wt resume <topic>`; for a merge that needs a person press `merge` under "Waiting for you" in the pane (or `wt merge <topic>`); while the approval runs the tests, that row shows `approving run <n> (since HH:MM)` and no button, and the result comes back as an event |
| 6. Next planning | `/repo-plan` | the planning session reads `plans/friction.md` (friction and cost gaps queued at each topic's wrap-up) and settles the config changes with me |
| 7. Backup | `git push` in an external terminal whenever I think of it | origin is only a backup |

A single topic outside the queue: the planning session opens the worktree with new-worktree and writes `plans/worker/<topic>.json`, and I type `wt launch <topic>` (always `wt resume` after that). To stop a worker midway: `wt stop <topic>` (`wt resume` later); stopping never happens automatically.

### Starting and the pane

All three ways end up sending the same `wt launch|resume|stop <topic>`, handled by `worker-control.py`; the hook checks are the same for all three.

| Way | How |
|---|---|
| Manual | type `wt launch / resume / stop <topic>` myself |
| Pane buttons | `wt pane` (or the `wt pane` button in the band above the prompt) → Controls, one row per topic; one press turns it into a yellow `confirm …?`, and only a second press within `confirm_seconds` sends it. After sending, that topic's button shows `<action>… (sent …)` and takes no clicks until the topic has a new event, the row's state changes, or `pending_minutes` runs out; a hook refusal restores it at once and the reason goes into `notices` |
| Automatic | `wt auto on` (or press `auto: off` in the pane): every time the planning session writes `plans/worker/<topic>.json`, the mod sends `wt launch` or `wt resume` when that turn ends |

Limits of auto mode: it holds only for this session (a new session is back to manual); only my own Enter in the prompt (or Remote Control) can turn it on, anything sent by another plugin or a peer session is refused; it never sends `wt stop` by itself; it doesn't auto-resume while there is an agent-guard HOLD; one topic is scheduled at most once per 60 seconds; a scheduled prompt is sent only after the main loop finishes answering that turn, and a refusal shows the reason in a toast. `wt auto` shows the state, `wt auto off` turns it off. All three commands are intercepted by the mod and never reach the model.

What the pane shows: the selected worker's office (in the terminal; other surfaces get a list), each subagent's time and spend against budget (a hook wait is marked `waiting for … <waited>/<limit>`, and the clock stops when the wait begins), and the title bar shows elapsed / estimate and limit; below that Controls (launch / resume / stop, auto, local), Waiting for you (merges that need a person), Queue, Planned; at the bottom `notices` (the latest `notices_max` events, full text, newest first; the unread count also shows in the band). What can be pressed is decided by `hooks/wt_controls.py`, and the pane only draws its output; the values are in `mods/wt-pane/config.json`.

### Local mode

Press `local: off` in the pane twice, or type `wt local on` (`wt local off` to turn it off, `wt local` for the state; only my own Enter in the prompt is accepted): the mod writes the mode into the main checkout's `.hook-only/flow-mode` and sets `FLOW_INTEGRATION` for this process; afterwards every planning session reads that file when it starts. It refuses to switch, and says why, while a worker is running, while a topic is created but not yet launched, or (when turning on) while the local main is behind origin/main. Each topic records the mode at its first `wt launch` (the worktree's `.hook-only/flow-mode`), so switching doesn't affect topics in progress. The repo settings can also set `"env": {"FLOW_INTEGRATION": "local"}`. The local `main` is the truth and origin only a backup; no PRs are opened.

- **Comparison base**: `skills/feature-flow/base-ref.sh` and `qg_common.integrationRef` always return the local `main` (`master` if there is none); new-worktree, finish, merge runs, quality gates and reviewers all use it; no fetch
- **Automatic merge**: when a worker ends in `STATUS: done`, `run-worker.sh` calls `hooks/auto-merge.py`, which runs `git merge --no-ff` into the main checkout's `main` (message from PR_DESCRIPTION) only when all of the following hold, and writes the result to `runs/merge-<n>.json`:
  1. No ALERT / HOLD; the repo has `quality-gates.json` (with a test command)
  2. The main checkout is on `main`, and `main` is still where it was when this run started (`runs/main-<n>`): if only automatic merges or `wt merge` moved it (recorded in `auto-merge/merges.jsonl`), a merge run comes first; if anything else moved it, I'm asked to check `git reflog main`. The check plus the merge are locked in `.hook-only/auto-merge/lock` throughout
  3. The branch already contains `main` (otherwise a merge run comes first), and the working tree is clean
  4. No paths that need a person: `hooks/auto-merge-hold.json` (`.claude/`, CI, dependency manifests and lock files) plus the repo's `quality-gates.json` `auto_merge_hold`; no new URLs in text files other than docs and tests
  5. The last complete code-reviewer verdict that agent-guard recorded is APPROVE (`.hook-only/reviews.jsonl`); later commits on the branch itself can't change source code, and a hand-resolved merge needs a merge review APPROVE. A docs-only branch (every change is .md / .txt / .rst, none in test paths, no reviewer record) needs no APPROVE, and `merge-<n>.json` records `"review": "docs-only"`
  6. Every language's test command passes in the sandbox; then every language's `checks` from `quality-gates.json` run (formatting, lint, e.g. `ruff format --check`, `gofmt -l`), in the same sandbox with the same cwd / env; ones written as `{"argv": [...], "empty_output": true}` must also print nothing. Any failure refuses the merge and names the item with the last lines of its output; without `checks` only the tests run. So in local mode the orchestrator no longer runs the completion checks itself at wrap-up
  7. When `plans/verify/<topic>.json` exists, `verify.py` passes; when the spec has a `verifier` section, the `verifier.py` verdict passes too
- **Automatic merge run when the branch is behind main**: when the only refusal reason is "the branch doesn't contain the latest `main`", `worker-control.py auto-merge` resumes the same session in merge mode (`merge.md`), and ending in `STATUS: done` goes back to auto-merge. The budget is `worker_auto_merge_minutes` / `_usd` in `budget-defaults.json`, at most `worker_auto_merge_max` in a row per topic; a `wt launch` / `wt resume` I type resets it
- **Automatic fix run when real data shows the branch is wrong**: when a `verify.py` check fails or the verifier's verdict is fail, `worker-control.py auto-fix` resumes with `FIX_PROMPT`: the worker treats the findings as must-fix (qa reproduces them with a fixture first, developer fixes, another code-reviewer round), and both check layers run again afterwards. Budget = `cost-history.py estimate branch … --fix`, at most `worker_auto_fix_max` in a row per topic; a `wt merge` I press never triggers it
- Refusals handled automatically are recorded as `retry` events and don't call me; other refusal reasons, or `worker-control.py` failing to start a merge / fix run, are recorded as `refused` and call me. To undo a wrong merge: `git revert -m 1 <commit>`
- **Automatic wrap-up after merge**: `auto-finish.py` waits for this run's wrapper to end, then in order archives the worktree's records (`.hook-only/`, plan, test plan, PR_DESCRIPTION) into `.hook-only/archive/<topic>/`, imports the cost, appends the plan's `## Friction` and the cost-gap reasons to `plans/friction.md`, and removes the worktree (the branch is kept). Each step's result is in `.hook-only/archive/<topic>.finish.json`; when a step fails the later ones don't run

### Topic queue (local mode)

Plan several topics at once, and after I approve them there's no need to `wt launch` each one: they run in the order I gave, several at a time within the limits, and the next one opens after earlier ones merge (`hooks/wt_queue.py`).

- **The planning session writes**: `plans/queue/<topic>.md` (the plan text) + `plans/queue/<topic>.json` (`branch`, `order`, `after`, `resources`, `estimate`); real-data checks still go in `plans/verify/<topic>.json`
- **I approve**: the pane's `queue all` / `queue` (pressed twice), or type `wt queue a b c` (each spec is checked first; one broken spec rejects the whole batch). The `wt queue` reply is shown only to me and doesn't enter the conversation; `wt queue` without arguments checks once what can start, then shows the queue; `wt queue drop <topic>` removes one that hasn't started
- **When its turn comes**: run new-worktree.sh from the current `main`, copy the plan into the worktree, write `plans/worker/<topic>.json`, go through `wt launch`
- **When it checks**: when `wt queue` adds topics, and after each run writes `exit-<n>` (auto-merge is done by then)
- **Wait reasons** (in order; a waiting topic doesn't block later ones that can start): already started; subscription quota; the `after` topic isn't merged into the local `main` yet; the estimate alone exceeds `queue_max_concurrent_usd` (stuck); busy topics reach `queue_max_workers` or their estimates together exceed `queue_max_concurrent_usd`; shares a resource tag with a busy topic
- **Limits**: `queue_max_workers` and `queue_max_concurrent_usd` in `budget-defaults.json` are the defaults; a repo overrides them in `.claude/flow.json` `{"queue": {...}}` (read from the local `main`); the repo defines what resource tags mean
- **Subscription quota** (5-hour and weekly): `hooks/quota-snapshot.py` wraps the status line and writes `~/.claude/quota/usage.json` on every refresh; when either window reaches `queue_max_five_hour_pct` / `queue_max_seven_day_pct` and hasn't reset, nothing new starts; when the snapshot is too old, `queue_quota_unknown` decides (`wait` or `start`). Only in `budget-defaults.json`, a repo can't override it; running workers aren't stopped
- Local mode only (`after` is judged against the local `main`)
- A failed start stays in the queue marked error, without retrying (`drop` removes it); starts and failures are recorded in `.hook-only/queue/log.jsonl` and are also events
- Finished items: once the branch is merged and the worktree removed, the item is marked done, `plans/queue/<topic>.*` moves to `.hook-only/archive/<topic>/queue/`, and it shows as merged for `queue_done_linger_minutes` before it is removed

### Real-data checks

Tests use fixtures. What only real data can show (the command finishes, two runs give the same output, error paths refuse without writing files, values are in range) the planning session writes into the main checkout's `plans/verify/<topic>.json` (the worker can't write it; format in `templates/verify.json`).

- **Layer 1 `verify.py`**: after the worker ends in `STATUS: done`, copies `data_paths` into a temporary DATA_ROOT and runs each check from the worktree inside bwrap (no network, worktree read-only), looking only at exit codes, output text, the sha256 of two runs, and files that must not change. Result in `runs/verify-<n>.json`
- **Layer 2 verifier**: what a hook can't judge (whether numbers make sense, whether a rule holds on real data) goes into the spec's `verifier` section (`usd`, `minutes`, `judge` items). After layer 1 passes, `hooks/verifier.py`:
  - puts the inputs in `<worktree>/.hook-only/verifier-<n>/`: the approved plan, the spec, the layer 1 result, a copy of the real data
  - approved plan: every time I start a run, `worker-control.py` copies the plan to `<main>/.hook-only/approved/<topic>.md`, so a plan the worker changes later can't become its own acceptance criteria; `plan-guard.py` blocks the verifier from reading the worktree's `plans/`
  - runs `claude -p "/verify-topic <topic> <n>"` in the worktree: skill `verify-topic` (`context: fork`, `agent: verifier`) forks the verifier, blind to the orchestrator but not to the plan
  - the budget, converted through the history model, is managed by `subagent-budget.py`; the verifier can't change the worktree (compared before and after); the report's second line is `VERDICT: pass|fail`
  - result in `runs/verifier-<n>.json`: `passed`, `failed` (back to the worker for a fix), `error` (the verifier itself failed → waits for me)
- The only thing left for a person to judge is the UI: it goes into `plans/manual-verify.md`

### Scripted flow (trial, 2026-11-02)

About half of the orchestrator's cost goes to mechanical work: reading files, bookkeeping, writing dispatch prompts (measured: 52 worker sessions, judgement requests under 5%). The scripted flow hands "what to do next" to a hook and the orchestrator just follows it; judgement goes to the Opus `flow-judge`.

- Turning it on: the planning session adds `"flow": "scripted"` to `plans/worker/<topic>.json` (logic, wiring and small fix only; other classes keep the original flow); at `wt launch`, `worker-control.py` runs `flow-next.py init` and the start prompt becomes "read `scripted.md`, follow `.hook-only/flow/next.md`"
- Each step: when a subagent hands back its report (`SubagentHandback`), `flow-next.py report` computes the next step from the state machine and writes it to `next.md`; the orchestrator reads it on receiving the report and follows it; after the orchestrator dispatches or commits, the next step comes back as additionalContext, with no file to read
- The assignment of a dispatch is written to `.hook-only/flow/prompts/<n>.md`; the dispatch prompt is a single line "read this file" plus `Estimate:` (historical median, which dispatch-guard then runs through the model); before a commit the hook `git add`s the files that agent changed, so the orchestrator types a single `git commit`
- No stops at phase boundaries: after the tests commit the developer is dispatched directly, after the implementation commit the first code review is dispatched directly, with no `PHASE:` turn and therefore no compaction or resume (measured 2026-11-02: each compaction about $0.04-0.07 and 0.3 minutes, plus a resume turn; the orchestrator reads only `next.md` and the reports, so its context is small anyway). `run-worker.sh`'s PHASE mechanism stays for the original flow
- Protection is unchanged: dispatches and commits are still the orchestrator's tool calls, and dispatch-guard, agent-guard, crap-gate and the budget hooks work as usual; an unreadable report, a second CHECKPOINT, the round limit, or a judge without a `DECISION:` → `STATUS: needs-decision`
- Report format: each agent's report starts with fixed fields (checked by `report-check.py` at hand-back), and flow-next reads only those fields, never guessing from the body
- After a stop: every stop saves the state before it and the report that caused it (`stopped_from` in `state.json`). When the hook misread something (e.g. a verdict line's format), fix `flow-next.py` and just `wt resume`: worker-control first runs `flow-next.py retry` automatically, reprocessing that report with the new code; if the state can continue (`flow-next.py runnable`), the run carries on scripted
- Falling back to the original flow: after a stop that retry doesn't cover (a decision waiting for me), `wt resume`, fix runs and merge mode all run `flow-next.py off` first, and the topic follows the original feature-flow from then on; the wrap-up (TODO, PR_DESCRIPTION, conflict check) already follows the original text
- Records: `.hook-only/flow/state.json` (state), `log.jsonl` (every step), `reports.jsonl` (every report verbatim); `## QA commits`, `## Reviewer` and the agents' `Friction` are written into the plan by the hook

### PR mode (when local mode is off)

The comparison base is `origin/<default branch>` (the hook fetches first); no automatic merge, no queue. After the worker ends I run the push command Claude gives in an external terminal and open the PR (paste the worktree's `PR_DESCRIPTION.md`; Claude first warns if the branch touches `.claude/` or `.mcp.json`). PR conflicts: `/pr-conflict <topic>`; when there are conflicts Claude estimates with `cost-history.py estimate branch … --merge` and writes an estimate file with `"mode": "merge"`, I type `wt resume <topic>`, and the worker merges following `merge.md`. After the PR merges, `/finish-worktree <topic>`: harvest cost and friction, remove the worktree (the branch is kept).

## Command reference

| Command | Where | Purpose |
|---|---|---|
| `/route [request]` | anywhere | pick the situation and load its playbook (Claude loads it by itself when the first message isn't a command) |
| `/repo-plan` | main checkout | plan, start a new repo (kickoff), pick the next topic, ask for progress |
| `wt queue [<topic> ...]` | main checkout | queue the `plans/queue/` topics in order and start what can start; without arguments shows the queue; `wt queue drop <topic>` removes one that hasn't started (local mode) |
| `wt launch <topic>` | main checkout | first start of a worker (outside the queue) |
| `wt resume <topic>` | main checkout | continue the last session (decision already in the plan); merges instead when the estimate file has `"mode": "merge"` |
| `wt stop <topic>` | main checkout | stop the worker (SIGINT, SIGTERM after 10 seconds) |
| `wt merge <topic>` | main checkout | approve a topic auto-merge refused because it needs a person: the hook runs the tests and other conditions in the background, then merges, records it in `merges.jsonl` (other workers treat it as a normal move), and wraps up automatically; the result is an event. One approval per run at a time: `runs/approve-<n>.json` (auto-merge's pid) is written before it starts, and a `wt merge` while it runs replies "already being approved"; if that process is gone (killed) the record counts as stale and it can start again; auto-merge deletes the record when it ends. auto-merge first checks "already merged" (`merge-<n>.json` says merged, or the branch tip is already in main and `merges.jsonl` has this branch): with a decision file it changes nothing and emits no event; without one (e.g. a merge run with nothing to merge) it records merged (`"review": "already-merged"`) and wraps up, without asking for another merge run |
| `wt pane` | interactive session | toggle the worker pane (the band has a button too); never reaches the model |
| `wt auto [on\|off]` | interactive session in the main checkout | auto mode on/off (this session only); never reaches the model |
| `wt local [on\|off]` | interactive session in the main checkout | local mode on/off; never reaches the model |
| `/pr-conflict <topic>` | main checkout | when a PR conflicts with main (PR mode) |
| `/finish-worktree <topic>` | main checkout | wrap-up after the PR merges (PR mode); also when the automatic wrap-up fails |
| `/feature-flow <topic>` | worktree | used by the worker itself; type it only to develop by hand in a desktop session |
| `/blind-review` | worktree | a truly blind review (a new reviewer each round), only when needed |

`/repo-plan`, `/feature-flow`, `/finish-worktree`, `/pr-conflict` and `/blind-review` all set `disable-model-invocation`: only I can type them (or a worker's start command carries them), so planning and orchestration never load each other's skills. Every `wt ...` is handled by a hook or the mod; the reply is shown only to me and costs no model turn.

## Things only the external terminal can do

The sandbox blocks these; Claude gives the commands and I run them:

- `git push`, `git remote add`, setting upstream, deleting branches (`.git/config` is read-only in the sandbox)
- installing tools (Go, uv…)
- changing `~/.claude` (skills, hooks, settings, CLAUDE.md) and committing
- anything that needs the `claude` CLI (the sandbox can't see `~/.local/share/claude`); `wt` hooks run outside the sandbox and aren't affected

## How to change `~/.claude`

1. The planning session finds a problem → records it in the repo's `plans/config-update/home-settings-plan.md` (the to-do summary is at the top)
2. Claude reads the current files and builds **complete new files** under `$TMPDIR/home-apply-<topic>/` (`/tmp/claude-1000/home-apply-<topic>/`), with a diff
3. Claude lists the commands one by one in its reply (not wrapped in a script); I `cp` them over, `git -C ~/.claude add <files>`, and commit (Claude gives the commit message)
4. `settings.json` Claude can't read: it only gives the block to paste and where
5. `~/.claude` is a git repo: restore with `git -C ~/.claude checkout <file>`, no `.bak` needed

## Where things are

| Location | Contents |
|---|---|
| `<repo>/plans/repo-plan.md` | planning record: under discussion, waiting for a branch, rejected, future |
| `<repo>/plans/queue/<topic>.md`, `.json` | plan and spec for the queue (order, `after`, resource tags, estimate); moved into the archive when done |
| `<repo>/plans/verify/<topic>.json` | real-data check spec (layer 1 checks, layer 2 `verifier` items) |
| `<repo>/plans/worker/<topic>.json` | the estimate `wt` reads: minutes and `est_usd` (limit = both × `budget_headroom`), session id; `"mode": "merge"` = merge run |
| `<repo>/plans/friction.md` | friction and cost gaps queued at each topic's wrap-up, handled by the next planning session |
| `<repo>/plans/config-update/` | plans to apply to `~/.claude` (drafts in `$TMPDIR/home-apply-<topic>/`) |
| `<repo>/.hook-only/events.jsonl` | event log (run ended, merged, refused, retry, verified, queue, wrap-up), written only by hooks |
| `<repo>/.hook-only/workers/<topic>.json` | PID, session id and run number recorded by `wt` |
| `<repo>/.hook-only/queue/` | queue state and `log.jsonl` |
| `<repo>/.hook-only/auto-merge/merges.jsonl` | records of automatic merges and `wt merge` (other workers use it to tell who moved main) |
| `<repo>/.hook-only/approved/<topic>.md` | the approved plan as of when I started the run (the verifier checks against it) |
| `<repo>/.hook-only/archive/<topic>/` | worktree records archived at wrap-up; `<topic>.finish.json` is the result of each wrap-up step |
| `<worktree>/plans/<topic>.md` | the topic's plan, `## Cost` (the orchestrator's own rows and overrun reasons), `## Friction` |
| `<worktree>/plans/tests/<topic>.md` | test plan |
| `<worktree>/.hook-only/runs/` | per run: `start-<n>`, `est-<n>.json`, `exit-<n>`, `result-<n>.json`, `stopped-<n>`, `merge-<n>.json`, `auto-<n>.json`, `verify-<n>.json`, `verifier-<n>.json`; `skills/repo-plan/run-result.py` reads them and prints the next step |
| `<worktree>/.hook-only/budget/` | subagent budget state (including a pending `hook_wait`) and `events.jsonl` |
| `<worktree>/.hook-only/cost/` | each subagent's actual usage per round (`usd`, tokens, start and end time) |
| `<worktree>/.hook-only/quality/` | crap-gate's result cache, pass records, developer block counts; `mutation-<reviewer>-<fingerprint>.json` summarises a mutation scan's result (`all_caught`, `delivered`, `no_result`, `sandbox_unavailable`) |
| `<worktree>/.hook-only/hook-read-only/` | records agents can't read: full mutation results (`mutation/`), worker-progress's progress for the pane (`progress/`) |
| `<worktree>/.hook-only/reviews.jsonl` | code-reviewer verdicts recorded by agent-guard; local-mode auto-merge trusts only this |
| `<worktree>/.hook-only/flow/` | scripted flow: `scripted` (switch), `state.json`, `next.md` (the orchestrator's next step), `prompts/<n>.md` (dispatch content), `log.jsonl`, `reports.jsonl` (reports verbatim) |
| `<worktree>/.claude-cost/history.tsv` | cost history copied in by new-worktree, used by `cost-history.py estimate`: estimate model = fixed overhead + slope × raw estimate (fitted per agent, branch class and merge run, no floor; the reasons for the choice and how to re-run it are in `cost-history.py`'s notes and `estimate-replay.py`) |
| `<worktree>/PR_DESCRIPTION.md` | merge message / PR body (not committed) |
| `~/.claude/mods/wt-pane/` | the worker pane mod (loaded by `env.CLAUDE_CODE_PLUGIN_DIRS` in `settings.json`; the sandbox allowRead needs `~/.claude/mods`); values in `config.json` |
| `~/.claude/quota/usage.json` | subscription quota snapshot written by the status line, read by the queue |
| `~/.claude/docs/` | design notes for each mechanism, index in `docs/README.md` |

`plans/` and `.hook-only/` are both in `.gitignore`; before a worktree is removed, the wrap-up archives its records and copies the cost into the history.

## Protections (why things get blocked)

| Mechanism | Blocks | To allow |
|---|---|---|
| Sandbox network | domains not on the allowlist | project domains go in the **repo**'s `.claude/settings.json` `sandbox.network.allowedDomains`, added by me after review (the planning session lists the domains and why); home only has GitHub |
| `permissions.deny` `Edit(...)` | Claude's Write / Edit to `.claude/settings.json`, `settings.local.json`, `.mcp.json`, `.claude/agents/**` (hooks are registered in settings and run outside the sandbox, so only I can change settings) | I change them outside |
| `autoMode.hard_deny` | a command opening domains with its own `allowed_domains` (the substitute while strict is off) | never allowed; always change settings |
| Sandbox filesystem | reading and writing under home outside the project; `blockReadsOutsideWorkingDirectories` makes the repo's `filesystem` settings **ineffective** | add tool or cache paths to the **home** settings `allowRead` / `allowWrite` (already open: `~/go`, `~/.cache/go-build`, `~/.local/bin`, `~/.cache/uv`) |
| `plans/` read limit | Bash can't read `plans/` | Claude handles plans with the Read / Write tools |
| `.hook-only/` | only hooks can write; agents can't change records | — |
| agent-guard | qa changing files other than tests, developer changing tests, any subagent changing anything under `.claude/` (guard-paths is read when the subagent starts, so changing it doesn't affect the current round's check) | test paths are listed in the repo's `.claude/guard-paths.json` (`<component>/tests/` prefixes); `.claude/` is changed by the planning session or me. After an ALERT, that session's dispatches and `git commit` / `git merge` stay blocked (`.hook-only/agent-guard/HOLD`) until I reply; for a worker, until I type `wt resume` |
| plan-guard | code-reviewer reading the plan, qa writing outside `plans/tests/`, developer writing `plans/` | — |
| crap-gate | changed functions with CCN > 10 (ones already over may not grow), CRAP > 30, changed lines no test executes, exclusion markers without a reason or more than 3 new ones (presentation checks CCN only). Blocks the developer at most 3 times when it hands back a `DONE` report (`SubagentHandback`) or stops (`CHECKPOINT` / `BLOCKED` aren't checked: stopped by budget or waiting for the orchestrator's decision, blocking would only burn budget until ALERT), then writes an agent-guard ALERT; checks again at `git commit`, by what is staged: with source code (the implementation commit) it also requires all tests to pass; with tests only, while the branch has committed neither source nor tests (test-first's first test commit), it requires the tests to fail, and not for a `red_invalid_patterns` reason (e.g. SyntaxError; a missing module / name is the expected red); later test-only commits (filling gaps, NEEDS-TEST, test fixes before the implementation commit) are checked for neither, and uncommitted implementation in the working tree doesn't block them. Red tests when the developer hands back don't count as its block (it may be reporting a wrong test) | limits in the repo's `.claude/quality-gates.json` `limits` (defaults in `hooks/quality-defaults.json`); ALERT is released as for agent-guard. `quality-gates.json` and `guard-paths.json` are always read from the diff base (the merged version), never the working tree: their commands run outside Claude Code's sandbox through hooks, so a change on a branch takes effect only after it merges. A repo can add `"protect"` to `quality-gates.json` (e.g. `["data"]`) to make those paths read-only in the sandbox, on top of the defaults in `hooks/qg_sandbox.json` (`.git`, `.hook-only`, `.claude`, `plans`), which it can't remove |
| Subagent budget | a dispatch prompt without an `Estimate: <min> min, $<usd>` (raw estimate of new work), `Remaining: …` (work left after a CHECKPOINT) or `Budget: …` line (dispatch-guard; the first two are run through the history model into a `Budget:` line by the hook, so the orchestrator doesn't run `cost-history.py estimate agent` itself); a subagent reaching budget × `budget_headroom` (the budget is the expected need, not a cap; stop and report CHECKPOINT) | the orchestrator can resume once by itself with a `Remaining:` line for the rest; a second CHECKPOINT on the same task goes to the user; `SubagentHandback` (handing back a report) is always allowed |
| Worker time limit | a worker past the minutes in `plans/worker/<topic>.json` | reports `STATUS: blocked`; re-estimate and resume |
| Worker time too short to dispatch | when dispatching or resuming an agent, the `Budget:` minutes + gate waits (code-reviewer's mutation scan, except merge reviews; developer's crap-gate) + wrap-up margin (`worker_dispatch_margin_minutes` in `budget-defaults.json`) exceed the minutes left (dispatch-guard) | reports `STATUS: blocked` with the next dispatch and its estimate written in the plan. dispatch-guard also records the minutes and USD a resume needs (dispatch budget + gate waits + wrap-up margin + resume start-up) in `.hook-only/runs/auto-resume-<n>.json`, and `run-worker.sh` hands it to `worker-control.py auto` at the end: if not stopped by `wt stop`, no ALERT / HOLD, the need within `worker_auto_resume_max_minutes` / `_usd`, and fewer than `worker_auto_resume_max` automatic resumes in a row (a `wt launch` / `wt resume` I type resets it), it resumes once automatically (budget = the larger of the dispatch record and "my last start estimate minus what was used", capped), with the decision in `auto-<n>.json` (run-result reports it); the record is deleted once a later dispatch is allowed. wt-pane's auto mode sends `wt resume` itself, so the count keeps resetting. The record is written by a hook and agents can't write it, so a worker can't request an automatic resume itself; otherwise re-estimate and resume as before |
| Worker end format | the worker's last message doesn't start with `STATUS:` / `PHASE:`, or writes `STATUS:` / `PHASE:` while background agents are still running (worker-stop) | blocked once to make it add it; allowed the second time, and the planning session treats it as blocked |
| `--max-budget-usd` | a worker spending past the USD cap | same as above |

## When stuck

| Symptom | What to do |
|---|---|
| A worker can't reach a domain | review it and add it to the repo's `.claude/settings.json` myself (the worktree needs it too; sync unmerged changes by hand), then resume |
| A worker can't find a tool (`uv: command not found`) | the tool is installed under home: add it to the home `allowRead`, open a new session |
| agent-guard ALERT: qa changed a non-test file | tests in the wrong place or missing from guard-paths; fix guard-paths or move the tests, reply, then `wt resume` |
| agent-guard ALERT: `no start snapshot` | check whether that round's SubagentStart fired (`.hook-only/budget/events.jsonl`) |
| A subagent blocked with "no budget state" | check the order of events in `<worktree>/.hook-only/budget/events.jsonl` |
| `exit-<n>` is 90 | `claude` isn't logged in: `claude auth login` in an external terminal |
| `run-result.py` says `STOPPED` | stopped by `wt stop`, normal; continue with `wt resume` |
| Typing `wt ...` does nothing | the hook isn't installed or isn't in settings: check `~/.claude/hooks/worker-control.py` and `hooks.UserPromptSubmit` |
| `wt auto on` but nothing starts automatically | the `wt-pane auto:` note in Claude's reply gives the reason (HOLD, no worktree, already running); without that note the mod isn't loaded or wasn't opened in this session: `wt auto` shows the state |
| A pane button press didn't send | the first press only turns it into a yellow `confirm …?`; press again within `confirm_seconds`; a button showing `<action>… (sent …)` was sent and is waiting for the result; the reason for a hook refusal is in `notices` |
| `wt launch` says there's already a session | this topic has run before; use `wt resume` |
| Automatic wrap-up failed (event `finish-failed`) | check which step failed in `.hook-only/archive/<topic>.finish.json`; wrap up a merged topic by hand with `/finish-worktree <topic>` |
| A docs topic refused by auto-merge: `Failed to spawn: pytest` | the worktree has no `.venv` (docs workers don't run tests): run `uv sync --offline` in the worktree, then `wt merge <topic>` |
| `wt resume` on a PR conflict says `no session id` | this topic wasn't done by a worker: open a session in the worktree and send `/feature-flow <topic> merge` |
| A merge commit blocked by `merge-commit-guard` | files still unresolved, or conflict-marker lines neither side has; delete the listed lines, `git add` again, then commit |
| An agent says there's no Grep / Glob | the native Linux / macOS build doesn't have them; use Bash `grep` / `find` (embedded ugrep / bfs) |
| A result file's `duration_ms` is tiny | it doesn't count background subagents; look at `start-<n>` / `exit-<n>` |
| crap-gate says `coverage for python produced no report` | the coverage command produced no file: the project doesn't have pytest-cov installed, or the command in `quality-gates.json` is wrong; tests run in a bwrap with no network and an almost empty `$HOME` (`hooks/qg_sandbox.json`), so needing the network, reading elsewhere in `$HOME`, or `uv run` without `--no-sync` all fail |
| crap-gate / mutation says `environment problem … bwrap unavailable` | bwrap can't create namespaces on this machine: run `bwrap --ro-bind / / --unshare-net true` in an external terminal to see the error; the hook never falls back to running outside the sandbox |
| A tool that calls git fails in the sandbox (e.g. setuptools-scm) and crap-gate only says `produced no report` | the worktree's `.git` points to the main checkout's `.git`, which is under the masked home; a known limitation for now |
| Changing `quality-gates.json` / `guard-paths.json` has no effect | hooks read the version on the comparison base (the local `main` in local mode, origin's default branch in PR mode): it takes effect only after it merges (on purpose: a branch can't change the commands a hook runs) |
| A reviewer is slow to hand back its report | mutation waits for the background scan at `SubagentHandback` (the pane shows `waiting for mutation scan`, and the wait doesn't count against the reviewer's budget). Languages without `select` run the whole suite per mutant; Python with `select`: the baseline measures only the directories of changed files and records each test's context, each mutant runs only the tests that executed that line (`fallback` when none is found), every survivor is re-run on the whole suite, and a kill counts only when the same tests pass without the mutation — more conservative than mainstream practice, but not guaranteed identical to running the whole suite per mutant; a later re-scan after test-only changes reuses unaffected results. Limits are `mutation_max` and `mutation_wait_seconds`; mutants the reviewer picks itself (`mutate-request`) work the same way: the pane shows `waiting for reviewer's mutants`, the wait doesn't count against the budget, with `select` only the tests covering that line run, and survivors are confirmed on the whole suite |

## Hooks at a glance

| File | Event | What it does |
|---|---|---|
| `wt_events.py` | UserPromptSubmit (main checkout); other hooks use `emit` | event log `.hook-only/events.jsonl` (run ended and STATUS, merged, refused, queue start / failure, wrap-up done / failed; written only by hooks). When I send a message in the main checkout, new events since the last one are attached after it (no extra turn; the ones I need to handle are marked `!`); the pane mod reads new events every `events_poll_seconds` and shows a toast (also with the pane closed, never in workers; shown between `toast_min_seconds` and `toast_max_seconds` by length, longest for ones I must handle), and keeps them in the pane's bottom `notices` list (the latest `notices_max`, full text, newest first; the unread count also shows in the band) |
| `wt_queue.py` | worker-control (`wt queue`), `run-worker.sh` (`tick` after each run's `exit-<n>`) | topic queue: starts in order within `queue_max_workers` / `queue_max_concurrent_usd` / resource tags / `after` / subscription quota; builds the worktree from the current `main`, then goes through `wt launch`; flock on `.hook-only/queue/lock` |
| `quota-snapshot.py` | status line (settings `statusLine` command wraps the original one) | writes the status line JSON's 5-hour / weekly usage to `~/.claude/quota/usage.json` for the queue; hands the output to the original status line command |
| `wt_controls.py` | (called by wt-pane and worker-control) | the single place that decides what each topic can do (launch / resume / stop, the reason when it can't, HOLD, whether auto may send) and whether local mode can switch; outputs JSON `schema` 1 (the version goes up when fields are removed or changed). A run counts as running from its run files (a `start-<n>` with no or an empty `exit-<n>`), never from a pid: the pane's process may be in the sandbox and can't see the worker's pid; worker-control, outside the sandbox, confirms with the pid before actually starting or stopping |
| `worker-control.py` + `run-worker.sh` | UserPromptSubmit | `wt launch / resume / stop / merge / queue` (`wt resume` is refused when the last run ended in `STATUS: done` with no new estimate, rule in `wt_controls.py`; stop writes `runs/stopped-<n>` first; resume releases that worktree's agent-guard HOLD; with `"mode": "merge"` in the estimate file resume sends the merge prompt, the mode is kept in the state for later resumes, and launch refuses); `run-worker.sh` runs `/compact` at `PHASE:` cut points and at the end of a run (`phase-<n>-<k>.json`, `compact-<n>-<k>.json` in `.hook-only/runs/`), giving each call only this run's remaining USD |
| `worker-budget.py` | PreToolUse | the worker's time limit (counted from `.hook-only/runs/start-<n>`) |
| `worker-progress.py` | PreToolUse (async) | only in workers: on every tool call writes `hook-read-only/progress/<key>.json` (what it's doing, this agent's spend this round, from `prices.json`) for the wt-pane pane; no output, never blocks |
| `dispatch-guard.py` | PreToolUse (Agent, SendMessage) | runs the dispatch prompt's `Estimate:` / `Remaining:` line through the history model into a `Budget:` line (an existing `Budget:` is used as is; blocks when the history file is missing) and stages it for subagent-budget; blocks when the worker's remaining time (including gate waits) / USD is too short; swaps the code-reviewer's prompt for the fixed blind-review prompt (except ones starting with `Merge review:`) |
| `worker-stop.py` | Stop | the worker's last message must start with `STATUS:` / `PHASE:`; neither line is allowed while background work is still running |
| `subagent-budget.py` | SubagentStart / Stop, PreToolUse | picks up the budget dispatch-guard staged (`.hook-only/budget/pending-*.json`); blocks a subagent at budget × `budget_headroom` (except `SubagentHandback`); event log |
| `subagent-cost.py` | SubagentStart / Stop | records each subagent's actual usage (`usd`, tokens, the old `weighted_tokens`); Start records the starting totals, `this_round` = now − start |
| `agent-guard.py` | before and after subagents, PreToolUse, UserPromptSubmit; PostToolUse `SubagentHandback` in code-reviewer's frontmatter | snapshots and compares what a subagent changed (moves of other branches only warn, because branches live in the `.git` shared by all worktrees and an agent can move another topic's branch with `git branch -f`; moves while a worker run is running in that worktree this round, or new commits recorded in `auto-merge/merges.jsonl`, don't warn, so parallel runs no longer flood it); after an ALERT blocks dispatches and commits until I reply; records code-reviewer verdicts in `reviews.jsonl` |
| `plan-guard.py` | PreToolUse | which plans each role can read and write |
| `crap-gate.py` | developer's PreToolUse `SubagentHandback` + Stop (frontmatter), PreToolUse (`git commit`) | changed code: CCN, CRAP, unexecuted lines, exclusion markers; blocks the developer at most 3 times, then hands over to agent-guard's HOLD; blocks commits that fail |
| `mutation.py` | code-reviewer's frontmatter: PreToolUse any tool (`start`, async), PreToolUse `mutate-request`, PreToolUse `SubagentHandback`, Stop | on the reviewer's first tool call, scans the changed lines in the background (`select` in `quality-gates.json`: only the tests that executed that line run, and a re-scan after test-only changes reuses results; contexts are read by `qg_cov_contexts.py` in the project's own Python); runs the 3–5 the reviewer picks (`mutate-request`: the same method as the full scan, with `select` only the tests that executed that line and survivors re-run on the whole suite; the wait is marked waiting (limit `manual_wait_seconds`) and taken off the budget); before the report is handed back, waits for the full scan to finish and gives the survivors to the reviewer to judge (the wait is taken off the budget); also blocks once when no result arrives, so the report says "didn't finish"; every outcome leaves a summary in `.hook-only/quality/mutation-*.json`. The tests crap-gate and mutation run all run in bwrap (`qg_common.runCommand`, `hooks/qg_sandbox.json`: root read-only, `$HOME`, `/mnt` etc. masked with only tool and cache paths put back, no network, environment cleared, `.git` / `.hook-only` / `.claude` read-only), because the hook itself isn't in the sandbox |
| `qg_common.py`, `qg_complexity.py` + `qg_goccn.go`, `qg_mutate.py`, `qg_cov_contexts.py`, `qg_langs.json`, `quality-defaults.json` | — | shared: diff ranges and coverage parsing, complexity (Python `ast`, Go `go/ast`, a generic engine for other languages), mutant generation, reading per-test coverage contexts (run in the project's own Python), per-language syntax, default limits; `markWait` / `excludeWait` record and subtract gate waits; tests in `hooks/tests/` |
| `block-write-outside-project.py` | PreToolUse | blocks Write / Edit outside the project |
| `bash-guard.py` | PreToolUse (Bash) | blocks commands starting with `cd <path> &&` / `cd <path>;` (global CLAUDE.md 「工具」: a compound `cd` triggers a permission prompt and interrupts auto mode and workers), suggesting absolute paths, `git -C`, `env -C`; a plain `cd` isn't blocked |
| `report-check.py` | PreToolUse `SubagentHandback` in the flow agents' frontmatter (qa-planner, spec-reviewer, qa, developer, code-reviewer, flow-judge) | the report must start with fixed fields (`report-schema.json`: `STATUS:`, `VERDICT:`, `QUESTIONS:`, `FIX-QA:` / `FIX-DEVELOPER:`, `NEEDS-TEST:`, `DECISION:`, parsed by `report_schema.py`); a non-conforming hand-back is blocked with a template, and after `max_denials` blocks for the same agent it is allowed (if flow-next can't read it, it stops and waits for me) |
| `flow-next.py` | PostToolUse `SubagentHandback` in the flow agents' frontmatter (including `flow-judge`) (`report`); PostToolUse Agent / SendMessage, Bash `git commit` (settings.json, main session) | the scripted flow's state machine (does nothing without `.hook-only/flow/scripted`): report → next step written to `.hook-only/flow/next.md`, the assignment to `prompts/<n>.md`; records the agent id waited on after a dispatch; `git add`s the agent's changed files before a commit and records QA commits after one; `init` / `off` are called by worker-control |
| `checkpoint-status.py` | UserPromptSubmit | when a background subagent's report has a `CHECKPOINT` status line, attaches the current `git status --short` (agents.md: record the files that actually exist, not what the report says); the input has no source field, so it also attaches to what I type myself, which is harmless |
| `session-mark.py` | SessionStart / End | records session start and end (for progress queries) |
| `fetch-origin.py` | before skills run | fetch before new-worktree, finish-worktree, pr-conflict |
| `finish-worktree.py`, `merge-origin.py` | PostToolUse | remove the worktree and merge the integration branch (origin's default branch, or the local `main` in local mode) outside the sandbox |
| `verify.py` | not a hook event: called by `run-worker.sh` after `STATUS: done` when `plans/verify/<topic>.json` exists (both modes) | runs the planned checks on a copy of the real data in the sandbox, result in `runs/verify-<n>.json` (see "Real-data checks") |
| `verifier.py` | not a hook event: called by `run-worker.sh` after `verify.py`; runs only when the spec has a `verifier` section and layer 1 passed | prepares inputs and budget, starts `claude -p "/verify-topic …"`, checks the worktree wasn't changed, reads the verdict into `runs/verifier-<n>.json` (see "Real-data checks") |
| `auto-merge.py` | not a hook event: called by `run-worker.sh` after `STATUS: done` in local mode | merges the branch into the local `main` only when every condition holds (see "Local mode"); starts `auto-finish.py` after a merge |
| `auto-finish.py` | not a hook event: started as a separate process by `auto-merge.py` after a successful merge | after the wrapper ends, archives records, imports cost, queues friction into `plans/friction.md`, removes the worktree (see "Local mode") |
| `merge-commit-guard.py` | PreToolUse (Bash) | during a merge (with `MERGE_HEAD`), blocks `git commit` / `git merge --continue` while files are unresolved or conflict-marker lines exist that neither parent has (`<<<<<<<`, `>>>>>>>`, `\|\|\|\|\|\|\|`; `=======` isn't checked, Markdown heading underlines would false-positive); a compound command (`git add … && git commit`) is checked before the whole chain runs, so it is still blocked, with a hint to run add and commit separately |
| `hookonly.py` | — | shared: where `.hook-only/` is |
| `prices.py` + `prices.json` | — | shared: per-model prices ($ / million tokens, cache writes split by 5m / 1h TTL); budgets, cost records and the worker dispatch check all use USD |
| `tokenweights.py` | — | shared: the old token price weights (for `tokens` budgets and old records) |

## New repo

1. Create the repo on GitHub; in an external terminal `git init`, `git remote add origin <url>`, `git fetch origin`, and create a local branch tracking `origin/<main>`
2. Create a `worktrees/` directory next to the repo (the sandbox can't create it)
3. Open Claude, `/repo-plan`, go through the kickoff checklist; the first branch is always `feature/docs/repo-init`
4. The planning session lists the domains the project needs; I review them and add them to the repo's `.claude/settings.json` myself; new tool paths go in the home settings
5. To enable crap-gate and mutation: copy `~/.claude/templates/quality-gates.json` into the repo's `.claude/`, fill in each language's coverage and test commands (Python can add `select` so mutation runs only related tests; local mode can add `checks` listing format and lint commands, which auto-merge runs after the tests, replacing the completion checks the orchestrator ran by hand at wrap-up), install the coverage tool in the project (Python: pytest-cov, `[tool.coverage.run] patch = ["subprocess"]`), then merge it into the comparison base (the local `main` in local mode, or origin's default branch): hooks read the version there, so the branch that adds the config isn't checked itself; when the comparison base doesn't exist (PR mode, a new repo not pushed yet) the gates are off entirely. Paths that must not break, such as real data, go in `"protect"`. The commands must run offline (`uv run --no-sync`), because the hook runs them in a bwrap with no network
6. To use local mode: `wt local on`
