"""
Live concurrency demonstrations (used by the dashboard and by scripts/).

race_demo      — two hospitals hit "Reserve" on the LAST O-negative unit at the
                 same instant.  unsafe mode shows the lost update; safe mode
                 shows exactly one winner.
deadlock_demo  — two bundle requests lock two units in opposite order.
                 Without Wait-Die PostgreSQL must detect and kill a victim;
                 with Wait-Die the cycle never forms.
"""
from __future__ import annotations

import threading
import time
import uuid

from .db import get_conn
from .lock_manager import new_timestamp
from .reservations import ReservationError, reserve_asset, reserve_bundle, unsafe_reserve


def _hospital_id(code: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT hospital_id FROM hospitals WHERE hospital_code = %s", (code,)
        ).fetchone()["hospital_id"]


def _make_unit(type_name: str, holder_code: str, blood_group: str | None, label: str) -> int:
    code = f"DEMO-{uuid.uuid4().hex[:8].upper()}"
    with get_conn() as conn:
        return conn.execute(
            """INSERT INTO emergency_assets
                   (asset_code, asset_name, asset_type_id, hospital_id, blood_group,
                    collected_at, expiry_date)
               SELECT %s, %s, t.asset_type_id, h.hospital_id, %s, now(), now() + interval '20 days'
                 FROM asset_types t, hospitals h
                WHERE t.asset_type_name = %s AND h.hospital_code = %s
               RETURNING asset_id""",
            (code, label, blood_group, type_name, holder_code),
        ).fetchone()["asset_id"]


def _run_parallel(jobs):
    """Start all jobs at the same instant using a barrier."""
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def runner(i, fn):
        barrier.wait()
        t0 = time.perf_counter()
        try:
            results[i] = fn()
        except ReservationError as exc:
            results[i] = {"outcome": "REJECTED", "code": exc.code, "reason": exc.message}
        except Exception as exc:  # surface anything unexpected in the demo output
            results[i] = {"outcome": "ERROR", "reason": f"{type(exc).__name__}: {exc}"}
        results[i]["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)

    threads = [threading.Thread(target=runner, args=(i, fn)) for i, fn in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _format_trace(trace, names):
    if not trace:
        return []
    t0 = min(e["t"] for e in trace)
    return [
        {"ms": round((e["t"] - t0) * 1000, 1), "who": names.get(e["txn"], str(e["txn"])),
         "msg": e["msg"]}
        for e in sorted(trace, key=lambda e: e["t"])
    ]


def race_demo(safe: bool = True):
    asset_id = _make_unit("Packed Red Cells", "VLR02", "O-", "Last O-negative PRC unit")
    a_id, b_id = _hospital_id("KPD01"), _hospital_id("RNP01")
    trace: list = []
    ts_a, ts_b = new_timestamp(), new_timestamp()  # A is older
    names = {ts_a: "Hospital A (Katpadi)", ts_b: "Hospital B (Ranipet)"}

    if safe:
        jobs = [
            lambda: reserve_asset(asset_id, a_id, {"department": "Trauma"}, txn_ts=ts_a,
                                  hold_seconds=0.3, trace=trace),
            lambda: reserve_asset(asset_id, b_id, {"department": "Obstetrics"}, txn_ts=ts_b,
                                  hold_seconds=0.3, trace=trace),
        ]
    else:
        jobs = [
            lambda: unsafe_reserve(asset_id, a_id, delay=0.3, trace=trace, txn_ts=ts_a),
            lambda: unsafe_reserve(asset_id, b_id, delay=0.3, trace=trace, txn_ts=ts_b),
        ]

    results = _run_parallel(jobs)
    with get_conn() as conn:
        final = conn.execute(
            """SELECT a.status, a.version,
                      (SELECT count(*) FROM transfer_transactions t
                        WHERE t.asset_id = a.asset_id AND t.status = 'ALLOCATED') AS active_claims
                 FROM emergency_assets a WHERE asset_id = %s""",
            (asset_id,),
        ).fetchone()

    winners = sum(1 for r in results if r["outcome"] == "RESERVED")
    believed = sum(1 for r in results if r.get("believed_it_won"))
    return {
        "mode": "SAFE (Wait-Die + SELECT FOR UPDATE)" if safe else "UNSAFE (read-then-write, no locks)",
        "asset_id": asset_id,
        "results": {"Hospital A": results[0], "Hospital B": results[1]},
        "winners": winners,
        "lost_update": (not safe) and believed > 1,
        "final_asset_state": final,
        "trace": _format_trace(trace, names),
    }


def deadlock_demo(use_wait_die: bool = True):
    x = _make_unit("Packed Red Cells", "VLR02", "O-", "O-negative PRC (bundle)")
    y = _make_unit("Polyvalent Anti-Snake Venom", "TPT01", None, "ASV vial (bundle)")
    h1, h2 = _hospital_id("KPD01"), _hospital_id("CHN02")
    ts1, ts2 = new_timestamp(), new_timestamp()  # T1 older
    names = {ts1: "T1 (older, wants X then Y)", ts2: "T2 (younger, wants Y then X)"}
    trace: list = []

    t0 = time.perf_counter()
    results = _run_parallel([
        lambda: reserve_bundle([x, y], h1, use_wait_die=use_wait_die, pause_between=0.4,
                               txn_ts=ts1, trace=trace),
        lambda: reserve_bundle([y, x], h2, use_wait_die=use_wait_die, pause_between=0.4,
                               txn_ts=ts2, trace=trace),
    ])
    total_ms = round((time.perf_counter() - t0) * 1000)
    return {
        "mode": "Wait-Die PREVENTION" if use_wait_die else "PostgreSQL deadlock DETECTION only",
        "assets": {"X": x, "Y": y},
        "results": {"T1": results[0], "T2": results[1]},
        "total_ms": total_ms,
        "deadlock_occurred": any(r.get("code") == "DEADLOCK" for r in results),
        "trace": _format_trace(trace, names),
    }
