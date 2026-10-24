---
name: blind-review
description: Optional true-blind code review - fork a fresh code-reviewer that never sees the plan and is never resumed. Not part of the default feature-flow. Takes no arguments.
when_to_use: Only when the user explicitly asks for a true blind review. Read ~/.claude/docs/blind-review.md first - in the desktop app (Agent SDK) forks run synchronously and return no agent id, so every round is a new reviewer.
context: fork
agent: code-reviewer
disable-model-invocation: true
---

Review the current branch blind.

1. Find the base yourself: the merge-base of `HEAD` with the default branch (`git merge-base master HEAD`, or `main` if there is no `master`).
2. Review everything from that base to the working tree, following your agent instructions.
3. End with your full report. Treat this as a fresh review: you will not be resumed.
