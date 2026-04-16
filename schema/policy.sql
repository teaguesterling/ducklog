-- ducklog: policy database schema
-- The relational form of a resolved umwelt view.
--
-- This schema is the IR (intermediate representation) between umwelt's
-- CSS-shaped source format and the tools that consume policy decisions.
-- umwelt parses .umw files and populates these tables; consumers query them.

-- ============================================================================
-- 1. VOCABULARY — what kinds of things exist in the policy language
-- ============================================================================

CREATE TABLE taxa (
    name            VARCHAR PRIMARY KEY,
    canonical       VARCHAR REFERENCES taxa(name),  -- non-NULL for aliases
    vsm_system      VARCHAR,  -- 'S0', 'S1', 'S2', 'S3', 'S3*', 'S4', 'S5'
    description     VARCHAR,
);

CREATE TABLE entity_types (
    name            VARCHAR NOT NULL,
    taxon           VARCHAR NOT NULL REFERENCES taxa(name),
    parent_type     VARCHAR,            -- structural parent (e.g., file's parent is dir)
    category        VARCHAR,
    description     VARCHAR,
    PRIMARY KEY (taxon, name),
);

CREATE TABLE property_types (
    name            VARCHAR NOT NULL,
    taxon           VARCHAR NOT NULL,
    entity_type     VARCHAR NOT NULL,
    value_type      VARCHAR NOT NULL,   -- 'bool', 'str', 'int', 'float', 'list'
    comparison      VARCHAR DEFAULT 'exact',  -- 'exact', '<=', '>=', 'in', 'overlap', 'pattern-in'
    description     VARCHAR,
    PRIMARY KEY (taxon, entity_type, name),
    FOREIGN KEY (taxon, entity_type) REFERENCES entity_types(taxon, name),
);


-- ============================================================================
-- 2. SOURCE — the parsed rules from the .umw file (provenance)
-- ============================================================================

CREATE SEQUENCE rule_seq START 1;
CREATE SEQUENCE selector_seq START 1;

CREATE TABLE rules (
    id              INTEGER PRIMARY KEY DEFAULT nextval('rule_seq'),
    source_file     VARCHAR,
    source_line     INTEGER,
    source_col      INTEGER,
    source_text     VARCHAR,            -- the original CSS text of this rule block
    rule_index      INTEGER NOT NULL,   -- document order (0-based)
);

CREATE TABLE selectors (
    id              INTEGER PRIMARY KEY DEFAULT nextval('selector_seq'),
    rule_id         INTEGER NOT NULL REFERENCES rules(id),
    selector_text   VARCHAR NOT NULL,   -- serialized selector string
    target_taxon    VARCHAR NOT NULL REFERENCES taxa(name),
    axis_count      INTEGER NOT NULL,   -- number of distinct axes named
    specificity     INTEGER[] NOT NULL, -- the full specificity tuple
);

CREATE TABLE selector_parts (
    selector_id     INTEGER NOT NULL REFERENCES selectors(id),
    part_index      INTEGER NOT NULL,   -- position in the compound selector (0-based)
    type_name       VARCHAR,            -- 'file', 'tool', 'mode', 'use', etc.
    taxon           VARCHAR NOT NULL REFERENCES taxa(name),
    id_value        VARCHAR,            -- #id value, nullable
    classes         VARCHAR[],          -- ['.implement', '.tdd']
    combinator      VARCHAR NOT NULL,   -- 'root', 'descendant', 'child'
    combinator_mode VARCHAR NOT NULL,   -- 'root', 'structural', 'context'
    PRIMARY KEY (selector_id, part_index),
);

CREATE TABLE selector_attrs (
    selector_id     INTEGER NOT NULL,
    part_index      INTEGER NOT NULL,
    attr_name       VARCHAR NOT NULL,
    attr_op         VARCHAR,            -- '=', '^=', '$=', '*=', '~=', '|=', NULL (presence)
    attr_value      VARCHAR,
    FOREIGN KEY (selector_id, part_index) REFERENCES selector_parts(selector_id, part_index),
);

CREATE TABLE declarations (
    rule_id         INTEGER NOT NULL REFERENCES rules(id),
    property_name   VARCHAR NOT NULL,
    property_value  VARCHAR NOT NULL,   -- always stored as string; consumers cast
    decl_index      INTEGER NOT NULL,   -- order within the rule block
    source_line     INTEGER,
    PRIMARY KEY (rule_id, property_name, decl_index),
);


-- ============================================================================
-- 3. WORLD — the entities that exist (populated by matchers at resolve time)
-- ============================================================================

CREATE SEQUENCE entity_seq START 1;

CREATE TABLE entities (
    id              INTEGER PRIMARY KEY DEFAULT nextval('entity_seq'),
    taxon           VARCHAR NOT NULL REFERENCES taxa(name),
    type_name       VARCHAR NOT NULL,
    entity_id       VARCHAR,            -- #id value (file path, tool name, etc.)
    classes         VARCHAR[],          -- class labels (for mode.implement.tdd → ['implement', 'tdd'])
    attributes      MAP(VARCHAR, VARCHAR),  -- typed attribute bag: {path: /src/auth.py, language: python}
    -- Hierarchy (adjacency list — source of truth for parent-child)
    parent_id       INTEGER REFERENCES entities(id),
    depth           INTEGER DEFAULT 0,  -- 0 = root, 1 = child of root, etc.
);

CREATE INDEX idx_entities_taxon ON entities(taxon);
CREATE INDEX idx_entities_type ON entities(type_name);
CREATE INDEX idx_entities_id ON entities(entity_id);
CREATE INDEX idx_entities_parent ON entities(parent_id);


-- ============================================================================
-- 3a. HIERARCHY — pre-computed transitive closure for fast descendant queries
-- ============================================================================

-- The closure table: one row per (ancestor, descendant) pair, including self.
-- Rebuilt when the entity set changes (after matcher population, before cascade).
CREATE TABLE entity_closure (
    ancestor_id     INTEGER NOT NULL REFERENCES entities(id),
    descendant_id   INTEGER NOT NULL REFERENCES entities(id),
    depth           INTEGER NOT NULL,   -- 0 = self, 1 = direct child, 2 = grandchild, etc.
    PRIMARY KEY (ancestor_id, descendant_id),
);

CREATE INDEX idx_closure_ancestor ON entity_closure(ancestor_id);
CREATE INDEX idx_closure_descendant ON entity_closure(descendant_id);

-- Populate the closure table from the adjacency list.
-- Run this after all entities are inserted.
--
-- This is the one place WITH RECURSIVE appears in the core schema.
-- Everything else queries the closure table directly (no recursion at query time).
CREATE OR REPLACE MACRO rebuild_closure() AS TABLE (
    WITH RECURSIVE closure(ancestor_id, descendant_id, depth) AS (
        -- Base: every entity is its own ancestor at depth 0
        SELECT id, id, 0
        FROM entities
        UNION ALL
        -- Step: walk up via parent_id
        SELECT c.ancestor_id, e.id, c.depth + 1
        FROM closure c
        JOIN entities e ON e.parent_id = c.descendant_id
    )
    SELECT * FROM closure
);

-- Convenience views over the closure table

-- All descendants of an entity (including itself)
CREATE OR REPLACE MACRO descendants(root_id) AS TABLE (
    SELECT e.*
    FROM entity_closure ec
    JOIN entities e ON e.id = ec.descendant_id
    WHERE ec.ancestor_id = root_id
    ORDER BY ec.depth
);

-- All ancestors of an entity (including itself)
CREATE OR REPLACE MACRO ancestors(entity_id) AS TABLE (
    SELECT e.*, ec.depth
    FROM entity_closure ec
    JOIN entities e ON e.id = ec.ancestor_id
    WHERE ec.descendant_id = entity_id
    ORDER BY ec.depth DESC
);

-- Direct children only
CREATE OR REPLACE MACRO children(parent_entity_id) AS TABLE (
    SELECT e.*
    FROM entities e
    WHERE e.parent_id = parent_entity_id
);


-- ============================================================================
-- 4. CASCADE — matching rules to entities, computing winners
-- ============================================================================

-- Every (entity, selector) pair where the selector matched the entity.
CREATE TABLE cascade_matches (
    entity_id       INTEGER NOT NULL REFERENCES entities(id),
    selector_id     INTEGER NOT NULL REFERENCES selectors(id),
    rule_id         INTEGER NOT NULL REFERENCES rules(id),
    PRIMARY KEY (entity_id, selector_id),
);

-- Every (entity, property) candidate: one row per rule that could set this property.
-- The `comparison` column determines HOW this candidate participates in resolution.
CREATE TABLE cascade_candidates (
    entity_id       INTEGER NOT NULL REFERENCES entities(id),
    property_name   VARCHAR NOT NULL,
    property_value  VARCHAR NOT NULL,
    comparison      VARCHAR NOT NULL DEFAULT 'exact',  -- resolution strategy
    selector_id     INTEGER NOT NULL REFERENCES selectors(id),
    rule_id         INTEGER NOT NULL REFERENCES rules(id),
    specificity     INTEGER[] NOT NULL,
    rule_index      INTEGER NOT NULL,   -- document order for tiebreaking
    source_file     VARCHAR,
    source_line     INTEGER,
);

CREATE INDEX idx_candidates_entity_prop ON cascade_candidates(entity_id, property_name);
CREATE INDEX idx_candidates_comparison ON cascade_candidates(comparison);


-- ============================================================================
-- 5. RESOLVED — comparison-aware cascade resolution
-- ============================================================================

-- Each comparison type has different resolution semantics:
--
-- | Comparison  | Strategy                          | Example                    |
-- |-------------|-----------------------------------|----------------------------|
-- | exact       | Highest specificity wins           | editable: true             |
-- | <=          | Tightest bound (MIN of all values) | max-level: 2               |
-- | >=          | Loosest floor (MAX of all values)  | min-budget: 256            |
-- | pattern-in  | Set union of all matching rules    | allow-pattern: "git *"     |
-- | in          | Set intersection                   | only-kits: python-dev      |
-- | overlap     | Set union (any match suffices)     | any-of-effects: read,write |

-- exact: highest specificity wins; document order breaks ties.
CREATE VIEW _resolved_exact AS
    SELECT DISTINCT ON (entity_id, property_name)
        entity_id,
        property_name,
        property_value,
        comparison,
        specificity,
        rule_index,
        source_file,
        source_line,
    FROM cascade_candidates
    WHERE comparison = 'exact'
    ORDER BY entity_id, property_name, specificity DESC, rule_index DESC;

-- <=: tightest bound wins. All candidates contribute; the minimum value is the bound.
-- Provenance points to the rule that set the tightest bound.
CREATE VIEW _resolved_cap AS
    WITH ranked AS (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY entity_id, property_name
                ORDER BY CAST(property_value AS INTEGER) ASC, specificity DESC
            ) AS rn
        FROM cascade_candidates
        WHERE comparison = '<='
    )
    SELECT entity_id, property_name, property_value, comparison,
           specificity, rule_index, source_file, source_line,
    FROM ranked WHERE rn = 1;

