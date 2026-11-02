# Operation: check and cite an official source

1. Find the official page: a web search, then the vendor's docs domain. For Claude Code: `https://code.claude.com/docs/en/<page>`.
2. Read it with the web-fetch agent; ask it for the answer with verbatim quotes, not a summary of the page. Quotes it marks as summarised are checked again before a decision rests on them.
3. HTML only. A PDF or other binary URL: ask the user before fetching it.
4. Only official vendor docs are opened without asking. Any other site (community posts, third-party repos, marketplaces, blogs): list its plain-text URL and why, and fetch only after the user agrees. Search-result snippets are never cited.
5. Cite as: claim — page title — plain-text URL in a code block (never a markdown hyperlink) — "quote"; say which quotes were verbatim and which were the fetch agent's summary. List only pages actually read.
6. The docs don't answer it → write it down as an open question; an experiment may fill the gap only when the user agrees, and its result is labelled "observed", not "documented".
