-- =====================================================================
-- 02_functions_triggers.sql  —  Stored functions, procedures, triggers
-- =====================================================================
SET search_path TO medmatch, public;

-- ---------------------------------------------------------------------
-- FUNCTION: great-circle distance between two coordinates (km)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION haversine_km(lat1 NUMERIC, lon1 NUMERIC, lat2 NUMERIC, lon2 NUMERIC)
RETURNS NUMERIC LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ROUND((6371 * 2 * ASIN(SQRT(
        POWER(SIN(RADIANS(lat2 - lat1) / 2), 2) +
        COS(RADIANS(lat1)) * COS(RADIANS(lat2)) *
        POWER(SIN(RADIANS(lon2 - lon1) / 2), 2)
    )))::NUMERIC, 2);
$$;

-- ---------------------------------------------------------------------
-- FUNCTION: ABO/Rh red-cell compatibility (can donor give to recipient?)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION blood_compatible(donor VARCHAR, recipient VARCHAR)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN donor IS NULL OR recipient IS NULL THEN FALSE
        -- Rh: an Rh- recipient may only receive Rh- blood
        WHEN right(recipient,1) = '-' AND right(donor,1) = '+' THEN FALSE
        -- ABO: donor antigens must be a subset of recipient antigens
        ELSE (replace(replace(donor,'+',''),'-','') = 'O')
          OR (replace(replace(recipient,'+',''),'-','') = 'AB')
          OR (replace(replace(donor,'+',''),'-','') = replace(replace(recipient,'+',''),'-',''))
    END;
$$;

-- ---------------------------------------------------------------------
-- FUNCTION: find matching, unexpired units ordered by distance.
-- Uses idx_asset_lookup (B+ tree) / idx_asset_hla_hash (hash).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_search_assets(
    p_asset_type_id   INT,
    p_requester_id    INT,
    p_blood_group     VARCHAR DEFAULT NULL,   -- recipient blood group
    p_hla_type        VARCHAR DEFAULT NULL,
    p_max_distance_km NUMERIC DEFAULT NULL,
    p_limit           INT     DEFAULT 25
)
RETURNS TABLE (
    asset_id BIGINT, asset_code VARCHAR, asset_name VARCHAR, blood_group VARCHAR,
    hla_type VARCHAR, expiry_date TIMESTAMPTZ, hours_to_expiry NUMERIC,
    hospital_id INT, hospital_name VARCHAR, city VARCHAR, distance_km NUMERIC
)
LANGUAGE sql STABLE AS $$
    SELECT a.asset_id, a.asset_code, a.asset_name, a.blood_group, a.hla_type, a.expiry_date,
           ROUND((EXTRACT(EPOCH FROM (a.expiry_date - now())) / 3600)::NUMERIC, 1),
           h.hospital_id, h.hospital_name, h.city,
           haversine_km(r.latitude, r.longitude, h.latitude, h.longitude) AS dist
    FROM   emergency_assets a
    JOIN   hospitals h ON h.hospital_id = a.hospital_id
    JOIN   hospitals r ON r.hospital_id = p_requester_id
    WHERE  a.asset_type_id = p_asset_type_id
      AND  a.expiry_date   > now()
      AND  a.status        = 'AVAILABLE'
      AND  a.hospital_id  <> p_requester_id
      AND  h.is_active
      AND  (p_blood_group IS NULL OR blood_compatible(a.blood_group, p_blood_group))
      AND  (p_hla_type    IS NULL OR a.hla_type = p_hla_type)
      AND  (p_max_distance_km IS NULL
            OR haversine_km(r.latitude, r.longitude, h.latitude, h.longitude) <= p_max_distance_km)
    ORDER  BY dist, a.expiry_date      -- nearest first, then use-soonest (reduces wastage)
    LIMIT  p_limit;
$$;

-- ---------------------------------------------------------------------
-- FUNCTION: mark expired stock (run periodically / on startup)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_expire_assets()
RETURNS INT LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
    UPDATE emergency_assets
       SET status = 'EXPIRED'
     WHERE status = 'AVAILABLE' AND expiry_date <= now();
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
        INSERT INTO audit_logs(action, details)
        VALUES ('AUTO_EXPIRE', jsonb_build_object('units_expired', n));
    END IF;
    RETURN n;
END $$;

-- ---------------------------------------------------------------------
-- PROCEDURE: dispatch an allocated transfer
-- ---------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_dispatch_transfer(p_txn BIGINT)
LANGUAGE plpgsql AS $$
DECLARE v_asset BIGINT;
BEGIN
    SELECT asset_id INTO v_asset FROM transfer_transactions
     WHERE transaction_id = p_txn AND status = 'ALLOCATED' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Transaction % is not in ALLOCATED state', p_txn USING ERRCODE = 'P0001';
    END IF;
    PERFORM 1 FROM emergency_assets WHERE asset_id = v_asset FOR UPDATE;
    UPDATE transfer_transactions SET status = 'IN_TRANSIT', dispatched_time = now() WHERE transaction_id = p_txn;
    UPDATE emergency_assets SET status = 'IN_TRANSIT' WHERE asset_id = v_asset;
END $$;

-- ---------------------------------------------------------------------
-- PROCEDURE: confirm delivery — ownership moves to the requester
-- ---------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_complete_transfer(p_txn BIGINT)
LANGUAGE plpgsql AS $$
DECLARE v_asset BIGINT; v_req INT;
BEGIN
    SELECT asset_id, requesting_hospital_id INTO v_asset, v_req
      FROM transfer_transactions
     WHERE transaction_id = p_txn AND status IN ('ALLOCATED','IN_TRANSIT') FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Transaction % cannot be completed from its current state', p_txn USING ERRCODE = 'P0001';
    END IF;
    PERFORM 1 FROM emergency_assets WHERE asset_id = v_asset FOR UPDATE;
    UPDATE transfer_transactions SET status = 'COMPLETED', completed_time = now() WHERE transaction_id = p_txn;
    UPDATE emergency_assets SET status = 'DELIVERED', hospital_id = v_req WHERE asset_id = v_asset;
