# MedMatch — Index Benchmark

Generated 2026-10-20 23:23. Median of 7 runs of `EXPLAIN (ANALYZE, BUFFERS)`.

Table size: **20,038** rows in `emergency_assets`.

| Query | Without index | With index | Speed-up | Plan before → after |
|---|---|---|---|---|
| Q1 — Unexpired, available units of one asset type (composite B+ tree) | 1.878 ms | 0.043 ms | **43.7×** | Seq Scan on emergency_assets → Bitmap Heap Scan on emergency_assets |
| Q2 — Exact HLA tissue match (hash index) | 1.381 ms | 0.031 ms | **44.5×** | Seq Scan on emergency_assets → Bitmap Heap Scan on emergency_assets |
| Q3 — Rare O-negative stock, soonest-expiring first (partial index) | 2.588 ms | 0.029 ms | **89.2×** | Seq Scan on emergency_assets → Index Scan using idx_asset_available_blood on emergency_assets |
| Q4 — Search joined with hospitals (selection pushed below the join) | 2.052 ms | 0.137 ms | **15.0×** | Seq Scan on emergency_assets a → Bitmap Heap Scan on emergency_assets a |

### Heuristic optimizations applied
- **Selection pushdown**: σ on `emergency_assets` is evaluated before the join with `hospitals`, so only matching units are joined (Q4).
- **Composite key order**: equality column `asset_type_id` first, range column `expiry_date` second, so the B+ tree is traversed to one leaf range and scanned forward.
- **Hash index for equality-only predicates** (HLA match): O(1) bucket lookup; PostgreSQL hash indexes grow by linear (dynamic) hashing, splitting one bucket at a time.
- **Partial index** stores only `status = 'AVAILABLE'` rows, keeping the hot index small and pre-sorted on `expiry_date` so `ORDER BY expiry_date LIMIT` needs no sort.


## Q1 — Unexpired, available units of one asset type (composite B+ tree)

**Relational algebra:** `π asset_id, asset_code, hospital_id, expiry_date ( σ asset_type_id = 5 ∧ expiry_date > now() ∧ status = 'AVAILABLE' (EMERGENCY_ASSETS) )`

```sql
SELECT asset_id, asset_code, hospital_id, expiry_date FROM medmatch.emergency_assets
                  WHERE asset_type_id = 5 AND expiry_date > now() AND status = 'AVAILABLE'
```

**Plan without index**
```
Seq Scan on emergency_assets  (cost=0.00..740.76 rows=124 width=36) (actual time=0.012..1.998 rows=42 loops=1)
  Filter: ((asset_type_id = 5) AND ((status)::text = 'AVAILABLE'::text) AND (expiry_date > now()))
  Rows Removed by Filter: 19996
  Buffers: shared hit=340
Planning Time: 0.035 ms
Execution Time: 2.005 ms
```
**Plan with index**
```
Bitmap Heap Scan on emergency_assets  (cost=10.56..257.99 rows=124 width=36) (actual time=0.011..0.029 rows=42 loops=1)
  Recheck Cond: ((asset_type_id = 5) AND (expiry_date > now()) AND ((status)::text = 'AVAILABLE'::text))
  Heap Blocks: exact=36
  Buffers: shared hit=38
  ->  Bitmap Index Scan on idx_asset_lookup  (cost=0.00..10.53 rows=124 width=0) (actual time=0.007..0.007 rows=42 loops=1)
        Index Cond: ((asset_type_id = 5) AND (expiry_date > now()) AND ((status)::text = 'AVAILABLE'::text))
        Buffers: shared hit=2
Planning Time: 0.050 ms
Execution Time: 0.037 ms
```


## Q2 — Exact HLA tissue match (hash index)

**Relational algebra:** `π asset_id, asset_code, hospital_id ( σ hla_type = 'HLA-A*02:01' (EMERGENCY_ASSETS) )`

```sql
SELECT asset_id, asset_code, hospital_id FROM medmatch.emergency_assets
                  WHERE hla_type = 'HLA-A*02:01'
```

**Plan without index**
```
Seq Scan on emergency_assets  (cost=0.00..590.48 rows=32 width=28) (actual time=0.140..1.346 rows=32 loops=1)
  Filter: ((hla_type)::text = 'HLA-A*02:01'::text)
  Rows Removed by Filter: 20006
  Buffers: shared hit=340
Planning Time: 0.013 ms
Execution Time: 1.350 ms
```
**Plan with index**
```
Bitmap Heap Scan on emergency_assets  (cost=4.25..100.57 rows=32 width=28) (actual time=0.005..0.036 rows=32 loops=1)
  Recheck Cond: ((hla_type)::text = 'HLA-A*02:01'::text)
  Heap Blocks: exact=26
  Buffers: shared hit=27
  ->  Bitmap Index Scan on idx_asset_hla_hash  (cost=0.00..4.24 rows=32 width=0) (actual time=0.002..0.002 rows=32 loops=1)
        Index Cond: ((hla_type)::text = 'HLA-A*02:01'::text)
        Buffers: shared hit=1
Planning Time: 0.017 ms
Execution Time: 0.042 ms
```


## Q3 — Rare O-negative stock, soonest-expiring first (partial index)

**Relational algebra:** `τ expiry_date ( π asset_id, hospital_id, expiry_date ( σ status = 'AVAILABLE' ∧ blood_group = 'O-' ∧ expiry_date > now() (EMERGENCY_ASSETS) ) )`

