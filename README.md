# ~/.claude 使用說明

這個目錄是我的 Claude Code 個人設定：一套「規劃 session 規劃、headless worker 在 worktree 開發、subagent 分工、hook 強制規則」的流程。這份文件寫給自己看：怎麼用、去哪裡找東西、卡住時看哪裡。給 Claude 的規則在 `CLAUDE.md` 和各 skill，不在這裡。

## 角色

| 誰 | 在哪 | 做什麼 |
|---|---|---|
| 我 | 規劃 session、外部終端機 | 討論、決定、push、開 PR、merge、改 `~/.claude` |
| 規劃 session | repo 的 main checkout | `/repo-plan`：討論、寫 plan、開 worktree、等 worker、收尾。不寫程式 |
| worker（指揮 session） | `../worktrees/<topic>`，`claude -p` 背景執行 | `/feature-flow <topic>`：派 subagent、驗證、commit、寫 `PR_DESCRIPTION.md`。不 push |
| subagent | worker 底下 | qa-planner、spec-reviewer、qa、developer、code-reviewer，各做一段 |

## 日常流程

| 步驟 | 我做什麼 | 發生什麼 |
|---|---|---|
| 1. 規劃 | 在 main checkout 開 Claude，`/repo-plan` | 討論 scope、決定、分類（logic／wiring／docs…）；plan 依賴的外部事實（API 欄位、單位、期限）當場查官方文件並引用，不猜、不留給 worker |
| 2. 開 worktree | 同意 plan | Claude 讀 `skills/repo-plan/handoff.md`、跑 new-worktree：建 branch＋worktree（並複製成本歷史進去）、寫 plan、估成本（`cost-history.py estimate branch` 直接印出修正後的數字與 json）、寫 `plans/worker/<topic>.json`（修正後的 `minutes`、`est_usd`） |
| 3. 啟動 | 輸入 `wt launch <topic>`（只有第一次；之後一律 `wt resume`） | hook 在背景啟動 worker；Claude 在背景跑 `run-result.py <topic> --wait`（只等 `exit-<n>` 檔，結束時印出判讀、本輪 USD／分鐘），並提醒可以問「進度」 |
| 4. 中途 | 什麼都不用做；想看進度就問「進度」 | worker 跑完會停在 `STATUS: done／needs-decision／blocked`；test-first 的 run 在兩個切點（測試 commit 後、實作 commit 後）由 wrapper 自動 `/compact` 再接續，run 結束時也 compact 一次 |
| 5. 需要決定／被擋 | 回答 Claude 的問題 | Claude 把決定寫進 worktree plan |
| 6. 繼續 | 輸入 `wt resume <topic>` | worker 重讀 plan，從中斷處繼續 |
| 7. 完成 | 外部終端機跑 Claude 給的 push 指令；做手動驗證；開 PR（內容貼 worktree 的 `PR_DESCRIPTION.md`） | Claude 先檢查 branch 有沒有改到 `.claude/`、`.mcp.json`，有就提醒我看 |
| 8. PR 衝突 | `/pr-conflict <topic>` | 檢查衝突，交給 worker 合併 |
| 9. merge 後 | `/finish-worktree <topic>` | 整理成本與踩坑、討論要改的設定、移除 worktree 與 branch |

要中途停掉 worker：`wt stop <topic>`（之後可 `wt resume`）。

沒有 `wt` hook 時（或它出問題），Claude 會改給一行完整的 `claude -p ...` 指令，在外部終端機貼上執行。

## 指令速查

| 指令 | 在哪打 | 用途 |
|---|---|---|
| `/repo-plan` | main checkout | 規劃、開新 repo（kickoff）、選下一個 topic |
| `wt launch <topic>` | main checkout | 第一次啟動 worker |
| `wt resume <topic>` | main checkout | 從上次 session 繼續（決定已寫進 plan） |
| `wt stop <topic>` | main checkout | 停止 worker（SIGINT，10 秒後 SIGTERM） |
| `/pr-conflict <topic>` | main checkout | PR 跟 main 衝突時 |
| `/finish-worktree <topic>` | main checkout | PR merge 之後收尾 |
| `/feature-flow <topic>` | worktree | worker 自己用；要在桌面 session 手動開發時才自己打 |
| `/blind-review` | worktree | 真正的盲審（每輪新的 reviewer），只在需要時 |

