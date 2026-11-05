-- =====================================================================
-- MedMatch : Inter-Hospital Rare Resource Logistics Network
-- 01_schema.sql  —  Relational schema (3NF / BCNF)
-- Mirrors the E-R model presented in Review 1.
-- =====================================================================

DROP SCHEMA IF EXISTS medmatch CASCADE;
CREATE SCHEMA medmatch;
SET search_path TO medmatch, public;

-- ---------------------------------------------------------------------
-- HOSPITALS  (ER: HOSPITALS)
-- Every attribute depends only on hospital_id  -> 3NF/BCNF.
-- ---------------------------------------------------------------------
CREATE TABLE hospitals (
    hospital_id     SERIAL          PRIMARY KEY,
    hospital_code   VARCHAR(12)     NOT NULL UNIQUE,
    hospital_name   VARCHAR(150)    NOT NULL,
    facility_type   VARCHAR(20)     NOT NULL
                    CHECK (facility_type IN ('GOVERNMENT','PRIVATE','BLOOD_BANK','TRANSPLANT_CENTER')),
    email           VARCHAR(120)    NOT NULL UNIQUE
                    CHECK (email ~* '^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$'),
    phone           VARCHAR(15)     NOT NULL CHECK (phone ~ '^[0-9+ -]{8,15}$'),
    address         VARCHAR(250)    NOT NULL,
    city            VARCHAR(60)     NOT NULL,
    state           VARCHAR(60)     NOT NULL,
    latitude        NUMERIC(9,6)    NOT NULL CHECK (latitude  BETWEEN -90  AND 90),
    longitude       NUMERIC(9,6)    NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- ASSET_TYPES  (ER: ASSET_TYPES)
-- Decomposed out of EMERGENCY_ASSETS:  asset_type_id -> {category,
-- storage range, shelf life}.  Keeping these on every asset row would be
-- a transitive dependency (update anomaly)  -> BCNF.
-- ---------------------------------------------------------------------
CREATE TABLE asset_types (
    asset_type_id       SERIAL       PRIMARY KEY,
    asset_type_name     VARCHAR(60)  NOT NULL UNIQUE,
    category            VARCHAR(15)  NOT NULL
                        CHECK (category IN ('BLOOD','ORGAN','ANTIVENOM')),
    storage_temp_min_c  NUMERIC(5,1) NOT NULL,
    storage_temp_max_c  NUMERIC(5,1) NOT NULL,
    shelf_life_days     NUMERIC(6,2) NOT NULL CHECK (shelf_life_days > 0),
    requires_cold_chain BOOLEAN      NOT NULL DEFAULT TRUE,
    CHECK (storage_temp_min_c <= storage_temp_max_c)
);

-- ---------------------------------------------------------------------
-- EMERGENCY_ASSETS  (ER: EMERGENCY_ASSETS)
-- One row = one physical, reservable unit.
-- ---------------------------------------------------------------------
CREATE TABLE emergency_assets (
    asset_id        BIGSERIAL    PRIMARY KEY,
    asset_code      VARCHAR(20)  NOT NULL UNIQUE,
    asset_name      VARCHAR(100) NOT NULL,
    asset_type_id   INT          NOT NULL REFERENCES asset_types(asset_type_id) ON DELETE RESTRICT,
    hospital_id     INT          NOT NULL REFERENCES hospitals(hospital_id)     ON DELETE RESTRICT,
    blood_group     VARCHAR(3)   CHECK (blood_group IN ('O-','O+','A-','A+','B-','B+','AB-','AB+')),
    hla_type        VARCHAR(40),
    collected_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expiry_date     TIMESTAMPTZ  NOT NULL,
    status          VARCHAR(12)  NOT NULL DEFAULT 'AVAILABLE'
                    CHECK (status IN ('AVAILABLE','RESERVED','IN_TRANSIT','DELIVERED','EXPIRED','DISCARDED')),
    version         INT          NOT NULL DEFAULT 1,        -- bumped by trigger on every change
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (expiry_date > collected_at)
);

-- ---------------------------------------------------------------------
-- ASSET_ATTRIBUTES  (ER: ASSET_ATTRIBUTES)  — weak entity, 1 asset : N attributes
-- Extra biological / physical properties (e.g. Rh antibody titre, organ weight).
-- ---------------------------------------------------------------------
CREATE TABLE asset_attributes (
    attribute_id    BIGSERIAL    PRIMARY KEY,
    asset_id        BIGINT       NOT NULL REFERENCES emergency_assets(asset_id) ON DELETE CASCADE,
    attribute_name  VARCHAR(60)  NOT NULL,
    attribute_type  VARCHAR(12)  NOT NULL CHECK (attribute_type IN ('BIOLOGICAL','PHYSICAL')),
    attribute_value VARCHAR(100) NOT NULL,
    uom             VARCHAR(20),
    UNIQUE (asset_id, attribute_name)
);

-- ---------------------------------------------------------------------
-- REQUESTING_HOSPITALS  (ER: REQUESTING_HOSPITALS)
-- The clinical request raised by a hospital department.
-- ---------------------------------------------------------------------
CREATE TABLE requesting_hospitals (
    request_id      BIGSERIAL    PRIMARY KEY,
    hospital_id     INT          NOT NULL REFERENCES hospitals(hospital_id),
    department      VARCHAR(60)  NOT NULL,
    contact_person  VARCHAR(100) NOT NULL,
    contact_phone   VARCHAR(15)  NOT NULL,
    urgency_level   VARCHAR(10)  NOT NULL CHECK (urgency_level IN ('CRITICAL','HIGH','MEDIUM','LOW')),
    request_date    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- TRANSPORT_NETWORK_GRAPH + ROUTES  (ER: TRANSPORT_NETWORK_GRAPH, ROUTES)
-- Relational source of truth for road/air corridors.  Synced into Neo4j
-- as (:Hospital)-[:ROUTE_TO]->(:Hospital) for path-finding.
-- ---------------------------------------------------------------------
CREATE TABLE transport_network_graph (
    graph_id        SERIAL       PRIMARY KEY,
    description     VARCHAR(200) NOT NULL,
    region          VARCHAR(60)  NOT NULL,
    control_phone   VARCHAR(15)  NOT NULL,
    last_updated    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE routes (
    route_id         SERIAL       PRIMARY KEY,
    graph_id         INT          NOT NULL REFERENCES transport_network_graph(graph_id) ON DELETE CASCADE,
    route_name       VARCHAR(120) NOT NULL,
    from_hospital_id INT          NOT NULL REFERENCES hospitals(hospital_id),
    to_hospital_id   INT          NOT NULL REFERENCES hospitals(hospital_id),
    transport_type   VARCHAR(5)   NOT NULL CHECK (transport_type IN ('ROAD','AIR')),
    distance_km      NUMERIC(7,2) NOT NULL CHECK (distance_km > 0),
    eta_minutes      INT          NOT NULL CHECK (eta_minutes > 0),
    temp_controlled  BOOLEAN      NOT NULL DEFAULT FALSE,
    conditions       VARCHAR(120),
    status           VARCHAR(8)   NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','CLOSED')),
    CHECK (from_hospital_id <> to_hospital_id),
    UNIQUE (from_hospital_id, to_hospital_id, transport_type)
);

-- ---------------------------------------------------------------------
-- TRANSFER_TRANSACTIONS  (ER: TRANSFER_TRANSACTIONS)
-- The reservation ledger.  Two FKs to HOSPITALS (requests / supplies).
-- ---------------------------------------------------------------------
CREATE TABLE transfer_transactions (
    transaction_id          BIGSERIAL    PRIMARY KEY,
    asset_id                BIGINT       NOT NULL REFERENCES emergency_assets(asset_id),
    request_id              BIGINT       NOT NULL REFERENCES requesting_hospitals(request_id),
    requesting_hospital_id  INT          NOT NULL REFERENCES hospitals(hospital_id),
    supplying_hospital_id   INT          NOT NULL REFERENCES hospitals(hospital_id),
    priority                SMALLINT     NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    status                  VARCHAR(12)  NOT NULL DEFAULT 'REQUESTED'
                            CHECK (status IN ('REQUESTED','ALLOCATED','IN_TRANSIT','COMPLETED','CANCELLED')),
    request_time            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    required_by             TIMESTAMPTZ,
    allocated_time          TIMESTAMPTZ,
    dispatched_time         TIMESTAMPTZ,
    completed_time          TIMESTAMPTZ,
    route_path              INT[],                 -- hospital_ids along the chosen path
    route_source            VARCHAR(10),           -- 'neo4j' or 'fallback'
    eta_minutes             INT CHECK (eta_minutes IS NULL OR eta_minutes >= 0),
    wait_die_ts             BIGINT,                -- transaction timestamp used by Wait-Die
    notes                   TEXT,
    CHECK (requesting_hospital_id <> supplying_hospital_id),
    CHECK (completed_time IS NULL OR completed_time >= request_time)
);

-- Hard guarantee: at most ONE live claim per physical unit, even if
-- application-level locking were bypassed.
CREATE UNIQUE INDEX uq_one_active_claim_per_asset
    ON transfer_transactions(asset_id)
    WHERE status IN ('REQUESTED','ALLOCATED','IN_TRANSIT');

-- ---------------------------------------------------------------------
-- AUDIT_LOGS  (ER: AUDIT_LOGS)  — append-only, written by trigger.
-- ---------------------------------------------------------------------
CREATE TABLE audit_logs (
    log_id          BIGSERIAL    PRIMARY KEY,
    transaction_id  BIGINT       REFERENCES transfer_transactions(transaction_id),
    asset_id        BIGINT       REFERENCES emergency_assets(asset_id),
    action          VARCHAR(30)  NOT NULL,
    old_status      VARCHAR(12),
    new_status      VARCHAR(12),
    log_time        TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    db_user         VARCHAR(60)  NOT NULL DEFAULT current_user,
    details         JSONB
);
