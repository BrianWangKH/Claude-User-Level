#!/usr/bin/env bash
# Check that a feature worktree can be removed: merged into origin's default branch and clean.
# Without --remove it only checks. The sandbox can't remove a worktree (its plans/ is a sandbox
# mount), so the finish-worktree hook re-runs this script outside the sandbox with --remove,
# which repeats every check before removing the worktree and deleting the branch.
# Run from the main checkout, after a fresh fetch (done by the fetch-origin hook).
# Usage: bash finish-worktree.sh <topic | type/scope/topic> [--remove]
# Exit: 0 ready / removed, 1 not ready or removal failed, 2 usage error.
set -u

target=${1:-}
remove=${2:-}
if [ -z "${target}" ] || { [ -n "${remove}" ] && [ "${remove}" != "--remove" ]; }; then
	echo "usage: bash finish-worktree.sh <topic | type/scope/topic> [--remove]" >&2;
	exit 2;
fi

fail() {
	echo "FAIL: $*" >&2;
	exit 1;
}

gitDir=$(git rev-parse --absolute-git-dir 2>/dev/null) || fail "not inside a git repo";
commonDir=$(git rev-parse --path-format=absolute --git-common-dir);
[ "${gitDir}" = "${commonDir}" ] || fail "run this from the main checkout, not a worktree";
mainRoot=$(dirname "${commonDir}");

statusFile="${commonDir}/claude-fetch-status";
[ -f "${statusFile}" ] || fail "no fetch status; invoke the skill again so the fetch hook runs";
read -r fetchedAt fetchResult fetchDetail < "${statusFile}";
[ "${fetchResult}" = "ok" ] || fail "last fetch failed: ${fetchDetail:-unknown}";
[ $(( $(date +%s) - fetchedAt )) -le 600 ] || fail "last fetch is older than 10 minutes; invoke the skill again";

path="";
branch="";
current="";
while IFS= read -r line; do
	case "${line}" in
		"worktree "*) current=${line#worktree };;
		"branch refs/heads/"*)
			candidate=${line#branch refs/heads/};
			if [ "${candidate}" = "${target}" ] || [ "${candidate##*/}" = "${target}" ]; then
				path=${current};
				branch=${candidate};
			fi;;
	esac
done < <(git worktree list --porcelain);
[ -n "${path}" ] || fail "no worktree for ${target}";
[ "${path}" != "${mainRoot}" ] || fail "${target} is the main checkout";

base=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null);
if [ -z "${base}" ]; then
	for candidate in origin/master origin/main; do
		git rev-parse --verify --quiet "${candidate}" >/dev/null && base=${candidate} && break;
	done
fi
[ -n "${base}" ] || fail "cannot find origin's default branch";
git merge-base --is-ancestor "${branch}" "${base}" || fail "${branch} is not merged into ${base} yet";

# Untracked PR descriptions are expected leftovers; anything else would be lost.
leftovers=$(git -C "${path}" status --porcelain --untracked-files=all | grep -v -E '^\?\? PR_DESCRIPTION[^/]*\.md$');
if [ -n "${leftovers}" ]; then
	echo "${leftovers}";
	fail "${path} has uncommitted changes or untracked files";
fi

if [ -z "${remove}" ]; then
	echo "OK: ${branch} is merged into ${base} and ${path} is clean.";
	echo "The finish-worktree hook removes it next. If no hook result follows, run in your terminal:";
	echo "git -C ${mainRoot} worktree remove --force ${path} && git -C ${mainRoot} branch -D ${branch}";
	exit 0;
fi

# Inside the sandbox, plans/ (and similar) are mount points that can't be deleted, and a failed
# `git worktree remove` still unregisters the worktree, leaving a half-removed directory.
# Refuse before touching anything if any mount point sits inside the worktree.
if awk -v prefix="${path}/" 'index($5, prefix) == 1 { found = 1 } END { exit !found }' /proc/self/mountinfo 2>/dev/null; then
	fail "${path} contains sandbox mount points; removal must run outside the sandbox (the finish-worktree hook does this)";
fi

git -C "${mainRoot}" worktree remove --force "${path}" || fail "git worktree remove failed for ${path}";
git -C "${mainRoot}" branch -D "${branch}" >/dev/null || fail "worktree removed, but deleting branch ${branch} failed";
echo "REMOVED: worktree ${path} and branch ${branch}";
