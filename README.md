# ~/.claude 使用說明

這個目錄是我的 Claude Code 個人設定：一套「規劃 session 規劃、headless worker 在 worktree 開發、subagent 分工、hook 強制規則」的流程。這份文件寫給自己看：架構、怎麼用、去哪裡找東西、卡住時怎麼辦。給 Claude 的規則在 `CLAUDE.md` 和各 skill，不在這裡。

## 架構

### 角色

| 誰 | 在哪 | 做什麼 |
|---|---|---|
| 我 | 規劃 session、外部終端機 | 討論、決定、核准（排佇列、要人看的 merge）、改 `~/.claude`、想到時 push 備份 |
| 規劃 session | repo 的 main checkout | `/repo-plan`：討論、寫 plan 與估計、排佇列或開 worktree。不寫程式 |
| worker（指揮 session） | `../worktrees/<topic>`，`claude -p` 背景執行 | `/feature-flow <topic>`：派 subagent、驗證、commit、寫 `PR_DESCRIPTION.md`。不 push |
| subagent | worker 底下 | qa-planner、spec-reviewer、qa、developer、code-reviewer，各做一段 |
| verifier | worker 結束後，由 hook 另開 | 真實資料第 2 層：對照核准的 plan 判斷結果（見「真實資料檢查」） |
| hook | Claude Code 外（sandbox 外） | 強制規則、記錄、啟動 worker、merge、收尾；agent 改不了它的紀錄（`.hook-only/`） |

### 一個 topic 從規劃到 merge（本機模式，預設）

```
規劃 session ─ /repo-plan ─ plans/queue/<topic>.md、.json（＋plans/verify/<topic>.json）
      │
我核准 ─ pane 的 queue all／queue，或 wt queue <topic> …
      │
wt_queue ─ 輪到時（after 已 merge、在 worker 數／金額／額度限制內）建 worktree ─ wt launch
      │
worker ─ /feature-flow：qa-planner → spec-reviewer → qa → developer → code-reviewer（hook 管預算、品質閘門、mutation）
      │                     預算不夠派工 → 自動續跑；STATUS: needs-decision／blocked → 叫我
STATUS: done
      │
verify.py（第 1 層）→ verifier.py（第 2 層）─ 沒過 → 自動修正 run
      │
auto-merge.py ─ 條件全過 → merge 進本機 main；branch 落後 main → 自動 merge run；要人看 → 叫我（wt merge）
      │
auto-finish.py ─ 歸檔紀錄、匯入成本、踩坑排進 plans/friction.md、移除 worktree ─ 佇列開下一個
```

叫我的方式：pane 的 toast 和 `notices` 列表、我下次在主 checkout 送訊息時附上的事件（要我處理的標 `!`）。

### 情境路由（skill 樹）

每個主 session 一開始都先載入 `route`（開頭是 `/` 或 `wt` 指令也一樣，載入後照原樣執行那個指令）：它的「與使用者互動」整個 session 適用（先回答、提案寫齊、依風險取得同意、長工作派背景 subagent、報狀態寫出依據）；subagent 和 headless worker 不載入，所以這些規則不在全域 `CLAUDE.md`。一般需求照表判斷情境、說一句 `情境：… → …`，再載入那個情境的 playbook；playbook 走到某一步才載入它用到的操作檔。也可以自己打 `/route <需求>`。

```
route（root：判斷情境，不做事）
├─ 開發已規劃的 topic ───────── 有估計檔：叫我打 wt launch／wt resume；沒有：/repo-plan 先交接
├─ 規劃／討論／開 repo／進度 ── 叫我打 /repo-plan ── new-worktree ── worker /feature-flow
│                                                                   ├─ test-first（agents.md、test-first.md）
│                                                                   ├─ small-fix、presentation、docs-investigation
│                                                                   └─ merge.md
├─ PR 衝突 ──────────────────── 叫我打 /pr-conflict
├─ merge 後收尾 ─────────────── 叫我打 /finish-worktree
├─ 改 ~/.claude ─────────────── playbooks/home-config.md ── ops/evidence、ops/cite、ops/home-draft
├─ 除錯（以前會動／為什麼）──── playbooks/debug.md ──────── ops/evidence
├─ 跑起來看／手動驗證 ───────── playbooks/verify.md
├─ 查外部事實 ───────────────── playbooks/research.md ───── ops/cite
├─ 看懂 code（不改）─────────── playbooks/ask.md
├─ 明確要求就地修改 ─────────── playbooks/direct.md
├─ 閒聊、一句話答案 ─────────── 直接回答
└─ 其他（以上都不符合）──────── playbooks/other.md ──────── 記一行到 repo 的 plans/route-misses.md
```

- 前四個情境的 skill 都設了 `disable-model-invocation`，`route` 叫不動它們（官方文件：Claude 用 Skill tool 呼叫會被擋），只會告訴我要打哪個指令，也不會用別的方式照做它們的步驟；規劃和指揮因此仍然分開
- skill 只能放在 `~/.claude/skills/<名稱>/SKILL.md` 這一層（官方文件只寫這種結構），所以樹是邏輯上的：第二、三層是 `route/playbooks/`、`route/ops/` 的檔案，用 `cat` 載入（Read 讀不到 `~/.claude`）
- compact 之後只會重新附上叫用過的 skill（從最近叫用的開始放，有共用的 token 上限，叫用很多 skill 時 `route` 也可能被擠掉）；用 `cat` 載入的 playbook、操作檔一定不在，Claude 要再 `cat` 一次目前的 playbook
- 專案可以在自己的 `CLAUDE.md` 加 `## 情境補充` 表（例如資料分析），`route` 先看那張表、專案的列優先
- 加新情境：在 `route/SKILL.md` 的表加一列＋寫一個 playbook；重複出現的步驟抽成 `ops/` 檔。候選來自「其他」：每次走「其他」都在 repo 的 `plans/route-misses.md` 記一行（請求類型、最接近的情境為什麼不符合、實際做的步驟），同一類出現第三次時 Claude 會提一次，我決定要不要加

### 輸出精簡度與程式碼品質

- **輸出**（全域 `CLAUDE.md`「輸出」，借 caveman 的做法）：給我的回覆用 Lite（答案先行、不客套、保留完整句子和術語解釋）；subagent 回報和 worker 的指揮文字用 Full（可以只寫片語，因為指揮每個請求都會重讀報告，越短越省）；commit、PR、plan 的決定表、文件不壓縮。程式碼、路徑、指令、錯誤訊息、數字永遠一字不改；講到風險、不可逆動作、要我做決定時改回完整句子。省多少看 `cost/<id>.jsonl` 前後比較
- **程式碼品質**：數字門檻（CCN、CRAP、沒被執行的行）由 crap-gate 強制，格式由 ruff／gofmt；需要判斷的部分（命名、單一職責、重複、死碼、註解、錯誤不吞、純函式核心、介面型別與單位、只改需要的）在 skill `clean-code` 的 C1–C9。developer 和 code-reviewer 都預載同一份：developer 照著寫，reviewer 照著審、finding 標規則編號；會產生錯數字的 C6 算正確性問題，其餘由指揮依「必修 vs TODO」決定

