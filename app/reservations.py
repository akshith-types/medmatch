"""
Reservation engine — ACID transactions + Two-Phase Locking (Modules 5 & 6).

reserve_asset() is the critical section of the whole system:

  GROWING PHASE
    1. Wait-Die lock manager grants an X lock on 'asset:<id>' (or makes the
       younger transaction die so it can retry).
    2. BEGIN;  SELECT ... FOR UPDATE   -> PostgreSQL row-level X lock.
  WORK
    3. Re-validate under the lock (still AVAILABLE? not expired? not own stock?)
    4. INSERT request, UPDATE asset -> RESERVED, INSERT transfer (trigger -> audit)
    5. Ask the graph tier for the fastest (cold-chain aware) route
  SHRINKING PHASE
    6. COMMIT  -> row lock released;  lock manager releases all locks.

Because the status check happens *after* the row lock is held, two hospitals
racing for the last O-negative unit are serialised: the second one wakes up,
re-reads the row, sees RESERVED and is rejected cleanly — no lost update.
"""
from __future__ import annotations

import random
import time
from typing import Iterable

import psycopg
from psycopg import errors

from .db import get_conn
from .graph import route_finder
from .lock_manager import (
    EXCLUSIVE,
    LockTimeout,
    TransactionAborted,
    lock_manager,
    new_timestamp,
)

MAX_RESTARTS = 8


class ReservationError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


LOCK_ASSET_SQL = """
SELECT a.asset_id, a.asset_code, a.asset_name, a.hospital_id, a.status, a.expiry_date,
       a.blood_group, a.version, t.requires_cold_chain, t.asset_type_name,
       (a.expiry_date <= now()) AS is_expired
  FROM emergency_assets a
  JOIN asset_types t ON t.asset_type_id = a.asset_type_id
 WHERE a.asset_id = %s
   FOR UPDATE OF a
"""


def _validate(row, requester_id: int):
    if row is None:
        raise ReservationError("NOT_FOUND", "Asset does not exist", 404)
    if row["hospital_id"] == requester_id:
        raise ReservationError("OWN_STOCK", "Cannot request an asset your own hospital holds", 400)
    if row["is_expired"]:
        raise ReservationError("EXPIRED", f"{row['asset_code']} has expired")
    if row["status"] != "AVAILABLE":
        raise ReservationError(
            "ALREADY_CLAIMED",
            f"{row['asset_code']} is no longer available (status {row['status']})",
        )


def _insert_request(conn, requester_id: int, req: dict) -> int:
    return conn.execute(
        """INSERT INTO requesting_hospitals
               (hospital_id, department, contact_person, contact_phone, urgency_level)
           VALUES (%s, %s, %s, %s, %s) RETURNING request_id""",
        (
            requester_id,
            req.get("department", "Emergency"),
            req.get("contact_person", "Duty Officer"),
            req.get("contact_phone", "0000000000"),
            req.get("urgency_level", "CRITICAL"),
        ),
    ).fetchone()["request_id"]


def _claim_locked_row(conn, row, requester_id: int, req: dict, priority: int, ts: int):
    """Everything after the row lock is held. Runs inside the open DB transaction."""
    request_id = _insert_request(conn, requester_id, req)
    conn.execute(
        "UPDATE emergency_assets SET status = 'RESERVED' WHERE asset_id = %s",
        (row["asset_id"],),
    )
    route = route_finder.fastest_route(
        row["hospital_id"], requester_id, cold_chain=row["requires_cold_chain"]
    )
    txn = conn.execute(
        """INSERT INTO transfer_transactions
               (asset_id, request_id, requesting_hospital_id, supplying_hospital_id,
                priority, status, allocated_time, required_by, route_path, route_source,
                eta_minutes, wait_die_ts, notes)
           VALUES (%s, %s, %s, %s, %s, 'ALLOCATED', now(), %s, %s, %s, %s, %s, %s)
           RETURNING transaction_id, allocated_time""",
        (
            row["asset_id"],
            request_id,
            requester_id,
            row["hospital_id"],
            priority,
            req.get("required_by"),
            route["hospital_ids"] if route else None,
            route["source"] if route else None,
            route["total_minutes"] if route else None,
            ts,
            req.get("notes"),
        ),
    ).fetchone()
    return {
        "transaction_id": txn["transaction_id"],
        "allocated_time": txn["allocated_time"].isoformat(),
        "asset_id": row["asset_id"],
        "asset_code": row["asset_code"],
        "asset_name": row["asset_name"],
        "supplying_hospital_id": row["hospital_id"],
        "requesting_hospital_id": requester_id,
        "route": route,
    }


