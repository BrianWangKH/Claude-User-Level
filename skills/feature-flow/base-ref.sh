#!/usr/bin/env bash
# Print the integration branch: what feature branches start from, merge into and are compared with.
# FLOW_INTEGRATION=local (the repo's .claude/settings.json env; agents can't change it): the local
# default branch, main or else master; the local branch is the source of truth and origin is an
# occasional backup push. Otherwise origin's default branch (origin/HEAD, else origin/master, else
# origin/main), the PR flow. qg_common.integrationRef applies the same rule for the Python hooks.
# Usage: bash base-ref.sh   Exit: 0 printed, 1 none found.
set -u
if [ "${FLOW_INTEGRATION:-}" = "local" ]; then
	for candidate in main master; do
		git rev-parse --verify --quiet "refs/heads/${candidate}" >/dev/null && echo "${candidate}" && exit 0;
	done
	echo "no local main or master branch" >&2;
	exit 1;
fi
base=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null);
if [ -z "${base}" ]; then
	for candidate in origin/master origin/main; do
		git rev-parse --verify --quiet "${candidate}" >/dev/null && base=${candidate} && break;
	done
fi
[ -n "${base}" ] || { echo "cannot find origin's default branch" >&2; exit 1; };
echo "${base}";