## 怎麼用

### 日常流程（本機模式）

| 步驟 | 我做什麼 | 發生什麼 |
|---|---|---|
| 1. 規劃 | 在 main checkout 開 Claude，`/repo-plan` | 討論 scope、決定、分類（logic／wiring／docs…）；plan 依賴的外部事實當場查官方文件並引用，不猜、不留給 worker |
| 2. 交接 | 同意 plan | 規劃 session 寫 `plans/queue/<topic>.md`（plan）、`.json`（順序、`after`、資源標籤、估計）、有真實資料檢查就寫 `plans/verify/<topic>.json`；估計用 `cost-history.py estimate branch …`（固定開銷＋比例，加上歷史的閘門等待，沒有下限） |
| 3. 核准 | pane 的 Planned 區塊按 `queue all`（或某個 topic 的 `queue`，都按兩次），或打 `wt queue a b c` | 佇列在限制內開始能開的 topic；其餘顯示在等什麼 |
| 4. 等 | 什麼都不用做；想看就開 `wt pane`，或問「進度」 | worker 跑完會自己做兩層檢查、merge、收尾，再開下一個 |
| 5. 被叫回時 | 看 toast／`notices`／附在訊息後的事件，回答 Claude 的問題 | 決定寫進 worktree plan 後 `wt resume <topic>`；要人看的 merge 在 pane 的「Waiting for you」按 `merge`（或 `wt merge <topic>`）；核准跑測試期間那一列顯示 `approving run <n> (since HH:MM)`、沒有按鈕，結果以事件回來 |
| 6. 下次規劃 | `/repo-plan` | 規劃 session 讀 `plans/friction.md`（各 topic 收尾時排進來的踩坑與成本差距），和我決定要改的設定 |
| 7. 備份 | 想到時在外部終端機 `git push` | origin 只是備份 |

不經佇列的單一 topic：規劃 session 用 new-worktree 開好 worktree、寫 `plans/worker/<topic>.json`，我打 `wt launch <topic>`（之後一律 `wt resume`）。要中途停掉 worker：`wt stop <topic>`（之後可 `wt resume`）；停止永遠不會自動發生。

### 啟動與 pane

三種方式最後都送出同一句 `wt launch|resume|stop <topic>`，由 `worker-control.py` 處理，hook 的檢查三種都一樣。

| 方式 | 怎麼用 |
|---|---|
| 手動 | 自己打 `wt launch／resume／stop <topic>` |
| pane 按鈕 | `wt pane`（或輸入框上方帶狀區的 `wt pane` 按鈕）→ Controls 每個 topic 一列；按一下變黃色 `confirm …?`，`confirm_seconds` 內再按一下才送出。送出後該 topic 的按鈕變成 `<動作>… (sent …)`、不接受點擊，直到該 topic 有新事件、列的狀態改變或 `pending_minutes` 到期；hook 拒絕時立刻恢復，原因進 `notices` |
| 自動 | `wt auto on`（或按 pane 的 `auto: off`）：規劃 session 每次寫 `plans/worker/<topic>.json`，mod 在那一輪結束後自動送出 `wt launch` 或 `wt resume` |

自動模式的界線：只在這個 session 有效（開新 session 回到手動）；只有我在輸入框按 Enter（或 Remote Control）能打開，其他 plugin、peer session 送的一律拒絕；從不自動 `wt stop`；有 agent-guard HOLD 時不自動 resume；同一 topic 60 秒內只排一次；排進來的 prompt 等主 loop 那一輪回答完才送出，被拒時 toast 寫原因。`wt auto` 顯示狀態，`wt auto off` 關掉。這三個指令都被 mod 攔下，不送進模型。

pane 的內容：選中的 worker 的辦公室（終端機；其他介面是條列），每個 subagent 的時間與花費對預算（hook 等待中標 `waiting for … <已等>/<上限>`，計時停在等待開始），標題列是經過／估計與上限；下方 Controls（launch／resume／stop、auto、local）、Waiting for you（要人看的 merge）、Queue、Planned；最下方 `notices`（最近 `notices_max` 筆事件，完整文字，新的在上；未讀數也顯示在帶狀區）。能不能按什麼由 `hooks/wt_controls.py` 判斷，pane 只畫它的輸出；數值在 `mods/wt-pane/config.json`。

### 本機模式（local）

按 pane 的 `local: off` 兩次，或打 `wt local on`（`wt local off` 關、`wt local` 看狀態；只接受我在輸入框按 Enter 的）：mod 把模式寫進主 checkout 的 `.hook-only/flow-mode`，並設定這個程序的 `FLOW_INTEGRATION`；之後每個規劃 session 開始時 mod 讀這個檔。有 worker 在跑、有建好但還沒 launch 的 topic，或（開啟時）本機 main 落後 origin/main，都不切換並說明原因。每個 topic 在第一次 `wt launch` 時記下當時的模式（worktree 的 `.hook-only/flow-mode`），所以切換不影響進行中的 topic。也可以在 repo 的 settings 加 `"env": {"FLOW_INTEGRATION": "local"}`。本機 `main` 是準，origin 只是備份；不開 PR。

- **比較基準**：`skills/feature-flow/base-ref.sh` 和 `qg_common.integrationRef` 一律回本機 `main`（沒有就 `master`）；new-worktree、finish、merge run、品質閘門、reviewer 都以它為準；不 fetch
- **自動 merge**：worker 結束在 `STATUS: done` 時，`run-worker.sh` 呼叫 `hooks/auto-merge.py`，下列全部成立才 `git merge --no-ff` 進主 checkout 的 `main`（訊息用 PR_DESCRIPTION），結果寫 `runs/merge-<n>.json`：
  1. 沒有 ALERT／HOLD；repo 有 `quality-gates.json`（要有測試指令）
  2. 主 checkout 在 `main` 上，而且 `main` 還在這一輪開始時的位置（`runs/main-<n>`）：只被自動 merge 或 `wt merge` 移動過（`auto-merge/merges.jsonl` 有紀錄）就要先跑 merge run；被別的東西移動過就請我查 `git reflog main`。檢查加 merge 全程鎖在 `.hook-only/auto-merge/lock`
  3. branch 已包含 `main`（沒有就要先跑 merge run）、工作目錄乾淨
  4. 沒碰到要給人看的路徑：`hooks/auto-merge-hold.json`（`.claude/`、CI、依賴清單與 lock 檔）加 repo `quality-gates.json` 的 `auto_merge_hold`；除了文件和測試以外的文字檔沒有新增網址
  5. agent-guard 記下的最後一個完整 code-reviewer 結論是 APPROVE（`.hook-only/reviews.jsonl`）；之後 branch 自己的 commit 不能改原始碼，手動解的 merge 要有 merge review APPROVE。純文件 branch（每個改動都是 .md／.txt／.rst、不在測試路徑、沒有 reviewer 紀錄）免 APPROVE，`merge-<n>.json` 記 `"review": "docs-only"`
  6. 每個語言的測試指令在沙盒裡全過；再跑每個語言 `quality-gates.json` 的 `checks`（格式、lint，例如 `ruff format --check`、`gofmt -l`），同樣在沙盒、同樣的 cwd／env；寫成 `{"argv": [...], "empty_output": true}` 的還要求沒有任何輸出。有一項失敗就拒絕並寫出是哪一項和輸出末幾行；沒有 `checks` 只跑測試。所以本機模式下指揮收尾不再自己跑完成檢查
  7. 有 `plans/verify/<topic>.json` 時，`verify.py` 通過；spec 有 `verifier` 區段時，`verifier.py` 的判定也通過
