"""Careful reads and writes of the files in the state directory.

The service runs around the clock from a microSD card, so every write here is deliberate:
compact JSON, one rename per write (a crash or power loss leaves the old file), ``fsync``
only for the files whose loss would hurt (the node identity, the settings), and a file
that cannot be read is moved aside rather than silently overwritten.
"""

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("meshcore-companion")

_STALE_TMP = re.compile(r".+\.\d+\.tmp$")  # what write_atomic leaves after a crash


def dumps(value: Any) -> bytes:
    """``value`` as compact JSON."""
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> bytes:
    """A short fingerprint of ``data``, to skip a write that would change nothing."""
    return hashlib.blake2b(data, digest_size=16).digest()


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, data: bytes, *, mode: int = 0o666, fsync: bool = False) -> None:
    """Replace ``path`` with ``data`` in one rename.

    The temporary file is created with ``mode`` (less the umask) from the start, so a
    secret is never readable by others, not even for a moment. With ``fsync`` the data
    is on the card before the rename and the rename is on the card before this returns.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        os.unlink(tmp)  # a leftover would keep its old mode through O_CREAT
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with open(fd, "wb", closefd=False) as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp)
        raise
    os.close(fd)
    os.replace(tmp, path)
    if fsync:
        _fsync_dir(path.parent)


def set_aside(path: Path, why: str) -> None:
    """Move a damaged ``path`` to ``<name>.bad`` (one is kept) with one warning."""
    bad = path.with_name(path.name + ".bad")
    try:
        os.replace(path, bad)
    except OSError as exc:
        log.warning("%s %s, and could not be moved aside (%s)", path, why, exc)
        return
    log.warning("%s %s; moved it to %s and starting without it", path, why, bad.name)


def read_json(path: Path, expect: Optional[type] = None) -> Any:
    """``path`` parsed as JSON, or ``None`` when it does not exist.

    A file that cannot be read, does not parse, or is not of type ``expect`` is moved
    aside (:func:`set_aside`) and reads as ``None``, so the next save starts a fresh file
    while the damaged one is kept for inspection.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        set_aside(path, f"cannot be read ({exc})")
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        set_aside(path, f"is not valid JSON ({exc})")
        return None
    if expect is not None and not isinstance(value, expect):
        set_aside(path, f"does not hold a JSON {expect.__name__}")
        return None
    return value


def remove_stale_tmp(directory: Path) -> int:
    """Delete the ``<name>.<pid>.tmp`` files a crash left in ``directory``."""
    removed = 0
    try:
        candidates = list(directory.glob("*.tmp"))
    except OSError:
        return 0
    for path in candidates:
        if _STALE_TMP.match(path.name) and path.is_file():
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                log.warning("could not remove %s: %s", path, exc)
    if removed:
        log.info("removed %d stale temporary file(s) in %s", removed, directory)
    return removed
