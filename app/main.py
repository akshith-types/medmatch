"""MedMatch FastAPI application."""
from __future__ import annotations

import logging
import time
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg import errors
from pydantic import BaseModel, Field

from . import demos
from .db import fetch_all, fetch_one, get_conn, open_pool, pool
from .graph import route_finder
from .lock_manager import COMPATIBILITY, lock_manager
from .reservations import ReservationError, reserve_asset

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    open_pool()
    expired = fetch_one("SELECT fn_expire_assets() AS n")["n"]
    logging.getLogger("medmatch").info("Expired %s stale units on startup", expired)
    route_finder.connect()
    yield
    route_finder.close()
    pool.close()


app = FastAPI(title="MedMatch", version="1.0",
              description="Inter-Hospital Rare Resource Logistics Network", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(ReservationError)
async def reservation_error_handler(_req, exc: ReservationError):
    return JSONResponse(status_code=exc.http_status,
                        content={"error": exc.code, "message": exc.message})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


# ------------------------------------------------------------------ meta
@app.get("/api/health")
def health():
    db_ok = fetch_one("SELECT 1 AS ok")["ok"] == 1
    return {"database": "ok" if db_ok else "down", "graph_backend": route_finder.backend}


@app.get("/api/stats")
def stats():
    return fetch_one("""
        SELECT (SELECT count(*) FROM hospitals WHERE is_active)                         AS hospitals,
               (SELECT count(*) FROM emergency_assets)                                  AS total_units,
               (SELECT count(*) FROM emergency_assets
                 WHERE status = 'AVAILABLE' AND expiry_date > now())                    AS available_units,
               (SELECT count(*) FROM emergency_assets
                 WHERE status = 'AVAILABLE' AND expiry_date > now() AND blood_group = 'O-') AS o_negative,
               (SELECT count(*) FROM transfer_transactions
                 WHERE status IN ('ALLOCATED','IN_TRANSIT'))                            AS active_transfers,
               (SELECT count(*) FROM transfer_transactions WHERE status = 'COMPLETED')  AS completed_transfers,
               (SELECT count(*) FROM routes WHERE status = 'ACTIVE')                    AS active_routes
    """)


@app.get("/api/hospitals")
def hospitals():
    return fetch_all("""SELECT hospital_id, hospital_code, hospital_name, facility_type, city, state,
                               latitude, longitude, phone FROM hospitals WHERE is_active ORDER BY hospital_name""")


@app.get("/api/asset-types")
def asset_types():
    return fetch_all("SELECT * FROM asset_types ORDER BY category, asset_type_name")


@app.get("/api/inventory")
def inventory(hospital_id: Optional[int] = None):
    if hospital_id:
        return fetch_all("SELECT * FROM v_hospital_inventory WHERE hospital_id = %s ORDER BY category",
                         (hospital_id,))
    return fetch_all("SELECT * FROM v_hospital_inventory ORDER BY hospital_name, category")


# ---------------------------------------------------------------- search
@app.get("/api/search")
def search(
    asset_type_id: int,
    requester_id: int,
    blood_group: Optional[str] = None,
    hla_type: Optional[str] = None,
    max_distance_km: Optional[float] = None,
    limit: int = Query(25, le=200),
):
    t0 = time.perf_counter()
    rows = fetch_all(
        "SELECT * FROM fn_search_assets(%s::int, %s::int, %s::varchar, %s::varchar, %s::numeric, %s::int)",
        (asset_type_id, requester_id, blood_group or None, hla_type or None, max_distance_km, limit),
    )
    return {"query_ms": round((time.perf_counter() - t0) * 1000, 2), "count": len(rows), "results": rows}


@app.get("/api/hla-types")
def hla_types():
    return [r["hla_type"] for r in fetch_all(
        "SELECT DISTINCT hla_type FROM emergency_assets WHERE hla_type IS NOT NULL ORDER BY 1")]


@app.get("/api/explain")
def explain(asset_type_id: int = 2, blood_group: str = "O-"):
    """Execution plan of the core lookup (shows which index the optimizer picks)."""
    with get_conn() as conn:
        plan = conn.execute(
            """EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
               SELECT asset_id, asset_code, hospital_id, expiry_date
                 FROM emergency_assets
                WHERE asset_type_id = %s AND expiry_date > now()
                  AND status = 'AVAILABLE' AND blood_group = %s""",
            (asset_type_id, blood_group),
        ).fetchall()
    return {"plan": [r["QUERY PLAN"] for r in plan]}


# ----------------------------------------------------------- reservation
class ReserveIn(BaseModel):
    asset_id: int
    requester_id: int
    department: str = "Emergency"
    contact_person: str = "Duty Officer"
    contact_phone: str = "9000000000"
    urgency_level: str = Field("CRITICAL", pattern="^(CRITICAL|HIGH|MEDIUM|LOW)$")
    priority: int = Field(1, ge=1, le=5)
    notes: Optional[str] = None


@app.post("/api/reserve")
def reserve(body: ReserveIn):
    return reserve_asset(body.asset_id, body.requester_id, body.model_dump(), priority=body.priority)


@app.get("/api/transactions")
def transactions(hospital_id: Optional[int] = None, limit: int = 50):
    if hospital_id:
        return fetch_all(
            """SELECT d.* FROM v_transfer_dashboard d
                JOIN transfer_transactions t USING (transaction_id)
               WHERE t.requesting_hospital_id = %s OR t.supplying_hospital_id = %s
               ORDER BY d.request_time DESC LIMIT %s""",
            (hospital_id, hospital_id, limit))
    return fetch_all("SELECT * FROM v_transfer_dashboard ORDER BY request_time DESC LIMIT %s", (limit,))


def _call_proc(sql: str, params):
    try:
        with get_conn() as conn:
            conn.execute(sql, params)
            conn.commit()
    except errors.RaiseException as exc:
        raise HTTPException(409, exc.diag.message_primary)


@app.post("/api/transactions/{txn_id}/dispatch")
def dispatch(txn_id: int):
    _call_proc("CALL sp_dispatch_transfer(%s)", (txn_id,))
    return {"transaction_id": txn_id, "status": "IN_TRANSIT"}


@app.post("/api/transactions/{txn_id}/complete")
def complete(txn_id: int):
    _call_proc("CALL sp_complete_transfer(%s)", (txn_id,))
    return {"transaction_id": txn_id, "status": "COMPLETED"}


@app.post("/api/transactions/{txn_id}/cancel")
def cancel(txn_id: int, reason: Optional[str] = None):
    _call_proc("CALL sp_cancel_transfer(%s, %s)", (txn_id, reason))
    return {"transaction_id": txn_id, "status": "CANCELLED"}


@app.get("/api/audit")
def audit(transaction_id: Optional[int] = None, limit: int = 100):
    if transaction_id:
        return fetch_all("SELECT * FROM audit_logs WHERE transaction_id = %s ORDER BY log_time",
                         (transaction_id,))
    return fetch_all("SELECT * FROM audit_logs ORDER BY log_time DESC LIMIT %s", (limit,))


# ---------------------------------------------------------------- graph
@app.get("/api/route")
def route(src: int, dst: int, cold_chain: bool = False):
    r = route_finder.fastest_route(src, dst, cold_chain)
    if not r:
        raise HTTPException(404, "No route between these hospitals")
    names = {h["hospital_id"]: h["hospital_name"] for h in fetch_all(
        "SELECT hospital_id, hospital_name FROM hospitals WHERE hospital_id = ANY(%s)", (r["hospital_ids"],))}
    r["hospital_names"] = [names[i] for i in r["hospital_ids"]]
    return r


@app.get("/api/graph")
def graph():
    nodes = fetch_all("SELECT hospital_id AS id, hospital_code AS code, hospital_name AS name, city, "
                      "facility_type, latitude::float AS lat, longitude::float AS lon FROM hospitals")
    edges = fetch_all("""SELECT route_id, from_hospital_id AS source, to_hospital_id AS target,
                                transport_type AS mode, eta_minutes, distance_km::float AS distance_km,
                                temp_controlled FROM routes WHERE status = 'ACTIVE'""")
    return {"nodes": nodes, "edges": edges, "backend": route_finder.backend}


# --------------------------------------------------------- concurrency
@app.get("/api/locks")
def locks(since: float = 0.0):
    return {
        "compatibility_matrix": COMPATIBILITY,
        "held": lock_manager.snapshot(),
        "events": [e.__dict__ for e in lock_manager.events(since)][-100:],
    }


@app.post("/api/demo/race")
def demo_race(safe: bool = True):
    return demos.race_demo(safe=safe)


@app.post("/api/demo/deadlock")
def demo_deadlock(wait_die: bool = True):
    return demos.deadlock_demo(use_wait_die=wait_die)
