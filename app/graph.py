"""
Transport routing (Module 7 — NoSQL Graph Database).

Neo4j property-graph model
    (:Hospital {hospital_id, code, name, city, lat, lon})
    (:Hospital)-[:ROUTE_TO   {route_id, mode, distance_km, eta_minutes, temp_controlled}]->(:Hospital)
    (:Hospital)-[:COLD_ROUTE {...same...}]->(:Hospital)   // only temperature-controlled legs

COLD_ROUTE is a projection of ROUTE_TO restricted to cold-chain legs, so an
organ/blood transfer that needs refrigeration can be routed with the same
Dijkstra call simply by choosing the relationship type.

If Neo4j is unreachable the same weighted graph is rebuilt in memory from the
relational ROUTES table and solved with NetworkX, so the app never blocks a
life-critical reservation on the routing tier.
"""
from __future__ import annotations

import logging
import threading
import time

import networkx as nx

from . import config
from .db import fetch_all

log = logging.getLogger("medmatch.graph")

DIJKSTRA_CYPHER = """
MATCH (a:Hospital {hospital_id: $src}), (b:Hospital {hospital_id: $dst})
CALL apoc.algo.dijkstra(a, b, $rel, 'eta_minutes') YIELD path, weight
RETURN [n IN nodes(path) | n.hospital_id]        AS hospital_ids,
       [r IN relationships(path) | r.mode]        AS modes,
       [r IN relationships(path) | r.eta_minutes] AS leg_minutes,
       weight                                     AS total_minutes
"""

# Pure-Cypher fallback if APOC is not installed (bounded hop count)
PURE_CYPHER = """
MATCH p = (a:Hospital {hospital_id: $src})-[rels:%s*1..6]->(b:Hospital {hospital_id: $dst})
WHERE all(n IN nodes(p) WHERE single(m IN nodes(p) WHERE m = n))
WITH p, reduce(t = 0, r IN relationships(p) | t + r.eta_minutes) AS total
ORDER BY total ASC LIMIT 1
RETURN [n IN nodes(p) | n.hospital_id] AS hospital_ids,
       [r IN relationships(p) | r.mode] AS modes,
       [r IN relationships(p) | r.eta_minutes] AS leg_minutes,
       total AS total_minutes
"""


class RouteFinder:
    def __init__(self):
        self._driver = None
        self._apoc = False
        self._lock = threading.Lock()
        self._nx_cache: dict[bool, nx.DiGraph] = {}
        self._last_attempt = 0.0

    # ----------------------------------------------------------- neo4j
    def connect(self) -> bool:
        if not config.USE_NEO4J:
            return False
        self._last_attempt = time.monotonic()
        try:
            from neo4j import GraphDatabase

            driver = GraphDatabase.driver(
                config.NEO4J_URI,
                auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
                connection_timeout=3,
            )
            driver.verify_connectivity()
            with driver.session() as s:
                try:
                    s.run("RETURN apoc.version() AS v").single()
                    self._apoc = True
                except Exception:
                    self._apoc = False
            self._driver = driver
            log.info("Connected to Neo4j at %s (APOC=%s)", config.NEO4J_URI, self._apoc)
            return True
        except Exception as exc:  # pragma: no cover - depends on local setup
            log.warning("Neo4j unavailable (%s); using NetworkX fallback", exc)
            self._driver = None
            return False

    @property
    def backend(self) -> str:
        return "neo4j" if self._driver else "fallback"

    def close(self):
        if self._driver:
            self._driver.close()

    def _neo4j_route(self, src: int, dst: int, cold_chain: bool):
        rel = "COLD_ROUTE" if cold_chain else "ROUTE_TO"
        with self._driver.session() as s:
            if self._apoc:
                rec = s.run(DIJKSTRA_CYPHER, src=src, dst=dst, rel=rel + ">").single()
            else:
                rec = s.run(PURE_CYPHER % rel, src=src, dst=dst).single()
        if not rec:
            return None
        return {
            "hospital_ids": list(rec["hospital_ids"]),
            "modes": list(rec["modes"]),
            "leg_minutes": [int(x) for x in rec["leg_minutes"]],
            "total_minutes": int(round(rec["total_minutes"])),
        }

    # -------------------------------------------------------- fallback
    def invalidate_cache(self):
        self._nx_cache.clear()

    def _nx_graph(self, cold_chain: bool) -> nx.DiGraph:
        with self._lock:
            if cold_chain not in self._nx_cache:
                rows = fetch_all(
                    """SELECT from_hospital_id, to_hospital_id, eta_minutes, transport_type
                         FROM routes
                        WHERE status = 'ACTIVE' AND (%s = FALSE OR temp_controlled)""",
                    (cold_chain,),
                )
                g = nx.DiGraph()
                for r in rows:
                    u, v = r["from_hospital_id"], r["to_hospital_id"]
                    # keep the fastest edge if both ROAD and AIR exist
                    if not g.has_edge(u, v) or g[u][v]["eta_minutes"] > r["eta_minutes"]:
                        g.add_edge(u, v, eta_minutes=r["eta_minutes"], mode=r["transport_type"])
                self._nx_cache[cold_chain] = g
            return self._nx_cache[cold_chain]

    def _nx_route(self, src: int, dst: int, cold_chain: bool):
        g = self._nx_graph(cold_chain)
        try:
            path = nx.dijkstra_path(g, src, dst, weight="eta_minutes")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        legs = list(zip(path, path[1:]))
        return {
            "hospital_ids": path,
            "modes": [g[u][v]["mode"] for u, v in legs],
            "leg_minutes": [g[u][v]["eta_minutes"] for u, v in legs],
            "total_minutes": sum(g[u][v]["eta_minutes"] for u, v in legs),
        }

    # ------------------------------------------------------------ public
    def fastest_route(self, src: int, dst: int, cold_chain: bool = False):
        result, source = None, "fallback"
        if not self._driver and config.USE_NEO4J and time.monotonic() - self._last_attempt > 30:
            self.connect()  # Neo4j may have finished booting after the app started
        if self._driver:
            try:
                result = self._neo4j_route(src, dst, cold_chain)
                source = "neo4j"
            except Exception as exc:  # pragma: no cover
                log.warning("Neo4j query failed (%s); falling back", exc)
        if result is None and source == "fallback":
            result = self._nx_route(src, dst, cold_chain)
        if result is None and cold_chain:
            # no fully refrigerated corridor: report the fastest normal path, flagged
            alt = self.fastest_route(src, dst, cold_chain=False)
            if alt:
                alt["cold_chain_required"] = True
                alt["cold_chain_satisfied"] = False
            return alt
        if result is not None:
            result["source"] = source
            result["cold_chain_required"] = cold_chain
            result["cold_chain_satisfied"] = True
        return result


route_finder = RouteFinder()
