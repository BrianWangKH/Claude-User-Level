---
name: route
description: Root of the skill tree - read the user's request, pick its situation (plan, develop, PR conflict, wrap-up, question, debugging, verification, research, ~/.claude change, direct change, plain chat) and load that situation's playbook, which in turn loads its operation files. Never does the work itself.
when_to_use: On the first user message of every main session, a slash command or a `wt` command included (load it, then run that command as typed; its 「與使用者互動」 applies for the whole session), and again whenever the user switches to a different kind of task. Never in subagents or headless workers.
argument-hint: "[request]"
---

# Route

You are choosing **how** to work, not doing the work. Three levels:

1. **route** (this file): match the request to one situation below.
2. **playbook**: the situation's steps. Either a slash-command skill the user types, or a file under `playbooks/` you load with one Bash call: `cat ~/.claude/skills/route/playbooks/<file>` (the Read tool can't reach `~/.claude`).
3. **operation**: reusable steps a playbook names (`ops/<file>`, loaded the same way), or an operation skill (`new-worktree`, `pr-conflict`, `finish-worktree`).

Checkout: !`bash ~/.claude/skills/feature-flow/where.sh`

If no result appears above, run `bash ~/.claude/skills/feature-flow/where.sh` yourself.

## Situations

Go top to bottom; take the first row that matches. The project `CLAUDE.md` may add rows in a `## 情境補充` table: check it before this one, project rows win.

| # | The user wants | Where | Go to |
|---|---|---|---|
| 1 | plan, discuss design or TODO, start a repo, pick the next topic, check worker progress | main checkout | tell the user to type `/repo-plan` |
| 2 | develop a planned topic | main checkout | the default is the headless worker: `plans/worker/<topic>.json` exists → tell the user to type `wt launch <topic>` (first run) or `wt resume <topic>` (later runs); missing → the hand-off isn't done, tell the user to type `/repo-plan` |
| 2b | develop a planned topic by hand, in a desktop session | worktree | tell the user to type `/feature-flow <topic>` |
| 3 | a PR shows conflicts / "does it still merge" | main checkout | tell the user to type `/pr-conflict <topic>` |
| 4 | a PR was merged, clean up | main checkout | tell the user to type `/finish-worktree <topic>` |
| 5 | change `~/.claude` (skills, hooks, settings, CLAUDE.md, mods, agents), or a flow problem whose fix lives there | any | `playbooks/home-config.md` |
| 6 | something used to work and now doesn't; "why did X happen" | any | `playbooks/debug.md` |
| 7 | run it / see it working / manual verification | any | `playbooks/verify.md` |
| 8 | an external fact: a library, API, data source, Claude Code behaviour | any | `playbooks/research.md` |
| 9 | understand the code or the repo, no change ("how does X work", "where is Y") | any | `playbooks/ask.md` |
| 10 | explicitly asks you to change something here and now, outside the worktree flow | any | `playbooks/direct.md` |
| 11 | small talk, a one-line answer | — | answer directly, no playbook |
| 12 | anything rows 1–11 don't fit | any | `playbooks/other.md` |

Rows 1–4 (except the `wt` commands, which a hook handles) are `disable-model-invocation` skills: you can't load them, and you never reproduce their steps another way (that is what keeps planning and orchestration apart). Tell the user which command to type and why, in one line. A row 1/2/3/4 request in a worktree, or row 2b in the main checkout: say where it has to run instead.

## 與使用者互動

主 session 專用（全域 `CLAUDE.md` 只放所有角色都需要的規則；subagent 與 headless worker 不載入這個 skill）。

- 執行使用者沒要求的指令前，先用一句話說明要做什麼
- 開始一項任務時，第一則回覆先用自己的話寫出：目標、要解決的問題（根本原因；還沒查清楚就明說）、做法（長期最佳的做法，以及它能拆出的最小、單獨停下也沒有風險的第一步）；有只繞過症狀的快修就一併列出差在哪裡，由使用者選。這段說明本身不必等回覆（動手仍照下面提案的規則）；證據顯示使用者要的改動解決不了問題時，說明並停下
- 使用者提問或打斷時，那一回合先回答：回答需要時只讀檔，不跑其他指令、不改檔，等使用者說繼續
- 長指令（全套測試、實測、掃描）一律背景執行，讓使用者的提問隨時能被回答
- 長的工作（起草加測試的迴圈、多檔修改、大量調查，約 30 次以上工具呼叫）預設派給背景 subagent：先寫好規格（目標、要改的檔、驗收測試、放草稿的位置），派新的 agent（不用 fork：fork 繼承整個 context，每個請求都重讀，很貴），主 session 收回合；做完看 diff 和測試結果再交給使用者
  - 原因：主 session 一個回合做很多事時，使用者和 plugin（pane 按鈕）送的 prompt 都要等那個回合結束才進得來（2026-11-02：pane 的 `wt resume` 等了約 5 分鐘）
- 向使用者提案時一次寫齊，不等追問：根本原因（只繞過症狀要明說）、每個數字標「實測」或「推算」（推算不是承諾）、成本、兩個方向的誤判風險（安全方向與危險方向）、主流做法。沒有風險、可以丟棄的步驟直接做、做完回報（`$TMPDIR` 的草稿、背景只讀的實測）；會影響正在運作的流程、花實際的錢或時間、改 home 或 repo 的實際檔案，或長期最佳與無風險無法兼顧的選擇，先取得同意。被問「要不要做」或看到一個方案時，給自己真實的判斷：不值得就說不值得、該反對就反對，不以附和為預設
- 說 run、merge、佇列的狀態前先讀紀錄，回覆寫出依據的檔案與時間（`ops/evidence.md`）

## Rules

- Row 12 only when no earlier row fits: say which row came closest and why not. It is the default, not a shortcut: a request a row fits always takes that row's playbook.
- Before loading, say in one line: `情境：<situation> → <playbook>`. If two rows fit equally, pick the earlier one and say so; ask only if the two would change files differently.
- One playbook at a time. When the user's request changes kind mid-session, come back to this table (it is already in context; don't reload it) and switch.
- After loading a playbook, copy its steps verbatim into a todo list (TaskCreate) before any task-specific item; a step you won't do stays in the list as `skip: <reason>`, so a skipped step is visible.
- A playbook's operation files are loaded only when the playbook reaches that step, not up front.
- After a compaction, playbook and operation files loaded with `cat` are gone (only invoked skills are re-attached, from the most recent, within a shared token budget, so even this file can drop out): reload the current playbook with the same `cat` before the next step, and invoke skill `route` again if this table is missing.
