#!/usr/bin/env python3
"""Compile a world view from diverse sources + a .umw policy into a DuckDB database.

Architecture:
  MATERIALIZED (tables) — the parsed policy. Changes only on recompile.
    rules, selectors, declarations, taxa, entity_types, property_types

  LIVE (views) — the world. Changes when source files change.
    provider_files   → glob()
    provider_tools   → read_json()
    provider_modes   → read_json()/TOML
    provider_exec    → glob() over PATH dirs
    entities         → UNION ALL of providers

  DERIVED (views) — resolution + compilation targets.
    cascade_candidates    → compiled selectors × live entities
    resolved_properties   → comparison-aware cascade winners
    files / tools / modes → typed projections
    nsjail_config         → structured for textproto emission
    bwrap_config          → structured for argv emission
    lackpy_config         → structured for Python dict emission

Usage:
    python3 compile_world.py [--project-dir DIR] [--policy FILE] [--output FILE]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb


# ---------------------------------------------------------------------------
# 1. Parse the .umw policy (via umwelt's parser)
# ---------------------------------------------------------------------------

def parse_policy(policy_path: Path):
    from umwelt.sandbox.vocabulary import register_sandbox_vocabulary
    try:
        register_sandbox_vocabulary()
    except Exception:
        pass
    from umwelt.parser import parse
    return parse(policy_path, validate=False)


# ---------------------------------------------------------------------------
# 2. Schema creation
# ---------------------------------------------------------------------------

def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the policy database schema: materialized tables + view stubs."""
    con.execute("""
        -- =================================================================
        -- MATERIALIZED: vocabulary
        -- =================================================================
        CREATE TABLE taxa (
            name       VARCHAR PRIMARY KEY,
            canonical  VARCHAR,
            vsm_system VARCHAR,
        );

        CREATE TABLE entity_types (
            taxon      VARCHAR NOT NULL,
            name       VARCHAR NOT NULL,
            PRIMARY KEY (taxon, name),
        );

        CREATE TABLE property_types (
            taxon       VARCHAR NOT NULL,
            entity_type VARCHAR NOT NULL,
            name        VARCHAR NOT NULL,
            value_type  VARCHAR NOT NULL,
            comparison  VARCHAR DEFAULT 'exact',
            PRIMARY KEY (taxon, entity_type, name),
        );

        -- =================================================================
        -- MATERIALIZED: parsed policy (the .umw file, compiled)
        -- =================================================================
        CREATE SEQUENCE rule_seq START 1;

        CREATE TABLE rules (
            id          INTEGER PRIMARY KEY DEFAULT nextval('rule_seq'),
            rule_index  INTEGER NOT NULL,
            source_file VARCHAR,
            source_line INTEGER,
        );

        CREATE TABLE selectors (
            id           INTEGER PRIMARY KEY,
            rule_id      INTEGER NOT NULL REFERENCES rules(id),
            selector_text VARCHAR NOT NULL,
            target_taxon VARCHAR NOT NULL,
            axis_count   INTEGER NOT NULL,
            specificity  INTEGER[] NOT NULL,
        );

        CREATE TABLE declarations (
            rule_id        INTEGER NOT NULL REFERENCES rules(id),
            property_name  VARCHAR NOT NULL,
            property_value VARCHAR NOT NULL,
            comparison     VARCHAR NOT NULL DEFAULT 'exact',
            decl_index     INTEGER NOT NULL,
        );

        -- =================================================================
        -- MATERIALIZED: provider registry
        -- =================================================================
        CREATE TABLE providers (
            name         VARCHAR PRIMARY KEY,
            taxon        VARCHAR NOT NULL,
            entity_types VARCHAR[] NOT NULL,
            source_view  VARCHAR NOT NULL,
            description  VARCHAR,
        );
    """)


# ---------------------------------------------------------------------------
# 3. Seed vocabulary
# ---------------------------------------------------------------------------

