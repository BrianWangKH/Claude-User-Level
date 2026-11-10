"""The one time source of home code (hooks/RULES.md X1, X2): stored timestamps are local ISO 8601 with
the UTC offset, to the second (2026-11-04T10:00:00+08:00), never epoch numbers.

now() is the current local time as an aware datetime; tests replace the source with useSource(fn) (fn
returns an aware datetime) and restore it with useSource(None). Durations may still be computed from
time.monotonic() or two parsed timestamps; only what is stored goes through here.
"""

from datetime import datetime

_source = None


def useSource(fn):
	"""Replace the time source (None: the system clock). For tests."""
	global _source
	_source = fn


def now():
	"""The current time, aware, in the local zone."""
	moment = _source() if _source is not None else datetime.now().astimezone()
	if moment.tzinfo is None:
		raise ValueError("clock: the time source returned a naive datetime")
	return moment


def isoNow():
	return isoOf(now())


def isoOf(moment):
	"""An aware datetime as the stored form; a naive one is refused."""
	if moment.tzinfo is None:
		raise ValueError("clock: a naive datetime has no offset to store")
	return moment.isoformat(timespec="seconds")


def parse(text):
	"""A stored timestamp back as an aware datetime; ValueError for anything else (naive text included)."""
	moment = datetime.fromisoformat(text)
	if moment.tzinfo is None:
		raise ValueError(f"clock: {text!r} has no UTC offset")
	return moment


def isStamp(value):
	"""Whether value is a stored-form timestamp (a string with an offset)."""
	if not isinstance(value, str):
		return False
	try:
		parse(value)
	except ValueError:
		return False
	return True