- **branch 落後 main 時自動跑 merge run**：拒絕原因只有「branch 不含最新的 `main`」時，交給 `worker-control.py auto-merge`，用 merge 模式 resume 同一個 session（`merge.md`），結束在 `STATUS: done` 又回到 auto-merge。預算是 `budget-defaults.json` 的 `worker_auto_merge_minutes`／`_usd`，同一 topic 連續最多 `worker_auto_merge_max` 次；我打的 `wt launch`／`wt resume` 會歸零
- **真實資料顯示 branch 錯了時自動跑修正 run**：`verify.py` 有檢查沒過，或 verifier 判定 fail，交給 `worker-control.py auto-fix`，用 `FIX_PROMPT` resume：worker 把發現當成必修項（qa 先用 fixture 重現、developer 修、再一輪 code-reviewer），結束後重跑兩層檢查。預算＝`cost-history.py estimate branch … --fix`，同一 topic 連續最多 `worker_auto_fix_max` 次；我按的 `wt merge` 不會觸發
- 自己處理的拒絕記成事件 `retry`，不叫我；其他原因被拒絕、或 `worker-control.py` 開不了 merge／修正 run 時記成 `refused` 叫我。merge 錯了 `git revert -m 1 <commit>`
- **merge 後自動收尾**：`auto-finish.py` 等這一輪的 wrapper 結束，依序把 worktree 的紀錄（`.hook-only/`、plan、測試計畫、PR_DESCRIPTION）歸檔到 `.hook-only/archive/<topic>/`、匯入成本、把 plan 的 `## Friction` 和成本差距原因附加到 `plans/friction.md`、移除 worktree（branch 保留）。每一步的結果在 `.hook-only/archive/<topic>.finish.json`，前一步失敗就不做後面的

### Topic 佇列（本機模式）

一次規劃幾個 topic、我核准後就不用一個個 `wt launch`：照我給的順序、在限制內同時開幾個，前面的 merge 後再開下一個（`hooks/wt_queue.py`）。

- **規劃 session 寫**：`plans/queue/<topic>.md`（plan 本文）＋`plans/queue/<topic>.json`（`branch`、`order`、`after`、`resources`、`estimate`）；有真實資料檢查照樣寫 `plans/verify/<topic>.json`
- **我核准**：pane 的 `queue all`／`queue`（按兩次），或打 `wt queue a b c`（每份 spec 先檢查，有一份壞就整批不加）。`wt queue` 的回應只顯示給我、不進對話；`wt queue` 不帶參數先檢查一次能不能開再顯示；`wt queue drop <topic>` 拿掉還沒開始的
- **輪到時**：從當下的 `main` 跑 new-worktree.sh、把 plan 複製進 worktree、寫 `plans/worker/<topic>.json`、走 `wt launch`
- **什麼時候檢查**：`wt queue` 加入時，以及每一輪寫完 `exit-<n>` 後（auto-merge 已做完）
- **等待原因**（照順序；等待中的不擋後面能開的）：已開始；訂閱額度；`after` 的 topic 還沒 merge 進本機 `main`；估計單獨就超過 `queue_max_concurrent_usd`（stuck）；忙碌中的 topic 達到 `queue_max_workers` 或估計加總超過 `queue_max_concurrent_usd`；跟忙碌中的 topic 有相同資源標籤
- **限制**：`budget-defaults.json` 的 `queue_max_workers`、`queue_max_concurrent_usd` 是預設；repo 在 `.claude/flow.json` `{"queue": {...}}` 覆寫（從本機 `main` 讀）；資源標籤的意義由 repo 定
- **訂閱額度**（5 小時與每週）：`hooks/quota-snapshot.py` 包住狀態列，每次刷新寫 `~/.claude/quota/usage.json`；任一時窗達 `queue_max_five_hour_pct`／`queue_max_seven_day_pct` 且還沒重置就不開新的；快照太舊時照 `queue_quota_unknown`（`wait` 或 `start`）。只在 `budget-defaults.json`，repo 不能覆寫；已經在跑的不停
- 只支援本機模式（`after` 靠本機 `main` 判斷）
- 啟動失敗留在佇列標 error、不重試（`drop` 可拿掉）；啟動與失敗記在 `.hook-only/queue/log.jsonl`，也是事件
- 完成的項目：branch 已 merge、worktree 已移除後標成 done，`plans/queue/<topic>.*` 搬到 `.hook-only/archive/<topic>/queue/`，以 merged 顯示 `queue_done_linger_minutes` 後移除

### 真實資料檢查

測試用 fixture；只有真實資料看得出的事（指令跑得完、輸出兩次相同、錯誤路徑會拒絕且不寫檔、數值在範圍內）由規劃 session 寫進主 checkout 的 `plans/verify/<topic>.json`（worker 寫不到；格式見 `templates/verify.json`）。

- **第 1 層 `verify.py`**：worker 結束在 `STATUS: done` 後，把 `data_paths` 複製到暫存的 DATA_ROOT，在 bwrap 裡從 worktree 跑每個檢查（斷網、worktree 唯讀），只看結束碼、輸出文字、兩次執行的 sha256、應該不變的檔案。結果 `runs/verify-<n>.json`
- **第 2 層 verifier**：hook 判斷不了的（數字合不合理、規則在真實資料上是否成立）寫進 spec 的 `verifier` 區段（`usd`、`minutes`、`judge` 項目）。第 1 層通過後 `hooks/verifier.py`：
  - 輸入放 `<worktree>/.hook-only/verifier-<n>/`：核准版 plan、spec、第 1 層結果、真實資料副本
  - 核准版 plan：我每次啟動 run 時，`worker-control.py` 把 plan 複製到 `<main>/.hook-only/approved/<topic>.md`，所以 worker 後來改的 plan 不會變成自己的驗收標準；`plan-guard.py` 擋 verifier 讀 worktree 的 `plans/`
  - 在 worktree 執行 `claude -p "/verify-topic <topic> <n>"`：skill `verify-topic`（`context: fork`、`agent: verifier`）fork 出 verifier，對指揮盲、不對 plan 盲
  - 預算經歷史模型換算後由 `subagent-budget.py` 管；verifier 不能改 worktree（前後比對）；報告第二行 `VERDICT: pass|fail`
  - 結果 `runs/verifier-<n>.json`：`passed`、`failed`（交回 worker 修正）、`error`（verifier 自己出錯 → 等我）
