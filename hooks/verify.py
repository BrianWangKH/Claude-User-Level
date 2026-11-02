#!/usr/bin/env python3
"""Real-data checks after a worker run, by the hook, never by the orchestrator.

Usage: verify.py <worktree> <topic> <run>   (run-worker.sh, after a run that ended `STATUS: done`)
The checks come from <main>/plans/verify/<topic>.json, which the planning session writes at hand-off
(skills/repo-plan/handoff.md; template ~/.claude/templates/verify.json). It sits in the main checkout,
where the worker can't write, so the worker can't loosen its own acceptance test. No spec: nothing to do.

Tests run on fixtures; what they can't show is the branch's behaviour on the real data, which lives
in the main checkout the worker can't read. So: copy the spec's data_paths of <main>/<data> into a
scratch DATA_ROOT, then run every check's command from the worktree in the sandbox
(qg_common.runCommand: bwrap, no network, the worktree read-only, only the scratch writable), and judge
it by what the hook sees:
  - "exit": the expected exit code (default 0)
  - "output_contains": strings the output tail must contain (an error message, a summary line)
  - "repeat_same": globs under the scratch DATA_ROOT; the command runs twice and every matching file
    must have the same sha256 both times (deterministic output)
  - "unchanged": globs under the scratch DATA_ROOT that must list and hash the same before and after
    (an error path that must not write)
"files" in the spec are written into {tmp} first (a broken config copy, a small script). Placeholders
in "run", "env" and "files": {repo} the worktree, {data} the scratch DATA_ROOT, {tmp} a scratch dir.
DATA_ROOT is always set to {data}. A value judgement the hook can't make stays out of the spec.

Result: <worktree>/.hook-only/runs/verify-<run>.json {"passed", "checks": [{"name", "ok", "why",
"exit", "tail"}]}; run-result.py prints it, and in local mode auto-merge.py merges only on "passed".
The scratch copy is removed afterwards; nothing touches <main>/<data>. Fails closed: an unreadable
spec, a missing data path or no sandbox is a failed result, never a pass.
"""
import glob
import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qg_common  # noqa: E402

DEFAULT_TIMEOUT = 1500


def specPath(mainDir, topic):
	return os.path.join(mainDir, "plans", "verify", f"{topic}.json")


def fill(value, mapping):
	if isinstance(value, list):
		return [fill(v, mapping) for v in value]
	if isinstance(value, dict):
		return {k: fill(v, mapping) for k, v in value.items()}
	return str(value).format(**mapping)


def hashes(root, patterns):
	"""{relative path: sha256} of the files under root matching any pattern."""
	out = {}
	for pattern in patterns:
		for path in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
			if os.path.isfile(path):
				with open(path, "rb") as f:
					out[os.path.relpath(path, root)] = hashlib.sha256(f.read()).hexdigest()
	return out


def copyData(source, dest, paths):
	os.makedirs(dest, exist_ok=True)
	for rel in paths:
		src = os.path.join(source, rel)
		if not os.path.exists(src):
			raise FileNotFoundError(f"data path {rel!r} not in {source}")
		target = os.path.join(dest, rel)
		if os.path.isdir(src):
			shutil.copytree(src, target, symlinks=True)
		else:
			os.makedirs(os.path.dirname(target), exist_ok=True)
			shutil.copy2(src, target)


def runCheck(check, worktree, env, mapping, protect):
	argv = fill(check["run"], mapping)
	timeout = int(check.get("timeout_seconds", DEFAULT_TIMEOUT))

	def once():
		return qg_common.runCommand(argv, worktree, env, timeout, writable=[mapping["data"], mapping["tmp"]],
			readable=[worktree], protect=protect)

	before = hashes(mapping["data"], check.get("unchanged") or [])
	code, _, tail = once()
	first = hashes(mapping["data"], check.get("repeat_same") or [])
	why = []
	if code is None:
		why.append(f"timed out after {timeout} s")
	elif code != int(check.get("exit", 0)):
		why.append(f"exit {code}, expected {check.get('exit', 0)}")
	why += [f"output lacks {s!r}" for s in fill(check.get("output_contains") or [], mapping) if s not in tail]
	if check.get("unchanged") and hashes(mapping["data"], check["unchanged"]) != before:
		why.append("files under " + ", ".join(check["unchanged"]) + " changed")
	if check.get("repeat_same"):
		if not first:
			why.append("no file matches " + ", ".join(check["repeat_same"]))
		else:
			code2, _, tail2 = once()
			second = hashes(mapping["data"], check["repeat_same"])
			changed = sorted(p for p in set(first) | set(second) if first.get(p) != second.get(p))
			if code2 != code:
				why.append(f"second run exit {code2}, first {code}")
			if changed:
				why.append("differs between two runs: " + ", ".join(changed[:5]))
	return {"name": check.get("name", " ".join(argv)[:60]), "ok": not why, "why": "; ".join(why),
		"exit": code, "tail": "\n".join(tail.strip().splitlines()[-5:])}


def verify(worktree, topic, mainDir):
	path = specPath(mainDir, topic)
	if not os.path.isfile(path):
		return None
	spec = qg_common.loadJson(path)
	checks = spec.get("checks") or []
	if not checks:
		raise ValueError("the spec has no checks")
	config, _ = qg_common.loadConfig(worktree)
	protect = (config or {}).get("protect") or []
	results = []
	with tempfile.TemporaryDirectory(prefix="verify-") as scratch:
		mapping = {"repo": worktree, "data": os.path.join(scratch, "data"), "tmp": os.path.join(scratch, "tmp")}
		os.makedirs(mapping["tmp"])
		copyData(os.path.join(mainDir, spec.get("data", "data")), mapping["data"], spec.get("data_paths") or ["."])
		for name, content in (spec.get("files") or {}).items():
			if os.path.isabs(name) or ".." in name.split("/"):
				raise ValueError(f"file name {name!r} must stay inside {{tmp}}")
			target = os.path.join(mapping["tmp"], name)
			os.makedirs(os.path.dirname(target), exist_ok=True)
			with open(target, "w", encoding="utf-8") as f:
				f.write(fill(content, mapping))
		env = {**fill(spec.get("env") or {}, mapping), "DATA_ROOT": mapping["data"]}
		env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
		for check in checks:
			results.append(runCheck(check, worktree, env, mapping, protect))
	return {"passed": all(r["ok"] for r in results), "checks": results}


def main():
	worktree, topic, run = os.path.abspath(sys.argv[1]), sys.argv[2], int(sys.argv[3])
	mainDir = os.environ.get("WT_MAIN_DIR") or ""
	out = os.path.join(worktree, ".hook-only", "runs", f"verify-{run}.json")
	try:
		if not os.path.isdir(mainDir):
			raise ValueError("no main checkout (WT_MAIN_DIR)")
		result = verify(worktree, topic, mainDir)
		if result is None:
			return
	except qg_common.SandboxError as error:
		result = {"passed": False, "checks": [], "error": f"environment problem, not the code: {error}"}
	except (OSError, ValueError, KeyError, TypeError) as error:
		result = {"passed": False, "checks": [], "error": f"{type(error).__name__}: {error}"}
	with open(out, "w", encoding="utf-8") as f:
		json.dump({"run": run, "spec": specPath(mainDir, topic), **result}, f, ensure_ascii=False)


if __name__ == "__main__":
	if len(sys.argv) != 4:
		sys.exit("usage: verify.py <worktree> <topic> <run>")
	main()
