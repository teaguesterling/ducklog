# ducklog

*Policy as a database. Compile umwelt views to queryable DuckDB schemas.*

ducklog compiles [umwelt](https://github.com/teaguesterling/umwelt) `.umw` policy files into DuckDB databases where every authorization decision is a SQL query. The policy is materialized; the world is live views; the resolution is derived. Consumers (kibitzer, nsjail, bwrap, lackpy) query the resolved policy with plain SQL — no Python import, no CSS knowledge required.

```
.umw file → umwelt parse → ducklog compile → world.duckdb → consumers query with SQL
```

## Quick start

```bash
pip install -e .

# Compile a policy against a project directory
python3 examples/compile/compile_world.py \
    --project-dir /path/to/your/project \
    --policy examples/compile/sample-policy.umw \
    --output world.duckdb

# Query the resolved world
duckdb world.duckdb "SELECT path, editable FROM files WHERE editable='true' ORDER BY path"
duckdb world.duckdb "SELECT name, allow, max_level FROM tools"
duckdb world.duckdb "SELECT * FROM nsjail_mounts WHERE rw LIMIT 5"
duckdb world.duckdb "SELECT * FROM kibitzer_modes"
```

## Architecture

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
                   │                      ▼
                   │             files / tools / modes / uses
                   │             (typed projections)
                   │                      │
                   │                      ▼
                   │             nsjail_config / bwrap_config
                   │             lackpy_config / kibitzer_config
                   │             (compiler views per enforcement tool)
```

- **Policy** is materialized (tables) — it's an authored commitment, changes only on recompile
- **World** is live (views over glob/json/toml) — always reflects current state
- **Resolution** is derived (views over both) — always consistent
- **Compiler output** is a projection (views over resolution) — one per enforcement altitude

## The compiler

Translates umwelt CSS selectors to SQL by walking the AST. No regex.

| CSS | SQL |
|---|---|
| `file` | `e.type_name = 'file'` |
| `tool#Bash` | `... AND e.entity_id = 'Bash'` |
| `[path^="src/"]` | `... AND e.attributes['path'] LIKE 'src/%'` |
| `mode.implement` | `... AND list_contains(e.classes, 'implement')` |
| `mode.X tool#Y` | target + `EXISTS (SELECT ... WHERE type='mode' AND ...)` |
| `dir[name="src"] file` | target + closure-table `JOIN entity_closure` |
| `:glob("src/*.py")` | `... AND e.attributes['path'] LIKE 'src/%.py'` |

See [`docs/compiler.md`](docs/compiler.md) for the full compilation rules.

## Comparison-aware cascade

Properties resolve differently based on their comparison type:

| Comparison | Strategy | Example |
|---|---|---|
| `exact` | Highest specificity wins | `editable: true` |
| `<=` | Tightest bound (MIN) | `max-level: 2` caps regardless of specificity |
| `pattern-in` | Union of all rules | `allow-pattern: "git *"` aggregates |

## Tests

49 tests (34 compiler + 15 round-trip):

```bash
python3 -m pytest -v
```

Round-trip tests verify the full pipeline: parse `.umw` → compile → DuckDB → resolve → assert values match. Includes cross-axis specificity ordering, comparison-type dispatch, structural descendants, and the A1 assertion (unique winners).

## Docs

| Document | What it covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Three-layer architecture (materialized/live/derived), compilation, resolution, integration patterns |
| [`docs/compiler.md`](docs/compiler.md) | Selector-to-SQL compilation rules, AST node → SQL mapping |
| [`docs/consumers.md`](docs/consumers.md) | How to read policy from a ducklog database in your tool |
| [`docs/providers.md`](docs/providers.md) | How to bring world state into the database (filesystem, tools, modes, custom) |
| [`docs/rosetta-stone.md`](docs/rosetta-stone.md) | Rosetta Stone: umwelt CSS ↔ SQL ↔ Datalog with 7 worked examples |
| [`schema/policy.sql`](schema/policy.sql) | Full DDL: entities, cascade, resolution, hierarchy, verification assertions |
| [`schema/seed-vocabulary.sql`](schema/seed-vocabulary.sql) | umwelt's registered taxa + entity types + property types as INSERT statements |

## Examples

| Example | What it demonstrates |
|---|---|
| [`01-file-permissions.sql`](examples/01-file-permissions.sql) | Basic cascade, audit query, A2 assertion |
| [`02-mode-tool-cascade.sql`](examples/02-mode-tool-cascade.sql) | Cross-axis (principal × mode × tool), axis_count ordering |
| [`03-diff-two-worlds.sql`](examples/03-diff-two-worlds.sql) | ATTACH-based policy diff, widening detection |
| [`04-hierarchy-descent.sql`](examples/04-hierarchy-descent.sql) | Adjacency list + closure table, structural descendant queries |
| [`05-comparison-types.sql`](examples/05-comparison-types.sql) | exact + <= + pattern-in on one entity |
| [`compile/compile_world.py`](examples/compile/compile_world.py) | End-to-end: glob + JSON + TOML → DuckDB with compiler views |

## Related

- [umwelt](https://github.com/teaguesterling/umwelt) — CSS-shaped policy specification (the source language)
- [sitting_duck](https://github.com/teaguesterling/sitting_duck) — CSS selectors over DuckDB (convergent design — same selector→SQL pattern over code ASTs)
- [blq](https://github.com/teaguesterling/lq) — build log queries in DuckDB (observation pipeline)
- [kibitzer](https://github.com/teaguesterling/kibitzer) — semantic-altitude enforcement (first consumer target)
