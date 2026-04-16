-- Example 3: Diff two resolved worlds
--
-- Demonstrates using ATTACH to compare two policy databases.
-- Run with: duckdb < examples/03-diff-two-worlds.sql
--
-- In practice you'd have before.duckdb and after.duckdb as separate files.
-- Here we simulate by creating two schemas in one database.

-- ============================================================================
-- Simulate "before" world
-- ============================================================================

CREATE SCHEMA before;

CREATE TABLE before.entities (
    id INTEGER PRIMARY KEY, taxon VARCHAR, type_name VARCHAR, entity_id VARCHAR
);
CREATE TABLE before.resolved (
    entity_id INTEGER, property_name VARCHAR, property_value VARCHAR,
    source_file VARCHAR, source_line INTEGER
);

INSERT INTO before.entities VALUES
    (1, 'world', 'file', 'src/auth.py'),
    (2, 'world', 'file', 'src/util.py'),
    (3, 'capability', 'tool', 'Bash');

INSERT INTO before.resolved VALUES
    (1, 'editable', 'true',  'v1.umw', 1),
    (2, 'editable', 'true',  'v1.umw', 1),
    (3, 'allow',    'true',  'v1.umw', 5);


-- ============================================================================
-- Simulate "after" world (auth.py locked down, new file added, Bash denied)
-- ============================================================================

CREATE SCHEMA after;

CREATE TABLE after.entities (
    id INTEGER PRIMARY KEY, taxon VARCHAR, type_name VARCHAR, entity_id VARCHAR
);
CREATE TABLE after.resolved (
    entity_id INTEGER, property_name VARCHAR, property_value VARCHAR,
    source_file VARCHAR, source_line INTEGER
);

INSERT INTO after.entities VALUES
    (1, 'world', 'file', 'src/auth.py'),
    (2, 'world', 'file', 'src/util.py'),
    (3, 'capability', 'tool', 'Bash'),
    (4, 'world', 'file', 'src/new.py');

INSERT INTO after.resolved VALUES
    (1, 'editable', 'false', 'v2.umw', 3),   -- CHANGED: true → false
    (2, 'editable', 'true',  'v2.umw', 1),   -- unchanged
    (3, 'allow',    'false', 'v2.umw', 7),   -- CHANGED: true → false
    (4, 'editable', 'true',  'v2.umw', 1);   -- ADDED


-- ============================================================================
-- DIFF QUERIES
-- ============================================================================

-- Added properties (entity+property in after but not before)
SELECT 'ADDED' AS change,
    ae.entity_id AS entity, ar.property_name, ar.property_value AS new_value,
    ar.source_file, ar.source_line,
FROM after.resolved ar
JOIN after.entities ae ON ar.entity_id = ae.id
LEFT JOIN before.resolved br
    ON ar.entity_id = br.entity_id AND ar.property_name = br.property_name
WHERE br.entity_id IS NULL;

-- Removed properties (in before but not after)
SELECT 'REMOVED' AS change,
    be.entity_id AS entity, br.property_name, br.property_value AS old_value,
    br.source_file, br.source_line,
FROM before.resolved br
JOIN before.entities be ON br.entity_id = be.id
LEFT JOIN after.resolved ar
    ON br.entity_id = ar.entity_id AND br.property_name = ar.property_name
WHERE ar.entity_id IS NULL;

-- Changed properties
SELECT 'CHANGED' AS change,
    ae.entity_id AS entity, ar.property_name,
    br.property_value AS old_value, ar.property_value AS new_value,
    br.source_line AS old_line, ar.source_line AS new_line,
FROM after.resolved ar
JOIN after.entities ae ON ar.entity_id = ae.id
JOIN before.resolved br ON ar.entity_id = br.entity_id AND ar.property_name = br.property_name
WHERE ar.property_value != br.property_value;

-- Widenings (security-relevant: false→true on permissions)
SELECT 'WIDENING' AS alert,
    ae.entity_id AS entity, ar.property_name,
    br.property_value AS was, ar.property_value AS now,
FROM after.resolved ar
JOIN after.entities ae ON ar.entity_id = ae.id
JOIN before.resolved br ON ar.entity_id = br.entity_id AND ar.property_name = br.property_name
WHERE br.property_value = 'false' AND ar.property_value = 'true'
  AND ar.property_name IN ('editable', 'allow', 'visible');

-- Expected output:
-- ADDED   | src/new.py    | editable | true
-- CHANGED | src/auth.py   | editable | true  → false
-- CHANGED | Bash          | allow    | true  → false
-- (no widenings — both changes are tightenings)
