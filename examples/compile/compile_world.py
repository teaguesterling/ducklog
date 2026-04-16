#!/usr/bin/env python3
"""Compile a world view from diverse sources + a .umw policy into a DuckDB database.

Demonstrates the full ducklog pipeline:
  1. Discover entities from real sources (glob, JSON, TOML)
  2. Parse a .umw policy file (via umwelt's parser)
  3. Populate the policy database schema
  4. Match selectors against entities (selector → SQL compilation)
  5. Resolve the cascade (comparison-aware)
  6. Query the resolved world

Usage:
    python3 compile_world.py [--project-dir DIR] [--policy FILE] [--output FILE]

Defaults to the umwelt project as the target and sample-policy.umw as the policy.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

# ---------------------------------------------------------------------------
# 1. Entity discovery — build the world from diverse sources
# ---------------------------------------------------------------------------


def discover_files(project_dir: Path, max_files: int = 200) -> list[dict]:
    """Glob a project directory for files. Returns entity dicts."""
    entities = []
    for p in sorted(project_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(project_dir))
        skip_prefixes = (".git/", ".mypy_cache/", "__pycache__/", ".pytest_cache/",
                         ".ruff_cache/", "dist/", "site/", ".claude/", ".eggs/")
        if any(rel.startswith(p) for p in skip_prefixes):
            continue
        lang = _infer_language(p.suffix)
        entities.append({
            "taxon": "world",
            "type_name": "file",
            "entity_id": rel,
            "attributes": {"path": rel, "name": p.name, "language": lang or ""},
        })
        if len(entities) >= max_files:
            break
    return entities


def discover_dirs(project_dir: Path) -> list[dict]:
    """Walk a project for directories. Returns entity dicts with parent info."""
    entities = []
    for p in sorted(project_dir.rglob("*")):
        if not p.is_dir():
            continue
        rel = str(p.relative_to(project_dir))
        if rel.startswith(".git"):
            continue
        entities.append({
            "taxon": "world",
            "type_name": "dir",
            "entity_id": rel,
            "attributes": {"path": rel, "name": p.name},
        })
    return entities


def discover_tools(tools_json: Path) -> list[dict]:
    """Read tool definitions from a JSON manifest."""
    data = json.loads(tools_json.read_text())
    return [
        {
            "taxon": "capability",
            "type_name": "tool",
            "entity_id": t["name"],
            "attributes": {
                "name": t["name"],
                "altitude": t.get("altitude", ""),
                "level": str(t.get("level", 0)),
            },
        }
        for t in data.get("tools", [])
    ]


def discover_modes(modes_toml: Path) -> list[dict]:
    """Read mode definitions from a TOML config (kibitzer-style)."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    data = tomllib.loads(modes_toml.read_text())
    entities = []
    for mode_name, config in data.get("modes", {}).items():
        entities.append({
            "taxon": "state",
            "type_name": "mode",
            "entity_id": None,
            "classes": [mode_name],
            "attributes": {
                "writable": ", ".join(config.get("writable", [])),
                "strategy": config.get("strategy", ""),
            },
        })
    return entities


def discover_resources() -> list[dict]:
    """Standard resources every sandbox policy needs."""
    return [
        {"taxon": "world", "type_name": "resource", "entity_id": "memory",
         "attributes": {"kind": "memory"}},
        {"taxon": "world", "type_name": "resource", "entity_id": "wall-time",
         "attributes": {"kind": "wall-time"}},
        {"taxon": "world", "type_name": "resource", "entity_id": "cpu-time",
         "attributes": {"kind": "cpu-time"}},
    ]


def discover_network() -> list[dict]:
    """A single bare network entity for deny-all rules."""
    return [
        {"taxon": "world", "type_name": "network", "entity_id": None,
         "attributes": {}},
    ]


def _infer_language(suffix: str) -> str | None:
    return {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".rs": "rust", ".go": "go", ".md": "markdown", ".toml": "toml",
        ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".sql": "sql",
        ".css": "css", ".html": "html",
    }.get(suffix)


# ---------------------------------------------------------------------------
# 2. Parse the .umw policy — delegate to umwelt's parser
# ---------------------------------------------------------------------------