END $$;

-- ---------------------------------------------------------------------
-- PROCEDURE: cancel — the unit goes back into the shared pool
-- ---------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_cancel_transfer(p_txn BIGINT, p_reason TEXT DEFAULT NULL)
LANGUAGE plpgsql AS $$
DECLARE v_asset BIGINT;
BEGIN
    SELECT asset_id INTO v_asset FROM transfer_transactions
     WHERE transaction_id = p_txn AND status IN ('REQUESTED','ALLOCATED') FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Transaction % cannot be cancelled from its current state', p_txn USING ERRCODE = 'P0001';
    END IF;
    PERFORM 1 FROM emergency_assets WHERE asset_id = v_asset FOR UPDATE;
    UPDATE transfer_transactions
       SET status = 'CANCELLED', notes = COALESCE(p_reason, notes)
     WHERE transaction_id = p_txn;
    UPDATE emergency_assets
       SET status = CASE WHEN expiry_date > now() THEN 'AVAILABLE' ELSE 'EXPIRED' END
     WHERE asset_id = v_asset;
END $$;

-- =====================================================================
-- TRIGGERS
-- =====================================================================

-- T1. Asset guard: bump version, block reserving expired stock,
--     enforce legal status transitions.
CREATE OR REPLACE FUNCTION trg_asset_guard_fn() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NEW.status = 'RESERVED' AND NEW.expiry_date <= now() THEN
            RAISE EXCEPTION 'Asset % has expired and cannot be reserved', OLD.asset_code
                  USING ERRCODE = 'P0002';
        END IF;
        IF NOT (
            (OLD.status = 'AVAILABLE'  AND NEW.status IN ('RESERVED','EXPIRED','DISCARDED')) OR
            (OLD.status = 'RESERVED'   AND NEW.status IN ('IN_TRANSIT','DELIVERED','AVAILABLE','EXPIRED')) OR
            (OLD.status = 'IN_TRANSIT' AND NEW.status IN ('DELIVERED')) OR
            (OLD.status = 'EXPIRED'    AND NEW.status IN ('DISCARDED'))
        ) THEN
            RAISE EXCEPTION 'Illegal asset status transition % -> % for %',
                  OLD.status, NEW.status, OLD.asset_code USING ERRCODE = 'P0003';
        END IF;
    END IF;
    NEW.version    := OLD.version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE TRIGGER trg_asset_guard
BEFORE UPDATE ON emergency_assets
FOR EACH ROW EXECUTE FUNCTION trg_asset_guard_fn();

-- T2. Transaction state machine.
CREATE OR REPLACE FUNCTION trg_txn_state_fn() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'REQUESTED'  AND NEW.status IN ('ALLOCATED','CANCELLED')) OR
        (OLD.status = 'ALLOCATED'  AND NEW.status IN ('IN_TRANSIT','COMPLETED','CANCELLED')) OR
        (OLD.status = 'IN_TRANSIT' AND NEW.status IN ('COMPLETED'))
    ) THEN
        RAISE EXCEPTION 'Illegal transaction transition % -> %', OLD.status, NEW.status
              USING ERRCODE = 'P0003';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_txn_state
BEFORE UPDATE ON transfer_transactions
FOR EACH ROW EXECUTE FUNCTION trg_txn_state_fn();

-- T3. Automatic audit trail for every transaction insert / status change.
CREATE OR REPLACE FUNCTION trg_txn_audit_fn() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO audit_logs(transaction_id, asset_id, action, old_status, new_status, details)
        VALUES (NEW.transaction_id, NEW.asset_id, 'TXN_CREATED', NULL, NEW.status,
                jsonb_build_object('requester', NEW.requesting_hospital_id,
                                   'supplier',  NEW.supplying_hospital_id,
                                   'priority',  NEW.priority,
                                   'wait_die_ts', NEW.wait_die_ts));
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO audit_logs(transaction_id, asset_id, action, old_status, new_status, details)
        VALUES (NEW.transaction_id, NEW.asset_id, 'STATUS_CHANGE', OLD.status, NEW.status,
                jsonb_build_object('notes', NEW.notes));
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_txn_audit
AFTER INSERT OR UPDATE OF status ON transfer_transactions
FOR EACH ROW EXECUTE FUNCTION trg_txn_audit_fn();

-- T4. Audit log is immutable (append-only ledger).
CREATE OR REPLACE FUNCTION trg_audit_immutable_fn() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only; % is not allowed', TG_OP USING ERRCODE = 'P0004';
END $$;

CREATE TRIGGER trg_audit_immutable
BEFORE UPDATE OR DELETE ON audit_logs
FOR EACH ROW EXECUTE FUNCTION trg_audit_immutable_fn();

-- T5. Keep transport graph's last_updated fresh when routes change.
CREATE OR REPLACE FUNCTION trg_route_touch_fn() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    UPDATE transport_network_graph SET last_updated = now()
     WHERE graph_id = COALESCE(NEW.graph_id, OLD.graph_id);
    RETURN NULL;
END $$;

CREATE TRIGGER trg_route_touch
AFTER INSERT OR UPDATE OR DELETE ON routes
FOR EACH ROW EXECUTE FUNCTION trg_route_touch_fn();
