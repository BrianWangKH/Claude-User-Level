# 全域規則

各專案的 `CLAUDE.md` 補充專案專屬的內容（環境、範圍表、程式碼慣例、各類別的具體範例），兩者不衝突時同時適用；專案明確寫出例外時，以專案為準。

這份檔案每個 session 和每個 subagent 都會載入，只放所有角色都需要的規則。各角色的流程放在它會載入的地方（見最後的索引）。

## 溝通

- 回覆使用者用繁體中文；commit、PR、程式碼註解用英文
- 執行使用者沒要求的指令前，先用一句話說明要做什麼

## 工具

- 讀檔用 Read（大檔用 offset／limit），修改用 Edit／Write；Bash 用來執行測試、git、腳本，以及搜尋
  - 原因：Bash 讀檔常被 sandbox 擋下（例如 `plans/`、`~/.claude`），失敗後要重試；複合的 `cd` 指令會觸發權限確認、中斷 auto mode；用 Bash 讀過的檔案 Edit 追蹤不到，要再讀一次
  - 過濾指令輸出可以用 Bash（例如 `<測試指令> | grep -E "..."`）
- 搜尋：有 Grep／Glob 工具就用（Windows、npm 版）；沒有時（Linux／macOS 原生版 v2.1.117 起移除，改由 Bash 內嵌 ugrep／bfs）用 Bash `grep`／`rg`／`find`
  - 一次一個指令，不用 `&&`、`;` 串接（複合指令會被權限或分類器擋下）
  - 控制輸出量：`grep -l`、`grep -m <n>`、`| head`；看檔案某段用 Read，不用 `sed -n`／`cat`
- Bash 指令不要用 `cd <路徑>;` 或 `cd <路徑> &&` 開頭：工作目錄已經是當前 checkout；需要其他目錄時用絕對路徑或 `git -C <路徑>`
- Bash 看不到也寫不進 `plans/`（sandbox）；plan 一律用 Read／Write 工具
- hook 的狀態在 checkout 根目錄的 `.hook-only/`，只有 hook 能寫；Read 和 Bash 都可以讀（`hook-read-only/` 子目錄除外）
- 例外：`~/.claude/skills/`、`~/.claude/templates/`、`~/.claude/docs/` 的檔案（skill 的參考檔、範本、文件）在工作目錄外，Read 讀不到，改用 Bash `cat`（sandbox 已開放讀取）

## Git

- 一律用明確路徑 `git add`，不用 `git add -A`／`git add .`
- 不自動 merge，PR 等人工審核
- 使用者在自己終端機做完 git 操作（fetch、rebase、push）後，用 `git log` 確認 ref 確實更新
- Branch 格式固定三段：`<類型>/<範圍>/<主題>`
  - 類型：`feature`（新功能、重構、文件、工具、調查）或 `fix`（單一 bug 修正，走簡化流程）
  - 範圍：只能從專案 `CLAUDE.md` 的範圍表選一個；跨多個範圍時選改動最多的；需要新範圍先問使用者
  - 主題：英文 kebab-case，2–4 個字；`plans/<主題>.md` 用同一個名稱
  - 例外：新 repo 還沒有範圍表時，第一個 branch 固定是 `feature/docs/repo-init`
- `plans/` 加入 `.gitignore`，不 commit；需要長期保留的決定寫進專案文件（DESIGN、TODO）和 PR 描述

## 角色

- **規劃 session**：主 checkout，`/repo-plan` 和使用者討論、開 worktree；`/finish-worktree` 收尾
- **指揮 session**：worktree 中執行 `/feature-flow <主題>`，派 subagent、驗證、commit
- **subagent**：qa-planner、spec-reviewer、qa、developer、code-reviewer。只做被指派的事，依預算回報；共通規則見 skill `agent-common`

## 原則

- **能測就測，測決定和數字，不測像素**
- 一定要成立的規則用 hook、sandbox、settings 強制；說明文字只用來解釋
- 數值放設定檔或資源，不寫死
- 邏輯和表現分離；邏輯不依賴 UI／場景樹，時間來源以注入方式提供

## 流程規則在哪裡

| 內容 | 位置 |
|---|---|
| 規劃：分類、計畫格式、討論紀錄、開局、worktree 交接、規劃時的踩坑 | skill `repo-plan` |
| 開發：各類別的步驟、派工與預算、必修 vs TODO、成本表與踩坑紀錄、收尾 | skill `feature-flow` |
| 收尾：整理成本與踩坑、經驗紀錄、移除 worktree | skill `finish-worktree` |
| subagent 共通：預算與求助、測試範圍、回報格式 | skill `agent-common` |
| 專案專屬：測試指令、受保護路徑、完成檢查 | 專案 skill `project-flow` |
| hook 的設計（agent-guard、`.hook-only/`、預算、消耗量） | `~/.claude/docs/` |
| PR 衝突：檢查、交給 worker 合併 | skill pr-conflict、feature-flow merge.md |