- 需要人判斷的只剩畫面：UI 進 `plans/manual-verify.md`

### 腳本化流程（試行，2026-11-02）

指揮的成本約一半花在讀檔、記帳、寫派工 prompt 這些機械性工作（實測：52 個 worker session，判斷類請求不到 5%）。腳本化流程把「下一步做什麼」交給 hook，指揮只照做；判斷交給 Opus 的 `flow-judge`。

- 開啟：規劃 session 在 `plans/worker/<topic>.json` 加 `"flow": "scripted"`（只支援 logic、wiring、small fix；其他類別照原流程），`wt launch` 時 `worker-control.py` 跑 `flow-next.py init`，啟動 prompt 換成「讀 `scripted.md`，照 `.hook-only/flow/next.md` 做」
- 每一步：subagent 交回報告（`SubagentHandback`）時 `flow-next.py report` 依狀態機算下一步，寫進 `next.md`；指揮收到回報讀它、照做；指揮自己派工或 commit 後，下一步以 additionalContext 回來，不用再讀檔
- 派工的指派內容寫在 `.hook-only/flow/prompts/<n>.md`，派工 prompt 只有一行「讀這個檔」加 `Estimate:`（歷史中位數，dispatch-guard 再套模型）；commit 前 hook 先 `git add` 該 agent 改的檔，指揮只打一個 `git commit`
- 不在階段邊界停：tests commit 後直接派 developer、實作 commit 後直接派第一輪 code review，沒有 `PHASE:` 回合，也就沒有 compact 和續跑（實測 2026-11-02：每次 compact 約 $0.04-0.07、0.3 分鐘，外加一個續跑回合；指揮只讀 `next.md` 和回報，context 本來就小）。`run-worker.sh` 的 PHASE 機制留給原流程
- 保護不變：派工、commit 都還是指揮的工具呼叫，dispatch-guard、agent-guard、crap-gate、預算 hook 照常；看不懂的回報、第二次 CHECKPOINT、輪數上限、judge 沒給 `DECISION:` → `STATUS: needs-decision`
- 回報格式：各 agent 的回報開頭是固定欄位（`report-check.py` 在交回時檢查），flow-next 只讀這些欄位，不猜內文
- 停下後：每次停下都存著停下前的狀態和造成停下的回報（`state.json` 的 `stopped_from`）。是 hook 讀錯（例如判定行格式）時，修好 `flow-next.py` 後直接 `wt resume`：worker-control 先自動 `flow-next.py retry`，用新程式重新處理那份回報：狀態可以繼續（`flow-next.py runnable`）就照腳本做法續跑
- 退回原流程：沒 retry 的停下（等我決定的事）後 `wt resume`、修正 run、merge mode 都先 `flow-next.py off`，之後這個 topic 走原本的 feature-flow；wrap-up（TODO、PR_DESCRIPTION、衝突檢查）本來就照原文字做
- 紀錄：`.hook-only/flow/state.json`（狀態）、`log.jsonl`（每一步）、`reports.jsonl`（每份回報原文）；`## QA commits`、`## Reviewer`、agent 的 `Friction` 由 hook 寫進 plan

### PR 模式（不開本機模式時）

比較基準是 `origin/<預設分支>`（hook 先 fetch）；不自動 merge、不排佇列。worker 結束後我在外部終端機跑 Claude 給的 push 指令、開 PR（內容貼 worktree 的 `PR_DESCRIPTION.md`；Claude 會先提醒 branch 有沒有改到 `.claude/`、`.mcp.json`）。PR 衝突：`/pr-conflict <topic>`，有衝突時 Claude 用 `cost-history.py estimate branch … --merge` 估計、寫帶 `"mode": "merge"` 的估計檔，我打 `wt resume <topic>`，worker 照 `merge.md` 合併。PR merge 後 `/finish-worktree <topic>`：整理成本與踩坑、移除 worktree（branch 保留）。

## 指令速查

| 指令 | 在哪打 | 用途 |
|---|---|---|
| `/route [需求]` | 任何地方 | 判斷情境、載入對應 playbook（開頭訊息不是指令時 Claude 會自己載入） |
| `/repo-plan` | main checkout | 規劃、開新 repo（kickoff）、選下一個 topic、問進度 |
| `wt queue [<topic> ...]` | main checkout | 把 `plans/queue/` 的 topic 依序排進佇列並開始能開的；不帶參數顯示佇列；`wt queue drop <topic>` 拿掉還沒開始的（本機模式） |
| `wt launch <topic>` | main checkout | 第一次啟動 worker（不經佇列時） |
| `wt resume <topic>` | main checkout | 從上次 session 繼續（決定已寫進 plan）；估計檔有 `"mode": "merge"` 時改成合併 |
| `wt stop <topic>` | main checkout | 停止 worker（SIGINT，10 秒後 SIGTERM） |
| `wt merge <topic>` | main checkout | 核准一個被 auto-merge 拒絕、要人看的 topic：hook 在背景跑測試等其他條件後 merge、記進 `merges.jsonl`（其他 worker 視為正常移動），再自動收尾；結果是事件。同一個 run 一次只核准一個：開始前寫 `runs/approve-<n>.json`（auto-merge 的 pid），進行中再下 `wt merge` 回「already being approved」；那個行程不在了（被殺）就視為過期、可以重來；auto-merge 結束時刪掉紀錄。auto-merge 先查「已經 merge 過」（`merge-<n>.json` 說 merged，或 branch tip 已在 main 且 `merges.jsonl` 有這個 branch）：有決定檔就什麼都不改、不發事件；沒有（例如 merge run 沒東西可 merge）就記成 merged（`"review": "already-merged"`）並收尾，不再要求 merge run |
| `wt pane` | 互動 session | 開關 worker 面板（帶狀區也有按鈕）；不送進模型 |
| `wt auto [on\|off]` | main checkout 的互動 session | 自動模式開關（只在這個 session）；不送進模型 |
| `wt local [on\|off]` | main checkout 的互動 session | 本機模式開關；不送進模型 |
| `/pr-conflict <topic>` | main checkout | PR 跟 main 衝突時（PR 模式） |
| `/finish-worktree <topic>` | main checkout | PR merge 之後收尾（PR 模式）；自動收尾失敗時也用它 |
| `/feature-flow <topic>` | worktree | worker 自己用；要在桌面 session 手動開發時才自己打 |
| `/blind-review` | worktree | 真正的盲審（每輪新的 reviewer），只在需要時 |

