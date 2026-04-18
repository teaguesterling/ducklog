"""Read a ducklog policy database and produce kibitzer's config dict.

This is the first real consumer of ducklog: kibitzer reads its mode
definitions, tool surfaces, and controller settings from a compiled
policy database instead of (or in addition to) its own TOML config.

The output dict has the same shape as kibitzer's `load_config()` returns,
so it's a drop-in alternative config source.
"""
from __future__ import annotations

from typing import Any

import duckdb


def load_config_from_duckdb(db_path: str | duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Read a ducklog database and return a kibitzer-compatible config dict.

    The returned dict has this shape (matching kibitzer's config.toml):
        {
            "modes": {
                "implement": {"writable": ["src/", "lib/"], "strategy": ""},
                "explore":   {"writable": [],               "strategy": "Map the territory..."},
                ...
            },
            "controller": { ... },  # preserved from defaults if not in the view
            "coach": { ... },       # same
            "tools": {              # EXTENSION: tool surface per mode
                "implement": {"allowed": ["Read", "Edit", ...], "denied": [...]},
                "explore":   {"allowed": ["Read", "Grep", "Glob"], "denied": [...]},
            },
        }
    """
    if isinstance(db_path, str):
        con = duckdb.connect(db_path, read_only=True)
        should_close = True
    else:
        con = db_path
        should_close = False

    try:
        config: dict[str, Any] = {}
        config["modes"] = _read_modes(con)
        config["tools"] = _read_tool_surfaces(con)
        return config
    finally:
        if should_close:
            con.close()


def _read_modes(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """Read mode definitions from the kibitzer_modes view (or entities + resolved_properties)."""
    modes = {}

    # Try the kibitzer_modes convenience view first
    try:
        rows = con.execute("SELECT * FROM kibitzer_modes").fetchall()
        cols = [d[0] for d in con.description]
        for row in rows:
            r = dict(zip(cols, row))
            name = r.get("mode_name")
            if not name:
                continue
            writable_str = r.get("writable", "")
            writable = [w.strip() for w in writable_str.split(",") if w.strip()] if writable_str else []
            modes[name] = {
                "writable": writable,
                "strategy": r.get("strategy", "") or "",
            }
        return modes
    except duckdb.CatalogException:
        pass

    # Fallback: read directly from entities + resolved_properties
    try:
        rows = con.execute("""
            SELECT
                e.classes[1] AS mode_name,
                e.attributes['writable'] AS writable,
                e.attributes['strategy'] AS strategy,
            FROM entities e
            WHERE e.type_name = 'mode'
        """).fetchall()
        for name, writable_str, strategy in rows:
            if not name:
                continue
            writable = [w.strip() for w in (writable_str or "").split(",") if w.strip()] if writable_str else []
            modes[name] = {
                "writable": writable,
                "strategy": strategy or "",
            }
    except duckdb.CatalogException:
        pass

    return modes


def _read_tool_surfaces(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """Read per-mode tool allow/deny from the kibitzer_tool_surface view."""
    surfaces: dict[str, dict[str, list[str]]] = {}

    try:
        rows = con.execute("SELECT * FROM kibitzer_tool_surface").fetchall()
        cols = [d[0] for d in con.description]
        for row in rows:
            r = dict(zip(cols, row))
            tool = r.get("tool_name", "")
            allowed = r.get("allowed", "")

            # Use the mode_name column if available, else extract from selector
            mode_name = r.get("mode_name") or _extract_mode_from_selector(r.get("selector_text", ""))
            if not mode_name or not tool:
                continue

            if mode_name not in surfaces:
                surfaces[mode_name] = {"allowed": [], "denied": []}

            if allowed == "true":
                if tool not in surfaces[mode_name]["allowed"]:
                    surfaces[mode_name]["allowed"].append(tool)
            elif allowed == "false":
                if tool not in surfaces[mode_name]["denied"]:
                    surfaces[mode_name]["denied"].append(tool)
    except duckdb.CatalogException:
        pass

    # Sort for stable output
    for surface in surfaces.values():
        surface["allowed"].sort()
        surface["denied"].sort()

    return surfaces


def _extract_mode_from_selector(selector_text: str) -> str | None:
    """Extract mode class name from a selector like 'mode.implement tool'."""
    for part in selector_text.split():
        if part.startswith("mode."):
            # mode.implement → implement; mode.implement.tdd → implement.tdd
            return part[5:]  # strip "mode."
    return None
