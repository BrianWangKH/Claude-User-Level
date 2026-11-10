"""Runtime state files, the one way new code writes them (hooks/RULES.md S5, S7, S8, X1).

- writeJson(path, obj): one JSON object, atomically (a temporary file in the same directory, then
  os.replace), so a reader never sees half a file.
- appendJsonl(path, obj): one line, appended with a single write on an O_APPEND descriptor.
- Every object carries "schema": <int>, and every "t" or "*_at" value is a timestamp with a UTC offset
  (clock.isoNow()); anything else raises StateError before a byte is written.
- write(root, kind, obj, key) / read(root, kind, key): the registered kinds of config/registry/state-registry.json
  (homeconfig "state-registry"): the path comes from the registry (hookonly.stateDir + its file pattern)
  and the object's schema must be the registered one. read() returns None for a missing file and for an
  object of another schema version: an unknown version counts as absent (a gate reading it refuses).
- A kind with "location": "home" (hooks/RULES.md S1) is home-level, not a checkout's: it lives in
  homeStateDir()/<component>/ (<home>/state, the home root of this tree, hooks/lib/homepath.py; no variable
  moves it: tests replace homeStateDir itself) and is written and read with root None, under the same
  atomic write, schema and lock rules. A home kind is never strict (hook-read-only/ is a checkout's).
- locked(root, kind, key): S9, the lock of a kind the registry marks "multi_writer": true; its "lock" is
  the file pattern plus ".lock". Held (fcntl.flock, exclusive) around one read-modify-write; a kind with
  one writer has no lock and refuses one, so a second writer can't appear unnoticed.
"""

import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import clock  # noqa: E402
import homeconfig  # noqa: E402
import hookonly  # noqa: E402

REGISTRY = "state-registry"
HOME_LOCATION = "home"  # a kind's "location" in the registry: <home>/state/<component>/ instead of a checkout's
HOME_STATE = "state"
KEY = re.compile(r"^[A-Za-z0-9][\w.-]*$")
FORMATS = ("json", "jsonl")


class StateError(Exception):
	pass


def checkObject(obj):
	"""StateError unless obj is a dict with an int schema and offset timestamps."""
	if not isinstance(obj, dict):
		raise StateError(f"statefile: state is one JSON object, got {type(obj).__name__}")
	schema = obj.get("schema")
	if not isinstance(schema, int) or isinstance(schema, bool):
		raise StateError('statefile: the object has no int "schema" field')
	for name, value in obj.items():
		if (name == "t" or name.endswith("_at")) and value is not None and not clock.isStamp(value):
			raise StateError(f"statefile: {name}={value!r} is not a timestamp with a UTC offset (clock.isoNow())")


def writeJson(path, obj):
	checkObject(obj)
	text = json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
	directory = os.path.dirname(os.path.abspath(path))
	os.makedirs(directory, exist_ok=True)
	handle, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
	try:
		with os.fdopen(handle, "w", encoding="utf-8") as f:
			f.write(text)
			f.flush()
			os.fsync(f.fileno())
		os.replace(temporary, path)
	except BaseException:
		if os.path.exists(temporary):
			os.remove(temporary)
		raise


def appendJsonl(path, obj):
	checkObject(obj)
	line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
	os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
	handle = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
	try:
		os.write(handle, line)
	finally:
		os.close(handle)


def readJson(path, schema):
	"""The object at path when its schema is the given one; None when missing, unreadable or another version."""
	try:
		with open(path, encoding="utf-8") as f:
			obj = json.load(f)
	except (OSError, ValueError):
		return None
	return obj if isinstance(obj, dict) and obj.get("schema") == schema else None


def readJsonl(path, schema):
	"""The lines at path with the given schema, in order; others (another version, a torn line) are skipped."""
	try:
		with open(path, encoding="utf-8") as f:
			lines = f.read().splitlines()
	except OSError:
		return []
	out = []
	for line in lines:
		try:
			obj = json.loads(line)
		except ValueError:
			continue
		if isinstance(obj, dict) and obj.get("schema") == schema:
			out.append(obj)
	return out