def reserve_asset(
    asset_id: int,
    requester_id: int,
    req: dict | None = None,
    priority: int = 1,
    txn_ts: int | None = None,
    hold_seconds: float = 0.0,
    trace: list | None = None,
):
    """Reserve one unit with Wait-Die + SELECT ... FOR UPDATE.  Returns a result dict."""
    req = req or {}
    ts = txn_ts or new_timestamp()
    resource = f"asset:{asset_id}"
    restarts = 0
    while True:
        try:
            lock_manager.acquire(ts, resource, EXCLUSIVE,
                                 on_wait=lambda h: _t(trace, ts, f"WAIT (older than holder {h})"))
            _t(trace, ts, f"lock-manager X lock granted on {resource}")
            with get_conn() as conn:
                with conn.transaction():
                    conn.execute("SET LOCAL lock_timeout = '5s'")
                    row = conn.execute(LOCK_ASSET_SQL, (asset_id,)).fetchone()
                    _t(trace, ts, "SELECT ... FOR UPDATE acquired row lock; status="
                       + (row["status"] if row else "missing"))
                    _validate(row, requester_id)
                    if hold_seconds:
                        time.sleep(hold_seconds)
                    result = _claim_locked_row(conn, row, requester_id, req, priority, ts)
                _t(trace, ts, f"COMMIT -> transaction #{result['transaction_id']}")
            result.update(wait_die_ts=ts, restarts=restarts, outcome="RESERVED")
            return result
        except TransactionAborted as exc:
            restarts += 1
            _t(trace, ts, f"DIE (younger than holder {exc.holder_ts}); restart #{restarts}")
            if restarts > MAX_RESTARTS:
                raise ReservationError("CONTENDED", "Too much contention, please retry", 503)
            lock_manager.release_all(ts)
            time.sleep(0.05 * restarts + random.random() * 0.05)
        except LockTimeout:
            raise ReservationError("LOCK_TIMEOUT", "Timed out waiting for the asset lock", 503)
        except errors.LockNotAvailable:
            raise ReservationError("LOCK_TIMEOUT", "Database row lock timed out", 503)
        except errors.UniqueViolation:
            raise ReservationError("ALREADY_CLAIMED", "Asset already has an active claim")
        except errors.RaiseException as exc:  # trigger guard (e.g. expired asset)
            raise ReservationError("RULE_VIOLATION", exc.diag.message_primary or str(exc))
        finally:
            lock_manager.release_all(ts)