`/repo-plan`、`/feature-flow`、`/finish-worktree`、`/pr-conflict`、`/blind-review` 都設了 `disable-model-invocation`：只能由我打（或 worker 啟動指令帶上），Claude 不會依描述自己載入，規劃與指揮也就不會載入彼此的 skill。

## 只能在外部終端機做的事

sandbox 擋住了這些，Claude 會給指令，我自己跑：

- `git push`、`git remote add`、設定 upstream、刪 branch（`.git/config` 在 sandbox 裡唯讀）
- 安裝工具（Go、uv…）
- 修改 `~/.claude`（skills、hooks、settings、CLAUDE.md）和 commit
- 需要 `claude` CLI 的事（sandbox 看不到 `~/.local/share/claude`）；`wt` hook 在 sandbox 外跑，不受影響

## 改 `~/.claude` 的方式

1. 規劃 session 發現問題 → 記進該 repo 的 `plans/config-update/home-settings-plan.md`（待辦總表在最上面）
2. Claude 讀現有檔案，做出**完整的新檔**放在 `$TMPDIR/home-apply-<主題>/`（`/tmp/claude-1000/home-apply-<主題>/`），附 diff
3. Claude 在回覆裡逐條列出指令（不包成腳本）；我 `cp` 過來、`git -C ~/.claude add <檔案>`、commit（Claude 會給 commit message）
4. `settings.json` Claude 讀不到，只給要貼的段落和位置
5. `~/.claude` 是 git repo：要還原就 `git -C ~/.claude checkout <檔案>`，不需要 `.bak`

## 東西在哪

| 位置 | 內容 |
|---|---|
| `<repo>/plans/repo-plan.md` | 規劃紀錄：討論中、待開 branch、不採用、未來 |
| `<repo>/plans/session-state.md` | 規劃 session 的交接狀態（compact 前寫） |
| `<repo>/plans/config-update/` | 要套用到 `~/.claude` 的計畫（草稿放 `$TMPDIR/home-apply-<主題>/`） |
| `<repo>/plans/worker/<topic>.json` | `wt` 讀的設定：修正後的分鐘與 `est_usd`（上限＝`est_usd` × `budget-defaults.json` 的 `worker_usd_cap_factor`）、session id；每次 launch／resume 前更新 |
| `~/.claude/docs/lessons-log.md` | 經驗紀錄；`/finish-worktree` 直接寫入（只新增，更正用新的一列） |
| `<repo>/.hook-only/workers/<topic>.json` | `wt` 記錄的 PID、session id、第幾輪 |
| `<worktree>/plans/<topic>.md` | 該 topic 的 plan、`## Cost`（只有指揮自己的列與超支原因；subagent 與 worker 由 hook 記錄）、`## Friction` |
| `<worktree>/plans/tests/<topic>.md` | 測試計畫 |
| `<worktree>/.hook-only/runs/` | 每輪的 `start-<n>`、`est-<n>.json`（`wt` 記的估計）、`exit-<n>`（結束碼＋時間）、`result-<n>.json`、`stopped-<n>`（`wt stop` 留的標記）；`skills/repo-plan/run-result.py` 判讀並印出下一步，`cost-history.py import` 匯入 worker 列與 branch 列 |
| `<worktree>/.hook-only/budget/events.jsonl` | subagent 預算 hook 的事件紀錄 |
| `<worktree>/.hook-only/cost/` | 每個 subagent 每輪實際用量：`usd`（成本表與預算用的單位，依 `hooks/prices.json`）、token 數、起訖時間；`cost-history.py rows` 和 `.hook-only/budget/events.jsonl` 的預算合成 Cost 表，`import` 匯入歷史 |
| `<worktree>/.claude-cost/history.tsv` | new-worktree 複製進來的成本歷史（唯讀副本；worktree 讀不到 main checkout），`cost-history.py estimate agent` 用 |
| `<worktree>/PR_DESCRIPTION.md` | PR 內容（不 commit） |
| `~/.claude/docs/` | 經驗文件與各機制的設計說明，索引見 `docs/README.md` |

`plans/` 和 `.hook-only/` 都在 `.gitignore`；worktree 移除時一起刪掉，所以 `/finish-worktree` 會先把成本抄進歷史。

