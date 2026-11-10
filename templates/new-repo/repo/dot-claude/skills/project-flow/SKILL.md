---
name: project-flow
description: Project-specific steps for feature-flow in this repo - completion checks and protected test paths. Agent-specific notes live in qa-notes, developer-notes, code-reviewer-notes and spec-reviewer-notes.
---

# Project flow

Setup and test commands are in the project `CLAUDE.md` "Environment" (older projects: 「環境」). What only one agent needs is in its notes skill: `qa-notes` (qa-planner, qa), `developer-notes`, `code-reviewer-notes`, `spec-reviewer-notes`.

## Completion check

The commands live in `.claude/quality-gates.json` and the home language profiles it lays over: each language's `test` and `checks`. In local mode auto-merge runs them before merging, so the orchestrator doesn't. In PR mode run them yourself, only for the side the change touches.

## Protected paths

See `.claude/guard-paths.json`.