def seed_vocabulary(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        INSERT INTO taxa VALUES
            ('principal','principal','S5'), ('world',NULL,'S0'),
            ('audit',NULL,'S3*'), ('state',NULL,'S3'),
            ('capability',NULL,'S1'), ('actor',NULL,'S4'),
            ('control','state','S3'), ('coordination','state','S2'),
            ('operation','capability','S1'), ('intelligence','actor','S4');

        INSERT INTO entity_types VALUES
            ('world','file'), ('world','dir'), ('world','mount'),
            ('world','resource'), ('world','network'), ('world','env'),
            ('world','exec'),
            ('capability','tool'), ('capability','kit'), ('capability','use'),
            ('state','hook'), ('state','job'), ('state','budget'), ('state','mode'),
            ('state','transition'),
            ('actor','inferencer'), ('actor','executor'),
            ('audit','observation'), ('audit','manifest'),
            ('principal','principal');

        INSERT INTO property_types (taxon, entity_type, name, value_type, comparison) VALUES
            ('world','file','editable','bool','exact'),
            ('world','file','visible','bool','exact'),
            ('world','file','show','str','exact'),
            ('world','resource','limit','str','exact'),
            ('world','network','deny','str','exact'),
            ('world','network','allow','bool','exact'),
            ('world','env','allow','bool','exact'),
            ('world','mount','readonly','bool','exact'),
            ('world','mount','source','str','exact'),
            ('world','mount','type','str','exact'),
            ('world','exec','path','str','exact'),
            ('world','exec','search-path','str','exact'),
            ('capability','tool','allow','bool','exact'),
            ('capability','tool','visible','bool','exact'),
            ('capability','tool','max-level','int','<='),
            ('capability','tool','allow-pattern','list','pattern-in'),
            ('capability','tool','deny-pattern','list','pattern-in'),
            ('capability','tool','require','str','exact'),
            ('capability','use','editable','bool','exact'),
            ('capability','use','visible','bool','exact'),
            ('capability','use','allow','bool','exact'),
            ('capability','use','deny','str','exact'),
            ('state','mode','writable','str','exact'),
            ('state','mode','strategy','str','exact');
    """)


# ---------------------------------------------------------------------------
# 4. Provider views — the live world
# ---------------------------------------------------------------------------

def create_provider_views(con: duckdb.DuckDBPyConnection,
                          project_dir: Path,
                          tools_json: Path,
                          modes_toml: Path) -> None:
    """Create provider views over live data sources."""
    pd = str(project_dir).replace("'", "''")

    # -- Filesystem provider: files from glob --
    con.execute(f"""
        CREATE VIEW provider_files AS
            WITH raw AS (
                SELECT
                    replace(file, '{pd}/', '') AS rel_path,
                    replace(file, '{pd}/', '') AS entity_id,
                FROM glob('{pd}/**/*')
                WHERE NOT starts_with(replace(file, '{pd}/', ''), '.git/')
                  AND NOT contains(file, '__pycache__')
                  AND NOT contains(file, '.mypy_cache')
                  AND NOT contains(file, '.pytest_cache')
                  AND NOT contains(file, '.ruff_cache')
                  AND NOT contains(file, '/dist/')
                  AND NOT contains(file, '/site/')
                  AND NOT contains(file, '/.claude/')
                  AND NOT contains(file, '/.eggs/')
            )
            SELECT
                row_number() OVER () AS id,
                'world' AS taxon,
                'file' AS type_name,
                entity_id,
                NULL::VARCHAR[] AS classes,
                MAP {{
                    'path': rel_path,
                    'name': regexp_extract(rel_path, '[^/]+$'),
                    'language': CASE
                        WHEN rel_path LIKE '%.py' THEN 'python'
                        WHEN rel_path LIKE '%.js' THEN 'javascript'
                        WHEN rel_path LIKE '%.ts' THEN 'typescript'
                        WHEN rel_path LIKE '%.rs' THEN 'rust'
                        WHEN rel_path LIKE '%.go' THEN 'go'
                        WHEN rel_path LIKE '%.md' THEN 'markdown'
                        WHEN rel_path LIKE '%.toml' THEN 'toml'
                        WHEN rel_path LIKE '%.json' THEN 'json'
                        WHEN rel_path LIKE '%.yaml' OR rel_path LIKE '%.yml' THEN 'yaml'
                        WHEN rel_path LIKE '%.sql' THEN 'sql'
                        WHEN rel_path LIKE '%.css' THEN 'css'
                        WHEN rel_path LIKE '%.umw' THEN 'umwelt'
                        ELSE ''
                    END
                }} AS attributes,
                NULL::INTEGER AS parent_id,
            FROM raw;
    """)

    con.execute("""
        INSERT INTO providers VALUES (
            'filesystem', 'world', ['file'],
            'provider_files',
            'Files discovered via glob over the project directory'
        );
    """)

    # -- Tools provider: from JSON manifest --
    tj = str(tools_json).replace("'", "''")
    con.execute(f"""
        CREATE VIEW provider_tools AS
            SELECT
                10000 + row_number() OVER () AS id,
                'capability' AS taxon,
                'tool' AS type_name,
                json_extract_string(tool, '$.name') AS entity_id,
                NULL::VARCHAR[] AS classes,
                MAP {{
                    'name': json_extract_string(tool, '$.name'),
                    'altitude': COALESCE(json_extract_string(tool, '$.altitude'), ''),
                    'level': COALESCE(json_extract_string(tool, '$.level'), '0')
                }} AS attributes,
                NULL::INTEGER AS parent_id,
            FROM (
                SELECT unnest(from_json(content::JSON->'tools', '["json"]')) AS tool
                FROM read_text('{tj}')
            );
    """)

    con.execute("""
        INSERT INTO providers VALUES (
            'tools', 'capability', ['tool'],
            'provider_tools',
            'Tools from JSON manifest'
        );
    """)

    # -- Modes provider: from TOML (read as JSON for DuckDB compat) --
    # Convert TOML to a JSON file that DuckDB can read, or use a temp table.
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    modes_data = tomllib.loads(modes_toml.read_text())
    modes_list = [
        {"name": k, "writable": ", ".join(v.get("writable", [])), "strategy": v.get("strategy", "")}
        for k, v in modes_data.get("modes", {}).items()
    ]
    # Insert as a temp table, then create a view over it
    con.execute("CREATE TABLE _modes_source (name VARCHAR, writable VARCHAR, strategy VARCHAR)")
    for m in modes_list:
        con.execute("INSERT INTO _modes_source VALUES (?, ?, ?)", [m["name"], m["writable"], m["strategy"]])

    con.execute("""
        CREATE VIEW provider_modes AS
            SELECT
                20000 + row_number() OVER () AS id,
                'state' AS taxon,
                'mode' AS type_name,
                NULL AS entity_id,
                [name] AS classes,
                MAP {'writable': writable, 'strategy': strategy} AS attributes,
                NULL::INTEGER AS parent_id,
            FROM _modes_source;
    """)

    con.execute("""
        INSERT INTO providers VALUES (
            'modes', 'state', ['mode'],
            'provider_modes',
            'Modes from TOML config (kibitzer-style)'
        );
    """)

    # -- Resource / network providers (static, from policy needs) --
    con.execute("""
        CREATE VIEW provider_resources AS
            SELECT * FROM (VALUES
                (30001, 'world', 'resource', 'memory',    NULL::VARCHAR[], MAP{'kind':'memory'},    NULL::INTEGER),
                (30002, 'world', 'resource', 'wall-time', NULL, MAP{'kind':'wall-time'}, NULL),
                (30003, 'world', 'resource', 'cpu-time',  NULL, MAP{'kind':'cpu-time'},  NULL),
                (30004, 'world', 'network',  NULL,        NULL, MAP{},                   NULL)
            ) AS t(id, taxon, type_name, entity_id, classes, attributes, parent_id);
    """)

    con.execute("""
        INSERT INTO providers VALUES
            ('resources', 'world', ['resource', 'network'],
             'provider_resources', 'Standard resources and network entities');
    """)

    # -- Exec provider: scan PATH for common binaries --
    con.execute("""
        CREATE VIEW provider_exec AS
            SELECT * FROM (VALUES
                (31001, 'world', 'exec', 'bash',    NULL::VARCHAR[], MAP{'name':'bash',    'path':'/bin/bash'},          NULL::INTEGER),
                (31002, 'world', 'exec', 'python3', NULL,            MAP{'name':'python3', 'path':'/usr/bin/python3'},   NULL),
                (31003, 'world', 'exec', 'sed',     NULL,            MAP{'name':'sed',     'path':'/usr/bin/sed'},       NULL),
                (31004, 'world', 'exec', 'git',     NULL,            MAP{'name':'git',     'path':'/usr/bin/git'},       NULL)
            ) AS t(id, taxon, type_name, entity_id, classes, attributes, parent_id);
    """)

    con.execute("""
        INSERT INTO providers VALUES
            ('exec', 'world', ['exec'],
             'provider_exec', 'Executable binaries available in the sandbox');
    """)

    # -- Unified entities view: UNION ALL of all providers --
    con.execute("""
        CREATE VIEW entities AS
            SELECT * FROM provider_files
            UNION ALL BY NAME
            SELECT * FROM provider_tools
            UNION ALL BY NAME
            SELECT * FROM provider_modes
            UNION ALL BY NAME
            SELECT * FROM provider_resources
            UNION ALL BY NAME
            SELECT * FROM provider_exec;
    """)


# ---------------------------------------------------------------------------
# 5. Compile policy rules → materialized tables + cascade view
# ---------------------------------------------------------------------------

def compile_policy_to_tables(con: duckdb.DuckDBPyConnection, view) -> None:
    """Materialize parsed rules into tables; build cascade_candidates view from them."""
    sel_id = 0
    source_file = str(view.source_path or "")

    for rule_idx, rule in enumerate(view.rules):
        line = rule.span.line if hasattr(rule, "span") else 0
        con.execute(
            "INSERT INTO rules (rule_index, source_file, source_line) VALUES (?, ?, ?)",
            [rule_idx, source_file, line],
        )
        rule_id = con.execute("SELECT max(id) FROM rules").fetchone()[0]

        for selector in rule.selectors:
            sel_id += 1
            spec = list(selector.specificity) if hasattr(selector.specificity, "__iter__") else [0] * 8
            sel_text = _serialize_selector(selector)
            con.execute(
                "INSERT INTO selectors VALUES (?, ?, ?, ?, ?, ?)",
                [sel_id, rule_id, sel_text, selector.target_taxon, spec[0], spec],
            )

        for di, decl in enumerate(rule.declarations):
            comparison = _infer_comparison(decl.property_name)
            val = ", ".join(decl.values)
            con.execute(
                "INSERT INTO declarations VALUES (?, ?, ?, ?, ?)",
                [rule_id, decl.property_name, val, comparison, di],
            )

    # Build cascade_candidates as a VIEW: each selector becomes one SELECT
    # branch in a UNION ALL, joining materialized rules against live entities.
    branches = []
    for row in con.execute("""
        SELECT s.id, s.rule_id, s.selector_text, s.specificity, r.rule_index,
               r.source_file, r.source_line
        FROM selectors s JOIN rules r ON s.rule_id = r.id
        ORDER BY r.rule_index, s.id
    """).fetchall():
        sel_id, rule_id, sel_text, specificity, rule_index, src_file, src_line = row

        # Get declarations for this rule
        decls = con.execute(
            "SELECT property_name, property_value, comparison FROM declarations WHERE rule_id = ?",
            [rule_id],
        ).fetchall()

        # Build WHERE clause from the selector text (re-parse from AST would be cleaner;
        # for now, retrieve the selector from the parsed view and compile)
        where_sql = _selector_to_where(con, sel_text)
        if where_sql is None:
            continue

        for prop_name, prop_value, comparison in decls:
            safe_val = prop_value.replace("'", "''")
            safe_sel = sel_text.replace("'", "''")
            safe_src = (src_file or "").replace("'", "''")
            spec_literal = f"[{','.join(str(s) for s in specificity)}]::INTEGER[]"

            branches.append(f"""
    SELECT e.id AS entity_id, '{prop_name}' AS property_name,
           '{safe_val}' AS property_value, '{comparison}' AS comparison,
           {spec_literal} AS specificity, {rule_index} AS rule_index,
           '{safe_sel}' AS selector_text,
           '{safe_src}' AS source_file, {src_line} AS source_line
    FROM entities e
    WHERE {where_sql}""")

    if branches:
        view_sql = "CREATE VIEW cascade_candidates AS\n" + "\n    UNION ALL BY NAME\n".join(branches)
        con.execute(view_sql)
    else:
        con.execute("CREATE VIEW cascade_candidates AS SELECT NULL::INTEGER AS entity_id WHERE false")


def _selector_to_where(con, sel_text: str) -> str | None:
    """Convert a selector text to a SQL WHERE clause.

    This is a simplified compiler — handles the common patterns.
    A production version would walk the AST instead of re-parsing text.
    """
    parts = sel_text.strip().split()
    if not parts:
        return None

    # The rightmost part is the target; earlier parts are context qualifiers.
    target = parts[-1]
    qualifiers = parts[:-1]

    # Parse the target part
    target_where = _parse_simple_selector_to_sql(target, "e")
    if target_where is None:
        return None

    # Parse context qualifiers as EXISTS subqueries
    qualifier_clauses = []
    for q in qualifiers:
        q_where = _parse_simple_selector_to_sql(q, "q")
        if q_where:
            qualifier_clauses.append(f"EXISTS (SELECT 1 FROM entities q WHERE {q_where})")

    all_clauses = [target_where] + qualifier_clauses
    return " AND ".join(all_clauses)


def _parse_simple_selector_to_sql(sel: str, alias: str) -> str | None:
    """Parse a single simple selector into SQL WHERE fragments."""
    import re

    clauses = []

    # Extract type name (everything before #, ., or [)
    m = re.match(r'^([a-zA-Z_][\w-]*)', sel)
    if m:
        type_name = m.group(1)
        clauses.append(f"{alias}.type_name = '{type_name}'")

    # Extract #id
    m = re.search(r'#([^\s.\[]+)', sel)
    if m:
        clauses.append(f"{alias}.entity_id = '{m.group(1)}'")

    # Extract .class
    for m in re.finditer(r'\.([a-zA-Z_][\w-]*)', sel):
        clauses.append(f"list_contains({alias}.classes, '{m.group(1)}')")

    # Extract [attr op "value"] patterns
    for m in re.finditer(r'\[(\w[\w-]*)(\^=|\$=|\*=|~=|\|=|=)"?([^"\]]*)"?\]', sel):
        attr_name, op, value = m.group(1), m.group(2), m.group(3)
        col = f"{alias}.attributes['{attr_name}']"
        if op == "=":
            clauses.append(f"{col} = '{value}'")
        elif op == "^=":
            clauses.append(f"{col} LIKE '{value}%'")
        elif op == "$=":
            clauses.append(f"{col} LIKE '%{value}'")
        elif op == "*=":
            clauses.append(f"{col} LIKE '%{value}%'")

    # Extract bare [attr] (presence check)
    for m in re.finditer(r'\[(\w+)\](?!=)', sel):
        if not re.search(rf'\[{m.group(1)}[=^$*~|]', sel):
            clauses.append(f"{alias}.attributes['{m.group(1)}'] IS NOT NULL")

    return " AND ".join(clauses) if clauses else None


# ---------------------------------------------------------------------------
# 6. Resolution views + compiler views
# ---------------------------------------------------------------------------

def create_resolution_views(con: duckdb.DuckDBPyConnection) -> None:
    """Create the cascade resolution + typed projections + compiler views."""
    con.execute("""
        -- =================================================================
        -- RESOLUTION: comparison-aware cascade
        -- =================================================================

        CREATE VIEW _resolved_exact AS
            SELECT DISTINCT ON (entity_id, property_name)
                entity_id, property_name, property_value, comparison,
                specificity, rule_index, selector_text, source_file, source_line,
            FROM cascade_candidates
            WHERE comparison = 'exact'
            ORDER BY entity_id, property_name, specificity DESC, rule_index DESC;

        CREATE VIEW _resolved_cap AS
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY entity_id, property_name
                    ORDER BY TRY_CAST(property_value AS INTEGER) ASC, specificity DESC
                ) AS rn
                FROM cascade_candidates WHERE comparison = '<='
            )
            SELECT entity_id, property_name, property_value, comparison,
                   specificity, rule_index, selector_text, source_file, source_line,
            FROM ranked WHERE rn = 1;

        CREATE VIEW _resolved_pattern AS
            WITH agg AS (
                SELECT entity_id, property_name,
                    STRING_AGG(DISTINCT property_value, ', ' ORDER BY property_value) AS property_value,
                    'pattern-in' AS comparison,
                    MAX(specificity) AS specificity, MAX(rule_index) AS rule_index,
                FROM cascade_candidates WHERE comparison = 'pattern-in'
                GROUP BY entity_id, property_name
            )
            SELECT a.*, c.selector_text, c.source_file, c.source_line,
            FROM agg a
            JOIN cascade_candidates c
                ON a.entity_id = c.entity_id AND a.property_name = c.property_name
                AND a.specificity = c.specificity AND a.rule_index = c.rule_index
                AND c.comparison = 'pattern-in';

        CREATE VIEW resolved_properties AS
            SELECT * FROM _resolved_exact
            UNION ALL BY NAME SELECT * FROM _resolved_cap
            UNION ALL BY NAME SELECT * FROM _resolved_pattern;


        -- =================================================================
        -- TYPED PROJECTIONS
        -- =================================================================

        CREATE VIEW files AS
            SELECT e.id, e.entity_id AS path,
                e.attributes['name'] AS name,
                e.attributes['language'] AS language,
                MAX(CASE WHEN rp.property_name = 'editable' THEN rp.property_value END) AS editable,
                MAX(CASE WHEN rp.property_name = 'visible' THEN rp.property_value END) AS visible,
            FROM entities e
            LEFT JOIN resolved_properties rp ON e.id = rp.entity_id
            WHERE e.type_name = 'file'
            GROUP BY e.id, e.entity_id, e.attributes;

        CREATE VIEW tools AS
            SELECT e.id, e.entity_id AS name,
                e.attributes['altitude'] AS altitude,
                e.attributes['level'] AS level,
                MAX(CASE WHEN rp.property_name = 'allow' THEN rp.property_value END) AS allow,
                MAX(CASE WHEN rp.property_name = 'visible' THEN rp.property_value END) AS visible,
                MAX(CASE WHEN rp.property_name = 'max-level' THEN rp.property_value END) AS max_level,
                MAX(CASE WHEN rp.property_name = 'allow-pattern' THEN rp.property_value END) AS allow_pattern,
                MAX(CASE WHEN rp.property_name = 'deny-pattern' THEN rp.property_value END) AS deny_pattern,
            FROM entities e
            LEFT JOIN resolved_properties rp ON e.id = rp.entity_id
            WHERE e.type_name = 'tool'
            GROUP BY e.id, e.entity_id, e.attributes;

        CREATE VIEW modes AS
            SELECT e.id, e.classes AS mode_classes,
                e.attributes['writable'] AS writable,
                e.attributes['strategy'] AS strategy,
            FROM entities e
            WHERE e.type_name = 'mode';


        -- =================================================================
        -- COMPILER VIEWS: nsjail (OS altitude → textproto)
        -- =================================================================

        -- nsjail mount stanzas: one row per bind mount
        CREATE VIEW nsjail_mounts AS
            SELECT
                COALESCE(e.attributes['path'], e.entity_id) AS src,
                '/workspace/' || COALESCE(e.attributes['path'], e.entity_id) AS dst,
                COALESCE(rp.property_value, 'false') = 'true' AS rw,
                'bind' AS mount_type,
            FROM entities e
            LEFT JOIN resolved_properties rp
                ON e.id = rp.entity_id AND rp.property_name = 'editable'
            WHERE e.type_name = 'file'
              AND COALESCE(
                  (SELECT rp2.property_value FROM resolved_properties rp2
                   WHERE rp2.entity_id = e.id AND rp2.property_name = 'visible'),
                  'true') = 'true';

        -- nsjail resource limits
        CREATE VIEW nsjail_rlimits AS
            SELECT
                e.attributes['kind'] AS kind,
                rp.property_value AS limit_value,
            FROM entities e
            JOIN resolved_properties rp ON e.id = rp.entity_id
            WHERE e.type_name = 'resource'
              AND rp.property_name = 'limit';

        -- nsjail network: clone_newnet if any network entity has deny="*"
        CREATE VIEW nsjail_network AS
            SELECT
                bool_or(rp.property_value = '*') AS clone_newnet,
            FROM entities e
            JOIN resolved_properties rp ON e.id = rp.entity_id
            WHERE e.type_name = 'network'
              AND rp.property_name = 'deny';

        -- nsjail exec: PATH from bare exec entity's search-path
        CREATE VIEW nsjail_exec AS
            SELECT
                e.entity_id AS exec_name,
                e.attributes['path'] AS exec_path,
            FROM entities e
            WHERE e.type_name = 'exec';


        -- =================================================================
        -- COMPILER VIEWS: bwrap (OS altitude → argv)
        -- =================================================================

        -- bwrap bind flags: one row per --bind or --ro-bind
        CREATE VIEW bwrap_binds AS
            SELECT
                CASE WHEN rw THEN '--bind' ELSE '--ro-bind' END AS flag,
                src,
                dst,
            FROM nsjail_mounts;

        -- bwrap resource wrappers (prlimit, timeout)
        CREATE VIEW bwrap_wrappers AS
            SELECT
                kind,
                limit_value,
                CASE
                    WHEN kind = 'memory' THEN 'prlimit --as=' || limit_value
                    WHEN kind = 'wall-time' THEN 'timeout ' || limit_value
                    WHEN kind = 'cpu-time' THEN 'prlimit --cpu=' || limit_value
                    ELSE NULL
                END AS wrapper_cmd,
            FROM nsjail_rlimits;

        -- bwrap network: --unshare-net if network denied
        CREATE VIEW bwrap_network AS
            SELECT
                CASE WHEN clone_newnet THEN '--unshare-net' ELSE NULL END AS flag,
            FROM nsjail_network;


        -- =================================================================
        -- COMPILER VIEWS: lackpy-namespace (language altitude → dict)
        -- =================================================================

        CREATE VIEW lackpy_allowed_tools AS
            SELECT name FROM tools WHERE allow = 'true';

        CREATE VIEW lackpy_denied_tools AS
            SELECT name FROM tools WHERE allow = 'false';

        CREATE VIEW lackpy_tool_config AS
            SELECT
                name,
                allow,
                max_level,
                allow_pattern,
                deny_pattern,
            FROM tools;


        -- =================================================================
        -- COMPILER VIEWS: kibitzer (semantic altitude → TOML)
        -- =================================================================

        CREATE VIEW kibitzer_modes AS
            SELECT
                mode_classes[1] AS mode_name,
                writable,
                strategy,
            FROM modes;

        CREATE VIEW kibitzer_tool_surface AS
            SELECT
                cc.selector_text,
                e.entity_id AS tool_name,
                cc.property_value AS allowed,
            FROM cascade_candidates cc
            JOIN entities e ON cc.entity_id = e.id
            WHERE e.type_name = 'tool'
              AND cc.property_name = 'allow'
              AND cc.selector_text LIKE 'mode.%'
            ORDER BY cc.selector_text, e.entity_id;


        -- =================================================================
        -- VERIFICATION ASSERTIONS
        -- =================================================================

        CREATE VIEW assert_a1_unique_winners AS
            SELECT entity_id, property_name, COUNT(*) AS n
            FROM resolved_properties
            GROUP BY entity_id, property_name HAVING n > 1;

        CREATE VIEW assert_c1_provenance AS
            SELECT entity_id, property_name
            FROM resolved_properties
            WHERE source_file IS NULL OR source_file = '';
    """)


# ---------------------------------------------------------------------------
# 7. Reporting
# ---------------------------------------------------------------------------

def print_report(con: duckdb.DuckDBPyConnection) -> None:
    print("\n" + "=" * 72)
    print("RESOLVED WORLD VIEW")
    print("=" * 72)

    # Entity counts (from live providers)
    counts = con.execute("""
        SELECT type_name, COUNT(*) AS n FROM entities GROUP BY type_name ORDER BY n DESC
    """).fetchall()
    print(f"\n--- Entities (live from providers) ---")
    for t, n in counts:
        print(f"  {t:15s} {n:>5d}")
    print(f"  {'TOTAL':15s} {sum(n for _, n in counts):>5d}")

    # Materialized policy
    rules_n = con.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    sels_n = con.execute("SELECT COUNT(*) FROM selectors").fetchone()[0]
    decls_n = con.execute("SELECT COUNT(*) FROM declarations").fetchone()[0]
    print(f"\n--- Policy (materialized) ---")
    print(f"  Rules:        {rules_n}")
    print(f"  Selectors:    {sels_n}")
    print(f"  Declarations: {decls_n}")

    # Cascade
    cand_n = con.execute("SELECT COUNT(*) FROM cascade_candidates").fetchone()[0]
    resolved_n = con.execute("SELECT COUNT(*) FROM resolved_properties").fetchone()[0]
    print(f"\n--- Cascade ---")
    print(f"  Candidates:   {cand_n}")
    print(f"  Resolved:     {resolved_n}")

    # Editable files
    print(f"\n--- Editable files ---")
    rows = con.execute("SELECT path FROM files WHERE editable = 'true' ORDER BY path LIMIT 15").fetchall()
    for (p,) in rows:
        print(f"  {p}")
    more = con.execute("SELECT COUNT(*) FROM files WHERE editable = 'true'").fetchone()[0] - len(rows)
    if more > 0:
        print(f"  ... and {more} more")

    # Tools
    print(f"\n--- Tools ---")
    print(f"  {'name':12s} {'allow':7s} {'vis':5s} {'max-lvl':7s} {'altitude':10s}")
    print(f"  {'─'*12} {'─'*7} {'─'*5} {'─'*7} {'─'*10}")
    for row in con.execute("SELECT name, allow, visible, max_level, altitude FROM tools ORDER BY name").fetchall():
        name, allow, vis, lvl, alt = row
        print(f"  {name or '':12s} {allow or '':7s} {vis or '':5s} {lvl or '':7s} {alt or '':10s}")

    # nsjail summary
    print(f"\n--- nsjail compiler output ---")
    rw = con.execute("SELECT COUNT(*) FROM nsjail_mounts WHERE rw = true").fetchone()[0]
    ro = con.execute("SELECT COUNT(*) FROM nsjail_mounts WHERE rw = false").fetchone()[0]
    print(f"  Mounts: {rw} read-write, {ro} read-only")
    for row in con.execute("SELECT kind, limit_value FROM nsjail_rlimits").fetchall():
        print(f"  rlimit {row[0]}: {row[1]}")
    net = con.execute("SELECT clone_newnet FROM nsjail_network").fetchone()
    if net:
        print(f"  clone_newnet: {net[0]}")

    # bwrap summary
    print(f"\n--- bwrap compiler output ---")
    binds = con.execute("SELECT flag, COUNT(*) FROM bwrap_binds GROUP BY flag ORDER BY flag").fetchall()
    for flag, n in binds:
        print(f"  {flag}: {n} entries")
    for row in con.execute("SELECT wrapper_cmd FROM bwrap_wrappers WHERE wrapper_cmd IS NOT NULL").fetchall():
        print(f"  wrapper: {row[0]}")

    # Verification
    print(f"\n--- Verification ---")
    a1 = con.execute("SELECT COUNT(*) FROM assert_a1_unique_winners").fetchone()[0]
    print(f"  A1 (unique winners):  {'PASS' if a1 == 0 else f'FAIL ({a1})'}")
    c1_missing = con.execute("SELECT COUNT(*) FROM assert_c1_provenance").fetchone()[0]
    print(f"  C1 (provenance):      {resolved_n - c1_missing}/{resolved_n} attributed")

    # Providers
    print(f"\n--- Registered providers ---")
    for row in con.execute("SELECT name, entity_types, source_view FROM providers ORDER BY name").fetchall():
        print(f"  {row[0]:15s} {str(row[1]):30s} → {row[2]}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_selector(selector) -> str:
    parts = []
    for p in selector.parts:
        s = p.selector
        text = s.type_name or "*"
        if s.id_value:
            text += f"#{s.id_value}"
        for cls in s.classes:
            text += f".{cls}"
        for attr in s.attributes:
            if attr.op and attr.value:
                text += f'[{attr.name}{attr.op}"{attr.value}"]'
            else:
                text += f"[{attr.name}]"
        parts.append(text)
    return " ".join(parts)


def _infer_comparison(property_name: str) -> str:
    if property_name.startswith("max-"):
        return "<="
    if property_name.startswith("min-"):
        return ">="
    if property_name in ("allow-pattern", "deny-pattern"):
        return "pattern-in"
    return "exact"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Compile a world view into a DuckDB policy database")
    here = Path(__file__).parent
    parser.add_argument("--project-dir", type=Path,
                        default=Path(__file__).parent.parent.parent.parent / "umwelt")
    parser.add_argument("--policy", type=Path, default=here / "sample-policy.umw")
    parser.add_argument("--tools", type=Path, default=here / "sources" / "tools.json")
    parser.add_argument("--modes", type=Path, default=here / "sources" / "modes.toml")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: not found: {project_dir}", file=sys.stderr)
        return 1

    print(f"Project:  {project_dir}")
    print(f"Policy:   {args.policy}")
    print(f"Output:   {args.output or ':memory:'}")

    # 1. Parse policy
    view = parse_policy(args.policy)
    print(f"Parsed:   {len(view.rules)} rules, {sum(len(r.selectors) for r in view.rules)} selectors")

    # 2. Create database
    con = duckdb.connect(str(args.output) if args.output else ":memory:")
    create_schema(con)
    seed_vocabulary(con)

    # 3. Create live provider views
    create_provider_views(con, project_dir, args.tools, args.modes)

    # 4. Materialize parsed policy + build cascade view
    compile_policy_to_tables(con, view)
    cand_n = con.execute("SELECT COUNT(*) FROM cascade_candidates").fetchone()[0]
    print(f"Cascade:  {cand_n} candidates from {len(view.rules)} rules × live entities")

    # 5. Create resolution + compiler views
    create_resolution_views(con)

    # 6. Report
    print_report(con)

    if args.output:
        print(f"\nDatabase: {args.output}")
        print(f"\nTry:")
        print(f"  duckdb {args.output} \"SELECT * FROM files WHERE editable='true' LIMIT 10\"")
        print(f"  duckdb {args.output} \"SELECT * FROM tools\"")
        print(f"  duckdb {args.output} \"SELECT * FROM nsjail_mounts WHERE rw LIMIT 5\"")
        print(f"  duckdb {args.output} \"SELECT * FROM bwrap_binds LIMIT 5\"")
        print(f"  duckdb {args.output} \"SELECT * FROM kibitzer_modes\"")
        print(f"  duckdb {args.output} \"SELECT * FROM providers\"")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
