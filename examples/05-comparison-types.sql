-- Example 5: Comparison-aware cascade resolution
--
-- umwelt source:
--   tool[name="Bash"] { allow: true; max-level: 5; }
--   mode.implement tool[name="Bash"] { max-level: 3; }
--   principal#Teague mode.implement tool[name="Bash"] { max-level: 2; }
--
--   tool[name="Bash"] { allow-pattern: "git *"; }
--   mode.implement tool[name="Bash"] { allow-pattern: "pytest *"; }
--   principal#Teague tool[name="Bash"] { allow-pattern: "make *"; }
--
-- Demonstrates:
--   - exact:      allow → highest specificity wins
--   - <=:         max-level → tightest bound (MIN) wins regardless of specificity
--   - pattern-in: allow-pattern → all patterns aggregate (UNION)
--
-- Run with: duckdb < examples/05-comparison-types.sql

-- ============================================================================
-- Schema (minimal, self-contained)
-- ============================================================================

CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    taxon       VARCHAR NOT NULL,
    type_name   VARCHAR NOT NULL,
    entity_id   VARCHAR,
    attributes  MAP(VARCHAR, VARCHAR),
);

CREATE TABLE cascade_candidates (
    entity_id       INTEGER NOT NULL,
    property_name   VARCHAR NOT NULL,
    property_value  VARCHAR NOT NULL,
    comparison      VARCHAR NOT NULL DEFAULT 'exact',
    specificity     INTEGER[] NOT NULL,
    rule_index      INTEGER NOT NULL,
    selector_text   VARCHAR,
    source_line     INTEGER,
);


-- ============================================================================
-- Entity
-- ============================================================================

INSERT INTO entities VALUES
    (1, 'capability', 'tool', 'Bash', MAP {'name': 'Bash'});


-- ============================================================================
-- Cascade candidates
-- ============================================================================

-- `allow` property: comparison = 'exact'
-- Three rules, different specificities. Highest specificity wins.
INSERT INTO cascade_candidates VALUES
    (1, 'allow', 'true', 'exact',
     [1, 0, 0, 0, 0, 101, 0, 0], 0,
     'tool[name="Bash"]', 1);
-- (Only one rule sets allow; it wins trivially. Included to show the pattern.)


-- `max-level` property: comparison = '<='
-- Three rules, each setting a different cap.
-- Resolution: the TIGHTEST (minimum) bound wins, regardless of specificity.
INSERT INTO cascade_candidates VALUES
    -- 1-axis: max-level: 5
    (1, 'max-level', '5', '<=',
     [1, 0, 0, 0, 0, 101, 0, 0], 0,
     'tool[name="Bash"]', 1),
    -- 2-axis: max-level: 3
    (1, 'max-level', '3', '<=',
     [2, 0, 0, 101, 0, 101, 0, 0], 1,
     'mode.implement tool[name="Bash"]', 2),
    -- 3-axis: max-level: 2
    (1, 'max-level', '2', '<=',
     [3, 10001, 0, 101, 0, 101, 0, 0], 2,
     'principal#Teague mode.implement tool[name="Bash"]', 3);


-- `allow-pattern` property: comparison = 'pattern-in'
-- Three rules, each adding patterns. Resolution: UNION of all patterns.
INSERT INTO cascade_candidates VALUES
    -- 1-axis: "git *"
    (1, 'allow-pattern', 'git *', 'pattern-in',
     [1, 0, 0, 0, 0, 101, 0, 0], 3,
     'tool[name="Bash"]', 4),
    -- 2-axis: "pytest *"
    (1, 'allow-pattern', 'pytest *', 'pattern-in',
     [2, 0, 0, 101, 0, 101, 0, 0], 4,
     'mode.implement tool[name="Bash"]', 5),
    -- 2-axis: "make *"
    (1, 'allow-pattern', 'make *', 'pattern-in',
     [2, 10001, 0, 0, 0, 101, 0, 0], 5,
     'principal#Teague tool[name="Bash"]', 6);


-- ============================================================================
-- CASCADE RESOLUTION (comparison-aware)
-- ============================================================================

