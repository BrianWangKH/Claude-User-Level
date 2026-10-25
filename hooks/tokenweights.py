"""Price weights for token counts, relative to plain input.

Shared by subagent-budget.py (budget limits) and subagent-cost.py (recorded actuals), so a budget,
the estimate it came from and the recorded actual are all in the same unit: weighted tokens.
Cache reads cost a tenth of plain input, so a resumed agent re-reading its context counts little.
Override the weights with "token_weights" in budget-defaults.json.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_FILE = os.environ.get("CLAUDE_BUDGET_DEFAULTS") or os.path.join(HERE, "budget-defaults.json")
BASE = {
	"input_tokens": 1.0,
	"cache_creation_input_tokens": 1.25,
	"cache_read_input_tokens": 0.1,
	"output_tokens": 1.0,
}


def load():
	"""BASE with the overrides from budget-defaults.json; BASE alone if that file can't be read."""
	try:
		with open(DEFAULTS_FILE, encoding="utf-8") as f:
			config = json.load(f)
	except (OSError, ValueError):
		return dict(BASE)
	return {**BASE, **config.get("token_weights", {})}


def weigh(usage, weights):
	"""Weighted token count of one usage dict (or of summed totals with the same keys)."""
	return round(sum(int(usage.get(k) or 0) * w for k, w in weights.items()))
