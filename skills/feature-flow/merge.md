# Merge mode: resolve conflicts with the default branch

Usually reached by `wt resume <topic>` in merge mode: skill `pr-conflict` found conflicts, and the planning session resumed the session that built the branch with worker-control.py's merge prompt. Also started as `/feature-flow <topic> merge` in the topic's worktree, and used from the wrap-up check before the first push.

**Where to start.** If a merge is in progress (`git rev-parse -q --verify MERGE_HEAD` prints a commit), an earlier run stopped inside this file: continue from the step where it stopped (`git status` shows what is resolved). Otherwise start at step 1, also when an earlier run already merged once: a new conflict is a new merge.

## Worker rules (always, even if the plan has no `## Worker 模式`)

Wherever this file says to ask the user, or anything blocks you, end the run with a `STATUS:` line (the worker-stop hook gives the format). Never guess, never work around a block, never push, never fetch.

## Steps

1. **Context.** Read `plans/<topic>.md` (goal, rules, decisions) and `PR_DESCRIPTION.md`. Find the integration branch, `<base>` below: `bash ~/.claude/skills/feature-flow/base-ref.sh` prints it (origin's default branch, or the local `main` in local mode, as the scripts use). Check `git status --short`: anything uncommitted besides `PR_DESCRIPTION*.md` and sandbox placeholders → `blocked`. Placeholders are untracked character devices (empty `/dev/null` mounts over protected paths such as `.bash_profile`, `.mcp.json`, `.claude/hooks`): check with `find <path> -maxdepth 0 -type c`, the same rule `merge-origin.sh` applies.
2. **What the other side changed.** `git log --oneline HEAD..<base>` and `git diff --stat HEAD...<base>`. For each conflicting file, read both sides' change: `git diff $(git merge-base HEAD <base>) <base> -- <file>` and the same against `HEAD`.
3. **Merge.** Run exactly `bash ~/.claude/skills/feature-flow/merge-origin.sh`, with nothing added. It checks; on `READY` the merge-origin hook runs `git merge --no-ff --no-commit <base>` outside the sandbox (git inside the sandbox can't rewrite protected paths such as `.claude/skills`) and returns the result as additional context: `MERGED`, or `MERGED WITH CONFLICTS` and the files. Never run `git merge` / `checkout` yourself, never rebase (in PR mode the branch is pushed; in local mode a rebase still invalidates the recorded review), and never work around a failed merge by pre-writing files: `FAIL` or no hook result → `blocked` with the message. Files under protected paths that hold conflict markers are fixed with Edit like any other file.
4. **Resolve by kind:**
   | Kind | Who |
   |---|---|
   | docs, TODO, PR-independent text (both sides appended, both edited a list) | you: keep both sides' meaning; fix numbering and cross-references that now collide (e.g. two sections both numbered 50 → renumber this branch's items and every reference to them, including in tests and the plan) |
   | protected test paths (project `guard-paths.json` `tests` / `qaConfig`) | `qa`: give it both sides' diffs and the rule each side follows |
   | implementation | `developer`: same, plus "tests must not change" |
   | both sides change the same rule or behaviour differently | `needs-decision`: quote both, recommend one |

   Also look past textual conflicts: shared helpers, renamed APIs, numbering that both sides extend. Search for this branch's references into anything the other side renumbered or renamed.
5. **Test.** Full suite (project `project-flow`). Failures route as usual (test-first.md): test problem → qa, implementation → developer. A failure that also fails on `<base>` alone is pre-existing: TODO, not this merge's job.
6. **Commit** the merge with explicit paths: `git add <resolved files>` then `git commit --no-edit` (keep git's merge message; append a short English summary of manual resolutions). The merge-commit-guard hook refuses the commit while a file is still unmerged or holds a conflict marker line (`<<<<<<<`, `>>>>>>>`, `|||||||`) that neither side of the merge had; the full test suite doesn't read docs, so this is what catches a leftover in `TODO.md`. Fix every line it lists, `git add` again, commit again; never work around it.
7. **Review only what was hand-made.** If any hunk was resolved by hand (by you, qa or developer), run `code-reviewer` on that part only: a prompt whose first line is `Merge review:` (so the hook keeps it), with `git diff HEAD^1 HEAD -- <hand-resolved files>`, the merge commit id, which hunks were automatic, and the `Estimate: <minutes> min, $<usd>` line (dispatch-guard adds the `Budget:` line; a merge review gets no automatic mutation scan, so dispatch-guard reserves no scan wait for it). A fully automatic merge with green tests skips review.
8. **Wrap up.** Update `PR_DESCRIPTION.md` (a "Merged <default> at <sha>" note and any renumbering), add `## Cost` / `## Friction` rows, end with `STATUS: done` and, in PR mode, the push command (a normal push, never `--force`). In local mode (`FLOW_INTEGRATION=local`) there is no push: auto-merge.py takes the branch from `STATUS: done`; a hand-resolved hunk needs the step 7 merge review, or auto-merge refuses.