-- exact: highest specificity wins
CREATE VIEW _resolved_exact AS
    SELECT DISTINCT ON (entity_id, property_name)
        entity_id, property_name, property_value, comparison,
        specificity, rule_index, selector_text, source_line,
    FROM cascade_candidates
    WHERE comparison = 'exact'
    ORDER BY entity_id, property_name, specificity DESC, rule_index DESC;

-- <=: tightest bound (MIN value) wins
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
           specificity, rule_index, selector_text, source_line,
    FROM ranked WHERE rn = 1;

-- pattern-in: aggregate ALL patterns into one comma-separated value
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
    SELECT a.*, c.selector_text, c.source_line,
    FROM agg a
    JOIN cascade_candidates c
        ON a.entity_id = c.entity_id
        AND a.property_name = c.property_name
        AND a.specificity = c.specificity
        AND a.rule_index = c.rule_index
        AND c.comparison = 'pattern-in';

CREATE VIEW resolved_properties AS
    SELECT * FROM _resolved_exact
    UNION ALL BY NAME
    SELECT * FROM _resolved_cap
    UNION ALL BY NAME
    SELECT * FROM _resolved_pattern;


-- ============================================================================
-- RESULTS
-- ============================================================================

-- Q1: All resolved properties for Bash
SELECT property_name, property_value, comparison, selector_text
FROM resolved_properties
JOIN entities e ON resolved_properties.entity_id = e.id
WHERE e.entity_id = 'Bash'
ORDER BY property_name;

-- Expected:
-- allow         | true                       | exact      | tool[name="Bash"]
-- allow-pattern | git *, make *, pytest *     | pattern-in | principal#Teague tool[name="Bash"]
-- max-level     | 2                          | <=         | principal#Teague mode.implement tool[name="Bash"]


-- Q2: Show all max-level candidates (the full cap-resolution story)
SELECT
    property_value AS cap,
    comparison,
    specificity,
    selector_text,
    CASE WHEN property_value = (
        SELECT property_value FROM resolved_properties rp
        WHERE rp.entity_id = cc.entity_id AND rp.property_name = 'max-level'
    ) THEN '>>> WINNER (tightest) <<<' ELSE '' END AS status,
FROM cascade_candidates cc
WHERE cc.entity_id = 1
  AND cc.property_name = 'max-level'
ORDER BY CAST(cc.property_value AS INTEGER) ASC;

-- Expected:
-- 2 | <= | [3,10001,...] | principal#Teague mode.implement tool[name="Bash"] | >>> WINNER (tightest) <<<
-- 3 | <= | [2,0,...]     | mode.implement tool[name="Bash"]                  |
-- 5 | <= | [1,0,...]     | tool[name="Bash"]                                 |
--
-- Note: the 3-axis rule set max-level=2, which is the tightest.
-- But the 1-axis rule's max-level=5 would have been the winner if
-- resolution were specificity-based (since it's the only candidate
-- with that specificity). <= resolution ignores specificity ordering —
-- the tightest value wins regardless.


-- Q3: Show all allow-pattern candidates (the union story)
SELECT
    property_value AS pattern,
    specificity,
    selector_text,
FROM cascade_candidates
WHERE entity_id = 1
  AND property_name = 'allow-pattern'
ORDER BY property_value;

-- Expected:
-- git *    | [1,0,...]     | tool[name="Bash"]
-- make *   | [2,10001,...] | principal#Teague tool[name="Bash"]
-- pytest * | [2,0,...]     | mode.implement tool[name="Bash"]
--
-- All three contribute to the resolved value: "git *, make *, pytest *"
-- Unlike exact (one winner) or <= (one bound), pattern-in aggregates all.


-- Q4: Verification — resolved property count
SELECT
    property_name,
    comparison,
    COUNT(*) AS candidate_count,
    (SELECT COUNT(*) FROM resolved_properties rp
     WHERE rp.entity_id = 1 AND rp.property_name = cc.property_name) AS resolved_count,
FROM cascade_candidates cc
WHERE entity_id = 1
GROUP BY property_name, comparison;

-- Expected:
-- allow         | exact      | 1 candidates | 1 resolved  (trivial: only one candidate)
-- allow-pattern | pattern-in | 3 candidates | 1 resolved  (aggregated to one row)
-- max-level     | <=         | 3 candidates | 1 resolved  (tightest bound = one row)
