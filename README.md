# ducklog

*Policy as a database. Datalog semantics over DuckDB.*

ducklog is the relational backend for [umwelt](https://github.com/teaguesterling/umwelt) — and potentially a standalone Datalog-over-SQL engine for DuckDB.

## What it is

A DuckDB schema and (eventually) extension that models authorization policy as relational tables. CSS-shaped policy declarations (umwelt `.umw` files) compile to facts and rules in the database; consumers query the resolved policy with plain SQL.

```
.umw file → umwelt parse → ducklog schema → consumers query with SQL
```

## Two tracks

### Track 1: Policy database (near-term)

A well-defined DuckDB schema that serves as the intermediate representation between umwelt's CSS parser and every consumer tool (kibitzer, nsjail, bwrap, lackpy, blq, agent-riggs). No extension needed — just tables, views, and `DISTINCT ON` for cascade resolution.

See [`schema/policy.sql`](schema/policy.sql) for the DDL and [`docs/rosetta-stone.md`](docs/rosetta-stone.md) for the full Rosetta Stone (umwelt CSS ↔ SQL ↔ Datalog).

### Track 2: DuckDB Datalog extension (aspirational)

A DuckDB extension (modeled on [duckpgq](https://github.com/cwida/duckpgq-extension)) that adds Datalog syntax to SQL:

```sql
LOAD datalog;

CREATE DATALOG VIEW ancestors AS $$
    ancestor(X, Y) :- parent(X, Y).
    ancestor(X, Z) :- parent(X, Y), ancestor(Y, Z).
$$;

SELECT * FROM ancestors;
```

Internally compiles Datalog rules to `WITH RECURSIVE` CTEs. Standalone value beyond umwelt: graph reachability, program analysis, transitive authorization, any fixed-point computation.

## Status

**Placeholder.** Schema designed; not yet wired to umwelt's resolver. The Rosetta Stone document demonstrates the compilation from umwelt CSS → SQL → Datalog for seven policy patterns.

## Related

- [umwelt](https://github.com/teaguesterling/umwelt) — CSS-shaped policy specification (the source language)
- [duckpgq](https://github.com/cwida/duckpgq-extension) — SQL/PGQ graph queries in DuckDB (architectural template)
- [sitting_duck](https://github.com/teaguesterling/sitting_duck) — CSS selectors over DuckDB (convergent design)
- [blq](https://github.com/teaguesterling/lq) — build log queries in DuckDB (observation pipeline)
