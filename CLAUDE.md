# Global rules

Each project's `CLAUDE.md` adds what is specific to the project (environment, scope table, code conventions, concrete examples per class); both apply where they don't conflict; where the project explicitly states an exception, the project wins.

Every session and every subagent loads this file, so it holds only the rules every role needs. Each role's flow lives where that role loads it (see the index at the end).

## Communication

- Reply to the user in the user's preferred language; when none is stated, in the language the user writes in. Commits, PRs and code comments in English
- The rules for how the main session works with the user (answer first, proposals, consent by risk, long work to a subagent, the evidence behind a status) are in skill `route`, section "Interacting with the user": only the main session loads it; subagents and headless workers don't read it

## Output

How terse to be depends on the reader:
- **Lite** (replies to the user): answer first; no greetings, no pleasantries, no restating the question, no announcing "next I will…"; keep full sentences, and give a plain-language explanation the first time a term appears
  - No repetition within one reply: before a tool call write only one sentence on what you are about to do, without stating the conclusion first; after the tool write only the new results, never restating what was said before the tool
  - Exceptions (written as usual, not counted as announcing or restating): the one-sentence note before running a command the user didn't ask for (skill `route`, "Interacting with the user"), the approach and progress notes a background session is required to give, and the first sentence of a background session's reply naming what the user asked
