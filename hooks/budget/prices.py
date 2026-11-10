"""USD cost of API usage, from the per-model price table in the home prices settings
(homeconfig.load("prices"), the prices section of ~/.claude/config/home.json; the *_usd_per_mtok fields,
$ per million tokens).

Shared by subagent_budget.py (USD budgets), subagent-cost.py (recorded actuals) and
worker_budget.py, so estimates, budgets, recorded actuals and the worker's --max-budget-usd all
use one unit. Cache writes are priced by TTL from usage.cache_creation (ephemeral_5m_input_tokens,
ephemeral_1h_input_tokens); without that split they count as 5-minute writes, the subagent default.
A model missing from the table is unpriced: requestUsd returns None, and callers report it.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402


def load():
	"""{model id: prices}. An unreadable settings file raises: a broken home config is reported, never
	turned into an empty table."""
	return homeconfig.load("prices")["models"]


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
	return (int(usage.get("input_tokens") or 0) * price["input_usd_per_mtok"]
		+ int(usage.get("output_tokens") or 0) * price["output_usd_per_mtok"]
		+ write5m * price["cache_write_5m_usd_per_mtok"]
		+ write1h * price["cache_write_1h_usd_per_mtok"]
		+ int(usage.get("cache_read_input_tokens") or 0) * price["cache_read_usd_per_mtok"]) / 1e6
