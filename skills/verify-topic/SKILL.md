---
name: verify-topic
description: Real-data verification, second layer - fork a verifier that judges a finished branch against its approved plan on a copy of the real data. Started by the verifier.py hook after verify.py passed (`claude -p "/verify-topic <topic> <run>"` in the worktree). Never invoked by a model (the orchestrator can't pick its own verifier). Takes `<topic> <run>`.
argument-hint: <topic> <run>
context: fork
agent: verifier
disable-model-invocation: true
---

Verify the finished branch of this worktree. Arguments: `$ARGUMENTS` (the topic, then the run number).

1. Your inputs are in `.hook-only/verifier-<run>/` of this worktree (your agent instructions, "Inputs"). If that folder or its `plan.md` is missing, report `BLOCKED` with what is missing: the hook didn't prepare this run.
2. Follow your agent instructions ("Steps") and end with your report in their format: `DONE`, then `VERDICT: pass` or `VERDICT: fail`, then one line per check.