`/repo-plan`、`/feature-flow`、`/finish-worktree`、`/pr-conflict`、`/blind-review` 都設了 `disable-model-invocation`：只能由我打（或 worker 啟動指令帶上），規劃與指揮不會載入彼此的 skill。`wt ...` 都由 hook 或 mod 處理，回應只顯示給我、不花模型回合。

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
| `<repo>/plans/queue/<topic>.md`、`.json` | 排佇列用的 plan 與 spec（順序、`after`、資源標籤、估計）；完成後搬進歸檔 |
| `<repo>/plans/verify/<topic>.json` | 真實資料檢查的 spec（第 1 層檢查、第 2 層 `verifier` 項目） |
| `<repo>/plans/worker/<topic>.json` | `wt` 讀的估計：分鐘與 `est_usd`（上限＝兩者 × `budget_headroom`）、session id；`"mode": "merge"`＝合併 run |
| `<repo>/plans/friction.md` | 各 topic 收尾時排進來的踩坑與成本差距，下次規劃 session 處理 |
| `<repo>/plans/config-update/` | 要套用到 `~/.claude` 的計畫（草稿放 `$TMPDIR/home-apply-<主題>/`） |
| `<repo>/.hook-only/events.jsonl` | 事件紀錄（run 結束、merged、refused、retry、verified、佇列、收尾），只有 hook 寫 |
| `<repo>/.hook-only/workers/<topic>.json` | `wt` 記錄的 PID、session id、第幾輪 |
| `<repo>/.hook-only/queue/` | 佇列狀態與 `log.jsonl` |
| `<repo>/.hook-only/auto-merge/merges.jsonl` | 自動 merge 與 `wt merge` 的紀錄（其他 worker 用它判斷 main 是誰移動的） |
| `<repo>/.hook-only/approved/<topic>.md` | 我啟動 run 時的核准版 plan（verifier 對照它） |
| `<repo>/.hook-only/archive/<topic>/` | 收尾時歸檔的 worktree 紀錄；`<topic>.finish.json` 是收尾每一步的結果 |
| `<worktree>/plans/<topic>.md` | 該 topic 的 plan、`## Cost`（指揮自己的列與超支原因）、`## Friction` |
| `<worktree>/plans/tests/<topic>.md` | 測試計畫 |
| `<worktree>/.hook-only/runs/` | 每輪的 `start-<n>`、`est-<n>.json`、`exit-<n>`、`result-<n>.json`、`stopped-<n>`、`merge-<n>.json`、`auto-<n>.json`、`verify-<n>.json`、`verifier-<n>.json`；`skills/repo-plan/run-result.py` 判讀並印出下一步 |
| `<worktree>/.hook-only/budget/` | subagent 預算狀態（等待中的 `hook_wait` 也在這）與 `events.jsonl` |
| `<worktree>/.hook-only/cost/` | 每個 subagent 每輪實際用量（`usd`、token、起訖時間） |
| `<worktree>/.hook-only/quality/` | crap-gate 的結果快取、通過紀錄、developer 被擋次數；`mutation-<reviewer>-<fingerprint>.json` 是 mutation 掃描的結果摘要（`all_caught`、`delivered`、`no_result`、`sandbox_unavailable`） |
| `<worktree>/.hook-only/hook-read-only/` | agent 讀不到的紀錄：mutation 的完整結果（`mutation/`）、worker-progress 給 pane 的進度（`progress/`） |
| `<worktree>/.hook-only/reviews.jsonl` | agent-guard 記的 code-reviewer 結論；本機模式的 auto-merge 只認這裡 |
| `<worktree>/.hook-only/flow/` | 腳本化流程：`scripted`（開關）、`state.json`、`next.md`（指揮的下一步）、`prompts/<n>.md`（派工內容）、`log.jsonl`、`reports.jsonl`（回報原文） |
| `<worktree>/.claude-cost/history.tsv` | new-worktree 複製進來的成本歷史，`cost-history.py estimate` 用：估計模型＝固定開銷＋比例 × 原始估計（每個 agent、branch 類別、merge run 各自擬合，沒有下限；選定理由與重跑方式見 `cost-history.py` 的說明和 `estimate-replay.py`） |
| `<worktree>/PR_DESCRIPTION.md` | merge 訊息／PR 內容（不 commit） |
| `~/.claude/mods/wt-pane/` | worker 面板 mod（`settings.json` 的 `env.CLAUDE_CODE_PLUGIN_DIRS` 載入；sandbox 的 allowRead 要有 `~/.claude/mods`）；數值在 `config.json` |
| `~/.claude/quota/usage.json` | 狀態列寫的訂閱額度快照，佇列讀 |
| `~/.claude/docs/` | 各機制的設計說明，索引見 `docs/README.md` |

