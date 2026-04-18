# Selector-to-SQL Compiler

The compiler translates umwelt's CSS-shaped selectors into SQL WHERE clauses that run against the ducklog entity schema. It walks the AST — no regex, no text re-parsing.

## API

```python
from ducklog.compiler import compile_selector, compile_view

# Single selector → SQL WHERE clause
sql = compile_selector(selector)
# Returns: "e.type_name = 'file' AND e.attributes['path'] LIKE 'src/%'"

# Full view → populated DuckDB (cascade_candidates + resolution views)
compile_view(con, view, source_file="policy.umw")
```

## Compilation rules

Each AST node type maps to one SQL pattern:

### Simple selectors (single-part)

| CSS | SQL | Example |
|---|---|---|
| Type | `e.type_name = '<type>'` | `file` → `e.type_name = 'file'` |
| ID | `e.entity_id = '<id>'` | `tool#Bash` → `e.entity_id = 'Bash'` |
| Class | `list_contains(e.classes, '<cls>')` | `mode.implement` → `list_contains(e.classes, 'implement')` |
| `[attr="val"]` | `e.attributes['<attr>'] = '<val>'` | `[path="src/auth.py"]` → exact match |
| `[attr^="val"]` | `e.attributes['<attr>'] LIKE '<val>%'` | `[path^="src/"]` → prefix |
| `[attr$="val"]` | `e.attributes['<attr>'] LIKE '%<val>'` | `[path$=".py"]` → suffix |
| `[attr*="val"]` | `e.attributes['<attr>'] LIKE '%<val>%'` | `[path*="auth"]` → contains |
| `:glob("pat")` | `e.attributes['path'] LIKE '<sql_pat>'` | `:glob("src/*.py")` → `LIKE 'src/%.py'` |

Multiple filters on the same selector are conjoined with AND.

### Compound selectors (multi-part)

The **rightmost** part is the target (matched against `e`). Earlier parts are qualifiers.

**Cross-axis qualifiers** (different taxon from target): become `EXISTS` subqueries.

```css
mode.implement tool[name="Bash"]
```
```sql
e.type_name = 'tool' AND e.attributes['name'] = 'Bash'
AND EXISTS (SELECT 1 FROM entities q0
            WHERE q0.type_name = 'mode'
            AND list_contains(q0.classes, 'implement'))
```

**Structural descendants** (same taxon, parent→child): become closure-table joins.

```css
dir[name="src"] file
```
```sql
e.type_name = 'file'
AND EXISTS (SELECT 1 FROM entities q0
            JOIN entity_closure ec ON ec.ancestor_id = q0.id
            WHERE ec.descendant_id = e.id AND ec.depth > 0
            AND q0.type_name = 'dir'
            AND q0.attributes['name'] = 'src')
```

**Three-axis and beyond**: each additional qualifier adds one more EXISTS clause. Axis count comes from the specificity tuple.

```css
principal#Teague mode.implement tool[name="Bash"]
```
```sql
e.type_name = 'tool' AND e.attributes['name'] = 'Bash'
AND EXISTS (SELECT 1 FROM entities q0 WHERE q0.type_name = 'principal' AND q0.entity_id = 'Teague')
AND EXISTS (SELECT 1 FROM entities q1 WHERE q1.type_name = 'mode' AND list_contains(q1.classes, 'implement'))
```

## Comparison-aware resolution

`compile_view` creates the cascade_candidates view (one SELECT per rule × declaration, UNION ALL BY NAME) and the resolution view stack:

| Comparison | Resolution view | Strategy |
|---|---|---|
| `exact` | `_resolved_exact` | `DISTINCT ON ... ORDER BY specificity DESC, rule_index DESC` |
| `<=` | `_resolved_cap` | `ROW_NUMBER() ... ORDER BY CAST(value AS INTEGER) ASC` — tightest bound wins |
| `pattern-in` | `_resolved_pattern` | `STRING_AGG(DISTINCT ...)` — all patterns aggregate |

`resolved_properties` unifies them via `UNION ALL BY NAME`.

## Glob-to-LIKE conversion

The `:glob()` pseudo-class converts glob patterns to SQL LIKE:

| Glob | LIKE |
|---|---|
| `*` | `%` |
| `?` | `_` |
| `**` | `%` (recursive) |

`src/*.py` → `src/%.py`
`**/*.py` → `%/%.py`

## Test coverage

49 tests organized by complexity level:

1. Type selectors (5 tests)
2. ID selectors (4)
3. Attribute selectors (7)
4. Class selectors (4)
5. Two-axis compounds (5)
6. Three-axis compounds (3)
7. Structural descendants via closure table (3)
8. Pseudo-classes — `:glob()` (3)
9. Round-trip: parse `.umw` → compile → DuckDB → resolve → verify (15)

The round-trip tests verify the full pipeline including comparison-type dispatch (exact, <=, pattern-in), cross-axis specificity ordering, and the A1 assertion (unique winners).
