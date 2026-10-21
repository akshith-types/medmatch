"""
Wait-Die lock manager (Module 6 — Concurrency Control).

MedMatch layers two mechanisms:

  1. This lock manager decides *who may proceed* when transactions conflict,
     using a Shared/Exclusive compatibility matrix and the Wait-Die
     deadlock-PREVENTION rule (no cycles can ever form).
  2. Inside the database, the winner then takes a real row lock with
     SELECT ... FOR UPDATE (strict 2PL: locks held until COMMIT/ROLLBACK).

Lock compatibility matrix
                    requested
                    S        X
    held   S        yes      no
           X        no       no

Wait-Die rule (timestamps are assigned once, when a transaction first
starts, and are KEPT across restarts so a transaction eventually becomes
the oldest and is guaranteed to finish — no starvation):

    if  requester.ts < holder.ts   (requester is OLDER)   -> WAIT
    else                           (requester is YOUNGER) -> DIE (abort, retry later)

Waits only ever go old -> young, so the wait-for graph is acyclic and a
deadlock is impossible.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

SHARED = "S"
EXCLUSIVE = "X"

COMPATIBILITY = {
    SHARED: {SHARED: True, EXCLUSIVE: False},
    EXCLUSIVE: {SHARED: False, EXCLUSIVE: False},
}

_ts_lock = threading.Lock()
_last_ts = 0


def new_timestamp() -> int:
    """Strictly increasing transaction timestamp in microseconds (smaller = older)."""
    global _last_ts
    with _ts_lock:
        _last_ts = max(_last_ts + 1, time.time_ns() // 1000)
        return _last_ts


class TransactionAborted(Exception):
    """Raised when Wait-Die decides the requesting (younger) transaction must die."""

    def __init__(self, txn_ts: int, resource: str, holder_ts: int):
        super().__init__(
            f"Wait-Die: txn {txn_ts} is younger than holder {holder_ts} on {resource} -> DIE"
        )
        self.txn_ts = txn_ts
        self.resource = resource
        self.holder_ts = holder_ts


class LockTimeout(Exception):
    pass


@dataclass
class _LockEntry:
    holders: dict = field(default_factory=dict)  # txn_ts -> mode


@dataclass
class LockEvent:
    t: float
    txn_ts: int
    resource: str
    mode: str
    outcome: str  # GRANTED / WAIT / DIE / RELEASE / TIMEOUT


class WaitDieLockManager:
    def __init__(self, wait_timeout: float = 5.0, keep_events: int = 500):
        self._table: dict[str, _LockEntry] = {}
        self._cv = threading.Condition()
        self.wait_timeout = wait_timeout
        self._events: list[LockEvent] = []
        self._keep = keep_events

    # ---------------------------------------------------------------- utils
    def _log(self, txn_ts, resource, mode, outcome):
        self._events.append(LockEvent(time.time(), txn_ts, resource, mode, outcome))
        if len(self._events) > self._keep:
            del self._events[: len(self._events) - self._keep]

    def events(self, since: float = 0.0):
        with self._cv:
            return [e for e in self._events if e.t >= since]

    def snapshot(self):
        with self._cv:
            return {r: dict(e.holders) for r, e in self._table.items() if e.holders}

    def _conflicts(self, entry: _LockEntry, txn_ts: int, mode: str):
        return [
            (ts, held)
            for ts, held in entry.holders.items()
            if ts != txn_ts and not COMPATIBILITY[held][mode]
        ]

    # ------------------------------------------------------------- acquire
    def acquire(self, txn_ts: int, resource: str, mode: str = EXCLUSIVE, on_wait=None) -> None:
        deadline = time.monotonic() + self.wait_timeout
        logged_wait = False
        with self._cv:
            while True:
                entry = self._table.setdefault(resource, _LockEntry())
                current = entry.holders.get(txn_ts)
                if current == EXCLUSIVE or current == mode:
                    return  # already holds a sufficient lock
                conflicts = self._conflicts(entry, txn_ts, mode)
                if not conflicts:
                    # grant (or upgrade S -> X)
                    entry.holders[txn_ts] = (
                        EXCLUSIVE if EXCLUSIVE in (mode, current) else SHARED
                    )
                    self._log(txn_ts, resource, mode, "GRANTED")
                    return
                oldest_holder = min(ts for ts, _ in conflicts)
                if txn_ts < oldest_holder:
                    # requester is older than every conflicting holder -> WAIT
                    if not logged_wait:
                        self._log(txn_ts, resource, mode, "WAIT")
                        logged_wait = True
                        if on_wait:
                            on_wait(oldest_holder)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._log(txn_ts, resource, mode, "TIMEOUT")
                        raise LockTimeout(f"txn {txn_ts} timed out waiting for {resource}")
                    self._cv.wait(timeout=remaining)
                else:
                    # requester is younger than some holder -> DIE
                    self._log(txn_ts, resource, mode, "DIE")
                    raise TransactionAborted(txn_ts, resource, oldest_holder)

    # ------------------------------------------------------------- release
    def release_all(self, txn_ts: int) -> None:
        """Strict 2PL shrinking phase: every lock released together at commit/abort."""
        with self._cv:
            for resource, entry in list(self._table.items()):
                if entry.holders.pop(txn_ts, None) is not None:
                    self._log(txn_ts, resource, "-", "RELEASE")
                if not entry.holders:
                    del self._table[resource]
            self._cv.notify_all()


lock_manager = WaitDieLockManager()