`plans/` 和 `.hook-only/` 都在 `.gitignore`；worktree 移除前，收尾會先把紀錄歸檔、成本抄進歷史。

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
| crap-gate | 改到的函式 CCN > 10（原本就超過的不得再增加）、CRAP > 30、改到的行沒被任何測試執行、排除標記沒寫理由或新增超過 3 個（presentation 只查 CCN）。developer 交回 `DONE` 報告（`SubagentHandback`）或結束時擋最多 3 次（`CHECKPOINT`／`BLOCKED` 不查：被預算停住或要指揮決定的狀況，擋了只會燒預算到 ALERT），之後寫 agent-guard ALERT；`git commit` 時再查一次，依 stage 的內容：有原始碼（實作 commit）還要求測試全過；只有測試、且 branch 還沒 commit 過原始碼也還沒 commit 過測試（test-first 的第一個測試 commit）要求測試失敗、且不是 `red_invalid_patterns` 的原因（例如 SyntaxError；缺模組／名稱是預期的紅）；之後只改測試的 commit（補缺口、NEEDS-TEST、實作 commit 之前的測試修正）兩者都不查，也不會被工作目錄裡沒 commit 的實作擋。developer 交回時測試紅不算它的 block（可能在回報測試寫錯） | 門檻在 repo 的 `.claude/quality-gates.json` `limits`（預設 `hooks/quality-defaults.json`）；ALERT 的解除同 agent-guard。`quality-gates.json` 和 `guard-paths.json` 一律讀 diff base（已 merge 的版本），不讀工作目錄：裡面的指令由 hook 在 Claude Code 的 sandbox 外執行，branch 上改了也要 merge 後才生效。repo 可以在 `quality-gates.json` 加 `"protect"`（例如 `["data"]`），沙盒裡那些路徑唯讀，疊加在 `hooks/qg_sandbox.json` 的預設（`.git`、`.hook-only`、`.claude`、`plans`）之上，不能移除預設 |
| subagent 預算 | 派工 prompt 沒有 `Estimate: <分> min, $<美元>`（新工作的原始估計）、`Remaining: …`（CHECKPOINT 後剩下的工作）或 `Budget: …` 行（dispatch-guard；前兩種由 hook 套歷史模型換成 `Budget:` 行，指揮不用自己跑 `cost-history.py estimate agent`）；subagent 用到預算 × `budget_headroom`（預算是估計的需要量，不是上限；停下回報 CHECKPOINT） | orchestrator 可以自己續跑一次，給剩下工作的 `Remaining:` 行；同一件事第二次 CHECKPOINT 問使用者；`SubagentHandback`（交回報告）永遠放行 |
| worker 時間上限 | worker 超過 `plans/worker/<topic>.json` 的分鐘 | 回報 `STATUS: blocked`，重新估再 resume |
| worker 剩餘時間不夠派工 | 派工或續跑 agent 時，`Budget:` 的分鐘＋閘門等待（code-reviewer 的 mutation 掃描，merge review 除外；developer 的 crap-gate）＋收尾餘裕（`budget-defaults.json` 的 `worker_dispatch_margin_minutes`）超過剩下的分鐘（dispatch-guard） | 回報 `STATUS: blocked`，plan 寫好下一個派工與估計。dispatch-guard 同時在 `.hook-only/runs/auto-resume-<n>.json` 記下續跑需要的分鐘與美元（派工預算＋閘門等待＋收尾餘裕＋續跑啟動），`run-worker.sh` 收尾時交給 `worker-control.py auto`：沒被 `wt stop`、沒有 ALERT／HOLD、需要量在 `worker_auto_resume_max_minutes`／`_usd` 內、連續自動續跑未滿 `worker_auto_resume_max`（我打的 `wt launch`／`wt resume` 會歸零）就自動續跑一次（預算取派工紀錄和「我上次啟動時的估計扣掉已用」較大者，上限封頂），決定寫在 `auto-<n>.json`（run-result 會說）；之後有派工被放行就刪掉紀錄。wt-pane 的 auto 模式自己會送 `wt resume`，次數就會一直歸零。紀錄是 hook 寫的，agent 寫不到，所以 worker 沒辦法自己要求自動續跑；其他情況照舊重新估再 resume |
| worker 結束格式 | worker 的最後訊息第一行不是 `STATUS:`／`PHASE:`，或還有背景 agent 在跑就寫了 `STATUS:`／`PHASE:`（worker-stop） | 擋一次要它補上；第二次放行，規劃 session 當 blocked 處理 |
| `--max-budget-usd` | worker 花超過美元上限 | 同上 |

## 卡住時

| 現象 | 處理 |
|---|---|
| worker 說連不到某個網域 | 審核後自己加進 repo 的 `.claude/settings.json`（worktree 裡也要有，未 merge 的改動要手動同步），再 resume |
| worker 找不到工具（`uv: command not found`） | 工具裝在 home：加進 home `allowRead`，開新 session |
| agent-guard ALERT：qa 改到非測試檔 | 測試放錯位置或 guard-paths 沒列；改 guard-paths 或搬測試，回話後 `wt resume` |
| agent-guard ALERT：`no start snapshot` | 看該輪的 SubagentStart 有沒有觸發（`.hook-only/budget/events.jsonl`） |
| subagent 被擋「no budget state」 | 看 `<worktree>/.hook-only/budget/events.jsonl` 的事件順序 |
| `exit-<n>` 是 90 | `claude` 沒登入：外部終端機 `claude auth login` |
| `run-result.py` 說 `STOPPED` | `wt stop` 停的，正常；用 `wt resume` 繼續 |
| 打 `wt ...` 沒反應 | hook 沒裝或沒在 settings：檢查 `~/.claude/hooks/worker-control.py` 和 `hooks.UserPromptSubmit` |
| `wt auto on` 了但沒自動啟動 | Claude 回覆裡的 `wt-pane auto:` 說明原因（HOLD、沒有 worktree、已在跑）；沒有這句代表 mod 沒載入或不是這個 session 開的：`wt auto` 看狀態 |
| pane 按鈕按了沒送出 | 第一下只會變黃色 `confirm …?`，要在 `confirm_seconds` 內按第二下；按鈕顯示 `<動作>… (sent …)` 代表已送出、在等結果；被 hook 拒絕的原因在 `notices` |
| `wt launch` 說已經有 session | 這個 topic 跑過了，用 `wt resume` |
| 自動收尾失敗（事件 `finish-failed`） | 看 `.hook-only/archive/<topic>.finish.json` 哪一步失敗；已 merge 的 topic 用 `/finish-worktree <topic>` 手動收尾 |
| docs topic 被 auto-merge 拒絕：`Failed to spawn: pytest` | worktree 沒有 `.venv`（docs worker 不跑測試）：在 worktree 跑 `uv sync --offline` 後 `wt merge <topic>` |
| PR 衝突時 `wt resume` 說 `no session id` | 這個 topic 不是 worker 做的：在 worktree 開 session 送 `/feature-flow <topic> merge` |
| merge 的 commit 被 `merge-commit-guard` 擋 | 還有沒解完的檔案，或留下了兩邊都沒有的衝突標記行；照列出的行刪掉、重新 `git add` 再 commit |
| agent 說沒有 Grep／Glob | Linux／macOS 原生版沒有，改用 Bash `grep`／`find`（內嵌 ugrep／bfs） |
| 結果檔的 `duration_ms` 很小 | 不算背景 subagent；看 `start-<n>`／`exit-<n>` |
| crap-gate 說 `coverage for python produced no report` | 覆蓋率指令沒產生檔案：專案沒裝 pytest-cov，或 `quality-gates.json` 的指令錯了；測試在斷網、`$HOME` 幾乎全空的 bwrap 裡跑（`hooks/qg_sandbox.json`），要連網、讀 `$HOME` 其他地方、`uv run` 沒加 `--no-sync` 都會失敗 |
| crap-gate／mutation 說 `environment problem … bwrap unavailable` | 這台機器的 bwrap 不能建 namespace：在外部終端機跑 `bwrap --ro-bind / / --unshare-net true` 看錯誤；hook 不會改成在沙盒外跑 |
| 沙盒裡呼叫 git 的工具失敗（例如 setuptools-scm），crap-gate 只說 `produced no report` | worktree 的 `.git` 指向主 checkout 的 `.git`，在被遮掉的 home 底下；目前的已知限制 |
| 改了 `quality-gates.json`／`guard-paths.json` 沒效果 | hook 讀比較基準（本機模式是本機 `main`，PR 模式是 origin 的預設分支）上的版本：merge 進去後才生效（刻意的：branch 不能改 hook 要執行的指令） |
| reviewer 交回報告得很慢 | mutation 在 `SubagentHandback` 等背景掃描（pane 顯示 `waiting for mutation scan`，等待不算進 reviewer 的預算）。沒設 `select` 的語言每個 mutant 跑整套測試；Python 設了 `select`：baseline 只量改動檔所在目錄並記每個測試的 context，每個 mutant 只跑執行到那一行的測試（查不到的跑 `fallback`），存活的一律整套重跑，抓到的要同一組測試在沒改壞時能通過才算——比業界主流保守，但不保證跟每個 mutant 跑整套完全相同；之後只改測試檔的重掃沿用沒受影響的結果。上限是 `mutation_max` 與 `mutation_wait_seconds`；reviewer 自己挑的 mutant（`mutate-request`）也一樣：pane 顯示 `waiting for reviewer's mutants`，等待不算進預算，設了 `select` 只跑涵蓋那行的測試、存活的再跑全套確認 |