- **Full** (subagent reports, a headless worker's orchestration text, messages written to another agent): Lite plus: fragments are fine, one fact per sentence, `→` for cause and effect; in English, drop articles and pleasantries
- **Uncompressed**: commit messages, PR descriptions, a plan's decision table, DESIGN / TODO / README, and any other record people read over the long term
- Never altered at any level: code, paths, commands and arguments, error messages, test names, numbers and units, and fixed-format lines such as `STATUS:` / `PHASE:` / `DONE`
- Switch back to full sentences for: security or permission risks, irreversible actions, deleting data, questions the user must decide or clarify

## Tools

- Read files with Read (offset / limit for large files), change them with Edit / Write; Bash is for running tests, git, scripts, and searching
  - Why: reading files with Bash is often blocked by the sandbox (e.g. `plans/`, `~/.claude`) and has to be retried after failing; a compound `cd` command triggers a permission prompt, which interrupts auto mode; Edit can't track a file read with Bash, so it has to be read again
  - Filtering command output with Bash is fine (e.g. `<test command> | grep -E "..."`)
- Search: use the Grep / Glob tools when they exist (Windows, npm build); without them (removed from the Linux / macOS native build since v2.1.117, replaced by ugrep / bfs embedded in Bash) use Bash `grep` / `rg` / `find`
  - One command at a time, not chained with `&&` or `;` (compound commands get blocked by permissions or the classifier)
  - Limit the output: `grep -l`, `grep -m <n>`, `| head`; to see part of a file use Read, not `sed -n` / `cat`
- Never start a Bash command with `cd <path>;` or `cd <path> &&`: the working directory is already the current checkout; for another directory use absolute paths, `git -C <path>` or `env -C <path> <command>` (the `bash-guard` hook blocks it)
- Bash / Monitor never take the `allowed_domains` parameter (`autoMode.hard_deny` always blocks it); project domains are added, after the user reviews them, to the repo's `.claude/settings.json` `sandbox.network.allowedDomains`. When a command is blocked, first tell which layer it was: a permission-classifier denial, or a domain listed in `<sandbox_violations>`; for a missing domain tell the user, never open it yourself
- Bash can neither see nor write `plans/` (sandbox); plans always go through the Read / Write tools
- Temporary files always go in a `$TMPDIR/<topic>/` subdirectory (`<topic>`: the branch topic or the name of this piece of work), never directly in the top level of `$TMPDIR`; read and write them with Bash (`cat`, heredoc, redirection, small scripts): Write / Edit can only write inside the project (a hook blocks the rest), and Read can't read them either (`permissions.blockReadsOutsideWorkingDirectories`)
  - Why: the sandbox's `$TMPDIR` (`/tmp/claude-<uid>`) is shared by every session (planning session, workers, other sessions), and generic file names at its top level (`SKILL.md`, `pyproject.toml`) overwrite each other
- Hook state is in `.hook-only/` at the checkout root; only hooks can write it; both Read and Bash can read it (except its `hook-read-only/` subdirectory)
- Exception: the files in `~/.claude/skills/`, `~/.claude/templates/` (skill reference files, templates) are outside the working directory, where Read can't reach them; use Bash `cat` instead (the sandbox allows reading them)

## Git

- Always `git add` explicit paths, never `git add -A` / `git add .`
- PR mode never merges automatically; PRs wait for human review. In local mode (`wt local on`, recorded in the main checkout's `.hook-only/flow-mode`; `python3 ~/.claude/config/homeconfig.py flow-mode` prints the current checkout's mode) the local main is the source of truth: after a worker ends with `STATUS: done`, a hook (`auto-merge.py`) merges it into the local main when every condition holds, and people approve only the plan; an agent itself never merges
- After the user runs git operations in their own terminal (fetch, rebase, push), confirm with `git log` that the refs really moved
- The branch format is fixed at three parts: `<type>/<scope>/<topic>`
  - Type: `feature` (new feature, refactor, docs, tooling, investigation) or `fix` (a single bug fix, which takes the simplified flow)
  - Scope: exactly one from the scope table in the project `CLAUDE.md`; when the change spans several scopes, the one with the most changes; for a new scope, ask the user first
  - Topic: English kebab-case, 2–4 words; `plans/<topic>.md` uses the same name
  - Exception: in a new repo with no scope table yet, the first branch is always `feature/docs/repo-init`
- `plans/` is added to `.gitignore` and never committed; decisions that must be kept long-term go into the project docs (DESIGN, TODO) and the PR description

## Roles

- **Entry**: the main session always loads skill `route` first (its "Interacting with the user" applies for the whole session): when the first message is a `/` command or a `wt` command, load it and then run that command as typed; for any other message, judge the situation from `route`'s table and load the matching playbook; when switching to another kind of work, go back to `route`'s table. Subagents and headless workers don't load it
- **Planning session**: the main checkout; `/repo-plan` discusses with the user and opens worktrees; `/finish-worktree` wraps up
- **Orchestrator session**: runs `/feature-flow <topic>` in a worktree; dispatches subagents, verifies, commits
- **subagent**: qa-planner, spec-reviewer, qa, developer, code-reviewer. Does only what it is assigned and reports according to its budget; shared rules in skill `agent-common`
- **verifier**: started by a hook after the worker ends (skill `verify-topic` fork); judges the result against the approved plan on real data; the orchestrator can't call it

## Principles

- **Find the root cause, do the long-term best**: on a problem, first find its root cause, then choose the long-term best approach, not the one that makes the symptom go away fastest or gets the check at hand to pass
  - A long-term best approach that is too big is split into stages, never downgraded to a short-term fix; every stage, once done, must be able to stop there on its own without risk (security, data, irreversible external effects, wrong results, a forced rewrite later)
  - Keep designs flexible and extensible: later stages plug into extension points left for them, without overturning the earlier stage
  - An approach that only works around the symptom may only be an explicitly stated temporary measure, written down together with the root cause and the proper fix to come
  - When an approach can't hold, go back and examine the cause; never stack patches on it or prop it up with more resources
- **Risk can veto a design, never a requirement**: a requirement is not the source of risk, the design is; safety rules are conditions a design must meet, not reasons to veto a requirement. When a design is risky, don't patch the original design: complete it, or think up an entirely new design from scratch; only when several rounds of designs still can't produce an acceptable one, apologise and say it is beyond your ability, list the designs tried and where each got stuck, and ask the requester whether the requirement can be adjusted (the main session asks the user; a subagent reports to whoever dispatched it, who passes the question on). When a requirement conflicts with another requirement, state the conflict clearly at once and let the requester decide the trade-off
- **Two failed fixes → verify the premise**: when two or more fixes built on the same premise have all failed, first write that premise down and verify it directly, then write the next fix
- **Explain a number before using it**: before reporting or acting on a measured number (duration, speed, cost, a result ratio), first confirm that it measures what you think it does, what limited it, and whether the same measurement contains results that shouldn't be there
- **Test what can be tested; test decisions and numbers, not pixels**
- Rules that must always hold are enforced with hooks, the sandbox or settings; text only explains
- Values go in config files or resources, never hard-coded
- Keep logic and presentation separate; logic doesn't depend on the UI / scene tree, and the time source is injected

## Where the flow rules live

| Content | Location |
|---|---|
| Situation routing: the entry, each situation's playbook (questions, debugging, verification, research, home changes, direct changes), shared operations; the rules for how the main session works with the user | skill `route` ("Interacting with the user", `playbooks/`, `ops/`) |
| Planning: classes, plan format, discussion record, kickoff, worktree hand-off, planning pitfalls | skill `repo-plan` |
| Development: each class's steps, dispatch and budgets, must-fix vs TODO, cost table and pitfall log, wrap-up | skill `feature-flow` |
| Wrap-up: sorting out costs and pitfalls, the lessons record, removing the worktree | skill `finish-worktree` |
| Shared subagent rules: budgets and asking for help, test scope, report format | skill `agent-common` |
| Refactoring: lock today's behaviour first, compare new and old case by case over generated inputs, removed-behaviour table, bugs found are only recorded | skill `refactor` (preloaded by home-drafter; a repo's developer loads it only when needed) |
| Code quality (the parts that need judgement: naming, duplication, dead code, comments, error handling); the numeric thresholds are in crap-gate | skill `clean-code` (preloaded by developer, code-reviewer) |
| Project-specific: test commands, protected paths, completion checks | project skill `project-flow` |
| PR conflicts: checking, handing the merge to a worker | skill pr-conflict, feature-flow merge.md |
