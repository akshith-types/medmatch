-- =====================================================================
-- 03_views_indexes.sql  —  Indexes (Module 4) and views
-- =====================================================================
SET search_path TO medmatch, public;

-- ---------------------------------------------------------------------
-- INDEXES
-- ---------------------------------------------------------------------
-- (1) Composite B+ Tree — the core search path:
--     WHERE asset_type_id = ? AND expiry_date > now() AND status = 'AVAILABLE'
--     Leading equality column, then the range column, then status.
CREATE INDEX idx_asset_lookup
    ON emergency_assets USING btree (asset_type_id, expiry_date, status);

-- (2) Hash index — equality-only HLA tissue matching (organs).
--     PostgreSQL hash indexes use linear (dynamic) hashing: buckets split
--     incrementally as the table grows, so no full rehash is needed.
CREATE INDEX idx_asset_hla_hash
    ON emergency_assets USING hash (hla_type);

-- (3) Partial B+ Tree — only rows that can actually be reserved.
--     Much smaller than a full index because most stock eventually
--     becomes DELIVERED / EXPIRED.
CREATE INDEX idx_asset_available_blood
    ON emergency_assets (blood_group, expiry_date)
    WHERE status = 'AVAILABLE';

-- (4) FK / dashboard support
CREATE INDEX idx_asset_hospital      ON emergency_assets (hospital_id);
CREATE INDEX idx_txn_status_time     ON transfer_transactions (status, request_time DESC);
CREATE INDEX idx_txn_requester       ON transfer_transactions (requesting_hospital_id);
CREATE INDEX idx_txn_supplier        ON transfer_transactions (supplying_hospital_id);
CREATE INDEX idx_audit_txn           ON audit_logs (transaction_id, log_time);
CREATE INDEX idx_routes_from         ON routes (from_hospital_id) WHERE status = 'ACTIVE';
CREATE INDEX idx_attr_asset          ON asset_attributes (asset_id);

-- ---------------------------------------------------------------------
-- VIEWS
-- ---------------------------------------------------------------------
-- Live, reservable inventory across the whole network
CREATE OR REPLACE VIEW v_available_assets AS
SELECT a.asset_id, a.asset_code, a.asset_name, t.asset_type_name, t.category,
       a.blood_group, a.hla_type, a.expiry_date,
       ROUND((EXTRACT(EPOCH FROM (a.expiry_date - now())) / 3600)::NUMERIC, 1) AS hours_to_expiry,
       h.hospital_id, h.hospital_name, h.city
FROM   emergency_assets a
JOIN   asset_types t ON t.asset_type_id = a.asset_type_id
JOIN   hospitals   h ON h.hospital_id   = a.hospital_id
WHERE  a.status = 'AVAILABLE' AND a.expiry_date > now();

-- Per-hospital stock summary (aggregation)
CREATE OR REPLACE VIEW v_hospital_inventory AS
SELECT h.hospital_id, h.hospital_name, h.city, t.category,
       COUNT(*) FILTER (WHERE a.status = 'AVAILABLE' AND a.expiry_date > now())  AS available,
       COUNT(*) FILTER (WHERE a.status = 'RESERVED')                              AS reserved,
       COUNT(*) FILTER (WHERE a.status = 'AVAILABLE'
                          AND a.expiry_date BETWEEN now() AND now() + INTERVAL '48 hours') AS expiring_48h,
       COUNT(*) FILTER (WHERE a.blood_group = 'O-' AND a.status = 'AVAILABLE'
                          AND a.expiry_date > now())                              AS o_negative_available
FROM   hospitals h
JOIN   emergency_assets a ON a.hospital_id = h.hospital_id
JOIN   asset_types      t ON t.asset_type_id = a.asset_type_id
GROUP  BY h.hospital_id, h.hospital_name, h.city, t.category;

-- Transfer ledger with readable names
CREATE OR REPLACE VIEW v_transfer_dashboard AS
SELECT tt.transaction_id, tt.status, tt.priority, tt.request_time, tt.allocated_time,
       tt.dispatched_time, tt.completed_time, tt.eta_minutes, tt.route_path, tt.route_source,
       a.asset_code, a.asset_name, a.blood_group, t.asset_type_name, t.category,
       rq.hospital_name AS requesting_hospital, sp.hospital_name AS supplying_hospital,
       r.department, r.contact_person, r.urgency_level
FROM   transfer_transactions tt
JOIN   emergency_assets     a  ON a.asset_id       = tt.asset_id
JOIN   asset_types          t  ON t.asset_type_id  = a.asset_type_id
JOIN   hospitals            rq ON rq.hospital_id   = tt.requesting_hospital_id
JOIN   hospitals            sp ON sp.hospital_id   = tt.supplying_hospital_id
JOIN   requesting_hospitals r  ON r.request_id     = tt.request_id;

-- Network-wide scarcity report: how many compatible O- units exist per region
CREATE OR REPLACE VIEW v_scarcity_report AS
SELECT t.asset_type_name, a.blood_group, h.city,
       COUNT(*) AS units,
       MIN(a.expiry_date) AS earliest_expiry
FROM   emergency_assets a
JOIN   asset_types t ON t.asset_type_id = a.asset_type_id
JOIN   hospitals   h ON h.hospital_id   = a.hospital_id
WHERE  a.status = 'AVAILABLE' AND a.expiry_date > now()
GROUP  BY ROLLUP (t.asset_type_name, a.blood_group, h.city);