## 保護機制（為什麼東西會被擋）

| 機制 | 擋什麼 | 要放行時 |
|---|---|---|
| sandbox 網路 | 不在允許清單的網域 | 專案網域寫在 **repo** 的 `.claude/settings.json` `sandbox.network.allowedDomains`，由我審核後自己加（規劃 session 列出網域和用途）；home 只放 GitHub |
| `permissions.deny` `Edit(...)` | Claude 的 Write／Edit 改 `.claude/settings.json`、`settings.local.json`、`.mcp.json`、`.claude/agents/**`（hook 註冊在 settings 裡、在 sandbox 外執行，所以設定只能我改） | 我在外部改 |
| `autoMode.hard_deny` | 指令自己帶 `allowed_domains` 開網域（strict 關著時的替代） | 不放行，一律改 settings |
| sandbox 檔案 | 讀寫 home 底下專案以外的地方；`blockReadsOutsideWorkingDirectories` 讓 repo 的 `filesystem` 設定**無效** | 工具或快取路徑加在 **home** settings 的 `allowRead`／`allowWrite`（已開：`~/go`、`~/.cache/go-build`、`~/.local/bin`、`~/.cache/uv`） |
| `plans/` 讀取限制 | Bash 讀不到 `plans/` | Claude 用 Read／Write 工具處理 plan |
| `.hook-only/` | 只有 hook 能寫，agent 改不了紀錄 | — |
| agent-guard | qa 改測試以外的檔案、developer 改測試、subagent 改 `.claude/` 底下任何東西（guard-paths 在 subagent 開始時就讀好，改了也不影響當輪檢查） | 測試路徑列在 repo 的 `.claude/guard-paths.json`（`<component>/tests/` 前綴）；`.claude/` 由規劃 session 或我改。ALERT 之後該 session 的派工和 `git commit`／`git merge` 一直被擋（`.hook-only/agent-guard/HOLD`），直到我回話；worker 則是我打 `wt resume` 才解除 |
| plan-guard | code-reviewer 讀 plan、qa 寫 `plans/tests/` 以外、developer 寫 `plans/` | — |
| subagent 預算 | 派工 prompt 沒有 `Budget: <分> min, $<美元>` 行（dispatch-guard）；subagent 用到預算 80%（停下回報）／100% | orchestrator 續跑時給新的 `Budget:` 行（`, full` 跳過 80%）；`SubagentHandback`（交回報告）永遠放行 |
| worker 時間上限 | worker 超過 `plans/worker/<topic>.json` 的分鐘 | 回報 `STATUS: blocked`，重新估再 resume |
| worker 剩餘時間不夠派工 | 派工或續跑 agent 時，`Budget:` 的分鐘＋收尾餘裕（`budget-defaults.json` 的 `worker_dispatch_margin_minutes`）超過剩下的分鐘（dispatch-guard） | 回報 `STATUS: blocked`，plan 寫好下一個派工與估計；重新估再 resume |
| worker 結束格式 | worker 的最後訊息第一行不是 `STATUS:`／`PHASE:`，或還有背景 agent 在跑就寫了 `STATUS:`／`PHASE:`（worker-stop） | 擋一次要它補上；第二次放行，規劃 session 當 blocked 處理 |
| `--max-budget-usd` | worker 花超過美元上限 | 同上 |

## 卡住時

| 現象 | 原因／處理 |
|---|---|
| worker 說連不到某個網域 | 審核後自己加進 repo 的 `.claude/settings.json`（worktree 裡也要有，未 merge 的改動要手動同步），再 resume |
| worker 找不到工具（`uv: command not found`） | 工具裝在 home：加進 home `allowRead`，開新 session |
| agent-guard ALERT：qa 改到非測試檔 | 測試放錯位置或 guard-paths 沒列；改 guard-paths 或搬測試 |
| agent-guard ALERT：`no start snapshot`，緊跟在同一個 agent 的 clean 之後（`guard.log`） | 背景 agent 交回報告會觸發第二次 SubagentStop；舊版第一次 Stop 就刪了快照。已改成 Stop 不刪快照；仍出現就看該輪的 SubagentStart 有沒有觸發（`.hook-only/budget/events.jsonl`） |
| subagent 被擋「no budget state」 | 看 `<worktree>/.hook-only/budget/events.jsonl` 的事件順序 |
| `exit-<n>` 是 90 | `claude` 沒登入：外部終端機 `claude auth login` |
| `run-result.py` 說 `STOPPED` | `wt stop` 停的，正常；用 `wt resume` 繼續 |
| 打 `wt ...` 沒反應 | hook 沒裝或沒在 settings：檢查 `~/.claude/hooks/worker-control.py` 和 `hooks.UserPromptSubmit` |
| `wt launch` 說已經有 session | 這個 topic 跑過了，用 `wt resume` |
| agent 說沒有 Grep／Glob | Linux／macOS 原生版 v2.1.117 起移除，改用 Bash `grep`／`find`（內嵌 ugrep／bfs）；規則寫成「有就用，沒有用 Bash」，`tools:` 保留給 Windows |
| 結果檔的 `duration_ms` 很小 | 不準（不算背景 subagent）；看 `start-<n>`／`exit-<n>` |
| PR 衝突 | `/pr-conflict <topic>` |

