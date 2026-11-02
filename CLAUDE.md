# 全域規則

各專案的 `CLAUDE.md` 補充專案專屬的內容（環境、範圍表、程式碼慣例、各類別的具體範例），兩者不衝突時同時適用；專案明確寫出例外時，以專案為準。

這份檔案每個 session 和每個 subagent 都會載入，只放所有角色都需要的規則。各角色的流程放在它會載入的地方（見最後的索引）。

## 溝通

- 回覆使用者用繁體中文；commit、PR、程式碼註解用英文
- 主 session 和使用者互動的規則（先回答、提案、依風險取得同意、長工作派 subagent、報狀態的依據）在 skill `route` 的「與使用者互動」：只有主 session 載入，subagent 與 headless worker 不讀

## 輸出

精簡程度依讀者決定：
- **Lite**（給使用者的回覆）：答案先行；不寒暄、不客套、不重述問題、不預告「接下來我會…」；保留完整句子，術語第一次出現時附白話解釋
  - 同一個回覆裡不重複：呼叫工具前只寫一句要做什麼，不先講結論；工具後只寫新的結果，不再重述工具前已經說過的內容
  - 例外（照常寫，不算預告或重述）：執行使用者沒要求的指令前的一句說明（skill `route` 的「與使用者互動」）、背景 session 要求的做法與進度說明、背景 session 回覆第一句帶出使用者問了什麼
- **Full**（subagent 回報、headless worker 的指揮文字、寫給另一個 agent 的訊息）：Lite 再加上：可以只寫片語，一句一個事實，因果用 `→`；英文省略冠詞和客套話
- **不壓縮**：commit message、PR 描述、plan 的決定表、DESIGN／TODO／README、lessons-log（長期給人看）
- 任何等級都一字不改：程式碼、路徑、指令和參數、錯誤訊息、測試名稱、數字和單位，以及 `STATUS:`／`PHASE:`／`DONE` 這類固定格式行
- 改回完整句子的情況：安全或權限風險、不可逆的動作、刪除資料、要使用者決定或釐清的問題

## 工具

- 讀檔用 Read（大檔用 offset／limit），修改用 Edit／Write；Bash 用來執行測試、git、腳本，以及搜尋
  - 原因：Bash 讀檔常被 sandbox 擋下（例如 `plans/`、`~/.claude`），失敗後要重試；複合的 `cd` 指令會觸發權限確認、中斷 auto mode；用 Bash 讀過的檔案 Edit 追蹤不到，要再讀一次
  - 過濾指令輸出可以用 Bash（例如 `<測試指令> | grep -E "..."`）
- 搜尋：有 Grep／Glob 工具就用（Windows、npm 版）；沒有時（Linux／macOS 原生版 v2.1.117 起移除，改由 Bash 內嵌 ugrep／bfs）用 Bash `grep`／`rg`／`find`
  - 一次一個指令，不用 `&&`、`;` 串接（複合指令會被權限或分類器擋下）
  - 控制輸出量：`grep -l`、`grep -m <n>`、`| head`；看檔案某段用 Read，不用 `sed -n`／`cat`
- Bash 指令不要用 `cd <路徑>;` 或 `cd <路徑> &&` 開頭：工作目錄已經是當前 checkout；需要其他目錄時用絕對路徑、`git -C <路徑>` 或 `env -C <路徑> <指令>`（`bash-guard` hook 擋）
- Bash／Monitor 不帶 `allowed_domains` 參數（`autoMode.hard_deny` 一律擋）；專案網域由使用者審核後加進 repo 的 `.claude/settings.json` `sandbox.network.allowedDomains`。指令被擋時先分辨是哪一層：權限分類器拒絕，還是 `<sandbox_violations>` 列出的網域；缺網域就告訴使用者，不自己開
- Bash 看不到也寫不進 `plans/`（sandbox）；plan 一律用 Read／Write 工具
- 暫存檔一律放 `$TMPDIR/<topic>/` 子目錄（`<topic>` 用 branch 主題或這次工作的名稱），不直接放 `$TMPDIR` 第一層；用 Bash 讀寫（`cat`、heredoc、重導向、小腳本）：Write／Edit 只能寫專案內（hook 擋），Read 也讀不到（`permissions.blockReadsOutsideWorkingDirectories`）
  - 原因：sandbox 的 `$TMPDIR`（`/tmp/claude-1000`）是所有 session 共用的（規劃 session、worker、其他 session），第一層的通用檔名（`SKILL.md`、`pyproject.toml`）會互相覆蓋
- hook 的狀態在 checkout 根目錄的 `.hook-only/`，只有 hook 能寫；Read 和 Bash 都可以讀（`hook-read-only/` 子目錄除外）
- 例外：`~/.claude/skills/`、`~/.claude/templates/`、`~/.claude/docs/` 的檔案（skill 的參考檔、範本、文件）在工作目錄外，Read 讀不到，改用 Bash `cat`（sandbox 已開放讀取）

