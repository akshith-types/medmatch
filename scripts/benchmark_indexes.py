"""
Index benchmark with EXPLAIN ANALYZE (Module 4 — Indexing & Query Optimization).

    python scripts/benchmark_indexes.py

For each core query: drop the supporting indexes, capture the plan and timing,
recreate them, capture again.  Writes reports/index_benchmark.md.
"""
from __future__ import annotations

import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import DATABASE_URL  # noqa: E402

INDEXES = {
    "idx_asset_lookup": "CREATE INDEX idx_asset_lookup ON medmatch.emergency_assets USING btree (asset_type_id, expiry_date, status)",
    "idx_asset_hla_hash": "CREATE INDEX idx_asset_hla_hash ON medmatch.emergency_assets USING hash (hla_type)",
    "idx_asset_available_blood": "CREATE INDEX idx_asset_available_blood ON medmatch.emergency_assets (blood_group, expiry_date) WHERE status = 'AVAILABLE'",
}

QUERIES = [
    {
        "name": "Q1 — Unexpired, available units of one asset type (composite B+ tree)",
        "algebra": "π asset_id, asset_code, hospital_id, expiry_date ( "
                   "σ asset_type_id = 5 ∧ expiry_date > now() ∧ status = 'AVAILABLE' (EMERGENCY_ASSETS) )",
        "sql": """SELECT asset_id, asset_code, hospital_id, expiry_date FROM medmatch.emergency_assets
                  WHERE asset_type_id = 5 AND expiry_date > now() AND status = 'AVAILABLE'""",
    },
    {
        "name": "Q2 — Exact HLA tissue match (hash index)",
        "algebra": "π asset_id, asset_code, hospital_id ( σ hla_type = 'HLA-A*02:01' (EMERGENCY_ASSETS) )",
        "sql": """SELECT asset_id, asset_code, hospital_id FROM medmatch.emergency_assets
                  WHERE hla_type = 'HLA-A*02:01'""",
    },
    {
        "name": "Q3 — Rare O-negative stock, soonest-expiring first (partial index)",
        "algebra": "τ expiry_date ( π asset_id, hospital_id, expiry_date ( "
                   "σ status = 'AVAILABLE' ∧ blood_group = 'O-' ∧ expiry_date > now() (EMERGENCY_ASSETS) ) )",
        "sql": """SELECT asset_id, hospital_id, expiry_date FROM medmatch.emergency_assets
                  WHERE status = 'AVAILABLE' AND blood_group = 'O-' AND expiry_date > now()
                  ORDER BY expiry_date LIMIT 20""",
    },
    {
        "name": "Q4 — Search joined with hospitals (selection pushed below the join)",
        "algebra": "π a.asset_code, h.hospital_name ( (σ asset_type_id = 2 ∧ blood_group = 'O-' ∧ "
                   "status = 'AVAILABLE' ∧ expiry_date > now() (EMERGENCY_ASSETS a)) ⋈ a.hospital_id = h.hospital_id HOSPITALS h )",
        "sql": """SELECT a.asset_code, h.hospital_name FROM medmatch.emergency_assets a
                  JOIN medmatch.hospitals h ON h.hospital_id = a.hospital_id
                  WHERE a.asset_type_id = 2 AND a.blood_group = 'O-'
                    AND a.status = 'AVAILABLE' AND a.expiry_date > now()""",
    },
]

RUNS = 7


def measure(conn, sql):
    times, plan = [], None
    for i in range(RUNS):
        rows = conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql).fetchall()
        text = [r[0] for r in rows]
        m = re.search(r"Execution Time: ([\d.]+) ms", "\n".join(text))
        times.append(float(m.group(1)))
        plan = text
    access = next((ln.strip().lstrip("-> ").split("  (")[0] for ln in plan
                   if re.search(r"Scan|Seq", ln)), "?")
    return statistics.median(times), access, plan


def main():
    out = [f"# MedMatch — Index Benchmark\n\nGenerated {datetime.now():%Y-%m-%d %H:%M}. "
           f"Median of {RUNS} runs of `EXPLAIN (ANALYZE, BUFFERS)`.\n"]
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        n = conn.execute("SELECT count(*) FROM medmatch.emergency_assets").fetchone()[0]
        out.append(f"Table size: **{n:,}** rows in `emergency_assets`.\n")
        out.append("| Query | Without index | With index | Speed-up | Plan before → after |\n|---|---|---|---|---|")
        details = []

        for q in QUERIES:
            for name in INDEXES:
                conn.execute(f"DROP INDEX IF EXISTS medmatch.{name}")
            conn.execute("ANALYZE medmatch.emergency_assets")
            t_off, acc_off, plan_off = measure(conn, q["sql"])

            for ddl in INDEXES.values():
                conn.execute(ddl)
            conn.execute("ANALYZE medmatch.emergency_assets")
            t_on, acc_on, plan_on = measure(conn, q["sql"])

            speed = t_off / t_on if t_on else float("inf")
            print(f"{q['name']}\n   without: {t_off:8.3f} ms  [{acc_off}]\n"
                  f"   with:    {t_on:8.3f} ms  [{acc_on}]   -> {speed:.1f}x faster\n")
            out.append(f"| {q['name']} | {t_off:.3f} ms | {t_on:.3f} ms | **{speed:.1f}×** | "
                       f"{acc_off} → {acc_on} |")
            details.append(
                f"\n## {q['name']}\n\n**Relational algebra:** `{q['algebra']}`\n\n"
                f"```sql\n{q['sql'].strip()}\n```\n\n**Plan without index**\n```\n" + "\n".join(plan_off) +
                "\n```\n**Plan with index**\n```\n" + "\n".join(plan_on) + "\n```\n")

    out.append("\n### Heuristic optimizations applied\n"
               "- **Selection pushdown**: σ on `emergency_assets` is evaluated before the join with "
               "`hospitals`, so only matching units are joined (Q4).\n"
               "- **Composite key order**: equality column `asset_type_id` first, range column "
               "`expiry_date` second, so the B+ tree is traversed to one leaf range and scanned forward.\n"
               "- **Hash index for equality-only predicates** (HLA match): O(1) bucket lookup; "
               "PostgreSQL hash indexes grow by linear (dynamic) hashing, splitting one bucket at a time.\n"
               "- **Partial index** stores only `status = 'AVAILABLE'` rows, keeping the hot index small "
               "and pre-sorted on `expiry_date` so `ORDER BY expiry_date LIMIT` needs no sort.\n")
    out.extend(details)
    report = ROOT / "reports" / "index_benchmark.md"
    report.parent.mkdir(exist_ok=True)
    report.write_text("\n".join(out), encoding="utf-8")
    print(f"Report written to {report}")


if __name__ == "__main__":
    main()