## Hooks 一覽

| 檔案 | 事件 | 作用 |
|---|---|---|
| `worker-control.py`＋`run-worker.sh` | UserPromptSubmit | `wt launch／resume／stop`（stop 先寫 `runs/stopped-<n>`；resume 解除該 worktree 的 agent-guard HOLD）；`run-worker.sh` 在 `PHASE:` 切點與 run 結束時 `/compact`（`.hook-only/runs/` 的 `phase-<n>-<k>.json`、`compact-<n>-<k>.json`），每次呼叫只給這一輪剩下的 USD |
| `worker-budget.py` | PreToolUse | worker 的時間上限（從 `.hook-only/runs/start-<n>` 起算） |
| `dispatch-guard.py` | PreToolUse（Agent、SendMessage） | 派工 prompt 必須有 `Budget:` 行，暫存給 subagent-budget；worker 剩餘時間／USD 不夠就擋；code-reviewer 換成固定的盲審 prompt（`Merge review:` 開頭的除外） |
| `worker-stop.py` | Stop | worker 最後訊息要以 `STATUS:`／`PHASE:` 開頭；背景還有工作時不准寫這兩行 |
| `subagent-budget.py` | SubagentStart／Stop、PreToolUse | 領取 dispatch-guard 暫存的預算（`.hook-only/budget/pending-*.json`）；subagent 預算 80%／100%（`SubagentHandback` 除外）、事件紀錄 |
| `subagent-cost.py` | SubagentStart／Stop | 記錄每個 subagent 實際用量（`usd`、token、舊的 `weighted_tokens`）；Start 記開始總量，`this_round` = 現在 − 開始 |
| `agent-guard.py` | subagent 前後、PreToolUse、UserPromptSubmit | 快照比對 subagent 改了什麼；ALERT 後擋派工與 commit，我回話時解除 |
| `plan-guard.py` | PreToolUse | 各角色能讀寫哪些 plan |
| `block-write-outside-project.py` | PreToolUse | 擋 Write／Edit 寫到專案外 |
| `session-mark.py` | SessionStart／End | 記錄 session 起訖（進度查詢用） |
| `fetch-origin.py` | skill 執行前 | new-worktree、finish-worktree、pr-conflict 前先 fetch |
| `finish-worktree.py`、`merge-origin.py` | PostToolUse | 在 sandbox 外移除 worktree、merge origin |
| `hookonly.py` | — | 共用：`.hook-only/` 的位置 |
| `prices.py`＋`prices.json` | — | 共用：各模型單價（$／百萬 token，cache write 依 5m／1h TTL 分開），預算、成本紀錄、worker 派工檢查都用 USD |
| `tokenweights.py` | — | 共用：舊的 token 價格權重（`tokens` 預算與舊紀錄用） |

## 新 repo

1. GitHub 建 repo；外部終端機 `git init`、`git remote add origin <url>`、`git fetch origin`，建立追蹤 `origin/<main>` 的本地 branch
2. 在 repo 旁邊建 `worktrees/` 目錄（sandbox 不能建）
3. 開 Claude，`/repo-plan`，走 kickoff checklist；第一個 branch 固定是 `feature/docs/repo-init`
4. 規劃 session 列出專案要用的網域，我審核後自己加進 repo 的 `.claude/settings.json`；新工具的路徑加在 home settings
