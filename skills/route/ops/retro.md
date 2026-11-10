# Operation: retro (the last step of every playbook)

Run once when the task is done, before the final report. It records quietly; it never turns into a repeated reminder.

1. **Reusable rule.** Did the task produce a rule that will apply again: a correction from the user, a constraint you discovered, a check that would have caught a mistake? For each, add one line to the report: the rule and where it belongs. Something that must always hold goes into a hook, test or script; something that needs judgement goes into skill text; the incident story goes to the incident log, never into a skill. Don't edit skills yourself: drafting follows `playbooks/home-config.md` once the user agrees.
2. **Ad-hoc sub-flow.** Did you run a sequence of steps the playbook doesn't describe? Append one line to the current repo's `plans/route-misses.md` (Write/Edit tool; create it with a header line if missing): `| <date> | <the kind of sub-task, a few words> | from <playbook>: no step for it | <the steps actually taken> |`. Outside a repo, put the line in the report instead. When the same kind appears there for the third time, mention it once to the user as a candidate `ops/` file or row; don't add it yourself.
3. Nothing found: skip silently, with no line in the report.
