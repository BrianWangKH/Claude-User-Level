"""PreToolUse(Bash) [every session, every agent; homeDeletes: home-drafter and home-verifier only]: no Bash command
that starts with `cd <path> &&` or `cd <path>;` (check), and no Bash command of a home agent that deletes
(homeDeletes).

check: global CLAUDE.md "Tools" says so: the working directory is already the checkout, and a compound `cd`
triggers a permission prompt, which stops auto mode and headless workers (seen while
the rule existed as text and agents kept breaking it). A lone `cd <path>` and `cd` later in a command are
left alone. The denial names the alternatives.

homeDeletes: skill home-draft SAFETY 3 (delete nothing) as a hook: the commands rm, rmdir, unlink and shred
anywhere a command word can stand (also after a path, `xargs`, `find -exec`), find's -delete and rsync's
--delete options, and inline Python deletions (shutil.rmtree, os.remove, os.unlink, os.rmdir, os.removedirs,
Path.unlink). The denial gives the alternative of the skill: move to <draft>/trash/<timestamp>/, leave a file out of
a fresh overlay to test its absence. It errs on the side of refusing: a word `rm` in an echo or `git rm` is refused
too. It is a reminder for the common spellings, not a boundary: a list of strings over shell syntax can't be
complete. Known limit, not seen: a backslash-escaped command word (`\\rm`), a word split by quotes (`r''m`),
a command in a variable or eval (`$c x`, `eval "r""m x"`), and a deletion written into a file that is then run (a
.py or .sh script). The skill's rule and the sealed, read-only round evidence cover them.

Reads: the hook input (agent_type: hook_entries.py runs homeDeletes only for the home agents).
Writes: nothing.
Output: check(data) and homeDeletes(data) return the PreToolUse deny, or None.
Fails: closed — steps of hook-entry.py's `pre` entry: an exception denies the call (hook-entry.py).
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import hookio

CD_PREFIX = re.compile(r"""^\s*cd\s+("[^"]*"|'[^']*'|[^\s;&|]+)\s*(&&|;)""")
# A delete command where a command word can stand: after the start, a separator, a quote, a subshell, or a path.
DELETE_COMMAND = re.compile(r"""(?:^|[\s;&|(`{'"]|\$\()(?:[^\s;&|(`{'"]*/)?(rm|rmdir|unlink|shred)(?=$|[\s;&|)`}'"])""")
DELETE_OPTION = re.compile(r"""(?:^|\s)(-delete|--delete(?:-[a-z]+)?)(?=$|[\s;&|)])""")
DELETE_PYTHON = re.compile(r"""\b(rmtree|removedirs)\b|\bos\.(remove|unlink|rmdir)\b|\.unlink\s*\(""")
HOME_DELETE_ALTERNATIVE = (
	"(skill home-draft SAFETY 3: delete nothing). To remove a file or directory: `mv` it to "
	"<draft>/trash/<timestamp>/. To test a missing file: build a fresh overlay that leaves it out, never delete "
	"from an existing one. Scratch files stay in your own draft directory."
)


def check(data):
	if data.get("hook_event_name") != "PreToolUse" or data.get("tool_name") != "Bash":
		return None
	command = (data.get("tool_input") or {}).get("command") or ""
	match = CD_PREFIX.match(command)
	if not match:
		return None
	return hookio.refuse("PreToolUse", f"bash-guard: don't start a Bash command with `cd {match.group(1)} "
		f"{match.group(2)}` (global CLAUDE.md, Tools): the working directory is already the checkout. Use "
		"absolute paths, `git -C <path>`, a tool's own directory flag (`go -C`, `uv --directory`), or "
		"`env -C <path> <command>` for a tool that needs its working directory.")


def deletion(command):
	"""The deleting word of a command (module doc, homeDeletes), None when it deletes nothing that is seen."""
	for pattern in (DELETE_COMMAND, DELETE_OPTION, DELETE_PYTHON):
		match = pattern.search(command)
		if match:
			return next((g for g in match.groups() if g), match.group(0))
	return None


def homeDeletes(data):
	"""A home agent's Bash command that deletes is refused with the alternative (module doc)."""
	if data.get("hook_event_name") != "PreToolUse" or data.get("tool_name") != "Bash":
		return None
	found = deletion((data.get("tool_input") or {}).get("command") or "")
	if not found:
		return None
	return hookio.refuse("PreToolUse", f"bash-guard: `{found.strip()}` deletes {HOME_DELETE_ALTERNATIVE}")
