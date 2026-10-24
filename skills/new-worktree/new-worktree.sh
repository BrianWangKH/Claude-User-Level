#!/usr/bin/env bash
# Create a feature branch and its worktree at <parent of main repo>/worktrees/<topic>,
# based on origin's default branch. Run from the main checkout, after a fresh fetch
# (the fetch-origin hook fetches when this skill is invoked).
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

statusFile="${commonDir}/claude-fetch-status";
[ -f "${statusFile}" ] || fail "no fetch status; invoke the skill again so the fetch hook runs";
read -r fetchedAt fetchResult fetchDetail < "${statusFile}";
[ "${fetchResult}" = "ok" ] || fail "last fetch failed: ${fetchDetail:-unknown}";
[ $(( $(date +%s) - fetchedAt )) -le 600 ] || fail "last fetch is older than 10 minutes; invoke the skill again";

base=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null);
if [ -z "${base}" ]; then
	for candidate in origin/master origin/main; do
		git rev-parse --verify --quiet "${candidate}" >/dev/null && base=${candidate} && break;
	done
fi
[ -n "${base}" ] || fail "cannot find origin's default branch";

mainRoot=$(dirname "${commonDir}");
worktreesDir="$(dirname "${mainRoot}")/worktrees";
path="${worktreesDir}/${topic}";

[ -d "${worktreesDir}" ] || fail "${worktreesDir} does not exist; ask the user to create it (the sandbox can't)";
[ ! -e "${path}" ] || fail "${path} already exists";
git rev-parse --verify --quiet "refs/heads/${branch}" >/dev/null && fail "branch ${branch} already exists locally";
git rev-parse --verify --quiet "refs/remotes/origin/${branch}" >/dev/null && fail "branch ${branch} already exists on origin";

# --no-track: no upstream (a branch tracking origin/master could push to the wrong place,
# and writing the upstream needs .git/config, which the sandbox blocks).
git worktree add --quiet --no-track -b "${branch}" "${path}" "${base}" || fail "git worktree add failed";

echo "OK: created ${branch} from ${base} ($(git rev-parse --short "${base}"))";
echo "worktree: ${path}";
echo "plan:     ${path}/plans/${topic}.md";
echo "start:    /feature-flow ${topic}";
