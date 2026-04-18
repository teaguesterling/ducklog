# Provider Guide

How to bring world state into a ducklog policy database.

## What providers do

A provider is a VIEW that populates the `entities` table with things that exist in the world — files, tools, modes, executables, network endpoints. The policy references these entities by type and attributes; providers make them real.

The policy compiles **without knowing what entities exist**. Providers populate entities **without knowing what the policy says**. The cascade resolution joins them.

## The entity contract

Every provider view must produce rows matching this schema:

```sql
SELECT
    <unique_id>    AS id,           -- INTEGER, unique across all providers
    '<taxon>'      AS taxon,        -- 'world', 'capability', 'state', etc.
    '<type>'       AS type_name,    -- 'file', 'tool', 'mode', etc.
    <entity_id>    AS entity_id,    -- VARCHAR, the #id value (nullable)
    <classes>      AS classes,      -- VARCHAR[], class labels (nullable)
    <attrs>        AS attributes,   -- MAP(VARCHAR, VARCHAR), attribute bag
    <parent>       AS parent_id,    -- INTEGER, for hierarchy (nullable)
FROM ...
```

The `entities` view is a UNION ALL BY NAME of all provider views.

## Built-in providers

### Filesystem — `provider_files`

```sql
CREATE VIEW provider_files AS
    SELECT
        row_number() OVER () AS id,
        'world' AS taxon,
        'file' AS type_name,
        replace(file, '/project/', '') AS entity_id,
        NULL::VARCHAR[] AS classes,
        MAP {
            'path': replace(file, '/project/', ''),
            'name': regexp_extract(file, '[^/]+$'),
            'language': CASE
                WHEN file LIKE '%.py' THEN 'python'
                WHEN file LIKE '%.js' THEN 'javascript'
                -- ...
                ELSE ''
            END
        } AS attributes,
        NULL::INTEGER AS parent_id,
    FROM glob('/project/**/*')
    WHERE NOT starts_with(file, '.git/');
```

**Source:** `glob()` over the project directory.
**Attributes:** `path`, `name`, `language` (inferred from extension).
**Selectors that reference this:** `file[path^="src/"]`, `file[language="python"]`, `file:glob("**/*.py")`.

### Tools — `provider_tools`

```sql
CREATE VIEW provider_tools AS
    SELECT
        10000 + row_number() OVER () AS id,
        'capability' AS taxon,
        'tool' AS type_name,
        name AS entity_id,
        NULL::VARCHAR[] AS classes,
        MAP {'name': name, 'altitude': altitude, 'level': level} AS attributes,
        NULL::INTEGER AS parent_id,
    FROM read_json('tools.json') AS t(name, altitude, level);
```

**Source:** JSON manifest, MCP server discovery, `.claude/settings.json`.
**Attributes:** `name`, `altitude`, `level`.
**Selectors:** `tool[name="Bash"]`, `tool[altitude="os"]`.

### Modes — `provider_modes`

```sql
CREATE VIEW provider_modes AS
    SELECT
        20000 + row_number() OVER () AS id,
        'state' AS taxon,
        'mode' AS type_name,
        NULL AS entity_id,
        [mode_name] AS classes,
        MAP {'writable': writable, 'strategy': strategy} AS attributes,
        NULL::INTEGER AS parent_id,
    FROM mode_definitions;  -- from TOML, JSON, or another source
```

**Source:** kibitzer's `config.toml`, a modes JSON file, environment.
**Key feature:** modes use `classes` not `entity_id`. `mode.implement` matches via `list_contains(classes, 'implement')`.
**Selectors:** `mode.implement`, `mode.implement.tdd`.

### Executables — `provider_exec`

```sql
CREATE VIEW provider_exec AS
    SELECT ... FROM (VALUES
        ('bash',    '/bin/bash'),
        ('python3', '/usr/bin/python3'),
        ('git',     '/usr/bin/git'),
    ) AS t(name, path);
```

**Source:** PATH scanning, `which` output, explicit declaration.
**Selectors:** `exec[name="bash"]`, `exec[path="/bin/bash"]`.

## Writing a custom provider

### Example: Docker images

```sql
CREATE VIEW provider_docker_images AS
    SELECT
        40000 + row_number() OVER () AS id,
        'world' AS taxon,
        'image' AS type_name,
        repository || ':' || tag AS entity_id,
        NULL::VARCHAR[] AS classes,
        MAP {
            'repository': repository,
            'tag': tag,
            'size': size::VARCHAR,
        } AS attributes,
        NULL::INTEGER AS parent_id,
    FROM read_json_auto('docker-images.json');
```

Register it in the database and add to the entities union:

```sql
INSERT INTO providers VALUES (
    'docker', 'world', ['image'], 'provider_docker_images',
    'Docker images available on the host'
);

CREATE OR REPLACE VIEW entities AS
    SELECT * FROM provider_files
    UNION ALL BY NAME SELECT * FROM provider_tools
    UNION ALL BY NAME SELECT * FROM provider_modes
    UNION ALL BY NAME SELECT * FROM provider_docker_images;
```

Now policies can reference docker images:

```css
image[repository="python"][tag^="3.12"] { allow: true; }
image { allow: false; }
```

### Example: MCP servers

```sql
CREATE VIEW provider_mcp_servers AS
    SELECT
        50000 + row_number() OVER () AS id,
        'capability' AS taxon,
        'tool' AS type_name,
        server_name || '.' || tool_name AS entity_id,
        NULL::VARCHAR[] AS classes,
        MAP {
            'name': server_name || '.' || tool_name,
            'server': server_name,
            'altitude': 'semantic',
        } AS attributes,
        NULL::INTEGER AS parent_id,
    FROM read_json('.mcp.json', format='auto');
    -- Exact query depends on MCP config structure
```

### Example: Git status

```sql
CREATE VIEW provider_git_status AS
    SELECT
        60000 + row_number() OVER () AS id,
        'world' AS taxon,
        'file' AS type_name,
        path AS entity_id,
        [status] AS classes,  -- ['modified'], ['untracked'], ['staged']
        MAP {'path': path, 'status': status} AS attributes,
        NULL::INTEGER AS parent_id,
    FROM read_csv_auto('git-status.csv');
    -- Generate with: git status --porcelain | awk '{print $2","$1}' > git-status.csv
```

Now policies can reference git state:

```css
file.modified { editable: true; }
file.untracked { visible: false; }
```

## Provider lifecycle

1. **Schema time:** Provider is registered (CREATE VIEW).
2. **Query time:** DuckDB evaluates the view, reading current source data.
3. **No refresh:** Views are always current. A file added to the project appears in the next query.

Providers never INSERT into entities. The entities table IS the UNION ALL of provider views. This is the key architectural invariant.

## Composing providers

Multiple providers can contribute the same entity type. For example, both `provider_files` (from glob) and `provider_git_status` (from git) produce `file` entities. The UNION ALL combines them; the cascade resolves any conflicting properties via specificity.

Be careful with `id` collisions — use offset ranges (files: 1-9999, tools: 10000-19999, modes: 20000-29999, etc.) or `row_number()` with different starting points.
