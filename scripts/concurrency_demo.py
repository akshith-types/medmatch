"""
Concurrency demonstrations for the viva (Module 6).

    python scripts/concurrency_demo.py            # all four scenarios
    python scripts/concurrency_demo.py race
    python scripts/concurrency_demo.py deadlock
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import demos  # noqa: E402
from app.db import open_pool, pool  # noqa: E402
from app.graph import route_finder  # noqa: E402
from app.lock_manager import COMPATIBILITY  # noqa: E402

LINE = "=" * 78


def show_trace(trace):
    for e in trace:
        print(f"   {e['ms']:>7.1f} ms  {e['who']:<32} {e['msg']}")


def race():
    print(f"\n{LINE}\nSCENARIO 1 — Race for the LAST O-negative unit, NO locking\n{LINE}")
    r = demos.race_demo(safe=False)
    show_trace(r["trace"])
    for k, v in r["results"].items():
        print(f"   {k}: {v}")
    print(f"   -> Lost update: {r['lost_update']}  (both read AVAILABLE and both overwrote the row;")
    print("      only the partial UNIQUE index stopped a double allocation in the ledger)")

    print(f"\n{LINE}\nSCENARIO 2 — Same race WITH Wait-Die + SELECT ... FOR UPDATE\n{LINE}")
    r = demos.race_demo(safe=True)
    show_trace(r["trace"])
    for k, v in r["results"].items():
        brief = {x: v[x] for x in ("outcome", "code", "reason", "transaction_id", "restarts", "elapsed_ms") if x in v}
        print(f"   {k}: {brief}")
    print(f"   -> Winners: {r['winners']} (must be exactly 1).  Final asset: {r['final_asset_state']}")


def deadlock():
    print(f"\n{LINE}\nSCENARIO 3 — Opposite-order bundle locks, DETECTION only (no Wait-Die)\n{LINE}")
    r = demos.deadlock_demo(use_wait_die=False)
    show_trace(r["trace"])
    for k, v in r["results"].items():
        print(f"   {k}: {v.get('outcome')} {v.get('code', '')} {v.get('elapsed_ms')} ms")
    print(f"   -> Deadlock occurred: {r['deadlock_occurred']}; total {r['total_ms']} ms "
          "(PostgreSQL waited deadlock_timeout before killing a victim)")

    print(f"\n{LINE}\nSCENARIO 4 — Same workload with Wait-Die PREVENTION\n{LINE}")
    r = demos.deadlock_demo(use_wait_die=True)
    show_trace(r["trace"])
    for k, v in r["results"].items():
        print(f"   {k}: {v.get('outcome')} {v.get('code', '')} restarts={v.get('restarts', '-')} "
              f"{v.get('elapsed_ms')} ms")
    print(f"   -> Deadlock occurred: {r['deadlock_occurred']}; total {r['total_ms']} ms")


def main():
    open_pool()
    route_finder.connect()
    print("Lock compatibility matrix (held -> requested):")
    for held, row in COMPATIBILITY.items():
        print(f"   {held}: " + "  ".join(f"{req}={'yes' if ok else 'no '}" for req, ok in row.items()))
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "race"):
        race()
    if which in ("all", "deadlock"):
        deadlock()
    pool.close()


if __name__ == "__main__":
    main()
