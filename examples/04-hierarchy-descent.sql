-- Example 4: Structural hierarchy and descendant queries
--
-- umwelt source:
--   world#dev {
--     dir[name="src"] file { editable: true; }
--     dir[name="tests"] file { editable: false; }
--   }
--
-- Demonstrates:
--   - Adjacency list (parent_id) as source of truth
--   - Closure table for fast descendant queries
--   - Structural descendant selectors compiled to closure joins
--
-- Run with: duckdb < examples/04-hierarchy-descent.sql

-- ============================================================================
-- Schema (minimal, self-contained)
-- ============================================================================

CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    taxon       VARCHAR NOT NULL,
    type_name   VARCHAR NOT NULL,
    entity_id   VARCHAR,
    classes     VARCHAR[],
    attributes  MAP(VARCHAR, VARCHAR),
    parent_id   INTEGER REFERENCES entities(id),
    depth       INTEGER DEFAULT 0,
);

CREATE TABLE entity_closure (
    ancestor_id   INTEGER NOT NULL,
    descendant_id INTEGER NOT NULL,
    depth         INTEGER NOT NULL,
    PRIMARY KEY (ancestor_id, descendant_id),
);

CREATE TABLE cascade_candidates (
    entity_id       INTEGER NOT NULL,
    property_name   VARCHAR NOT NULL,
    property_value  VARCHAR NOT NULL,
    specificity     INTEGER[] NOT NULL,
    rule_index      INTEGER NOT NULL,
    selector_text   VARCHAR,
    is_winner       BOOLEAN DEFAULT false,
);


-- ============================================================================
-- Entities: world#dev → dir → file hierarchy
-- ============================================================================

-- world#dev (root, depth 0)
INSERT INTO entities VALUES
    (1, 'world', 'world', 'dev', NULL,
     MAP {'name': 'dev'}, NULL, 0);

-- dir "src" under world#dev (depth 1)
INSERT INTO entities VALUES
    (2, 'world', 'dir', 'src', NULL,
     MAP {'path': 'src', 'name': 'src'}, 1, 1);

-- dir "tests" under world#dev (depth 1)
INSERT INTO entities VALUES
    (3, 'world', 'dir', 'tests', NULL,
     MAP {'path': 'tests', 'name': 'tests'}, 1, 1);

-- Files under src/ (depth 2)
INSERT INTO entities VALUES
    (10, 'world', 'file', 'src/auth.py', NULL,
     MAP {'path': 'src/auth.py', 'name': 'auth.py', 'language': 'python'}, 2, 2),
    (11, 'world', 'file', 'src/util.py', NULL,
     MAP {'path': 'src/util.py', 'name': 'util.py', 'language': 'python'}, 2, 2),
    (12, 'world', 'file', 'src/README.md', NULL,
     MAP {'path': 'src/README.md', 'name': 'README.md'}, 2, 2);

-- Files under tests/ (depth 2)
INSERT INTO entities VALUES
    (20, 'world', 'file', 'tests/test_auth.py', NULL,
     MAP {'path': 'tests/test_auth.py', 'name': 'test_auth.py', 'language': 'python'}, 3, 2),
    (21, 'world', 'file', 'tests/conftest.py', NULL,
     MAP {'path': 'tests/conftest.py', 'name': 'conftest.py', 'language': 'python'}, 3, 2);

-- A standalone file (no parent dir — depth 0 under world)
INSERT INTO entities VALUES
    (30, 'world', 'file', 'README.md', NULL,
     MAP {'path': 'README.md', 'name': 'README.md'}, 1, 1);


-- ============================================================================
-- Build the closure table (transitive closure of parent_id)
-- ============================================================================

INSERT INTO entity_closure
WITH RECURSIVE closure(ancestor_id, descendant_id, depth) AS (
    -- Base: every entity is its own ancestor at depth 0
    SELECT id, id, 0 FROM entities
    UNION ALL
    -- Step: if A is an ancestor of B, and B is the parent of C,
    -- then A is an ancestor of C at depth+1
    SELECT c.ancestor_id, e.id, c.depth + 1
    FROM closure c
    JOIN entities e ON e.parent_id = c.descendant_id
)
SELECT DISTINCT ancestor_id, descendant_id, depth FROM closure;


-- ============================================================================
-- Verify the closure
-- ============================================================================

-- Q0: Show the full closure
SELECT
    a.type_name || COALESCE('#' || a.entity_id, '') AS ancestor,
    d.type_name || COALESCE('#' || d.entity_id, '') AS descendant,
    ec.depth,
FROM entity_closure ec
JOIN entities a ON ec.ancestor_id = a.id
JOIN entities d ON ec.descendant_id = d.id
WHERE ec.depth > 0  -- skip self-references for readability
ORDER BY a.id, ec.depth, d.id;

