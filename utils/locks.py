# utils/locks.py
"""Cross-process lock files, for the two things that must not overlap: two
runs of one backup job, and two writers of the jobs file.

Qt-free by design (CLAUDE.md §3/§8). A lock is a small file created with
`O_CREAT | O_EXCL`, which the OS guarantees only one creator wins, holding
`{pid, started, what, token}` as JSON. The file is the lock; nothing is held
open, so a crashed process cannot leave a handle behind -- only a file, which
is why a lock whose owner is gone has to be recognised as stale and taken over.

Stale means the recorded process no longer exists, or the lock is older than
`STALE_AFTER` (a guard against a recycled pid, and against a process that is
alive but wedged for a day). An unreadable lock file counts as stale only once
it is older than `CORRUPT_GRACE`: its writer may be between `open` and
`write`, and stealing a lock mid-create would hand it to two owners.
"""
import json
import os
import random
import time
import uuid
from datetime import datetime
from typing import Dict, Optional

# A lock this old is stale whatever its pid says (pids are recycled).
STALE_AFTER = 24 * 3600
# How long an unparseable/empty lock file is given before it is called stale.
CORRUPT_GRACE = 60
# Passes `acquire` makes over a contended lock (each pass may sleep briefly)
# before giving up as busy.
ATTEMPTS = 25
# A takeover guard file older than this was left by a taker that crashed.
GUARD_STALE = 30


class LockBusy(Exception):
    """Another live owner holds the lock. `holder` is its recorded
    `{pid, started, what}`, or None when the file could not be parsed yet."""

    def __init__(self, path: str, holder: Optional[Dict] = None):
        """Remember which lock was busy and who holds it."""
        super().__init__(f"'{path}' is locked")
        self.path = path
        self.holder = holder


def pid_alive(pid: int) -> bool:
    """True when a process with this pid exists (stdlib/ctypes only).

    Windows: OpenProcess + GetExitCodeProcess == STILL_ACTIVE, because a
    process that exited but whose handle someone still holds also opens
    successfully. Access denied means it exists but belongs to someone else,
    so alive. Elsewhere: signal 0, with the same PermissionError reading.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # cannot tell; wrongly "alive" only delays a takeover
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _parse(raw: bytes) -> Optional[Dict]:
    """The holder dict from a lock file's bytes, or None if it is not one."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    return data


def _age(path: str, holder: Optional[Dict]) -> float:
    """Seconds since the lock was taken: its own `started`, else the file mtime."""
    try:
        return (datetime.now() - datetime.fromisoformat(holder["started"])).total_seconds()
    except (TypeError, KeyError, ValueError):
        return time.time() - os.path.getmtime(path)


class Lock:
    """A held lock. Release it (or use it as a context manager).

    `recovered` is the pid of the stale owner this lock replaced, else None.
    """

    def __init__(self, path: str, token: str, recovered: Optional[int] = None):
        """`token` is what this owner wrote into the lock file at `path`."""
        self.path = path
        self.token = token
        self.recovered = recovered

    def release(self) -> None:
        """Delete the lock file, but only if it is still ours.

        A lock taken over as stale (age timeout) while this process was still
        running now belongs to someone else; deleting it would free theirs.
        """
        # Windows will not delete a file a rival is reading at that instant
        # (PermissionError), and a swallowed failure would leave the lock held
        # until its owner exited -- so retry for about a second.
        for _ in range(100):
            try:
                with open(self.path, "rb") as handle:
                    holder = _parse(handle.read())
                if holder is not None and holder.get("token") != self.token:
                    return
                os.remove(self.path)
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.01)
        # Still stuck: a leftover is recovered as stale once this process exits.

    def __enter__(self) -> "Lock":
        """Context-manager entry: the lock is already held."""
        return self

    def __exit__(self, *_exc) -> None:
        """Context-manager exit: release, whether or not the block raised."""
        self.release()


