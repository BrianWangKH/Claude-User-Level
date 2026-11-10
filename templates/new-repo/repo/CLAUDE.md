# CLAUDE.md

## Overview

{{NAME}}: a skeleton built by the home's new-repo kit. The goal, scope, architecture and the first work items are filled in with the user at the `/repo-plan` kickoff (kickoff checklist `~/.claude/skills/repo-plan/kickoff-checklist.md`); the user sets up the remote by the kickoff checklist.

## Environment

{{ENVIRONMENT}}

## Architecture rules

- Values and parameters go in config files, never hard-coded
- Keep logic and presentation separate

## Conventions

- Comments, commits and PRs in English; documents in the user's preferred language

## Branch scopes

| Scope | Covers |
|---|---|
| `app` | the program itself |
| `docs` | documents, `.claude/`, repo settings |

## Class examples

- logic: the program's calculations
- wiring: connecting the parts
- presentation: the output format

## Documents

- Design and to-do are created at the kickoff (`DESIGN.md`, `TODO.md`)