-- >=: loosest floor wins. All candidates contribute; the maximum value is the floor.
CREATE VIEW _resolved_floor AS
    WITH ranked AS (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY entity_id, property_name
                ORDER BY CAST(property_value AS INTEGER) DESC, specificity DESC
            ) AS rn
        FROM cascade_candidates
        WHERE comparison = '>='
    )
    SELECT entity_id, property_name, property_value, comparison,
           specificity, rule_index, source_file, source_line,
    FROM ranked WHERE rn = 1;

-- pattern-in: union of all patterns from all matching rules.
-- The resolved value is a comma-separated aggregate; provenance points
-- to the highest-specificity contributor.
CREATE VIEW _resolved_pattern AS
    WITH agg AS (
        SELECT
            entity_id,
            property_name,
            STRING_AGG(DISTINCT property_value, ', ' ORDER BY property_value) AS property_value,
            'pattern-in' AS comparison,
            MAX(specificity) AS specificity,
            MAX(rule_index) AS rule_index,
        FROM cascade_candidates
        WHERE comparison = 'pattern-in'
        GROUP BY entity_id, property_name
    )
    SELECT a.entity_id, a.property_name, a.property_value, a.comparison,
           a.specificity, a.rule_index,
           c.source_file, c.source_line,
    FROM agg a
    JOIN cascade_candidates c
        ON a.entity_id = c.entity_id
        AND a.property_name = c.property_name
        AND a.specificity = c.specificity
        AND a.rule_index = c.rule_index
        AND c.comparison = 'pattern-in';