def _create(path: str, what: str) -> Optional[Lock]:
    """Atomically create the lock file; None if someone else already has it."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    token = uuid.uuid4().hex
    body = json.dumps({"pid": os.getpid(), "started": datetime.now().isoformat(timespec="seconds"),
                       "what": what, "token": token}).encode("utf-8")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return Lock(path, token)


def _take_guard(path: str) -> Optional[Lock]:
    """The takeover guard for the lock at `path`, or None if a rival holds it.

    A guard is itself a lock file (`<path>.takeover`), held only for the few
    milliseconds a takeover takes. One left by a taker that crashed mid-way is
    removed once it is older than `GUARD_STALE`.
    """
    guard_path = path + ".takeover"
    try:
        guard = _create(guard_path, "takeover")
    except PermissionError:
        return None  # Windows: mid-delete, i.e. a rival just released it
    if guard is not None:
        return guard
    try:
        if time.time() - os.path.getmtime(guard_path) > GUARD_STALE:
            os.remove(guard_path)
    except OSError:
        pass
    return None


def acquire(path: str, what: str = "") -> Lock:
    """Take the lock at `path` or raise `LockBusy`; a stale one is taken over.

    Takeover is serialised by a short-lived guard file: only the guard holder
    may move a stale lock, and it re-reads the lock under the guard to be sure
    it is still the one judged stale. It then renames it to a unique name and
    creates the fresh lock with O_EXCL; losing that create means busy. (A bare
    rename with no guard is not enough: a rival that finished its own takeover
    between our staleness read and our rename would have its *fresh* lock moved
    away, and a third contender could then be handed the same lock.)

    ponytail: the grave-content check below is a last line of defence for the
    one window the guard cannot close -- the stale lock's owner releasing it at
    the exact instant of takeover, so a fresh lock appears before our rename.
    We then put it back with `os.link`; a contender could slip in during that
    gap. Needs a live-but-old owner releasing mid-takeover; not worth more.
    """
    recovered = None
    denied = None
    for _ in range(ATTEMPTS):
        try:
            lock = _create(path, what)
        except PermissionError as exc:
            # Windows reports a file that is mid-delete (a rival still has it
            # open) as "access denied" on create rather than "exists". Wait it
            # out; if it never clears it is a real permission problem.
            denied = exc
            time.sleep(random.uniform(0.002, 0.02))
            continue
        denied = None
        if lock is not None:
            lock.recovered = recovered
            return lock
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            continue  # released between our create and read: just retry
        except OSError:
            # Windows refuses a read while a rival is mid-rename: wait a beat
            # and look again; if it never clears, the loop ends as busy.
            time.sleep(random.uniform(0.002, 0.02))
            continue
        holder = _parse(raw)
        try:
            age = _age(path, holder)
        except OSError:
            continue
        if holder is None:
            stale = age > CORRUPT_GRACE
        else:
            stale = (not pid_alive(holder["pid"])) or age > STALE_AFTER
        if not stale:
            raise LockBusy(path, holder)
        guard = _take_guard(path)
        if guard is None:
            time.sleep(random.uniform(0.002, 0.02))
            continue  # a rival is taking it over; look again afterwards
        try:
            try:
                with open(path, "rb") as handle:
                    unchanged = handle.read() == raw
            except OSError:
                unchanged = False
            if not unchanged:
                continue  # replaced or removed since we judged it: re-evaluate
            grave = f"{path}.stale-{uuid.uuid4().hex}"
            try:
                os.replace(path, grave)
            except OSError:
                time.sleep(random.uniform(0.002, 0.02))
                continue
            try:
                with open(grave, "rb") as handle:
                    moved = handle.read()
            except OSError:
                moved = None
            if moved != raw:
                # We moved a lock that was not the stale one we judged.
                try:
                    os.link(grave, path)
                except OSError:
                    pass
                _remove(grave)
                raise LockBusy(path, _parse(moved or b""))
            _remove(grave)
            recovered = holder["pid"] if holder is not None else 0
        finally:
            guard.release()
    if denied is not None:
        raise denied
    raise LockBusy(path)


def _remove(path: str) -> None:
    """Best-effort delete of a leftover file."""
    try:
        os.remove(path)
    except OSError:
        pass


def acquire_wait(path: str, what: str = "", timeout: float = 5.0,
                 poll: float = 0.005) -> Lock:
    """`acquire`, retrying for up to `timeout` seconds; OSError if still busy.

    For short critical sections (the jobs file read-modify-write), where the
    holder will be gone in milliseconds.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return acquire(path, what)
        except LockBusy as busy:
            if time.monotonic() >= deadline:
                owner = f" (process {busy.holder['pid']})" if busy.holder else ""
                raise OSError(
                    f"Timed out after {timeout:g}s waiting for another Safekeep "
                    f"process{owner} to finish writing the jobs file. Nothing "
                    "was changed; try again.") from None
            time.sleep(random.uniform(poll, poll * 3))
