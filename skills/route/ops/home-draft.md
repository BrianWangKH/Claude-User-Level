# Operation: draft a ~/.claude change as full files

1. Directory: `$TMPDIR/home-apply-<topic>/` with
   - `orig/<path>`: the current file, copied with `cat` (same layout as `~/.claude`)
   - `new/<path>`: the complete new file, never a fragment
   - `changes.diff`: `diff -ruN orig new` (new files show up against nothing)
   No commit-message file: the message goes inline in the commit command (step 4).
2. `settings.json` can't be read: give the exact JSON block and where it goes instead of a file.
3. Show the user the diff summary (files, what changes, why).
4. Apply steps in the reply, one command per line, no wrapper script:
   - `mkdir -p ~/.claude/<dir>` for new directories
   - `cp /tmp/claude-1000/home-apply-<topic>/new/<path> ~/.claude/<path>` per file. Commands the user runs in an outside terminal always use the absolute path: `$TMPDIR` is set only inside the sandbox and is usually empty outside it
   - one `git -C ~/.claude add <paths>` with every changed path
   - one `git -C ~/.claude commit -F - <<'EOF'` block with the English message inline, ending with the attribution line
5. Files the user keeps outside this home's exported tree (an incident log, if your memory names one) are the exception: append to them yourself, only add, never edit.
