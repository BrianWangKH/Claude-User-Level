---
name: route
description: Root of the skill tree - read the user's request, pick its situation (plan, develop, PR conflict, wrap-up, question, debugging, verification, research, ~/.claude change, direct change, plain chat) and load that situation's playbook, which in turn loads its operation files. Never does the work itself.
when_to_use: On the first user message of every main session, a slash command or a `wt` command included (load it, then run that command as typed; its "Interacting with the user" applies for the whole session), and again whenever the user switches to a different kind of task. Never in subagents or headless workers.
argument-hint: "[request]"
---

# Route

You are choosing **how** to work, not doing the work. Three levels:

1. **route** (this file): match the request to one situation below.
2. **playbook**: the situation's steps. Either a slash-command skill the user types, or a file under `playbooks/` you load with one Bash call: `cat ~/.claude/skills/route/playbooks/<file>` (the Read tool can't reach `~/.claude`).
3. **operation**: reusable steps a playbook names (`ops/<file>`, loaded the same way), or an operation skill (`new-worktree`, `pr-conflict`, `finish-worktree`).

Checkout: !`bash ~/.claude/skills/feature-flow/where.sh`

If no result appears above, run `bash ~/.claude/skills/feature-flow/where.sh` yourself.

## Situations

Go top to bottom; take the first row that matches. The project `CLAUDE.md` may add rows in a `## Situation additions` table (older projects: `## 情境補充`): check it before this one, project rows win.

| # | The user wants | Where | Go to |
|---|---|---|---|
| 1 | plan, discuss design or TODO, start a repo, pick the next topic, check worker progress | main checkout | tell the user to type `/repo-plan` |
| 2 | develop a planned topic | main checkout | the default is the headless worker: `plans/worker/<topic>.json` exists → tell the user to type `wt launch <topic>` (first run) or `wt resume <topic>` (later runs); missing → the hand-off isn't done, tell the user to type `/repo-plan` |
| 2b | develop a planned topic by hand, in a desktop session | worktree | tell the user to type `/feature-flow <topic>` |
| 3 | a PR shows conflicts / "does it still merge" | main checkout | tell the user to type `/pr-conflict <topic>` |
| 4 | a PR was merged, clean up | main checkout | tell the user to type `/finish-worktree <topic>` |
| 5 | change `~/.claude` (skills, hooks, settings, CLAUDE.md, mods, agents), or a flow problem whose fix lives there | any | `playbooks/home-config.md` |
| 6 | something used to work and now doesn't; "why did X happen" | any | `playbooks/debug.md` |
| 7 | run it / see it working / manual verification | any | `playbooks/verify.md` |
| 8 | an external fact: a library, API, data source, Claude Code behaviour | any | `playbooks/research.md` |
| 9 | understand the code or the repo, no change ("how does X work", "where is Y") | any | `playbooks/ask.md` |
| 10 | explicitly asks you to change something here and now, outside the worktree flow | any | `playbooks/direct.md` |
| 11 | small talk, a one-line answer | — | answer directly, no playbook |
| 12 | anything rows 1–11 don't fit | any | `playbooks/other.md` |

Rows 1–4 (except the `wt` commands, which a hook handles) are `disable-model-invocation` skills: you can't load them, and you never reproduce their steps another way (that is what keeps planning and orchestration apart). Tell the user which command to type and why, in one line. A row 1/2/3/4 request in a worktree, or row 2b in the main checkout: say where it has to run instead.

## Interacting with the user

For the main session only (the global `CLAUDE.md` holds only the rules every role needs; subagents and headless workers don't load this skill).

- Before running a command the user didn't ask for, say in one sentence what it will do
- When starting a task, the first reply states in your own words: the goal, the problem to solve (its root cause; say so plainly if that isn't clear yet), and the approach (the long-term best approach, and the smallest first step it can be split into that carries no risk even if work stops there); if there is a quick fix that only works around the symptom, list it too with how it differs, and let the user choose. This statement itself needn't wait for a reply (acting still follows the proposal rule below); when the evidence shows the change the user asked for won't solve the problem, say so and stop
- When the user asks a question or interrupts, answer first in that turn: read files only if the answer needs it, run no other command, change no file, and wait for the user to say continue
- Long commands (the full test suite, measurements, scans) always run in the background, so the user's questions can be answered at any time
- Long work (a draft-and-test loop, multi-file changes, heavy investigation; about 30 or more tool calls) goes to a background subagent by default: write the spec first (goal, files to change, acceptance tests, where the draft goes), dispatch a new agent (not a fork: a fork inherits the whole context and rereads it on every request, which is expensive), and the main session ends its turn; when it is done, look at the diff and the test results before handing it to the user
  - Why: while the main session does a lot in one turn, prompts sent by the user and by plugins (pane buttons) can't get in until that turn ends (a pane's `wt resume` can wait minutes)
- When proposing to the user, write it all at once without waiting to be asked: the root cause (say explicitly when it only works around the symptom), each number labelled "measured" or "estimated" (an estimate is not a promise), the cost, the risk of misjudging in both directions (the safe direction and the dangerous one), and the mainstream approach. Steps that carry no risk and can be thrown away are done directly and reported afterwards (drafts in `$TMPDIR`, background read-only measurements); anything that affects a running flow, spends real money or time, changes real files of the home or a repo, or is a choice where long-term best and risk-free can't both hold, needs consent first. When asked "should we do it" or shown a proposal, give your real judgement: if it isn't worth it, say it isn't; if it should be opposed, oppose it; agreeing is not the default
- Before stating the status of a run, a merge or the queue, read the records first, and name in the reply the files and times it rests on (`ops/evidence.md`)

## Rules

- Row 12 only when no earlier row fits: say which row came closest and why not. It is the default, not a shortcut: a request a row fits always takes that row's playbook.
- Before loading, say in one line: `Situation: <situation> → <playbook>`. If two rows fit equally, pick the earlier one and say so; ask only if the two would change files differently.
- One playbook at a time. When the user's request changes kind mid-session, come back to this table (it is already in context; don't reload it) and switch.
- After loading a playbook, copy its steps verbatim into a todo list (TaskCreate) before any task-specific item; a step you won't do stays in the list as `skip: <reason>`, so a skipped step is visible.
- A playbook's operation files are loaded only when the playbook reaches that step, not up front.
- After a compaction, playbook and operation files loaded with `cat` are gone (only invoked skills are re-attached, from the most recent, within a shared token budget, so even this file can drop out): reload the current playbook with the same `cat` before the next step, and invoke skill `route` again if this table is missing.
- A row whose playbook is missing (its file under `playbooks/` doesn't exist: the `cat` says `No such file or directory`; or its slash-command skill isn't installed; any other `cat` error is a failure to report, not a missing flow): first tell the user that this install doesn't include that flow, then go on down the table to the next row that matches. When no row is left (row 12's playbook is missing too), stop: tell the user that no installed flow fits this request, and change nothing.
