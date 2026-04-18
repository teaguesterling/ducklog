"""Read a ducklog policy database and produce lackpy's namespace config dict.

Returns the same shape as umwelt's LackpyNamespaceCompiler.compile():
    {
        "allowed_tools": ["Read", "Edit"],
        "denied_tools": ["Bash"],
        "kits": ["python-dev"],
        "max_level": 3,
        "allow_patterns": {"Bash": ["git *", "pytest *"]},
        "deny_patterns": {"Bash": ["rm -rf *"]},
        "tool_levels": {"Bash": 2},
    }
"""
from __future__ import annotations

from typing import Any

import duckdb


def load_config_from_duckdb(db_path: str | duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Read a ducklog database and return a lackpy-compatible namespace config."""
    if isinstance(db_path, str):
        con = duckdb.connect(db_path, read_only=True)
        should_close = True
    else:
        con = db_path
        should_close = False

    try:
        config: dict[str, Any] = {
            "allowed_tools": [],
            "denied_tools": [],
            "kits": [],
            "max_level": None,
            "allow_patterns": {},
            "deny_patterns": {},
            "tool_levels": {},
        }

        # Read from the tools view if available, else from resolved_properties
        try:
            rows = con.execute("""
                SELECT name, allow, max_level, allow_pattern, deny_pattern
                FROM tools
            """).fetchall()
        except duckdb.CatalogException:
            rows = _fallback_tool_query(con)

        for name, allow, max_level, allow_pattern, deny_pattern in rows:
            if not name:
                continue

            if allow == "true":
                config["allowed_tools"].append(name)
            elif allow == "false":
                config["denied_tools"].append(name)

            if max_level is not None and max_level != "":
                try:
                    level = int(max_level)
                    config["tool_levels"][name] = level
                    # Global max_level is the tightest across all tools
                    if config["max_level"] is None or level < config["max_level"]:
                        config["max_level"] = level
                except ValueError:
                    pass

            if allow_pattern:
                patterns = [p.strip() for p in allow_pattern.split(",") if p.strip()]
                if patterns:
                    config["allow_patterns"][name] = patterns

            if deny_pattern:
                patterns = [p.strip() for p in deny_pattern.split(",") if p.strip()]
                if patterns:
                    config["deny_patterns"][name] = patterns

        config["allowed_tools"].sort()
        config["denied_tools"].sort()
        return config
    finally:
        if should_close:
            con.close()


def _fallback_tool_query(con):
    """Query resolved_properties directly when the tools view doesn't exist."""
    rows = con.execute("""
        SELECT
            e.entity_id AS name,
            MAX(CASE WHEN rp.property_name = 'allow' THEN rp.property_value END),
            MAX(CASE WHEN rp.property_name = 'max-level' THEN rp.property_value END),
            MAX(CASE WHEN rp.property_name = 'allow-pattern' THEN rp.property_value END),
            MAX(CASE WHEN rp.property_name = 'deny-pattern' THEN rp.property_value END),
        FROM entities e
        LEFT JOIN resolved_properties rp ON e.id = rp.entity_id
        WHERE e.type_name = 'tool'
        GROUP BY e.entity_id
    """).fetchall()
    return rows