-- Expected (partial):
-- world#dev   | dir#src            | 1
-- world#dev   | dir#tests          | 1
-- world#dev   | file#src/auth.py   | 2
-- world#dev   | file#src/util.py   | 2
-- world#dev   | file#tests/test_auth.py | 2
-- dir#src     | file#src/auth.py   | 1
-- dir#src     | file#src/util.py   | 1
-- dir#tests   | file#tests/test_auth.py | 1


-- ============================================================================
-- Compile structural descendant selectors to closure joins
-- ============================================================================

-- CSS: dir[name="src"] file { editable: true; }
-- Meaning: "any file that is a descendant of dir named 'src'"
-- Compiled to: join entity_closure to find files under dir#src

-- Rule 0: dir[name="src"] file { editable: true; }
-- Selector: structural descendant (same taxon, world→world)
INSERT INTO cascade_candidates (entity_id, property_name, property_value, specificity, rule_index, selector_text)
SELECT
    file_e.id,
    'editable',
    'true',
    [1, 0, 102, 0, 0, 0, 0, 0],  -- dir[name="src"](attr+type=101) + file(type=1) = world_w=102
    0,
    'dir[name="src"] file',
FROM entities dir_e
JOIN entity_closure ec ON ec.ancestor_id = dir_e.id
JOIN entities file_e ON ec.descendant_id = file_e.id
WHERE dir_e.type_name = 'dir'
  AND dir_e.attributes['name'] = 'src'
  AND file_e.type_name = 'file'
  AND ec.depth > 0;  -- not self

-- Rule 1: dir[name="tests"] file { editable: false; }
INSERT INTO cascade_candidates (entity_id, property_name, property_value, specificity, rule_index, selector_text)
SELECT
    file_e.id,
    'editable',
    'false',
    [1, 0, 102, 0, 0, 0, 0, 0],
    1,
    'dir[name="tests"] file',
FROM entities dir_e
JOIN entity_closure ec ON ec.ancestor_id = dir_e.id
JOIN entities file_e ON ec.descendant_id = file_e.id
WHERE dir_e.type_name = 'dir'
  AND dir_e.attributes['name'] = 'tests'
  AND file_e.type_name = 'file'
  AND ec.depth > 0;


-- ============================================================================
-- Resolve cascade
-- ============================================================================

UPDATE cascade_candidates cc
SET is_winner = true
WHERE (cc.entity_id, cc.property_name, cc.specificity, cc.rule_index) IN (
    SELECT DISTINCT ON (entity_id, property_name)
        entity_id, property_name, specificity, rule_index
    FROM cascade_candidates
    ORDER BY entity_id, property_name, specificity DESC, rule_index DESC
);


-- ============================================================================
-- Results
-- ============================================================================

-- Q1: Which files are editable?
SELECT e.entity_id AS file_path, cc.property_value AS editable, cc.selector_text
FROM cascade_candidates cc
JOIN entities e ON cc.entity_id = e.id
WHERE cc.is_winner = true
  AND cc.property_name = 'editable'
ORDER BY e.entity_id;

-- Expected:
-- src/auth.py          | true  | dir[name="src"] file
-- src/util.py          | true  | dir[name="src"] file
-- src/README.md        | true  | dir[name="src"] file
-- tests/test_auth.py   | false | dir[name="tests"] file
-- tests/conftest.py    | false | dir[name="tests"] file
-- (README.md at root has no candidate — not under any matched dir)


-- Q2: "What's inside world#dev?" (all descendants)
SELECT e.type_name, e.entity_id, ec.depth
FROM entity_closure ec
JOIN entities e ON ec.descendant_id = e.id
WHERE ec.ancestor_id = 1  -- world#dev
  AND ec.depth > 0
ORDER BY ec.depth, e.type_name, e.entity_id;


-- Q3: "What's the ancestry chain for tests/test_auth.py?"
SELECT e.type_name || COALESCE('#' || e.entity_id, '') AS entity, ec.depth
FROM entity_closure ec
JOIN entities e ON ec.ancestor_id = e.id
WHERE ec.descendant_id = 20  -- tests/test_auth.py
ORDER BY ec.depth DESC;

-- Expected:
-- world#dev              | 2
-- dir#tests              | 1
-- file#tests/test_auth.py | 0  (self)


-- Q4: "Are there files that aren't under any dir?" (hierarchy gap detection)
SELECT e.entity_id
FROM entities e
WHERE e.type_name = 'file'
  AND NOT EXISTS (
    SELECT 1 FROM entity_closure ec
    JOIN entities ancestor ON ec.ancestor_id = ancestor.id
    WHERE ec.descendant_id = e.id
      AND ancestor.type_name = 'dir'
      AND ec.depth > 0
  );

-- Expected: README.md (directly under world, no dir parent)