-- The unified resolved view: one row per (entity, property) after
-- comparison-aware resolution.
CREATE VIEW resolved_properties AS
    SELECT * FROM _resolved_exact
    UNION ALL BY NAME
    SELECT * FROM _resolved_cap
    UNION ALL BY NAME
    SELECT * FROM _resolved_floor
    UNION ALL BY NAME
    SELECT * FROM _resolved_pattern;


-- ============================================================================
-- 5a. TYPED PROJECTIONS — consumer-friendly views per entity type
-- ============================================================================

-- Convenience: entity + all its resolved properties as a MAP.
CREATE VIEW resolved_entities AS
    SELECT
        e.id,
        e.taxon,
        e.type_name,
        e.entity_id,
        e.classes,
        e.attributes,
        MAP(LIST(rp.property_name), LIST(rp.property_value)) AS properties,
    FROM entities e
    LEFT JOIN resolved_properties rp ON e.id = rp.entity_id
    GROUP BY e.id, e.taxon, e.type_name, e.entity_id, e.classes, e.attributes;


-- Files: world-axis resource properties pivoted to columns
CREATE VIEW files AS
    SELECT
        e.id,
        e.entity_id AS path,
        e.attributes['name'] AS name,
        e.attributes['language'] AS language,
        e.parent_id,
        MAX(CASE WHEN rp.property_name = 'editable' THEN rp.property_value END) AS editable,
        MAX(CASE WHEN rp.property_name = 'visible' THEN rp.property_value END) AS visible,
        MAX(CASE WHEN rp.property_name = 'show' THEN rp.property_value END) AS show_mode,
    FROM entities e
    LEFT JOIN resolved_properties rp ON e.id = rp.entity_id
    WHERE e.type_name = 'file'
    GROUP BY e.id, e.entity_id, e.attributes, e.parent_id;