```sql
SELECT asset_id, hospital_id, expiry_date FROM medmatch.emergency_assets
                  WHERE status = 'AVAILABLE' AND blood_group = 'O-' AND expiry_date > now()
                  ORDER BY expiry_date LIMIT 20
```

**Plan without index**
```
Limit  (cost=744.83..744.88 rows=20 width=20) (actual time=2.579..2.581 rows=20 loops=1)
  Buffers: shared hit=340
  ->  Sort  (cost=744.83..745.21 rows=153 width=20) (actual time=2.578..2.579 rows=20 loops=1)
        Sort Key: expiry_date
        Sort Method: top-N heapsort  Memory: 27kB
        Buffers: shared hit=340
        ->  Seq Scan on emergency_assets  (cost=0.00..740.76 rows=153 width=20) (actual time=0.023..2.543 rows=193 loops=1)
              Filter: (((status)::text = 'AVAILABLE'::text) AND ((blood_group)::text = 'O-'::text) AND (expiry_date > now()))
              Rows Removed by Filter: 19845
              Buffers: shared hit=340
Planning Time: 0.038 ms
Execution Time: 2.588 ms
```
**Plan with index**
```
Limit  (cost=0.29..63.75 rows=20 width=20) (actual time=0.017..0.027 rows=20 loops=1)
  Buffers: shared hit=22
  ->  Index Scan using idx_asset_available_blood on emergency_assets  (cost=0.29..485.75 rows=153 width=20) (actual time=0.016..0.025 rows=20 loops=1)
        Index Cond: (((blood_group)::text = 'O-'::text) AND (expiry_date > now()))
        Buffers: shared hit=22
Planning Time: 0.075 ms
Execution Time: 0.035 ms
```


## Q4 — Search joined with hospitals (selection pushed below the join)

**Relational algebra:** `π a.asset_code, h.hospital_name ( (σ asset_type_id = 2 ∧ blood_group = 'O-' ∧ status = 'AVAILABLE' ∧ expiry_date > now() (EMERGENCY_ASSETS a)) ⋈ a.hospital_id = h.hospital_id HOSPITALS h )`

```sql
SELECT a.asset_code, h.hospital_name FROM medmatch.emergency_assets a
                  JOIN medmatch.hospitals h ON h.hospital_id = a.hospital_id
                  WHERE a.asset_type_id = 2 AND a.blood_group = 'O-'
                    AND a.status = 'AVAILABLE' AND a.expiry_date > now()
```

**Plan without index**
```
Hash Join  (cost=1.36..792.38 rows=49 width=46) (actual time=0.085..2.070 rows=88 loops=1)
  Hash Cond: (a.hospital_id = h.hospital_id)
  Buffers: shared hit=341
  ->  Seq Scan on emergency_assets a  (cost=0.00..790.86 rows=49 width=20) (actual time=0.079..2.046 rows=88 loops=1)
        Filter: ((asset_type_id = 2) AND ((blood_group)::text = 'O-'::text) AND ((status)::text = 'AVAILABLE'::text) AND (expiry_date > now()))
        Rows Removed by Filter: 19950
        Buffers: shared hit=340
  ->  Hash  (cost=1.16..1.16 rows=16 width=34) (actual time=0.005..0.005 rows=16 loops=1)
        Buckets: 1024  Batches: 1  Memory Usage: 10kB
        Buffers: shared hit=1
        ->  Seq Scan on hospitals h  (cost=0.00..1.16 rows=16 width=34) (actual time=0.001..0.003 rows=16 loops=1)
              Buffers: shared hit=1
Planning:
  Buffers: shared hit=4
Planning Time: 0.056 ms
Execution Time: 2.078 ms
```
**Plan with index**
```
Hash Join  (cost=7.19..283.42 rows=49 width=46) (actual time=0.030..0.115 rows=88 loops=1)
  Hash Cond: (a.hospital_id = h.hospital_id)
  Buffers: shared hit=151
  ->  Bitmap Heap Scan on emergency_assets a  (cost=5.83..281.90 rows=49 width=20) (actual time=0.024..0.094 rows=88 loops=1)
        Recheck Cond: (((blood_group)::text = 'O-'::text) AND (expiry_date > now()) AND ((status)::text = 'AVAILABLE'::text))
        Filter: (asset_type_id = 2)
        Rows Removed by Filter: 105
        Heap Blocks: exact=148
        Buffers: shared hit=150
        ->  Bitmap Index Scan on idx_asset_available_blood  (cost=0.00..5.82 rows=153 width=0) (actual time=0.015..0.021 rows=193 loops=1)
              Index Cond: (((blood_group)::text = 'O-'::text) AND (expiry_date > now()))
              Buffers: shared hit=2
  ->  Hash  (cost=1.16..1.16 rows=16 width=34) (actual time=0.005..0.005 rows=16 loops=1)
        Buckets: 1024  Batches: 1  Memory Usage: 10kB
        Buffers: shared hit=1
        ->  Seq Scan on hospitals h  (cost=0.00..1.16 rows=16 width=34) (actual time=0.001..0.003 rows=16 loops=1)
              Buffers: shared hit=1
Planning:
  Buffers: shared hit=4
Planning Time: 0.073 ms
Execution Time: 0.126 ms
```
