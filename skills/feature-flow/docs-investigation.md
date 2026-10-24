# Docs and investigation

No subagents: you do the work yourself and keep the same `## Cost` and `## Friction` records.

## Docs

Changes to docs, flow, agents, skills or hooks that live in this repo.

1. Change exactly the files and sections the plan names.
2. Follow "Wrap up" in `SKILL.md`.

## Investigation

The option isn't settled and needs an experiment to decide.

1. Run the experiment for each candidate option the plan lists, the way it says to test it. Keep prototype code in `$TMPDIR` or another throwaway place; it never goes into the repo. Stay within the plan's budget; stop and report if an option can't be tested as planned.
2. Report a comparison table to the user in Traditional Chinese: each option against each decision criterion from the plan, with the measured numbers and how you got them. Recommend one, but the user picks.
3. Write only the decision into the doc the plan names (what was chosen, why, the rejected options in one line each) and commit it.
4. Follow "Wrap up" in `SKILL.md`; the PR description holds the full comparison table.
