-- Example 1: File permissions (world-axis)
--
-- umwelt source:
--   file[path^="src/"]    { editable: true; }
--   file[path^="tests/"]  { editable: false; }
--   file                   { editable: false; }
--
-- This example is self-contained — creates tables, populates, resolves.
-- Run with: duckdb < examples/01-file-permissions.sql

-- ============================================================================
-- Minimal schema (inline for self-containment)
-- ============================================================================

CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    taxon       VARCHAR NOT NULL,
    type_name   VARCHAR NOT NULL,
    entity_id   VARCHAR,
    attrs       JSON,
);

CREATE TABLE cascade_candidates (
    entity_id       INTEGER NOT NULL,
    property_name   VARCHAR NOT NULL,
    property_value  VARCHAR NOT NULL,
    specificity     INTEGER[] NOT NULL,
    rule_index      INTEGER NOT NULL,
    source_file     VARCHAR,
    source_line     INTEGER,
    is_winner       BOOLEAN DEFAULT false,
);

-- ============================================================================
-- Entities (what the filesystem matcher found)
-- ============================================================================

INSERT INTO entities VALUES
    (1, 'world', 'file', 'src/auth.py',       '{"path": "src/auth.py"}'),
    (2, 'world', 'file', 'src/util.py',       '{"path": "src/util.py"}'),
    (3, 'world', 'file', 'tests/test_auth.py','{"path": "tests/test_auth.py"}'),
    (4, 'world', 'file', 'README.md',         '{"path": "README.md"}');

-- ============================================================================
-- Cascade candidates (what the selector matcher produced)
-- ============================================================================

-- Rule 0: file[path^="src/"] { editable: true; }       specificity: [1, 0, 101, ...]
-- Rule 1: file[path^="tests/"] { editable: false; }    specificity: [1, 0, 101, ...]
-- Rule 2: file { editable: false; }                     specificity: [1, 0, 1, ...]

INSERT INTO cascade_candidates (entity_id, property_name, property_value, specificity, rule_index, source_file, source_line) VALUES
    -- src/auth.py matches rule 0 and rule 2
    (1, 'editable', 'true',  [1, 0, 101, 0, 0, 0, 0, 0], 0, 'view.umw', 1),
    (1, 'editable', 'false', [1, 0, 1, 0, 0, 0, 0, 0],   2, 'view.umw', 3),
    -- src/util.py matches rule 0 and rule 2
    (2, 'editable', 'true',  [1, 0, 101, 0, 0, 0, 0, 0], 0, 'view.umw', 1),
    (2, 'editable', 'false', [1, 0, 1, 0, 0, 0, 0, 0],   2, 'view.umw', 3),
    -- tests/test_auth.py matches rule 1 and rule 2
    (3, 'editable', 'false', [1, 0, 101, 0, 0, 0, 0, 0], 1, 'view.umw', 2),
    (3, 'editable', 'false', [1, 0, 1, 0, 0, 0, 0, 0],   2, 'view.umw', 3),
    -- README.md matches only rule 2
    (4, 'editable', 'false', [1, 0, 1, 0, 0, 0, 0, 0],   2, 'view.umw', 3);


-- ============================================================================
-- CASCADE RESOLUTION — the entire resolver in one query
-- ============================================================================

-- Mark winners: for each (entity, property), the candidate with the
-- highest (specificity DESC, rule_index DESC) wins.
UPDATE cascade_candidates cc
SET is_winner = true
WHERE (cc.entity_id, cc.property_name, cc.specificity, cc.rule_index) IN (
    SELECT DISTINCT ON (entity_id, property_name)
        entity_id, property_name, specificity, rule_index
    FROM cascade_candidates
    ORDER BY entity_id, property_name, specificity DESC, rule_index DESC
);

-- The resolved policy
CREATE VIEW resolved_properties AS
    SELECT entity_id, property_name, property_value, specificity, source_file, source_line
    FROM cascade_candidates
    WHERE is_winner = true;


-- ============================================================================
-- CONSUMER QUERIES
-- ============================================================================

-- Q1: Which files are editable?
SELECT e.entity_id AS file_path, rp.property_value AS editable
FROM resolved_properties rp
JOIN entities e ON rp.entity_id = e.id
WHERE rp.property_name = 'editable'
ORDER BY e.entity_id;

-- Expected:
-- src/auth.py          | true
-- src/util.py          | true
-- tests/test_auth.py   | false
-- README.md            | false


-- Q2: Why is src/auth.py editable? (audit / provenance query)
SELECT
    cc.property_value,
    cc.specificity,
    cc.rule_index,
    cc.source_file,
    cc.source_line,
    CASE WHEN cc.is_winner THEN '>>> WINNER <<<' ELSE '' END AS status,
FROM cascade_candidates cc
JOIN entities e ON cc.entity_id = e.id
WHERE e.entity_id = 'src/auth.py'
  AND cc.property_name = 'editable'
ORDER BY cc.specificity DESC, cc.rule_index DESC;

-- Expected:
-- true  | [1,0,101,...] | 0 | view.umw | 1 | >>> WINNER <<<
-- false | [1,0,1,...]   | 2 | view.umw | 3 |


-- Q3: Verification assertion A2 — no ties
SELECT entity_id, property_name, COUNT(*) AS winners
FROM cascade_candidates
WHERE is_winner = true
GROUP BY entity_id, property_name
HAVING winners > 1;

-- Expected: 0 rows (no ties)
