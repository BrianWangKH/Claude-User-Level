# Claude Code Dev Flow

一套 Claude Code 的 home 設定（`~/.claude`），把 Claude Code 變成一個測試先行的小型開發團隊；必須永遠成立的規則由 hook 強制，不靠提示詞。

[English](README.md)

## 功能

- **規劃與開發分開。** 你在互動 session 裡和 Claude 談定計畫；headless worker 在自己的 git worktree 裡實作。
- **測試先行的 subagent 團隊。** 測試規劃、規格審查、QA、開發、看不到計畫的 code reviewer，各自只做自己的工作。
- **hook 強制規則。** guard 擋下越權的修改，quality gate 檢查複雜度、覆蓋率和 mutation 結果，預算機制防止失控的執行。
- **真實資料驗證。** 完成的 branch 要先通過腳本檢查和獨立的 verifier agent，才能合併。
- **免看管的本地模式。** 完成的 topic 自動驗證、合併進本地 `main` 並收尾；佇列中的 topic 依序開始。
- **Worker pane。** 側邊面板顯示每個 worker 的進度、花費和待你決定的事，可以啟動、續跑、停止。

## 運作方式

```
/repo-plan（你 + 規劃 session）
    │  寫出計畫、估算和真實資料檢查
    ▼
wt launch / wt queue
    │
worker（headless claude，獨立 worktree）
    │  qa-planner → spec-reviewer → qa → developer → code-reviewer
    │  hook：guard、quality gate、預算
    ▼
STATUS: done
    │
verify.py（腳本檢查）→ verifier agent（真實資料上的判斷）
    │
自動合併進本地 main → 收尾 → 下一個佇列中的 topic
```

需要人決定時，worker 會停下來通知你；你在規劃 session 回答後再讓它續跑。

## 需求