def reserve_bundle(
    asset_ids: Iterable[int],
    requester_id: int,
    req: dict | None = None,
    use_wait_die: bool = True,
    pause_between: float = 0.0,
    txn_ts: int | None = None,
    trace: list | None = None,
):
    """
    Reserve several units atomically (e.g. 2 units of blood + 1 antivenom vial
    for one patient). Locks are taken one at a time in the given order, which
    is exactly what produces a deadlock when two requests ask in opposite
    order.  With use_wait_die=False we rely on PostgreSQL's deadlock DETECTION
    (one victim is killed after deadlock_timeout); with Wait-Die the cycle is
    PREVENTED and the younger request restarts immediately.
    """
    req = req or {}
    asset_ids = list(asset_ids)
    ts = txn_ts or new_timestamp()
    restarts = 0
    while True:
        try:
            with get_conn() as conn:
                with conn.transaction():
                    conn.execute("SET LOCAL lock_timeout = '10s'")
                    rows = []
                    for i, aid in enumerate(asset_ids):
                        if use_wait_die:
                            lock_manager.acquire(
                                ts, f"asset:{aid}", EXCLUSIVE,
                                on_wait=lambda h, a=aid: _t(trace, ts, f"WAIT for asset {a} (older than holder {h})"))
                        row = conn.execute(LOCK_ASSET_SQL, (aid,)).fetchone()
                        _t(trace, ts, f"locked asset {aid}")
                        _validate(row, requester_id)
                        rows.append(row)
                        if pause_between and i < len(asset_ids) - 1:
                            time.sleep(pause_between)
                    claims = [
                        _claim_locked_row(conn, r, requester_id, req, 1, ts) for r in rows
                    ]
            _t(trace, ts, "COMMIT bundle")
            return {
                "outcome": "RESERVED",
                "wait_die_ts": ts,
                "restarts": restarts,
                "transactions": [c["transaction_id"] for c in claims],
            }
        except TransactionAborted as exc:
            restarts += 1
            _t(trace, ts, f"DIE on {exc.resource} (holder {exc.holder_ts} is older); restart #{restarts}")
            lock_manager.release_all(ts)
            if restarts > MAX_RESTARTS:
                raise ReservationError("CONTENDED", "Too much contention", 503)
            time.sleep(0.05 * restarts)
        except errors.DeadlockDetected:
            _t(trace, ts, "PostgreSQL DEADLOCK DETECTED -> chosen as victim, rolled back")
            raise ReservationError("DEADLOCK", "Deadlock detected by PostgreSQL; transaction rolled back")
        finally:
            lock_manager.release_all(ts)


def unsafe_reserve(asset_id: int, requester_id: int, delay: float, trace: list | None = None,
                   txn_ts: int | None = None):
    """
    Deliberately WRONG implementation used only by the race-condition demo:
    read-then-write with no lock.  Both racers read AVAILABLE, both overwrite
    the row (lost update); only the partial unique index stops a second ledger
    entry.
    """
    ts = txn_ts or new_timestamp()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT asset_id, hospital_id, status FROM emergency_assets WHERE asset_id = %s",
            (asset_id,),
        ).fetchone()
        conn.commit()
        _t(trace, ts, f"read status={row['status']} (no lock)")
        if row["status"] != "AVAILABLE":
            return {"outcome": "REJECTED", "reason": "saw non-available status"}
        time.sleep(delay)  # the other hospital reads during this window
        try:
            with conn.transaction():
                conn.execute(
                    "UPDATE emergency_assets SET status = 'RESERVED' WHERE asset_id = %s",
                    (asset_id,),
                )
                _t(trace, ts, "UPDATE status='RESERVED' succeeded (overwrote blindly)")
                request_id = _insert_request(conn, requester_id, {})
                txn = conn.execute(
                    """INSERT INTO transfer_transactions
                         (asset_id, request_id, requesting_hospital_id, supplying_hospital_id,
                          status, allocated_time, wait_die_ts, notes)
                       VALUES (%s,%s,%s,%s,'ALLOCATED',now(),%s,'UNSAFE DEMO')
                       RETURNING transaction_id""",
                    (asset_id, request_id, requester_id, row["hospital_id"], ts),
                ).fetchone()
            _t(trace, ts, f"COMMIT -> transaction #{txn['transaction_id']}")
            return {"outcome": "RESERVED", "transaction_id": txn["transaction_id"],
                    "believed_it_won": True}
        except psycopg.errors.UniqueViolation:
            _t(trace, ts, "INSERT blocked by uq_one_active_claim_per_asset -> ROLLBACK")
            return {"outcome": "BLOCKED_BY_CONSTRAINT", "believed_it_won": True}


def _t(trace, ts, msg):
    if trace is not None:
        trace.append({"t": time.perf_counter(), "txn": ts, "msg": msg})
