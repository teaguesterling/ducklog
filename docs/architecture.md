# Architecture

ducklog is the relational backend for [umwelt](https://github.com/teaguesterling/umwelt). It compiles CSS-shaped policy files into DuckDB databases where every authorization decision is a SQL query.

## The three layers

```
MATERIALIZED (tables)              LIVE (views)
━━━━━━━━━━━━━━━━━━━━              ━━━━━━━━━━━━━━
                                   provider_files → glob()
rules ─────────────┐               provider_tools → read_json()
selectors          │               provider_modes → read_toml()
declarations ──────┼──────────→  entities (UNION ALL of providers)
                   │                      │
                   │                      ▼
                   │             cascade_candidates
                   │             (compiled selectors × live entities)
                   │                      │
                   │                      ▼
                   │             resolved_properties
                   │             (comparison-aware resolution)
                   │                      │
                   │              ┌───────┴───────┐
                   │              ▼               ▼
                   │         typed views     compiler views
                   │         (files,tools)   (nsjail,bwrap,
                   │                          kibitzer,lackpy)
```

### Layer 1: Materialized policy

**Tables.** The parsed `.umw` file — rules, selectors, declarations. Changes only when the policy is recompiled. This is the authored, committed artifact.

Why materialized: the policy is a commitment. It shouldn't change between queries. Re-parsing a `.umw` file on every SELECT is wasteful and semantically wrong.

### Layer 2: Live providers

**Views over external sources.** Each provider is a CREATE VIEW that reads from glob(), read_json(), read_toml(), or any other DuckDB data source. The `entities` view is a UNION ALL BY NAME of all providers.

Why views: the world changes constantly (files appear, tools are installed, modes are configured). A view is always current without a refresh step. No INSERT, no populate, no stale state.

Providers include:
- **filesystem** — `glob()` over the project directory
- **tools** — `read_json()` over a tool manifest
- **modes** — TOML config (kibitzer-style)
- **exec** — available binaries
- **resources** — standard limits (memory, wall-time, cpu-time)
- **network** — endpoint entities

### Layer 3: Derived resolution

**Views over both layers.** The cascade_candidates view joins materialized selectors against live entities. The resolution views apply comparison-aware winner selection. Typed projections and compiler views sit on top.

Why views: the resolution is a pure derivation. It should always be consistent with the current policy and the current world. Materializing it would require refresh logic; views are always correct by construction.

## The compilation

The compiler walks umwelt's AST and emits SQL. Each selector primitive maps to one SQL pattern:

| CSS primitive | SQL pattern |
|---|---|
| `file` | `e.type_name = 'file'` |
| `#id` | `e.entity_id = 'id'` |
| `.class` | `list_contains(e.classes, 'class')` |
| `[attr="val"]` | `e.attributes['attr'] = 'val'` |
| `[attr^="val"]` | `e.attributes['attr'] LIKE 'val%'` |
| Cross-axis qualifier | `EXISTS (SELECT 1 FROM entities q WHERE ...)` |
| Structural descendant | `EXISTS (... JOIN entity_closure ec ...)` |
| `:glob("pat")` | `e.attributes['path'] LIKE '<converted>'` |

The cascade_candidates view is one UNION ALL BY NAME of all compiled selectors. Each branch is a SELECT that produces candidate rows from live entities.

## Comparison-aware resolution

Not all properties resolve the same way:

| Comparison | Strategy | SQL | Example |
|---|---|---|---|
| `exact` | Highest specificity wins | `DISTINCT ON ... ORDER BY specificity DESC` | `editable: true` |
| `<=` | Tightest bound (MIN) | `ROW_NUMBER() ... ORDER BY CAST(value AS INT) ASC` | `max-level: 2` |
| `>=` | Loosest floor (MAX) | Same, DESC | `min-budget: 256` |
| `pattern-in` | Union of all patterns | `STRING_AGG(DISTINCT ...)` | `allow-pattern: "git *"` |

The `resolved_properties` view unifies all strategies via `UNION ALL BY NAME`.

## Compiler views

Each enforcement tool gets views structured for its native format:

| Tool | Altitude | Views | Consumes |
|---|---|---|---|
| nsjail | OS | `nsjail_mounts`, `nsjail_rlimits`, `nsjail_network` | textproto emitter |
| bwrap | OS | `bwrap_binds`, `bwrap_wrappers`, `bwrap_network` | argv builder |
| lackpy | Language | `lackpy_tool_config`, `lackpy_allowed_tools` | Python dict |
| kibitzer | Semantic | `kibitzer_modes`, `kibitzer_tool_surface` | TOML or direct consumption |

Adding a new enforcement target: write a SQL view over `resolved_properties`, write a format serializer (~30 lines). The policy logic is in the views; the serializer is thin.

## Consumer integration

Consumers read the database, not the `.umw` source. Two integration patterns:

### Pattern 1: SQL queries

```bash
duckdb world.duckdb "SELECT path, editable FROM files WHERE editable='true'"
duckdb world.duckdb "SELECT * FROM kibitzer_modes"
```

Any language with a DuckDB driver. No Python, no umwelt import.

### Pattern 2: Python consumer module

```python
from ducklog.consumers.kibitzer import load_config_from_duckdb
config = load_config_from_duckdb("world.duckdb")
# Returns dict compatible with kibitzer's load_config()
```

Drop-in replacement for existing config loading.

### Pattern 3: Fallback integration (kibitzer example)

```python
# In kibitzer's config.py:
def load_config(project_dir):
    config = load_toml_config(...)  # existing
    policy_db = project_dir / ".kibitzer" / "policy.duckdb"
    if policy_db.exists():
        from ducklog.consumers.kibitzer import load_config_from_duckdb
        config = deep_merge(config, load_config_from_duckdb(str(policy_db)))
    return config
```

Optional dependency. Falls back to TOML when ducklog isn't installed.

## Verification

The database carries its own verification assertions as views:

```sql
-- A1: every (entity, property) has exactly one winner
SELECT COUNT(*) FROM assert_a1_unique_winners;  -- must be 0

-- C1: every resolved property traces to a source line
SELECT COUNT(*) FROM assert_c1_provenance;  -- must be 0
```

The schema IS the verification infrastructure.

## Entity schema

All entities live in one generic table:

```sql
CREATE TABLE entities (
    id         INTEGER PRIMARY KEY,
    taxon      VARCHAR NOT NULL,     -- 'world', 'capability', 'state', ...
    type_name  VARCHAR NOT NULL,     -- 'file', 'tool', 'mode', ...
    entity_id  VARCHAR,              -- #id value
    classes    VARCHAR[],            -- ['.implement', '.tdd']
    attributes MAP(VARCHAR, VARCHAR),-- {path: 'src/auth.py', language: 'python'}
    parent_id  INTEGER,              -- adjacency list for hierarchy
);
```

Typed projection views (files, tools, modes) pivot properties to columns for consumer ergonomics. The generic table is the source of truth; projections are sugar.

## Hierarchy

Parent-child relationships (world → dir → file) use:
- **Adjacency list** (`parent_id` column) as source of truth
- **Closure table** (`entity_closure`) as pre-computed index for descendant queries

Structural descendant selectors (`dir[name="src"] file`) compile to closure-table joins. No recursion at query time.
