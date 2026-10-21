"""
Sync the transport network from PostgreSQL (source of truth) into Neo4j.

    python scripts/load_graph.py

Creates:
    (:Hospital {hospital_id, code, name, city, type, lat, lon})
    (:Hospital)-[:ROUTE_TO   {route_id, mode, distance_km, eta_minutes, temp_controlled}]->(:Hospital)
    (:Hospital)-[:COLD_ROUTE {...}]->(:Hospital)     for temperature-controlled legs only
then runs a sample fastest-path query.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import psycopg
from neo4j import GraphDatabase
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config  # noqa: E402


def main():
    with psycopg.connect(config.DATABASE_URL, row_factory=dict_row) as pg:
        hospitals = pg.execute(
            """SELECT hospital_id, hospital_code AS code, hospital_name AS name, city,
                      facility_type AS type, latitude::float AS lat, longitude::float AS lon
                 FROM medmatch.hospitals WHERE is_active""").fetchall()
        routes = pg.execute(
            """SELECT route_id, from_hospital_id AS src, to_hospital_id AS dst, transport_type AS mode,
                      distance_km::float AS distance_km, eta_minutes, temp_controlled
                 FROM medmatch.routes WHERE status = 'ACTIVE'""").fetchall()

    driver = GraphDatabase.driver(config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD))
    for attempt in range(40):  # Neo4j takes ~20-60 s to boot the first time
        try:
            driver.verify_connectivity()
            break
        except Exception as exc:
            if attempt == 39:
                raise
            print(f"  waiting for Neo4j ({exc.__class__.__name__})...")
            time.sleep(3)
    with driver.session() as s:
        s.run("MATCH (n:Hospital) DETACH DELETE n")
        s.run("CREATE CONSTRAINT hospital_id_unique IF NOT EXISTS "
              "FOR (h:Hospital) REQUIRE h.hospital_id IS UNIQUE")
        s.run("""UNWIND $rows AS r
                 CREATE (:Hospital {hospital_id: r.hospital_id, code: r.code, name: r.name,
                                    city: r.city, type: r.type, lat: r.lat, lon: r.lon})""",
              rows=hospitals)
        s.run("""UNWIND $rows AS r
                 MATCH (a:Hospital {hospital_id: r.src}), (b:Hospital {hospital_id: r.dst})
                 CREATE (a)-[:ROUTE_TO {route_id: r.route_id, mode: r.mode, distance_km: r.distance_km,
                                        eta_minutes: r.eta_minutes, temp_controlled: r.temp_controlled}]->(b)""",
              rows=routes)
        s.run("""MATCH (a:Hospital)-[r:ROUTE_TO {temp_controlled: true}]->(b:Hospital)
                 CREATE (a)-[:COLD_ROUTE {route_id: r.route_id, mode: r.mode, distance_km: r.distance_km,
                                          eta_minutes: r.eta_minutes, temp_controlled: true}]->(b)""")
        counts = s.run("""MATCH (h:Hospital) WITH count(h) AS nodes
                          MATCH ()-[r]->() RETURN nodes, count(r) AS rels""").single()
        print(f"Loaded {counts['nodes']} hospital nodes and {counts['rels']} relationships into Neo4j")

        try:
            rec = s.run("""MATCH (a:Hospital {code:'BLR01'}), (b:Hospital {code:'CHN02'})
                           CALL apoc.algo.dijkstra(a, b, 'COLD_ROUTE>', 'eta_minutes') YIELD path, weight
                           RETURN [n IN nodes(path) | n.code] AS hops, weight""").single()
            print(f"Sample cold-chain route BLR01 -> CHN02: {' -> '.join(rec['hops'])} ({rec['weight']:.0f} min)")
        except Exception as exc:
            print(f"APOC not available ({exc.__class__.__name__}); app will use pure-Cypher search")
    driver.close()


if __name__ == "__main__":
    main()
