"""
Create the MedMatch schema and load realistic seed data.

    python scripts/setup_db.py            # default: 20,000 inventory units
    python scripts/setup_db.py --units 50000

Steps
  1. Run db/01..04 SQL files (schema, functions/triggers, indexes/views, master data)
  2. Generate the road/air corridor network into ROUTES
  3. Bulk-load emergency inventory with COPY (fast) + asset attributes
  4. Mark expired stock, ANALYZE for the optimizer
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import DATABASE_URL  # noqa: E402

SQL_FILES = ["01_schema.sql", "02_functions_triggers.sql", "03_views_indexes.sql", "04_seed_master.sql"]

# Realistic Indian ABO/Rh distribution (approx.) — negatives are genuinely rare
BLOOD_DIST = [("O+", 36.5), ("B+", 31.0), ("A+", 21.5), ("AB+", 6.5),
              ("O-", 1.5), ("B-", 1.3), ("A-", 1.0), ("AB-", 0.7)]
HLA_POOL = [f"HLA-{loc}*{a:02d}:{b:02d}" for loc, a, b in [
    ("A", 1, 1), ("A", 2, 1), ("A", 3, 1), ("A", 11, 1), ("A", 24, 2), ("A", 33, 3),
    ("B", 7, 2), ("B", 8, 1), ("B", 15, 1), ("B", 35, 1), ("B", 40, 1), ("B", 51, 1), ("B", 52, 1),
    ("DRB1", 3, 1), ("DRB1", 4, 1), ("DRB1", 7, 1), ("DRB1", 11, 1), ("DRB1", 15, 1), ("DRB1", 15, 2),
]]


def haversine(lat1, lon1, lat2, lon2):
    r = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def run_sql_files(conn):
    for name in SQL_FILES:
        print(f"  running db/{name}")
        conn.execute((ROOT / "db" / name).read_text())
    conn.commit()


def build_routes(conn):
    hs = conn.execute(
        "SELECT hospital_id, hospital_code, city, facility_type, latitude::float, longitude::float "
        "FROM medmatch.hospitals").fetchall()
    gid = conn.execute("SELECT graph_id FROM medmatch.transport_network_graph LIMIT 1").fetchone()[0]
    hubs = {"VLR01", "CHN01", "BLR01", "TPT01"}        # helipad-equipped hubs
    cold_types = {"BLOOD_BANK", "TRANSPLANT_CENTER"}
    rows = []
    for a in hs:
        for b in hs:
            if a[0] == b[0]:
                continue
            d = haversine(a[4], a[5], b[4], b[5])
            if d <= 100:  # road corridor between neighbouring facilities
                road_km = round(d * 1.3 + 1, 2)                       # winding factor
                eta = max(4, round(road_km / 45 * 60 + 3))           # ~45 km/h ambulance avg
                cold = d <= 40 or a[3] in cold_types or b[3] in cold_types
                rows.append((gid, f"{a[1]} → {b[1]} (road)", a[0], b[0], "ROAD", road_km, eta, cold,
                             "Ambulance, green corridor" if cold else "Standard ambulance"))
            if a[1] in hubs and b[1] in hubs:  # air corridor
                eta = round(d / 180 * 60 + 20)                        # 180 km/h + 20 min handling
                rows.append((gid, f"{a[1]} → {b[1]} (air)", a[0], b[0], "AIR", round(d, 2), eta, True,
                             "Helicopter, refrigerated box"))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO medmatch.routes (graph_id, route_name, from_hospital_id, to_hospital_id,
                   transport_type, distance_km, eta_minutes, temp_controlled, conditions)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
    conn.commit()
    print(f"  {len(rows)} directed route edges created")


def build_inventory(conn, n_units: int, seed: int = 42):
    rnd = random.Random(seed)
    types = conn.execute(
        "SELECT asset_type_id, asset_type_name, category, shelf_life_days::float "
        "FROM medmatch.asset_types").fetchall()
    hospitals = conn.execute(
        "SELECT hospital_id, hospital_code, facility_type FROM medmatch.hospitals").fetchall()
    blood_types = [t for t in types if t[2] == "BLOOD"]
    organ_types = [t for t in types if t[2] == "ORGAN"]
    av_types = [t for t in types if t[2] == "ANTIVENOM"]
    groups, gweights = zip(*BLOOD_DIST)

    # weight which facilities hold what
    def holder(category):
        weights = []
        for h in hospitals:
            ft = h[2]
            w = {"BLOOD": {"BLOOD_BANK": 6, "GOVERNMENT": 3, "PRIVATE": 2, "TRANSPLANT_CENTER": 2},
                 "ORGAN": {"TRANSPLANT_CENTER": 8, "GOVERNMENT": 1, "PRIVATE": 1, "BLOOD_BANK": 0},
                 "ANTIVENOM": {"GOVERNMENT": 5, "PRIVATE": 2, "TRANSPLANT_CENTER": 1, "BLOOD_BANK": 0}}[category][ft]
            if category == "ANTIVENOM" and h[1] == "TPT01":
                w = 10  # toxicology centre
            weights.append(w)
        return rnd.choices(hospitals, weights)[0]

    now = datetime.now(timezone.utc)
    lines, attrs = [], []
    for i in range(1, n_units + 1):
        roll = rnd.random()
        if roll < 0.80:
            t = rnd.choices(blood_types, [30, 40, 15, 15])[0]
            bg = rnd.choices(groups, gweights)[0]
            hla = None
        elif roll < 0.83:
            t = rnd.choices(organ_types, [35, 15, 5, 45])[0]
            bg = rnd.choices(groups, gweights)[0]
            hla = rnd.choice(HLA_POOL)
        else:
            t = rnd.choice(av_types)
            bg, hla = None, None
        h = holder(t[2])
        shelf = timedelta(days=t[3])
        # spread collection over the past ~1.2 shelf-lives so some stock is already expired
        collected = now - shelf * rnd.uniform(0, 1.2)
        expiry = collected + shelf
        r2 = rnd.random()
        status = "AVAILABLE"
        if expiry <= now:
            status = "EXPIRED" if r2 < 0.7 else "DISCARDED"
        elif r2 < 0.12:
            status = "DELIVERED"
        prefix = {"BLOOD": "BLD", "ORGAN": "ORG", "ANTIVENOM": "AVN"}[t[2]]
        code = f"{prefix}-{h[1]}-{i:06d}"
        lines.append("\t".join([
            code, t[1], str(t[0]), str(h[0]), bg or r"\N", hla or r"\N",
            collected.isoformat(), expiry.isoformat(), status]))
        if t[2] == "ORGAN":
            attrs.append((code, "Donor Age", "BIOLOGICAL", str(rnd.randint(18, 65)), "years"))
            attrs.append((code, "Cold Ischemia Limit", "PHYSICAL", f"{t[3] * 24:.0f}", "hours"))
        elif t[2] == "BLOOD" and rnd.random() < 0.05:
            attrs.append((code, "Volume", "PHYSICAL", str(rnd.choice([250, 350, 450])), "ml"))
            attrs.append((code, "Irradiated", "BIOLOGICAL", rnd.choice(["Yes", "No"]), None))
        elif t[2] == "ANTIVENOM" and rnd.random() < 0.2:
            attrs.append((code, "Neutralising Capacity", "BIOLOGICAL", "0.6", "mg/ml"))

    with conn.cursor() as cur:
        with cur.copy(
            "COPY medmatch.emergency_assets (asset_code, asset_name, asset_type_id, hospital_id, "
            "blood_group, hla_type, collected_at, expiry_date, status) FROM STDIN"
        ) as cp:
            cp.write("\n".join(lines) + "\n")
        cur.executemany(
            """INSERT INTO medmatch.asset_attributes (asset_id, attribute_name, attribute_type, attribute_value, uom)
               SELECT asset_id, %s, %s, %s, %s FROM medmatch.emergency_assets WHERE asset_code = %s""",
            [(a[1], a[2], a[3], a[4], a[0]) for a in attrs])
    conn.commit()
    print(f"  {n_units} inventory units and {len(attrs)} attributes loaded")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--units", type=int, default=20000)
    args = ap.parse_args()
    print(f"Connecting to {DATABASE_URL.split('@')[-1]}")
    with psycopg.connect(DATABASE_URL) as conn:
        print("[1/4] Schema, functions, triggers, indexes, views, master data")
        run_sql_files(conn)
        print("[2/4] Transport network")
        build_routes(conn)
        print("[3/4] Inventory")
        build_inventory(conn, args.units)
        print("[4/4] Expire stale stock + ANALYZE")
        n = conn.execute("SELECT medmatch.fn_expire_assets()").fetchone()[0]
        conn.execute("ANALYZE")
        conn.commit()
        print(f"  {n} additional units expired")
    print("Database ready.")


if __name__ == "__main__":
    main()