def parse_policy(policy_path: Path):
    """Parse a .umw file and return the AST rules."""
    from umwelt.sandbox.vocabulary import register_sandbox_vocabulary
    try:
        register_sandbox_vocabulary()
    except Exception:
        pass  # already registered in this scope
    from umwelt.parser import parse
    return parse(policy_path, validate=False)


# ---------------------------------------------------------------------------
# 3. Selector → SQL compilation
# ---------------------------------------------------------------------------


def selector_to_sql(selector, rule_idx: int, declarations: list, source_file: str = "") -> list[str]:
    """Compile one ComplexSelector into SQL INSERT statements for cascade_candidates.

    This is the core of the compilation: a CSS selector becomes a SQL query
    that matches entities and produces candidate rows.
    """
    parts = selector.parts
    if not parts:
        return []

    target = parts[-1]  # rightmost = the matched entity type
    target_type = target.selector.type_name

    # Build the WHERE clause for the target entity
    where_clauses = [f"e.type_name = '{target_type}'"]
    where_clauses.extend(_attr_filters_to_sql(target.selector))

    # Build context-qualifier EXISTS clauses for non-target parts
    exists_clauses = []
    for part in parts[:-1]:
        exists_clauses.append(_context_qualifier_sql(part.selector))

    # Emit one INSERT per declaration
    stmts = []
    for decl_idx, decl in enumerate(declarations):
        comparison = _infer_comparison(decl.property_name)
        spec = list(selector.specificity) if hasattr(selector.specificity, '__iter__') else [0]*8
        spec_literal = f"[{', '.join(str(s) for s in spec)}]"

        where = " AND ".join(where_clauses)
        exists = ""
        if exists_clauses:
            exists = " AND " + " AND ".join(exists_clauses)

        values_joined = ", ".join(decl.values)

        stmts.append(f"""
INSERT INTO cascade_candidates (entity_id, property_name, property_value, comparison,
    specificity, rule_index, selector_text, source_file, source_line)
SELECT e.id, '{decl.property_name}', '{values_joined}', '{comparison}',
    {spec_literal}::INTEGER[], {rule_idx},
    '{_serialize_selector(selector)}',
    '{source_file}',
    {getattr(decl, 'span', None) and decl.span.line or 0}
FROM entities e
WHERE {where}{exists};""")

    return stmts


def _attr_filters_to_sql(simple) -> list[str]:
    """Convert attribute filters on a SimpleSelector to SQL WHERE fragments."""
    clauses = []
    if simple.id_value is not None:
        clauses.append(f"e.entity_id = '{simple.id_value}'")
    for attr in simple.attributes:
        col = f"e.attributes['{attr.name}']"
        if attr.op is None:
            clauses.append(f"{col} IS NOT NULL")
        elif attr.op == "=":
            clauses.append(f"{col} = '{attr.value}'")
        elif attr.op == "^=":
            clauses.append(f"{col} LIKE '{attr.value}%'")
        elif attr.op == "$=":
            clauses.append(f"{col} LIKE '%{attr.value}'")
        elif attr.op == "*=":
            clauses.append(f"{col} LIKE '%{attr.value}%'")
    for cls in simple.classes:
        clauses.append(f"list_contains(e.classes, '{cls}')")
    return clauses


def _context_qualifier_sql(simple) -> str:
    """Build an EXISTS subquery for a cross-axis context qualifier."""
    where = [f"q.type_name = '{simple.type_name}'"]
    where.extend(
        clause.replace("e.", "q.")
        for clause in _attr_filters_to_sql(simple)
    )
    where_str = " AND ".join(where)
    return f"EXISTS (SELECT 1 FROM entities q WHERE {where_str})"


def _infer_comparison(property_name: str) -> str:
    """Infer the comparison type from the property name."""
    if property_name.startswith("max-"):
        return "<="
    if property_name.startswith("min-"):
        return ">="
    if property_name in ("allow-pattern", "deny-pattern"):
        return "pattern-in"
    if property_name.startswith("only-"):
        return "in"
    return "exact"


def _serialize_selector(selector) -> str:
    """Best-effort serialization of a ComplexSelector back to CSS text."""
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
    return " ".join(parts).replace("'", "''")


