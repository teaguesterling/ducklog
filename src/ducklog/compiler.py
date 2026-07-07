"""Compile umwelt selectors to SQL WHERE clauses.

Walks umwelt's AST (ComplexSelector, SimpleSelector, CompoundPart) and
emits SQL fragments that match against the ducklog entity schema.

Entry points:
  compile_selector(selector) → SQL WHERE clause string
  compile_view(con, view)    → populates cascade_candidates + resolution views
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb
    from umwelt.ast import ComplexSelector, CompoundPart, SimpleSelector, View


class UnsupportedSelector(ValueError):
    """A selector construct the resolver cannot faithfully express in SQL.

    Raised (fail closed) instead of silently dropping the constraint, which
    would collapse the selector to match every entity of the target type and
    make the compiled audit view diverge from what enforcement actually does.
    """


def compile_selector(selector: ComplexSelector) -> str:
    """Compile a ComplexSelector to a SQL WHERE clause.

    The returned string is a valid SQL expression that can be used as:
        SELECT e.id FROM entities e WHERE <returned_string>

    For compound selectors, the rightmost part is the target (matched
    against `e`); earlier parts become EXISTS subqueries (cross-axis
    context qualifiers) or JOIN entity_closure (structural descendants).
    """
    parts = selector.parts
    if not parts:
        return "FALSE"

    target = parts[-1]
    qualifiers = parts[:-1]

    target_sql = _compile_simple(target.selector, "e")

    qualifier_clauses = []
    for i, qual in enumerate(qualifiers):
        q_alias = f"q{i}"
        is_structural = (
            qual.selector.taxon == target.selector.taxon
            and qual.mode != "context"
        )
        if is_structural:
            qualifier_clauses.append(_compile_structural_ancestor(qual.selector, q_alias))
        else:
            qualifier_clauses.append(_compile_context_qualifier(qual.selector, q_alias))

    all_clauses = [target_sql] + qualifier_clauses
    return " AND ".join(all_clauses)


def _compile_simple(simple: SimpleSelector, alias: str) -> str:
    """Compile a SimpleSelector to SQL WHERE fragments against `alias`."""
    clauses = []

    if simple.type_name and simple.type_name != "*":
        safe_type = simple.type_name.replace("'", "''")
        clauses.append(f"{alias}.type_name = '{safe_type}'")

    if simple.id_value is not None:
        safe_id = simple.id_value.replace("'", "''")
        clauses.append(f"{alias}.entity_id = '{safe_id}'")

    for cls in simple.classes:
        safe_cls = cls.replace("'", "''")
        clauses.append(f"list_contains({alias}.classes, '{safe_cls}')")

    for attr in simple.attributes:
        clauses.append(_compile_attr_filter(attr, alias))

    for pseudo in simple.pseudo_classes:
        # _compile_pseudo raises on anything it cannot express (fail closed):
        # a dropped constraint would collapse the selector to match everything.
        clauses.append(_compile_pseudo(pseudo, alias))

    if not clauses:
        return "TRUE"
    return " AND ".join(clauses)


def _compile_attr_filter(attr, alias: str) -> str:
    """Compile an AttrFilter to a SQL expression.

    Raises UnsupportedSelector for any operator we cannot express, rather than
    emitting a permissive clause (fail closed).
    """
    safe_name = attr.name.replace("'", "''")
    col = f"{alias}.attributes['{safe_name}']"
    if attr.op is None:
        return f"{col} IS NOT NULL"
    safe_val = (attr.value or "").replace("'", "''")
    if attr.op == "=":
        return f"{col} = '{safe_val}'"
    if attr.op == "^=":
        return f"{col} LIKE '{_escape_like(safe_val)}%' ESCAPE '\\'"
    if attr.op == "$=":
        return f"{col} LIKE '%{_escape_like(safe_val)}' ESCAPE '\\'"
    if attr.op == "*=":
        return f"{col} LIKE '%{_escape_like(safe_val)}%' ESCAPE '\\'"
    if attr.op == "~=":
        return f"list_contains(string_split({col}, ' '), '{safe_val}')"
    if attr.op == "|=":
        return (
            f"({col} = '{safe_val}' OR "
            f"{col} LIKE '{_escape_like(safe_val)}-%' ESCAPE '\\')"
        )
    raise UnsupportedSelector(
        f"unsupported attribute operator {attr.op!r} on {attr.name!r}; "
        f"refusing to compile (fail closed)"
    )


def _compile_pseudo(pseudo, alias: str) -> str:
    """Compile a pseudo-class to a SQL expression.

    Raises UnsupportedSelector for any pseudo-class other than :glob. Silently
    dropping it would collapse the selector to match every entity of the target
    type — a fail-open audit view that no longer matches enforcement.
    """
    if pseudo.name == "glob":
        pattern = (pseudo.argument or "").strip().strip("'\"")
        sql_pattern = _glob_to_like(pattern)
        return f"{alias}.attributes['path'] LIKE '{sql_pattern}' ESCAPE '\\'"
    raise UnsupportedSelector(
        f"unsupported pseudo-class ':{pseudo.name}'; refusing to compile "
        f"(fail closed) rather than match every {alias} entity"
    )


def _escape_like(value: str) -> str:
    """Escape SQL LIKE metacharacters so a literal value matches literally.

    Backslash is used as the ESCAPE character in the emitted LIKE clauses, so
    literal `\\`, `%`, and `_` in the value must be backslash-escaped. Without
    this, `[path*="a_b"]` would treat `_` as a single-char wildcard.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _glob_to_like(pattern: str) -> str:
    """Convert a glob pattern to a SQL LIKE pattern.

    Only glob's own wildcards become SQL wildcards; literal LIKE metacharacters
    in the pattern are escaped (backslash is the ESCAPE char in the clause):

    * / ** → %  (match any sequence)
    ?      → _  (match one character)
    literal % _ \\ → escaped so they match literally
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            while i + 1 < n and pattern[i + 1] == "*":
                i += 1  # collapse ** → * (both are recursive % in LIKE)
            out.append("%")
        elif c == "?":
            out.append("_")
        elif c in ("\\", "%", "_"):
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    return "".join(out).replace("'", "''")


def _compile_context_qualifier(simple: SimpleSelector, alias: str) -> str:
    """Compile a cross-axis context qualifier to an EXISTS subquery."""
    where = _compile_simple(simple, alias)
    return f"EXISTS (SELECT 1 FROM entities {alias} WHERE {where})"


def _compile_structural_ancestor(simple: SimpleSelector, alias: str) -> str:
    """Compile a structural-descent qualifier to a closure-table EXISTS.

    dir[name="src"] file  →  the dir is an ancestor of the file in entity_closure.
    """
    where = _compile_simple(simple, alias)
    return (
        f"EXISTS ("
        f"SELECT 1 FROM entities {alias} "
        f"JOIN entity_closure ec ON ec.ancestor_id = {alias}.id "
        f"WHERE ec.descendant_id = e.id AND ec.depth > 0 AND {where}"
        f")"
    )


# ---------------------------------------------------------------------------
# Full view compilation
# ---------------------------------------------------------------------------


def compile_view(con: duckdb.DuckDBPyConnection, view: View, source_file: str = "") -> None:
    """Compile a full parsed View into the database.

    Creates cascade_candidates as a VIEW (UNION ALL of compiled selectors
    against live entities), plus the resolution view stack.
    """
    branches = []
    for rule_idx, rule in enumerate(view.rules):
        for selector in rule.selectors:
            where_sql = compile_selector(selector)
            spec = list(selector.specificity) if hasattr(selector.specificity, "__iter__") else [0] * 8
            spec_literal = f"[{','.join(str(s) for s in spec)}]::INTEGER[]"
            sel_text = _serialize_selector(selector).replace("'", "''")
            safe_src = source_file.replace("'", "''")
            src_line = rule.span.line if hasattr(rule, "span") else 0

            for decl in rule.declarations:
                comparison = _infer_comparison(decl.property_name)
                safe_val = ", ".join(decl.values).replace("'", "''")
                branches.append(f"""
    SELECT e.id AS entity_id,
           '{decl.property_name}' AS property_name,
           '{safe_val}' AS property_value,
           '{comparison}' AS comparison,
           {spec_literal} AS specificity,
           {rule_idx} AS rule_index,
           '{sel_text}' AS selector_text,
           '{safe_src}' AS source_file,
           {src_line} AS source_line
    FROM entities e
    WHERE {where_sql}""")

    if branches:
        view_sql = "CREATE OR REPLACE VIEW cascade_candidates AS\n" + "\n    UNION ALL BY NAME\n".join(branches)
        con.execute(view_sql)
    else:
        con.execute("""
            CREATE OR REPLACE VIEW cascade_candidates AS
            SELECT NULL::INTEGER AS entity_id, NULL::VARCHAR AS property_name,
                   NULL::VARCHAR AS property_value, NULL::VARCHAR AS comparison,
                   NULL::INTEGER[] AS specificity, NULL::INTEGER AS rule_index,
                   NULL::VARCHAR AS selector_text, NULL::VARCHAR AS source_file,
                   NULL::INTEGER AS source_line
            WHERE FALSE
        """)

    _create_resolution_views(con)


def _create_resolution_views(con: duckdb.DuckDBPyConnection) -> None:
    """Create the comparison-aware resolution views."""
    con.execute("""
        CREATE OR REPLACE VIEW _resolved_exact AS
            SELECT DISTINCT ON (entity_id, property_name)
                entity_id, property_name, property_value, comparison,
                specificity, rule_index, selector_text, source_file, source_line
            FROM cascade_candidates
            WHERE comparison = 'exact'
            ORDER BY entity_id, property_name, specificity DESC, rule_index DESC;

        CREATE OR REPLACE VIEW _resolved_cap AS
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY entity_id, property_name
                    ORDER BY TRY_CAST(property_value AS INTEGER) ASC, specificity DESC
                ) AS rn
                FROM cascade_candidates WHERE comparison = '<='
            )
            SELECT entity_id, property_name, property_value, comparison,
                   specificity, rule_index, selector_text, source_file, source_line
            FROM ranked WHERE rn = 1;

        CREATE OR REPLACE VIEW _resolved_pattern AS
            WITH agg AS (
                SELECT entity_id, property_name,
                    STRING_AGG(DISTINCT property_value, ', ' ORDER BY property_value) AS property_value,
                    'pattern-in' AS comparison,
                    MAX(specificity) AS specificity,
                    MAX(rule_index) AS rule_index
                FROM cascade_candidates WHERE comparison = 'pattern-in'
                GROUP BY entity_id, property_name
            )
            SELECT a.entity_id, a.property_name, a.property_value, a.comparison,
                   a.specificity, a.rule_index,
                   c.selector_text, c.source_file, c.source_line
            FROM agg a
            LEFT JOIN cascade_candidates c
                ON a.entity_id = c.entity_id AND a.property_name = c.property_name
                AND a.specificity = c.specificity AND a.rule_index = c.rule_index
                AND c.comparison = 'pattern-in';

        CREATE OR REPLACE VIEW resolved_properties AS
            SELECT * FROM _resolved_exact
            UNION ALL BY NAME SELECT * FROM _resolved_cap
            UNION ALL BY NAME SELECT * FROM _resolved_pattern;
    """)


def _serialize_selector(selector: ComplexSelector) -> str:
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
        for pseudo in s.pseudo_classes:
            if pseudo.argument:
                text += f":{pseudo.name}({pseudo.argument})"
            else:
                text += f":{pseudo.name}"
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
