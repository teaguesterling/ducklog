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
    attrs           JSON,               -- attribute bag: {"path": "/src/auth.py", "language": "python"}
);

CREATE INDEX idx_entities_taxon ON entities(taxon);
CREATE INDEX idx_entities_type ON entities(type_name);
CREATE INDEX idx_entities_id ON entities(entity_id);


-- ============================================================================
-- 4. CASCADE — matching rules to entities, computing winners
-- ============================================================================

-- Every (entity, selector) pair where the selector matched the entity.
-- This is the "candidate" set before winner selection.
CREATE TABLE cascade_matches (
    entity_id       INTEGER NOT NULL REFERENCES entities(id),
    selector_id     INTEGER NOT NULL REFERENCES selectors(id),
    rule_id         INTEGER NOT NULL REFERENCES rules(id),
    PRIMARY KEY (entity_id, selector_id),
);

-- Every (entity, property) candidate: one row per rule that could set this property.
CREATE TABLE cascade_candidates (
    entity_id       INTEGER NOT NULL REFERENCES entities(id),
    property_name   VARCHAR NOT NULL,
    property_value  VARCHAR NOT NULL,
    selector_id     INTEGER NOT NULL REFERENCES selectors(id),
    rule_id         INTEGER NOT NULL REFERENCES rules(id),
    specificity     INTEGER[] NOT NULL,
    rule_index      INTEGER NOT NULL,   -- document order for tiebreaking
    source_file     VARCHAR,
    source_line     INTEGER,
    is_winner       BOOLEAN NOT NULL DEFAULT false,
);

CREATE INDEX idx_candidates_entity_prop ON cascade_candidates(entity_id, property_name);
CREATE INDEX idx_candidates_winner ON cascade_candidates(is_winner) WHERE is_winner = true;


-- ============================================================================
-- 5. RESOLVED — the cascade winners (the queryable policy)
-- ============================================================================

-- One row per (entity, property): the winning value after cascade resolution.
-- This is the primary table consumers query.
CREATE VIEW resolved_properties AS
    SELECT
        cc.entity_id,
        cc.property_name,
        cc.property_value,
        cc.specificity,
        cc.rule_index,
        cc.source_file,
        cc.source_line,
    FROM cascade_candidates cc
    WHERE cc.is_winner = true;

-- Convenience: entity + all its resolved properties as a JSON object.
CREATE VIEW resolved_entities AS
    SELECT
        e.id,
        e.taxon,
        e.type_name,
        e.entity_id,
        e.classes,
        e.attrs,
        json_group_object(rp.property_name, rp.property_value) AS properties,
    FROM entities e
    LEFT JOIN resolved_properties rp ON e.id = rp.entity_id
    GROUP BY e.id, e.taxon, e.type_name, e.entity_id, e.classes, e.attrs;


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
        e.attrs->>'from' AS from_mode,
        e.attrs->>'to' AS to_mode,
        rp_run.property_value AS run_command,
        rp_tier.property_value AS tier,
        rp_phase.property_value AS phase,
        rp_cond.property_value AS condition,
        rp_adv.property_value AS advisory,
    FROM entities e
    LEFT JOIN resolved_properties rp_run ON e.id = rp_run.entity_id AND rp_run.property_name = 'run'
    LEFT JOIN resolved_properties rp_tier ON e.id = rp_tier.entity_id AND rp_tier.property_name = 'tier'
    LEFT JOIN resolved_properties rp_phase ON e.id = rp_phase.entity_id AND rp_phase.property_name = 'phase'
    LEFT JOIN resolved_properties rp_cond ON e.id = rp_cond.entity_id AND rp_cond.property_name = 'condition'
    LEFT JOIN resolved_properties rp_adv ON e.id = rp_adv.entity_id AND rp_adv.property_name = 'advisory'
    WHERE e.type_name = 'transition';


-- ============================================================================
-- 8. VERIFICATION ASSERTIONS — evaluation-framework claims as SQL
-- ============================================================================

-- A1: resolver determinism — every (entity, property) has exactly one winner.
CREATE VIEW assert_a1_unique_winners AS
    SELECT entity_id, property_name, COUNT(*) AS winner_count
    FROM cascade_candidates
    WHERE is_winner = true
    GROUP BY entity_id, property_name
    HAVING winner_count > 1;
-- MUST return 0 rows.

-- A2: cascade is well-ordered — no ties. Every winner has strictly higher
-- (specificity, rule_index) than all other candidates for the same (entity, property).
CREATE VIEW assert_a2_no_ties AS
    SELECT
        w.entity_id,
        w.property_name,
        w.specificity AS winner_spec,
        c.specificity AS challenger_spec,
        w.rule_index AS winner_order,
        c.rule_index AS challenger_order,
    FROM cascade_candidates w
    JOIN cascade_candidates c
        ON w.entity_id = c.entity_id
        AND w.property_name = c.property_name
        AND w.selector_id != c.selector_id
    WHERE w.is_winner = true
      AND c.is_winner = false
      AND (c.specificity > w.specificity
           OR (c.specificity = w.specificity AND c.rule_index > w.rule_index));
-- MUST return 0 rows. Any row means a non-winner has higher precedence than the winner.

-- C1: proof-tree traceability — every resolved property traces back to a source line.
CREATE VIEW assert_c1_provenance AS
    SELECT entity_id, property_name
    FROM resolved_properties
    WHERE source_file IS NULL OR source_line IS NULL;
-- MUST return 0 rows.

-- H3: no enforcement-tool wrapper dependency (checked at build time, not in the DB).
