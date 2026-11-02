"""Fail-safe helpers: atomic writes, JSON that survives corruption, JSONC, warnings logged once."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("delege")

_WARNED: set[str] = set()
_WARNED_LOCK = threading.Lock()
_WARNED_MAX = 1000
_UMASK = os.umask(0o022)               # read once (os.umask can only be read by setting it)
os.umask(_UMASK)


def warn_once(key: str, msg: str, exc: BaseException | None = None) -> bool:
    """Logs ``msg`` (and ``exc``) as a warning the first time ``key`` is seen, at debug level afterwards."""
    text = f"{msg}: {type(exc).__name__}: {exc}" if exc is not None else msg
    with _WARNED_LOCK:
        first = key not in _WARNED
        if first and len(_WARNED) < _WARNED_MAX:
            _WARNED.add(key)
    log.log(logging.WARNING if first else logging.DEBUG, text)
    return first


def atomic_write_bytes(path: str | os.PathLike, data: bytes, mode: int | None = None) -> None:
    """Writes ``data`` to ``path`` atomically (a temporary file in the same folder, then ``os.replace``). ``mode``:
    the file's permissions (default: the old file's, else the umask's). Raises ``OSError``; the old file is then
    left untouched."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if mode is None:
            try:
                mode = path.stat().st_mode & 0o777
            except OSError:
                mode = 0o666 & ~_UMASK
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: str | os.PathLike, text: str, mode: int | None = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode)


def write_json(path: str | os.PathLike, data: Any, *, indent: int | None = 1, mode: int | None = None) -> None:
    """Writes ``data`` as JSON, atomically."""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=indent) + "\n", mode)


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    """The JSON content of ``path``, or ``default`` when it is missing or unreadable. A file that is not valid JSON
    is renamed ``<name>.corrupt`` (so the next save does not destroy it) and a warning is logged once."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return default
    except OSError as e:
        warn_once(f"read:{path}", f"cannot read {path}", e)
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as e:
        backup = path.with_name(path.name + ".corrupt")
        try:
            os.replace(path, backup)
            where = f"kept as {backup.name}"
        except OSError:
            where = "could not be moved aside"
        warn_once(f"corrupt:{path}", f"{path} is not valid JSON ({where}); using defaults", e)
        return default


def read_jsonc(path: str | os.PathLike) -> dict:
    """``opencode.jsonc``: JSON with ``//`` comment lines and trailing commas."""
    text = "\n".join(line for line in Path(path).read_text().splitlines() if not line.lstrip().startswith("//"))
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))


def tail(text: str, n: int = 40) -> str:
    return "\n".join((text or "").strip().splitlines()[-n:])
