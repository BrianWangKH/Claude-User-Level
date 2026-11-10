#!/usr/bin/env bash
# Create a new repo for the flow from the kit beside this script (templates/new-repo/):
#   bash ~/.claude/templates/new-repo.sh <name> --lang go|python [--parent <dir>]
# In the working directory (or --parent) it makes <name>-Workforest/<name>/ and <name>-Workforest/worktrees/;
# the rules, refusals and checks are in hooks/tools/new-repo.py, which this runs.
set -euo pipefail
here=$(dirname "$(readlink -f "$0")")
exec python3 "$here/../hooks/tools/new-repo.py" "$@"
