# Kickoff checklist

Topics to settle with the user for a new repo. One line per item; add lines as new needs come up. Each answer goes into a repo file through the `feature/docs/repo-init` branch (target in brackets).

## Product
- Goal, target users, the core experience in one paragraph [DESIGN]
- What is explicitly out of scope for now [DESIGN]

## Architecture
- Language, framework and engine version; where binaries live [project CLAUDE.md `Environment` (older: 環境)]
- Main modules and what each owns; logic vs presentation split [DESIGN]
- Where numbers and tunables live (config files, resources) [project CLAUDE.md `Architecture rules` (older: 架構規則)]

## Conventions
- Naming, formatting, comment language [project CLAUDE.md `Conventions` (older: 慣例)]
- Branch scope table: one scope per module or area [project CLAUDE.md, heading `Branch scopes` (older: `Branch 範圍`)]
- Class examples: which work in this repo is logic, wiring, presentation [project CLAUDE.md `Class examples` (older: 分類範例)]

## Tests
- Test framework and version; how to run all tests and one test [project-flow skill]
- "Done" checks beyond tests (build, smoke run, no generated-file churn) [project-flow skill]
- Protected paths: tests, test config, shared files [`.claude/guard-paths.json` from `~/.claude/templates/guard-paths.json`]
- Quality gates (complexity, CRAP, unexecuted changed lines at the developer's stop and at commit; mutation for the code reviewer): per language the coverage command, a fail-fast test command, source and excluded paths; the coverage tool as a dev dependency (Python: pytest-cov and `[tool.coverage.run] patch = ["subprocess"]`) [`.claude/quality-gates.json` from `~/.claude/templates/quality-gates.json`, dependency file, project-flow skill]. Without the file the gates still run, on the home profiles (`~/.claude/config/home.json` (`toolchains`)) of the languages detected from the tracked files, and block when a command can't run (e.g. pytest-cov missing); the file adds what only the repo knows (packages, excludes, `select`, lint `checks`) and `"languages": {"<lang>": false}` turns a detected language off
- Agent notes skills for project pitfalls (`developer-notes`, `qa-notes`, `code-reviewer-notes`, `spec-reviewer-notes`) [`.claude/skills/`]

## Repo hygiene
- `.gitignore`: `plans/`, build caches, local files [`.gitignore`]
- Files no agent may ever change (e.g. `.env`): add a sandbox denyWrite [user settings, by the user]

## Remote (by the user, outside Claude Code)
- The sandbox mounts `.git/config` read-only (also for `!` commands), so the user runs these in an outside terminal: create the GitHub repo, `git init` if needed, `git remote add origin <url>`, `git fetch origin`, then create the local default branch tracking `origin/<branch>`. Check the remote branch name after the fetch (GitHub may create `main` regardless of the account setting); never work around it with `--no-track` [nothing in the repo; new-worktree needs origin]

## First work
- First batch of TODO items, in priority order [TODO]
