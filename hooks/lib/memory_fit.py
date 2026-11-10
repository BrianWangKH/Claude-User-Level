"""How many items of a known memory size fit in a share of the available memory: the hook suite's pytest-xdist
workers (hooks/tools/suite-workers.py) and the parallel groups of an external test harness (not included).

- memAvailableKb(meminfoText): MemAvailable in kB from the text of /proc/meminfo; ValueError when the line is
  missing or malformed.
- fitCount(availableKb, shareFactor, itemKb, cap, shareKey, itemKey): floor(availableKb x shareFactor / itemKb),
  at least 1 and at most cap; ValueError naming the settings key (shareKey, itemKey) of a share outside (0, 1]
  or a size that isn't positive. Memory bounds the count because an item that runs out of it takes the others
  (and the person's sessions) down.
"""

MEMINFO = "/proc/meminfo"
MEM_AVAILABLE = "MemAvailable:"


def memAvailableKb(meminfoText):
	"""MemAvailable in kB from the text of /proc/meminfo; ValueError when the line is missing or malformed."""
	for line in meminfoText.splitlines():
		if line.startswith(MEM_AVAILABLE):
			fields = line[len(MEM_AVAILABLE):].split()
			if len(fields) == 2 and fields[1] == "kB" and fields[0].isdigit():
				return int(fields[0])
			raise ValueError(f"malformed line {line!r}")
	raise ValueError(f"no {MEM_AVAILABLE} line")


def fitCount(availableKb, shareFactor, itemKb, cap, shareKey, itemKey):
	"""Items that fit in shareFactor of availableKb at itemKb each: at least 1, at most cap (module doc)."""
	if not 0 < shareFactor <= 1:
		raise ValueError(f"{shareKey} must be in (0, 1], got {shareFactor}")
	if itemKb <= 0:
		raise ValueError(f"{itemKey} must be positive, got {itemKb}")
	return min(cap, max(1, int(availableKb * shareFactor // itemKb)))