## Git

- 一律用明確路徑 `git add`，不用 `git add -A`／`git add .`
- PR 模式不自動 merge，PR 等人工審核；repo 的 `.claude/settings.json` 設 `FLOW_INTEGRATION=local`（本機模式）時，本機 main 是準，worker 結束在 `STATUS: done` 後由 hook（`auto-merge.py`）在所有條件成立時 merge 進本機 main，人只核准 plan；agent 自己永遠不 merge
- 使用者在自己終端機做完 git 操作（fetch、rebase、push）後，用 `git log` 確認 ref 確實更新
- Branch 格式固定三段：`<類型>/<範圍>/<主題>`
  - 類型：`feature`（新功能、重構、文件、工具、調查）或 `fix`（單一 bug 修正，走簡化流程）
  - 範圍：只能從專案 `CLAUDE.md` 的範圍表選一個；跨多個範圍時選改動最多的；需要新範圍先問使用者
  - 主題：英文 kebab-case，2–4 個字；`plans/<主題>.md` 用同一個名稱
  - 例外：新 repo 還沒有範圍表時，第一個 branch 固定是 `feature/docs/repo-init`
- `plans/` 加入 `.gitignore`，不 commit；需要長期保留的決定寫進專案文件（DESIGN、TODO）和 PR 描述

## 角色

- **入口**：主 session 一律先載入 skill `route`（它的「與使用者互動」整個 session 都適用）：第一則訊息是 `/` 指令或 `wt` 指令時，載入後照原樣執行那個指令；其他訊息照 `route` 的表判斷情境、載入對應的 playbook；換成另一種工作時回到 `route` 的表。subagent 和 headless worker 不載入
- **規劃 session**：主 checkout，`/repo-plan` 和使用者討論、開 worktree；`/finish-worktree` 收尾
- **指揮 session**：worktree 中執行 `/feature-flow <主題>`，派 subagent、驗證、commit
- **subagent**：qa-planner、spec-reviewer、qa、developer、code-reviewer。只做被指派的事，依預算回報；共通規則見 skill `agent-common`
- **verifier**：worker 結束後由 hook 開（skill `verify-topic` fork），對照核准的 plan 在真實資料上判斷結果；指揮叫不到它

## 原則

- **先找根本原因，做長期最佳**：遇到問題先找出根本原因，再選長期最佳的做法，不挑最快讓症狀消失或讓眼前檢查通過的做法
  - 長期最佳的做法太大就拆成階段，不降級成短期解；每個階段做完都要能單獨停在那裡而不帶風險（安全、資料、對外不可逆、錯誤結果、日後被迫重寫）
  - 設計保持彈性與可延伸性：後續階段從預留的擴充點接上，不用推翻前一階段
  - 只繞過症狀的做法只能是明說的暫時措施，同時寫下根本原因和之後的正解
  - 一個做法撐不住時，回頭檢查原因，不往上疊補丁或加資源硬撐
- **兩次修法失敗就驗證前提**：兩個以上的修法建立在同一個前提上卻都失敗，先寫下那個前提、直接驗證它，再寫下一個修法
- **數字先解釋再用**：測到的數字（耗時、速度、成本、結果比例）在回報或據以行動前，先確認它量的是你以為的東西、是什麼限制了它，以及同一次測量裡有沒有不該出現的結果
- **能測就測，測決定和數字，不測像素**
- 一定要成立的規則用 hook、sandbox、settings 強制；說明文字只用來解釋
- 數值放設定檔或資源，不寫死
- 邏輯和表現分離；邏輯不依賴 UI／場景樹，時間來源以注入方式提供

## 流程規則在哪裡

| 內容 | 位置 |
|---|---|
| 情境判斷：入口、各情境的 playbook（問答、除錯、驗證、查證、改 home、直接修改）、共用操作；主 session 和使用者互動的規則 | skill `route`（「與使用者互動」、`playbooks/`、`ops/`） |
| 規劃：分類、計畫格式、討論紀錄、開局、worktree 交接、規劃時的踩坑 | skill `repo-plan` |
| 開發：各類別的步驟、派工與預算、必修 vs TODO、成本表與踩坑紀錄、收尾 | skill `feature-flow` |
| 收尾：整理成本與踩坑、經驗紀錄、移除 worktree | skill `finish-worktree` |
| subagent 共通：預算與求助、測試範圍、回報格式 | skill `agent-common` |
| 程式碼品質（需要判斷的部分：命名、重複、死碼、註解、錯誤處理）；數字門檻在 crap-gate | skill `clean-code`（developer、code-reviewer 預載） |
| 專案專屬：測試指令、受保護路徑、完成檢查 | 專案 skill `project-flow` |
| hook 的設計（agent-guard、`.hook-only/`、預算、消耗量） | `~/.claude/docs/` |
| PR 衝突：檢查、交給 worker 合併 | skill pr-conflict、feature-flow merge.md |