# ---------------------------------------------------------------------------
# 4. Database creation and population
# ---------------------------------------------------------------------------


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the policy database schema (inline, no external .sql dependency)."""
    con.execute("""
        CREATE TABLE entities (
            id          INTEGER PRIMARY KEY,
            taxon       VARCHAR NOT NULL,
            type_name   VARCHAR NOT NULL,
            entity_id   VARCHAR,
            classes     VARCHAR[],
            attributes  MAP(VARCHAR, VARCHAR),
            parent_id   INTEGER,
            depth       INTEGER DEFAULT 0,
        );

        CREATE TABLE cascade_candidates (
            entity_id       INTEGER NOT NULL,
            property_name   VARCHAR NOT NULL,
            property_value  VARCHAR NOT NULL,
            comparison      VARCHAR NOT NULL DEFAULT 'exact',
            specificity     INTEGER[] NOT NULL,
            rule_index      INTEGER NOT NULL,
            selector_text   VARCHAR,
            source_file     VARCHAR,
            source_line     INTEGER,
        );

        CREATE INDEX idx_ent_type ON entities(type_name);
        CREATE INDEX idx_ent_id ON entities(entity_id);
        CREATE INDEX idx_cc_ep ON cascade_candidates(entity_id, property_name);
    """)


def populate_entities(con: duckdb.DuckDBPyConnection, all_entities: list[dict]) -> None:
    """Insert discovered entities into the database."""
    for i, ent in enumerate(all_entities, start=1):
        attrs = ent.get("attributes", {})
        attr_entries = ", ".join(f"'{k}': '{v}'" for k, v in attrs.items() if v)
        attr_map = f"MAP {{{attr_entries}}}" if attr_entries else "MAP {}"

        classes = ent.get("classes")
        classes_literal = "NULL"
        if classes:
            classes_literal = "[" + ", ".join(f"'{c}'" for c in classes) + "]"

        eid = f"'{ent['entity_id']}'" if ent.get("entity_id") else "NULL"

        con.execute(f"""
            INSERT INTO entities (id, taxon, type_name, entity_id, classes, attributes)
            VALUES ({i}, '{ent['taxon']}', '{ent['type_name']}', {eid},
                    {classes_literal}, {attr_map})
        """)


def compile_rules(con: duckdb.DuckDBPyConnection, view) -> int:
    """Compile parsed rules into cascade_candidates via selector→SQL."""
    total_inserts = 0
    for rule_idx, rule in enumerate(view.rules):
        for selector in rule.selectors:
            src = str(view.source_path or "")
            stmts = selector_to_sql(selector, rule_idx, list(rule.declarations), source_file=src)
            for stmt in stmts:
                try:
                    result = con.execute(stmt)
                    total_inserts += result.fetchone()[0] if result.description else 0
                except Exception as exc:
                    print(f"  WARN: selector compilation failed: {exc}", file=sys.stderr)
                    print(f"        SQL: {stmt[:200]}...", file=sys.stderr)
    return total_inserts


def resolve_cascade(con: duckdb.DuckDBPyConnection) -> None:
    """Create the comparison-aware resolved_properties view."""
    con.execute("""
        CREATE VIEW _resolved_exact AS
            SELECT DISTINCT ON (entity_id, property_name)
                entity_id, property_name, property_value, comparison,
                specificity, rule_index, selector_text, source_file, source_line,
            FROM cascade_candidates
            WHERE comparison = 'exact'
            ORDER BY entity_id, property_name, specificity DESC, rule_index DESC;

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
                   specificity, rule_index, selector_text, source_file, source_line,
            FROM ranked WHERE rn = 1;

        CREATE VIEW _resolved_pattern AS
            WITH agg AS (
                SELECT
                    entity_id, property_name,
                    STRING_AGG(DISTINCT property_value, ', ' ORDER BY property_value) AS property_value,
                    'pattern-in' AS comparison,
                    MAX(specificity) AS specificity,
                    MAX(rule_index) AS rule_index,
                FROM cascade_candidates
                WHERE comparison = 'pattern-in'
                GROUP BY entity_id, property_name
            )
            SELECT a.*, c.selector_text, c.source_file, c.source_line,
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

        -- Typed projections
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
                   MAX(CASE WHEN rp.property_name = 'allow' THEN rp.property_value END) AS allow,
                   MAX(CASE WHEN rp.property_name = 'visible' THEN rp.property_value END) AS visible,
                   MAX(CASE WHEN rp.property_name = 'max-level' THEN rp.property_value END) AS max_level,
                   MAX(CASE WHEN rp.property_name = 'allow-pattern' THEN rp.property_value END) AS allow_pattern,
                   MAX(CASE WHEN rp.property_name = 'deny-pattern' THEN rp.property_value END) AS deny_pattern,
            FROM entities e
            LEFT JOIN resolved_properties rp ON e.id = rp.entity_id
            WHERE e.type_name = 'tool'
            GROUP BY e.id, e.entity_id, e.attributes;
    """)


# ---------------------------------------------------------------------------
# 5. Reporting
# ---------------------------------------------------------------------------


def print_report(con: duckdb.DuckDBPyConnection) -> None:
    """Print a summary of the resolved world."""
    print("\n" + "=" * 72)
    print("RESOLVED WORLD VIEW")
    print("=" * 72)

    # Entity counts
    counts = con.execute("""
        SELECT type_name, COUNT(*) AS n
        FROM entities GROUP BY type_name ORDER BY n DESC
    """).fetchall()
    print(f"\n--- Entity counts ---")
    for type_name, n in counts:
        print(f"  {type_name:20s} {n}")

    # Candidate + resolved counts
    total_cand = con.execute("SELECT COUNT(*) FROM cascade_candidates").fetchone()[0]
    total_resolved = con.execute("SELECT COUNT(*) FROM resolved_properties").fetchone()[0]
    print(f"\n  Cascade candidates:  {total_cand}")
    print(f"  Resolved properties: {total_resolved}")

    # Files
    print(f"\n--- Files (sample) ---")
    rows = con.execute("""
        SELECT path, editable, visible, language
        FROM files
        ORDER BY path
        LIMIT 20
    """).fetchall()
    print(f"  {'path':40s} {'editable':10s} {'visible':10s} {'language':10s}")
    print(f"  {'─'*40} {'─'*10} {'─'*10} {'─'*10}")
    for path, editable, visible, lang in rows:
        print(f"  {path:40s} {editable or '':10s} {visible or '':10s} {lang or '':10s}")
    more = con.execute("SELECT COUNT(*) FROM files").fetchone()[0] - len(rows)
    if more > 0:
        print(f"  ... and {more} more files")

    # Tools
    print(f"\n--- Tools ---")
    rows = con.execute("""
        SELECT name, allow, visible, max_level, altitude, allow_pattern, deny_pattern
        FROM tools ORDER BY name
    """).fetchall()
    print(f"  {'name':12s} {'allow':7s} {'vis':5s} {'lvl':5s} {'altitude':10s} {'allow_pattern':30s} {'deny_pattern':20s}")
    print(f"  {'─'*12} {'─'*7} {'─'*5} {'─'*5} {'─'*10} {'─'*30} {'─'*20}")
    for name, allow, vis, lvl, alt, ap, dp in rows:
        print(f"  {name or '':12s} {allow or '':7s} {vis or '':5s} {lvl or '':5s} {alt or '':10s} {(ap or ''):30s} {(dp or ''):20s}")

    # Mode-tool matrix (the kibitzer view)
    print(f"\n--- Mode × Tool matrix (from cascade) ---")
    print(f"  Showing tool.allow values when mode-gated rules are present.")
    rows = con.execute("""
        SELECT
            cc.selector_text,
            e.entity_id AS tool,
            cc.property_name,
            cc.property_value,
            cc.specificity[1] AS axis_count,
        FROM cascade_candidates cc
        JOIN entities e ON cc.entity_id = e.id
        WHERE e.type_name = 'tool'
          AND cc.property_name = 'allow'
          AND cc.selector_text LIKE 'mode.%'
        ORDER BY cc.selector_text, e.entity_id
        LIMIT 30
    """).fetchall()
    for sel, tool, prop, val, ac in rows:
        print(f"  [{ac}-axis] {sel:45s} → {tool:12s} allow={val}")

    # Verification
    print(f"\n--- Verification assertions ---")
    for view_name in ["assert_a1_unique_winners", "assert_a2_no_ties", "assert_c1_provenance"]:
        try:
            con.execute(f"CREATE VIEW {view_name} AS SELECT 1 WHERE false")
        except Exception:
            pass  # view may already exist
        # Check inline
    a1 = con.execute("""
        SELECT COUNT(*) FROM (
            SELECT entity_id, property_name FROM resolved_properties
            GROUP BY entity_id, property_name HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    print(f"  A1 (unique winners):  {'PASS' if a1 == 0 else f'FAIL ({a1} duplicates)'}")

    c1 = con.execute("""
        SELECT COUNT(*) FROM resolved_properties
        WHERE source_file IS NULL OR source_file = ''
    """).fetchone()[0]
    total_rp = con.execute("SELECT COUNT(*) FROM resolved_properties").fetchone()[0]
    print(f"  C1 (provenance):      {total_rp - c1}/{total_rp} have source attribution")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Compile a world view into a DuckDB policy database")
    here = Path(__file__).parent
    parser.add_argument("--project-dir", type=Path,
                        default=Path(__file__).parent.parent.parent.parent / "umwelt",
                        help="Project directory to scan for files")
    parser.add_argument("--policy", type=Path,
                        default=here / "sample-policy.umw",
                        help="Policy .umw file")
    parser.add_argument("--tools", type=Path,
                        default=here / "sources" / "tools.json",
                        help="Tool manifest JSON")
    parser.add_argument("--modes", type=Path,
                        default=here / "sources" / "modes.toml",
                        help="Mode definitions TOML")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output .duckdb file (default: in-memory)")
    args = parser.parse_args()

    # Resolve project dir
    project_dir = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: project directory not found: {project_dir}", file=sys.stderr)
        return 1
    print(f"Project:  {project_dir}")
    print(f"Policy:   {args.policy}")
    print(f"Tools:    {args.tools}")
    print(f"Modes:    {args.modes}")
    print(f"Output:   {args.output or ':memory:'}")

    # 1. Discover entities
    print(f"\n--- Discovering entities ---")
    all_entities = []

    files = discover_files(project_dir)
    print(f"  Files:     {len(files)}")
    all_entities.extend(files)

    dirs = discover_dirs(project_dir)
    print(f"  Dirs:      {len(dirs)}")
    all_entities.extend(dirs)

    if args.tools.exists():
        tools = discover_tools(args.tools)
        print(f"  Tools:     {len(tools)} (from {args.tools.name})")
        all_entities.extend(tools)

    if args.modes.exists():
        modes = discover_modes(args.modes)
        print(f"  Modes:     {len(modes)} (from {args.modes.name})")
        all_entities.extend(modes)

    resources = discover_resources()
    print(f"  Resources: {len(resources)}")
    all_entities.extend(resources)

    network = discover_network()
    print(f"  Network:   {len(network)}")
    all_entities.extend(network)

    print(f"  TOTAL:     {len(all_entities)} entities")

    # 2. Parse the policy
    print(f"\n--- Parsing policy ---")
    view = parse_policy(args.policy)
    print(f"  Rules:     {len(view.rules)}")
    print(f"  Selectors: {sum(len(r.selectors) for r in view.rules)}")

    # 3. Create database
    db_path = str(args.output) if args.output else ":memory:"
    con = duckdb.connect(db_path)
    create_schema(con)

    # 4. Populate entities
    print(f"\n--- Populating database ---")
    populate_entities(con, all_entities)
    ent_count = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    print(f"  Entities inserted: {ent_count}")

    # 5. Compile rules → cascade_candidates
    print(f"\n--- Compiling selectors → SQL ---")
    compile_rules(con, view)
    cand_count = con.execute("SELECT COUNT(*) FROM cascade_candidates").fetchone()[0]
    print(f"  Cascade candidates: {cand_count}")

    # 6. Resolve
    resolve_cascade(con)

    # 7. Report
    print_report(con)

    # 8. Save if requested
    if args.output:
        print(f"\n  Database written to: {args.output}")
    else:
        # Show a sample query the user could run
        print(f"\n--- Try these queries ---")
        print(f"  duckdb world.duckdb \"SELECT * FROM files WHERE editable = 'true' ORDER BY path\"")
        print(f"  duckdb world.duckdb \"SELECT * FROM tools ORDER BY name\"")
        print(f"  duckdb world.duckdb \"SELECT * FROM resolved_properties WHERE property_name = 'allow-pattern'\"")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
