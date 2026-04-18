# Consumer Guide

How to read policy from a ducklog database in your tool.

## Overview

ducklog produces a `.duckdb` file from an umwelt `.umw` policy. Your tool queries this database for its configuration instead of (or in addition to) its own config format.

```
umwelt compile --target duckdb -o policy.duckdb view.umw
→ your tool reads policy.duckdb
```

## Quick integration

### Option 1: SQL (any language)

```sql
-- What files are editable?
SELECT path, editable FROM files WHERE editable = 'true';

-- What tools are allowed?
SELECT name, allow, max_level FROM tools WHERE allow = 'true';

-- Mode definitions
SELECT * FROM kibitzer_modes;

-- Audit: why is this file editable?
SELECT property_value, specificity, selector_text, source_line
FROM cascade_candidates cc
JOIN entities e ON cc.entity_id = e.id
WHERE e.entity_id = 'src/auth.py' AND cc.property_name = 'editable'
ORDER BY specificity DESC;
```

Works from any DuckDB client: CLI, Python, Node, Rust, Go, R.

### Option 2: Python consumer module

```python
# kibitzer
from ducklog.consumers.kibitzer import load_config_from_duckdb
config = load_config_from_duckdb("policy.duckdb")
# → {"modes": {"implement": {"writable": [...], "strategy": ""}}, "tools": {...}}

# lackpy
from ducklog.consumers.lackpy import load_config_from_duckdb
config = load_config_from_duckdb("policy.duckdb")
# → {"allowed_tools": [...], "denied_tools": [...], "max_level": 3, ...}
```

### Option 3: Fallback (optional dependency)

Add ducklog as an optional dependency. Read from the database when it exists; fall back to your native config otherwise.

```python
def load_config(project_dir):
    config = load_native_config(...)

    policy_db = project_dir / ".config" / "policy.duckdb"
    if policy_db.exists():
        try:
            from ducklog.consumers.your_tool import load_config_from_duckdb
            ducklog_config = load_config_from_duckdb(str(policy_db))
            config = deep_merge(config, ducklog_config)
        except ImportError:
            pass  # ducklog not installed — use native config

    return config
```

## Writing a new consumer module

If your tool isn't kibitzer or lackpy, write a consumer module at `src/ducklog/consumers/your_tool.py`:

```python
"""Read a ducklog policy database and produce your_tool's config dict."""
import duckdb

def load_config_from_duckdb(db_path: str) -> dict:
    con = duckdb.connect(db_path, read_only=True)
    try:
        # Query the views that matter for your tool
        rows = con.execute("SELECT * FROM resolved_properties WHERE ...").fetchall()
        # Transform to your tool's expected config shape
        return {"your_key": transform(rows)}
    finally:
        con.close()
```

The contract: `resolved_properties` has columns `(entity_id, property_name, property_value, comparison, specificity, rule_index, selector_text, source_file, source_line)`. Query it with whatever WHERE clause matches your tool's concerns.

For typed access, use the projection views: `files`, `tools`, `modes`, `uses`.

## Available views

| View | What it contains |
|---|---|
| `entities` | All entities from all providers (live) |
| `cascade_candidates` | All (entity, property) candidates before resolution |
| `resolved_properties` | Cascade winners — the queryable policy |
| `files` | File entities with pivoted properties (path, editable, visible, language) |
| `tools` | Tool entities with pivoted properties (name, allow, visible, max_level, patterns) |
| `modes` | Mode entities with classes, writable paths, strategy |
| `uses` | Action-axis use entities with permissions |
| `nsjail_mounts` | Mount stanzas for nsjail textproto |
| `nsjail_rlimits` | Resource limits for nsjail |
| `nsjail_network` | Network config for nsjail |
| `bwrap_binds` | Bind mount flags for bwrap argv |
| `bwrap_wrappers` | prlimit/timeout wrappers for bwrap |
| `kibitzer_modes` | Mode name, writable paths, strategy |
| `kibitzer_tool_surface` | Per-mode tool allow/deny (cascade winners) |
| `lackpy_tool_config` | Tool name, allow, max_level, patterns |
| `lackpy_allowed_tools` | Just the allowed tool names |
| `lackpy_denied_tools` | Just the denied tool names |

## The workflow

```bash
# 1. Author policy
cat > policy.umw << 'EOF'
file[path^="src/"] { editable: true; }
file { editable: false; }
tool { allow: true; max-level: 3; }
tool[name="Bash"] { allow-pattern: "git *", "pytest *"; }
mode.explore tool { allow: false; }
mode.explore tool[name="Read"] { allow: true; }
EOF

# 2. Compile to database
umwelt compile --target duckdb -o .kibitzer/policy.duckdb policy.umw

# 3. Tools consume automatically
#    kibitzer: reads .kibitzer/policy.duckdb on next session start
#    manual query: duckdb .kibitzer/policy.duckdb "SELECT * FROM tools"
```

## Testing your integration

```python
def test_my_tool_reads_ducklog(tmp_path):
    import duckdb
    from ducklog.compiler import compile_view
    from tests.conftest import parse_view

    # Set up a DB with entities + policy
    db = duckdb.connect(":memory:")
    db.execute("CREATE TABLE entities (...)")  # see conftest.py
    # ... insert entities ...

    view = parse_view('tool { allow: true; }')
    compile_view(db, view)

    # Your consumer
    config = load_config_from_duckdb(db)
    assert "Read" in config["allowed_tools"]
```
