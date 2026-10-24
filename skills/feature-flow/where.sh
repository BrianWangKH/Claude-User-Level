#!/usr/bin/env bash
# Print whether the current directory is the main checkout or a linked git worktree.
# Used by skill feature-flow through an inline !`...` block, so the command itself stays
# a plain `bash <path>` that permission checks can analyse.
gitDir=$(git rev-parse --absolute-git-dir 2>/dev/null) || { echo "not a git repo"; exit 0; }
commonDir=$(git rev-parse --path-format=absolute --git-common-dir);
if [ "${gitDir}" = "${commonDir}" ]; then
	echo "main checkout";
else
	echo "worktree";
fi