-- Tools: capability-axis properties pivoted to columns
CREATE VIEW tools AS
    SELECT
        e.id,
        e.entity_id AS name,
        e.attributes['altitude'] AS altitude,
        MAX(CASE WHEN rp.property_name = 'allow' THEN rp.property_value END) AS allow,
        MAX(CASE WHEN rp.property_name = 'visible' THEN rp.property_value END) AS visible,
        MAX(CASE WHEN rp.property_name = 'max-level' THEN rp.property_value END) AS max_level,
        MAX(CASE WHEN rp.property_name = 'allow-pattern' THEN rp.property_value END) AS allow_pattern,
        MAX(CASE WHEN rp.property_name = 'deny-pattern' THEN rp.property_value END) AS deny_pattern,
    FROM entities e
    LEFT JOIN resolved_properties rp ON e.id = rp.entity_id
    WHERE e.type_name = 'tool'
    GROUP BY e.id, e.entity_id, e.attributes;


-- Modes: control-axis properties pivoted to columns
CREATE VIEW modes AS
    SELECT
        e.id,
        e.classes AS mode_classes,
        MAX(CASE WHEN rp.property_name = 'writable' THEN rp.property_value END) AS writable,
        MAX(CASE WHEN rp.property_name = 'strategy' THEN rp.property_value END) AS strategy,
        MAX(CASE WHEN rp.property_name = 'description' THEN rp.property_value END) AS description,
    FROM entities e
    LEFT JOIN resolved_properties rp ON e.id = rp.entity_id
    WHERE e.type_name = 'mode'
    GROUP BY e.id, e.classes;


-- Uses: action-axis permission projections
CREATE VIEW uses AS
    SELECT
        e.id,
        e.attributes['of'] AS of_target,
        e.attributes['of-kind'] AS of_kind,
        e.attributes['of-like'] AS of_like,
        MAX(CASE WHEN rp.property_name = 'editable' THEN rp.property_value END) AS editable,
        MAX(CASE WHEN rp.property_name = 'visible' THEN rp.property_value END) AS visible,
        MAX(CASE WHEN rp.property_name = 'allow' THEN rp.property_value END) AS allow,
        MAX(CASE WHEN rp.property_name = 'deny' THEN rp.property_value END) AS deny,
        MAX(CASE WHEN rp.property_name = 'allow-pattern' THEN rp.property_value END) AS allow_pattern,
        MAX(CASE WHEN rp.property_name = 'deny-pattern' THEN rp.property_value END) AS deny_pattern,
    FROM entities e
    LEFT JOIN resolved_properties rp ON e.id = rp.entity_id
    WHERE e.type_name = 'use'
    GROUP BY e.id, e.attributes;


-- ============================================================================
-- 6. CROSS-AXIS LINKS — use[of=...] references into the world
-- ============================================================================

CREATE TABLE use_refs (
    use_entity_id       INTEGER NOT NULL REFERENCES entities(id),
    target_entity_id    INTEGER REFERENCES entities(id),  -- NULL if unresolved
    ref_type            VARCHAR NOT NULL,  -- 'of', 'of-kind', 'of-like'
    ref_value           VARCHAR NOT NULL,  -- the raw selector string
);