- Linux 或 WSL2
- [Claude Code](https://docs.claude.com/en/docs/claude-code)，已登入帳號
- Python 3.12 與 [uv](https://docs.astral.sh/uv/)
- git
- bubblewrap（`bwrap`），用來在沒有網路的環境跑 quality gate
- Go 工具鏈，只有 Go repo 需要

## 安裝

1. 備份你現有的 `~/.claude`。
2. 把這個 repository clone 成 `~/.claude`。內附的 `settings.json` 會註冊所有 hook、載入 worker pane 外掛並設定 sandbox；請依你的機器檢查其中的 `sandbox` 和 `permissions`。
3. 把個人設定值寫進 `~/.claude/config/local.json`（見[設定](#設定)），個人規則（例如回覆語言）寫成 `~/.claude/rules/` 底下的檔案。兩者都不進 git。
4. 檢查設定：

   ```bash
   python3 ~/.claude/config/homeconfig.py check
   ```

## 快速開始

1. 用範本建立 repository：

   ```bash
   bash ~/.claude/templates/new-repo.sh my-app --lang python
   ```

   它會建立主 checkout 和同層的 `worktrees/` 目錄，commit 一份骨架，並開啟本地模式。
2. 在主 checkout 開啟 Claude Code，輸入 `/repo-plan`，談到第一個 topic 的計畫定案。
3. 用 `wt queue <topic>`（或 `wt launch <topic>`）核准並執行。
4. 用 `wt pane` 或 `wt status` 看進度。所有檢查都通過時，topic 會自動合併進 `main`。

## 使用方式

`wt` 指令由 hook 處理，不消耗模型回合。

| 指令 | 作用 |
|---|---|
| `/repo-plan` | 規劃 topic、建立 repository、查看進度 |
| `wt queue [<topic> ...]` | 把 topic 排入佇列並啟動可以跑的；不帶參數則顯示佇列 |
| `wt queue drop <topic>` | 移除還沒開始的 topic |
| `wt launch <topic>` | 不經佇列，直接為 topic 啟動 worker |
| `wt resume <topic>` | 做完決定或停止後讓 topic 續跑 |
| `wt stop <topic>` | 停止 worker |
| `wt status [<topic>]` | 顯示每一輪正在做什麼 |
| `wt merge <topic>` | 核准等待人工的合併 |
| `wt wrapup <topic>` | 完成沒做完的收尾 |
| `wt reconcile [--dry-run]` | 找出並補完欠著的收尾和後續執行 |
| `wt pane` | 開關 worker pane |
| `wt auto [on\|off]` | 規劃完自動啟動或續跑 worker（只限這個 session） |
| `wt local [on\|off]` | 切換本地模式與 PR 模式 |
| `/pr-conflict <topic>` | PR 模式：把有衝突的 PR 交給 worker 合併 |
| `/finish-worktree <topic>` | PR 模式：PR 合併後收尾 |
| `/feature-flow <topic>` | 在 worktree 裡手動執行開發流程 |

### 本地模式與 PR 模式

| | 本地模式（預設） | PR 模式 |
|---|---|---|
| 以哪裡為準 | 本地 `main` | `origin` 的預設 branch |
| 合併 | 所有條件成立時自動合併 | 你自己 push 並開 PR |
| 收尾 | 自動 | PR 合併後執行 `/finish-worktree` |

topic 只在以下條件全部成立時自動合併：沒有未處理的 guard 警報、branch 已包含最新的 `main`、沒有改到受保護的路徑（設定、CI、相依套件檔）、code review 已核准、所有測試與檢查通過、兩層真實資料檢查都通過。否則會通知你，由你用 `wt merge` 決定。

## 設定

所有設定都經由 `config/homeconfig.py` 讀取。

| 檔案 | 用途 |
|---|---|
| `config/home.json` | 每個值的預設，每個鍵旁邊附說明 |
| `config/local.json` | 你的覆寫（不進 git）；不合法的值退回預設並警告 |
| `<repo>/.claude/quality-gates.json` | 各 repo 的測試、覆蓋率、lint 指令；預設來自語言 profile |
| `<repo>/.claude/flow.json` | 各 repo 的佇列上限 |
| `<repo>/.claude/guard-paths.json` | 哪些路徑算測試 |

覆寫範例：

```json
{ "budget": { "budget_headroom_factor": 1.5 } }
```

讀取單一生效值：`python3 ~/.claude/config/homeconfig.py get <section> <key>`。

## 目錄結構

```
~/.claude/
├── CLAUDE.md          每個 session 都會載入的全域規則
├── settings.json      hook 註冊、sandbox、權限
├── agents/            subagent 定義
├── skills/            流程 skill（規劃、開發、分流、審查、收尾）
├── hooks/
│   ├── hook-entry.py  所有 hook 的單一入口
│   ├── guards/        允許或擋下工具呼叫
│   ├── gates/         quality gate 與真實資料檢查
│   ├── budget/        時間、花費與額度追蹤
│   ├── worker/        worker 生命週期與 wt 指令
│   ├── lib/           共用模組
│   ├── tools/         輔助腳本（列出 hook 入口、建立新 repo）
│   └── tests/         測試
├── config/            設定讀取器與預設值
├── templates/         新 repository 範本與設定範本
└── mods/wt-pane/      worker pane 外掛
```

## 測試

```bash
uv run --no-project --with pytest --with pytest-cov --with pytest-xdist \
  python -m pytest ~/.claude/hooks/tests -n auto
```

## 疑難排解

| 症狀 | 解法 |
|---|---|
| `wt ...` 沒反應 | hook 沒註冊；檢查 `settings.json` 的 `UserPromptSubmit` |
| worker 連不到某個網域 | 把網域加進 repository 的 `.claude/settings.json` sandbox 允許清單，再 `wt resume` |
| worker 找不到工具 | 在 `config/local.json` 的 `toolchains.tools` 設定工具路徑 |
| 某一輪顯示為 orphan | 先 `wt status <topic>`，再 `wt resume <topic>` |
| gate 顯示 `bwrap unavailable` | 執行 `bwrap --ro-bind / / --unshare-net true` 查看原因 |
| 改了 `quality-gates.json` 沒效果 | gate 從 `main` 讀取，先合併 |