def kind(name):
	"""The registry entry of a state kind; StateError for an unregistered or malformed one."""
	kinds = homeconfig.load(REGISTRY)["kinds"]
	if name not in kinds:
		raise StateError(f"statefile: state kind {name!r} is not registered in {homeconfig.path(REGISTRY)}")
	entry = kinds[name]
	if entry["format"] not in FORMATS:
		raise StateError(f"statefile: kind {name!r} has format {entry['format']!r}, not one of {FORMATS}")
	if not isinstance(entry.get("multi_writer"), bool):
		raise StateError(f"statefile: kind {name!r} has no boolean multi_writer (hooks/RULES.md S9)")
	if entry["multi_writer"] != (entry.get("lock") == entry["file_pattern"] + ".lock"):
		raise StateError(
			f"statefile: kind {name!r}: a multi-writer kind's lock is its file pattern plus .lock, a single-writer one has none"
		)
	if entry.get("location", HOME_LOCATION) != HOME_LOCATION or (entry.get("location") and entry["strict"]):
		raise StateError(
			f"statefile: kind {name!r}: location is {HOME_LOCATION!r} (and not strict) or absent, got {entry.get('location')!r}"
		)
	return entry


def homeStateDir():
	"""<home>/state: the home-level runtime state of this tree (homepath.HOME_ROOT)."""
	return os.path.join(homepath.HOME_ROOT, HOME_STATE)


def pathOf(root, name, key=None):
	"""Where a kind's file lives: in the checkout root, or for a home kind (root None) in homeStateDir(); key fills
	the {id} of a per-id file pattern."""
	entry = kind(name)
	home = entry.get("location") == HOME_LOCATION
	if home != (root is None):
		where = f"the home ({homeStateDir()}): pass root None" if home else "a checkout: pass its root"
		raise StateError(f"statefile: kind {name!r} lives in {where}")
	pattern = entry["file_pattern"]
	if "{id}" in pattern:
		if key is None or not KEY.match(str(key)):
			raise StateError(f"statefile: kind {name!r} needs an id of letters, digits, '.', '_' or '-', got {key!r}")
		pattern = pattern.replace("{id}", str(key))
	elif key is not None:
		raise StateError(f"statefile: kind {name!r} is one file; it takes no id")
	if home:
		return os.path.join(homeStateDir(), entry["component"], pattern)
	return os.path.join(hookonly.stateDir(root, entry["component"], strict=bool(entry["strict"])), pattern)


def lockPath(root, name, key=None):
	"""The lock file of a multi-writer kind's file; StateError for a single-writer kind."""
	if not kind(name)["multi_writer"]:
		raise StateError(f"statefile: kind {name!r} has one writer; it takes no lock (hooks/RULES.md S9)")
	return pathOf(root, name, key) + ".lock"


@contextlib.contextmanager
def locked(root, name, key=None):
	"""Hold the kind's lock (exclusive flock) for one read-modify-write."""
	path = lockPath(root, name, key)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
	try:
		fcntl.flock(handle, fcntl.LOCK_EX)
		yield path
	finally:
		os.close(handle)


def write(root, name, obj, key=None):
	"""Write (json) or append (jsonl) one object of a registered kind."""
	entry = kind(name)
	if isinstance(obj, dict) and obj.get("schema") != entry["schema"]:
		raise StateError(f"statefile: kind {name!r} is schema {entry['schema']}, the object says {obj.get('schema')!r}")
	path = pathOf(root, name, key)
	if entry["format"] == "json":
		writeJson(path, obj)
	else:
		appendJsonl(path, obj)
	return path


def read(root, name, key=None):
	"""A json kind's object (None when absent or another version), or a jsonl kind's matching lines."""
	entry = kind(name)
	path = pathOf(root, name, key)
	if entry["format"] == "json":
		return readJson(path, entry["schema"])
	return readJsonl(path, entry["schema"])