-- ============================================================================
-- 7. TRANSITIONS — mode-change edges (coordination axis)
-- ============================================================================

CREATE VIEW transitions AS
    SELECT
        e.id,
        e.entity_id AS transition_id,
        e.attributes['from'] AS from_mode,
        e.attributes['to'] AS to_mode,
        MAX(CASE WHEN rp.property_name = 'run' THEN rp.property_value END) AS run_command,
        MAX(CASE WHEN rp.property_name = 'tier' THEN rp.property_value END) AS tier,
        MAX(CASE WHEN rp.property_name = 'phase' THEN rp.property_value END) AS phase,
        MAX(CASE WHEN rp.property_name = 'condition' THEN rp.property_value END) AS condition,
        MAX(CASE WHEN rp.property_name = 'advisory' THEN rp.property_value END) AS advisory,
    FROM entities e
    LEFT JOIN resolved_properties rp ON e.id = rp.entity_id
    WHERE e.type_name = 'transition'
    GROUP BY e.id, e.entity_id, e.attributes;


-- ============================================================================
-- 8. VERIFICATION ASSERTIONS — evaluation-framework claims as SQL
-- ============================================================================

-- A1: resolver determinism — every (entity, property) has exactly one resolved row.
CREATE VIEW assert_a1_unique_winners AS
    SELECT entity_id, property_name, COUNT(*) AS winner_count
    FROM resolved_properties
    GROUP BY entity_id, property_name
    HAVING winner_count > 1;
-- MUST return 0 rows. (pattern-in aggregates to one row; exact picks one winner.)

-- A2: cascade is well-ordered for exact properties — no non-winner has
-- higher precedence than the winner.
CREATE VIEW assert_a2_no_ties AS
    SELECT
        w.entity_id,
        w.property_name,
        w.specificity AS winner_spec,
        c.specificity AS challenger_spec,
    FROM _resolved_exact w
    JOIN cascade_candidates c
        ON w.entity_id = c.entity_id
        AND w.property_name = c.property_name
        AND c.comparison = 'exact'
        AND (c.specificity, c.rule_index) != (w.specificity, w.rule_index)
    WHERE c.specificity > w.specificity
       OR (c.specificity = w.specificity AND c.rule_index > w.rule_index);
-- MUST return 0 rows.

-- A5: comparison semantics — every resolved property's comparison matches
-- the registered property_type comparison.
CREATE VIEW assert_a5_comparison_match AS
    SELECT rp.entity_id, rp.property_name, rp.comparison AS resolved_cmp,
           pt.comparison AS registered_cmp,
    FROM resolved_properties rp
    JOIN entities e ON rp.entity_id = e.id
    JOIN property_types pt
        ON pt.taxon = e.taxon
        AND pt.entity_type = e.type_name
        AND pt.name = rp.property_name
    WHERE rp.comparison != pt.comparison;
-- MUST return 0 rows. Any row means a property was resolved with the wrong strategy.

-- A6: world-axis and action-axis are independent — no file.editable
-- candidate shares a selector with a use.editable candidate.
-- (They resolve separately through different entity types.)
CREATE VIEW assert_a6_axis_independence AS
    SELECT DISTINCT 'file+use collision' AS issue,
           fc.selector_id,
           fc.property_name,
    FROM cascade_candidates fc
    JOIN entities fe ON fc.entity_id = fe.id AND fe.type_name = 'file'
    JOIN cascade_candidates uc ON fc.selector_id = uc.selector_id
    JOIN entities ue ON uc.entity_id = ue.id AND ue.type_name = 'use'
    WHERE fc.property_name = uc.property_name
      AND fc.property_name IN ('editable', 'visible', 'allow');
-- MUST return 0 rows. A selector can't match both a file and a use entity
-- (different type_names), so this should be structurally impossible.

-- C1: proof-tree traceability — every resolved property traces to a source line.
CREATE VIEW assert_c1_provenance AS
    SELECT entity_id, property_name
    FROM resolved_properties
    WHERE source_file IS NULL OR source_line IS NULL;
-- MUST return 0 rows.

-- D2: ratchet monotonicity check (run between two databases).
-- See examples/03-diff-two-worlds.sql for the full diff query.
-- The assertion: no widening should exist in a ratchet-produced diff.
-- Widenings: editable false→true, allow false→true, deny "*"→"",
--            max-level increasing, pattern set shrinking.

-- H3: no enforcement-tool wrapper dependency (checked at build time, not in the DB).
