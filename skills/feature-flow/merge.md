# Merge mode: resolve conflicts with the default branch

Started as `/feature-flow <topic> merge` in the topic's worktree, usually as a new headless worker after skill `pr-conflict` found conflicts. Also used from the wrap-up check before the first push.

## Worker rules (always, even if the plan has no `## Worker 模式`)

Wherever this file says to ask the user, or anything blocks you, end the run. Your last message starts with one line `STATUS: done`, `STATUS: needs-decision` or `STATUS: blocked` (format: skill `repo-plan`, "Worker"), in Traditional Chinese. Never guess, never work around a block, never push, never fetch.

## Steps

1. **Context.** Read `plans/<topic>.md` (goal, rules, decisions) and `PR_DESCRIPTION.md`. Find the default branch: `git symbolic-ref --short refs/remotes/origin/HEAD` (fallback `origin/master`). Check `git status --short`: anything uncommitted besides `PR_DESCRIPTION*.md` → `blocked`.
2. **What the other side changed.** `git log --oneline HEAD..origin/<default>` and `git diff --stat HEAD...origin/<default>`. For each conflicting file, read both sides' change: `git diff $(git merge-base HEAD origin/<default>) origin/<default> -- <file>` and the same against `HEAD`.
3. **Merge.** Run exactly `bash ~/.claude/skills/feature-flow/merge-origin.sh`, with nothing added. It checks; on `READY` the merge-origin hook runs `git merge --no-ff --no-commit origin/<default>` outside the sandbox (git inside the sandbox can't rewrite protected paths such as `.claude/skills`) and returns the result as additional context: `MERGED`, or `MERGED WITH CONFLICTS` and the files. Never run `git merge` / `checkout` yourself, never rebase (the branch is pushed), and never work around a failed merge by pre-writing files: `FAIL` or no hook result → `blocked` with the message. Files under protected paths that hold conflict markers are fixed with Edit like any other file.
4. **Resolve by kind:**
   | Kind | Who |
   |---|---|
   | docs, TODO, PR-independent text (both sides appended, both edited a list) | you: keep both sides' meaning; fix numbering and cross-references that now collide (e.g. two sections both numbered 50 → renumber this branch's items and every reference to them, including in tests and the plan) |
   | protected test paths (project `guard-paths.json` `tests` / `qaConfig`) | `qa`: give it both sides' diffs and the rule each side follows |
   | implementation | `developer`: same, plus "tests must not change" |
   | both sides change the same rule or behaviour differently | `needs-decision`: quote both, recommend one |

   Also look past textual conflicts: shared helpers, renamed APIs, numbering that both sides extend. Search for this branch's references into anything the other side renumbered or renamed.
5. **Test.** Full suite (project `project-flow`). Failures route as usual (test-first.md): test problem → qa, implementation → developer. A failure that also fails on `origin/<default>` alone is pre-existing: TODO, not this merge's job.
6. **Commit** the merge with explicit paths: `git add <resolved files>` then `git commit --no-edit` (keep git's merge message; append a short English summary of manual resolutions).
7. **Review only what was hand-made.** If any hunk was resolved by hand (by you, qa or developer), run `code-reviewer` on that part only: give it `git diff HEAD^1 HEAD -- <hand-resolved files>` and the merge commit id; say which hunks were automatic. A fully automatic merge with green tests skips review.
8. **Wrap up.** Update `PR_DESCRIPTION.md` (a "Merged <default> at <sha>" note and any renumbering), add `## Cost` / `## Friction` rows, end with `STATUS: done` and the push command (a normal push, never `--force`).
