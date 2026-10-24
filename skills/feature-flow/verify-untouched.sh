#!/usr/bin/env bash
# Verify that protected paths are unchanged since a given commit.
# Covers committed and uncommitted edits (git diff) and new untracked files (git status).
# Usage: bash verify-untouched.sh <commit> <path>...
# Exit: 0 unchanged, 1 changed, 2 usage error.
set -u

if [ $# -lt 2 ]; then
	echo "usage: bash verify-untouched.sh <commit> <path>..." >&2;
	exit 2;
fi

commit=$1;
shift;

if ! git rev-parse --verify --quiet "${commit}^{commit}" >/dev/null; then
	echo "unknown commit: ${commit}" >&2;
	exit 2;
fi

changes=$(git diff --stat "${commit}" -- "$@"; git status --short --untracked-files=all -- "$@");

if [ -n "${changes}" ]; then
	echo "${changes}";
	echo "FAIL: protected paths changed since ${commit}: $*" >&2;
	exit 1;
fi

echo "OK: protected paths unchanged since ${commit}: $*";