## Hooks 一覽

| 檔案 | 事件 | 作用 |
|---|---|---|
| `wt_events.py` | UserPromptSubmit（主 checkout）；各 hook 用 `emit` | 事件紀錄 `.hook-only/events.jsonl`（run 結束與 STATUS、merged、refused、佇列開始／失敗、收尾完成／失敗，只有 hook 寫）。我在主 checkout 送訊息時，把上次之後的新事件附在訊息後（不多一個回合；要我處理的標 `!`）；pane 的 mod 每 `events_poll_seconds` 讀新事件跳 toast（pane 關著也會，worker 不會；顯示時間依長度在 `toast_min_seconds`～`toast_max_seconds` 之間，要我處理的最久），並收進 pane 底部的 `notices` 列表（最近 `notices_max` 筆、完整文字、新的在上，未讀數也顯示在帶狀區） |
| `wt_queue.py` | worker-control（`wt queue`）、`run-worker.sh`（每輪 `exit-<n>` 後 `tick`） | topic 佇列：依序、在 `queue_max_workers`／`queue_max_concurrent_usd`／資源標籤／`after`／訂閱額度內開始；從當下 `main` 建 worktree 再走 `wt launch`；flock 在 `.hook-only/queue/lock` |
| `quota-snapshot.py` | 狀態列（settings `statusLine` 的 command 包住原本的指令） | 把狀態列 JSON 的 5 小時／每週用量寫進 `~/.claude/quota/usage.json` 給佇列讀；輸出交給原本的狀態列指令 |
| `wt_controls.py` | （wt-pane 與 worker-control 呼叫） | 每個 topic 能做什麼（launch／resume／stop、不能時的原因、HOLD、auto 能不能送）與本機模式能不能切換的唯一判斷；輸出 JSON `schema` 1（欄位刪改就升版）。跑中看 run 檔（有 `start-<n>`、沒有或空的 `exit-<n>`），不看 pid：pane 的程序可能在 sandbox 裡，看不到 worker 的 pid；真正啟動和停止前由 sandbox 外的 worker-control 用 pid 核實 |
| `worker-control.py`＋`run-worker.sh` | UserPromptSubmit | `wt launch／resume／stop／merge／queue`；（`wt resume` 在最後一輪 `STATUS: done` 且沒有新估計時拒絕，規則在 `wt_controls.py`；stop 先寫 `runs/stopped-<n>`；resume 解除該 worktree 的 agent-guard HOLD；估計檔 `"mode": "merge"` 時 resume 送合併 prompt，mode 記在 state 裡延續到之後的 resume，launch 拒絕）；`run-worker.sh` 在 `PHASE:` 切點與 run 結束時 `/compact`（`.hook-only/runs/` 的 `phase-<n>-<k>.json`、`compact-<n>-<k>.json`），每次呼叫只給這一輪剩下的 USD |
| `worker-budget.py` | PreToolUse | worker 的時間上限（從 `.hook-only/runs/start-<n>` 起算） |
| `worker-progress.py` | PreToolUse（async） | 只在 worker 裡：每次工具呼叫寫 `hook-read-only/progress/<key>.json`（做什麼、這個 agent 本輪花費，依 `prices.json`），給 wt-pane 面板；不輸出、不擋 |
| `dispatch-guard.py` | PreToolUse（Agent、SendMessage） | 派工 prompt 的 `Estimate:`／`Remaining:` 行套歷史模型換成 `Budget:` 行（已寫 `Budget:` 照用；歷史檔不在就擋），暫存給 subagent-budget；worker 剩餘時間（含閘門等待）／USD 不夠就擋；code-reviewer 換成固定的盲審 prompt（`Merge review:` 開頭的除外） |
| `worker-stop.py` | Stop | worker 最後訊息要以 `STATUS:`／`PHASE:` 開頭；背景還有工作時不准寫這兩行 |
| `subagent-budget.py` | SubagentStart／Stop、PreToolUse | 領取 dispatch-guard 暫存的預算（`.hook-only/budget/pending-*.json`）；subagent 用到預算 × `budget_headroom` 就擋（`SubagentHandback` 除外）、事件紀錄 |
| `subagent-cost.py` | SubagentStart／Stop | 記錄每個 subagent 實際用量（`usd`、token、舊的 `weighted_tokens`）；Start 記開始總量，`this_round` = 現在 − 開始 |
| `agent-guard.py` | subagent 前後、PreToolUse、UserPromptSubmit；code-reviewer frontmatter 的 PostToolUse `SubagentHandback` | 快照比對 subagent 改了什麼（其他 branch 的移動只提醒，因為 branch 在所有 worktree 共用的 `.git` 裡，agent 用 `git branch -f` 就能移動別的 topic 的 branch；這輪期間該 worktree 有 worker run 在跑、或新 commit 記在 `auto-merge/merges.jsonl` 的移動不提醒，並行時不再洗版）；ALERT 後擋派工與 commit，我回話時解除；記 code-reviewer 的結論到 `reviews.jsonl` |
| `plan-guard.py` | PreToolUse | 各角色能讀寫哪些 plan |
| `crap-gate.py` | developer 的 PreToolUse `SubagentHandback`＋Stop（frontmatter）、PreToolUse（`git commit`） | 改到的程式：CCN、CRAP、沒被執行的行、排除標記；擋 developer 最多 3 次後交給 agent-guard 的 HOLD；擋不合格的 commit |
| `mutation.py` | code-reviewer 的 frontmatter：PreToolUse 任何工具（`start`、async）、PreToolUse `mutate-request`、PreToolUse `SubagentHandback`、Stop | reviewer 第一次用工具時背景全掃改到的行（`quality-gates.json` 的 `select`：只跑執行到該行的測試、只改測試的重掃沿用結果；context 由 `qg_cov_contexts.py` 在專案的 Python 裡讀）；跑 reviewer 手挑的 3–5 個（`mutate-request`：跟全掃同一套跑法，有 `select` 時只跑執行到該行的測試、存活的整套重跑；等待期間標記 waiting（上限 `manual_wait_seconds`）並從預算扣除）；交回報告前等全掃完成，把活下來的交給 reviewer 判斷（等待從預算扣除）；等不到結果時也擋一次，讓報告寫「沒跑完」；每種結果都在 `.hook-only/quality/mutation-*.json` 留摘要。crap-gate 和 mutation 跑的測試都在 bwrap 裡（`qg_common.runCommand`、`hooks/qg_sandbox.json`：根目錄唯讀、`$HOME` 與 `/mnt` 等遮掉只放回工具和快取路徑、斷網、清空環境變數、`.git`／`.hook-only`／`.claude` 唯讀），因為 hook 本身不在 sandbox 裡 |
| `qg_common.py`、`qg_complexity.py`＋`qg_goccn.go`、`qg_mutate.py`、`qg_cov_contexts.py`、`qg_langs.json`、`quality-defaults.json` | — | 共用：diff 範圍與覆蓋率解析、複雜度（Python `ast`、Go `go/ast`、其他語言通用引擎）、mutant 產生、per-test coverage context 的讀取（在專案自己的 Python 裡執行）、各語言語法、預設門檻；`markWait`／`excludeWait` 記錄與扣除閘門等待；測試在 `hooks/tests/` |
| `block-write-outside-project.py` | PreToolUse | 擋 Write／Edit 寫到專案外 |
| `bash-guard.py` | PreToolUse（Bash） | 擋 `cd <路徑> &&`／`cd <路徑>;` 開頭的指令（全域 CLAUDE.md「工具」：複合 `cd` 會跳權限確認、中斷 auto mode 和 worker），提示絕對路徑、`git -C`、`env -C`；單獨的 `cd` 不擋 |
| `report-check.py` | flow agent（qa-planner、spec-reviewer、qa、developer、code-reviewer、flow-judge）frontmatter 的 PreToolUse `SubagentHandback` | 回報開頭必須是固定欄位（`report-schema.json`：`STATUS:`、`VERDICT:`、`QUESTIONS:`、`FIX-QA:`／`FIX-DEVELOPER:`、`NEEDS-TEST:`、`DECISION:`，`report_schema.py` 解析）；不合格擋下交回並附範本，同一 agent 擋 `max_denials` 次後放行（flow-next 讀不到就停下等我） |
| `flow-next.py` | flow agent（含 `flow-judge`）frontmatter 的 PostToolUse `SubagentHandback`（`report`）；PostToolUse Agent／SendMessage、Bash `git commit`（settings.json，主 session） | 腳本化流程的狀態機（沒有 `.hook-only/flow/scripted` 就什麼都不做）：回報 → 下一步寫進 `.hook-only/flow/next.md`、指派寫進 `prompts/<n>.md`；派工記下等待的 agent id；commit 前 `git add` agent 改的檔、commit 後記 QA commit；`init`／`off` 由 worker-control 呼叫 |
| `checkpoint-status.py` | UserPromptSubmit | 背景 subagent 回報有 `CHECKPOINT` 狀態行時，附上當下的 `git status --short`（agents.md：照實際存在的檔案記錄，不照回報）；輸入沒有來源欄位，所以使用者自己打的也會附，無害 |
| `session-mark.py` | SessionStart／End | 記錄 session 起訖（進度查詢用） |
| `fetch-origin.py` | skill 執行前 | new-worktree、finish-worktree、pr-conflict 前先 fetch |
| `finish-worktree.py`、`merge-origin.py` | PostToolUse | 在 sandbox 外移除 worktree、merge 整合分支（origin 的預設分支，本機模式是本機 `main`） |
| `verify.py` | 不是 hook 事件：`run-worker.sh` 在 `STATUS: done` 後、有 `plans/verify/<topic>.json` 時呼叫（兩種模式） | 在沙盒裡對真實資料副本跑規劃寫的檢查，結果寫 `runs/verify-<n>.json`（見「真實資料檢查」） |
| `verifier.py` | 不是 hook 事件：`run-worker.sh` 在 `verify.py` 之後呼叫；spec 有 `verifier` 區段且第 1 層通過才跑 | 準備輸入與預算、開 `claude -p "/verify-topic …"`、檢查 worktree 沒被改、讀判定寫 `runs/verifier-<n>.json`（見「真實資料檢查」） |
| `auto-merge.py` | 不是 hook 事件：本機模式下 `run-worker.sh` 在 `STATUS: done` 後呼叫 | 條件全部成立才把 branch merge 進本機 `main`（見「本機模式」）；merge 後啟動 `auto-finish.py` |
| `auto-finish.py` | 不是 hook 事件：`auto-merge.py` merge 成功後以獨立程序啟動 | 等 wrapper 結束後歸檔紀錄、匯入成本、把踩坑排進 `plans/friction.md`、移除 worktree（見「本機模式」） |
| `merge-commit-guard.py` | PreToolUse（Bash） | merge 進行中（有 `MERGE_HEAD`）時，`git commit`／`git merge --continue` 遇到還沒解完的檔案、或兩個 parent 都沒有的衝突標記行（`<<<<<<<`、`>>>>>>>`、`\|\|\|\|\|\|\|`；`=======` 不查，Markdown 標題底線會誤判）就擋；複合指令（`git add … && git commit`）在整串執行前就檢查，所以照擋並提示 add 和 commit 分開跑 |
| `hookonly.py` | — | 共用：`.hook-only/` 的位置 |
| `prices.py`＋`prices.json` | — | 共用：各模型單價（$／百萬 token，cache write 依 5m／1h TTL 分開），預算、成本紀錄、worker 派工檢查都用 USD |
| `tokenweights.py` | — | 共用：舊的 token 價格權重（`tokens` 預算與舊紀錄用） |

