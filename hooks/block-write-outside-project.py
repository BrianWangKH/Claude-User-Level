#!/usr/bin/env python3
"""PreToolUse guard: deny Write/Edit/NotebookEdit outside allowed directories."""
import json
import os
import sys
import subprocess

def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def is_under(target: str, base: str) -> bool:
    return target == base or target.startswith(base + os.sep)


def linked_worktrees(root: str) -> list:
    """Linked worktrees of root, only when root is the main checkout."""
    def git(*args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True, timeout=5).stdout.strip()

    try:
        if os.path.realpath(git("rev-parse", "--absolute-git-dir")) != os.path.realpath(git("rev-parse", "--path-format=absolute", "--git-common-dir")):
            return []
        listing = git("worktree", "list", "--porcelain")
    except (subprocess.SubprocessError, OSError):
        return []
    paths = [os.path.realpath(line[len("worktree "):]) for line in listing.splitlines() if line.startswith("worktree ")]
    return [p for p in paths if p != root]


def main() -> None:
    data = json.load(sys.stdin)  # 解析失敗會非零退出，onFailure: block 擋下
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        sys.exit(0)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir:
        deny("CLAUDE_PROJECT_DIR not set; refusing write")

    home = os.path.expanduser("~")
    root = os.path.realpath(project_dir)

    allowed = [
        root,
        os.path.realpath(os.path.join(home, ".claude", "plans")),
        os.path.realpath(os.path.join(home, ".claude", "agent-memory")),
    ]
    allowed += linked_worktrees(root)
    scratchpad = data.get("scratchpad_dir")
    if scratchpad:
        allowed.append(os.path.realpath(scratchpad))

    raw = path if os.path.isabs(path) else os.path.join(root, path)
    target = os.path.realpath(raw)

    if any(is_under(target, a) for a in allowed):
        sys.exit(0)  # 不做決定，交回正常 permission 流程

    # auto memory：只放行 ~/.claude/projects/<slug>/memory/**/*.md
    mem_base = os.path.realpath(os.path.join(home, ".claude", "projects"))
    if is_under(target, mem_base):
        parts = os.path.relpath(target, mem_base).split(os.sep)
        if len(parts) >= 3 and parts[1] == "memory" and target.endswith(".md"):
            sys.exit(0)

    deny(f"Write outside allowed directories blocked: {target}")


if __name__ == "__main__":
    main()