# Playbook: research an external fact

1. **Write the question as a claim to check** (e.g. "field X is cumulative, not per quarter"). One claim per line.
2. **Official sources first** (`ops/cite.md`): docs written for developers or AI (llms.txt, API reference, schema, changelog). Community posts only to find the official page, or when nothing official exists, labelled as such.
3. **Search, then read**: a search result's snippet is not evidence; open the page (web-fetch agent) and quote it.
4. **Each claim ends in one of three states:** confirmed (link + quote), contradicted (link + quote), or unanswered → an open question for the user, never a guess and never "the worker checks later".
5. Where it goes: planning → the plan or `plans/repo-plan.md` `討論中` with the citation; ~/.claude change → the home-settings-plan row; a plain question → the answer with a Sources list.
