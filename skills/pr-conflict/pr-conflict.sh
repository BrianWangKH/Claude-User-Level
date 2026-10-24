#!/usr/bin/env bash
# Check whether a topic's branch conflicts with origin's default branch, without touching any
# working tree (git merge-tree). Run from the main checkout after a fresh fetch (done by the
# fetch-origin hook when skill pr-conflict is invoked). In a worktree, pass --local to skip the
# fetch check and use whatever origin/<default> was fetched last (feature-flow wrap-up).
# Usage: bash pr-conflict.sh <topic | type/scope/topic> [--local]
# Exit: 0 when the check ran (first word OK / MERGED / CONFLICT, files listed on CONFLICT),
# 1 error, 2 usage error. A conflict is a normal result, so it doesn't show as a failed command.
set -u

target=${1:-}
mode=${2:-}
if [ -z "${target}" ] || { [ -n "${mode}" ] && [ "${mode}" != "--local" ]; }; then
	echo "usage: bash pr-conflict.sh <topic | type/scope/topic> [--local]" >&2;
	exit 2;
fi

fail() {
	echo "FAIL: $*" >&2;
	exit 1;
}

gitDir=$(git rev-parse --absolute-git-dir 2>/dev/null) || fail "not inside a git repo";
commonDir=$(git rev-parse --path-format=absolute --git-common-dir);

if [ -z "${mode}" ]; then
	[ "${gitDir}" = "${commonDir}" ] || fail "run this from the main checkout, or pass --local in a worktree";
	statusFile="${commonDir}/claude-fetch-status";
	[ -f "${statusFile}" ] || fail "no fetch status; invoke the skill again so the fetch hook runs";
	read -r fetchedAt fetchResult fetchDetail < "${statusFile}";
	[ "${fetchResult}" = "ok" ] || fail "last fetch failed: ${fetchDetail:-unknown}";
	[ $(( $(date +%s) - fetchedAt )) -le 600 ] || fail "last fetch is older than 10 minutes; invoke the skill again";
fi

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

base=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null);
if [ -z "${base}" ]; then
	for candidate in origin/master origin/main; do
		git rev-parse --verify --quiet "${candidate}" >/dev/null && base=${candidate} && break;
	done
fi
[ -n "${base}" ] || fail "cannot find origin's default branch";

if git merge-base --is-ancestor "${branch}" "${base}"; then
	echo "MERGED: ${branch} is contained in ${base} (merged, or no commits yet); nothing to resolve.";
	exit 0;
fi
if git merge-base --is-ancestor "${base}" "${branch}"; then
	echo "OK: ${base} has not moved since ${branch} last merged it; no conflict.";
	exit 0;
fi

if [ -n "$(git -C "${path}" status --porcelain --untracked-files=no)" ]; then
	echo "WARN: ${path} has uncommitted changes; they are not part of this check.";
fi

output=$(git merge-tree --write-tree --name-only --no-messages "${base}" "${branch}");
status=$?;
case "${status}" in
	0)
		echo "OK: ${branch} merges cleanly into ${base}; GitHub can merge it, no merge commit needed.";
		exit 0;;
	1)
		echo "CONFLICT: ${branch} vs ${base} ($(git rev-parse --short "${base}"))";
		echo "worktree: ${path}";
		echo "files:";
		echo "${output}" | tail -n +2 | sed 's/^/  /';
		exit 0;;
	*)
		fail "git merge-tree failed (exit ${status})";;
esac