## 新 repo

1. GitHub 建 repo；外部終端機 `git init`、`git remote add origin <url>`、`git fetch origin`，建立追蹤 `origin/<main>` 的本地 branch
2. 在 repo 旁邊建 `worktrees/` 目錄（sandbox 不能建）
3. 開 Claude，`/repo-plan`，走 kickoff checklist；第一個 branch 固定是 `feature/docs/repo-init`
4. 規劃 session 列出專案要用的網域，我審核後自己加進 repo 的 `.claude/settings.json`；新工具的路徑加在 home settings
5. 要啟用 crap-gate 和 mutation：複製 `~/.claude/templates/quality-gates.json` 到 repo 的 `.claude/`，填各語言的覆蓋率與測試指令（Python 可加 `select` 讓 mutation 只跑相關測試；本機模式可加 `checks` 列出格式與 lint 指令，auto-merge 在測試之後執行，取代指揮收尾時手動跑的完成檢查），專案裝好覆蓋率工具（Python：pytest-cov，`[tool.coverage.run] patch = ["subprocess"]`），再 merge 進比較基準（本機模式的本機 `main`，或 origin 的預設分支）：hook 讀那裡的版本，加入設定的那個 branch 自己不受檢查；比較基準不存在時（PR 模式、還沒 push 的新 repo）閘門整個關閉。真實資料等不能壞的路徑加進 `"protect"`。指令要能離線跑（`uv run --no-sync`），因為 hook 在斷網的 bwrap 裡執行它
6. 要用本機模式：`wt local on`
