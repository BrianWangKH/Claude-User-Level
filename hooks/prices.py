"""USD cost of API usage, from the per-model price table in prices.json ($ per million tokens).

Shared by subagent-budget.py (USD budgets), subagent-cost.py (recorded actuals) and
worker-budget.py, so estimates, budgets, recorded actuals and the worker's --max-budget-usd all
use one unit. Cache writes are priced by TTL from usage.cache_creation (ephemeral_5m_input_tokens,
ephemeral_1h_input_tokens); without that split they count as 5-minute writes, the subagent default.
A model missing from the table is unpriced: requestUsd returns None, and callers report it.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES_FILE = os.environ.get("CLAUDE_PRICES") or os.path.join(HERE, "prices.json")


def load():
	"""{model id: prices}; empty if prices.json can't be read (every request is then unpriced)."""
	try:
		with open(PRICES_FILE, encoding="utf-8") as f:
			return json.load(f).get("models", {})
	except (OSError, ValueError):
		return {}


def modelKey(model):
	"""Model id without a context-size suffix such as "[1m]"."""
	return (model or "").split("[")[0].strip()


def requestUsd(usage, model, table):
	"""USD of one request's usage dict, or None when the model has no price."""
	price = table.get(modelKey(model))
	if price is None:
		return None
	split = usage.get("cache_creation") or {}
	write5m = int(split.get("ephemeral_5m_input_tokens") or 0)
	write1h = int(split.get("ephemeral_1h_input_tokens") or 0)
	if write5m + write1h == 0:
		write5m = int(usage.get("cache_creation_input_tokens") or 0)
	return (int(usage.get("input_tokens") or 0) * price["input"]
		+ int(usage.get("output_tokens") or 0) * price["output"]
		+ write5m * price["cache_write_5m"]
		+ write1h * price["cache_write_1h"]
		+ int(usage.get("cache_read_input_tokens") or 0) * price["cache_read"]) / 1e6
