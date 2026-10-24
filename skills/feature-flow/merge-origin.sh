#!/usr/bin/env bash
# Check that this worktree can merge origin's default branch. The sandbox can't run the merge
# itself (git can't rewrite files under protected paths such as .claude/skills), so the
# merge-origin hook re-runs this script outside the sandbox with --merge, which repeats every
# check and then runs `git merge --no-ff --no-commit`. Uses the last fetch (skill pr-conflict).
# Usage: bash merge-origin.sh [--merge]
# Exit: 0 ready / merged (conflicts are a normal result), 1 not ready or merge error, 2 usage error.
set -u

mode=${1:-}
if [ -n "${mode}" ] && [ "${mode}" != "--merge" ]; then
	echo "usage: bash merge-origin.sh [--merge]" >&2;
	exit 2;
fi

fail() {
	echo "FAIL: $*" >&2;
	exit 1;
}

gitDir=$(git rev-parse --absolute-git-dir 2>/dev/null) || fail "not inside a git repo";
commonDir=$(git rev-parse --path-format=absolute --git-common-dir);
[ "${gitDir}" != "${commonDir}" ] || fail "run this in a feature worktree, not the main checkout";
branch=$(git symbolic-ref --quiet --short HEAD) || fail "HEAD is detached";

base=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null);
if [ -z "${base}" ]; then
	for candidate in origin/master origin/main; do
		git rev-parse --verify --quiet "${candidate}" >/dev/null && base=${candidate} && break;
	done
fi
[ -n "${base}" ] || fail "cannot find origin's default branch";

[ ! -e "${gitDir}/MERGE_HEAD" ] || fail "a merge is already in progress; resolve or abort it first";
# Untracked PR descriptions are expected; anything else could be overwritten or lost by the merge.
leftovers=$(git status --porcelain --untracked-files=all | grep -v -E '^\?\? PR_DESCRIPTION[^/]*\.md$');
if [ -n "${leftovers}" ]; then
	echo "${leftovers}";
	fail "working tree is not clean; commit or remove these first";
fi
if git merge-base --is-ancestor "${base}" HEAD; then
	echo "OK: ${base} is already in ${branch}; nothing to merge.";
	exit 0;
fi

if [ -z "${mode}" ]; then
	echo "READY: ${branch} can merge ${base} ($(git rev-parse --short "${base}")).";
	echo "The merge-origin hook runs the merge next; its result arrives as additional context.";
	exit 0;
fi

# Outside the sandbox: never run the repo's own git hooks here.
git -c core.hooksPath=/dev/null merge --no-ff --no-commit "${base}";
status=$?;
conflicts=$(git diff --name-only --diff-filter=U);
if [ -n "${conflicts}" ]; then
	echo "MERGED WITH CONFLICTS: ${base} into ${branch}; resolve these, then commit:";
	echo "${conflicts}" | sed 's/^/  /';
	exit 0;
fi
[ "${status}" -eq 0 ] || fail "git merge failed (exit ${status})";
echo "MERGED: ${base} into ${branch} without conflicts (not committed yet).";
