-- Example 2: Mode-gated tool visibility with cross-axis cascade
--
-- umwelt source:
--   tool { allow: true; }
--   mode.explore tool { allow: false; }
--   mode.explore tool[name="Read"] { allow: true; }
--   mode.explore tool[name="Grep"] { allow: true; }
--   principal#Teague mode.explore tool[name="Bash"] { allow: true; allow-pattern: "git log *"; }
--
-- Demonstrates axis_count-first specificity: 3-axis rule beats 2-axis beats 1-axis.
-- Run with: duckdb < examples/02-mode-tool-cascade.sql

CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    taxon       VARCHAR NOT NULL,
    type_name   VARCHAR NOT NULL,
    entity_id   VARCHAR,
    classes     VARCHAR[],
    attrs       JSON,
);

CREATE TABLE cascade_candidates (
    entity_id       INTEGER NOT NULL,
    property_name   VARCHAR NOT NULL,
    property_value  VARCHAR NOT NULL,
    specificity     INTEGER[] NOT NULL,
    rule_index      INTEGER NOT NULL,
    selector_text   VARCHAR,
    axis_count      INTEGER,
    is_winner       BOOLEAN DEFAULT false,
);

-- ============================================================================
-- Entities
-- ============================================================================

INSERT INTO entities VALUES
    (1, 'capability', 'tool', 'Read',  NULL, '{"name": "Read"}'),
    (2, 'capability', 'tool', 'Grep',  NULL, '{"name": "Grep"}'),
    (3, 'capability', 'tool', 'Edit',  NULL, '{"name": "Edit"}'),
    (4, 'capability', 'tool', 'Bash',  NULL, '{"name": "Bash"}'),
    (5, 'state',      'mode', NULL,    ['explore'], NULL),
    (6, 'principal',  'principal', 'Teague', NULL, NULL);


-- ============================================================================
-- Cascade candidates for the `allow` property on each tool
-- ============================================================================

-- Specificity encoding:
--   (axis_count, principal_w, world_w, state_w, actor_w, capability_w, audit_w, other_w)
--   weight = ids*10000 + (classes+attrs+pseudos)*100 + types

-- Rule 0: tool { allow: true; }
--   axis_count=1, capability_w = 0*10000 + 0*100 + 1 = 1
WITH rule_0 AS (
    SELECT id AS entity_id, 'allow' AS pn, 'true' AS pv,
           [1, 0, 0, 0, 0, 1, 0, 0] AS spec, 0 AS ri,
           'tool' AS sel, 1 AS ac
    FROM entities WHERE type_name = 'tool'
),
-- Rule 1: mode.explore tool { allow: false; }
--   axis_count=2, state_w = 0*10000 + 1*100 + 1 = 101... wait, mode.explore has .explore (class=1) + type 'mode'(type=1) = 0+100+1=101? No:
--   mode.explore: type_name='mode' (types=1), classes=['.explore'] (classes=1), no attrs, no id
--   weight = 0*10000 + 1*100 + 1 = 101
--   tool: type_name='tool' (types=1), no attrs
--   weight = 0*10000 + 0*100 + 1 = 1
--   state_w=101, capability_w=1
rule_1 AS (
    SELECT id AS entity_id, 'allow' AS pn, 'false' AS pv,
           [2, 0, 0, 101, 0, 1, 0, 0] AS spec, 1 AS ri,
           'mode.explore tool' AS sel, 2 AS ac
    FROM entities WHERE type_name = 'tool'
),
-- Rule 2: mode.explore tool[name="Read"] { allow: true; }
--   tool[name="Read"]: type_name='tool' (types=1), attrs=[name="Read"] (attrs=1)
--   capability_w = 0*10000 + 1*100 + 1 = 101
rule_2 AS (
    SELECT id AS entity_id, 'allow' AS pn, 'true' AS pv,
           [2, 0, 0, 101, 0, 101, 0, 0] AS spec, 2 AS ri,
           'mode.explore tool[name="Read"]' AS sel, 2 AS ac
    FROM entities WHERE type_name = 'tool' AND entity_id = 'Read'
),
-- Rule 3: mode.explore tool[name="Grep"] { allow: true; }
rule_3 AS (
    SELECT id AS entity_id, 'allow' AS pn, 'true' AS pv,
           [2, 0, 0, 101, 0, 101, 0, 0] AS spec, 3 AS ri,
           'mode.explore tool[name="Grep"]' AS sel, 2 AS ac
    FROM entities WHERE type_name = 'tool' AND entity_id = 'Grep'
),
-- Rule 4: principal#Teague mode.explore tool[name="Bash"] { allow: true; }
--   principal#Teague: id='Teague' → principal_w = 1*10000 + 0 + 1 = 10001
--   axis_count=3
rule_4 AS (
    SELECT id AS entity_id, 'allow' AS pn, 'true' AS pv,
           [3, 10001, 0, 101, 0, 101, 0, 0] AS spec, 4 AS ri,
           'principal#Teague mode.explore tool[name="Bash"]' AS sel, 3 AS ac
    FROM entities WHERE type_name = 'tool' AND entity_id = 'Bash'
)

INSERT INTO cascade_candidates (entity_id, property_name, property_value, specificity, rule_index, selector_text, axis_count)
    SELECT * FROM rule_0
    UNION ALL SELECT * FROM rule_1
    UNION ALL SELECT * FROM rule_2
    UNION ALL SELECT * FROM rule_3
    UNION ALL SELECT * FROM rule_4;


-- ============================================================================
-- CASCADE RESOLUTION
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
-- RESULTS
-- ============================================================================

-- Q1: Final allow status per tool
SELECT
    e.entity_id AS tool,
    cc.property_value AS allowed,
    cc.selector_text AS winning_rule,
    cc.axis_count,
FROM cascade_candidates cc
JOIN entities e ON cc.entity_id = e.id
WHERE cc.is_winner = true
  AND cc.property_name = 'allow'
ORDER BY e.entity_id;

-- Expected:
-- Bash | true  | principal#Teague mode.explore tool[name="Bash"]  | 3   ← Teague's 3-axis override
-- Edit | false | mode.explore tool                                | 2   ← mode default-deny
-- Grep | true  | mode.explore tool[name="Grep"]                   | 2   ← specific allowlist
-- Read | true  | mode.explore tool[name="Read"]                   | 2   ← specific allowlist


-- Q2: Show the full cascade for Bash (why did Teague's override win?)
SELECT
    cc.property_value AS value,
    cc.specificity,
    cc.axis_count,
    cc.selector_text,
    CASE WHEN cc.is_winner THEN 'WINNER' ELSE '' END AS status,
FROM cascade_candidates cc
JOIN entities e ON cc.entity_id = e.id
WHERE e.entity_id = 'Bash'
  AND cc.property_name = 'allow'
ORDER BY cc.specificity DESC, cc.rule_index DESC;

-- Expected:
-- true  | [3,10001,0,101,0,101,0,0] | 3 | principal#Teague mode.explore tool[name="Bash"] | WINNER
-- false | [2,0,0,101,0,1,0,0]       | 2 | mode.explore tool                                |
-- true  | [1,0,0,0,0,1,0,0]         | 1 | tool                                             |


-- Q3: Axis-count distribution
SELECT axis_count, COUNT(*) AS rules, COUNT(*) FILTER (WHERE is_winner) AS winners
FROM cascade_candidates
GROUP BY axis_count
ORDER BY axis_count;
