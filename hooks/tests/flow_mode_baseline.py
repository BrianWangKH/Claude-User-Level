"""How each caller decided the flow mode before S4 (one reader, homeconfig.flowMode): the baseline
test_flow_mode_source.py's matrix holds the new reader to. Unused since S4 except as that baseline. Test data,
never a home program: pure functions of the record contents and the session's FLOW_INTEGRATION.

Inputs: main and topic = the content of <main>/.hook-only/flow-mode and <worktree>/.hook-only/flow-mode (None:
no file); env = FLOW_INTEGRATION of the process (None: unset). Each function gives (mode, source): mode "origin",
"local" or ERROR; source "env" (the variable or its default), "topic", "main", or "garbage" (a record holding
something else, read silently as origin).

Transcribed from the pre-S4 tree:
- homeconfig.flowMode(): env("FLOW_INTEGRATION"): the value, else "origin"; another value raised
- run-worker.sh and finish-worktree.sh: a topic record exports FLOW_INTEGRATION=local when it says local
  (whitespace removed) and unsets it otherwise; without one the inherited variable stays. Every hook of a
  worker run (crap-gate, mutation, agent-guard, dispatch-guard, auto-merge, base-ref.sh) read that variable
- worker-control.flow_mode: the topic record's text, else homeconfig.flowMode(); callers compared it with "local"
- run-result.py / worker-status.py flowLocal: record == "local", else homeconfig.flowMode() == "local"
- wt_queue.isLocal: the main record == "local" (no file: False)
- an interactive session in a worktree: its hooks read the session's variable (the wt-pane mod set it from the
  main record at the session's start)
- run-steps.py wrapup: --flow-mode from run-worker.sh (the worker's variable); the run record unused
"""

ERROR = "error"
MODES = ("origin", "local")


def envMode(env):
	if not env:
		return "origin", "env"
	return (env, "env") if env in MODES else (ERROR, "env")


def workerEnv(topic, env):
	"""The FLOW_INTEGRATION run-worker.sh (and finish-worktree.sh) left, as a mode."""
	if topic is None:
		return envMode(env)
	text = "".join(topic.split())
	return ("local" if text == "local" else "origin"), ("topic" if text in MODES else "garbage")


def workerControl(topic, env):
	if topic is None:
		return envMode(env)
	text = topic.strip()
	return ("local" if text == "local" else "origin"), ("topic" if text in MODES else "garbage")


def flowLocal(topic, env):
	return workerControl(topic, env)


def wtQueue(main):
	if main is None:
		return "origin", "default"
	text = main.strip()
	return ("local" if text == "local" else "origin"), ("main" if text in MODES else "garbage")


def mirror(main):
	"""FLOW_INTEGRATION as the wt-pane mod set it at a session's start from the main record."""
	return "local" if main is not None and main.strip() == "local" else None


# caller -> (scope, old rule over (main, topic, env)); the new answer is homeconfig.flowMode of the main checkout
# (scope main) or of the worktree (scope topic; the wrap-up also passes the run record as recorded)
CALLERS = {
	"main-env (fetch-origin, new-worktree.sh, base-ref.sh, wt_controls/pane, gates in the main checkout)": (
		"main",
		lambda main, topic, env: envMode(env),
	),
	"main-queue (wt_queue)": ("main", lambda main, topic, env: wtQueue(main)),
	"worker hooks (crap-gate, mutation, agent-guard, dispatch-guard, auto-merge, base-ref.sh in a run)": (
		"topic",
		lambda main, topic, env: workerEnv(topic, env),
	),
	"worker shell (run-worker.sh, finish-worktree.sh)": ("topic", lambda main, topic, env: workerEnv(topic, env)),
	"worker-control (wt merge, launch message)": ("topic", lambda main, topic, env: workerControl(topic, env)),
	"display (run-result.py, worker-status.py)": ("topic", lambda main, topic, env: flowLocal(topic, env)),
	"interactive worktree session hooks": ("topic", lambda main, topic, env: envMode(env)),
	"run-steps.py wrapup": ("wrapup", lambda main, topic, env: workerEnv(topic, env)),
}
