#!/usr/bin/env bash
# Create a feature branch and its worktree at <parent of main repo>/worktrees/<topic>,
# based on the integration branch (base-ref.sh: origin's default branch, or the local main in local
# mode). Run from the main checkout, after a fresh fetch (the fetch-origin hook fetches when this skill
# is invoked; local mode needs none).
# Usage: bash new-worktree.sh <type>/<scope>/<topic>
# Exit: 0 created, 1 precondition failed, 2 usage error.
set -u

branch=${1:-}
if ! [[ "${branch}" =~ ^(feature|fix)/[a-z0-9]+/[a-z0-9]+(-[a-z0-9]+){1,3}$ ]]; then
	echo "usage: bash new-worktree.sh <feature|fix>/<scope>/<topic> (topic: 2-4 kebab-case words)" >&2;
	exit 2;
fi
topic=${branch##*/};

fail() {
	echo "FAIL: $*" >&2;
	exit 1;
}

gitDir=$(git rev-parse --absolute-git-dir 2>/dev/null) || fail "not inside a git repo";
commonDir=$(git rev-parse --path-format=absolute --git-common-dir);
[ "${gitDir}" = "${commonDir}" ] || fail "run this from the main checkout, not a worktree";

# Local mode (FLOW_INTEGRATION=local) compares with the local default branch: no fetch needed.
if [ "${FLOW_INTEGRATION:-}" != "local" ]; then
	statusFile="${commonDir}/claude-fetch-status";
	[ -f "${statusFile}" ] || fail "no fetch status; invoke the skill again so the fetch hook runs";
	read -r fetchedAt fetchResult fetchDetail < "${statusFile}";
	[ "${fetchResult}" = "ok" ] || fail "last fetch failed: ${fetchDetail:-unknown}";
	[ $(( $(date +%s) - fetchedAt )) -le 600 ] || fail "last fetch is older than 10 minutes; invoke the skill again";
fi

base=$(bash ~/.claude/skills/feature-flow/base-ref.sh) || fail "cannot find the integration branch (base-ref.sh)";

mainRoot=$(dirname "${commonDir}");
worktreesDir="$(dirname "${mainRoot}")/worktrees";
path="${worktreesDir}/${topic}";

[ -d "${worktreesDir}" ] || fail "${worktreesDir} does not exist; ask the user to create it (the sandbox can't)";
[ ! -e "${path}" ] || fail "${path} already exists";
git rev-parse --verify --quiet "refs/heads/${branch}" >/dev/null && fail "branch ${branch} already exists locally (finish-worktree keeps merged branches; if it is merged into ${base}, delete it in your terminal: git branch -D ${branch})";
git rev-parse --verify --quiet "refs/remotes/origin/${branch}" >/dev/null && fail "branch ${branch} already exists on origin";

# --no-track: no upstream (a branch tracking origin/master could push to the wrong place,
# and writing the upstream needs .git/config, which the sandbox blocks).
git worktree add --quiet --no-track -b "${branch}" "${path}" "${base}" || fail "git worktree add failed";

# The worktree session can't read the main checkout: give it a copy of the cost history for its
# estimates (cost-history.py estimate agent).
python3 ~/.claude/skills/repo-plan/cost-history.py snapshot "${path}" || fail "cost history copy failed";

echo "OK: created ${branch} from ${base} ($(git rev-parse --short "${base}"))";
echo "worktree: ${path}";
echo "plan:     ${path}/plans/${topic}.md";
echo "start:    /feature-flow ${topic}";
